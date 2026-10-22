"""Tests for R-212 Graceful Shutdown."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from ai_youtube_agent.core.audit import (
    Actor,
    ActorKind,
    AuditLog,
    AuditResult,
    InMemoryAuditSink,
)
from ai_youtube_agent.core.db.database import Database
from ai_youtube_agent.core.db.repositories.jobs import (
    AIJobRepository,
    SessionRepository,
)
from ai_youtube_agent.pipeline.graceful_shutdown import (
    GracefulShutdownError,
    GracefulShutdownService,
)
from ai_youtube_agent.pipeline.job import AIJob, AIJobStatus, Session, SessionStatus
from ai_youtube_agent.pipeline.job_queue import SqliteJobQueue


def _FixedClock(dt: datetime):
    return lambda: dt


def test_graceful_shutdown_checkpoints_running_jobs_and_stops_sessions(
    database: Database,
) -> None:
    t0 = datetime(2026, 10, 21, 14, 0, tzinfo=UTC)
    user = Actor(ActorKind.USER, "dung")
    sink = InMemoryAuditSink()
    audit = AuditLog(sink)
    queue = SqliteJobQueue(database, clock=_FixedClock(t0))

    session = Session.start(actor=user, clock=_FixedClock(t0))
    running_job = AIJob.create(
        "content.generate",
        "gen:shutdown-1",
        session_id=session.id,
        clock=_FixedClock(t0),
    ).start(clock=_FixedClock(t0 + timedelta(seconds=5)))
    waiting_job = (
        AIJob.create(
            "content.publish",
            "pub:shutdown-2",
            session_id=session.id,
            clock=_FixedClock(t0),
        )
        .start(clock=_FixedClock(t0 + timedelta(seconds=5)))
        .wait_for_approval("req-1", clock=_FixedClock(t0 + timedelta(seconds=10)))
    )
    queued_job = AIJob.create(
        "content.generate",
        "gen:shutdown-3",
        session_id=session.id,
        clock=_FixedClock(t0),
    )

    with database.transaction() as conn:
        SessionRepository(conn).add(session)
        repo = AIJobRepository(conn)
        repo.add(running_job)
        repo.add(waiting_job)
        repo.add(queued_job)

    shutdown_time = t0 + timedelta(minutes=2)
    service = GracefulShutdownService(
        database,
        queue,
        audit,
        clock=_FixedClock(shutdown_time),
    )
    hook_calls: list[str] = []
    service.register_hook("flush_buffers", lambda: hook_calls.append("flushed"))

    assert not service.is_shutting_down
    service.ensure_accepting_work()

    report = service.shutdown(user, reason="operator_exit")

    assert service.is_shutting_down
    assert service.last_report == report
    assert hook_calls == ["flushed"]
    assert report.checkpointed_job_ids == (running_job.id,)
    assert report.waiting_job_ids == (waiting_job.id,)
    assert report.queued_job_ids == (queued_job.id,)
    assert report.stopped_session_ids == (session.id,)
    assert report.hooks_ran == 1
    assert report.hook_errors == ()

    with pytest.raises(GracefulShutdownError) as exc_info:
        service.ensure_accepting_work()
    assert exc_info.value.code == "domain.shutdown_in_progress"

    with database.connection() as conn:
        stored_job = AIJobRepository(conn).get(running_job.id)
        stored_session = SessionRepository(conn).get(session.id)

    assert stored_job is not None
    assert stored_job.status is AIJobStatus.RUNNING
    assert stored_job.last_checkpoint is not None
    assert stored_job.last_checkpoint.step == "runtime.shutdown_checkpoint"
    assert stored_job.last_checkpoint.data["reason"] == "operator_exit"

    assert stored_session is not None
    assert stored_session.status is SessionStatus.STOPPED

    events = sink.events()
    assert len(events) == 1
    assert events[0].action == "orchestrator.graceful_shutdown"
    assert events[0].result is AuditResult.SUCCESS
    assert events[0].metadata["checkpointed_jobs"] == 1
    assert events[0].metadata["stopped_sessions"] == 1


def test_graceful_shutdown_continues_when_hook_raises(database: Database) -> None:
    t0 = datetime(2026, 10, 21, 15, 0, tzinfo=UTC)
    system = Actor(ActorKind.SYSTEM, "sigterm")
    sink = InMemoryAuditSink()
    audit = AuditLog(sink)
    queue = SqliteJobQueue(database, clock=_FixedClock(t0))

    service = GracefulShutdownService(database, queue, audit, clock=_FixedClock(t0))

    def _failing_hook() -> None:
        raise RuntimeError("socket close error")

    service.register_hook("failing_hook", _failing_hook)
    report = service.shutdown(system, reason="sigterm")

    assert report.hooks_ran == 1
    assert report.hook_errors == ("failing_hook: RuntimeError",)
    events = sink.events()
    assert len(events) == 1
    assert events[0].result is AuditResult.FAILURE
