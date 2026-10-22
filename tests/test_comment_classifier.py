"""Tests for comment classification service (P-184, prompt #184)."""

import dataclasses
from datetime import UTC, datetime, timedelta

import pytest

from ai_youtube_agent.content.channel import ChannelStatus
from ai_youtube_agent.content.comment import Comment, CommentLabel
from ai_youtube_agent.content.comment_classifier import (
    COMMENT_CLASSIFIED,
    CommentClassificationError,
    CommentClassificationErrorCode,
    CommentClassifier,
    evaluate_comment_rules,
)
from ai_youtube_agent.content.strategy import Brand
from ai_youtube_agent.core.audit import AuditLog, InMemoryAuditSink
from ai_youtube_agent.core.db.database import Database
from ai_youtube_agent.core.db.repositories.channel import (
    ChannelRepository,
    StrategyProfileRepository,
)
from ai_youtube_agent.core.db.repositories.community import CommentRepository
from factories import USER, make_channel, make_strategy_profile

NOW = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)
VID = "dQw4w9WgXcQ"


class TestCommentClassifier:
    def test_evaluates_moderation_attention_reply_candidate_and_no_action_rules(
        self, database: Database
    ) -> None:
        channel = make_channel(status=ChannelStatus.ACTIVE)
        brand = Brand(
            name="Money Minute",
            tone="educational",
            banned_phrases=("secret pump group",),
        )
        profile = dataclasses.replace(make_strategy_profile(channel), brand=brand)
        c_mod_phrase = Comment.create(
            channel.id,
            "Ugx-mod-1",
            VID,
            author_display_name="Spammer 1",
            text="Join our secret pump group today!",
            published_at=NOW - timedelta(minutes=40),
        )
        c_mod_link = Comment.create(
            channel.id,
            "Ugx-mod-2",
            VID,
            author_display_name="Spammer 2",
            text="Click https://scam.example.com/win now",
            published_at=NOW - timedelta(minutes=30),
        )
        c_attn = Comment.create(
            channel.id,
            "Ugx-attn-1",
            VID,
            author_display_name="Viewer A",
            text="Heads up: audio is out of sync after 02:15",
            published_at=NOW - timedelta(minutes=20),
        )
        c_reply = Comment.create(
            channel.id,
            "Ugx-rep-1",
            VID,
            author_display_name="Viewer B",
            text="How do you rebalance a 3-fund portfolio?",
            published_at=NOW - timedelta(minutes=10),
        )
        c_none = Comment.create(
            channel.id,
            "Ugx-none-1",
            VID,
            author_display_name="Viewer C",
            text="Awesome video, thanks!",
            published_at=NOW - timedelta(minutes=5),
        )
        with database.transaction() as connection:
            ChannelRepository(connection).add(channel)
            StrategyProfileRepository(connection).add(profile)
            repo = CommentRepository(connection)
            for c in (c_mod_phrase, c_mod_link, c_attn, c_reply, c_none):
                repo.add(c)

        sink = InMemoryAuditSink()
        classifier = CommentClassifier(
            database, AuditLog(sink, clock=lambda: NOW), clock=lambda: NOW
        )

        summary = classifier.classify_channel(channel.id)
        assert summary.created_count == 5
        assert summary.unchanged_count == 0
        assert summary.by_label == {
            "moderation": 2,
            "needs_attention": 1,
            "reply_candidate": 1,
            "no_action": 1,
        }
        assert all(e.action == COMMENT_CLASSIFIED for e in sink.entries)
        assert "secret pump group" not in str(sink.entries[0].metadata)

        # Re-running is idempotent
        repeat = classifier.classify_channel(channel.id)
        assert repeat.created_count == 0
        assert repeat.unchanged_count == 5

        # Manual override appends a new classification record while keeping history
        later = NOW + timedelta(minutes=1)
        override_classifier = CommentClassifier(
            database, AuditLog(sink, clock=lambda: later), clock=lambda: later
        )
        override = override_classifier.classify_comment(
            c_none.id,
            actor=USER,
            override_label=CommentLabel.REPLY_CANDIDATE,
            rationale="Owner wants to thank early supporter",
        )
        assert override.created is True
        assert override.classification.label is CommentLabel.REPLY_CANDIDATE
        history = classifier.history_for_comment(c_none.id)
        assert [h.label for h in history] == [
            CommentLabel.NO_ACTION,
            CommentLabel.REPLY_CANDIDATE,
        ]
        assert (
            len(classifier.list_by_label(channel.id, CommentLabel.REPLY_CANDIDATE)) == 2
        )

    def test_refuses_unknown_comment_or_channel_and_blank_rationale(
        self, database: Database
    ) -> None:
        classifier = CommentClassifier(
            database, AuditLog(InMemoryAuditSink()), clock=lambda: NOW
        )
        with pytest.raises(CommentClassificationError) as exc_nf:
            classifier.classify_comment("missing")
        assert (
            exc_nf.value.classification_code
            is CommentClassificationErrorCode.COMMENT_NOT_FOUND
        )

        with pytest.raises(CommentClassificationError) as exc_ch:
            classifier.classify_channel("missing-ch")
        assert (
            exc_ch.value.classification_code
            is CommentClassificationErrorCode.CHANNEL_NOT_FOUND
        )

        with pytest.raises(CommentClassificationError) as exc_rat:
            classifier.classify_comment("missing", rationale="   ")
        assert (
            exc_rat.value.classification_code
            is CommentClassificationErrorCode.INVALID_RATIONALE
        )

        assert (
            evaluate_comment_rules("Message me on WhatsApp +12345").label
            is CommentLabel.MODERATION
        )
