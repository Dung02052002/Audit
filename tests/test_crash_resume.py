"""Tests for CrashResumeService (Prompt Pack v8, task R-211)."""

from datetime import UTC, datetime, timedelta
import uuid

import pytest

from ai_youtube_agent.content.channel import Channel, ChannelStatus, YouTubeIdentifiers
from ai_youtube_agent.content.strategy import StrategyProfile
from ai_youtube_agent.core.audit import (
    Actor,
    ActorKind,
    AuditLog,
    InMemoryAuditSink,
)
from ai_youtube_agent.core.content_item import ContentItem, ContentType
from ai_youtube_agent.core.db.codec import format_datetime
from ai_youtube_agent.core.db.database import Database
from ai_youtube_agent.core.db.repositories.channel import (
    ChannelRepository,
    StrategyProfileRepository,
)
from ai_youtube_agent.core.db.repositories.content import ContentItemRepository
from ai_youtube_agent.core.db.repositories.jobs import AIJobRepository
from ai_youtube_agent.pipeline.crash_resume import (
    CrashResumeError,
    CrashResumeService,
    extract_safe_checkpoint,
)
from ai_youtube_agent.pipeline.job import AIJob, AIJobStatus
from ai_youtube_agent.pipeline.job_queue import LEASE_STEP_NAME

T0 = datetime(2026, 10, 21, 10, 0, tzinfo=UTC)
USER = Actor(ActorKind.USER, "owner")


def _at(dt: datetime):
    return lambda: dt


@pytest.fixture
def sample_item(database: Database) -> ContentItem:
    channel_id = uuid.uuid4().hex
    channel = Channel(
        id=channel_id,
        title="Crash Resume Channel",
        youtube=YouTubeIdentifiers(channel_id=f"UC{channel_id[:22].ljust(22, 'x')}"),
        status=ChannelStatus.ACTIVE,
        created_at=T0,
        updated_at=T0,
    )
    strategy = StrategyProfile.create(channel_id=channel_id, actor=USER, clock=_at(T0))
    item = ContentItem.create(
        channel_id=channel.id,
        strategy_profile_id=strategy.id,
        strategy_version=strategy.version,
        content_type=ContentType.SHORTS,
        title="Crash Test Short",
        clock=_at(T0),
    )
    with database.transaction() as conn:
        ChannelRepository(conn).add(channel)
        StrategyProfileRepository(conn).add(strategy)
        ContentItemRepository(conn).add(item)
    return item


def test_extract_safe_checkpoint_ignores_administrative_steps(
    sample_item: ContentItem,
) -> None:
    job = (
        AIJob.create(
            "audio.generate",
            "key-audio-1",
            content_item_id=sample_item.id,
            clock=_at(T0),
        )
        .start(clock=_at(T0 + timedelta(seconds=1)))
        .checkpoint(
            LEASE_STEP_NAME,
            {"worker_id": "w1", "token": "t1", "expires_at": format_datetime(T0 + timedelta(minutes=5))},
            clock=_at(T0 + timedelta(seconds=2)),
        )
        .checkpoint(
            "audio.chunk_done",
            {"index": 0, "duration_ms": 1200},
            clock=_at(T0 + timedelta(seconds=3)),
        )
        .checkpoint(
            LEASE_STEP_NAME,
            {"worker_id": "w1", "token": "t1", "expires_at": format_datetime(T0 + timedelta(minutes=10))},
            clock=_at(T0 + timedelta(seconds=4)),
        )
    )

    state = extract_safe_checkpoint(job)
    assert state.has_safe_checkpoint is True
    assert state.last_safe_step == "audio.chunk_done"
    assert state.last_safe_sequence == 2
    assert dict(state.checkpoint_data) == {"index": 0, "duration_ms": 1200}
    assert state.completed_domain_steps == ("audio.chunk_done",)


def test_resume_job_from_last_safe_checkpoint(
    database: Database, sample_item: ContentItem
) -> None:
    job = (
        AIJob.create(
            "script.generate",
            "key-script-1",
            content_item_id=sample_item.id,
            clock=_at(T0),
        )
        .start(clock=_at(T0 + timedelta(seconds=1)))
        .checkpoint(
            "script.outline_saved",
            {"sections": 4},
            clock=_at(T0 + timedelta(seconds=5)),
        )
    )
    with database.transaction() as conn:
        AIJobRepository(conn).add(job)

    sink = InMemoryAuditSink()
    service = CrashResumeService(
        database, AuditLog(sink), max_attempts=3, clock=_at(T0 + timedelta(minutes=1))
    )

    plan = service.resume_job(job.id)
    assert plan.resumed is True
    assert plan.job.status is AIJobStatus.RUNNING
    assert plan.job.attempts == 2
    assert plan.checkpoint_state.last_safe_step == "script.outline_saved"
    assert plan.job.last_checkpoint is not None
    assert plan.job.last_checkpoint.step == "crash.recovered"
    assert plan.job.last_checkpoint.data["resume_from_step"] == "script.outline_saved"

    events = sink.events()
    assert any(e.action == "orchestrator.crash_resumed" for e in events)


def test_recover_interrupted_jobs_skips_live_leases_unless_forced(
    database: Database, sample_item: ContentItem
) -> None:
    live_job = (
        AIJob.create(
            "script.generate",
            "key-live",
            content_item_id=sample_item.id,
            clock=_at(T0),
        )
        .start(clock=_at(T0))
        .checkpoint(
            LEASE_STEP_NAME,
            {
                "worker_id": "w1",
                "token": "t1",
                "expires_at": format_datetime(T0 + timedelta(minutes=10)),
            },
            clock=_at(T0 + timedelta(seconds=1)),
        )
    )
    expired_job = (
        AIJob.create(
            "audio.generate",
            "key-expired",
            content_item_id=sample_item.id,
            clock=_at(T0),
        )
        .start(clock=_at(T0))
        .checkpoint(
            "audio.planned",
            {"chunks": 3},
            clock=_at(T0 + timedelta(seconds=1)),
        )
        .checkpoint(
            LEASE_STEP_NAME,
            {
                "worker_id": "w2",
                "token": "t2",
                "expires_at": format_datetime(T0 + timedelta(minutes=2)),
            },
            clock=_at(T0 + timedelta(seconds=2)),
        )
    )
    with database.transaction() as conn:
        repo = AIJobRepository(conn)
        repo.add(live_job)
        repo.add(expired_job)

    service = CrashResumeService(
        database,
        AuditLog(InMemoryAuditSink()),
        clock=_at(T0 + timedelta(minutes=5)),
    )

    report = service.recover_interrupted_jobs(force_reclaim=False)
    assert report.scanned_running_jobs == 2
    assert report.skipped_active_leases == 1
    assert len(report.recovered_jobs) == 1
    assert report.recovered_jobs[0].job.id == expired_job.id
    assert report.recovered_jobs[0].checkpoint_state.last_safe_step == "audio.planned"


def test_resume_job_refuses_waiting_or_terminal_jobs(
    database: Database, sample_item: ContentItem
) -> None:
    waiting_job = (
        AIJob.create(
            "content.generate",
            "key-wait",
            content_item_id=sample_item.id,
            clock=_at(T0),
        )
        .start(clock=_at(T0))
        .wait_for_approval("req-1", clock=_at(T0 + timedelta(seconds=1)))
    )
    with database.transaction() as conn:
        AIJobRepository(conn).add(waiting_job)

    service = CrashResumeService(
        database, AuditLog(InMemoryAuditSink()), clock=_at(T0 + timedelta(minutes=1))
    )
    with pytest.raises(CrashResumeError, match="WAITING"):
        service.resume_job(waiting_job.id)
