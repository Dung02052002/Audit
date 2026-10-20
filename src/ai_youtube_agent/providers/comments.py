"""YouTube Comment provider interface (Prompt Pack v8, prompt #182), context C15 Community.

Defines how the agent reads video comments (top-level threads and replies) and
posts approved replies to YouTube without ever exposing private viewer data or
auto-posting by default (#026, #183–#188):

- ``CommentQuery``: one page request for a published YouTube video's comments,
  with ``youtube_channel_id``, ``youtube_video_id``, ``include_replies``,
  ``page_size`` (1..100), and an opaque ``page_token``.
- ``RemoteComment``: a single YouTube comment or reply carrying only what the
  community features need (``youtube_comment_id``, ``youtube_video_id``,
  ``parent_comment_id``, ``author_display_name``, ``text``, ``published_at``).
  No author channel id, email, or avatar URL (#026).
- ``CommentPage``: a page of ``RemoteComment`` items with ``next_page_token``,
  ``source``, and ``retrieved_at``.
- ``ReplyPostRequest``: validated payload for posting an approved reply to a
  parent YouTube comment, carrying a deterministic ``idempotency_key`` (#187).
- ``ReplyPostReceipt``: immutable confirmation with the assigned
  ``youtube_reply_id`` and ``posted_at`` UTC timestamp.
- ``CommentErrorCode`` and ``CommentProviderError(ProviderError)``: typed
  provider errors with retryability flags and safe user-facing messages.
- ``CommentProvider``: synchronous ``@runtime_checkable`` Protocol (``name``,
  ``list_comments``, ``post_reply``, ``check``).
"""

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any, Final, Protocol, runtime_checkable

from ai_youtube_agent.content.analytics import SOURCE_PATTERN
from ai_youtube_agent.content.comment import VIDEO_ID_PATTERN
from ai_youtube_agent.core.errors import ProviderError
from ai_youtube_agent.providers.youtube_channel import YOUTUBE_CHANNEL_ID_PATTERN
from ai_youtube_agent.providers.youtube_oauth import OAuthToken

MAX_PAGE_SIZE: Final[int] = 100
DEFAULT_PAGE_SIZE: Final[int] = 50
MAX_PAGE_TOKEN_LENGTH: Final[int] = 200
MAX_COMMENT_TEXT_LENGTH: Final[int] = 10_000
MAX_REPLY_TEXT_LENGTH: Final[int] = 10_000
MAX_AUTHOR_NAME_LENGTH: Final[int] = 200

_PAGE_TOKEN_PATTERN: Final[re.Pattern[str]] = re.compile(r"[A-Za-z0-9_.:=-]+")
COMMENT_ID_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")


class CommentErrorCode(StrEnum):
    UNAVAILABLE = "comment.unavailable"
    TIMEOUT = "comment.timeout"
    RATE_LIMITED = "comment.rate_limited"
    QUOTA_EXCEEDED = "comment.quota_exceeded"
    UNAUTHORIZED = "comment.unauthorized"
    NOT_FOUND = "comment.not_found"
    COMMENTS_DISABLED = "comment.comments_disabled"
    INVALID_REQUEST = "comment.invalid_request"
    DUPLICATE_REPLY = "comment.duplicate_reply"


RETRYABLE_COMMENT_CODES: Final[frozenset[CommentErrorCode]] = frozenset(
    {
        CommentErrorCode.UNAVAILABLE,
        CommentErrorCode.TIMEOUT,
        CommentErrorCode.RATE_LIMITED,
    }
)

USER_MESSAGES: Final[dict[CommentErrorCode, str]] = {
    CommentErrorCode.UNAVAILABLE: (
        "The YouTube comments service is temporarily unavailable."
    ),
    CommentErrorCode.TIMEOUT: "The YouTube comments request timed out.",
    CommentErrorCode.RATE_LIMITED: (
        "YouTube comments requests are currently rate-limited."
    ),
    CommentErrorCode.QUOTA_EXCEEDED: (
        "The YouTube comments API quota has been exceeded."
    ),
    CommentErrorCode.UNAUTHORIZED: (
        "YouTube comments could not be accessed with the current authorization."
    ),
    CommentErrorCode.NOT_FOUND: (
        "The requested YouTube video or comment was not found."
    ),
    CommentErrorCode.COMMENTS_DISABLED: (
        "Comments are disabled on the requested YouTube video."
    ),
    CommentErrorCode.INVALID_REQUEST: "The YouTube comments request was invalid.",
    CommentErrorCode.DUPLICATE_REPLY: (
        "A reply with this idempotency key has already been posted."
    ),
}


class CommentProviderError(ProviderError):
    default_code = CommentErrorCode.UNAVAILABLE.value
    default_user_message = USER_MESSAGES[CommentErrorCode.UNAVAILABLE]

    def __init__(
        self,
        code: CommentErrorCode,
        detail: str,
        *,
        provider: str = "comment",
        cause: Exception | None = None,
    ) -> None:
        if not isinstance(code, CommentErrorCode):
            raise TypeError("code must be a CommentErrorCode")
        self.comment_code = code
        super().__init__(
            detail,
            provider=provider,
            code=code.value,
            user_message=USER_MESSAGES[code],
            retryable=code in RETRYABLE_COMMENT_CODES,
        )
        self.__cause__ = cause


def _require_utc(moment: object, name: str) -> None:
    if not isinstance(moment, datetime) or moment.utcoffset() != timedelta(0):
        raise ValueError(f"{name} must be timezone-aware UTC")


@dataclass(frozen=True)
class CommentQuery:
    youtube_channel_id: str
    youtube_video_id: str
    include_replies: bool = True
    page_size: int = DEFAULT_PAGE_SIZE
    page_token: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.youtube_channel_id, str) or not (
            YOUTUBE_CHANNEL_ID_PATTERN.match(self.youtube_channel_id)
        ):
            raise ValueError(
                "youtube_channel_id must be 'UC' followed by 22 characters"
            )
        if not isinstance(self.youtube_video_id, str) or not VIDEO_ID_PATTERN.match(
            self.youtube_video_id
        ):
            raise ValueError(
                "youtube_video_id must be 11 letters, digits, '_' or '-'"
            )
        if not isinstance(self.include_replies, bool):
            raise TypeError("include_replies must be a bool")
        if (
            isinstance(self.page_size, bool)
            or not isinstance(self.page_size, int)
            or not 1 <= self.page_size <= MAX_PAGE_SIZE
        ):
            raise ValueError(
                f"page_size must be a whole number from 1 to {MAX_PAGE_SIZE}"
            )
        if self.page_token is not None and (
            not isinstance(self.page_token, str)
            or not 1 <= len(self.page_token) <= MAX_PAGE_TOKEN_LENGTH
            or not _PAGE_TOKEN_PATTERN.fullmatch(self.page_token)
        ):
            raise ValueError("page_token must be a short opaque token")


@dataclass(frozen=True)
class RemoteComment:
    youtube_comment_id: str
    youtube_video_id: str
    author_display_name: str
    text: str
    published_at: datetime
    parent_comment_id: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(
            self.youtube_comment_id, str
        ) or not COMMENT_ID_PATTERN.match(self.youtube_comment_id):
            raise ValueError(
                "youtube_comment_id must be a non-empty YouTube comment identifier"
            )
        if not isinstance(self.youtube_video_id, str) or not VIDEO_ID_PATTERN.match(
            self.youtube_video_id
        ):
            raise ValueError(
                "youtube_video_id must be 11 letters, digits, '_' or '-'"
            )
        if (
            not isinstance(self.author_display_name, str)
            or not self.author_display_name.strip()
            or len(self.author_display_name) > MAX_AUTHOR_NAME_LENGTH
        ):
            raise ValueError(
                f"author_display_name must be 1..{MAX_AUTHOR_NAME_LENGTH} non-blank characters"
            )
        if (
            not isinstance(self.text, str)
            or not self.text.strip()
            or len(self.text) > MAX_COMMENT_TEXT_LENGTH
        ):
            raise ValueError(
                f"text must be 1..{MAX_COMMENT_TEXT_LENGTH} non-blank characters"
            )
        if self.parent_comment_id is not None:
            if not isinstance(
                self.parent_comment_id, str
            ) or not COMMENT_ID_PATTERN.match(self.parent_comment_id):
                raise ValueError(
                    "parent_comment_id must be a valid YouTube comment identifier"
                )
            if self.parent_comment_id == self.youtube_comment_id:
                raise ValueError("a comment cannot be its own parent")
        _require_utc(self.published_at, "published_at")

    @property
    def is_reply(self) -> bool:
        return self.parent_comment_id is not None

    def as_dict(self) -> dict[str, Any]:
        return {
            "youtube_comment_id": self.youtube_comment_id,
            "youtube_video_id": self.youtube_video_id,
            "parent_comment_id": self.parent_comment_id,
            "author_display_name": self.author_display_name,
            "text": self.text,
            "published_at": self.published_at.isoformat(),
        }


@dataclass(frozen=True)
class CommentPage:
    comments: tuple[RemoteComment, ...]
    youtube_video_id: str
    next_page_token: str | None
    source: str
    retrieved_at: datetime

    def __post_init__(self) -> None:
        if not isinstance(self.comments, tuple) or any(
            not isinstance(c, RemoteComment) for c in self.comments
        ):
            raise TypeError("comments must be a tuple of RemoteComment values")
        if not isinstance(self.youtube_video_id, str) or not VIDEO_ID_PATTERN.match(
            self.youtube_video_id
        ):
            raise ValueError(
                "youtube_video_id must be 11 letters, digits, '_' or '-'"
            )
        if any(c.youtube_video_id != self.youtube_video_id for c in self.comments):
            raise ValueError(
                "every comment in a CommentPage must belong to page.youtube_video_id"
            )
        ids = [c.youtube_comment_id for c in self.comments]
        if len(set(ids)) != len(ids):
            raise ValueError(
                "a CommentPage must not contain duplicate youtube_comment_id values"
            )
        if not isinstance(self.source, str) or not SOURCE_PATTERN.match(self.source):
            raise ValueError("source must be a lowercase provider name")
        _require_utc(self.retrieved_at, "retrieved_at")
        if self.next_page_token is not None and not self.next_page_token.strip():
            raise ValueError("next_page_token must not be blank")

    @property
    def is_last(self) -> bool:
        return self.next_page_token is None

    def as_dict(self) -> dict[str, Any]:
        return {
            "comments": [c.as_dict() for c in self.comments],
            "youtube_video_id": self.youtube_video_id,
            "next_page_token": self.next_page_token,
            "source": self.source,
            "retrieved_at": self.retrieved_at.isoformat(),
            "is_last": self.is_last,
        }


@dataclass(frozen=True)
class ReplyPostRequest:
    channel_id: str
    youtube_channel_id: str
    youtube_video_id: str
    parent_youtube_comment_id: str
    reply_draft_id: str
    text: str
    idempotency_key: str

    def __post_init__(self) -> None:
        for field_name in ("channel_id", "reply_draft_id", "idempotency_key"):
            val = getattr(self, field_name)
            if not isinstance(val, str) or not val.strip():
                raise ValueError(f"{field_name} must not be empty")
        if any(ch.isspace() for ch in self.idempotency_key):
            raise ValueError("idempotency_key must not contain whitespace")
        if not isinstance(self.youtube_channel_id, str) or not (
            YOUTUBE_CHANNEL_ID_PATTERN.match(self.youtube_channel_id)
        ):
            raise ValueError("youtube_channel_id must match UC + 22 chars")
        if not isinstance(self.youtube_video_id, str) or not VIDEO_ID_PATTERN.match(
            self.youtube_video_id
        ):
            raise ValueError(
                "youtube_video_id must be 11 letters, digits, '_' or '-'"
            )
        if not isinstance(
            self.parent_youtube_comment_id, str
        ) or not COMMENT_ID_PATTERN.match(self.parent_youtube_comment_id):
            raise ValueError(
                "parent_youtube_comment_id must be a valid YouTube comment identifier"
            )
        if (
            not isinstance(self.text, str)
            or not self.text.strip()
            or len(self.text.strip()) > MAX_REPLY_TEXT_LENGTH
        ):
            raise ValueError(
                f"text must be 1..{MAX_REPLY_TEXT_LENGTH} non-blank characters"
            )

    def as_dict(self) -> dict[str, Any]:
        return {
            "channel_id": self.channel_id,
            "youtube_channel_id": self.youtube_channel_id,
            "youtube_video_id": self.youtube_video_id,
            "parent_youtube_comment_id": self.parent_youtube_comment_id,
            "reply_draft_id": self.reply_draft_id,
            "text": self.text,
            "idempotency_key": self.idempotency_key,
        }


@dataclass(frozen=True)
class ReplyPostReceipt:
    youtube_reply_id: str
    parent_youtube_comment_id: str
    youtube_video_id: str
    channel_id: str
    reply_draft_id: str
    idempotency_key: str
    posted_at: datetime
    provider: str

    def __post_init__(self) -> None:
        for id_name in ("youtube_reply_id", "parent_youtube_comment_id"):
            val = getattr(self, id_name)
            if not isinstance(val, str) or not COMMENT_ID_PATTERN.match(val):
                raise ValueError(f"{id_name} must be a valid YouTube comment id")
        if self.youtube_reply_id == self.parent_youtube_comment_id:
            raise ValueError(
                "youtube_reply_id must differ from parent_youtube_comment_id"
            )
        if not isinstance(self.youtube_video_id, str) or not VIDEO_ID_PATTERN.match(
            self.youtube_video_id
        ):
            raise ValueError(
                "youtube_video_id must be 11 letters, digits, '_' or '-'"
            )
        for str_name in ("channel_id", "reply_draft_id", "idempotency_key", "provider"):
            val = getattr(self, str_name)
            if not isinstance(val, str) or not val.strip():
                raise ValueError(f"{str_name} must not be empty")
        _require_utc(self.posted_at, "posted_at")

    def as_dict(self) -> dict[str, Any]:
        return {
            "youtube_reply_id": self.youtube_reply_id,
            "parent_youtube_comment_id": self.parent_youtube_comment_id,
            "youtube_video_id": self.youtube_video_id,
            "channel_id": self.channel_id,
            "reply_draft_id": self.reply_draft_id,
            "idempotency_key": self.idempotency_key,
            "posted_at": self.posted_at.isoformat(),
            "provider": self.provider,
        }


@runtime_checkable
class CommentProvider(Protocol):
    @property
    def name(self) -> str: ...

    def list_comments(
        self, query: CommentQuery, *, token: OAuthToken | None = None
    ) -> CommentPage: ...

    def post_reply(
        self, request: ReplyPostRequest, *, token: OAuthToken | None = None
    ) -> ReplyPostReceipt: ...

    def check(self) -> None: ...
