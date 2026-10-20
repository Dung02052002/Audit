"""Comments Screen view and service (Prompt Pack v8, prompt Q-198), context X3 Command Center.

Builds the Command Center Comments Screen view model and iOS-style HTML rendering,
presenting:
- Synced ``Comment`` records with their latest ``CommentClassification``
  (``needs_attention``, ``moderation``, ``reply_candidate``, ``no_action``, or
  ``unclassified`` when newly synced).
- Filtered queues for new (unclassified) comments, attention/moderation/reply-candidate
  comments, and reply drafts across ``ReplyStatus`` (``draft``, ``approved``,
  ``posted``, ``failed``, ``discarded``).
- Explicit ``auto_reply_enabled`` status indicator (``False`` by default) and
  human-only review actions via ``ReplyApprovalService`` (no autonomous reply
  posting when ``auto_reply_enabled`` is false).
"""

import html
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final

from ai_youtube_agent.content.comment import (
    Comment,
    CommentClassification,
    CommentLabel,
    ReplyDraft,
    ReplyStatus,
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
from ai_youtube_agent.content.reply_approval import (
    ReplyApprovalOutcome,
    ReplyApprovalService,
)
from ai_youtube_agent.core.audit import Actor
from ai_youtube_agent.core.db.database import Database
from ai_youtube_agent.core.db.repositories.channel import ChannelRepository
from ai_youtube_agent.core.db.repositories.community import CommentRepository
from ai_youtube_agent.core.flags import FeatureFlags

Clock = Callable[[], datetime]

_LABEL_TONES: Final[dict[str, BadgeTone]] = {
    CommentLabel.NEEDS_ATTENTION.value: BadgeTone.WARNING,
    CommentLabel.MODERATION.value: BadgeTone.DANGER,
    CommentLabel.REPLY_CANDIDATE.value: BadgeTone.INFO,
    CommentLabel.NO_ACTION.value: BadgeTone.NEUTRAL,
    "unclassified": BadgeTone.INFO,
}

_DRAFT_TONES: Final[dict[ReplyStatus, BadgeTone]] = {
    ReplyStatus.DRAFT: BadgeTone.WARNING,
    ReplyStatus.APPROVED: BadgeTone.INFO,
    ReplyStatus.POSTED: BadgeTone.SUCCESS,
    ReplyStatus.FAILED: BadgeTone.DANGER,
    ReplyStatus.DISCARDED: BadgeTone.NEUTRAL,
}


@dataclass(frozen=True)
class ReplyDraftViewItem:
    draft_id: str
    comment_id: str
    text: str
    status: str
    tone: BadgeTone
    created_by_kind: str
    created_by_id: str
    created_at: datetime
    youtube_reply_id: str | None

    @classmethod
    def from_draft(cls, draft: ReplyDraft) -> "ReplyDraftViewItem":
        return cls(
            draft_id=draft.id,
            comment_id=draft.comment_id,
            text=draft.text,
            status=draft.status.value,
            tone=_DRAFT_TONES[draft.status],
            created_by_kind=draft.created_by.kind.value,
            created_by_id=draft.created_by.id,
            created_at=draft.created_at,
            youtube_reply_id=draft.youtube_reply_id,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "draft_id": self.draft_id,
            "comment_id": self.comment_id,
            "text": self.text,
            "status": self.status,
            "tone": self.tone.value,
            "created_by_kind": self.created_by_kind,
            "created_by_id": self.created_by_id,
            "created_at": self.created_at.isoformat(),
            "youtube_reply_id": self.youtube_reply_id,
        }


@dataclass(frozen=True)
class CommentThreadViewItem:
    comment_id: str
    channel_id: str
    youtube_comment_id: str
    youtube_video_id: str
    parent_comment_id: str | None
    author_display_name: str
    text: str
    published_at: datetime
    label: str
    label_tone: BadgeTone
    rationale: str | None
    classified_at: datetime | None
    is_new_unclassified: bool
    requires_attention: bool
    reply_drafts: tuple[ReplyDraftViewItem, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "comment_id": self.comment_id,
            "channel_id": self.channel_id,
            "youtube_comment_id": self.youtube_comment_id,
            "youtube_video_id": self.youtube_video_id,
            "parent_comment_id": self.parent_comment_id,
            "author_display_name": self.author_display_name,
            "text": self.text,
            "published_at": self.published_at.isoformat(),
            "label": self.label,
            "label_tone": self.label_tone.value,
            "rationale": self.rationale,
            "classified_at": (
                self.classified_at.isoformat() if self.classified_at else None
            ),
            "is_new_unclassified": self.is_new_unclassified,
            "requires_attention": self.requires_attention,
            "reply_drafts": [d.as_dict() for d in self.reply_drafts],
        }


@dataclass(frozen=True)
class CommentsScreenView:
    shell: DashboardShellState
    load_state: ViewLoadState
    channel_id: str | None
    auto_reply_enabled: bool
    total_comments: int
    new_unclassified_count: int
    needs_attention_count: int
    moderation_count: int
    reply_candidate_count: int
    pending_draft_count: int
    approved_draft_count: int
    posted_reply_count: int
    attention_comments: tuple[CommentThreadViewItem, ...]
    new_comments: tuple[CommentThreadViewItem, ...]
    all_comments: tuple[CommentThreadViewItem, ...]
    reply_drafts: tuple[ReplyDraftViewItem, ...]
    empty_message: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "shell": self.shell.as_dict(),
            "load_state": self.load_state.value,
            "channel_id": self.channel_id,
            "auto_reply_enabled": self.auto_reply_enabled,
            "total_comments": self.total_comments,
            "new_unclassified_count": self.new_unclassified_count,
            "needs_attention_count": self.needs_attention_count,
            "moderation_count": self.moderation_count,
            "reply_candidate_count": self.reply_candidate_count,
            "pending_draft_count": self.pending_draft_count,
            "approved_draft_count": self.approved_draft_count,
            "posted_reply_count": self.posted_reply_count,
            "attention_comments": [c.as_dict() for c in self.attention_comments],
            "new_comments": [c.as_dict() for c in self.new_comments],
            "all_comments": [c.as_dict() for c in self.all_comments],
            "reply_drafts": [d.as_dict() for d in self.reply_drafts],
            "empty_message": self.empty_message,
        }


class CommentsScreenService:
    """Builds the Comments Screen view model, renders HTML, and exposes human draft review actions."""

    def __init__(
        self,
        database: Database,
        *,
        flags: FeatureFlags | None = None,
        reply_approval_service: ReplyApprovalService | None = None,
        shell_service: DashboardShellService | None = None,
        clock: Clock | None = None,
    ) -> None:
        self._database = database
        self._flags = flags or FeatureFlags()
        self._reply_approval = reply_approval_service
        self._clock = clock
        self._shell_service = shell_service or DashboardShellService(
            database, clock=clock
        )

    def load(
        self,
        channel_id: str | None = None,
        *,
        label_filter: CommentLabel | str | None = None,
        draft_status_filter: ReplyStatus | str | None = None,
    ) -> CommentsScreenView:
        parsed_label = _parse_label_filter(label_filter)
        parsed_draft_status = _parse_draft_status_filter(draft_status_filter)

        shell = self._shell_service.build_shell(
            DashboardScreenId.COMMENTS,
            channel_id=channel_id,
        )

        connection = self._database.connect()
        try:
            channels = ChannelRepository(connection).list_all()
            selected_channel = _select_channel(channels, channel_id)
            if selected_channel is None:
                return CommentsScreenView(
                    shell=shell,
                    load_state=ViewLoadState.EMPTY,
                    channel_id=None,
                    auto_reply_enabled=self._flags.auto_reply_enabled,
                    total_comments=0,
                    new_unclassified_count=0,
                    needs_attention_count=0,
                    moderation_count=0,
                    reply_candidate_count=0,
                    pending_draft_count=0,
                    approved_draft_count=0,
                    posted_reply_count=0,
                    attention_comments=(),
                    new_comments=(),
                    all_comments=(),
                    reply_drafts=(),
                    empty_message="No channel configured yet. Create a channel to review comments and reply drafts.",
                )

            comment_repo = CommentRepository(connection)
            comments = comment_repo.list_by_channel(selected_channel.id)
            classifications_by_comment: dict[str, list[CommentClassification]] = {}
            drafts_by_comment: dict[str, list[ReplyDraft]] = {}
            for c in comments:
                classifications_by_comment[c.id] = comment_repo.list_classifications(
                    c.id
                )
                drafts_by_comment[c.id] = comment_repo.list_reply_drafts(c.id)
        finally:
            connection.close()

        thread_items: list[CommentThreadViewItem] = []
        all_draft_items: list[ReplyDraftViewItem] = []

        new_unclassified_count = 0
        needs_attention_count = 0
        moderation_count = 0
        reply_candidate_count = 0
        pending_draft_count = 0
        approved_draft_count = 0
        posted_reply_count = 0

        for comment in sorted(
            comments, key=lambda c: (c.published_at, c.id), reverse=True
        ):
            cls_list = classifications_by_comment.get(comment.id, [])
            latest_cls = cls_list[-1] if cls_list else None
            label_str = (
                latest_cls.label.value if latest_cls is not None else "unclassified"
            )
            is_new = latest_cls is None
            requires_att = label_str in (
                CommentLabel.NEEDS_ATTENTION.value,
                CommentLabel.MODERATION.value,
                CommentLabel.REPLY_CANDIDATE.value,
            )

            if is_new:
                new_unclassified_count += 1
            elif latest_cls is not None:
                if latest_cls.label is CommentLabel.NEEDS_ATTENTION:
                    needs_attention_count += 1
                elif latest_cls.label is CommentLabel.MODERATION:
                    moderation_count += 1
                elif latest_cls.label is CommentLabel.REPLY_CANDIDATE:
                    reply_candidate_count += 1

            raw_drafts = drafts_by_comment.get(comment.id, [])
            draft_views: list[ReplyDraftViewItem] = []
            for d in raw_drafts:
                if d.status is ReplyStatus.DRAFT:
                    pending_draft_count += 1
                elif d.status is ReplyStatus.APPROVED:
                    approved_draft_count += 1
                elif d.status is ReplyStatus.POSTED:
                    posted_reply_count += 1
                dv = ReplyDraftViewItem.from_draft(d)
                draft_views.append(dv)
                if (
                    parsed_draft_status is None
                    or d.status is parsed_draft_status
                ):
                    all_draft_items.append(dv)

            if parsed_label is not None and label_str != parsed_label:
                continue

            thread_items.append(
                CommentThreadViewItem(
                    comment_id=comment.id,
                    channel_id=comment.channel_id,
                    youtube_comment_id=comment.youtube_comment_id,
                    youtube_video_id=comment.youtube_video_id,
                    parent_comment_id=comment.parent_comment_id,
                    author_display_name=comment.author_display_name,
                    text=comment.text,
                    published_at=comment.published_at,
                    label=label_str,
                    label_tone=_LABEL_TONES.get(label_str, BadgeTone.NEUTRAL),
                    rationale=latest_cls.rationale if latest_cls else None,
                    classified_at=(
                        latest_cls.classified_at if latest_cls else None
                    ),
                    is_new_unclassified=is_new,
                    requires_attention=requires_att,
                    reply_drafts=tuple(draft_views),
                )
            )

        attention_list = tuple(c for c in thread_items if c.requires_attention)
        new_list = tuple(c for c in thread_items if c.is_new_unclassified)

        return CommentsScreenView(
            shell=shell,
            load_state=(
                ViewLoadState.READY if thread_items or all_draft_items else ViewLoadState.EMPTY
            ),
            channel_id=selected_channel.id,
            auto_reply_enabled=self._flags.auto_reply_enabled,
            total_comments=len(comments),
            new_unclassified_count=new_unclassified_count,
            needs_attention_count=needs_attention_count,
            moderation_count=moderation_count,
            reply_candidate_count=reply_candidate_count,
            pending_draft_count=pending_draft_count,
            approved_draft_count=approved_draft_count,
            posted_reply_count=posted_reply_count,
            attention_comments=attention_list,
            new_comments=new_list,
            all_comments=tuple(thread_items),
            reply_drafts=tuple(all_draft_items),
            empty_message=(
                "No comments or reply drafts found."
                if not thread_items and not all_draft_items
                else None
            ),
        )

    def approve_draft(
        self,
        draft_id: str,
        *,
        actor: Actor,
        edited_text: str | None = None,
    ) -> ReplyApprovalOutcome:
        """Delegate human approval (and optional edit) of a reply draft to ``ReplyApprovalService``."""
        if self._reply_approval is None:
            raise RuntimeError("ReplyApprovalService is not configured")
        return self._reply_approval.approve(
            draft_id, actor=actor, edited_text=edited_text
        )

    def discard_draft(
        self,
        draft_id: str,
        *,
        actor: Actor,
        reason: str | None = None,
    ) -> ReplyApprovalOutcome:
        """Delegate discarding a reply draft to ``ReplyApprovalService``."""
        if self._reply_approval is None:
            raise RuntimeError("ReplyApprovalService is not configured")
        return self._reply_approval.discard(draft_id, actor=actor, reason=reason)

    def render_html(self, view: CommentsScreenView) -> str:
        auto_reply_badge = (
            '<span class="yt-pill yt-pill--warning" data-testid="auto-reply-flag">AUTO_REPLY_ENABLED=true</span>'
            if view.auto_reply_enabled
            else '<span class="yt-pill yt-pill--neutral" data-testid="auto-reply-flag">AUTO_REPLY_ENABLED=false (Manual Approval Required)</span>'
        )

        attention_rows = "".join(
            f"<li><strong>{html.escape(c.author_display_name)}</strong> "
            f'<span class="yt-pill yt-pill--{c.label_tone.value}">{html.escape(c.label)}</span>: '
            f"{html.escape(c.text)}</li>"
            for c in (*view.attention_comments, *view.new_comments)
        )
        attention_html = (
            f"<ul>{attention_rows}</ul>"
            if attention_rows
            else '<p class="yt-empty">No new or attention-needed comments.</p>'
        )

        draft_rows = "".join(
            f"<tr><td><code>{html.escape(d.draft_id[:8])}</code></td>"
            f"<td>{html.escape(d.text)}</td>"
            f'<td><span class="yt-pill yt-pill--{d.tone.value}">{html.escape(d.status)}</span></td></tr>'
            for d in view.reply_drafts
        )
        drafts_html = (
            '<table class="yt-table" data-testid="comments-drafts-table">'
            "<thead><tr><th>Draft</th><th>Reply Text</th><th>Status</th></tr></thead>"
            f"<tbody>{draft_rows}</tbody></table>"
            if draft_rows
            else '<p class="yt-empty">No reply drafts queued.</p>'
        )

        body = f"""<div class="yt-grid" data-testid="comments-screen">
  <section class="yt-card yt-grid__span-6" data-testid="comments-attention-card">
    <div class="yt-card__header">
      <div>
        <p class="yt-card__subtitle">Community Inbox</p>
        <h2 class="yt-card__title">New &amp; Attention Comments</h2>
      </div>
      {auto_reply_badge}
    </div>
    <p class="yt-card__subtitle">Total: {view.total_comments} · New: {view.new_unclassified_count} · Needs attention: {view.needs_attention_count} · Moderation: {view.moderation_count} · Reply candidates: {view.reply_candidate_count}</p>
    {attention_html}
  </section>

  <section class="yt-card yt-grid__span-6" data-testid="comments-drafts-card">
    <div class="yt-card__header">
      <div>
        <p class="yt-card__subtitle">Human Review Queue</p>
        <h2 class="yt-card__title">Reply Drafts</h2>
      </div>
      <span class="yt-pill yt-pill--warning">{view.pending_draft_count} pending</span>
    </div>
    <p class="yt-card__subtitle">Draft: {view.pending_draft_count} · Approved: {view.approved_draft_count} · Posted: {view.posted_reply_count}</p>
    {drafts_html}
  </section>
</div>"""
        return render_dashboard_shell_html(
            view.shell,
            body_html=body,
            page_title="Comments & Reply Drafts — YouTube Command Center",
        )


def _parse_label_filter(value: CommentLabel | str | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, CommentLabel):
        return value.value
    norm = str(value).strip().lower()
    if norm == "unclassified":
        return "unclassified"
    return CommentLabel(norm).value


def _parse_draft_status_filter(
    value: ReplyStatus | str | None,
) -> ReplyStatus | None:
    if value is None:
        return None
    if isinstance(value, ReplyStatus):
        return value
    return ReplyStatus(str(value).strip().lower())

