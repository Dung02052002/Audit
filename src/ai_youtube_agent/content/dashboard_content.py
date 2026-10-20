"""Content Screen view and service (Prompt Pack v8, prompt Q-194), context X3 Command Center.

Builds the Command Center Content Screen view model and iOS-style HTML rendering,
strictly separating the two content types (``ContentType.SHORTS`` and
``ContentType.LONGFORM``) and grouping each by the ten shared ``ContentStatus``
lifecycle states (``draft``, ``generating``, ``testing``, ``preview_ready``,
``awaiting_approval``, ``approved``, ``publishing``, ``published``, ``rejected``,
``failed``).

Note: ``TEST``, ``QC``, ``PREVIEW``, and ``APPROVAL`` are shared lifecycle/control
stages, never content types.
"""

import html
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final

from ai_youtube_agent.content.approval import ApprovalStatus
from ai_youtube_agent.content.dashboard_shell import (
    BadgeTone,
    DashboardScreenId,
    DashboardShellService,
    DashboardShellState,
    ViewLoadState,
    _select_channel,
    render_dashboard_shell_html,
)
from ai_youtube_agent.core.artifact import ArtifactKind
from ai_youtube_agent.core.content_item import ContentItem, ContentStatus, ContentType
from ai_youtube_agent.core.db.database import Database
from ai_youtube_agent.core.db.repositories.channel import ChannelRepository
from ai_youtube_agent.core.db.repositories.content import (
    ArtifactRepository,
    ContentItemRepository,
)
from ai_youtube_agent.core.db.repositories.publish import PublishJobRepository
from ai_youtube_agent.core.db.repositories.review import ApprovalRequestRepository
from ai_youtube_agent.pipeline.publish import PublishStatus

Clock = Callable[[], datetime]

_STATUS_LABELS: Final[dict[ContentStatus, str]] = {
    ContentStatus.DRAFT: "Draft",
    ContentStatus.GENERATING: "Generating",
    ContentStatus.TESTING: "Testing & QC",
    ContentStatus.PREVIEW_READY: "Preview Ready",
    ContentStatus.AWAITING_APPROVAL: "Awaiting Approval",
    ContentStatus.APPROVED: "Approved",
    ContentStatus.PUBLISHING: "Publishing",
    ContentStatus.PUBLISHED: "Published",
    ContentStatus.REJECTED: "Rejected",
    ContentStatus.FAILED: "Failed",
}

_STATUS_TONES: Final[dict[ContentStatus, BadgeTone]] = {
    ContentStatus.DRAFT: BadgeTone.NEUTRAL,
    ContentStatus.GENERATING: BadgeTone.INFO,
    ContentStatus.TESTING: BadgeTone.INFO,
    ContentStatus.PREVIEW_READY: BadgeTone.INFO,
    ContentStatus.AWAITING_APPROVAL: BadgeTone.WARNING,
    ContentStatus.APPROVED: BadgeTone.SUCCESS,
    ContentStatus.PUBLISHING: BadgeTone.INFO,
    ContentStatus.PUBLISHED: BadgeTone.SUCCESS,
    ContentStatus.REJECTED: BadgeTone.DANGER,
    ContentStatus.FAILED: BadgeTone.DANGER,
}


@dataclass(frozen=True)
class ContentCardItem:
    content_item_id: str
    channel_id: str
    content_type: str
    title: str
    status: str
    tone: BadgeTone
    strategy_profile_id: str | None
    strategy_version: int | None
    latest_video_version: int | None
    youtube_video_id: str | None
    published_at: datetime | None
    pending_approval_id: str | None
    detail_href: str
    created_at: datetime
    updated_at: datetime

    def as_dict(self) -> dict[str, Any]:
        return {
            "content_item_id": self.content_item_id,
            "channel_id": self.channel_id,
            "content_type": self.content_type,
            "title": self.title,
            "status": self.status,
            "tone": self.tone.value,
            "strategy_profile_id": self.strategy_profile_id,
            "strategy_version": self.strategy_version,
            "latest_video_version": self.latest_video_version,
            "youtube_video_id": self.youtube_video_id,
            "published_at": (
                self.published_at.isoformat() if self.published_at else None
            ),
            "pending_approval_id": self.pending_approval_id,
            "detail_href": self.detail_href,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }


@dataclass(frozen=True)
class LifecycleStageGroup:
    status: str
    label: str
    tone: BadgeTone
    items: tuple[ContentCardItem, ...]

    @property
    def count(self) -> int:
        return len(self.items)

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "label": self.label,
            "tone": self.tone.value,
            "count": self.count,
            "items": [item.as_dict() for item in self.items],
        }


@dataclass(frozen=True)
class ContentTypeColumn:
    content_type: str
    label: str
    total_count: int
    stages: tuple[LifecycleStageGroup, ...]

    def stage_for(self, status: ContentStatus | str) -> LifecycleStageGroup:
        key = status.value if isinstance(status, ContentStatus) else str(status)
        for group in self.stages:
            if group.status == key:
                return group
        raise KeyError(f"unknown lifecycle stage {key!r}")

    def as_dict(self) -> dict[str, Any]:
        return {
            "content_type": self.content_type,
            "label": self.label,
            "total_count": self.total_count,
            "stages": [stage.as_dict() for stage in self.stages],
        }


@dataclass(frozen=True)
class ContentScreenView:
    shell: DashboardShellState
    load_state: ViewLoadState
    channel_id: str | None
    selected_content_type: str | None
    selected_status: str | None
    shorts_column: ContentTypeColumn
    longform_column: ContentTypeColumn
    total_items: int
    empty_message: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "shell": self.shell.as_dict(),
            "load_state": self.load_state.value,
            "channel_id": self.channel_id,
            "selected_content_type": self.selected_content_type,
            "selected_status": self.selected_status,
            "shorts_column": self.shorts_column.as_dict(),
            "longform_column": self.longform_column.as_dict(),
            "total_items": self.total_items,
            "empty_message": self.empty_message,
        }


class ContentScreenService:
    """Builds the Content Screen view model separating Shorts and LongForm by lifecycle state."""

    def __init__(
        self,
        database: Database,
        *,
        shell_service: DashboardShellService | None = None,
        clock: Clock | None = None,
    ) -> None:
        self._database = database
        self._clock = clock
        self._shell_service = shell_service or DashboardShellService(
            database, clock=clock
        )

    def load(
        self,
        channel_id: str | None = None,
        *,
        content_type: ContentType | str | None = None,
        status: ContentStatus | str | None = None,
    ) -> ContentScreenView:
        parsed_type = _parse_content_type(content_type)
        parsed_status = _parse_status(status)

        shell = self._shell_service.build_shell(
            DashboardScreenId.CONTENT,
            channel_id=channel_id,
        )
        connection = self._database.connect()
        try:
            channels = ChannelRepository(connection).list_all()
            selected_channel = _select_channel(channels, channel_id)
            if selected_channel is None:
                empty_shorts = _build_column(ContentType.SHORTS, (), parsed_status)
                empty_longform = _build_column(ContentType.LONGFORM, (), parsed_status)
                return ContentScreenView(
                    shell=shell,
                    load_state=ViewLoadState.EMPTY,
                    channel_id=None,
                    selected_content_type=(
                        parsed_type.value if parsed_type else None
                    ),
                    selected_status=parsed_status.value if parsed_status else None,
                    shorts_column=empty_shorts,
                    longform_column=empty_longform,
                    total_items=0,
                    empty_message="No channel configured yet. Create a channel to manage Shorts and LongForm content.",
                )

            items = ContentItemRepository(connection).list_by_channel(
                selected_channel.id
            )
            artifact_repo = ArtifactRepository(connection)
            publish_repo = PublishJobRepository(connection)
            approval_repo = ApprovalRequestRepository(connection)

            cards: list[ContentCardItem] = []
            for item in items:
                if parsed_type is not None and item.content_type is not parsed_type:
                    continue
                if parsed_status is not None and item.status is not parsed_status:
                    continue
                latest_video = artifact_repo.get_latest(item.id, ArtifactKind.VIDEO)
                pub_jobs = publish_repo.list_by_content_item(item.id)
                yt_video_id: str | None = None
                published_at: datetime | None = None
                for job in reversed(pub_jobs):
                    if job.status is PublishStatus.SUCCEEDED and job.result is not None:
                        yt_video_id = job.result.youtube_video_id
                        published_at = job.result.published_at
                        break
                approvals = approval_repo.list_by_content_item(item.id)
                pending_app_id = next(
                    (
                        req.id
                        for req in reversed(approvals)
                        if req.status is ApprovalStatus.PENDING
                    ),
                    None,
                )
                cards.append(
                    ContentCardItem(
                        content_item_id=item.id,
                        channel_id=item.channel_id,
                        content_type=item.content_type.value,
                        title=item.title,
                        status=item.status.value,
                        tone=_STATUS_TONES[item.status],
                        strategy_profile_id=item.strategy_profile_id,
                        strategy_version=item.strategy_version,
                        latest_video_version=(
                            latest_video.version if latest_video else None
                        ),
                        youtube_video_id=yt_video_id,
                        published_at=published_at,
                        pending_approval_id=pending_app_id,
                        detail_href=f"/content/{item.id}",
                        created_at=item.created_at,
                        updated_at=item.updated_at,
                    )
                )
        finally:
            connection.close()

        shorts_cards = [
            c for c in cards if c.content_type == ContentType.SHORTS.value
        ]
        longform_cards = [
            c for c in cards if c.content_type == ContentType.LONGFORM.value
        ]

        shorts_col = _build_column(ContentType.SHORTS, shorts_cards, parsed_status)
        longform_col = _build_column(
            ContentType.LONGFORM, longform_cards, parsed_status
        )
        total = shorts_col.total_count + longform_col.total_count

        return ContentScreenView(
            shell=shell,
            load_state=ViewLoadState.READY if total > 0 else ViewLoadState.EMPTY,
            channel_id=selected_channel.id,
            selected_content_type=parsed_type.value if parsed_type else None,
            selected_status=parsed_status.value if parsed_status else None,
            shorts_column=shorts_col,
            longform_column=longform_col,
            total_items=total,
            empty_message=(
                "No content items match the current filter."
                if total == 0
                else None
            ),
        )

    def render_html(self, view: ContentScreenView) -> str:
        shorts_html = _render_column_html(
            view.shorts_column, testid="content-column-shorts"
        )
        longform_html = _render_column_html(
            view.longform_column, testid="content-column-longform"
        )
        empty_banner = ""
        if view.empty_message:
            empty_banner = (
                f'<p class="yt-empty" data-testid="content-empty-banner">'
                f"{html.escape(view.empty_message)}</p>"
            )

        body = f"""<div class="yt-grid" data-testid="content-screen">
  {empty_banner}
  {shorts_html}
  {longform_html}
</div>"""
        return render_dashboard_shell_html(
            view.shell,
            body_html=body,
            page_title="Content Lifecycle — YouTube Command Center",
        )


def _parse_content_type(value: ContentType | str | None) -> ContentType | None:
    if value is None:
        return None
    if isinstance(value, ContentType):
        return value
    try:
        return ContentType(str(value).strip().lower())
    except ValueError as exc:
        raise ValueError(
            f"content_type must be 'shorts' or 'longform', got {value!r}"
        ) from exc


def _parse_status(value: ContentStatus | str | None) -> ContentStatus | None:
    if value is None:
        return None
    if isinstance(value, ContentStatus):
        return value
    try:
        return ContentStatus(str(value).strip().lower())
    except ValueError as exc:
        raise ValueError(f"unknown ContentStatus {value!r}") from exc


def _build_column(
    content_type: ContentType,
    cards: Sequence[ContentCardItem],
    only_status: ContentStatus | None,
) -> ContentTypeColumn:
    statuses = (only_status,) if only_status is not None else tuple(ContentStatus)
    stages: list[LifecycleStageGroup] = []
    for st in statuses:
        matching = tuple(
            sorted(
                (c for c in cards if c.status == st.value),
                key=lambda item: (item.updated_at, item.content_item_id),
                reverse=True,
            )
        )
        stages.append(
            LifecycleStageGroup(
                status=st.value,
                label=_STATUS_LABELS[st],
                tone=_STATUS_TONES[st],
                items=matching,
            )
        )
    label = "Shorts" if content_type is ContentType.SHORTS else "LongForm"
    return ContentTypeColumn(
        content_type=content_type.value,
        label=label,
        total_count=sum(len(g.items) for g in stages),
        stages=tuple(stages),
    )


def _render_column_html(column: ContentTypeColumn, *, testid: str) -> str:
    stage_blocks: list[str] = []
    for stage in column.stages:
        if stage.items:
            rows = "".join(
                f'<li><a href="{html.escape(item.detail_href)}">{html.escape(item.title)}</a> '
                f'<span class="yt-pill yt-pill--{item.tone.value}">{html.escape(stage.label)}</span>'
                + (
                    f" <small>v{item.latest_video_version}</small>"
                    if item.latest_video_version is not None
                    else ""
                )
                + (
                    f" <small>({html.escape(item.youtube_video_id)})</small>"
                    if item.youtube_video_id
                    else ""
                )
                + "</li>"
                for item in stage.items
            )
            items_html = f"<ul>{rows}</ul>"
        else:
            items_html = '<p class="yt-card__subtitle">No items in this stage.</p>'
        stage_blocks.append(
            f'<div class="yt-stage-group" data-stage="{html.escape(stage.status)}">'
            f"<h3>{html.escape(stage.label)} "
            f'<span class="yt-pill yt-pill--{stage.tone.value}">{stage.count}</span></h3>'
            f"{items_html}"
            f"</div>"
        )
    joined_stages = "".join(stage_blocks)
    return (
        f'<section class="yt-card yt-grid__span-6" data-testid="{html.escape(testid)}">'
        f'<div class="yt-card__header">'
        f'<h2 class="yt-card__title">{html.escape(column.label)}</h2>'
        f'<span class="yt-pill yt-pill--info">{column.total_count} items</span>'
        f"</div>"
        f"{joined_stages}"
        f"</section>"
    )

