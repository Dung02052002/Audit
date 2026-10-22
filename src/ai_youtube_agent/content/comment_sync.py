"""Comment sync and pagination service.

(Prompt Pack v8, prompt #183), context C15 Community.

``CommentSync`` reads comments and replies for a channel's published videos from
``CommentProvider`` (#182) and persists new comments as immutable ``Comment``
rows (#026) in ``comments``:

- Channel must exist and be ``ACTIVE`` or ``PAUSED``.
- Target videos default to all videos published by the channel (via
  ``PublishResultService``, #155). When ``video_ids`` is given, every video id
  must have been published by this channel (``VIDEO_NOT_PUBLISHED``), preventing
  cross-channel reads.
- Obtains an OAuth token carrying ``OAuthScope.YOUTUBE_READONLY`` (or falls back
  to ``OAuthScope.YOUTUBE_FORCE_SSL``) and pages each video's comments up to
  ``max_pages_per_video`` (default 20), retrying retryable provider errors via
  ``call_with_retry``.
- Videos with comments disabled (``CommentErrorCode.COMMENTS_DISABLED``) are
  safely recorded in ``disabled_video_ids`` without aborting the sync of other
  videos.
- Idempotent by ``youtube_comment_id``: existing comments are returned with
  ``created=False``; cross-channel collisions raise ``CHANNEL_MISMATCH``.
- Audits ``comment.synced`` after commit with scalar counts only (never comment
  text or author names).
"""

import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from http import HTTPStatus
from typing import Any, Final

from ai_youtube_agent.content.channel import Channel, ChannelStatus
from ai_youtube_agent.content.channel_metrics import (
    CHANNEL_ENTITY,
    DEFAULT_SYNC_RETRY,
)
from ai_youtube_agent.content.comment import Comment
from ai_youtube_agent.core.audit import Actor, AuditLog, AuditResult, EntityRef
from ai_youtube_agent.core.db.database import Database
from ai_youtube_agent.core.db.repositories.channel import ChannelRepository
from ai_youtube_agent.core.db.repositories.community import CommentRepository
from ai_youtube_agent.core.errors import DomainError
from ai_youtube_agent.pipeline.publish_result import (
    PublishResultRecord,
    PublishResultService,
)
from ai_youtube_agent.pipeline.upload_retry import UploadRetryPolicy, call_with_retry
from ai_youtube_agent.providers.comments import (
    DEFAULT_PAGE_SIZE,
    MAX_PAGE_SIZE,
    CommentErrorCode,
    CommentPage,
    CommentProvider,
    CommentProviderError,
    CommentQuery,
    RemoteComment,
)
from ai_youtube_agent.providers.youtube_oauth import (
    OAuthErrorCode,
    OAuthScope,
    OAuthToken,
    YouTubeOAuthError,
    YouTubeOAuthProvider,
    get_or_renew_token,
)

COMMENTS_SYNCED: Final[str] = "comment.synced"
DEFAULT_MAX_PAGES_PER_VIDEO: Final[int] = 20


class CommentSyncErrorCode(StrEnum):
    CHANNEL_NOT_FOUND = "domain.comment_sync_channel_not_found"
    CHANNEL_NOT_SYNCABLE = "domain.comment_sync_channel_not_syncable"
    VIDEO_NOT_PUBLISHED = "domain.comment_sync_video_not_published"
    TOO_MANY_PAGES = "domain.comment_sync_too_many_pages"
    CHANNEL_MISMATCH = "domain.comment_sync_channel_mismatch"


USER_MESSAGES: Final[dict[CommentSyncErrorCode, str]] = {
    CommentSyncErrorCode.CHANNEL_NOT_FOUND: "The channel does not exist.",
    CommentSyncErrorCode.CHANNEL_NOT_SYNCABLE: (
        "Comments can only be synced for an active or paused channel."
    ),
    CommentSyncErrorCode.VIDEO_NOT_PUBLISHED: (
        "Comments can only be synced for videos published by this channel."
    ),
    CommentSyncErrorCode.TOO_MANY_PAGES: (
        "The comments source returned more pages than the configured page limit."
    ),
    CommentSyncErrorCode.CHANNEL_MISMATCH: (
        "A synced YouTube comment ID is already associated with another channel."
    ),
}

_STATUS: Final[dict[CommentSyncErrorCode, HTTPStatus]] = {
    CommentSyncErrorCode.CHANNEL_NOT_FOUND: HTTPStatus.NOT_FOUND,
    CommentSyncErrorCode.CHANNEL_NOT_SYNCABLE: HTTPStatus.CONFLICT,
    CommentSyncErrorCode.VIDEO_NOT_PUBLISHED: HTTPStatus.UNPROCESSABLE_ENTITY,
    CommentSyncErrorCode.TOO_MANY_PAGES: HTTPStatus.BAD_GATEWAY,
    CommentSyncErrorCode.CHANNEL_MISMATCH: HTTPStatus.CONFLICT,
}


class CommentSyncError(DomainError):
    default_code = CommentSyncErrorCode.CHANNEL_NOT_FOUND.value
    default_user_message = USER_MESSAGES[CommentSyncErrorCode.CHANNEL_NOT_FOUND]
    default_http_status = HTTPStatus.NOT_FOUND

    def __init__(self, code: CommentSyncErrorCode, detail: str) -> None:
        if not isinstance(code, CommentSyncErrorCode):
            raise TypeError("code must be a CommentSyncErrorCode")
        self.sync_code = code
        super().__init__(detail, code=code.value, user_message=USER_MESSAGES[code])
        self.default_http_status = _STATUS[code]


@dataclass(frozen=True)
class SyncedCommentOutcome:
    comment: Comment
    created: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "comment": self.comment.as_dict(),
            "created": self.created,
        }


@dataclass(frozen=True)
class CommentSyncResult:
    channel_id: str
    source: str | None
    requested_video_ids: tuple[str, ...]
    disabled_video_ids: tuple[str, ...]
    outcomes: tuple[SyncedCommentOutcome, ...]
    pages: int
    retrieved_at: datetime | None

    @property
    def comments(self) -> tuple[Comment, ...]:
        return tuple(outcome.comment for outcome in self.outcomes)

    @property
    def created_count(self) -> int:
        return sum(1 for outcome in self.outcomes if outcome.created)

    @property
    def unchanged_count(self) -> int:
        return sum(1 for outcome in self.outcomes if not outcome.created)

    def as_dict(self) -> dict[str, Any]:
        return {
            "channel_id": self.channel_id,
            "source": self.source,
            "requested_video_ids": list(self.requested_video_ids),
            "disabled_video_ids": list(self.disabled_video_ids),
            "outcomes": [outcome.as_dict() for outcome in self.outcomes],
            "created_count": self.created_count,
            "unchanged_count": self.unchanged_count,
            "pages": self.pages,
            "retrieved_at": (
                self.retrieved_at.isoformat() if self.retrieved_at else None
            ),
        }


class CommentSync:
    """Syncs and queries YouTube video comments and reply threads safely."""

    def __init__(
        self,
        database: Database,
        provider: CommentProvider,
        oauth_provider: YouTubeOAuthProvider,
        audit_log: AuditLog,
        *,
        results: PublishResultService | None = None,
        retry_policy: UploadRetryPolicy = DEFAULT_SYNC_RETRY,
        page_size: int = DEFAULT_PAGE_SIZE,
        max_pages_per_video: int = DEFAULT_MAX_PAGES_PER_VIDEO,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if isinstance(page_size, bool) or not 1 <= page_size <= MAX_PAGE_SIZE:
            raise ValueError(
                f"page_size must be a whole number from 1 to {MAX_PAGE_SIZE}"
            )
        if (
            isinstance(max_pages_per_video, bool)
            or not isinstance(max_pages_per_video, int)
            or max_pages_per_video < 1
        ):
            raise ValueError("max_pages_per_video must be a positive integer")
        self._database = database
        self._provider = provider
        self._oauth = oauth_provider
        self._results = results or PublishResultService(database)
        self._audit = audit_log
        self._retry = retry_policy
        self._page_size = page_size
        self._max_pages = max_pages_per_video
        self._sleep = sleep

    def sync(
        self,
        channel_id: str,
        *,
        actor: Actor,
        video_ids: Sequence[str] | None = None,
        include_replies: bool = True,
    ) -> CommentSyncResult:
        """Sync comments for a channel's published videos across pages."""
        if not isinstance(include_replies, bool):
            raise TypeError("include_replies must be a bool")
        channel = _load_syncable_channel(self._database, channel_id)
        published = {
            rec.youtube_video_id: rec
            for rec in self._results.list_for_channel(channel.id)
        }
        wanted = _select_published_videos(published, video_ids)

        remote_comments: list[RemoteComment] = []
        disabled_videos: list[str] = []
        pages: list[CommentPage] = []
        if wanted:
            token = self._acquire_read_token(channel.id)
            for vid in wanted:
                try:
                    self._read_video_pages(
                        channel.youtube.channel_id,
                        vid,
                        include_replies=include_replies,
                        token=token,
                        remote_comments=remote_comments,
                        pages=pages,
                    )
                except CommentProviderError as exc:
                    if exc.comment_code is CommentErrorCode.COMMENTS_DISABLED:
                        disabled_videos.append(vid)
                        continue
                    raise

        outcomes = self._store_comments(channel.id, remote_comments)
        source = pages[0].source if pages else None
        retrieved_at = max(page.retrieved_at for page in pages) if pages else None
        result = CommentSyncResult(
            channel_id=channel.id,
            source=source,
            requested_video_ids=wanted,
            disabled_video_ids=tuple(disabled_videos),
            outcomes=outcomes,
            pages=len(pages),
            retrieved_at=retrieved_at,
        )
        self._audit.record(
            COMMENTS_SYNCED,
            actor,
            EntityRef(CHANNEL_ENTITY, channel.id),
            AuditResult.SUCCESS,
            metadata={
                "channel_id": channel.id,
                "source": source,
                "requested_video_count": len(wanted),
                "disabled_video_count": len(disabled_videos),
                "created_count": result.created_count,
                "unchanged_count": result.unchanged_count,
                "pages": len(pages),
                "include_replies": include_replies,
            },
        )
        return result

    def list_for_channel(
        self,
        channel_id: str,
        *,
        youtube_video_id: str | None = None,
        include_replies: bool = True,
    ) -> list[Comment]:
        """List stored comments for ``channel_id``.

        Ordered by ``(published_at, id)``.
        """
        connection = self._database.connect()
        try:
            if ChannelRepository(connection).get(channel_id) is None:
                raise CommentSyncError(
                    CommentSyncErrorCode.CHANNEL_NOT_FOUND,
                    f"channel {channel_id} does not exist",
                )
            comments = CommentRepository(connection).list_by_channel(channel_id)
        finally:
            connection.close()
        return [
            c
            for c in comments
            if (youtube_video_id is None or c.youtube_video_id == youtube_video_id)
            and (include_replies or not c.is_reply)
        ]

    def list_for_video(
        self,
        channel_id: str,
        youtube_video_id: str,
        *,
        include_replies: bool = True,
    ) -> list[Comment]:
        """List stored comments for a specific video on ``channel_id``."""
        return self.list_for_channel(
            channel_id,
            youtube_video_id=youtube_video_id,
            include_replies=include_replies,
        )

    def _acquire_read_token(self, channel_id: str) -> OAuthToken:
        try:
            return get_or_renew_token(
                self._oauth, channel_id, required_scopes=(OAuthScope.YOUTUBE_READONLY,)
            )
        except YouTubeOAuthError as exc:
            if exc.oauth_code is OAuthErrorCode.INSUFFICIENT_SCOPE:
                return get_or_renew_token(
                    self._oauth,
                    channel_id,
                    required_scopes=(OAuthScope.YOUTUBE_FORCE_SSL,),
                )
            raise

    def _read_video_pages(
        self,
        youtube_channel_id: str,
        youtube_video_id: str,
        *,
        include_replies: bool,
        token: OAuthToken,
        remote_comments: list[RemoteComment],
        pages: list[CommentPage],
    ) -> None:
        page_token: str | None = None
        seen_tokens: set[str] = set()
        seen_comment_ids: dict[str, int] = {}
        for _ in range(self._max_pages):
            query = CommentQuery(
                youtube_channel_id=youtube_channel_id,
                youtube_video_id=youtube_video_id,
                include_replies=include_replies,
                page_size=self._page_size,
                page_token=page_token,
            )
            page = call_with_retry(
                lambda q=query: self._provider.list_comments(q, token=token),
                self._retry,
                sleep=self._sleep,
            )
            pages.append(page)
            for item in page.comments:
                if not include_replies and item.is_reply:
                    continue
                if item.youtube_comment_id in seen_comment_ids:
                    remote_comments[seen_comment_ids[item.youtube_comment_id]] = item
                else:
                    seen_comment_ids[item.youtube_comment_id] = len(remote_comments)
                    remote_comments.append(item)
            if page.next_page_token is None:
                return
            if page.next_page_token in seen_tokens:
                raise CommentSyncError(
                    CommentSyncErrorCode.TOO_MANY_PAGES,
                    f"repeated page_token {page.next_page_token!r} "
                    f"while syncing video {youtube_video_id}",
                )
            seen_tokens.add(page.next_page_token)
            page_token = page.next_page_token
        raise CommentSyncError(
            CommentSyncErrorCode.TOO_MANY_PAGES,
            f"more than {self._max_pages} comment pages for video {youtube_video_id}",
        )

    def _store_comments(
        self, channel_id: str, remote_comments: list[RemoteComment]
    ) -> tuple[SyncedCommentOutcome, ...]:
        if not remote_comments:
            return ()
        outcomes: list[SyncedCommentOutcome] = []
        with self._database.transaction() as connection:
            repository = CommentRepository(connection)
            for remote in remote_comments:
                existing = repository.get_by_youtube_comment_id(
                    remote.youtube_comment_id
                )
                if existing is not None:
                    if existing.channel_id != channel_id:
                        raise CommentSyncError(
                            CommentSyncErrorCode.CHANNEL_MISMATCH,
                            f"youtube_comment_id {remote.youtube_comment_id} "
                            f"belongs to channel {existing.channel_id}",
                        )
                    outcomes.append(SyncedCommentOutcome(existing, False))
                    continue
                comment = Comment.create(
                    channel_id,
                    remote.youtube_comment_id,
                    remote.youtube_video_id,
                    author_display_name=remote.author_display_name,
                    text=remote.text,
                    published_at=remote.published_at,
                    parent_comment_id=remote.parent_comment_id,
                )
                repository.add(comment)
                outcomes.append(SyncedCommentOutcome(comment, True))
        return tuple(outcomes)


def _load_syncable_channel(database: Database, channel_id: str) -> Channel:
    connection = database.connect()
    try:
        channel = ChannelRepository(connection).get(channel_id)
    finally:
        connection.close()
    if channel is None:
        raise CommentSyncError(
            CommentSyncErrorCode.CHANNEL_NOT_FOUND,
            f"channel {channel_id} does not exist",
        )
    if channel.status not in (ChannelStatus.ACTIVE, ChannelStatus.PAUSED):
        raise CommentSyncError(
            CommentSyncErrorCode.CHANNEL_NOT_SYNCABLE,
            f"channel {channel.id} is {channel.status.value}",
        )
    return channel


def _select_published_videos(
    published: dict[str, PublishResultRecord], video_ids: Sequence[str] | None
) -> tuple[str, ...]:
    if video_ids is None:
        return tuple(published)
    chosen = tuple(dict.fromkeys(video_ids))
    for video_id in chosen:
        if video_id not in published:
            raise CommentSyncError(
                CommentSyncErrorCode.VIDEO_NOT_PUBLISHED,
                f"video {video_id} was not published by this channel",
            )
    return chosen
