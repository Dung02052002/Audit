"""Tests for reply draft generation service (P-185, prompt #185)."""

import dataclasses
from datetime import UTC, datetime, timedelta

import pytest

from ai_youtube_agent.content.channel import ChannelStatus
from ai_youtube_agent.content.comment import (
    Comment,
    ReplyStatus,
)
from ai_youtube_agent.content.comment_classifier import CommentClassifier
from ai_youtube_agent.content.reply_draft import (
    REPLY_DRAFTED,
    ReplyDraftError,
    ReplyDraftErrorCode,
    ReplyDraftService,
)
from ai_youtube_agent.content.strategy import Brand
from ai_youtube_agent.core.audit import AuditLog, InMemoryAuditSink
from ai_youtube_agent.core.db.database import Database
from ai_youtube_agent.core.db.repositories.channel import (
    ChannelRepository,
    StrategyProfileRepository,
)
from ai_youtube_agent.core.db.repositories.community import CommentRepository
from ai_youtube_agent.providers.mock_text_generation import MockTextGenerator
from factories import USER, make_channel, make_strategy_profile

NOW = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)
VID = "dQw4w9WgXcQ"


class TestReplyDraftService:
    def test_generates_draft_without_posting_and_reuses_identical_draft(
        self, database: Database
    ) -> None:
        channel = make_channel(status=ChannelStatus.ACTIVE)
        brand = Brand(
            name="Money Minute",
            tone="educational",
            banned_phrases=("guaranteed returns",),
        )
        profile = dataclasses.replace(make_strategy_profile(channel), brand=brand)
        c_question = Comment.create(
            channel.id,
            "Ugx-q1",
            VID,
            author_display_name="Viewer One",
            text="What expense ratio should I look for in an S&P 500 ETF?",
            published_at=NOW - timedelta(minutes=20),
        )
        c_mod = Comment.create(
            channel.id,
            "Ugx-m1",
            VID,
            author_display_name="Spammer",
            text="Join our WhatsApp group for free crypto!",
            published_at=NOW - timedelta(minutes=10),
        )
        with database.transaction() as connection:
            ChannelRepository(connection).add(channel)
            StrategyProfileRepository(connection).add(profile)
            repo = CommentRepository(connection)
            repo.add(c_question)
            repo.add(c_mod)

        sink = InMemoryAuditSink()
        audit = AuditLog(sink, clock=lambda: NOW)
        classifier = CommentClassifier(database, audit, clock=lambda: NOW)
        classifier.classify_channel(channel.id)

        text_gen = MockTextGenerator(clock=lambda: NOW)
        text_gen.queue("Look for an expense ratio below 0.05% for broad index ETFs!")
        service = ReplyDraftService(
            database, text_gen, audit, classifier=classifier, clock=lambda: NOW
        )

        # Draft for all REPLY_CANDIDATE comments on the channel
        outcomes = service.draft_for_candidates(channel.id)
        assert len(outcomes) == 1
        outcome = outcomes[0]
        assert outcome.created is True
        assert outcome.draft.status is ReplyStatus.DRAFT
        assert outcome.draft.youtube_reply_id is None
        assert outcome.is_posted is False
        assert (
            outcome.draft.text
            == "Look for an expense ratio below 0.05% for broad index ETFs!"
        )
        assert sink.entries[-1].action == REPLY_DRAFTED
        assert sink.entries[-1].metadata["posted"] is False
        assert "expense ratio" not in str(sink.entries[-1].metadata)

        # Re-generating identical text reuses the active DRAFT
        text_gen.queue("Look for an expense ratio below 0.05% for broad index ETFs!")
        repeat = service.generate_draft(c_question.id)
        assert repeat.created is False
        assert repeat.draft.id == outcome.draft.id

        # Moderation comment is refused for AI drafting
        with pytest.raises(ReplyDraftError) as exc_mod:
            service.generate_draft(c_mod.id)
        assert exc_mod.value.draft_code is ReplyDraftErrorCode.MODERATION_BLOCKED

    def test_blocks_banned_phrases_and_already_replied_comments(
        self, database: Database
    ) -> None:
        channel = make_channel(status=ChannelStatus.ACTIVE)
        brand = Brand(
            name="Money Minute",
            tone="friendly",
            banned_phrases=("guaranteed returns",),
        )
        profile = dataclasses.replace(make_strategy_profile(channel), brand=brand)
        comment = Comment.create(
            channel.id,
            "Ugx-q2",
            VID,
            author_display_name="Viewer Two",
            text="Can this strategy lose money?",
            published_at=NOW - timedelta(minutes=15),
        )
        with database.transaction() as connection:
            ChannelRepository(connection).add(channel)
            StrategyProfileRepository(connection).add(profile)
            CommentRepository(connection).add(comment)

        audit = AuditLog(InMemoryAuditSink(), clock=lambda: NOW)
        text_gen = MockTextGenerator(clock=lambda: NOW)
        service = ReplyDraftService(database, text_gen, audit, clock=lambda: NOW)

        # Generated text containing a banned phrase is rejected and not stored
        text_gen.queue("No, it offers guaranteed returns every month!")
        with pytest.raises(ReplyDraftError) as exc_ban:
            service.generate_draft(comment.id)
        assert exc_ban.value.draft_code is ReplyDraftErrorCode.BANNED_PHRASE
        assert service.list_for_comment(comment.id) == []

        # Manual draft with clean text succeeds and stays DRAFT
        manual = service.create_manual_draft(
            comment.id,
            text="All market investments carry risk; diversification helps manage it.",
            actor=USER,
        )
        assert manual.created is True
        assert manual.draft.status is ReplyStatus.DRAFT

        # Once a reply is POSTED for the comment, further drafting is refused
        posted = dataclasses.replace(
            manual.draft,
            status=ReplyStatus.POSTED,
            youtube_reply_id="Ugw-posted-1",
        )
        with database.transaction() as connection:
            CommentRepository(connection).update_reply_draft(
                posted, expected_status=ReplyStatus.DRAFT
            )

        with pytest.raises(ReplyDraftError) as exc_already:
            service.create_manual_draft(comment.id, text="Another reply", actor=USER)
        assert exc_already.value.draft_code is ReplyDraftErrorCode.ALREADY_REPLIED
