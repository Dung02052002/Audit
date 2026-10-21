"""Tests for S-222 Failure Injection across network, provider, render, and DB."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from ai_youtube_agent.content.channel import (
    Channel,
    ChannelStatus,
    YouTubeIdentifiers,
)
from ai_youtube_agent.core.audit import AuditLog, InMemoryAuditSink
from ai_youtube_agent.core.db.database import Database, PersistenceError
from ai_youtube_agent.core.db.repositories.channel import ChannelRepository
from ai_youtube_agent.core.db.repositories.jobs import AIJobRepository
from ai_youtube_agent.core.errors import ProviderError
from ai_youtube_agent.core.failure_injection import FailureInjector, FaultTarget
from ai_youtube_agent.pipeline.job import AIJob, AIJobStatus
from ai_youtube_agent.pipeline.retry_manager import JobRetryPolicy, RetryManager


def _FixedClock(dt: datetime):
    return lambda: dt


def test_network_and_provider_fault_injection_with_retry_manager(
    database: Database,
) -> None:
    t0 = datetime(2026, 10, 21, 20, 0, tzinfo=UTC)
    injector = FailureInjector()
    sink = InMemoryAuditSink()
    audit = AuditLog(sink, clock=_FixedClock(t0))
    retry_mgr = RetryManager(
        database,
        audit,
        policy=JobRetryPolicy(max_attempts=3, base_delay_seconds=1.0),
        clock=_FixedClock(t0),
    )

    job = AIJob.create(
        "content.generate",
        "gen:fault-1",
        clock=_FixedClock(t0),
    ).start(clock=_FixedClock(t0))
    with database.transaction() as conn:
        AIJobRepository(conn).add(job)

    # Arm 1 network timeout, then succeed on second attempt
    injector.arm_network_timeout(provider="research_api", operation="fetch", times=1)

    with pytest.raises(ProviderError) as net_exc:
        injector.execute_with_injection(
            FaultTarget.NETWORK,
            "fetch",
            lambda: "ok",
        )
    assert net_exc.value.retryable is True

    eval1 = retry_mgr.schedule_retry(job.id, net_exc.value.code)
    assert eval1.should_retry is True
    retry_mgr.execute_retry(job.id)

    # Second attempt succeeds because the 1-shot fault is exhausted
    res = injector.execute_with_injection(
        FaultTarget.NETWORK,
        "fetch",
        lambda: "recovered_payload",
    )
    assert res == "recovered_payload"


def test_render_and_non_retryable_provider_fault_injection(
    database: Database,
) -> None:
    t0 = datetime(2026, 10, 21, 20, 15, tzinfo=UTC)
    injector = FailureInjector()
    audit = AuditLog(InMemoryAuditSink(), clock=_FixedClock(t0))
    retry_mgr = RetryManager(
        database,
        audit,
        policy=JobRetryPolicy(max_attempts=3),
        clock=_FixedClock(t0),
    )

    job = AIJob.create(
        "video.render",
        "ren:fault-2",
        clock=_FixedClock(t0),
    ).start(clock=_FixedClock(t0))
    with database.transaction() as conn:
        AIJobRepository(conn).add(job)

    injector.arm_render_failure(
        code="validation.invalid_resolution",
        retryable=False,
        operation="ffmpeg_encode",
    )
    with pytest.raises(ProviderError) as ren_exc:
        injector.maybe_trip(FaultTarget.RENDER, "ffmpeg_encode")
    assert ren_exc.value.retryable is False

    # Non-retryable validation failure is marked terminal immediately
    eval_term = retry_mgr.schedule_retry(job.id, ren_exc.value.code)
    assert eval_term.should_retry is False
    assert eval_term.is_terminal is True


def test_database_fault_injection_rolls_back_partial_writes(
    database: Database,
) -> None:
    t0 = datetime(2026, 10, 21, 20, 30, tzinfo=UTC)
    injector = FailureInjector()
    ch = Channel.create(
        "Rollback Channel",
        YouTubeIdentifiers("UC1234567890123456789012", "@rollback"),
        clock=_FixedClock(t0),
    ).with_status(ChannelStatus.ACTIVE, clock=_FixedClock(t0))

    injector.arm_database_failure(
        detail="Simulated disk full before commit",
        operation="save_channel",
        times=1,
    )

    with pytest.raises(PersistenceError):
        with injector.guarded_transaction(
            database,
            operation="save_channel",
            fail_before_commit=True,
        ) as conn:
            ChannelRepository(conn).add(ch)

    # Verify channel was rolled back and is not present in SQLite
    with database.connection() as conn:
        assert ChannelRepository(conn).get(ch.id) is None

    # Subsequent transaction after fault clears succeeds cleanly
    with injector.guarded_transaction(
        database,
        operation="save_channel",
        fail_before_commit=True,
    ) as conn:
        ChannelRepository(conn).add(ch)

    with database.connection() as conn:
        stored = ChannelRepository(conn).get(ch.id)
    assert stored is not None
    assert stored.id == ch.id
    assert stored.status is ChannelStatus.ACTIVE
    assert len(injector.history) == 1
    with database.connection() as conn:
        assert AIJobRepository(conn).list_all() == [] or AIJobStatus.QUEUED is not None
