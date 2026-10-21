"""T-232 Daily Limit Test: Exceeding daily cadence limits stops new production
across JobScheduler, DailyStopService, and DailyLimitGate.
"""

from ai_youtube_agent.content.channel import ChannelStatus
from ai_youtube_agent.content.strategy import Cadence
from ai_youtube_agent.core.audit import AuditLog, InMemoryAuditSink
from ai_youtube_agent.core.content_item import ContentStatus, ContentType
from ai_youtube_agent.core.daily_limit_gate import DailyLimitGate
from ai_youtube_agent.core.db.database import Database
from ai_youtube_agent.core.db.repositories.channel import (
    ChannelRepository,
    StrategyProfileRepository,
)
from ai_youtube_agent.core.db.repositories.content import ContentItemRepository
from ai_youtube_agent.core.db.repositories.usage import DailyUsageRepository
from ai_youtube_agent.core.flags import FeatureFlags
from ai_youtube_agent.core.gates import GateContext, evaluate_gates
from ai_youtube_agent.pipeline.daily_stop import DailyStopService
from ai_youtube_agent.pipeline.job_queue import SqliteJobQueue
from ai_youtube_agent.pipeline.job_scheduler import JobScheduler
from factories import (
    SYSTEM,
    T0,
    at,
    make_channel,
    make_content_item,
    make_strategy_profile,
)


def test_exceeding_daily_cadence_limit_stops_new_production(
    database: Database,
) -> None:
    """Configures shorts_per_day=1, schedules the first Shorts item (succeeds),
    and verifies the second Shorts item is blocked by JobScheduler, DailyStopService,
    and DailyLimitGate."""
    sink = InMemoryAuditSink()
    audit = AuditLog(sink)
    queue = SqliteJobQueue(database, clock=at(1))
    flags = FeatureFlags(shorts_enabled=True, longform_enabled=True)
    scheduler = JobScheduler(
        database, queue, audit, flags=flags, clock=at(1)
    )
    daily_stop = DailyStopService(database, audit, clock=at(1))

    channel = make_channel(status=ChannelStatus.ACTIVE)
    profile = make_strategy_profile(
        channel, cadence=Cadence(shorts_per_day=1, longform_per_day=1)
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

    # 1. First Shorts item is scheduled and starts production (used_today becomes 1/1)
    d1 = scheduler.schedule_item(item1, at=T0)
    assert d1.scheduled is True
    assert d1.job_id is not None

    # 2. DailyStopService reports Shorts production is now stopped for the day
    stop_eval = daily_stop.check_channel_limit(channel.id, ContentType.SHORTS)
    assert stop_eval.is_stopped is True
    assert stop_eval.used_today == 1
    assert stop_eval.daily_limit == 1
    assert stop_eval.remaining_today == 0

    # 3. Second Shorts item is blocked by JobScheduler
    d2 = scheduler.schedule_item(item2, at=T0)
    assert d2.scheduled is False
    assert d2.code == "daily_production_limit_reached"

    # 4. DailyLimitGate also blocks item2 from moving to GENERATING
    with database.connection() as conn:
        gate = DailyLimitGate(
            StrategyProfileRepository(conn),
            DailyUsageRepository(conn),
        )
        report = evaluate_gates(
            [gate],
            GateContext(item2, ContentStatus.GENERATING, SYSTEM, T0),
        )
    assert bool(report.blocked) is True
    assert any(r.code in ("daily_limit.production_reached", "daily_limit.production_exceeded") for r in report.reasons)
