"""Tests for RetryManager and JobRetryPolicy (Prompt Pack v8, task R-208)."""

import uuid
from datetime import UTC, datetime, timedelta

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
from ai_youtube_agent.core.db.database import Database
from ai_youtube_agent.core.db.repositories.channel import (
    ChannelRepository,
    StrategyProfileRepository,
)
from ai_youtube_agent.core.db.repositories.content import ContentItemRepository
from ai_youtube_agent.core.db.repositories.jobs import AIJobRepository
from ai_youtube_agent.pipeline.job import AIJob, AIJobStatus
from ai_youtube_agent.pipeline.job_queue import InMemoryJobQueue
from ai_youtube_agent.pipeline.retry_manager import (
    JobRetryPolicy,
    RetryManager,
)

T0 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
USER = Actor(ActorKind.USER, "owner")


def _fixed_clock(dt: datetime):
    return lambda: dt


@pytest.fixture
def audit_log():
    return AuditLog(InMemoryAuditSink())


@pytest.fixture
def sample_item(database: Database) -> ContentItem:
    channel_id = uuid.uuid4().hex
    channel = Channel(
        id=channel_id,
        title="Retry Test Channel",
        youtube=YouTubeIdentifiers(channel_id=f"UC{channel_id[:22].ljust(22, 'x')}"),
        status=ChannelStatus.ACTIVE,
        created_at=T0,
        updated_at=T0,
    )
    strategy = StrategyProfile.create(
        channel_id=channel_id,
        actor=USER,
        clock=_fixed_clock(T0),
    )
    item = ContentItem.create(
        channel_id=channel.id,
        strategy_profile_id=strategy.id,
        strategy_version=strategy.version,
        content_type=ContentType.SHORTS,
        title="Test Item",
        clock=_fixed_clock(T0),
    )
    with database.transaction() as conn:
        ChannelRepository(conn).add(channel)
        StrategyProfileRepository(conn).add(strategy)
        ContentItemRepository(conn).add(item)
    return item


@pytest.fixture
def sample_job(database: Database, sample_item: ContentItem) -> AIJob:
    job = AIJob.create(
        "content.generate",
        f"gen:{sample_item.id}",
        content_item_id=sample_item.id,
        clock=_fixed_clock(T0),
    ).start(clock=_fixed_clock(T0))
    with database.transaction() as conn:
        AIJobRepository(conn).add(job)
    return job


def test_retry_policy_exponential_backoff_calculation() -> None:
    policy = JobRetryPolicy(
        max_attempts=4,
        base_delay_seconds=2.0,
        max_delay_seconds=30.0,
        backoff_multiplier=2.0,
        jitter=False,
    )
    assert policy.delay_after(1) == 2.0
    assert policy.delay_after(2) == 4.0
    assert policy.delay_after(3) == 8.0
    assert policy.delay_after(4) == 16.0
    assert policy.delay_after(5) == 30.0  # capped at max_delay_seconds


def test_retry_policy_validation() -> None:
    with pytest.raises(ValueError, match="max_attempts"):
        JobRetryPolicy(max_attempts=0)
    with pytest.raises(ValueError, match="max_attempts"):
        JobRetryPolicy(max_attempts=11)
    with pytest.raises(ValueError, match="base_delay_seconds"):
        JobRetryPolicy(base_delay_seconds=-1)
    with pytest.raises(ValueError, match="max_delay_seconds"):
        JobRetryPolicy(base_delay_seconds=10.0, max_delay_seconds=5.0)


def test_retry_policy_error_classification() -> None:
    policy = JobRetryPolicy()
    assert (
        policy.is_retryable_error("Connection timeout connecting to provider") is True
    )
    assert policy.is_retryable_error("HTTP 503 Service Unavailable") is True
    assert policy.is_retryable_error("Policy_Violation: prohibited term") is False
    assert policy.is_retryable_error("Copyright_Infringement detected") is False
    assert policy.is_retryable_error("Validation_Error in metadata") is False


def test_evaluate_retry_success(
    database: Database, audit_log: AuditLog, sample_job: AIJob
) -> None:
    manager = RetryManager(database, audit_log, clock=_fixed_clock(T0))
    eval_res = manager.evaluate_retry(
        sample_job.id, "Network glitch", jitter_factor=0.5
    )

    assert eval_res.should_retry is True
    assert eval_res.attempt == 1
    assert eval_res.max_attempts == 3
    assert eval_res.delay_seconds > 0
    assert eval_res.next_eligible_at is not None
    assert eval_res.is_terminal is False


def test_schedule_retry_running_job(
    database: Database, audit_log: AuditLog, sample_job: AIJob
) -> None:
    queue = InMemoryJobQueue(clock=_fixed_clock(T0))
    manager = RetryManager(
        database, audit_log, queue=queue, clock=_fixed_clock(T0 + timedelta(seconds=5))
    )
    res = manager.schedule_retry(sample_job.id, "Provider timeout", jitter_factor=0.5)

    assert res.should_retry is True

    with database.connection() as conn:
        persisted = AIJobRepository(conn).get(sample_job.id)
        assert persisted is not None
        assert persisted.status is AIJobStatus.FAILED
        assert persisted.checkpoints[-1].step == "retry.scheduled"
        assert persisted.checkpoints[-1].data["error"] == "Provider timeout"


def test_schedule_retry_exhausted_attempts(
    database: Database, audit_log: AuditLog, sample_job: AIJob
) -> None:
    policy = JobRetryPolicy(max_attempts=1)
    manager = RetryManager(database, audit_log, policy=policy, clock=_fixed_clock(T0))

    res = manager.schedule_retry(sample_job.id, "Persistent error")

    assert res.should_retry is False
    assert res.is_terminal is True
    assert "Exceeded max retry attempts" in res.reason

    with database.connection() as conn:
        persisted = AIJobRepository(conn).get(sample_job.id)
        assert persisted is not None
        assert persisted.checkpoints[-1].step == "retry.terminal"


def test_schedule_retry_non_retryable_error(
    database: Database, audit_log: AuditLog, sample_job: AIJob
) -> None:
    manager = RetryManager(database, audit_log, clock=_fixed_clock(T0))
    res = manager.schedule_retry(sample_job.id, "Fatal Policy_Violation in script")

    assert res.should_retry is False
    assert res.is_terminal is True
    assert "Non-retryable error" in res.reason


def test_execute_retry(
    database: Database, audit_log: AuditLog, sample_job: AIJob
) -> None:
    manager = RetryManager(database, audit_log, clock=_fixed_clock(T0))
    manager.schedule_retry(sample_job.id, "Transient network drop")

    restarted = manager.execute_retry(sample_job.id)
    assert restarted.status is AIJobStatus.RUNNING
    assert restarted.attempts == 2

    with database.connection() as conn:
        persisted = AIJobRepository(conn).get(sample_job.id)
        assert persisted is not None
        assert persisted.status is AIJobStatus.RUNNING
        assert persisted.attempts == 2


def test_list_retriable_jobs(
    database: Database, audit_log: AuditLog, sample_job: AIJob
) -> None:
    manager = RetryManager(database, audit_log, clock=_fixed_clock(T0))
    manager.schedule_retry(sample_job.id, "Temporary 503 error")

    retriable = manager.list_retriable_jobs()
    assert len(retriable) == 1
    assert retriable[0].id == sample_job.id
