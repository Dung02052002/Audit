"""Tests for R-213 Kill Switch Service."""

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
from ai_youtube_agent.core.content_item import ContentItem, ContentStatus, ContentType
from ai_youtube_agent.core.db.database import Database
from ai_youtube_agent.core.db.repositories.jobs import AIJobRepository
from ai_youtube_agent.core.gates import GateContext, GateOutcome
from ai_youtube_agent.pipeline.job import AIJob, AIJobStatus
from ai_youtube_agent.pipeline.kill_switch_service import (
    KillSwitchBlockedError,
    KillSwitchNotAllowedError,
    KillSwitchService,
)


def _FixedClock(dt: datetime):
    return lambda: dt


def test_kill_switch_activation_blocks_gate_and_cancels_queued_jobs(
    database: Database,
) -> None:
    t0 = datetime(2026, 10, 21, 16, 0, tzinfo=UTC)
    user = Actor(ActorKind.USER, "dung")
    sink = InMemoryAuditSink()
    audit = AuditLog(sink)

    queued_job = AIJob.create(
        "content.generate",
        "gen:ks-1",
        clock=_FixedClock(t0),
    )
    with database.transaction() as conn:
        AIJobRepository(conn).add(queued_job)

    service = KillSwitchService(
        audit,
        database=database,
        clock=_FixedClock(t0 + timedelta(seconds=10)),
    )
    assert not service.is_active
    service.ensure_inactive(actor=user)

    transition = service.activate(
        user,
        reason="runaway provider cost",
        cancel_queued_jobs=True,
    )
    assert service.is_active
    assert transition.current.active
    assert transition.current.reason == "runaway provider cost"
    assert transition.cancelled_job_ids == (queued_job.id,)

    with pytest.raises(KillSwitchBlockedError) as exc_info:
        service.ensure_inactive(actor=user, action_name="schedule_production")
    assert exc_info.value.code == "domain.killswitch_active"
    assert "runaway provider cost" in exc_info.value.user_message

    # Verify KillSwitchGate integration
    gate = service.gate()
    item = ContentItem.create(
        "ch-1",
        "sp-1",
        1,
        ContentType.SHORTS,
        "Test Short",
        clock=_FixedClock(t0),
    )
    res_gen = gate.evaluate(
        GateContext(
            item=item,
            target_status=ContentStatus.GENERATING,
            actor=user,
            at=t0 + timedelta(seconds=15),
        )
    )
    assert res_gen.outcome is GateOutcome.BLOCK
    assert res_gen.reasons[0].code == "killswitch.active"

    with database.connection() as conn:
        stored_job = AIJobRepository(conn).get(queued_job.id)
    assert stored_job is not None
    assert stored_job.status is AIJobStatus.CANCELLED
    assert stored_job.cancellation_reason == "kill_switch: runaway provider cost"


def test_only_human_user_can_deactivate_kill_switch() -> None:
    t0 = datetime(2026, 10, 21, 16, 30, tzinfo=UTC)
    user = Actor(ActorKind.USER, "dung")
    system = Actor(ActorKind.SYSTEM, "watchdog")
    ai = Actor(ActorKind.AI, "agent")
    sink = InMemoryAuditSink()
    audit = AuditLog(sink)

    service = KillSwitchService(audit, clock=_FixedClock(t0))

    # AI cannot activate
    with pytest.raises(KillSwitchNotAllowedError):
        service.activate(ai, reason="ai attempt")

    # System watchdog CAN activate emergency stop
    service.activate(system, reason="watchdog anomaly")
    assert service.is_active

    # System and AI CANNOT deactivate emergency stop
    with pytest.raises(KillSwitchNotAllowedError):
        service.deactivate(system, reason="auto clear")
    with pytest.raises(KillSwitchNotAllowedError):
        service.deactivate(ai, reason="ai clear")
    assert service.is_active

    # Human user CAN deactivate
    reset = service.deactivate(user, reason="issue resolved")
    assert not service.is_active
    assert not reset.current.active
    assert len(service.history()) == 2
    assert any(
        e.action == "killswitch.deactivated" and e.result is AuditResult.SUCCESS
        for e in sink.events()
    )
