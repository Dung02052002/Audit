"""T-234 Kill Switch Test: Activating KillSwitchService during an active session
immediately stops new production, cancels queued jobs, and blocks PipelineRunner.
"""

import pytest

from ai_youtube_agent.content.channel import ChannelStatus
from ai_youtube_agent.content.strategy import Cadence
from ai_youtube_agent.core.audit import AuditLog, InMemoryAuditSink
from ai_youtube_agent.core.config import Environment, Settings
from ai_youtube_agent.core.content_item import ContentStatus, ContentType
from ai_youtube_agent.core.db.database import Database
from ai_youtube_agent.core.db.repositories.channel import (
    ChannelRepository,
    StrategyProfileRepository,
)
from ai_youtube_agent.core.db.repositories.content import ContentItemRepository
from ai_youtube_agent.core.db.repositories.jobs import AIJobRepository
from ai_youtube_agent.core.flags import FeatureFlags
from ai_youtube_agent.pipeline.job import AIJobStatus
from ai_youtube_agent.pipeline.job_queue import SqliteJobQueue
from ai_youtube_agent.pipeline.job_scheduler import JobScheduler
from ai_youtube_agent.pipeline.kill_switch_service import (
    KillSwitchBlockedError,
    KillSwitchService,
)
from ai_youtube_agent.pipeline.pipeline_runner import PipelineRunner
from ai_youtube_agent.pipeline.session_manager import SessionManager
from factories import (
    SYSTEM,
    USER,
    at,
    make_channel,
    make_content_item,
    make_strategy_profile,
)


def test_kill_switch_activation_during_session_stops_production_and_publish(
    database: Database,
) -> None:
    """Starts an active session, schedules a job, activates the kill switch, and
    verifies queued jobs are cancelled and new production/publish transitions stop."""
    sink = InMemoryAuditSink()
    audit = AuditLog(sink)
    settings = Settings(environment=Environment.TEST)
    flags = FeatureFlags(
        shorts_enabled=True, longform_enabled=True, publish_enabled=True
    )

    kill_switch = KillSwitchService(audit, database=database, clock=at(1))
    queue = SqliteJobQueue(database, clock=at(1))
    session_mgr = SessionManager(database, audit, clock=at(1))
    scheduler = JobScheduler(
        database,
        queue,
        audit,
        kill_switch=kill_switch,
        flags=flags,
        clock=at(1),
    )
    runner = PipelineRunner(
        database,
        settings,
        audit,
        flags=flags,
        kill_switch=kill_switch,
        clock=at(1),
    )

    channel = make_channel(status=ChannelStatus.ACTIVE)
    profile = make_strategy_profile(
        channel, cadence=Cadence(shorts_per_day=5, longform_per_day=2)
    )
    item1 = make_content_item(
        channel,
        profile,
        content_type=ContentType.SHORTS,
        status=ContentStatus.DRAFT,
    )
    item2 = make_content_item(
        channel,
        profile,
        content_type=ContentType.SHORTS,
        status=ContentStatus.DRAFT,
    )

    with database.transaction() as conn:
        ChannelRepository(conn).add(channel)
        StrategyProfileRepository(conn).add(profile)
        ContentItemRepository(conn).add(item1)
        ContentItemRepository(conn).add(item2)

    # 1. Start session and schedule item1
    session = session_mgr.start_session(actor=USER)
    d1 = scheduler.schedule_item(item1, session_id=session.id, at=at(1))
    assert d1.scheduled is True
    assert d1.job_id is not None

    # 2. Activate kill switch during the session -> cancels queued job for item1
    transition = kill_switch.activate(
        actor=USER,
        reason="Emergency stop triggered during session",
        cancel_queued_jobs=True,
    )
    assert transition.current.active is True
    assert d1.job_id in transition.cancelled_job_ids

    with database.connection() as conn:
        job1 = AIJobRepository(conn).get(d1.job_id)
        assert job1 is not None
        assert job1.status is AIJobStatus.CANCELLED

    # 3. ensure_inactive raises KillSwitchBlockedError
    with pytest.raises(KillSwitchBlockedError):
        kill_switch.ensure_inactive(actor=SYSTEM)

    # 4. JobScheduler refuses to schedule item2 while kill switch is active
    d2 = scheduler.schedule_item(item2, session_id=session.id, at=at(2))
    assert d2.scheduled is False
    assert d2.code == "killswitch_active"

    # 5. PipelineRunner refuses to advance item2 from DRAFT to GENERATING
    step = runner.advance(item2.id, actor=SYSTEM, at=at(2))
    assert step.transitioned is False
    assert step.gate_report is not None
    assert bool(step.gate_report.blocked) is True
    assert any(
        r.code in ("killswitch.active", "kill_switch.active")
        for r in step.gate_report.reasons
    )

    # 6. Deactivate kill switch -> item2 can now be scheduled
    kill_switch.deactivate(actor=USER, reason="All clear")
    d2_after = scheduler.schedule_item(item2, session_id=session.id, at=at(3))
    assert d2_after.scheduled is True
