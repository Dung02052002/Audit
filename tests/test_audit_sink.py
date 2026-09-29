import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from ai_youtube_agent.bootstrap import build_container
from ai_youtube_agent.core.audit import (
    Actor,
    ActorKind,
    AuditError,
    AuditEvent,
    AuditLog,
    AuditResult,
    AuditSink,
    EntityRef,
    InMemoryAuditSink,
)
from ai_youtube_agent.core.config import Environment, Settings
from ai_youtube_agent.core.db.database import Database
from ai_youtube_agent.core.db.migrate import migrate
from ai_youtube_agent.core.db.repositories.audit import SqliteAuditSink
from ai_youtube_agent.core.db.repositories.channel import ChannelRepository
from factories import make_channel

T0 = datetime(2026, 9, 30, 8, 0, 0, 5, tzinfo=UTC)
OWNER = Actor(ActorKind.USER, "owner-1")
JOB = EntityRef("ai_job", "job-1")


def event(event_id: str = "e1", moment: datetime = T0, **overrides) -> AuditEvent:
    values = {
        "event_id": event_id,
        "timestamp": moment,
        "action": "job.started",
        "actor": OWNER,
        "entity": JOB,
        "result": AuditResult.SUCCESS,
        "correlation_id": "corr-1",
        "session_id": None,
        "job_id": "job-1",
        "metadata": {"attempt": 1, "model": "mock", "cost": 0.25, "ok": True},
    }
    return AuditEvent(**{**values, **overrides})


def test_sqlite_sink_is_an_audit_sink(database: Database) -> None:
    assert isinstance(SqliteAuditSink(database), AuditSink)


def test_events_round_trip_in_time_order(database: Database) -> None:
    sink = SqliteAuditSink(database)
    later = event("e2", T0 + timedelta(seconds=1), result=AuditResult.DENIED)
    first = event("e1")

    sink.append(later)
    sink.append(first)

    assert sink.events() == (first, later)


def test_events_survive_a_restart(database: Database) -> None:
    SqliteAuditSink(database).append(event())
    assert SqliteAuditSink(Database(database.path)).events() == (event(),)


def test_duplicate_event_id_is_refused(database: Database) -> None:
    sink = SqliteAuditSink(database)
    sink.append(event())
    with pytest.raises(AuditError, match="already recorded"):
        sink.append(event(action="job.finished"))
    assert sink.events() == (event(),)


def test_audit_survives_a_rolled_back_business_transaction(
    database: Database,
) -> None:
    sink = SqliteAuditSink(database)
    channel = make_channel()

    # The pattern for callers: audit after the transaction has ended.
    with pytest.raises(RuntimeError):
        try:
            with database.transaction() as connection:
                ChannelRepository(connection).add(channel)
                raise RuntimeError("business failure")
        except RuntimeError:
            sink.append(event(result=AuditResult.FAILURE))
            raise

    with database.transaction() as connection:
        assert ChannelRepository(connection).get(channel.id) is None
    assert [e.result for e in sink.events()] == [AuditResult.FAILURE]


def test_stored_events_cannot_be_changed(database: Database) -> None:
    SqliteAuditSink(database).append(event())
    connection = database.connect()
    try:
        with pytest.raises(sqlite3.DatabaseError, match="append-only"):
            connection.execute("DELETE FROM audit_events")
    finally:
        connection.close()


def test_audit_log_records_through_the_sqlite_sink(database: Database) -> None:
    audit = AuditLog(SqliteAuditSink(database), clock=lambda: T0)

    recorded = audit.record("job.started", OWNER, JOB, AuditResult.SUCCESS)

    assert SqliteAuditSink(database).events() == (recorded,)


# Bootstrap choice


def test_test_environment_keeps_events_in_memory(tmp_path: Path) -> None:
    settings = Settings(environment=Environment.TEST, database_path=tmp_path / "a.db")
    container = build_container(settings)

    assert isinstance(container.resolve(AuditSink), InMemoryAuditSink)
    assert not (tmp_path / "a.db").exists()


@pytest.mark.parametrize(
    "environment", [Environment.DEVELOPMENT, Environment.PRODUCTION]
)
def test_application_environments_store_events_in_sqlite(
    tmp_path: Path, environment: Environment
) -> None:
    path = tmp_path / "app.db"
    migrate(path)
    container = build_container(Settings(environment=environment, database_path=path))

    sink = container.resolve(AuditSink)
    recorded = container.resolve(AuditLog).record(
        "job.started", OWNER, JOB, AuditResult.SUCCESS
    )

    assert isinstance(sink, SqliteAuditSink)
    assert container.resolve(Database).path == path
    assert container.resolve(Database) is container.resolve(Database)
    assert SqliteAuditSink(Database(path)).events() == (recorded,)
