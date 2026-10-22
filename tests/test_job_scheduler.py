"""Tests for JobScheduler (Prompt Pack v8, prompt #205), context C16 Orchestration."""

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from ai_youtube_agent.content.budget_guard import BudgetGuardStatus
from ai_youtube_agent.content.channel import Channel, ChannelStatus, YouTubeIdentifiers
from ai_youtube_agent.content.strategy import (
    Cadence,
    StrategyProfile,
)
from ai_youtube_agent.core.audit import (
    Actor,
    ActorKind,
    AuditLog,
    InMemoryAuditSink,
)
from ai_youtube_agent.core.content_item import ContentItem, ContentStatus, ContentType
from ai_youtube_agent.core.db.database import Database
from ai_youtube_agent.core.db.repositories.channel import (
    ChannelRepository,
    StrategyProfileRepository,
)
from ai_youtube_agent.core.db.repositories.content import ContentItemRepository
from ai_youtube_agent.core.db.repositories.jobs import (
    AIJobRepository,
    SessionRepository,
)
from ai_youtube_agent.core.db.repositories.usage import ProductionStartRepository
from ai_youtube_agent.core.flags import FeatureFlags
from ai_youtube_agent.core.kill_switch_gate import KillSwitchSource
from ai_youtube_agent.core.production_start import ProductionStart
from ai_youtube_agent.pipeline.job import AIJobStatus, Session
from ai_youtube_agent.pipeline.job_queue import InMemoryJobQueue
from ai_youtube_agent.pipeline.job_scheduler import (
    JOB_SCHEDULED_ACTION,
    JobScheduler,
)
from ai_youtube_agent.pipeline.kill_switch import EmergencyStop


class StubKillSwitch(KillSwitchSource):
    def __init__(self, stop: EmergencyStop | None = None) -> None:
        self.stop = stop or EmergencyStop.inactive()

    def current(self) -> EmergencyStop:
        return self.stop


class StubBudgetGuard:
    def __init__(self, allowed: bool = True) -> None:
        self.allowed = allowed

    def evaluate(
        self, channel_id: str, *, at: datetime | None = None
    ) -> BudgetGuardStatus:
        now = at or datetime.now(UTC)
        return BudgetGuardStatus(
            channel_id=channel_id,
            time_zone="UTC",
            currency="USD",
            day_start=now,
            day_end=now + timedelta(days=1),
            month_start=now,
            month_end=now + timedelta(days=30),
            daily_limit=Decimal("100.00"),
            monthly_limit=Decimal("1000.00"),
            daily_spend=Decimal("0.00") if self.allowed else Decimal("100.00"),
            monthly_spend=Decimal("0.00") if self.allowed else Decimal("1000.00"),
            estimated_additional_cost=Decimal("0.00"),
            projected_daily_spend=Decimal("0.00")
            if self.allowed
            else Decimal("100.00"),
            projected_monthly_spend=Decimal("0.00")
            if self.allowed
            else Decimal("1000.00"),
            daily_remaining=Decimal("100.00") if self.allowed else Decimal("0.00"),
            monthly_remaining=Decimal("1000.00") if self.allowed else Decimal("0.00"),
            allowed=self.allowed,
            blocking_reasons=(),
            alerts=(),
        )


def _seed_channel_and_strategy(
    database: Database,
    channel_id: str,
    *,
    status: ChannelStatus = ChannelStatus.ACTIVE,
    shorts_per_day: int = 5,
    longform_per_day: int = 2,
    time_zone: str = "UTC",
    has_strategy: bool = True,
    has_cadence: bool = True,
) -> tuple[Channel, StrategyProfile | None]:
    now = datetime(2026, 10, 21, 12, 0, tzinfo=UTC)
    channel = Channel(
        id=channel_id,
        title="Tech Test Channel",
        youtube=YouTubeIdentifiers(channel_id=f"UC{channel_id[:22].ljust(22, 'x')}"),
        status=status,
        created_at=now,
        updated_at=now,
    )
    with database.transaction() as conn:
        ChannelRepository(conn).add(channel)

    strategy: StrategyProfile | None = None
    if has_strategy:
        strategy = StrategyProfile.create(
            channel_id=channel_id,
            actor=Actor(ActorKind.USER, "owner"),
            clock=lambda: now,
        )
        if has_cadence:
            cadence = Cadence(
                shorts_per_day=shorts_per_day,
                longform_per_day=longform_per_day,
                time_zone=time_zone,
            )
            strategy = strategy.update(
                cadence=cadence, actor=Actor(ActorKind.USER, "owner"), clock=lambda: now
            )
        with database.transaction() as conn:
            StrategyProfileRepository(conn).add(strategy)

    return channel, strategy


def test_evaluate_eligibility_refuses_terminal_or_non_draft_status(
    database: Database,
) -> None:
    channel_id = str(uuid.uuid4())
    _seed_channel_and_strategy(database, channel_id)

    now = datetime(2026, 10, 21, 12, 0, tzinfo=UTC)
    item_published = ContentItem(
        id=str(uuid.uuid4()),
        channel_id=channel_id,
        strategy_profile_id=channel_id,
        strategy_version=1,
        content_type=ContentType.SHORTS,
        title="Published",
        status=ContentStatus.PUBLISHED,
        created_at=now,
        updated_at=now,
    )
    item_generating = ContentItem(
        id=str(uuid.uuid4()),
        channel_id=channel_id,
        strategy_profile_id=channel_id,
        strategy_version=1,
        content_type=ContentType.SHORTS,
        title="Generating",
        status=ContentStatus.GENERATING,
        created_at=now,
        updated_at=now,
    )

    scheduler = JobScheduler(
        database=database,
        job_queue=InMemoryJobQueue(),
        audit_log=AuditLog(InMemoryAuditSink()),
    )

    el_pub = scheduler.evaluate_eligibility(item_published)
    assert not el_pub.eligible
    assert el_pub.code == "terminal_status"

    el_gen = scheduler.evaluate_eligibility(item_generating)
    assert not el_gen.eligible
    assert el_gen.code == "not_draft"


def test_evaluate_eligibility_checks_feature_flags(database: Database) -> None:
    channel_id = str(uuid.uuid4())
    _seed_channel_and_strategy(database, channel_id)

    now = datetime(2026, 10, 21, 12, 0, tzinfo=UTC)
    longform_item = ContentItem(
        id=str(uuid.uuid4()),
        channel_id=channel_id,
        strategy_profile_id=channel_id,
        strategy_version=1,
        content_type=ContentType.LONGFORM,
        title="Long Video",
        status=ContentStatus.DRAFT,
        created_at=now,
        updated_at=now,
    )

    flags = FeatureFlags(longform_enabled=False, shorts_enabled=True)
    scheduler = JobScheduler(
        database=database,
        job_queue=InMemoryJobQueue(),
        audit_log=AuditLog(InMemoryAuditSink()),
        flags=flags,
    )

    el = scheduler.evaluate_eligibility(longform_item)
    assert not el.eligible
    assert el.code == "longform_disabled"


def test_evaluate_eligibility_refuses_when_kill_switch_active(
    database: Database,
) -> None:
    channel_id = str(uuid.uuid4())
    _seed_channel_and_strategy(database, channel_id)

    now = datetime(2026, 10, 21, 12, 0, tzinfo=UTC)
    item = ContentItem(
        id=str(uuid.uuid4()),
        channel_id=channel_id,
        strategy_profile_id=channel_id,
        strategy_version=1,
        content_type=ContentType.SHORTS,
        title="Draft Short",
        status=ContentStatus.DRAFT,
        created_at=now,
        updated_at=now,
    )

    stop = EmergencyStop.activated(
        by=Actor(ActorKind.USER, "admin"),
        reason="Security incident",
        clock=lambda: now,
    )
    scheduler = JobScheduler(
        database=database,
        job_queue=InMemoryJobQueue(),
        audit_log=AuditLog(InMemoryAuditSink()),
        kill_switch=StubKillSwitch(stop),
    )

    el = scheduler.evaluate_eligibility(item, at=now)
    assert not el.eligible
    assert el.code == "killswitch_active"
    assert "Security incident" in (el.reason or "")


def test_evaluate_eligibility_refuses_inactive_channel(database: Database) -> None:
    channel_id = str(uuid.uuid4())
    _seed_channel_and_strategy(database, channel_id, status=ChannelStatus.PAUSED)

    now = datetime(2026, 10, 21, 12, 0, tzinfo=UTC)
    item = ContentItem(
        id=str(uuid.uuid4()),
        channel_id=channel_id,
        strategy_profile_id=channel_id,
        strategy_version=1,
        content_type=ContentType.SHORTS,
        title="Draft Short",
        status=ContentStatus.DRAFT,
        created_at=now,
        updated_at=now,
    )

    scheduler = JobScheduler(
        database=database,
        job_queue=InMemoryJobQueue(),
        audit_log=AuditLog(InMemoryAuditSink()),
    )

    el = scheduler.evaluate_eligibility(item)
    assert not el.eligible
    assert el.code == "channel_paused"


def test_evaluate_eligibility_checks_cadence_and_daily_limit(
    database: Database,
) -> None:
    channel_id = str(uuid.uuid4())
    _, strategy = _seed_channel_and_strategy(database, channel_id, shorts_per_day=1)

    now = datetime(2026, 10, 21, 12, 0, tzinfo=UTC)
    item1 = ContentItem(
        id=str(uuid.uuid4()),
        channel_id=channel_id,
        strategy_profile_id=strategy.id,
        strategy_version=1,
        content_type=ContentType.SHORTS,
        title="Draft Short 1",
        status=ContentStatus.DRAFT,
        created_at=now,
        updated_at=now,
    )
    with database.transaction() as conn:
        ContentItemRepository(conn).add(item1)

    scheduler = JobScheduler(
        database=database,
        job_queue=InMemoryJobQueue(),
        audit_log=AuditLog(InMemoryAuditSink()),
        clock=lambda: now,
    )

    # 1. First item is eligible
    el1 = scheduler.evaluate_eligibility(item1, at=now)
    assert el1.eligible

    # 2. Simulate production start of item1
    with database.transaction() as conn:
        start = ProductionStart.of(item1, now)
        ProductionStartRepository(conn).add(start)

    # 3. Second item should now be blocked by daily limit
    item2 = ContentItem(
        id=str(uuid.uuid4()),
        channel_id=channel_id,
        strategy_profile_id=strategy.id,
        strategy_version=1,
        content_type=ContentType.SHORTS,
        title="Draft Short 2",
        status=ContentStatus.DRAFT,
        created_at=now,
        updated_at=now,
    )
    el2 = scheduler.evaluate_eligibility(item2, at=now)
    assert not el2.eligible
    assert el2.code == "daily_production_limit_reached"


def test_schedule_item_transitions_and_enqueues_job(database: Database) -> None:
    channel_id = str(uuid.uuid4())
    _, strategy = _seed_channel_and_strategy(database, channel_id, shorts_per_day=3)

    now = datetime(2026, 10, 21, 12, 0, tzinfo=UTC)
    item = ContentItem(
        id=str(uuid.uuid4()),
        channel_id=channel_id,
        strategy_profile_id=strategy.id,
        strategy_version=1,
        content_type=ContentType.SHORTS,
        title="Draft Short",
        status=ContentStatus.DRAFT,
        created_at=now,
        updated_at=now,
    )
    with database.transaction() as conn:
        ContentItemRepository(conn).add(item)

    sink = InMemoryAuditSink()
    audit = AuditLog(sink)
    queue = InMemoryJobQueue()
    scheduler = JobScheduler(
        database=database,
        job_queue=queue,
        audit_log=audit,
        clock=lambda: now,
    )

    session = Session.start(actor=Actor(ActorKind.USER, "owner"), clock=lambda: now)
    with database.transaction() as conn:
        SessionRepository(conn).add(session)
    session_id = session.id
    decision = scheduler.schedule_item(item, session_id=session_id, at=now)

    assert decision.scheduled is True
    assert decision.job_id is not None
    assert decision.session_id == session_id

    # Verify item moved to GENERATING
    with database.connection() as conn:
        updated_item = ContentItemRepository(conn).get(item.id)
        assert updated_item is not None
        assert updated_item.status is ContentStatus.GENERATING

        # Verify AI job created
        jobs = AIJobRepository(conn).list_by_content_item(item.id)
        assert len(jobs) == 1
        assert jobs[0].id == decision.job_id
        assert jobs[0].status is AIJobStatus.QUEUED

    # Verify audit event
    events = sink.events()
    assert any(e.action == JOB_SCHEDULED_ACTION for e in events)


def test_remaining_capacity_calculation(database: Database) -> None:
    channel_id = str(uuid.uuid4())
    _, strategy = _seed_channel_and_strategy(database, channel_id, shorts_per_day=5)

    now = datetime(2026, 10, 21, 12, 0, tzinfo=UTC)
    scheduler = JobScheduler(
        database=database,
        job_queue=InMemoryJobQueue(),
        audit_log=AuditLog(InMemoryAuditSink()),
        clock=lambda: now,
    )

    cap_before = scheduler.remaining_capacity(channel_id, ContentType.SHORTS, at=now)
    assert cap_before == 5

    # Record 2 production starts
    item = ContentItem(
        id=str(uuid.uuid4()),
        channel_id=channel_id,
        strategy_profile_id=strategy.id,
        strategy_version=1,
        content_type=ContentType.SHORTS,
        title="Short",
        status=ContentStatus.DRAFT,
        created_at=now,
        updated_at=now,
    )
    with database.transaction() as conn:
        ContentItemRepository(conn).add(item)
        ProductionStartRepository(conn).add(ProductionStart.of(item, now))
        ProductionStartRepository(conn).add(ProductionStart.of(item, now))

    cap_after = scheduler.remaining_capacity(channel_id, ContentType.SHORTS, at=now)
    assert cap_after == 3


def test_schedule_channel_batch_limits(database: Database) -> None:
    channel_id = str(uuid.uuid4())
    _, strategy = _seed_channel_and_strategy(database, channel_id, shorts_per_day=2)

    now = datetime(2026, 10, 21, 12, 0, tzinfo=UTC)
    items = [
        ContentItem(
            id=str(uuid.uuid4()),
            channel_id=channel_id,
            strategy_profile_id=strategy.id,
            strategy_version=1,
            content_type=ContentType.SHORTS,
            title=f"Short {i}",
            status=ContentStatus.DRAFT,
            created_at=now,
            updated_at=now,
        )
        for i in range(4)
    ]
    with database.transaction() as conn:
        for item in items:
            ContentItemRepository(conn).add(item)

    scheduler = JobScheduler(
        database=database,
        job_queue=InMemoryJobQueue(),
        audit_log=AuditLog(InMemoryAuditSink()),
        clock=lambda: now,
    )

    decisions = scheduler.schedule_channel(channel_id, at=now)
    scheduled = [d for d in decisions if d.scheduled]
    assert len(scheduled) == 2
    # Third item is blocked by daily limit
    blocked = [d for d in decisions if not d.scheduled]
    assert len(blocked) >= 1
    assert blocked[0].code == "daily_production_limit_reached"
