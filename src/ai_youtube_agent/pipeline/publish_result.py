"""Publish result (Prompt Pack v8, prompt #155), context C12 Publishing.

Two parts keep the confirmed YouTube video id honest:

``confirm_receipt`` (used by ``PublishExecutor`` before it stores anything) checks
that the receipt the provider returned is for *this* publish: same internal and
YouTube channel, same upload key, same video sha256, same privacy status, and the
same ``contains_synthetic_media`` flag as the payload (so a mismatch of the AI
disclosure is never recorded as a success). A mismatching receipt raises
``PublishReceiptError``; nothing is stored as published, the job fails and the item
moves to ``FAILED`` for a human to look at (a video may exist on YouTube).

``PublishResultService`` reads the stored result back. The confirmed result lives on
the succeeded ``PublishJob`` (``result_youtube_video_id``, ``result_published_at``),
written in the same transaction that moves the item to ``PUBLISHED``; the receipt
details that have no column (privacy, disclosure flag) are read from the
``<prefix>.published`` audit event when a sink is given. A succeeded job whose item
is not ``PUBLISHED`` is reported as inconsistent, never silently returned.

The result carries only the video id, the watch URL and scalars; it never holds a
token, a file or text of the video.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from http import HTTPStatus
from typing import Any, Final

from ai_youtube_agent.core.audit import AuditSink
from ai_youtube_agent.core.content_item import ContentStatus, ContentType
from ai_youtube_agent.core.db.database import Database
from ai_youtube_agent.core.db.repositories.channel import ChannelRepository
from ai_youtube_agent.core.db.repositories.content import ContentItemRepository
from ai_youtube_agent.core.db.repositories.publish import PublishJobRepository
from ai_youtube_agent.core.errors import DomainError
from ai_youtube_agent.pipeline.publish import PublishJob, PublishStatus
from ai_youtube_agent.providers.youtube_upload import (
    YouTubeUploadPayload,
    YouTubeUploadReceipt,
)

PUBLISHED_ACTIONS: Final[frozenset[str]] = frozenset(
    {"shorts.published", "longform.published"}
)
WATCH_URL: Final[str] = "https://www.youtube.com/watch?v={video_id}"
SHORTS_URL: Final[str] = "https://www.youtube.com/shorts/{video_id}"


class PublishReceiptError(DomainError):
    default_code = "domain.publish_receipt_mismatch"
    default_user_message = (
        "YouTube confirmed a video that does not match this publish, so it was "
        "not recorded as published. Check the channel before trying again."
    )
    default_http_status = HTTPStatus.CONFLICT

    def __init__(self, mismatches: Sequence[str]) -> None:
        self.mismatches = tuple(mismatches)
        super().__init__(
            "the upload receipt differs from the payload in: "
            + ", ".join(self.mismatches)
        )


class PublishResultNotFoundError(DomainError):
    default_code = "domain.publish_result_not_found"
    default_user_message = "There is no published result for this content."
    default_http_status = HTTPStatus.NOT_FOUND


class PublishResultInconsistentError(DomainError):
    default_code = "domain.publish_result_inconsistent"
    default_user_message = (
        "The stored publish result and the content status disagree. "
        "It needs to be checked before it can be shown."
    )
    default_http_status = HTTPStatus.CONFLICT


def confirm_receipt(
    receipt: YouTubeUploadReceipt, payload: YouTubeUploadPayload
) -> None:
    """Raise ``PublishReceiptError`` unless the receipt answers this payload."""
    mismatches: list[str] = []
    if receipt.channel_id != payload.channel_id:
        mismatches.append("channel_id")
    if receipt.youtube_channel_id != payload.youtube_channel_id:
        mismatches.append("youtube_channel_id")
    if receipt.idempotency_key != payload.idempotency_key:
        mismatches.append("idempotency_key")
    if receipt.video_sha256 != payload.video_sha256:
        mismatches.append("video_sha256")
    if receipt.privacy_status is not payload.status.privacy_status:
        mismatches.append("privacy_status")
    if receipt.contains_synthetic_media != payload.status.contains_synthetic_media:
        mismatches.append("contains_synthetic_media")
    if mismatches:
        raise PublishReceiptError(mismatches)


@dataclass(frozen=True)
class PublishResultRecord:
    job_id: str
    content_item_id: str
    channel_id: str
    youtube_channel_id: str
    content_type: ContentType
    youtube_video_id: str
    watch_url: str
    published_at: datetime
    approval_request_id: str
    job_key: str
    attempts: int
    privacy_status: str | None = None
    contains_synthetic_media: bool | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "content_item_id": self.content_item_id,
            "channel_id": self.channel_id,
            "youtube_channel_id": self.youtube_channel_id,
            "content_type": self.content_type.value,
            "youtube_video_id": self.youtube_video_id,
            "watch_url": self.watch_url,
            "published_at": self.published_at.isoformat(),
            "approval_request_id": self.approval_request_id,
            "job_key": self.job_key,
            "attempts": self.attempts,
            "privacy_status": self.privacy_status,
            "contains_synthetic_media": self.contains_synthetic_media,
        }


class PublishResultService:
    def __init__(self, database: Database, audit_sink: AuditSink | None = None) -> None:
        self._database = database
        self._sink = audit_sink

    def for_content_item(self, content_item_id: str) -> PublishResultRecord:
        connection = self._database.connect()
        try:
            item = ContentItemRepository(connection).get(content_item_id)
            if item is None:
                raise PublishResultNotFoundError(
                    f"content item {content_item_id} was not found"
                )
            channel = ChannelRepository(connection).get(item.channel_id)
            jobs = [
                job
                for job in PublishJobRepository(connection).list_by_content_item(
                    item.id
                )
                if job.status is PublishStatus.SUCCEEDED
            ]
        finally:
            connection.close()
        if not jobs:
            raise PublishResultNotFoundError(
                f"content item {content_item_id} has no succeeded publish job"
            )
        if (
            len(jobs) > 1
            or item.status is not ContentStatus.PUBLISHED
            or channel is None
        ):
            raise PublishResultInconsistentError(
                f"content item {item.id} is {item.status.value} with "
                f"{len(jobs)} succeeded publish job(s)"
            )
        return self._record(jobs[0], channel.youtube.channel_id, item.channel_id)

    def list_for_channel(self, channel_id: str) -> list[PublishResultRecord]:
        connection = self._database.connect()
        try:
            channel = ChannelRepository(connection).get(channel_id)
            if channel is None:
                raise PublishResultNotFoundError(f"channel {channel_id} was not found")
            published = [
                item
                for item in ContentItemRepository(connection).list_by_channel(
                    channel_id
                )
                if item.status is ContentStatus.PUBLISHED
            ]
            job_repository = PublishJobRepository(connection)
            succeeded = [
                job
                for item in published
                for job in job_repository.list_by_content_item(item.id)
                if job.status is PublishStatus.SUCCEEDED
            ]
        finally:
            connection.close()
        records = [
            self._record(job, channel.youtube.channel_id, channel.id)
            for job in succeeded
        ]
        return sorted(
            records,
            key=lambda record: (
                record.published_at,
                record.youtube_video_id,
                record.job_id,
            ),
        )

    def _record(
        self, job: PublishJob, youtube_channel_id: str, channel_id: str
    ) -> PublishResultRecord:
        assert job.result is not None
        video_id = job.result.youtube_video_id
        privacy: str | None = None
        synthetic: bool | None = None
        if self._sink is not None:
            for event in self._sink.events():
                if event.action in PUBLISHED_ACTIONS and event.entity.id == job.id:
                    value = event.metadata.get("privacy_status")
                    privacy = value if isinstance(value, str) else None
                    flag = event.metadata.get("contains_synthetic_media")
                    synthetic = flag if isinstance(flag, bool) else None
        url = (
            SHORTS_URL if job.content_type is ContentType.SHORTS else WATCH_URL
        ).format(video_id=video_id)
        return PublishResultRecord(
            job_id=job.id,
            content_item_id=job.content_item_id,
            channel_id=channel_id,
            youtube_channel_id=youtube_channel_id,
            content_type=job.content_type,
            youtube_video_id=video_id,
            watch_url=url,
            published_at=job.result.published_at,
            approval_request_id=job.approval_request_id,
            job_key=job.idempotency_key,
            attempts=job.attempts,
            privacy_status=privacy,
            contains_synthetic_media=synthetic,
        )
