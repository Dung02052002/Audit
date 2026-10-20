"""Seeded in-memory YouTube Comment provider (Prompt Pack v8, prompt #182), context C15.

``MockCommentProvider`` is the deterministic comment provider for tests and local runs:

- Never connects to any external network and never invents comments.
- ``seed_comments(youtube_channel_id, youtube_video_id, comments)`` registers a
  video and stores its comments in deterministic ``(published_at, youtube_comment_id)``
  order; ``disable_comments`` marks a video as having comments disabled.
- ``list_comments`` enforces ``OAuthScope.YOUTUBE_READONLY`` or
  ``OAuthScope.YOUTUBE_FORCE_SSL`` when a token is supplied, filters out reply
  comments when ``query.include_replies is False``, and pages with ``o<offset>``
  page tokens.
- ``post_reply`` enforces ``OAuthScope.YOUTUBE_FORCE_SSL`` when a token is
  supplied, verifies that the parent comment exists on the requested video,
  returns a deterministic ``ReplyPostReceipt`` (reusing the stored receipt or
  raising ``DUPLICATE_REPLY`` on duplicate ``idempotency_key``), and appends the
  posted reply as a ``RemoteComment`` on the video thread.
"""

import hashlib
import re
import threading
from collections.abc import Callable, Sequence
from datetime import UTC, datetime

from ai_youtube_agent.content.comment import VIDEO_ID_PATTERN
from ai_youtube_agent.providers.comments import (
    CommentErrorCode,
    CommentPage,
    CommentProviderError,
    CommentQuery,
    RemoteComment,
    ReplyPostReceipt,
    ReplyPostRequest,
)
from ai_youtube_agent.providers.youtube_channel import YOUTUBE_CHANNEL_ID_PATTERN
from ai_youtube_agent.providers.youtube_oauth import (
    OAuthScope,
    OAuthToken,
    YouTubeOAuthError,
)

Clock = Callable[[], datetime]
_OFFSET_TOKEN = re.compile(r"o(\d{1,9})")


def deterministic_youtube_reply_id(
    parent_youtube_comment_id: str, idempotency_key: str
) -> str:
    """Derive a deterministic YouTube reply ID from parent comment ID and key."""
    digest = hashlib.sha256(
        f"{parent_youtube_comment_id}:{idempotency_key}".encode("utf-8")
    ).hexdigest()
    return f"Ugw-reply-{digest[:16]}"


class MockCommentProvider:
    """Thread-safe deterministic mock YouTube comment provider."""

    def __init__(
        self,
        *,
        name: str = "mock_comments",
        channel_author_display_name: str = "Channel Owner",
        reject_duplicate_replies: bool = False,
        clock: Clock | None = None,
    ) -> None:
        if not isinstance(name, str) or not name.strip():
            raise ValueError("name must not be empty")
        if (
            not isinstance(channel_author_display_name, str)
            or not channel_author_display_name.strip()
        ):
            raise ValueError("channel_author_display_name must not be empty")
        self._name = name.strip()
        self._owner_name = channel_author_display_name.strip()
        self._reject_duplicates = bool(reject_duplicate_replies)
        self._clock = clock
        self._lock = threading.Lock()
        self._video_channels: dict[str, str] = {}
        self._disabled_videos: set[str] = set()
        self._comments: dict[tuple[str, str], dict[str, RemoteComment]] = {}
        self._posted_by_key: dict[str, ReplyPostReceipt] = {}
        self._posted_replies: list[tuple[ReplyPostRequest, ReplyPostReceipt]] = []
        self._failures: list[CommentErrorCode] = []
        self._healthy = True
        self._list_calls = 0
        self._post_calls = 0

    @property
    def name(self) -> str:
        return self._name

    @property
    def list_calls(self) -> int:
        with self._lock:
            return self._list_calls

    @property
    def post_calls(self) -> int:
        with self._lock:
            return self._post_calls

    @property
    def posted_replies(
        self,
    ) -> tuple[tuple[ReplyPostRequest, ReplyPostReceipt], ...]:
        with self._lock:
            return tuple(self._posted_replies)

    def set_healthy(self, healthy: bool) -> None:
        with self._lock:
            self._healthy = bool(healthy)

    def fail_next(self, code: CommentErrorCode, times: int = 1) -> None:
        if not isinstance(code, CommentErrorCode):
            raise TypeError("code must be a CommentErrorCode")
        if isinstance(times, bool) or not isinstance(times, int) or times < 1:
            raise ValueError("times must be a whole number of 1 or more")
        with self._lock:
            self._failures.extend([code] * times)

    def clear(self) -> None:
        with self._lock:
            self._video_channels.clear()
            self._disabled_videos.clear()
            self._comments.clear()
            self._posted_by_key.clear()
            self._posted_replies.clear()
            self._failures.clear()
            self._list_calls = 0
            self._post_calls = 0

    def seed_comments(
        self,
        youtube_channel_id: str,
        youtube_video_id: str,
        comments: Sequence[RemoteComment] = (),
    ) -> None:
        if not isinstance(youtube_channel_id, str) or not (
            YOUTUBE_CHANNEL_ID_PATTERN.match(youtube_channel_id)
        ):
            raise ValueError("youtube_channel_id must match UC + 22 chars")
        if not isinstance(youtube_video_id, str) or not VIDEO_ID_PATTERN.match(
            youtube_video_id
        ):
            raise ValueError("youtube_video_id must be a valid 11-char video id")
        for item in comments:
            if not isinstance(item, RemoteComment):
                raise TypeError("comments must contain RemoteComment instances")
            if item.youtube_video_id != youtube_video_id:
                raise ValueError(
                    f"comment {item.youtube_comment_id} belongs to video {item.youtube_video_id}, expected {youtube_video_id}"
                )
        with self._lock:
            existing_ch = self._video_channels.get(youtube_video_id)
            if existing_ch is not None and existing_ch != youtube_channel_id:
                raise ValueError(
                    f"video {youtube_video_id} is already seeded for channel {existing_ch}"
                )
            self._video_channels[youtube_video_id] = youtube_channel_id
            bucket = self._comments.setdefault(
                (youtube_channel_id, youtube_video_id), {}
            )
            for item in comments:
                bucket[item.youtube_comment_id] = item

    def disable_comments(
        self,
        youtube_channel_id: str,
        youtube_video_id: str,
        *,
        disabled: bool = True,
    ) -> None:
        self.seed_comments(youtube_channel_id, youtube_video_id)
        with self._lock:
            if disabled:
                self._disabled_videos.add(youtube_video_id)
            else:
                self._disabled_videos.discard(youtube_video_id)

    def list_comments(
        self, query: CommentQuery, *, token: OAuthToken | None = None
    ) -> CommentPage:
        if not isinstance(query, CommentQuery):
            raise TypeError("query must be a CommentQuery")
        now = self._now()
        if token is not None:
            self._check_read_token(token, now)

        with self._lock:
            self._list_calls += 1
            self._maybe_fail()
            owner_ch = self._video_channels.get(query.youtube_video_id)
            if owner_ch is None or owner_ch != query.youtube_channel_id:
                raise CommentProviderError(
                    CommentErrorCode.NOT_FOUND,
                    f"video {query.youtube_video_id} not found on channel {query.youtube_channel_id}",
                    provider=self._name,
                )
            if query.youtube_video_id in self._disabled_videos:
                raise CommentProviderError(
                    CommentErrorCode.COMMENTS_DISABLED,
                    f"comments are disabled on video {query.youtube_video_id}",
                    provider=self._name,
                )
            stored = list(
                self._comments.get(
                    (query.youtube_channel_id, query.youtube_video_id), {}
                ).values()
            )

        if not query.include_replies:
            stored = [c for c in stored if not c.is_reply]
        stored.sort(key=lambda c: (c.published_at, c.youtube_comment_id))

        offset = 0
        if query.page_token is not None:
            match = _OFFSET_TOKEN.fullmatch(query.page_token)
            if match is None or (stored and int(match.group(1)) >= len(stored)) or (
                not stored and int(match.group(1)) > 0
            ):
                raise CommentProviderError(
                    CommentErrorCode.INVALID_REQUEST,
                    "the page token does not belong to this comment query",
                    provider=self._name,
                )
            offset = int(match.group(1))

        window = tuple(stored[offset : offset + query.page_size])
        end = offset + query.page_size
        next_token = f"o{end}" if end < len(stored) else None
        return CommentPage(
            comments=window,
            youtube_video_id=query.youtube_video_id,
            next_page_token=next_token,
            source=self._name,
            retrieved_at=now,
        )

    def post_reply(
        self, request: ReplyPostRequest, *, token: OAuthToken | None = None
    ) -> ReplyPostReceipt:
        if not isinstance(request, ReplyPostRequest):
            raise TypeError("request must be a ReplyPostRequest")
        now = self._now()
        if token is not None:
            self._check_write_token(token, request.channel_id, now)

        with self._lock:
            self._post_calls += 1
            self._maybe_fail()

            existing = self._posted_by_key.get(request.idempotency_key)
            if existing is not None:
                if self._reject_duplicates:
                    raise CommentProviderError(
                        CommentErrorCode.DUPLICATE_REPLY,
                        f"duplicate reply post for key {request.idempotency_key}",
                        provider=self._name,
                    )
                return existing

            owner_ch = self._video_channels.get(request.youtube_video_id)
            if owner_ch is None or owner_ch != request.youtube_channel_id:
                raise CommentProviderError(
                    CommentErrorCode.NOT_FOUND,
                    f"video {request.youtube_video_id} not found on channel {request.youtube_channel_id}",
                    provider=self._name,
                )
            if request.youtube_video_id in self._disabled_videos:
                raise CommentProviderError(
                    CommentErrorCode.COMMENTS_DISABLED,
                    f"comments are disabled on video {request.youtube_video_id}",
                    provider=self._name,
                )
            bucket = self._comments.setdefault(
                (request.youtube_channel_id, request.youtube_video_id), {}
            )
            parent = bucket.get(request.parent_youtube_comment_id)
            if parent is None:
                raise CommentProviderError(
                    CommentErrorCode.NOT_FOUND,
                    f"parent comment {request.parent_youtube_comment_id} was not found on video {request.youtube_video_id}",
                    provider=self._name,
                )
            top_level_parent_id = (
                parent.parent_comment_id
                if parent.parent_comment_id is not None
                else parent.youtube_comment_id
            )
            reply_yt_id = deterministic_youtube_reply_id(
                top_level_parent_id, request.idempotency_key
            )
            receipt = ReplyPostReceipt(
                youtube_reply_id=reply_yt_id,
                parent_youtube_comment_id=top_level_parent_id,
                youtube_video_id=request.youtube_video_id,
                channel_id=request.channel_id,
                reply_draft_id=request.reply_draft_id,
                idempotency_key=request.idempotency_key,
                posted_at=now,
                provider=self._name,
            )
            bucket[reply_yt_id] = RemoteComment(
                youtube_comment_id=reply_yt_id,
                youtube_video_id=request.youtube_video_id,
                parent_comment_id=top_level_parent_id,
                author_display_name=self._owner_name,
                text=request.text.strip(),
                published_at=now,
            )
            self._posted_by_key[request.idempotency_key] = receipt
            self._posted_replies.append((request, receipt))
            return receipt

    def check(self) -> None:
        with self._lock:
            if not self._healthy:
                raise CommentProviderError(
                    CommentErrorCode.UNAVAILABLE,
                    "mock comment provider marked unhealthy",
                    provider=self._name,
                )

    def _check_read_token(self, token: OAuthToken, now: datetime) -> None:
        try:
            token.require_valid(now, provider=self._name)
        except YouTubeOAuthError as exc:
            raise CommentProviderError(
                CommentErrorCode.UNAUTHORIZED,
                f"comment read token check failed: {exc.code}",
                provider=self._name,
                cause=exc,
            ) from exc
        if not (
            token.has_scopes((OAuthScope.YOUTUBE_READONLY,))
            or token.has_scopes((OAuthScope.YOUTUBE_FORCE_SSL,))
        ):
            raise CommentProviderError(
                CommentErrorCode.UNAUTHORIZED,
                "comment read token requires youtube.readonly or youtube.force-ssl scope",
                provider=self._name,
            )

    def _check_write_token(
        self, token: OAuthToken, channel_id: str, now: datetime
    ) -> None:
        try:
            token.require_valid(
                now,
                required_scopes=(OAuthScope.YOUTUBE_FORCE_SSL,),
                provider=self._name,
            )
        except YouTubeOAuthError as exc:
            raise CommentProviderError(
                CommentErrorCode.UNAUTHORIZED,
                f"comment write token check failed: {exc.code}",
                provider=self._name,
                cause=exc,
            ) from exc
        if token.channel_id != channel_id:
            raise CommentProviderError(
                CommentErrorCode.UNAUTHORIZED,
                f"token belongs to channel {token.channel_id}, not {channel_id}",
                provider=self._name,
            )

    def _maybe_fail(self) -> None:
        if self._failures:
            code = self._failures.pop(0)
            raise CommentProviderError(
                code,
                f"injected comment failure ({code.value})",
                provider=self._name,
            )

    def _now(self) -> datetime:
        return self._clock() if self._clock else datetime.now(UTC)
