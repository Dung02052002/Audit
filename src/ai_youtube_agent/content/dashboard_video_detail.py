"""Video Detail screen view and service (Prompt Pack v8, prompt Q-195), context X3 Command Center.

Builds the Command Center Video Detail screen view model and iOS-style HTML
rendering for a single ``ContentItem``, integrating:
- Real HTML5 video preview player via ``PreviewPlayerService`` and
  ``render_preview_player_html`` (when a rendered ``VIDEO`` artifact exists).
- Exact rendered ``PreviewSnapshot`` and full artifact version history across
  all ``ArtifactKind``s (``video``, ``audio``, ``subtitles``, ``thumbnail``,
  ``metadata``).
- Human-readable ``PreviewMetadataView`` (title, description, tags, thumbnail,
  parsed subtitle cues, and formatted chapters) when metadata is available.
- Complete ``PreviewReportsBundle`` (QC checks, Rights report, AI disclosure
  status, Policy report, ``ready_for_approval``, and ``blocking_reasons``).
- ``VersionComparisonResult`` showing diffs between current and previous
  artifact versions.
- Approval history and publish job status (including YouTube video ID when
  published).
"""

import html
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from ai_youtube_agent.content.approval import ApprovalRequest
from ai_youtube_agent.content.dashboard_content import _STATUS_LABELS, _STATUS_TONES
from ai_youtube_agent.content.dashboard_shell import (
    BadgeTone,
    DashboardScreenId,
    DashboardShellService,
    DashboardShellState,
    ViewLoadState,
    render_dashboard_shell_html,
)
from ai_youtube_agent.content.preview_artifact import (
    PreviewArtifactDescriptor,
    PreviewArtifactService,
    PreviewError,
    PreviewErrorCode,
    PreviewSnapshot,
)
from ai_youtube_agent.content.preview_metadata import (
    PreviewMetadataService,
    PreviewMetadataView,
)
from ai_youtube_agent.content.preview_player import (
    PreviewPlayerConfig,
    PreviewPlayerService,
    render_preview_player_html,
)
from ai_youtube_agent.content.preview_reports import (
    PreviewReportService,
    PreviewReportsBundle,
)
from ai_youtube_agent.content.version_comparison import (
    VersionComparisonResult,
    VersionComparisonService,
)
from ai_youtube_agent.core.artifact import Artifact, ArtifactKind
from ai_youtube_agent.core.artifact_storage import ArtifactStorage
from ai_youtube_agent.core.config import Settings
from ai_youtube_agent.core.content_item import ContentItem
from ai_youtube_agent.core.db.database import Database
from ai_youtube_agent.core.db.repositories.content import (
    ArtifactRepository,
    ContentItemRepository,
)
from ai_youtube_agent.core.db.repositories.publish import PublishJobRepository
from ai_youtube_agent.core.db.repositories.review import ApprovalRequestRepository
from ai_youtube_agent.pipeline.publish import PublishJob

Clock = Callable[[], datetime]


@dataclass(frozen=True)
class VideoDetailView:
    shell: DashboardShellState
    load_state: ViewLoadState
    content_item_id: str
    channel_id: str
    content_type: str
    title: str
    status: str
    status_label: str
    status_tone: BadgeTone
    strategy_profile_id: str | None
    strategy_version: int | None
    created_at: datetime
    updated_at: datetime
    preview_snapshot: PreviewSnapshot | None
    player_config: PreviewPlayerConfig | None
    metadata_view: PreviewMetadataView | None
    reports: PreviewReportsBundle
    version_comparison: VersionComparisonResult | None
    all_artifacts: tuple[PreviewArtifactDescriptor, ...]
    approvals: tuple[ApprovalRequest, ...]
    publish_jobs: tuple[PublishJob, ...]
    preview_unavailable_reason: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "shell": self.shell.as_dict(),
            "load_state": self.load_state.value,
            "content_item_id": self.content_item_id,
            "channel_id": self.channel_id,
            "content_type": self.content_type,
            "title": self.title,
            "status": self.status,
            "status_label": self.status_label,
            "status_tone": self.status_tone.value,
            "strategy_profile_id": self.strategy_profile_id,
            "strategy_version": self.strategy_version,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "preview_snapshot": (
                self.preview_snapshot.as_dict() if self.preview_snapshot else None
            ),
            "player_config": (
                self.player_config.as_dict() if self.player_config else None
            ),
            "metadata_view": (
                self.metadata_view.as_dict() if self.metadata_view else None
            ),
            "reports": self.reports.as_dict(),
            "version_comparison": (
                self.version_comparison.as_dict()
                if self.version_comparison
                else None
            ),
            "all_artifacts": [art.as_dict() for art in self.all_artifacts],
            "approvals": [req.as_dict() for req in self.approvals],
            "publish_jobs": [job.as_dict() for job in self.publish_jobs],
            "preview_unavailable_reason": self.preview_unavailable_reason,
        }


class VideoDetailService:
    """Loads the complete Video Detail view and renders its HTML."""

    def __init__(
        self,
        database: Database,
        storage: ArtifactStorage,
        settings: Settings,
        *,
        shell_service: DashboardShellService | None = None,
        artifact_service: PreviewArtifactService | None = None,
        player_service: PreviewPlayerService | None = None,
        metadata_service: PreviewMetadataService | None = None,
        report_service: PreviewReportService | None = None,
        comparison_service: VersionComparisonService | None = None,
        clock: Clock | None = None,
    ) -> None:
        self._database = database
        self._storage = storage
        self._settings = settings
        self._clock = clock
        self._shell_service = shell_service or DashboardShellService(
            database, clock=clock
        )
        self._artifact_service = artifact_service or PreviewArtifactService(
            database, storage
        )
        self._player_service = player_service or PreviewPlayerService(
            database, storage, artifact_service=self._artifact_service
        )
        self._metadata_service = metadata_service or PreviewMetadataService(
            database, storage, artifact_service=self._artifact_service
        )
        self._report_service = report_service or PreviewReportService(
            database, storage, settings
        )
        self._comparison_service = comparison_service or VersionComparisonService(
            database, storage
        )

    def load(
        self,
        content_item_id: str,
        *,
        video_version: int | None = None,
    ) -> VideoDetailView:
        connection = self._database.connect()
        try:
            item = ContentItemRepository(connection).get(content_item_id)
            if item is None:
                raise PreviewError(
                    f"content item {content_item_id} not found",
                    code=PreviewErrorCode.ITEM_NOT_FOUND,
                )
            raw_artifacts = ArtifactRepository(connection).list_by_content_item(
                content_item_id
            )
            approvals = tuple(
                ApprovalRequestRepository(connection).list_by_content_item(
                    content_item_id
                )
            )
            publish_jobs = tuple(
                PublishJobRepository(connection).list_by_content_item(content_item_id)
            )
        finally:
            connection.close()

        shell = self._shell_service.build_shell(
            DashboardScreenId.CONTENT,
            channel_id=item.channel_id,
        )

        latest_version_by_kind: dict[ArtifactKind, int] = {}
        for art in raw_artifacts:
            prev = latest_version_by_kind.get(art.kind, 0)
            if art.version > prev:
                latest_version_by_kind[art.kind] = art.version

        descriptors = tuple(
            PreviewArtifactDescriptor.from_artifact(
                art,
                is_latest=art.version == latest_version_by_kind.get(art.kind, 0),
            )
            for art in raw_artifacts
        )

        snapshot: PreviewSnapshot | None = None
        player_config: PreviewPlayerConfig | None = None
        metadata_view: PreviewMetadataView | None = None
        version_comparison: VersionComparisonResult | None = None
        unavailable_reason: str | None = None

        has_video = any(art.kind is ArtifactKind.VIDEO for art in raw_artifacts)
        if has_video or video_version is not None:
            snapshot = self._artifact_service.get_snapshot(
                content_item_id, video_version=video_version
            )
            player_config = self._player_service.build_player(
                content_item_id, video_version=video_version
            )
            if snapshot.metadata is not None:
                try:
                    metadata_view = self._metadata_service.get_metadata(
                        content_item_id, video_version=video_version
                    )
                except PreviewError:
                    metadata_view = None
            version_comparison = self._comparison_service.compare(
                content_item_id, target_version=video_version
            )
        else:
            unavailable_reason = (
                "No rendered VIDEO artifact exists yet for this content item."
            )

        reports = self._report_service.get_reports(content_item_id)

        return VideoDetailView(
            shell=shell,
            load_state=ViewLoadState.READY,
            content_item_id=item.id,
            channel_id=item.channel_id,
            content_type=item.content_type.value,
            title=item.title,
            status=item.status.value,
            status_label=_STATUS_LABELS[item.status],
            status_tone=_STATUS_TONES[item.status],
            strategy_profile_id=item.strategy_profile_id,
            strategy_version=item.strategy_version,
            created_at=item.created_at,
            updated_at=item.updated_at,
            preview_snapshot=snapshot,
            player_config=player_config,
            metadata_view=metadata_view,
            reports=reports,
            version_comparison=version_comparison,
            all_artifacts=descriptors,
            approvals=approvals,
            publish_jobs=publish_jobs,
            preview_unavailable_reason=unavailable_reason,
        )

    def render_html(self, view: VideoDetailView) -> str:
        if view.player_config is not None:
            player_html = render_preview_player_html(view.player_config)
        else:
            reason = html.escape(
                view.preview_unavailable_reason or "Preview player unavailable."
            )
            player_html = (
                f'<p class="yt-empty" data-testid="video-preview-unavailable">'
                f"{reason}</p>"
            )

        if view.all_artifacts:
            artifact_rows = "".join(
                f"<tr><td>{html.escape(a.kind.value)}</td>"
                f"<td>v{a.version}{' (latest)' if a.is_latest else ''}</td>"
                f"<td><code>{html.escape(a.sha256[:12])}</code></td>"
                f"<td>{a.size_bytes} B</td>"
                f"<td>{html.escape(a.media_type)}</td>"
                f'<td><a href="{html.escape(a.stream_url)}">Stream</a></td></tr>'
                for a in view.all_artifacts
            )
            artifacts_html = (
                '<table class="yt-table" data-testid="video-artifacts-table">'
                "<thead><tr><th>Kind</th><th>Version</th><th>SHA-256</th>"
                "<th>Size</th><th>Media Type</th><th>Link</th></tr></thead>"
                f"<tbody>{artifact_rows}</tbody></table>"
            )
        else:
            artifacts_html = '<p class="yt-empty">No artifacts recorded yet.</p>'

        qc = view.reports.qc
        qc_status = qc.status.value if qc else "missing"
        rights_status = (
            "Cleared" if view.reports.rights_report.cleared_for_publish else "Blocked"
        )
        policy_status = view.reports.policy_report.overall_status.value
        ready_badge = (
            '<span class="yt-pill yt-pill--success">Ready for Approval</span>'
            if view.reports.ready_for_approval
            else '<span class="yt-pill yt-pill--danger">Blocked</span>'
        )
        blocking_html = (
            "<ul>"
            + "".join(
                f"<li>{html.escape(r)}</li>" for r in view.reports.blocking_reasons
            )
            + "</ul>"
            if view.reports.blocking_reasons
            else '<p class="yt-card__subtitle">No blocking findings.</p>'
        )

        meta_html = ""
        if view.metadata_view is not None:
            mv = view.metadata_view
            tags_str = ", ".join(html.escape(t) for t in mv.tags) or "None"
            meta_html = (
                f'<div data-testid="video-metadata-summary">'
                f"<p><strong>Title:</strong> {html.escape(mv.title)}</p>"
                f"<p><strong>Description:</strong> {html.escape(mv.description)}</p>"
                f"<p><strong>Tags:</strong> {tags_str}</p>"
                f"<p><strong>Synthetic media disclosure:</strong> "
                f"{'Yes' if mv.contains_synthetic_media else 'No'}</p>"
                f"</div>"
            )

        body = f"""<div class="yt-grid" data-testid="video-detail-screen">
  <section class="yt-card yt-grid__span-8" data-testid="video-preview-card">
    <div class="yt-card__header">
      <div>
        <p class="yt-card__subtitle">{html.escape(view.content_type.upper())} · {html.escape(view.content_item_id)}</p>
        <h2 class="yt-card__title">{html.escape(view.title)}</h2>
      </div>
      <span class="yt-pill yt-pill--{view.status_tone.value}">{html.escape(view.status_label)}</span>
    </div>
    {player_html}
    {meta_html}
  </section>

  <section class="yt-card yt-grid__span-4" data-testid="video-reports-card">
    <div class="yt-card__header">
      <div>
        <p class="yt-card__subtitle">Verification Reports</p>
        <h2 class="yt-card__title">QC · Rights · Policy</h2>
      </div>
      {ready_badge}
    </div>
    <p class="yt-card__subtitle">QC: <strong>{html.escape(qc_status)}</strong> · Rights: <strong>{html.escape(rights_status)}</strong> · Policy: <strong>{html.escape(policy_status)}</strong></p>
    {blocking_html}
  </section>

  <section class="yt-card yt-grid__span-12" data-testid="video-artifacts-card">
    <div class="yt-card__header">
      <div>
        <p class="yt-card__subtitle">Immutable Artifact Versions</p>
        <h2 class="yt-card__title">All Video Artifacts &amp; Checksums</h2>
      </div>
      <span class="yt-pill yt-pill--info">{len(view.all_artifacts)} artifacts</span>
    </div>
    {artifacts_html}
  </section>
</div>"""
        return render_dashboard_shell_html(
            view.shell,
            body_html=body,
            page_title=f"{view.title} — Video Detail",
        )

