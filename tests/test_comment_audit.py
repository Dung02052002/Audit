"""Unit tests for P-189 CommentAuditService."""

from datetime import UTC, datetime, timedelta

import pytest

from ai_youtube_agent.content.auto_reply import (
    AUTO_REPLY_BLOCKED,
    AUTO_REPLY_POSTED,
    AutoReplyDisabledError,
    AutoReplyService,
)
from ai_youtube_agent.content.channel import ChannelStatus
from ai_youtube_agent.content.comment import Comment
from ai_youtube_agent.content.comment_audit import (
    CommentAuditError,
    CommentAuditErrorCode,
    CommentAuditService,
)
from ai_youtube_agent.content.comment_classifier import (
    COMMENT_CLASSIFIED,
    CommentClassifier,
)
from ai_youtube_agent.content.duplicate_reply_guard import (
    DuplicateReplyGuard,
)
from ai_youtube_agent.content.reply_approval import (
    REPLY_APPROVED,
    REPLY_DISCARDED,
    REPLY_POSTED,
    ReplyApprovalService,
)
from ai_youtube_agent.content.reply_draft import REPLY_DRAFTED, ReplyDraftService
from ai_youtube_agent.core.audit import Actor, ActorKind, AuditLog, InMemoryAuditSink
from ai_youtube_agent.core.db.database import Database
from ai_youtube_agent.core.db.repositories.channel import (
    ChannelRepository,
    StrategyProfileRepository,
)
from ai_youtube_agent.core.db.repositories.community import CommentRepository
from ai_youtube_agent.core.flags import FeatureFlags
from ai_youtube_agent.providers.comments import RemoteComment
from ai_youtube_agent.providers.mock_comments import MockCommentProvider
from ai_youtube_agent.providers.mock_text_generation import MockTextGenerator
from ai_youtube_agent.providers.mock_youtube_oauth import MockYouTubeOAuthProvider
from ai_youtube_agent.providers.youtube_oauth import OAuthScope
from factories import make_channel, make_strategy_profile

NOW = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)
VID = "dQw4w9WgXcQ"
REVIEWER = Actor(kind=ActorKind.USER, id="operator:alice")


def test_trail_for_reply_draft_and_comment_and_channel(database: Database) -> None:
    channel = make_channel(status=ChannelStatus.ACTIVE)
    profile = make_strategy_profile(channel)
    c1 = Comment.create(
        channel.id,
        "Ugx-audit-1",
        VID,
        author_display_name="Reader One",
        text="Could you explain how the caching layer works?",
        published_at=NOW - timedelta(minutes=20),
    )
    c2 = Comment.create(
        channel.id,
        "Ugx-audit-2",
        VID,
        author_display_name="Reader Two",
        text="What about invalidation on schema upgrades?",
        published_at=NOW - timedelta(minutes=15),
    )
    with database.transaction() as conn:
        ChannelRepository(conn).add(channel)
        StrategyProfileRepository(conn).add(profile)
        CommentRepository(conn).add(c1)
        CommentRepository(conn).add(c2)

    sink = InMemoryAuditSink()
    audit_log = AuditLog(sink, clock=lambda: NOW)
    comments = MockCommentProvider(clock=lambda: NOW)
    comments.seed_comments(
        channel.youtube.channel_id,
        VID,
        (
            RemoteComment(
                youtube_comment_id=c1.youtube_comment_id,
                youtube_video_id=VID,
                author_display_name=c1.author_display_name,
                text=c1.text,
                published_at=c1.published_at,
            ),
            RemoteComment(
                youtube_comment_id=c2.youtube_comment_id,
                youtube_video_id=VID,
                author_display_name=c2.author_display_name,
                text=c2.text,
                published_at=c2.published_at,
            ),
        ),
    )
    oauth = MockYouTubeOAuthProvider(clock=lambda: NOW)
    oauth.seed_token(channel.id, scopes=frozenset({OAuthScope.YOUTUBE_FORCE_SSL}))
    text_gen = MockTextGenerator(clock=lambda: NOW)
    text_gen.queue("Thank you for asking! You can find the breakdown in section two.")
    text_gen.queue("Schema upgrades invalidate cached rows automatically.")

    classifier = CommentClassifier(database, audit_log, clock=lambda: NOW)
    drafter = ReplyDraftService(database, text_gen, audit_log, clock=lambda: NOW)
    dup_guard = DuplicateReplyGuard(database, audit_log)
    approver = ReplyApprovalService(
        database,
        comments,
        oauth,
        audit_log,
        duplicate_guard=dup_guard,
        sleep=lambda _: None,
    )
    disabled_auto = AutoReplyService(
        database,
        drafter,
        approver,
        audit_log,
        flags=FeatureFlags(auto_reply_enabled=False),
    )
    audit_service = CommentAuditService(database, sink)

    classifier.classify_comment(c1.id)
    classifier.classify_comment(c2.id)

    draft1 = drafter.generate_draft(c1.id).draft
    draft2 = drafter.generate_draft(c2.id).draft

    # Attempting auto-reply while flag is disabled records comment.auto_reply_blocked
    with pytest.raises(AutoReplyDisabledError):
        disabled_auto.auto_post_draft(draft1.id)

    # Human approves and posts draft1, then discards draft2
    approver.approve(
        draft1.id,
        actor=REVIEWER,
        edited_text="Thanks for asking! Cache invalidation is versioned.",
    )
    post_outcome = approver.post_approved(draft1.id, actor=REVIEWER)
    approver.discard(draft2.id, actor=REVIEWER, reason="covered_in_faq")

    # 1. Draft trail for draft1
    draft1_trail = audit_service.trail_for_reply_draft(draft1.id)
    assert draft1_trail.channel_id == channel.id
    assert draft1_trail.comment_id == c1.id
    assert draft1_trail.reply_draft_id == draft1.id
    assert [e.action for e in draft1_trail.entries] == [
        REPLY_DRAFTED,
        AUTO_REPLY_BLOCKED,
        REPLY_APPROVED,
        REPLY_POSTED,
    ]
    assert draft1_trail.posted_reply_ids == (post_outcome.youtube_reply_id,)

    # 2. Comment trail for c1
    c1_trail = audit_service.trail_for_comment(c1.id)
    assert c1_trail.comment_id == c1.id
    assert c1_trail.action_counts[COMMENT_CLASSIFIED] == 1
    assert c1_trail.action_counts[REPLY_DRAFTED] == 1
    assert c1_trail.action_counts[AUTO_REPLY_BLOCKED] == 1
    assert c1_trail.action_counts[REPLY_APPROVED] == 1
    assert c1_trail.action_counts[REPLY_POSTED] == 1

    # 3. Channel trail includes all comment & reply events
    ch_trail = audit_service.trail_for_channel(channel.id)
    assert ch_trail.channel_id == channel.id
    assert ch_trail.action_counts[REPLY_DISCARDED] == 1
    assert ch_trail.posted_reply_ids == (post_outcome.youtube_reply_id,)

    # Verify serialized payload never leaks comment body text or reply text
    serialized = str(ch_trail.as_dict())
    assert "Could you explain how the caching layer works?" not in serialized
    assert "Thanks for asking! Cache invalidation is versioned." not in serialized


def test_trail_includes_auto_reply_posted_when_flag_enabled(
    database: Database,
) -> None:
    channel = make_channel(status=ChannelStatus.ACTIVE)
    profile = make_strategy_profile(channel)
    c_auto = Comment.create(
        channel.id,
        "Ugx-audit-auto",
        VID,
        author_display_name="Viewer",
        text="When is part two coming out?",
        published_at=NOW - timedelta(minutes=5),
    )
    with database.transaction() as conn:
        ChannelRepository(conn).add(channel)
        StrategyProfileRepository(conn).add(profile)
        CommentRepository(conn).add(c_auto)

    sink = InMemoryAuditSink()
    audit_log = AuditLog(sink, clock=lambda: NOW)
    comments = MockCommentProvider(clock=lambda: NOW)
    comments.seed_comments(
        channel.youtube.channel_id,
        VID,
        (
            RemoteComment(
                youtube_comment_id=c_auto.youtube_comment_id,
                youtube_video_id=VID,
                author_display_name=c_auto.author_display_name,
                text=c_auto.text,
                published_at=c_auto.published_at,
            ),
        ),
    )
    oauth = MockYouTubeOAuthProvider(clock=lambda: NOW)
    oauth.seed_token(channel.id, scopes=frozenset({OAuthScope.YOUTUBE_FORCE_SSL}))
    text_gen = MockTextGenerator(clock=lambda: NOW)
    text_gen.queue("Part two launches next Friday!")

    drafter = ReplyDraftService(database, text_gen, audit_log, clock=lambda: NOW)
    approver = ReplyApprovalService(
        database, comments, oauth, audit_log, sleep=lambda _: None
    )
    enabled_auto = AutoReplyService(
        database,
        drafter,
        approver,
        audit_log,
        flags=FeatureFlags(auto_reply_enabled=True),
    )
    audit_service = CommentAuditService(database, sink)

    outcome = enabled_auto.draft_or_auto_reply(c_auto.id)
    assert outcome.posted is True
    assert outcome.post_outcome is not None

    trail = audit_service.trail_for_reply_draft(outcome.draft.id)
    assert [e.action for e in trail.entries] == [
        REPLY_DRAFTED,
        REPLY_POSTED,
        AUTO_REPLY_POSTED,
    ]
    assert trail.posted_reply_ids == (outcome.post_outcome.youtube_reply_id,)


def test_audit_service_not_found_errors(database: Database) -> None:
    sink = InMemoryAuditSink()
    audit_service = CommentAuditService(database, sink)

    with pytest.raises(CommentAuditError) as ch_err:
        audit_service.trail_for_channel("missing-channel")
    assert ch_err.value.reason == CommentAuditErrorCode.CHANNEL_NOT_FOUND

    with pytest.raises(CommentAuditError) as c_err:
        audit_service.trail_for_comment("missing-comment")
    assert c_err.value.reason == CommentAuditErrorCode.COMMENT_NOT_FOUND

    with pytest.raises(CommentAuditError) as d_err:
        audit_service.trail_for_reply_draft("missing-draft")
    assert d_err.value.reason == CommentAuditErrorCode.DRAFT_NOT_FOUND
