"""Publish Duplicate Guard (Prompt Pack v8, prompt #152), context C12 Publishing.

``PublishDuplicateGuard`` decides, before anything is uploaded, whether a publish
request is new, a retry, a replay or a duplicate. It reads one consistent
snapshot (a deferred ``BEGIN`` that is rolled back) and writes nothing.

Rules, in this order:

1. The job stored under the publish job key of this approval (``publish_key``):
   - ``SUCCEEDED``: the video is already on YouTube, so the answer is ``REPLAY``
     (the stored result is returned and nothing is uploaded again);
   - ``QUEUED`` or ``IN_PROGRESS``: another worker owns the publish, so the
     request is refused with ``IN_FLIGHT`` (409);
   - ``FAILED``: ``RESUME``, the same job restarts (retry limits are #153).
2. Any other ``SUCCEEDED`` job of the same content item (another approval):
   ``ALREADY_PUBLISHED`` (409). A new approval can never publish an item twice.
3. A ``SUCCEEDED`` job of another item of the same channel whose approved VIDEO
   artifact has the same sha256: ``SAME_VIDEO_PUBLISHED`` (409). The same bytes are
   never published twice on one channel.
4. Otherwise ``PROCEED``.

The guard is the first line of defence only. Two workers can still read ``PROCEED``
at the same time, so the publish job insert (unique idempotency key) and the item
status update (optimistic check) stay the last line: ``concurrent_publish`` turns
their failures into the same typed error (``CONCURRENT``, 409).
"""

import sqlite3
from dataclasses import dataclass
from enum import StrEnum
from http import HTTPStatus
from typing import Final

from ai_youtube_agent.core.db.database import ConcurrencyError, Database
from ai_youtube_agent.core.db.repositories.publish import PublishJobRepository
from ai_youtube_agent.core.errors import DomainError
from ai_youtube_agent.pipeline.idempotency import PublishKeys
from ai_youtube_agent.pipeline.publish import PublishJob, PublishStatus


class DuplicateDecision(StrEnum):
    PROCEED = "proceed"
    RESUME = "resume"
    REPLAY = "replay"


class DuplicateErrorCode(StrEnum):
    IN_FLIGHT = "domain.publish_duplicate_in_flight"
    ALREADY_PUBLISHED = "domain.publish_duplicate_already_published"
    SAME_VIDEO_PUBLISHED = "domain.publish_duplicate_same_video"
    CONCURRENT = "domain.publish_duplicate_concurrent"


USER_MESSAGES: Final[dict[DuplicateErrorCode, str]] = {
    DuplicateErrorCode.IN_FLIGHT: "This video is already being published.",
    DuplicateErrorCode.ALREADY_PUBLISHED: (
        "This content item has already been published."
    ),
    DuplicateErrorCode.SAME_VIDEO_PUBLISHED: (
        "The same video file was already published on this channel."
    ),
    DuplicateErrorCode.CONCURRENT: (
        "Another publish of this video started at the same time."
    ),
}


class DuplicatePublishError(DomainError):
    default_code = DuplicateErrorCode.IN_FLIGHT.value
    default_user_message = USER_MESSAGES[DuplicateErrorCode.IN_FLIGHT]
    default_http_status = HTTPStatus.CONFLICT

    def __init__(self, code: DuplicateErrorCode, detail: str) -> None:
        if not isinstance(code, DuplicateErrorCode):
            raise TypeError("code must be a DuplicateErrorCode")
        self.duplicate_code = code
        super().__init__(detail, code=code.value, user_message=USER_MESSAGES[code])


@dataclass(frozen=True)
class DuplicateCheck:
    decision: DuplicateDecision
    job: PublishJob | None = None


class PublishDuplicateGuard:
    def __init__(self, database: Database) -> None:
        self._database = database

    def check(
        self,
        *,
        content_item_id: str,
        channel_id: str,
        video_sha256: str,
        keys: PublishKeys,
    ) -> DuplicateCheck:
        connection = self._database.connect()
        try:
            connection.execute("BEGIN")
            try:
                jobs = PublishJobRepository(connection)
                own = jobs.get_by_idempotency_key(keys.job_key)
                others = [
                    job
                    for job in jobs.list_by_content_item(content_item_id)
                    if job.idempotency_key != keys.job_key
                ]
                same_video = jobs.find_succeeded_by_video_sha(
                    channel_id,
                    video_sha256,
                    exclude_content_item_id=content_item_id,
                )
            finally:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
        finally:
            connection.close()
        return _decide(own, others, same_video, content_item_id, channel_id)


def concurrent_publish(
    error: BaseException, content_item_id: str
) -> DuplicatePublishError:
    """The typed error for a lost race on the job insert or the item update."""
    if not isinstance(error, sqlite3.IntegrityError | ConcurrencyError):
        raise TypeError("only IntegrityError and ConcurrencyError mean a lost race")
    return DuplicatePublishError(
        DuplicateErrorCode.CONCURRENT,
        f"a concurrent publish of content item {content_item_id} won the race",
    )


def _decide(
    own: PublishJob | None,
    others: list[PublishJob],
    same_video: list[PublishJob],
    content_item_id: str,
    channel_id: str,
) -> DuplicateCheck:
    if own is not None:
        if own.status is PublishStatus.SUCCEEDED:
            return DuplicateCheck(DuplicateDecision.REPLAY, own)
        if own.status is PublishStatus.FAILED:
            return DuplicateCheck(DuplicateDecision.RESUME, own)
        raise DuplicatePublishError(
            DuplicateErrorCode.IN_FLIGHT,
            f"publish job {own.id} of content item {content_item_id} is "
            f"{own.status.value}",
        )
    for job in others:
        if job.status is PublishStatus.SUCCEEDED:
            raise DuplicatePublishError(
                DuplicateErrorCode.ALREADY_PUBLISHED,
                f"content item {content_item_id} was already published by job {job.id}",
            )
    if same_video:
        raise DuplicatePublishError(
            DuplicateErrorCode.SAME_VIDEO_PUBLISHED,
            f"channel {channel_id} already published the same video bytes "
            f"through job {same_video[0].id}",
        )
    return DuplicateCheck(DuplicateDecision.PROCEED)
