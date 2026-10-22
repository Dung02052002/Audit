"""Approval Center UI view and service (Prompt Pack v8, prompt Q-200), context X3
Command Center.

Builds the Command Center Approval Center UI view model and iOS-style HTML
rendering, presenting:
- Pending and historical ``ApprovalRequest`` records for a channel's content items.
- Exact bound ``ArtifactBinding`` versions and SHA-256 checksums (highlighting
  ``VIDEO`` artifact ID, version, and SHA-256).
- Real-time staleness detection via ``stale_kinds`` against the content item's
  latest artifacts, plus TTL expiry status via ``request.is_expired`` and
  ``request.expires_at``.
- Explicit human-only review actions (``approve``, ``reject``, ``request_changes``)
  delegating to ``ApprovalService`` with ``expected_video_artifact_id`` and
  ``expected_video_version`` guards.
"""

import html
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Final

from ai_youtube_agent.content.approval import (
    DEFAULT_APPROVAL_TTL,
    ApprovalRequest,
    ApprovalStatus,
    stale_kinds,
)
from ai_youtube_agent.content.approval_service import (
    ApprovalDecisionResult,
    ApprovalService,
    _latest_artifacts_by_kind,
)
from ai_youtube_agent.content.dashboard_shell import (
    BadgeTone,
    DashboardScreenId,
    DashboardShellService,
    DashboardShellState,
    ViewLoadState,
    _select_channel,
    render_dashboard_shell_html,
)
from ai_youtube_agent.core.artifact import Artifact, ArtifactKind
from ai_youtube_agent.core.audit import Actor, AuditLog
from ai_youtube_agent.core.content_item import ContentItem
from ai_youtube_agent.core.db.database import Database
from ai_youtube_agent.core.db.repositories.audit import SqliteAuditSink
from ai_youtube_agent.core.db.repositories.channel import ChannelRepository
from ai_youtube_agent.core.db.repositories.content import (
    ArtifactRepository,
    ContentItemRepository,
)
from ai_youtube_agent.core.db.repositories.review import ApprovalRequestRepository

Clock = Callable[[], datetime]

_APPROVAL_TONES: Final[dict[ApprovalStatus, BadgeTone]] = {
    ApprovalStatus.PENDING: BadgeTone.WARNING,
    ApprovalStatus.APPROVED: BadgeTone.SUCCESS,
    ApprovalStatus.REJECTED: BadgeTone.DANGER,
    ApprovalStatus.CHANGES_REQUESTED: BadgeTone.WARNING,
    ApprovalStatus.INVALIDATED: BadgeTone.NEUTRAL,
    ApprovalStatus.EXPIRED: BadgeTone.NEUTRAL,
}


@dataclass(frozen=True)
class ApprovalQueueItem:
    approval_request_id: str
    content_item_id: str
    channel_id: str
    content_type: str
    title: str
    content_status: str
    approval_status: str
    tone: BadgeTone
    video_artifact_id: str | None
    video_version: int | None
    video_sha256: str | None
    bound_artifacts: tuple[dict[str, Any], ...]
    stale_artifact_kinds: tuple[str, ...]
    is_stale: bool
    is_expired: bool
    expires_at: datetime
    qc_result_id: str | None
    requested_by_kind: str
    requested_by_id: str
    created_at: datetime
    detail_href: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "approval_request_id": self.approval_request_id,
            "content_item_id": self.content_item_id,
            "channel_id": self.channel_id,
            "content_type": self.content_type,
            "title": self.title,
            "content_status": self.content_status,
            "approval_status": self.approval_status,
            "tone": self.tone.value,
            "video_artifact_id": self.video_artifact_id,
            "video_version": self.video_version,
            "video_sha256": self.video_sha256,
            "bound_artifacts": list(self.bound_artifacts),
            "stale_artifact_kinds": list(self.stale_artifact_kinds),
            "is_stale": self.is_stale,
            "is_expired": self.is_expired,
            "expires_at": self.expires_at.isoformat(),
            "qc_result_id": self.qc_result_id,
            "requested_by_kind": self.requested_by_kind,
            "requested_by_id": self.requested_by_id,
            "created_at": self.created_at.isoformat(),
            "detail_href": self.detail_href,
        }


@dataclass(frozen=True)
class ApprovalCenterView:
    shell: DashboardShellState
    load_state: ViewLoadState
    channel_id: str | None
    pending_count: int
    approved_count: int
    rejected_or_changes_count: int
    expired_or_invalidated_count: int
    pending_approvals: tuple[ApprovalQueueItem, ...]
    historical_approvals: tuple[ApprovalQueueItem, ...]
    all_approvals: tuple[ApprovalQueueItem, ...]
    empty_message: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "shell": self.shell.as_dict(),
            "load_state": self.load_state.value,
            "channel_id": self.channel_id,
            "pending_count": self.pending_count,
            "approved_count": self.approved_count,
            "rejected_or_changes_count": self.rejected_or_changes_count,
            "expired_or_invalidated_count": self.expired_or_invalidated_count,
            "pending_approvals": [a.as_dict() for a in self.pending_approvals],
            "historical_approvals": [a.as_dict() for a in self.historical_approvals],
            "all_approvals": [a.as_dict() for a in self.all_approvals],
            "empty_message": self.empty_message,
        }


class ApprovalCenterService:
    """Builds the Approval Center UI view model, renders HTML, and delegates human
    decisions."""

    def __init__(
        self,
        database: Database,
        *,
        approval_service: ApprovalService | None = None,
        shell_service: DashboardShellService | None = None,
        ttl: timedelta = DEFAULT_APPROVAL_TTL,
        clock: Clock | None = None,
    ) -> None:
        self._database = database
        self._clock = clock
        self._ttl = ttl
        self._approval_service = approval_service or ApprovalService(
            database,
            AuditLog(SqliteAuditSink(database), clock=clock),
            ttl=ttl,
            clock=clock,
        )
        self._shell_service = shell_service or DashboardShellService(
            database, clock=clock
        )

    def load(
        self,
        channel_id: str | None = None,
        *,
        status_filter: ApprovalStatus | str | None = None,
    ) -> ApprovalCenterView:
        parsed_filter = _parse_approval_status(status_filter)
        now = self._now()

        shell = self._shell_service.build_shell(
            DashboardScreenId.APPROVALS,
            channel_id=channel_id,
        )

        connection = self._database.connect()
        try:
            channels = ChannelRepository(connection).list_all()
            selected_channel = _select_channel(channels, channel_id)
            if selected_channel is None:
                return ApprovalCenterView(
                    shell=shell,
                    load_state=ViewLoadState.EMPTY,
                    channel_id=None,
                    pending_count=0,
                    approved_count=0,
                    rejected_or_changes_count=0,
                    expired_or_invalidated_count=0,
                    pending_approvals=(),
                    historical_approvals=(),
                    all_approvals=(),
                    empty_message=(
                        "No pending approvals or channel configured yet. Create a "
                        "channel to review approvals."
                    ),
                )

            items = ContentItemRepository(connection).list_by_channel(
                selected_channel.id
            )
            approvals_repo = ApprovalRequestRepository(connection)
            artifacts_repo = ArtifactRepository(connection)

            records: list[tuple[ContentItem, ApprovalRequest, list[Artifact]]] = []
            for item in items:
                latest_arts = _latest_artifacts_by_kind(
                    artifacts_repo.list_by_content_item(item.id)
                )
                for req in approvals_repo.list_by_content_item(item.id):
                    records.append((item, req, latest_arts))
        finally:
            connection.close()

        pending_count = 0
        approved_count = 0
        rejected_or_changes_count = 0
        expired_or_invalidated_count = 0

        queue_items: list[ApprovalQueueItem] = []
        for item, req, latest_arts in sorted(
            records, key=lambda r: (r[1].created_at, r[1].id), reverse=True
        ):
            if req.status is ApprovalStatus.PENDING:
                pending_count += 1
            elif req.status is ApprovalStatus.APPROVED:
                approved_count += 1
            elif req.status in (
                ApprovalStatus.REJECTED,
                ApprovalStatus.CHANGES_REQUESTED,
            ):
                rejected_or_changes_count += 1
            else:
                expired_or_invalidated_count += 1

            if parsed_filter is not None and req.status is not parsed_filter:
                continue

            queue_items.append(
                _to_queue_item(item, req, latest_arts, now=now, ttl=self._ttl)
            )

        pending_list = tuple(
            q for q in queue_items if q.approval_status == ApprovalStatus.PENDING.value
        )
        historical_list = tuple(
            q for q in queue_items if q.approval_status != ApprovalStatus.PENDING.value
        )

        return ApprovalCenterView(
            shell=shell,
            load_state=ViewLoadState.READY if queue_items else ViewLoadState.EMPTY,
            channel_id=selected_channel.id,
            pending_count=pending_count,
            approved_count=approved_count,
            rejected_or_changes_count=rejected_or_changes_count,
            expired_or_invalidated_count=expired_or_invalidated_count,
            pending_approvals=pending_list,
            historical_approvals=historical_list,
            all_approvals=tuple(queue_items),
            empty_message=(
                "No approval requests match the current filter."
                if not queue_items
                else None
            ),
        )

    def approve(
        self,
        approval_request_id: str,
        *,
        actor: Actor,
        expected_video_artifact_id: str | None = None,
        expected_video_version: int | None = None,
        note: str | None = None,
    ) -> ApprovalDecisionResult:
        return self._approval_service.approve(
            approval_request_id,
            actor=actor,
            expected_video_artifact_id=expected_video_artifact_id,
            expected_video_version=expected_video_version,
            note=note,
        )

    def reject(
        self,
        approval_request_id: str,
        *,
        actor: Actor,
        reason: str,
    ) -> ApprovalDecisionResult:
        return self._approval_service.reject(
            approval_request_id,
            actor=actor,
            reason=reason,
        )

    def request_changes(
        self,
        approval_request_id: str,
        *,
        actor: Actor,
        reason: str,
        target_kinds: Sequence[ArtifactKind] = (),
    ) -> ApprovalDecisionResult:
        return self._approval_service.request_changes(
            approval_request_id,
            actor=actor,
            reason=reason,
            target_kinds=target_kinds,
        )

    def render_html(self, view: ApprovalCenterView) -> str:
        pending_rows = "".join(
            f'<tr data-approval-id="{html.escape(q.approval_request_id)}">'
            f"<td><a "
            f'href="{html.escape(q.detail_href)}">{html.escape(q.title)}</a></td>'
            f"<td>{html.escape(q.content_type)}</td>"
            f"<td>v{q.video_version if q.video_version is not None else '—'}</td>"
            f"<td><code>{html.escape((q.video_sha256 or '')[:12] or '—')}</code></td>"
            f'<td><span class="yt-pill '
            f'yt-pill--{q.tone.value}">{html.escape(q.approval_status)}</span>'
            + (
                ' <span class="yt-pill yt-pill--danger">STALE</span>'
                if q.is_stale
                else ""
            )
            + (
                ' <span class="yt-pill yt-pill--neutral">EXPIRED</span>'
                if q.is_expired
                else ""
            )
            + "</td></tr>"
            for q in view.pending_approvals
        )
        pending_html = (
            '<table class="yt-table" data-testid="approvals-pending-table">'
            "<thead><tr><th>Title</th><th>Type</th><th>Video Version</th>"
            "<th>Video SHA-256</th><th>Status</th></tr></thead>"
            f"<tbody>{pending_rows}</tbody></table>"
            if pending_rows
            else '<p class="yt-empty">No pending approvals waiting for review.</p>'
        )

        history_rows = "".join(
            f"<tr><td>{html.escape(q.title)}</td>"
            f"<td>v{q.video_version if q.video_version is not None else '—'}</td>"
            f"<td><code>{html.escape((q.video_sha256 or '')[:12] or '—')}</code></td>"
            f'<td><span class="yt-pill yt-pill--{q.tone.value}">'
            f"{html.escape(q.approval_status)}</span></td></tr>"
            for q in view.historical_approvals
        )
        history_html = (
            '<table class="yt-table" data-testid="approvals-history-table">'
            "<thead><tr><th>Title</th><th>Video Version</th><th>Video "
            "SHA-256</th><th>Status</th></tr></thead>"
            f"<tbody>{history_rows}</tbody></table>"
            if history_rows
            else '<p class="yt-empty">No past approval decisions yet.</p>'
        )

        body = f"""<div class="yt-grid" data-testid="approval-center-screen">
  <section class="yt-card yt-grid__span-12" data-testid="approvals-pending-card">
    <div class="yt-card__header">
      <div>
        <p class="yt-card__subtitle">Mandatory Human Gate</p>
        <h2 class="yt-card__title">Pending Approvals &amp; Bound Artifact Versions</h2>
      </div>
      <span class="yt-pill yt-pill--warning">{view.pending_count} pending</span>
    </div>
    <p class="yt-card__subtitle">Pending: {view.pending_count} · Approved: \
{view.approved_count} · Rejected/Changes: {view.rejected_or_changes_count} · \
Expired/Invalidated: {view.expired_or_invalidated_count}</p>
    {pending_html}
  </section>

  <section class="yt-card yt-grid__span-12" data-testid="approvals-history-card">
    <div class="yt-card__header">
      <div>
        <p class="yt-card__subtitle">Decision Log</p>
        <h2 class="yt-card__title">Approval History</h2>
      </div>
      <span class="yt-pill yt-pill--neutral">{len(view.historical_approvals)} \
records</span>
    </div>
    {history_html}
  </section>
</div>"""
        return render_dashboard_shell_html(
            view.shell,
            body_html=body,
            page_title="Approval Center — YouTube Command Center",
        )

    def _now(self) -> datetime:
        return self._clock() if self._clock else datetime.now(UTC)


def _parse_approval_status(
    value: ApprovalStatus | str | None,
) -> ApprovalStatus | None:
    if value is None:
        return None
    if isinstance(value, ApprovalStatus):
        return value
    return ApprovalStatus(str(value).strip().lower())


def _to_queue_item(
    item: ContentItem,
    req: ApprovalRequest,
    latest_artifacts: Sequence[Artifact],
    *,
    now: datetime,
    ttl: timedelta,
) -> ApprovalQueueItem:
    video_binding = req.binding_for(ArtifactKind.VIDEO)
    stale = stale_kinds(req, latest_artifacts)
    eval_now = max(now, req.created_at)
    expired = req.is_expired(eval_now, ttl)
    return ApprovalQueueItem(
        approval_request_id=req.id,
        content_item_id=item.id,
        channel_id=item.channel_id,
        content_type=item.content_type.value,
        title=item.title,
        content_status=item.status.value,
        approval_status=req.status.value,
        tone=_APPROVAL_TONES[req.status],
        video_artifact_id=(
            video_binding.artifact_id if video_binding is not None else None
        ),
        video_version=video_binding.version if video_binding is not None else None,
        video_sha256=video_binding.sha256 if video_binding is not None else None,
        bound_artifacts=tuple(b.as_dict() for b in req.artifacts),
        stale_artifact_kinds=tuple(k.value for k in stale),
        is_stale=bool(stale),
        is_expired=expired,
        expires_at=req.expires_at(ttl),
        qc_result_id=req.qc_result_id,
        requested_by_kind=req.requested_by.kind.value,
        requested_by_id=req.requested_by.id,
        created_at=req.created_at,
        detail_href=f"/content/{item.id}",
    )
