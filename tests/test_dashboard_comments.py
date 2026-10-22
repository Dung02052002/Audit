"""Tests for Comments Screen view and service (Prompt Pack v8, prompt Q-198)."""

from datetime import UTC, datetime, timedelta

from ai_youtube_agent.content.channel import ChannelStatus
from ai_youtube_agent.content.comment import (
    Comment,
    CommentClassification,
    CommentLabel,
    ReplyDraft,
    ReplyStatus,
)
from ai_youtube_agent.content.dashboard_comments import CommentsScreenService
from ai_youtube_agent.content.dashboard_shell import DashboardScreenId, ViewLoadState
from ai_youtube_agent.core.audit import Actor, ActorKind
from ai_youtube_agent.core.db.database import Database
from ai_youtube_agent.core.db.repositories.channel import ChannelRepository
from ai_youtube_agent.core.db.repositories.community import CommentRepository
from factories import make_channel

FIXED_NOW = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)


def test_comments_screen_empty_when_no_channels(database: Database) -> None:
    service = CommentsScreenService(database, clock=lambda: FIXED_NOW)
    view = service.load()

    assert view.load_state is ViewLoadState.EMPTY
    assert view.channel_id is None
    assert view.auto_reply_enabled is False
    assert view.total_comments == 0


def test_comments_screen_shows_new_and_attention_comments_and_reply_drafts(
    database: Database,
) -> None:
    channel = make_channel(title="Community Hub", status=ChannelStatus.ACTIVE)

    c_new = Comment.create(
        channel.id,
        "UgxNewComment1",
        "dQw4w9WgXcQ",
        author_display_name="Alice",
        text="Can you share the slides from chapter 2?",
        published_at=FIXED_NOW - timedelta(minutes=10),
    )
    c_att = Comment.create(
        channel.id,
        "UgxAttComment2",
        "dQw4w9WgXcQ",
        author_display_name="Bob",
        text="The audio cuts out around 03:15 — could you check?",
        published_at=FIXED_NOW - timedelta(minutes=30),
    )
    c_no_action = Comment.create(
        channel.id,
        "UgxNoAction3",
        "dQw4w9WgXcQ",
        author_display_name="Carol",
        text="Nice video!",
        published_at=FIXED_NOW - timedelta(hours=1),
    )

    ai_actor = Actor(ActorKind.AI, "comment-classifier")
    cls_att = CommentClassification.create(
        c_att.id,
        CommentLabel.NEEDS_ATTENTION,
        classified_by=ai_actor,
        rationale="Viewer reported an audio issue.",
        clock=lambda: FIXED_NOW,
    )
    cls_no = CommentClassification.create(
        c_no_action.id,
        CommentLabel.NO_ACTION,
        classified_by=ai_actor,
        rationale="Brief compliment.",
        clock=lambda: FIXED_NOW,
    )

    draft = ReplyDraft.create(
        c_att.id,
        "Thanks for flagging! We checked the timestamp and pinned a note.",
        created_by=Actor(ActorKind.AI, "reply-drafter"),
        clock=lambda: FIXED_NOW,
    )

    with database.transaction() as conn:
        ChannelRepository(conn).add(channel)
        repo = CommentRepository(conn)
        repo.add(c_new)
        repo.add(c_att)
        repo.add(c_no_action)
        repo.add_classification(cls_att)
        repo.add_classification(cls_no)
        repo.add_reply_draft(draft)

    service = CommentsScreenService(database, clock=lambda: FIXED_NOW)
    view = service.load(channel.id)

    assert view.shell.active_screen is DashboardScreenId.COMMENTS
    assert view.load_state is ViewLoadState.READY
    assert view.auto_reply_enabled is False
    assert view.total_comments == 3
    assert view.new_unclassified_count == 1
    assert view.needs_attention_count == 1
    assert view.pending_draft_count == 1
    assert len(view.new_comments) == 1
    assert view.new_comments[0].comment_id == c_new.id
    assert len(view.attention_comments) == 1
    assert view.attention_comments[0].comment_id == c_att.id
    assert len(view.reply_drafts) == 1
    assert view.reply_drafts[0].status == ReplyStatus.DRAFT.value

    html_out = service.render_html(view)
    assert 'data-testid="comments-screen"' in html_out
    assert "AUTO_REPLY_ENABLED=false" in html_out
    assert "Thanks for flagging!" in html_out
