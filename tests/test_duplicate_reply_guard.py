"""Tests for duplicate reply guard (P-187, prompt #187)."""

import dataclasses
from datetime import UTC, datetime, timedelta

import pytest

from ai_youtube_agent.content.channel import ChannelStatus
from ai_youtube_agent.content.comment import Comment, ReplyDraft, ReplyStatus
from ai_youtube_agent.content.duplicate_reply_guard import (
    REPLY_DUPLICATE_BLOCKED,
    DuplicateReplyError,
    DuplicateReplyErrorCode,
    DuplicateReplyGuard,
    ReplyDuplicateDecision,
    concurrent_reply_post,
)
from ai_youtube_agent.core.audit import AuditLog, AuditResult, InMemoryAuditSink
from ai_youtube_agent.core.db.database import ConcurrencyError, Database
from ai_youtube_agent.core.db.repositories.channel import ChannelRepository
from ai_youtube_agent.core.db.repositories.community import CommentRepository
from factories import USER, make_channel

NOW = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)
VID = "dQw4w9WgXcQ"


class TestDuplicateReplyGuard:
    def test_blocks_comment_thread_and_identical_video_reply_duplicates(
        self, database: Database
    ) -> None:
        channel = make_channel(status=ChannelStatus.ACTIVE)
        c_top = Comment.create(
            channel.id,
            "Ugx-thread-1",
            VID,
            author_display_name="Viewer 1",
            text="Top level comment",
            published_at=NOW - timedelta(minutes=30),
        )
        c_in_thread = Comment.create(
            channel.id,
            "Ugx-thread-1-sub",
            VID,
            parent_comment_id="Ugx-thread-1",
            author_display_name="Viewer 2",
            text="Follow up in same thread",
            published_at=NOW - timedelta(minutes=20),
        )
        c_other_thread = Comment.create(
            channel.id,
            "Ugx-thread-2",
            VID,
            author_display_name="Viewer 3",
            text="Another question on same video",
            published_at=NOW - timedelta(minutes=10),
        )

        d_posted = dataclasses.replace(
            ReplyDraft.create(
                c_top.id,
                "Thanks for watching! Here is the answer.",
                created_by=USER,
                clock=lambda: NOW,
            ),
            status=ReplyStatus.POSTED,
            youtube_reply_id="Ugw-posted-1",
        )
        d_same_comment = ReplyDraft.create(
            c_top.id,
            "Different text on same comment",
            created_by=USER,
            clock=lambda: NOW,
        )
        d_same_thread = ReplyDraft.create(
            c_in_thread.id,
            "Different text in same thread",
            created_by=USER,
            clock=lambda: NOW,
        )
        d_identical_video = ReplyDraft.create(
            c_other_thread.id,
            "  thanks for watching!   here is the answer. ",
            created_by=USER,
            clock=lambda: NOW,
        )
        d_distinct_video = ReplyDraft.create(
            c_other_thread.id,
            "Completely distinct helpful answer for thread 2!",
            created_by=USER,
            clock=lambda: NOW,
        )

        with database.transaction() as connection:
            ChannelRepository(connection).add(channel)
            repo = CommentRepository(connection)
            for c in (c_top, c_in_thread, c_other_thread):
                repo.add(c)
            for d in (
                d_posted,
                d_same_comment,
                d_same_thread,
                d_identical_video,
                d_distinct_video,
            ):
                repo.add_reply_draft(d)

        sink = InMemoryAuditSink()
        guard = DuplicateReplyGuard(database, AuditLog(sink, clock=lambda: NOW))

        # 1. Already POSTED draft returns REPLAY
        replay_check = guard.ensure_can_post(d_posted, comment=c_top, actor=USER)
        assert replay_check.decision is ReplyDuplicateDecision.REPLAY

        # 2. Second draft on the same comment is blocked with COMMENT_ALREADY_REPLIED
        with pytest.raises(DuplicateReplyError) as exc_same:
            guard.ensure_can_post(d_same_comment, comment=c_top, actor=USER)
        assert (
            exc_same.value.duplicate_code
            is DuplicateReplyErrorCode.COMMENT_ALREADY_REPLIED
        )
        assert sink.entries[-1].action == REPLY_DUPLICATE_BLOCKED
        assert sink.entries[-1].result is AuditResult.DENIED

        # 3. Draft on another comment in the same thread is blocked with
        # THREAD_ALREADY_REPLIED
        with pytest.raises(DuplicateReplyError) as exc_thread:
            guard.ensure_can_post(d_same_thread, comment=c_in_thread, actor=USER)
        assert (
            exc_thread.value.duplicate_code
            is DuplicateReplyErrorCode.THREAD_ALREADY_REPLIED
        )

        # 4. Identical normalized text on another thread of the same video is blocked
        with pytest.raises(DuplicateReplyError) as exc_text:
            guard.ensure_can_post(d_identical_video, comment=c_other_thread, actor=USER)
        assert (
            exc_text.value.duplicate_code
            is DuplicateReplyErrorCode.IDENTICAL_TEXT_ON_VIDEO
        )

        # 5. Distinct text on another thread of the same video proceeds
        ok_check = guard.ensure_can_post(
            d_distinct_video, comment=c_other_thread, actor=USER
        )
        assert ok_check.decision is ReplyDuplicateDecision.PROCEED

        # 6. concurrent_reply_post wraps ConcurrencyError
        err = concurrent_reply_post(ConcurrencyError("stale"), d_same_comment.id)
        assert err.duplicate_code is DuplicateReplyErrorCode.CONCURRENT
