"""End-to-end Phase R Orchestrator & Session Runtime scenario tests (R-215, prompt
#215).

Covers:
1. Session lifecycle, job scheduling within cadence/budget limits, and concurrent
   worker lease isolation via ``SqliteJobQueue``.
2. Shared pipeline progression, mandatory human approval wait & resume via
   ``ApprovalWaitService`` (``ActorKind.USER``), and bounded retry via ``RetryManager``.
3. Crash recovery from safe checkpoints via ``CrashResumeService`` and clean exit
   via ``GracefulShutdownService``.
4. Daily production limit enforcement via ``DailyStopService``, immediate emergency
   stop via ``KillSwitchService``, and full audit trail verification via
   ``OrchestratorAuditService``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from ai_youtube_agent.content.channel import (
    Channel,
    ChannelStatus,
    YouTubeIdentifiers,
)
from ai_youtube_agent.content.strategy import (
    Audience,
    Brand,
    Budget,
    Cadence,
    LanguageSettings,
    Market,
    Niche,
    Pillar,
    StrategyProfile,
)
from ai_youtube_agent.core.artifact import Artifact, ArtifactKind
from ai_youtube_agent.core.audit import Actor, ActorKind, AuditLog, InMemoryAuditSink
from ai_youtube_agent.core.config import Environment, Settings
from ai_youtube_agent.core.content_item import ContentItem, ContentStatus, ContentType
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
from ai_youtube_agent.core.flags import FeatureFlags
from ai_youtube_agent.pipeline.approval_wait import (
    ApprovalWaitService,
    ApprovalWaitStatus,
)
from ai_youtube_agent.pipeline.crash_resume import CrashResumeService
from ai_youtube_agent.pipeline.daily_stop import DailyStopService
from ai_youtube_agent.pipeline.graceful_shutdown import GracefulShutdownService
from ai_youtube_agent.pipeline.job import AIJobStatus
from ai_youtube_agent.pipeline.job_queue import SqliteJobQueue
from ai_youtube_agent.pipeline.job_scheduler import JobScheduler
from ai_youtube_agent.pipeline.kill_switch_service import KillSwitchService
from ai_youtube_agent.pipeline.orchestrator_audit import (
    OrchestratorAuditRecorder,
    OrchestratorAuditService,
)
from ai_youtube_agent.pipeline.pipeline_runner import PipelineRunner
from ai_youtube_agent.pipeline.retry_manager import JobRetryPolicy, RetryManager
from ai_youtube_agent.pipeline.session_manager import SessionManager


def _FixedClock(dt: datetime):
    return lambda: dt


def _seed_channel_and_strategy(
    database: Database,
    t0: datetime,
    *,
    shorts_per_day: int = 2,
) -> tuple[Channel, StrategyProfile]:
    user = Actor(ActorKind.USER, "dung")
    ch = Channel.create(
        "Finance Quick",
        YouTubeIdentifiers("UC1234567890123456789012", "@financequick"),
        clock=_FixedClock(t0),
    ).with_status(ChannelStatus.ACTIVE, clock=_FixedClock(t0))
    sp = StrategyProfile.create(
        ch.id,
        user,
        market=Market("US"),
        languages=LanguageSettings("en"),
        audience=Audience("Young professionals"),
        niche=Niche("Personal Finance", (Pillar("Investing"),)),
        brand=Brand("Finance Quick", "clear"),
        cadence=Cadence(
            shorts_per_day=shorts_per_day,
            longform_per_day=1,
            time_zone="UTC",
        ),
        budget=Budget("USD", Decimal("50.00"), Decimal("500.00")),
        clock=_FixedClock(t0),
    )
    with database.transaction() as conn:
        ChannelRepository(conn).add(ch)
        StrategyProfileRepository(conn).add(sp)
    return ch, sp


def test_orchestrator_end_to_end_lifecycle_concurrency_wait_retry_and_limits(
    database: Database,
) -> None:
    t0 = datetime(2026, 10, 21, 9, 0, tzinfo=UTC)
    user = Actor(ActorKind.USER, "dung")
    worker_a = Actor(ActorKind.SYSTEM, "worker-a")
    sink = InMemoryAuditSink()
    audit = AuditLog(sink, clock=_FixedClock(t0))
    settings = Settings(environment=Environment.TEST, database_path=database.path)

    ch, sp = _seed_channel_and_strategy(database, t0, shorts_per_day=1)

    item1 = ContentItem.create(
        ch.id, sp.id, sp.version, ContentType.SHORTS, "Short #1", clock=_FixedClock(t0)
    )
    item2 = ContentItem.create(
        ch.id,
        sp.id,
        sp.version,
        ContentType.SHORTS,
        "Short #2",
        clock=_FixedClock(t0 + timedelta(seconds=1)),
    )
    with database.transaction() as conn:
        ContentItemRepository(conn).add(item1)
        ContentItemRepository(conn).add(item2)

    queue = SqliteJobQueue(database, clock=_FixedClock(t0))
    kill_switch = KillSwitchService(audit, database=database, clock=_FixedClock(t0))
    session_mgr = SessionManager(database, audit, clock=_FixedClock(t0))
    daily_stop = DailyStopService(database, audit, clock=_FixedClock(t0))
    scheduler = JobScheduler(
        database,
        queue,
        audit,
        kill_switch=kill_switch,
        flags=FeatureFlags(shorts_enabled=True),
        clock=_FixedClock(t0),
    )
    runner = PipelineRunner(
        database,
        settings,
        audit,
        flags=FeatureFlags(shorts_enabled=True, publish_enabled=False),
        kill_switch=kill_switch,
        clock=_FixedClock(t0 + timedelta(seconds=10)),
    )
    wait_svc = ApprovalWaitService(
        database, audit, queue=queue, clock=_FixedClock(t0 + timedelta(seconds=20))
    )
    retry_mgr = RetryManager(
        database,
        audit,
        policy=JobRetryPolicy(max_attempts=3, base_delay_seconds=5.0),
        queue=queue,
        clock=_FixedClock(t0 + timedelta(seconds=30)),
    )
    audit_recorder = OrchestratorAuditRecorder(audit)
    audit_svc = OrchestratorAuditService(database, sink)

    # 1. Start user session & schedule channel items (daily limit = 1)
    session = session_mgr.start_session(user)
    decisions = scheduler.schedule_channel(ch.id, session_id=session.id, actor=user)
    assert len(decisions) == 2
    assert decisions[0].scheduled is True
    assert decisions[0].item_id == item1.id
    assert decisions[1].scheduled is False
    assert decisions[1].code == "daily_production_limit_reached"

    # DailyStopService confirms limit reached for SHORTS today
    stop_eval = daily_stop.check_channel_limit(ch.id, ContentType.SHORTS)
    assert stop_eval.is_stopped is True
    assert stop_eval.used_today == 1
    daily_stop.record_stop_event(ch.id, ContentType.SHORTS, stop_eval)

    # 2. Concurrent worker dequeue: worker-a acquires the single queued job; worker-b
    # gets None
    lease_a = queue.dequeue(worker_id="worker-a")
    lease_b = queue.dequeue(worker_id="worker-b")
    assert lease_a is not None
    assert lease_b is None
    job_id = lease_a.job.id
    audit_recorder.record_job_started(lease_a.job, actor=worker_a, worker_id="worker-a")

    # 3. Simulate transient failure and bounded retry
    eval_retry = retry_mgr.schedule_retry(job_id, "provider.timeout")
    assert eval_retry.should_retry is True
    restarted_job = retry_mgr.execute_retry(job_id)
    assert restarted_job.status is AIJobStatus.RUNNING
    assert restarted_job.attempts == 2

    # 4. Commit rendered video artifact and advance item1 through TESTING ->
    # PREVIEW_READY -> AWAITING_APPROVAL
    video = Artifact.create(
        item1.id,
        ArtifactKind.VIDEO,
        uri="file:///tmp/short1.mp4",
        sha256="a" * 64,
        size_bytes=1024,
        media_type="video/mp4",
        clock=_FixedClock(t0 + timedelta(seconds=15)),
    )
    with database.transaction() as conn:
        ArtifactRepository(conn).add(video)

    run_res = runner.run(item1.id, actor=worker_a)
    assert run_res.final_status is ContentStatus.AWAITING_APPROVAL
    assert run_res.waiting_for_approval is True

    # Pause job for human approval
    with database.connection() as conn:
        req = ApprovalRequestRepository(conn).list_by_content_item(item1.id)[-1]
    waiting_job = wait_svc.pause_for_approval(job_id, req.id)
    assert waiting_job.status is AIJobStatus.WAITING

    # Polling while still pending keeps job WAITING
    poll_pending = wait_svc.check_and_resolve(job_id)
    assert poll_pending.status is ApprovalWaitStatus.STILL_WAITING

    # Human user approves the request; polling resumes the job to RUNNING
    approved_req = req.approve(user, clock=_FixedClock(t0 + timedelta(seconds=25)))
    with database.transaction() as conn:
        ApprovalRequestRepository(conn).update(approved_req, expected_status=req.status)
    poll_approved = wait_svc.check_and_resolve(job_id)
    assert poll_approved.status is ApprovalWaitStatus.RESUMED

    # Runner advances AWAITING_APPROVAL -> APPROVED
    step_approved = runner.advance(item1.id, actor=user)
    assert step_approved.to_status is ContentStatus.APPROVED

    # Complete job
    with database.transaction() as conn:
        curr_job = AIJobRepository(conn).get(job_id)
        assert curr_job is not None
        done_job = curr_job.succeed(clock=_FixedClock(t0 + timedelta(seconds=40)))
        AIJobRepository(conn).update(done_job, expected_updated_at=curr_job.updated_at)
    audit_recorder.record_job_completed(done_job, actor=worker_a)

    # Verify audit trail for session and runtime
    session_trail = audit_svc.trail_for_session(session.id)
    assert session_trail.action_counts["job.started"] == 1
    assert session_trail.action_counts["job.retry_scheduled"] == 1
    assert session_trail.action_counts["job.approval_wait"] == 1
    assert session_trail.action_counts["job.approval_resumed"] == 1
    assert session_trail.action_counts["job.completed"] == 1


def test_orchestrator_crash_resume_graceful_shutdown_and_kill_switch(
    database: Database,
) -> None:
    t0 = datetime(2026, 10, 21, 10, 0, tzinfo=UTC)
    user = Actor(ActorKind.USER, "dung")
    sink = InMemoryAuditSink()
    audit = AuditLog(sink, clock=_FixedClock(t0))

    ch, sp = _seed_channel_and_strategy(database, t0, shorts_per_day=3)
    item = ContentItem.create(
        ch.id,
        sp.id,
        sp.version,
        ContentType.SHORTS,
        "Crash & Shutdown Short",
        clock=_FixedClock(t0),
    )
    with database.transaction() as conn:
        ContentItemRepository(conn).add(item)

    queue = SqliteJobQueue(database, clock=_FixedClock(t0))
    kill_switch = KillSwitchService(audit, database=database, clock=_FixedClock(t0))
    session_mgr = SessionManager(database, audit, clock=_FixedClock(t0))
    scheduler = JobScheduler(
        database,
        queue,
        audit,
        kill_switch=kill_switch,
        flags=FeatureFlags(shorts_enabled=True),
        clock=_FixedClock(t0),
    )

    session = session_mgr.start_session(user)
    dec = scheduler.schedule_item(item, session_id=session.id, actor=user)
    assert dec.scheduled is True and dec.job_id is not None

    # Lease job so it is RUNNING, then execute GracefulShutdownService
    lease = queue.dequeue(worker_id="worker-1")
    assert lease is not None

    shutdown_svc = GracefulShutdownService(
        database,
        queue,
        audit,
        clock=_FixedClock(t0 + timedelta(minutes=1)),
    )
    shutdown_report = shutdown_svc.shutdown(user, reason="planned_restart")
    assert shutdown_report.checkpointed_job_ids == (lease.job.id,)
    assert shutdown_report.stopped_session_ids == (session.id,)

    # Simulate process restart and recover interrupted jobs via CrashResumeService
    crash_resume = CrashResumeService(
        database,
        audit,
        queue=queue,
        clock=_FixedClock(t0 + timedelta(minutes=2)),
    )
    # The old process is gone, so its worker lease is dead: take the job over.
    recovery_report = crash_resume.recover_interrupted_jobs(
        force_reclaim=True, actor=user
    )
    assert len(recovery_report.recovered_jobs) == 1
    plan = recovery_report.recovered_jobs[0]
    assert plan.job.id == lease.job.id
    assert plan.checkpoint_state.has_safe_checkpoint
    assert plan.checkpoint_state.last_safe_step == "runtime.shutdown_checkpoint"

    # Activate kill switch and verify new scheduling is blocked immediately
    kill_switch.activate(user, reason="emergency maintenance", cancel_queued_jobs=True)
    item_blocked = ContentItem.create(
        ch.id,
        sp.id,
        sp.version,
        ContentType.SHORTS,
        "Blocked Short",
        clock=_FixedClock(t0 + timedelta(minutes=3)),
    )
    with database.transaction() as conn:
        ContentItemRepository(conn).add(item_blocked)

    blocked_dec = scheduler.schedule_item(item_blocked, actor=user)
    assert blocked_dec.scheduled is False
    assert blocked_dec.code == "killswitch_active"
