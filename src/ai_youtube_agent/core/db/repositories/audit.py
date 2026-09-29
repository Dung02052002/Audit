"""SQLite audit sink (Prompt Pack v8, prompt #030; the sink promised by A-011).

``SqliteAuditSink`` implements the append-only ``AuditSink`` protocol on the
``audit_events`` table, whose triggers refuse any update or delete. Each event
is written in its own short transaction on its own connection, so the audit
record of a failed or denied action survives even when the caller's business
transaction rolls back. A duplicate event id is refused with ``AuditError``,
as in ``InMemoryAuditSink``.

SQLite allows one writer at a time. Record an audit event after the business
transaction has committed or rolled back, not inside it: an append while a
``Database.transaction()`` on the same file is still open waits for its lock
and fails with "database is locked" after the busy timeout.
"""

import sqlite3

from ai_youtube_agent.core.audit import (
    Actor,
    ActorKind,
    AuditError,
    AuditEvent,
    AuditResult,
    EntityRef,
)
from ai_youtube_agent.core.db.database import Database
from ai_youtube_agent.core.db.repositories.base import dt, from_json, parse_dt, to_json


class SqliteAuditSink:
    def __init__(self, database: Database) -> None:
        self._database = database

    def append(self, event: AuditEvent) -> None:
        try:
            with self._database.transaction() as connection:
                connection.execute(
                    "INSERT INTO audit_events (event_id, timestamp, action, "
                    "actor_kind, actor_id, entity_type, entity_id, result, "
                    "correlation_id, session_id, job_id, metadata_json) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        event.event_id,
                        dt(event.timestamp),
                        event.action,
                        event.actor.kind.value,
                        event.actor.id,
                        event.entity.type,
                        event.entity.id,
                        event.result.value,
                        event.correlation_id,
                        event.session_id,
                        event.job_id,
                        to_json(dict(event.metadata)),
                    ),
                )
        except sqlite3.IntegrityError as error:
            if self._exists(event.event_id):
                raise AuditError(
                    f"audit event {event.event_id} is already recorded"
                ) from error
            raise

    def events(self) -> tuple[AuditEvent, ...]:
        connection = self._database.connect()
        try:
            cursor = connection.execute(
                "SELECT * FROM audit_events ORDER BY timestamp, rowid"
            )
            cursor.row_factory = sqlite3.Row
            return tuple(_event(row) for row in cursor.fetchall())
        finally:
            connection.close()

    def _exists(self, event_id: str) -> bool:
        connection = self._database.connect()
        try:
            row = connection.execute(
                "SELECT 1 FROM audit_events WHERE event_id = ?", (event_id,)
            ).fetchone()
            return row is not None
        finally:
            connection.close()


def _event(row: sqlite3.Row) -> AuditEvent:
    return AuditEvent(
        event_id=row["event_id"],
        timestamp=parse_dt(row["timestamp"]),
        action=row["action"],
        actor=Actor(ActorKind(row["actor_kind"]), row["actor_id"]),
        entity=EntityRef(row["entity_type"], row["entity_id"]),
        result=AuditResult(row["result"]),
        correlation_id=row["correlation_id"],
        session_id=row["session_id"],
        job_id=row["job_id"],
        metadata=from_json(row["metadata_json"]),
    )
