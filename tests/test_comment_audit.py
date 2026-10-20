"""Unit tests for P-189 CommentAuditService."""

from datetime import UTC, datetime
from pathlib import Path

import pytest

from ai_youtube_agent.content.auto_reply import (
    AUTO_REPLY_BLOCKED,
    AUTO_REPLY_POSTED,
    AutoReplyDisabledError,
    AutoReplyGuard,
    AutoReplyService,
)
from ai_youtube_agent.content.comment_audit import (
    CommentAuditError,
    CommentAuditErrorCode,
    CommentAuditService,
)
from ai_youtube_agent.content.comment_classifier import (
    COMMENT_CLASSIFIED,
    CommentClassifier,
)
from ai_youtube_agent.content.comment_sync import COMMENTS_SYNCED, CommentSync
from ai_youtube_agent.content.duplicate_reply_guard import (
    REPLY_DUPLICATE_BLOCKED,
    DuplicateReplyError,
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
from ai_youtube_agent.core.db.migrate import migrate
from ai_youtube_agent.core.db.models import Channel
from ai_youtube_agent.core.db.repositories.channels import ChannelRepository
from ai_youtube_agent.core.flags import FeatureFlags
from ai_youtube_agent.providers.comments import RemoteComment
from ai_youtube_agent.providers.mock_comments import MockCommentProvider
from ai_youtube_agent.providers.mock_text_generation import MockTextGenerator
from ai_youtube_agent.providers.mock_youtube_oauth import MockYouTubeOAuthProvider

REVIEWER = Actor(kind=ActorKind.USER, id="operator:alice")


def _setup(
    tmp_path: Path,
) -> tuple[
    Database,
    InMemoryAuditSink,
    AuditLog,
    MockCommentProvider,
    MockYouTubeOAuthProvider,
    MockTextGenerator,
]:
    db_path = tmp_path / "comment_audit.db"
    migrate(db_path)
    db = Database(db_path)
    with db.transaction() as conn:
        ChannelRepository(conn).add(
            Channel(
                id="ch-1",
                slug="audit-channel",
                name="Audit Channel",
                youtube_channel_id="UC-audit-1",
            )
        )
    sink = InMemoryAuditSink()
    audit_log = AuditLog(sink)
    comments = MockCommentProvider()
    oauth = MockYouTubeOAuthProvider()
    code = oauth.create_authorization_code(channel_id="ch-1")
    oauth.connect_channel("ch-1", authorization_code=code)
    comments.register_AccessToken("mock-access-token")
    text_gen = MockTextGenerator()
    text_gen.set_default_texts(
        ("Thank you for asking! You can find the breakdown in section two.",)
    )
    return db, sink, audit_log, comments, oauth, text_gen


def test_trail_for_reply_draft_and_comment_and_channel(tmp_path: Path) -> None:
    db, sink, audit_log, comments, oauth, text_gen = _setup(tmp_path)

    comments.seed_video_comments(
        "ch-1",
        "yt-vid-1",
        (
            RemoteComment(
                youtube_comment_id="yt-c-1",
                youtube_video_id="yt-vid-1",
                author_display_name="Reader One",
                text="Could you explain how the caching layer works?",
                published_at=datetime(2026, 6, 12, 10, 0, tzinfo=UTC),
            ),
            RemoteComment(
                youtube_comment_id="yt-c-2",
                youtube_video_id="yt-vid-1",
                author_display_name="Reader Two",
                text="What about invalidation on schema upgrades?",
                published_at=datetime(2026, 6, 12, 10, 5, tzinfo=UTC),
            ),
        ),
    )

    sync = CommentSync(db, comments, oauth, audit_log)
    classifier = CommentClassifier(db, audit_log)
    drafter = ReplyDraftService(db, text_gen, audit_log)
    dup_guard = DuplicateReplyGuard(db, audit_log)
    approver = ReplyApprovalService(
        db, comments, oauth, audit_log, duplicate_guard=dup_guard
    )
    disabled_auto = AutoReplyService(
        db,
        comments,
        oauth,
        audit_log,
        AutoReplyGuard(FeatureFlags(auto_reply_enabled=False), audit_log),
        duplicate_guard=dup_guard,
    )
    audit_service = CommentAuditService(db, sink)

    sync_res = sync.sync_video_comments("ch-1", youtube_video_id="yt-vid-1")
    c1 = sync_res.outcomes[0].comment
    c2 = sync_res.outcomes[1].comment

    classifier.classify_comment(c1.id)
    classifier.classify_comment(c2.id)

    draft1 = drafter.create_draft(c1.id).draft
    draft2 = drafter.create_draft(c2.id).draft

    # Attempting auto-reply while flag is disabled records comment.auto_reply_blocked
    with pytest.raises(AutoReplyDisabledError):
        disabled_auto.auto_reply_draft(draft1.id)

    # Human approves and posts draft1, then discards draft2
    post_outcome = approver.approve_and_post(
        draft1.id,
        actor=REVIEWER,
        edited_text="Thanks for asking! Cache invalidation is versioned.",
    )
    approver.discard_draft(draft2.id, actor=REVIEWER, reason="covered_in_faq")

    # Attempting a second reply on c1 is blocked by DuplicateReplyGuard
    draft1_dup = drafter.create_draft(
        c1.id, reply_text="Another reply on the same comment."
    ).draft
    with pytest.raises(DuplicateReplyError):
        approver.approve_and_post(draft1_dup.id, actor=REVIEWER)

    # 1. Draft trail for draft1
    draft1_trail = audit_service.trail_for_reply_draft(draft1.id)
    assert draft1_trail.channel_id == "ch-1"
    assert draft1_trail.comment_id == c1.id
    assert draft1_trail.reply_draft_id == draft1.id
    assert [e.action for e in draft1_trail.entries] == [
        REPLY_DRAFTED,
        AUTO_REPLY_BLOCKED,
        REPLY_APPROVED,
        REPLY_POSTED,
    ]
    assert draft1_trail.posted_reply_ids == (post_outcome.receipt.youtube_reply_id,)

    # 2. Comment trail for c1 includes classification, both drafts, auto-reply block, approval, post, and duplicate block
    c1_trail = audit_service.trail_for_comment(c1.id)
    assert c1_trail.comment_id == c1.id
    assert c1_trail.action_counts[COMMENT_CLASSIFIED] == 1
    assert c1_trail.action_counts[REPLY_DRAFTED] == 2
    assert c1_trail.action_counts[AUTO_REPLY_BLOCKED] == 1
    assert c1_trail.action_counts[REPLY_APPROVED] == 2
    assert c1_trail.action_counts[REPLY_POSTED] == 1
    assert c1_trail.action_counts[REPLY_DUPLICATE_BLOCKED] == 1

    # 3. Channel trail includes comment.synced plus all comment & reply events
    ch_trail = audit_service.trail_for_channel("ch-1")
    assert ch_trail.channel_id == "ch-1"
    assert ch_trail.action_counts[COMMENTS_SYNCED] == 1
    assert ch_trail.action_counts[REPLY_DISCARDED] == 1
    assert ch_trail.posted_reply_ids == (post_outcome.receipt.youtube_reply_id,)

    # Verify serialized payload never leaks comment body text, reply text, or OAuth tokens
    serialized = str(ch_trail.as_dict())
    assert "Could you explain how the caching layer works?" not in serialized
    assert "Thanks for asking! Cache invalidation is versioned." not in serialized
    assert "mock-access-token" not in serialized


def test_trail_includes_auto_reply_posted_when_flag_enabled(tmp_path: Path) -> None:
    db, sink, audit_log, comments, oauth, text_gen = _setup(tmp_path)
    comments.seed_video_comments(
        "ch-1",
        "yt-vid-2",
        (
            RemoteComment(
                youtube_comment_id="yt-c-auto",
                youtube_video_id="yt-vid-2",
                author_display_name="Viewer",
                text="When is part two coming out?",
                published_at=datetime(2026, 6, 12, 11, 0, tzinfo=UTC),
            ),
        ),
    )

    sync = CommentSync(db, comments, oauth, audit_log)
    classifier = CommentClassifier(db, audit_log)
    drafter = ReplyDraftService(db, text_gen, audit_log)
    enabled_auto = AutoReplyService(
        db,
        comments,
        oauth,
        audit_log,
        AutoReplyGuard(FeatureFlags(auto_reply_enabled=True), audit_log),
    )
    audit_service = CommentAuditService(db, sink)

    comment = sync.sync_video_comments("ch-1", youtube_video_id="yt-vid-2").outcomes[0].comment
    classifier.classify_comment(comment.id)
    draft = drafter.create_draft(comment.id).draft
    outcome = enabled_auto.auto_reply_draft(draft.id)

    trail = audit_service.trail_for_reply_draft(draft.id)
    assert [e.action for e in trail.entries] == [REPLY_DRAFTED, AUTO_REPLY_POSTED]
    assert trail.posted_reply_ids == (outcome.receipt.youtube_reply_id,)


def test_audit_service_not_found_errors(tmp_path: Path) -> None:
    db, sink, *_ = _setup(tmp_path)
    audit_service = CommentAuditService(db, sink)

    with pytest.raises(CommentAuditError) as ch_err:
        audit_service.trail_for_channel("missing-channel")
    assert ch_err.value.reason == CommentAuditErrorCode.CHANNEL_NOT_FOUND

    with pytest.raises(CommentAuditError) as c_err:
        audit_service.trail_for_comment("missing-comment")
    assert c_err.value.reason == CommentAuditErrorCode.COMMENT_NOT_FOUND

    with pytest.raises(CommentAuditError) as d_err:
        audit_service.trail_for_reply_draft("missing-draft")
    assert d_err.value.reason == CommentAuditErrorCode.DRAFT_NOT_FOUND
