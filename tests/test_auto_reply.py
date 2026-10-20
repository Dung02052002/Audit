"""Tests for AUTO_REPLY_ENABLED flag enforcement (P-188, prompt #188)."""

from datetime import UTC, datetime, timedelta

import pytest

from ai_youtube_agent.content.auto_reply import (
    AUTO_REPLY_BLOCKED,
    AUTO_REPLY_POSTED,
    AutoReplyDisabledError,
    AutoReplyErrorCode,
    AutoReplyGuard,
    AutoReplyService,
)
from ai_youtube_agent.content.channel import ChannelStatus
from ai_youtube_agent.content.comment import Comment, ReplyStatus
from ai_youtube_agent.content.duplicate_reply_guard import DuplicateReplyGuard
from ai_youtube_agent.content.reply_approval import ReplyApprovalService
from ai_youtube_agent.content.reply_draft import ReplyDraftService
from ai_youtube_agent.core.audit import AuditLog, AuditResult, InMemoryAuditSink
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
from factories import USER, make_channel, make_strategy_profile

NOW = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)
VID = "dQw4w9WgXcQ"


class TestAutoReplyFlagEnforcement:
    def test_default_flag_is_false_and_blocks_auto_reply_while_preserving_draft(
        self, database: Database
    ) -> None:
        default_flags = FeatureFlags()
        assert default_flags.auto_reply_enabled is False

        channel = make_channel(status=ChannelStatus.ACTIVE)
        profile = make_strategy_profile(channel)
        comment = Comment.create(
            channel.id,
            "Ugx-ar-1",
            VID,
            author_display_name="Viewer",
            text="What brokerage do you use?",
            published_at=NOW - timedelta(minutes=15),
        )
        with database.transaction() as connection:
            ChannelRepository(connection).add(channel)
            StrategyProfileRepository(connection).add(profile)
            CommentRepository(connection).add(comment)

        provider = MockCommentProvider(clock=lambda: NOW)
        provider.seed_comments(
            channel.youtube.channel_id,
            VID,
            (
                RemoteComment(
                    youtube_comment_id="Ugx-ar-1",
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
        audit = AuditLog(sink, clock=lambda: NOW)
        text_gen = MockTextGenerator(clock=lambda: NOW)
        text_gen.queue("Any low-cost SIPC-insured brokerage works great!")

        draft_svc = ReplyDraftService(
            database, text_gen, audit, clock=lambda: NOW
        )
        approval_svc = ReplyApprovalService(
            database,
            provider,
            oauth,
            audit,
            duplicate_guard=DuplicateReplyGuard(database, audit),
            sleep=lambda _: None,
        )
        auto_svc = AutoReplyService(
            database,
            draft_svc,
            approval_svc,
            audit,
            flags=default_flags,
        )

        # 1. draft_or_auto_reply with default flags creates a DRAFT and does NOT post
        outcome = auto_svc.draft_or_auto_reply(comment.id)
        assert outcome.posted is False
        assert outcome.draft.status is ReplyStatus.DRAFT
        assert outcome.draft.youtube_reply_id is None
        assert provider.post_calls == 0

        # 2. Direct auto_post_draft call is blocked with AutoReplyDisabledError and audited DENIED
        with pytest.raises(AutoReplyDisabledError) as exc_off:
            auto_svc.auto_post_draft(outcome.draft.id)
        assert exc_off.value.auto_reply_code is AutoReplyErrorCode.DISABLED
        assert sink.entries[-1].action == AUTO_REPLY_BLOCKED
        assert sink.entries[-1].result is AuditResult.DENIED
        assert provider.post_calls == 0

        # 3. When AUTO_REPLY_ENABLED=True is explicitly configured, auto_post_draft posts
        enabled_svc = AutoReplyService(
            database,
            draft_svc,
            approval_svc,
            audit,
            flags=FeatureFlags(auto_reply_enabled=True),
        )
        posted = enabled_svc.auto_post_draft(outcome.draft.id)
        assert posted.draft.status is ReplyStatus.POSTED
        assert posted.draft.youtube_reply_id is not None
        assert sink.entries[-1].action == AUTO_REPLY_POSTED
        assert provider.post_calls == 1

    def test_moderation_comment_is_never_auto_replied_even_when_flag_enabled(
        self, database: Database
    ) -> None:
        channel = make_channel(status=ChannelStatus.ACTIVE)
        profile = make_strategy_profile(channel)
        c_mod = Comment.create(
            channel.id,
            "Ugx-ar-mod",
            VID,
            author_display_name="Spammer",
            text="Visit https://spam.example.com/crypto",
            published_at=NOW - timedelta(minutes=10),
        )
        with database.transaction() as connection:
            ChannelRepository(connection).add(channel)
            StrategyProfileRepository(connection).add(profile)
            CommentRepository(connection).add(c_mod)

        audit = AuditLog(InMemoryAuditSink(), clock=lambda: NOW)
        draft_svc = ReplyDraftService(
            database, MockTextGenerator(clock=lambda: NOW), audit, clock=lambda: NOW
        )
        manual_draft = draft_svc.create_manual_draft(
            c_mod.id,
            text="Please do not post links.",
            actor=USER,
            allow_moderation=True,
        )
        approval_svc = ReplyApprovalService(
            database,
            MockCommentProvider(clock=lambda: NOW),
            MockYouTubeOAuthProvider(clock=lambda: NOW),
            audit,
        )
        enabled_svc = AutoReplyService(
            database,
            draft_svc,
            approval_svc,
            audit,
            flags=FeatureFlags(auto_reply_enabled=True),
        )
        with pytest.raises(AutoReplyDisabledError) as exc_mod:
            enabled_svc.auto_post_draft(manual_draft.draft.id)
        assert (
            exc_mod.value.auto_reply_code
            is AutoReplyErrorCode.MODERATION_BLOCKED
        )
