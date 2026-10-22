"""T-233 Budget Test: Exceeding daily or monthly budget thresholds stops
cost-incurring jobs across CostLedger, BudgetGuard, BudgetGate, and JobScheduler.
"""

from decimal import Decimal

import pytest

from ai_youtube_agent.content.budget_guard import (
    BudgetGuard,
    BudgetGuardBlockedError,
)
from ai_youtube_agent.content.channel import ChannelStatus
from ai_youtube_agent.content.cost import CostCategory
from ai_youtube_agent.content.cost_ledger import CostLedger
from ai_youtube_agent.content.strategy import Budget, Cadence
from ai_youtube_agent.core.audit import AuditLog, InMemoryAuditSink
from ai_youtube_agent.core.budget_gate import BudgetGate
from ai_youtube_agent.core.content_item import ContentStatus, ContentType
from ai_youtube_agent.core.db.database import Database
from ai_youtube_agent.core.db.repositories.channel import (
    ChannelRepository,
    StrategyProfileRepository,
)
from ai_youtube_agent.core.db.repositories.content import ContentItemRepository
from ai_youtube_agent.core.db.repositories.economics import CostRecordRepository
from ai_youtube_agent.core.flags import FeatureFlags
from ai_youtube_agent.core.gates import GateContext, evaluate_gates
from ai_youtube_agent.pipeline.job_queue import SqliteJobQueue
from ai_youtube_agent.pipeline.job_scheduler import JobScheduler
from factories import (
    SYSTEM,
    at,
    make_channel,
    make_content_item,
    make_strategy_profile,
)


def test_exceeding_budget_threshold_blocks_cost_incurring_jobs(
    database: Database,
) -> None:
    """Configures a $5.00 daily budget, records $5.50 of LLM/TTS costs in CostLedger,
    and verifies BudgetGuard, BudgetGate, and JobScheduler block new production."""
    sink = InMemoryAuditSink()
    audit = AuditLog(sink)
    cost_ledger = CostLedger(database, audit, clock=at(1))
    budget_guard = BudgetGuard(database, audit, clock=at(1))
    queue = SqliteJobQueue(database, clock=at(1))
    scheduler = JobScheduler(
        database,
        queue,
        audit,
        budget_guard=budget_guard,
        flags=FeatureFlags(shorts_enabled=True, longform_enabled=True),
        clock=at(1),
    )

    channel = make_channel(status=ChannelStatus.ACTIVE)
    profile = make_strategy_profile(
        channel,
        cadence=Cadence(shorts_per_day=5, longform_per_day=2),
        budget=Budget(
            currency="USD",
            daily_limit=Decimal("5.00"),
            monthly_limit=Decimal("50.00"),
        ),
    )
    item = make_content_item(
        channel,
        profile,
        content_type=ContentType.SHORTS,
        status=ContentStatus.DRAFT,
    )

    with database.transaction() as conn:
        ChannelRepository(conn).add(channel)
        StrategyProfileRepository(conn).add(profile)
        ContentItemRepository(conn).add(item)

    # 1. Record $3.00 + $2.50 = $5.50 > $5.00 daily limit
    cost_ledger.record(
        channel.id,
        CostCategory.LLM,
        provider="mock_llm",
        amount=Decimal("3.00"),
        currency="USD",
        content_item_id=item.id,
    )
    cost_ledger.record(
        channel.id,
        CostCategory.TTS,
        provider="mock_tts",
        amount=Decimal("2.50"),
        currency="USD",
        content_item_id=item.id,
    )

    # 2. BudgetGuard blocks and raises BudgetGuardBlockedError on enforce
    status = budget_guard.evaluate(channel.id, at=at(2))
    assert status.allowed is False
    with pytest.raises(BudgetGuardBlockedError):
        budget_guard.enforce(channel.id, at=at(2))

    # 3. JobScheduler refuses to schedule new production when budget is exceeded
    decision = scheduler.schedule_item(item, at=at(2))
    assert decision.scheduled is False
    assert decision.code == "budget_exceeded"

    # 4. BudgetGate also blocks transitioning item to GENERATING
    with database.connection() as conn:
        gate = BudgetGate(
            StrategyProfileRepository(conn),
            CostRecordRepository(conn),
        )
        report = evaluate_gates(
            [gate],
            GateContext(item, ContentStatus.GENERATING, SYSTEM, at(2)),
        )
    assert bool(report.blocked) is True
    assert any(r.code == "budget.daily_exceeded" for r in report.reasons)
