"""Tests for human reply approval and posting service (P-186, prompt #186)."""

from datetime import UTC, datetime, timedelta

import pytest

from ai_youtube_agent.content.channel import ChannelStatus
from ai_youtube_agent.content.comment import Comment, ReplyDraft, ReplyStatus
from ai_youtube_agent.content.reply_approval import (
    REPLY_APPROVED,
    REPLY_DISCARDED,
    REPLY_POSTED,
    REPLY_POST_FAILED,
    ReplyApprovalError,
    ReplyApprovalErrorCode,
    ReplyApprovalService,
)
from ai_youtube_agent.core.audit import AuditLog, AuditResult, InMemoryAuditSink
from ai_youtube_agent.core.db.database import Database
from ai_youtube_agent.core.db.repositories.channel import (
    ChannelRepository,
    StrategyProfileRepository,
)
from ai_youtube_agent.core.db.repositories.community import CommentRepository
from ai_youtube_agent.pipeline.upload_retry import UploadRetryPolicy
from ai_youtube_agent.providers.comments import (
    CommentErrorCode,
    CommentProviderError,
    RemoteComment,
)
from ai_youtube_agent.providers.mock_comments import MockCommentProvider
from ai_youtube_agent.providers.mock_youtube_oauth import MockYouTubeOAuthProvider
from ai_youtube_agent.providers.youtube_oauth import OAuthScope
from factories import AI, USER, make_channel, make_strategy_profile

NOW = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)
VID = "dQw4w9WgXcQ"


class TestReplyApprovalService:
    def test_requires_human_approval_before_posting_and_records_youtube_reply_id(
        self, database: Database
    ) -> None:
        channel = make_channel(status=ChannelStatus.ACTIVE)
        profile = make_strategy_profile(channel)
        comment = Comment.create(
            channel.id,
            "Ugx-parent-1",
            VID,
            author_display_name="Viewer",
            text="Great breakdown! Will you cover tax-loss harvesting?",
            published_at=NOW - timedelta(minutes=30),
        )
        draft = ReplyDraft.create(
            comment.id,
            "Yes, tax-loss harvesting is coming next Friday!",
            created_by=AI,
            clock=lambda: NOW - timedelta(minutes=10),
        )
        with database.transaction() as connection:
            ChannelRepository(connection).add(channel)
            StrategyProfileRepository(connection).add(profile)
            repo = CommentRepository(connection)
            repo.add(comment)
            repo.add_reply_draft(draft)

        provider = MockCommentProvider(clock=lambda: NOW)
        provider.seed_comments(
            channel.youtube.channel_id,
            VID,
            (
                RemoteComment(
                    youtube_comment_id="Ugx-parent-1",
                    youtube_video_id=VID,
                    author_display_name="Viewer",
                    text=comment.text,
                    published_at=comment.published_at,
                ),
            ),
        )
        oauth = MockYouTubeOAuthProvider(clock=lambda: NOW)
        oauth.seed_token(
            channel.id, scopes=frozenset({OAuthScope.YOUTUBE_FORCE_SSL})
        )
        sink = InMemoryAuditSink()
        service = ReplyApprovalService(
            database,
            provider,
            oauth,
            AuditLog(sink, clock=lambda: NOW),
            retry_policy=UploadRetryPolicy(max_attempts=2, base_delay_seconds=1),
            sleep=lambda _: None,
        )

        # 1. AI actor cannot approve or post
        with pytest.raises(ReplyApprovalError) as exc_ai:
            service.approve(draft.id, actor=AI)
        assert (
            exc_ai.value.approval_code
            is ReplyApprovalErrorCode.USER_ACTOR_REQUIRED
        )

        # 2. Cannot post an unapproved DRAFT
        with pytest.raises(ReplyApprovalError) as exc_unapp:
            service.post_approved(draft.id, actor=USER)
        assert (
            exc_unapp.value.approval_code is ReplyApprovalErrorCode.NOT_APPROVED
        )

        # 3. Human user approves with edited text
        approved_out = service.approve(
            draft.id,
            actor=USER,
            edited_text="Yes! Tax-loss harvesting is our topic next Friday.",
        )
        assert approved_out.edited is True
        assert approved_out.draft.status is ReplyStatus.APPROVED
        assert sink.entries[-1].action == REPLY_APPROVED

        # 4. Human user posts the approved draft -> transitions to POSTED with youtube_reply_id
        post_out = service.post_approved(draft.id, actor=USER)
        assert post_out.replayed is False
        assert post_out.draft.status is ReplyStatus.POSTED
        assert post_out.draft.youtube_reply_id is not None
        assert sink.entries[-1].action == REPLY_POSTED

        # 5. Re-posting the same draft replays idempotently without calling provider again
        replay = service.post_approved(draft.id, actor=USER)
        assert replay.replayed is True
        assert provider.post_calls == 1

    def test_provider_failure_marks_draft_failed_and_discard_transitions_cleanly(
        self, database: Database
    ) -> None:
        channel = make_channel(status=ChannelStatus.ACTIVE)
        profile = make_strategy_profile(channel)
        comment = Comment.create(
            channel.id,
            "Ugx-parent-2",
            VID,
            author_display_name="Viewer",
            text="Question?",
            published_at=NOW - timedelta(minutes=20),
        )
        d1 = ReplyDraft.create(
            comment.id, "Reply 1", created_by=AI, clock=lambda: NOW
        )
        d2 = ReplyDraft.create(
            comment.id, "Reply 2", created_by=AI, clock=lambda: NOW
        )
        with database.transaction() as connection:
            ChannelRepository(connection).add(channel)
            StrategyProfileRepository(connection).add(profile)
            repo = CommentRepository(connection)
            repo.add(comment)
            repo.add_reply_draft(d1)
            repo.add_reply_draft(d2)

        provider = MockCommentProvider(clock=lambda: NOW)
        provider.seed_comments(
            channel.youtube.channel_id,
            VID,
            (
                RemoteComment(
                    youtube_comment_id="Ugx-parent-2",
                    youtube_video_id=VID,
                    author_display_name="Viewer",
                    text=comment.text,
                    published_at=comment.published_at,
                ),
            ),
        )
        oauth = MockYouTubeOAuthProvider(clock=lambda: NOW)
        oauth.seed_token(
            channel.id, scopes=frozenset({OAuthScope.YOUTUBE_FORCE_SSL})
        )
        sink = InMemoryAuditSink()
        service = ReplyApprovalService(
            database,
            provider,
            oauth,
            AuditLog(sink, clock=lambda: NOW),
            sleep=lambda _: None,
        )

        service.approve(d1.id, actor=USER)
        provider.fail_next(CommentErrorCode.QUOTA_EXCEEDED, times=1)
        with pytest.raises(CommentProviderError):
            service.post_approved(d1.id, actor=USER)

        assert sink.entries[-1].action == REPLY_POST_FAILED
        assert sink.entries[-1].result is AuditResult.FAILURE

        connection = database.connect()
        try:
            stored_d1 = CommentRepository(connection).get_reply_draft(d1.id)
        finally:
            connection.close()
        assert stored_d1 is not None and stored_d1.status is ReplyStatus.FAILED

        # Discard d2
        discarded = service.discard(d2.id, actor=USER, reason="Not needed")
        assert discarded.status is ReplyStatus.DISCARDED
        assert sink.entries[-1].action == REPLY_DISCARDED
