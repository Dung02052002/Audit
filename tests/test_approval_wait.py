"""Tests for ApprovalWaitService (Prompt Pack v8, task R-207)."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from ai_youtube_agent.content.approval import (
    ApprovalRequest,
    ApprovalStatus,
)
from ai_youtube_agent.content.channel import Channel, ChannelStatus, YouTubeIdentifiers
from ai_youtube_agent.content.strategy import StrategyProfile
from ai_youtube_agent.core.artifact import Artifact, ArtifactKind
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
from ai_youtube_agent.core.db.repositories.content import (
    ArtifactRepository,
    ContentItemRepository,
)
from ai_youtube_agent.core.db.repositories.jobs import AIJobRepository
from ai_youtube_agent.core.db.repositories.review import ApprovalRequestRepository
from ai_youtube_agent.pipeline.approval_wait import (
    ApprovalWaitError,
    ApprovalWaitService,
    ApprovalWaitStatus,
)
from ai_youtube_agent.pipeline.job import AIJob, AIJobStatus
from ai_youtube_agent.pipeline.job_queue import InMemoryJobQueue

T0 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
USER = Actor(ActorKind.USER, "alice")


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
        title="Wait Test Channel",
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
        title="Test Video",
        clock=_fixed_clock(T0),
    )
    with database.transaction() as conn:
        ChannelRepository(conn).add(channel)
        StrategyProfileRepository(conn).add(strategy)
        ContentItemRepository(conn).add(item)
    return item


@pytest.fixture
def sample_approval(database: Database, sample_item: ContentItem) -> ApprovalRequest:
    artifact = Artifact(
        id=uuid.uuid4().hex,
        content_item_id=sample_item.id,
        kind=ArtifactKind.VIDEO,
        version=1,
        uri="file:///tmp/video.mp4",
        sha256="a" * 64,
        size_bytes=1024,
        media_type="video/mp4",
        created_at=T0,
    )
    req = ApprovalRequest.create(
        content_item_id=sample_item.id,
        artifacts=(artifact,),
        requested_by=Actor(ActorKind.SYSTEM, "orchestrator"),
        clock=_fixed_clock(T0),
    )
    with database.transaction() as conn:
        ArtifactRepository(conn).add(artifact)
        ApprovalRequestRepository(conn).add(req)
    return req


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


def test_pause_for_approval_transitions_to_waiting(
    database: Database,
    audit_log: AuditLog,
    sample_job: AIJob,
    sample_approval: ApprovalRequest,
) -> None:
    service = ApprovalWaitService(
        database, audit_log, clock=_fixed_clock(T0 + timedelta(seconds=10))
    )
    waiting = service.pause_for_approval(sample_job.id, sample_approval.id)

    assert waiting.status is AIJobStatus.WAITING
    assert waiting.checkpoints[-1].step == "approval.wait"
    assert waiting.checkpoints[-1].data.get("approval_request_id") == sample_approval.id

    with database.connection() as conn:
        persisted = AIJobRepository(conn).get(sample_job.id)
        assert persisted is not None
        assert persisted.status is AIJobStatus.WAITING


def test_pause_for_approval_rejects_non_running_job(
    database: Database,
    audit_log: AuditLog,
    sample_item: ContentItem,
    sample_approval: ApprovalRequest,
) -> None:
    pending_job = AIJob.create(
        "content.generate",
        "gen:queued-only",
        content_item_id=sample_item.id,
        clock=_fixed_clock(T0),
    )
    with database.transaction() as conn:
        AIJobRepository(conn).add(pending_job)

    service = ApprovalWaitService(database, audit_log, clock=_fixed_clock(T0))
    with pytest.raises(ApprovalWaitError, match="must be in RUNNING status"):
        service.pause_for_approval(pending_job.id, sample_approval.id)


def test_pause_for_approval_rejects_missing_approval(
    database: Database, audit_log: AuditLog, sample_job: AIJob
) -> None:
    service = ApprovalWaitService(database, audit_log, clock=_fixed_clock(T0))
    with pytest.raises(ApprovalWaitError, match="Approval request .* not found"):
        service.pause_for_approval(sample_job.id, "nonexistent-approval")


def test_check_and_resolve_resumes_when_approved(
    database: Database,
    audit_log: AuditLog,
    sample_job: AIJob,
    sample_approval: ApprovalRequest,
) -> None:
    queue = InMemoryJobQueue(clock=_fixed_clock(T0))
    service = ApprovalWaitService(
        database, audit_log, queue=queue, clock=_fixed_clock(T0 + timedelta(seconds=10))
    )
    service.pause_for_approval(sample_job.id, sample_approval.id)

    with database.transaction() as conn:
        approval_repo = ApprovalRequestRepository(conn)
        req = approval_repo.get(sample_approval.id)
        assert req is not None
        approved_req = req.approve(USER)
        approval_repo.update(approved_req, expected_status=ApprovalStatus.PENDING)

    res = service.check_and_resolve(sample_job.id)
    assert res.status is ApprovalWaitStatus.RESUMED
    assert res.approval_request_id == sample_approval.id

    with database.connection() as conn:
        job = AIJobRepository(conn).get(sample_job.id)
        assert job is not None
        assert job.status is AIJobStatus.RUNNING


def test_check_and_resolve_cancels_when_rejected(
    database: Database,
    audit_log: AuditLog,
    sample_job: AIJob,
    sample_approval: ApprovalRequest,
) -> None:
    service = ApprovalWaitService(
        database, audit_log, clock=_fixed_clock(T0 + timedelta(seconds=10))
    )
    service.pause_for_approval(sample_job.id, sample_approval.id)

    with database.transaction() as conn:
        approval_repo = ApprovalRequestRepository(conn)
        req = approval_repo.get(sample_approval.id)
        assert req is not None
        rejected_req = req.reject(USER, "Audio out of sync")
        approval_repo.update(rejected_req, expected_status=ApprovalStatus.PENDING)

    res = service.check_and_resolve(sample_job.id)
    assert res.status is ApprovalWaitStatus.REJECTED

    with database.connection() as conn:
        job = AIJobRepository(conn).get(sample_job.id)
        assert job is not None
        assert job.status is AIJobStatus.CANCELLED
        assert job.cancellation_reason is not None


def test_check_and_resolve_still_waiting_when_pending(
    database: Database,
    audit_log: AuditLog,
    sample_job: AIJob,
    sample_approval: ApprovalRequest,
) -> None:
    service = ApprovalWaitService(
        database, audit_log, clock=_fixed_clock(T0 + timedelta(seconds=10))
    )
    service.pause_for_approval(sample_job.id, sample_approval.id)

    res = service.check_and_resolve(sample_job.id)
    assert res.status is ApprovalWaitStatus.STILL_WAITING

    with database.connection() as conn:
        job = AIJobRepository(conn).get(sample_job.id)
        assert job is not None
        assert job.status is AIJobStatus.WAITING


def test_poll_waiting_jobs(
    database: Database,
    audit_log: AuditLog,
    sample_job: AIJob,
    sample_approval: ApprovalRequest,
) -> None:
    service = ApprovalWaitService(
        database, audit_log, clock=_fixed_clock(T0 + timedelta(seconds=10))
    )
    service.pause_for_approval(sample_job.id, sample_approval.id)

    waiting_jobs = service.get_waiting_jobs()
    assert len(waiting_jobs) == 1
    assert waiting_jobs[0].id == sample_job.id

    resolutions = service.poll_waiting_jobs()
    assert len(resolutions) == 1
    assert resolutions[0].status is ApprovalWaitStatus.STILL_WAITING
