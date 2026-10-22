"""Tests for R-214 Orchestrator Audit."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from tests.factories import make_channel, make_content_item, make_strategy_profile

from ai_youtube_agent.core.audit import (
    Actor,
    ActorKind,
    AuditLog,
    InMemoryAuditSink,
)
from ai_youtube_agent.core.db.database import Database
from ai_youtube_agent.core.db.repositories.channel import (
    ChannelRepository,
    StrategyProfileRepository,
)
from ai_youtube_agent.core.db.repositories.content import ContentItemRepository
from ai_youtube_agent.core.db.repositories.jobs import AIJobRepository
from ai_youtube_agent.pipeline.job import AIJob
from ai_youtube_agent.pipeline.job_queue import SqliteJobQueue
from ai_youtube_agent.pipeline.kill_switch_service import KillSwitchService
from ai_youtube_agent.pipeline.orchestrator_audit import (
    JOB_BLOCKED,
    JOB_COMPLETED,
    JOB_FAILED,
    JOB_STARTED,
    OrchestratorAuditError,
    OrchestratorAuditRecorder,
    OrchestratorAuditService,
)
from ai_youtube_agent.pipeline.retry_manager import JobRetryPolicy, RetryManager
from ai_youtube_agent.pipeline.session_manager import SessionManager


def _FixedClock(dt: datetime):
    return lambda: dt


def test_orchestrator_audit_records_and_queries_job_session_and_runtime_trails(
    database: Database,
) -> None:
    t0 = datetime(2026, 10, 21, 17, 0, tzinfo=UTC)
    user = Actor(ActorKind.USER, "dung")
    system = Actor(ActorKind.SYSTEM, "worker-1")
    sink = InMemoryAuditSink()
    audit = AuditLog(sink, clock=_FixedClock(t0))

    session_mgr = SessionManager(database, audit, clock=_FixedClock(t0))
    session = session_mgr.start_session(user)

    channel = make_channel()
    profile = make_strategy_profile(channel)
    item = make_content_item(channel, profile)
    with database.transaction() as conn:
        ChannelRepository(conn).add(channel)
        StrategyProfileRepository(conn).add(profile)
        ContentItemRepository(conn).add(item)

    job = AIJob.create(
        "content.generate",
        "gen:audit-job-1",
        content_item_id=item.id,
        session_id=session.id,
        clock=_FixedClock(t0),
    ).start(clock=_FixedClock(t0 + timedelta(seconds=1)))

    with database.transaction() as conn:
        AIJobRepository(conn).add(job)

    recorder = OrchestratorAuditRecorder(audit)
    recorder.record_job_started(job, actor=system, worker_id="worker-1")
    recorder.record_job_blocked(
        job,
        actor=system,
        reason_code="approval.missing",
        gate_name="approval",
    )
    recorder.record_job_failed(
        job,
        actor=system,
        error_code="provider.timeout",
        retryable=True,
    )

    queue = SqliteJobQueue(database, clock=_FixedClock(t0 + timedelta(seconds=6)))
    retry_mgr = RetryManager(
        database,
        audit,
        policy=JobRetryPolicy(max_attempts=3, base_delay_seconds=2.0),
        queue=queue,
        clock=_FixedClock(t0 + timedelta(seconds=6)),
    )
    # schedule_retry transitions RUNNING -> FAILED, records checkpoint & audit
    retry_mgr.schedule_retry(job.id, "provider.timeout")

    restarted = retry_mgr.execute_retry(job.id)
    succeeded = restarted.succeed(clock=_FixedClock(t0 + timedelta(seconds=15)))
    with database.transaction() as conn:
        AIJobRepository(conn).update(
            succeeded, expected_updated_at=restarted.updated_at
        )
    recorder.record_job_completed(succeeded, actor=system)

    # Activate kill switch and stop session
    ks = KillSwitchService(
        audit, database=database, clock=_FixedClock(t0 + timedelta(seconds=20))
    )
    ks.activate(user, reason="end of test")
    session_mgr.stop_session(session.id, user)

    audit_svc = OrchestratorAuditService(database, sink)

    job_trail = audit_svc.trail_for_job(job.id)
    assert job_trail.scope == "ai_job"
    assert job_trail.subject_id == job.id
    assert [e.action for e in job_trail.entries] == [
        JOB_STARTED,
        JOB_BLOCKED,
        JOB_FAILED,
        "job.retry_scheduled",
        "job.retry_started",
        JOB_COMPLETED,
    ]
    assert job_trail.result_counts["success"] == 4
    assert job_trail.result_counts["denied"] == 1
    assert job_trail.result_counts["failure"] == 1

    session_trail = audit_svc.trail_for_session(session.id)
    assert session_trail.action_counts["session.started"] == 1
    assert session_trail.action_counts["session.stopped"] == 1
    assert session_trail.action_counts[JOB_COMPLETED] == 1

    item_trail = audit_svc.trail_for_content_item(item.id)
    assert item_trail.total_events == 6

    runtime_trail = audit_svc.trail_for_runtime()
    assert runtime_trail.action_counts["killswitch.activated"] == 1
    assert runtime_trail.total_events == 9


def test_orchestrator_audit_not_found_raises(database: Database) -> None:
    audit_svc = OrchestratorAuditService(database, InMemoryAuditSink())
    with pytest.raises(OrchestratorAuditError) as exc_job:
        audit_svc.trail_for_job("missing-job")
    assert exc_job.value.code == "domain.orchestrator_audit.job_not_found"

    with pytest.raises(OrchestratorAuditError) as exc_sess:
        audit_svc.trail_for_session("missing-session")
    assert exc_sess.value.code == "domain.orchestrator_audit.session_not_found"
