"""Repositories for sessions and AI jobs (C16)."""

import sqlite3
from datetime import datetime

from ai_youtube_agent.core.db.database import ConcurrencyError
from ai_youtube_agent.core.db.repositories.base import (
    Repository,
    actor_columns,
    actor_from,
    dt,
    from_json,
    parse_dt,
    to_json,
)
from ai_youtube_agent.pipeline.job import (
    AIJob,
    AIJobStatus,
    JobCheckpoint,
    Session,
    SessionStatus,
)


class SessionRepository(Repository):
    table = "sessions"

    def add(self, session: Session) -> None:
        self._insert(
            self.table,
            {
                "id": session.id,
                **actor_columns("started_by", session.started_by),
                "status": session.status.value,
                "started_at": dt(session.started_at),
                "stopped_at": dt(session.stopped_at),
            },
        )

    def get(self, session_id: str) -> Session | None:
        row = self._one("SELECT * FROM sessions WHERE id = ?", (session_id,))
        if row is None:
            return None
        return Session(
            id=row["id"],
            started_by=actor_from(row, "started_by"),
            status=SessionStatus(row["status"]),
            started_at=parse_dt(row["started_at"]),
            stopped_at=parse_dt(row["stopped_at"]),
        )

    def update(self, session: Session, *, expected_status: SessionStatus) -> None:
        self._update(
            session.id,
            {"status": session.status.value, "stopped_at": dt(session.stopped_at)},
            guard_column="status",
            guard_value=expected_status.value,
        )


class AIJobRepository(Repository):
    """Checkpoints are append-only: ``update`` only adds new ones."""

    table = "ai_jobs"

    def add(self, job: AIJob) -> None:
        self._insert(self.table, _job_row(job))
        self._add_checkpoints(job, job.checkpoints)

    def get(self, job_id: str) -> AIJob | None:
        row = self._one("SELECT * FROM ai_jobs WHERE id = ?", (job_id,))
        return self._job(row) if row else None

    def get_by_idempotency_key(self, key: str) -> AIJob | None:
        row = self._one("SELECT * FROM ai_jobs WHERE idempotency_key = ?", (key,))
        return self._job(row) if row else None

    def list_by_content_item(self, content_item_id: str) -> list[AIJob]:
        rows = self._all(
            "SELECT * FROM ai_jobs WHERE content_item_id = ? ORDER BY created_at, id",
            (content_item_id,),
        )
        return [self._job(row) for row in rows]

    def update(self, job: AIJob, *, expected_updated_at: datetime) -> None:
        values = _job_row(job)
        mutable = ("status", "attempts", "last_error", "updated_at")
        self._update(
            job.id,
            {column: values[column] for column in mutable},
            guard_column="updated_at",
            guard_value=dt(expected_updated_at),
        )
        (stored,) = self.connection.execute(
            "SELECT count(*) FROM job_checkpoints WHERE job_id = ?", (job.id,)
        ).fetchone()
        if len(job.checkpoints) < stored:
            raise ConcurrencyError(
                f"ai_jobs {job.id} has {stored} checkpoints stored, but the update "
                f"has only {len(job.checkpoints)}; checkpoints are append-only"
            )
        self._add_checkpoints(job, job.checkpoints[stored:])

    def _add_checkpoints(self, job: AIJob, checkpoints: tuple[JobCheckpoint, ...]):
        for checkpoint in checkpoints:
            self._insert(
                "job_checkpoints",
                {
                    "job_id": job.id,
                    "sequence": checkpoint.sequence,
                    "step": checkpoint.step,
                    "data_json": to_json(dict(checkpoint.data)),
                    "created_at": dt(checkpoint.created_at),
                },
            )

    def _job(self, row: sqlite3.Row) -> AIJob:
        checkpoints = self._all(
            "SELECT * FROM job_checkpoints WHERE job_id = ? ORDER BY sequence",
            (row["id"],),
        )
        return AIJob(
            id=row["id"],
            kind=row["kind"],
            idempotency_key=row["idempotency_key"],
            content_item_id=row["content_item_id"],
            session_id=row["session_id"],
            status=AIJobStatus(row["status"]),
            attempts=row["attempts"],
            last_error=row["last_error"],
            checkpoints=tuple(
                JobCheckpoint(
                    sequence=c["sequence"],
                    step=c["step"],
                    created_at=parse_dt(c["created_at"]),
                    data=from_json(c["data_json"]),
                )
                for c in checkpoints
            ),
            created_at=parse_dt(row["created_at"]),
            updated_at=parse_dt(row["updated_at"]),
        )


def _job_row(job: AIJob) -> dict:
    return {
        "id": job.id,
        "kind": job.kind,
        "idempotency_key": job.idempotency_key,
        "content_item_id": job.content_item_id,
        "session_id": job.session_id,
        "status": job.status.value,
        "attempts": job.attempts,
        "last_error": job.last_error,
        "created_at": dt(job.created_at),
        "updated_at": dt(job.updated_at),
    }
