"""Tests for YouTube Comment provider contract and mock (P-182, prompt #182)."""

import dataclasses
from datetime import UTC, datetime, timedelta

import pytest

from ai_youtube_agent.providers.comments import (
    CommentErrorCode,
    CommentPage,
    CommentProvider,
    CommentProviderError,
    CommentQuery,
    RemoteComment,
    ReplyPostReceipt,
    ReplyPostRequest,
)
from ai_youtube_agent.providers.mock_comments import (
    MockCommentProvider,
    deterministic_youtube_reply_id,
)
from ai_youtube_agent.providers.mock_youtube_oauth import MockYouTubeOAuthProvider
from ai_youtube_agent.providers.youtube_oauth import OAuthScope

NOW = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)
YT_CH = "UC" + "a" * 22
VID = "dQw4w9WgXcQ"


class TestCommentProviderContract:
    def test_protocol_and_value_objects_validate_inputs(self) -> None:
        provider = MockCommentProvider(clock=lambda: NOW)
        assert isinstance(provider, CommentProvider)

        c1 = RemoteComment(
            youtube_comment_id="Ugx-top-1",
            youtube_video_id=VID,
            author_display_name="Viewer One",
            text="Great video!",
            published_at=NOW - timedelta(hours=2),
        )
        assert not c1.is_reply
        field_names = {f.name for f in dataclasses.fields(RemoteComment)}
        assert not field_names & {"author_channel_id", "email", "avatar_url"}

        reply = RemoteComment(
            youtube_comment_id="Ugx-reply-1",
            youtube_video_id=VID,
            parent_comment_id="Ugx-top-1",
            author_display_name="Viewer Two",
            text="Agreed!",
            published_at=NOW - timedelta(hours=1),
        )
        assert reply.is_reply

        with pytest.raises(ValueError):
            RemoteComment(
                youtube_comment_id="Ugx-self",
                youtube_video_id=VID,
                parent_comment_id="Ugx-self",
                author_display_name="Viewer",
                text="Self parent",
                published_at=NOW,
            )

        with pytest.raises(ValueError):
            CommentQuery(youtube_channel_id="bad", youtube_video_id=VID)

        with pytest.raises(ValueError):
            CommentPage(
                comments=(c1, c1),
                youtube_video_id=VID,
                next_page_token=None,
                source="mock_comments",
                retrieved_at=NOW,
            )

    def test_mock_provider_pages_comments_filters_replies_and_posts_idempotently(
        self,
    ) -> None:
        provider = MockCommentProvider(clock=lambda: NOW)
        oauth = MockYouTubeOAuthProvider(clock=lambda: NOW)
        read_token = oauth.seed_token(
            "ch-1", scopes=frozenset({OAuthScope.YOUTUBE_READONLY})
        )
        write_token = oauth.seed_token(
            "ch-1", scopes=frozenset({OAuthScope.YOUTUBE_FORCE_SSL})
        )

        c1 = RemoteComment(
            youtube_comment_id="Ugx-1",
            youtube_video_id=VID,
            author_display_name="Alice",
            text="Question 1?",
            published_at=NOW - timedelta(minutes=30),
        )
        c2 = RemoteComment(
            youtube_comment_id="Ugx-2",
            youtube_video_id=VID,
            parent_comment_id="Ugx-1",
            author_display_name="Bob",
            text="Reply to 1",
            published_at=NOW - timedelta(minutes=20),
        )
        c3 = RemoteComment(
            youtube_comment_id="Ugx-3",
            youtube_video_id=VID,
            author_display_name="Carol",
            text="Question 2?",
            published_at=NOW - timedelta(minutes=10),
        )
        provider.seed_comments(YT_CH, VID, (c3, c1, c2))

        # Page 1 (size 2, include_replies=True) -> c1, c2
        p1 = provider.list_comments(
            CommentQuery(
                youtube_channel_id=YT_CH,
                youtube_video_id=VID,
                page_size=2,
            ),
            token=read_token,
        )
        assert [c.youtube_comment_id for c in p1.comments] == ["Ugx-1", "Ugx-2"]
        assert p1.next_page_token == "o2"
        assert not p1.is_last

        # Page 2 -> c3
        p2 = provider.list_comments(
            CommentQuery(
                youtube_channel_id=YT_CH,
                youtube_video_id=VID,
                page_size=2,
                page_token=p1.next_page_token,
            ),
            token=read_token,
        )
        assert [c.youtube_comment_id for c in p2.comments] == ["Ugx-3"]
        assert p2.is_last

        # include_replies=False returns only top-level comments
        top_only = provider.list_comments(
            CommentQuery(
                youtube_channel_id=YT_CH,
                youtube_video_id=VID,
                include_replies=False,
            ),
            token=read_token,
        )
        assert [c.youtube_comment_id for c in top_only.comments] == [
            "Ugx-1",
            "Ugx-3",
        ]

        # Posting a reply with read-only token is rejected as UNAUTHORIZED
        req = ReplyPostRequest(
            channel_id="ch-1",
            youtube_channel_id=YT_CH,
            youtube_video_id=VID,
            parent_youtube_comment_id="Ugx-1",
            reply_draft_id="draft-1",
            text="Thanks for asking!",
            idempotency_key="rpl:draft-1",
        )
        with pytest.raises(CommentProviderError) as exc_auth:
            provider.post_reply(req, token=read_token)
        assert exc_auth.value.comment_code is CommentErrorCode.UNAUTHORIZED

        # Posting with YOUTUBE_FORCE_SSL succeeds and is idempotent by key
        receipt = provider.post_reply(req, token=write_token)
        assert receipt.youtube_reply_id == deterministic_youtube_reply_id(
            "Ugx-1", "rpl:draft-1"
        )
        assert provider.post_reply(req, token=write_token) == receipt
        assert len(provider.posted_replies) == 1

        # Posted reply is now visible when listing comments with replies
        all_after = provider.list_comments(
            CommentQuery(youtube_channel_id=YT_CH, youtube_video_id=VID),
            token=write_token,
        )
        assert len(all_after.comments) == 4
        assert all_after.comments[-1].youtube_comment_id == receipt.youtube_reply_id
        assert all_after.comments[-1].parent_comment_id == "Ugx-1"

    def test_disabled_comments_and_injected_failures(self) -> None:
        provider = MockCommentProvider(
            reject_duplicate_replies=True, clock=lambda: NOW
        )
        provider.disable_comments(YT_CH, VID, disabled=True)

        with pytest.raises(CommentProviderError) as exc_dis:
            provider.list_comments(
                CommentQuery(youtube_channel_id=YT_CH, youtube_video_id=VID)
            )
        assert exc_dis.value.comment_code is CommentErrorCode.COMMENTS_DISABLED
        assert exc_dis.value.retryable is False

        provider.disable_comments(YT_CH, VID, disabled=False)
        provider.fail_next(CommentErrorCode.RATE_LIMITED, times=1)
        with pytest.raises(CommentProviderError) as exc_rl:
            provider.list_comments(
                CommentQuery(youtube_channel_id=YT_CH, youtube_video_id=VID)
            )
        assert exc_rl.value.comment_code is CommentErrorCode.RATE_LIMITED
        assert exc_rl.value.retryable is True
