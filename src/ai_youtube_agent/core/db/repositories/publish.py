"""Repository for publish jobs (C12)."""

import sqlite3
from datetime import datetime

from ai_youtube_agent.core.content_item import ContentType
from ai_youtube_agent.core.db.repositories.base import Repository, dt, parse_dt
from ai_youtube_agent.pipeline.publish import PublishJob, PublishResult, PublishStatus


class PublishJobRepository(Repository):
    table = "publish_jobs"

    def add(self, job: PublishJob) -> None:
        self._insert(self.table, _job_row(job))

    def get(self, job_id: str) -> PublishJob | None:
        row = self._one("SELECT * FROM publish_jobs WHERE id = ?", (job_id,))
        return _job(row) if row else None

    def get_by_idempotency_key(self, key: str) -> PublishJob | None:
        row = self._one("SELECT * FROM publish_jobs WHERE idempotency_key = ?", (key,))
        return _job(row) if row else None

    def list_by_content_item(self, content_item_id: str) -> list[PublishJob]:
        rows = self._all(
            "SELECT * FROM publish_jobs WHERE content_item_id = ? "
            "ORDER BY created_at, id",
            (content_item_id,),
        )
        return [_job(row) for row in rows]

    def update(self, job: PublishJob, *, expected_updated_at: datetime) -> None:
        values = _job_row(job)
        mutable = (
            "status",
            "attempts",
            "last_error",
            "result_youtube_video_id",
            "result_published_at",
            "updated_at",
        )
        self._update(
            job.id,
            {column: values[column] for column in mutable},
            guard_column="updated_at",
            guard_value=dt(expected_updated_at),
        )


def _job_row(job: PublishJob) -> dict:
    return {
        "id": job.id,
        "idempotency_key": job.idempotency_key,
        "content_item_id": job.content_item_id,
        "approval_request_id": job.approval_request_id,
        "content_type": job.content_type.value,
        "status": job.status.value,
        "attempts": job.attempts,
        "last_error": job.last_error,
        "result_youtube_video_id": job.result.youtube_video_id if job.result else None,
        "result_published_at": dt(job.result.published_at) if job.result else None,
        "created_at": dt(job.created_at),
        "updated_at": dt(job.updated_at),
    }


def _job(row: sqlite3.Row) -> PublishJob:
    result = None
    if row["result_youtube_video_id"] is not None:
        result = PublishResult(
            row["id"],
            row["result_youtube_video_id"],
            parse_dt(row["result_published_at"]),
        )
    return PublishJob(
        id=row["id"],
        idempotency_key=row["idempotency_key"],
        content_item_id=row["content_item_id"],
        approval_request_id=row["approval_request_id"],
        content_type=ContentType(row["content_type"]),
        status=PublishStatus(row["status"]),
        attempts=row["attempts"],
        last_error=row["last_error"],
        result=result,
        created_at=parse_dt(row["created_at"]),
        updated_at=parse_dt(row["updated_at"]),
    )
