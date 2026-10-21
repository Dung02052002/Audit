"""End-to-end Phase P Community & Comments scenario tests (Prompt Pack v8, prompt #190)."""

from datetime import UTC, datetime, timedelta

import pytest

from ai_youtube_agent.content.auto_reply import (
    AUTO_REPLY_BLOCKED,
    AUTO_REPLY_POSTED,
    AutoReplyDisabledError,
    AutoReplyService,
)
from ai_youtube_agent.content.channel import ChannelStatus
from ai_youtube_agent.content.comment import Comment, CommentLabel, ReplyStatus
from ai_youtube_agent.content.comment_audit import CommentAuditService
from ai_youtube_agent.content.comment_classifier import (
    COMMENT_CLASSIFIED,
    CommentClassifier,
)
from ai_youtube_agent.content.duplicate_reply_guard import (
    REPLY_DUPLICATE_BLOCKED,
    DuplicateReplyError,
    DuplicateReplyGuard,
    ReplyDuplicateReason,
)
from ai_youtube_agent.content.reply_approval import (
    REPLY_APPROVED,
    REPLY_DISCARDED,
    REPLY_POSTED,
    ReplyApprovalService,
)
from ai_youtube_agent.content.reply_draft import (
    REPLY_DRAFTED,
    ReplyDraftError,
    ReplyDraftErrorCode,
    ReplyDraftService,
)
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
OPERATOR = Actor(kind=ActorKind.USER, id="operator:dung")


def test_end_to_end_classification_moderation_draft_approval_duplicate_and_audit(
    database: Database,
) -> None:
    channel = make_channel(status=ChannelStatus.ACTIVE)
    profile = make_strategy_profile(channel)

    q_comment = Comment.create(
        channel.id,
        "Ugx-q-1",
        VID,
        author_display_name="Curious Dev",
        text="How did you configure SQLite WAL mode for concurrent reads?",
        published_at=NOW - timedelta(minutes=30),
    )
    q_reply_comment = Comment.create(
        channel.id,
        "Ugx-q-1-r1",
        VID,
        parent_id=q_comment.youtube_comment_id,
        author_display_name="Second Viewer",
        text="Also curious whether busy_timeout is set?",
        published_at=NOW - timedelta(minutes=25),
    )
    spam_comment = Comment.create(
        channel.id,
        "Ugx-spam-1",
        VID,
        author_display_name="Crypto Bot",
        text="Visit https://scam.example/airdrop for free coins",
        published_at=NOW - timedelta(minutes=20),
    )
    feedback_comment = Comment.create(
        channel.id,
        "Ugx-fb-1",
        VID,
        author_display_name="Loyal Subscriber",
        text="Could you share more benchmarks in the next episode?",
        published_at=NOW - timedelta(minutes=15),
    )

    with database.transaction() as conn:
        ChannelRepository(conn).add(channel)
        StrategyProfileRepository(conn).add(profile)
        repo = CommentRepository(conn)
        for c in (q_comment, q_reply_comment, spam_comment, feedback_comment):
            repo.add(c)

    sink = InMemoryAuditSink()
    audit_log = AuditLog(sink, clock=lambda: NOW)
    comments = MockCommentProvider(clock=lambda: NOW)
    comments.seed_comments(
        channel.youtube.channel_id,
        VID,
        tuple(
            RemoteComment(
                youtube_comment_id=c.youtube_comment_id,
                youtube_video_id=VID,
                parent_id=c.parent_id,
                author_display_name=c.author_display_name,
                text=c.text,
                published_at=c.published_at,
            )
            for c in (q_comment, q_reply_comment, spam_comment, feedback_comment)
        ),
    )
    oauth = MockYouTubeOAuthProvider(clock=lambda: NOW)
    oauth.seed_token(channel.id, scopes=frozenset({OAuthScope.YOUTUBE_FORCE_SSL}))
    text_gen = MockTextGenerator(clock=lambda: NOW)
    text_gen.queue("We set journal_mode=WAL and busy_timeout=5000ms.")
    text_gen.queue("More benchmarks are coming in episode two!")

    classifier = CommentClassifier(database, audit_log, clock=lambda: NOW)
    drafter = ReplyDraftService(
        database, text_gen, audit_log, classifier=classifier, clock=lambda: NOW
    )
    dup_guard = DuplicateReplyGuard(database, audit_log)
    approver = ReplyApprovalService(
        database,
        comments,
        oauth,
        audit_log,
        duplicate_guard=dup_guard,
        sleep=lambda _: None,
    )
    auto_reply = AutoReplyService(
        database,
        drafter,
        approver,
        audit_log,
        flags=FeatureFlags(auto_reply_enabled=False),
    )
    audit_service = CommentAuditService(database, sink)

    # 1. Classify all comments
    summary = classifier.classify_channel(channel.id)
    assert summary.created_count == 4
    assert summary.by_label[CommentLabel.MODERATION.value] == 1

    # 2. Moderation comment blocks draft generation unless allow_moderation=True
    with pytest.raises(ReplyDraftError) as mod_err:
        drafter.generate_draft(spam_comment.id)
    assert mod_err.value.draft_code is ReplyDraftErrorCode.MODERATION_BLOCKED

    # 3. Draft creation succeeds for question and feedback comments and NEVER posts
    q_draft = drafter.generate_draft(q_comment.id).draft
    fb_draft = drafter.generate_draft(feedback_comment.id).draft
    assert q_draft.status is ReplyStatus.DRAFT
    assert fb_draft.status is ReplyStatus.DRAFT
    assert comments.post_calls == 0

    # 4. AUTO_REPLY_ENABLED=false blocks automated reply posting and audits denial
    with pytest.raises(AutoReplyDisabledError):
        auto_reply.auto_post_draft(q_draft.id)
    assert comments.post_calls == 0

    # 5. Human approves and edits q_draft, then posts to YouTube
    approver.approve(
        q_draft.id,
        actor=OPERATOR,
        edited_text="We set PRAGMA journal_mode=WAL and busy_timeout=5000ms at init.",
    )
    post_outcome = approver.post_approved(q_draft.id, actor=OPERATOR)
    assert post_outcome.draft.status is ReplyStatus.POSTED
    assert comments.post_calls == 1

    # 6. DuplicateReplyGuard blocks a reply in the same parent thread
    thread_child_draft = drafter.create_manual_draft(
        q_reply_comment.id,
        text="Yes, busy_timeout is 5000ms!",
        actor=OPERATOR,
    ).draft
    approver.approve(thread_child_draft.id, actor=OPERATOR)
    with pytest.raises(DuplicateReplyError) as dup_thread:
        approver.post_approved(thread_child_draft.id, actor=OPERATOR)
    assert dup_thread.value.reason is ReplyDuplicateReason.THREAD_ALREADY_REPLIED

    # DuplicateReplyGuard blocks identical normalized reply text on the same video
    approver.approve(
        fb_draft.id,
        actor=OPERATOR,
        edited_text="  we set pragma journal_mode=wal and busy_timeout=5000ms at init.! ",
    )
    with pytest.raises(DuplicateReplyError) as dup_text:
        approver.post_approved(fb_draft.id, actor=OPERATOR)
    assert dup_text.value.reason is ReplyDuplicateReason.DUPLICATE_TEXT_ON_VIDEO

    # Discard unused thread draft and edit + post genuine feedback reply
    discarded = approver.discard(
        thread_child_draft.id, actor=OPERATOR, reason="already_answered_in_thread"
    )
    assert discarded.status is ReplyStatus.DISCARDED

    approver.approve(
        fb_draft.id,
        actor=OPERATOR,
        edited_text="More benchmarks are coming next week!",
    )
    fb_post = approver.post_approved(fb_draft.id, actor=OPERATOR)
    assert fb_post.draft.status is ReplyStatus.POSTED
    assert comments.post_calls == 2

    # 7. Verify CommentAuditService trails
    q_trail = audit_service.trail_for_comment(q_comment.id)
    assert q_trail.action_counts[COMMENT_CLASSIFIED] == 1
    assert q_trail.action_counts[REPLY_DRAFTED] == 1
    assert q_trail.action_counts[AUTO_REPLY_BLOCKED] == 1
    assert q_trail.action_counts[REPLY_APPROVED] == 1
    assert q_trail.action_counts[REPLY_POSTED] == 1
    assert q_trail.posted_reply_ids == (post_outcome.youtube_reply_id,)

    ch_trail = audit_service.trail_for_channel(channel.id)
    assert ch_trail.action_counts[COMMENT_CLASSIFIED] == 4
    assert ch_trail.action_counts[REPLY_POSTED] == 2
    assert ch_trail.action_counts[REPLY_DISCARDED] == 1
    assert ch_trail.action_counts[REPLY_DUPLICATE_BLOCKED] == 2

    audit_dump = str(ch_trail.as_dict())
    assert "journal_mode=WAL" not in audit_dump
    assert "More benchmarks are coming next week!" not in audit_dump
