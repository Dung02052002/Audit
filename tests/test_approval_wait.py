"""Tests for ApprovalWaitService (Prompt Pack v8, task R-207)."""

from datetime import UTC, datetime, timedelta

import pytest

from ai_youtube_agent.content.approval import (
    ApprovalRequest,
    ApprovalStatus,
)
from ai_youtube_agent.content.approval_service import ApprovalService
from ai_youtube_agent.core.audit import (
    Actor,
    ActorKind,
    AuditLog,
)
from ai_youtube_agent.core.content_item import ContentItem, ContentStatus, ContentType
from ai_youtube_agent.core.db.database import Database
from ai_youtube_agent.core.db.repositories.content import ContentItemRepository
from ai_youtube_agent.core.db.repositories.jobs import AIJobRepository
from ai_youtube_agent.core.db.repositories.review import ApprovalRequestRepository
from ai_youtube_agent.pipeline.approval_wait import (
    ApprovalWaitError,
    ApprovalWaitService,
    ApprovalWaitStatus,
)
from ai_youtube_agent.pipeline.job import AIJob, AIJobStatus
from ai_youtube_agent.pipeline.job_queue import InMemoryJobQueue
from ai_youtube_agent.pipeline.stages import PipelineStage

T0 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
USER = Actor(ActorKind.USER, "alice")


def _fixed_clock(dt: datetime):
    return lambda: dt


@pytest.fixture
def db(tmp_path):
    database = Database(str(tmp_path / "test.db"))
    database.run_migrations()
    return database


@pytest.fixture
def audit_log(db):
    return AuditLog(db)


@pytest.fixture
def sample_item(db):
    item = ContentItem.create(
        title="Test Video",
        content_type=ContentType.SHORTS,
        clock=_fixed_clock(T0),
    )
    with db.transaction() as conn:
        ContentItemRepository(conn).add(item)
    return item


@pytest.fixture
def sample_approval(db, sample_item):
    req = ApprovalRequest.create(
        content_item_id=sample_item.id,
        artifacts=(),
        requested_by=Actor(ActorKind.SYSTEM, "orchestrator"),
        clock=_fixed_clock(T0),
    )
    with db.transaction() as conn:
        ApprovalRequestRepository(conn).add(req)
    return req


@pytest.fixture
def sample_job(db, sample_item):
    job = AIJob.create(
        content_item_id=sample_item.id,
        pipeline_stage=PipelineStage.APPROVAL,
        clock=_fixed_clock(T0),
    ).start(clock=_fixed_clock(T0))
    with db.transaction() as conn:
        AIJobRepository(conn).add(job)
    return job


def test_pause_for_approval_transitions_to_waiting(db, audit_log, sample_job, sample_approval):
    service = ApprovalWaitService(db, audit_log, clock=_fixed_clock(T0 + timedelta(seconds=10)))
    waiting = service.pause_for_approval(sample_job.id, sample_approval.id)

    assert waiting.status is AIJobStatus.WAITING
    assert waiting.checkpoints[-1].step == "approval.wait"
    assert waiting.checkpoints[-1].data.get("approval_request_id") == sample_approval.id

    with db.transaction() as conn:
        persisted = AIJobRepository(conn).get(sample_job.id)
        assert persisted is not None
        assert persisted.status is AIJobStatus.WAITING


def test_pause_for_approval_rejects_non_running_job(db, audit_log, sample_item, sample_approval):
    pending_job = AIJob.create(
        content_item_id=sample_item.id,
        pipeline_stage=PipelineStage.APPROVAL,
        clock=_fixed_clock(T0),
    )
    with db.transaction() as conn:
        AIJobRepository(conn).add(pending_job)

    service = ApprovalWaitService(db, audit_log, clock=_fixed_clock(T0))
    with pytest.raises(ApprovalWaitError, match="must be in RUNNING status"):
        service.pause_for_approval(pending_job.id, sample_approval.id)


def test_pause_for_approval_rejects_missing_approval(db, audit_log, sample_job):
    service = ApprovalWaitService(db, audit_log, clock=_fixed_clock(T0))
    with pytest.raises(ApprovalWaitError, match="Approval request .* not found"):
        service.pause_for_approval(sample_job.id, "nonexistent-approval")


def test_check_and_resolve_resumes_when_approved(db, audit_log, sample_job, sample_approval):
    queue = InMemoryJobQueue(clock=_fixed_clock(T0))
    service = ApprovalWaitService(db, audit_log, queue=queue, clock=_fixed_clock(T0 + timedelta(seconds=10)))
    service.pause_for_approval(sample_job.id, sample_approval.id)

    # Approve request
    with db.transaction() as conn:
        approval_repo = ApprovalRequestRepository(conn)
        req = approval_repo.get(sample_approval.id)
        assert req is not None
        approved_req = req.approve(actor=USER, clock=_fixed_clock(T0 + timedelta(seconds=20)))
        approval_repo.update(approved_req, expected_updated_at=req.updated_at)

    res = service.check_and_resolve(sample_job.id)
    assert res.status == ApprovalWaitStatus.RESUMED
    assert res.approval_request_id == sample_approval.id

    with db.transaction() as conn:
        job = AIJobRepository(conn).get(sample_job.id)
        assert job is not None
        assert job.status is AIJobStatus.RUNNING

    # Verify enqueued into queue
    assert queue.depth() == 1


def test_check_and_resolve_cancels_when_rejected(db, audit_log, sample_job, sample_approval):
    service = ApprovalWaitService(db, audit_log, clock=_fixed_clock(T0 + timedelta(seconds=10)))
    service.pause_for_approval(sample_job.id, sample_approval.id)

    with db.transaction() as conn:
        approval_repo = ApprovalRequestRepository(conn)
        req = approval_repo.get(sample_approval.id)
        assert req is not None
        rejected_req = req.reject(actor=USER, reason="Audio out of sync", clock=_fixed_clock(T0 + timedelta(seconds=20)))
        approval_repo.update(rejected_req, expected_updated_at=req.updated_at)

    res = service.check_and_resolve(sample_job.id)
    assert res.status == ApprovalWaitStatus.REJECTED

    with db.transaction() as conn:
        job = AIJobRepository(conn).get(sample_job.id)
        assert job is not None
        assert job.status is AIJobStatus.CANCELLED
        assert "Audio out of sync" in (job.last_error or "")


def test_check_and_resolve_still_waiting_when_pending(db, audit_log, sample_job, sample_approval):
    service = ApprovalWaitService(db, audit_log, clock=_fixed_clock(T0 + timedelta(seconds=10)))
    service.pause_for_approval(sample_job.id, sample_approval.id)

    res = service.check_and_resolve(sample_job.id)
    assert res.status == ApprovalWaitStatus.STILL_WAITING

    with db.transaction() as conn:
        job = AIJobRepository(conn).get(sample_job.id)
        assert job is not None
        assert job.status is AIJobStatus.WAITING


def test_poll_waiting_jobs(db, audit_log, sample_job, sample_approval):
    service = ApprovalWaitService(db, audit_log, clock=_fixed_clock(T0 + timedelta(seconds=10)))
    service.pause_for_approval(sample_job.id, sample_approval.id)

    # Poll when pending
    waiting_jobs = service.get_waiting_jobs()
    assert len(waiting_jobs) == 1
    assert waiting_jobs[0].id == sample_job.id

    resolutions = service.poll_waiting_jobs()
    assert len(resolutions) == 1
    assert resolutions[0].status == ApprovalWaitStatus.STILL_WAITING
