"""Tests for budget guard and threshold alert service (O-180, prompt #180)."""

import dataclasses
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from ai_youtube_agent.content.budget_guard import (
    BUDGET_ALERT_TRIGGERED,
    BUDGET_GUARD_BLOCKED,
    BudgetGuard,
    BudgetGuardBlockedError,
    BudgetGuardError,
    BudgetGuardErrorCode,
    BudgetWindowKind,
)
from ai_youtube_agent.content.channel import ChannelStatus
from ai_youtube_agent.content.cost import CostCategory, CostRecord
from ai_youtube_agent.content.cost_ledger import CostLedger
from ai_youtube_agent.content.strategy import Budget
from ai_youtube_agent.core.audit import AuditLog, AuditResult, InMemoryAuditSink
from ai_youtube_agent.core.db.database import Database
from ai_youtube_agent.core.db.repositories.channel import (
    ChannelRepository,
    StrategyProfileRepository,
)
from ai_youtube_agent.core.db.repositories.economics import CostRecordRepository
from factories import make_channel, make_strategy_profile

NOW = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)
EARLIER_THIS_MONTH = datetime(2026, 10, 5, 9, 0, tzinfo=UTC)


class TestBudgetGuard:
    def test_evaluates_alerts_and_projected_daily_and_monthly_limits(
        self, database: Database
    ) -> None:
        channel = make_channel(status=ChannelStatus.ACTIVE)
        budget = Budget(
            currency="USD",
            daily_limit=Decimal("10.00"),
            monthly_limit=Decimal("50.00"),
            alert_thresholds=(50, 80, 100),
        )
        profile = dataclasses.replace(make_strategy_profile(channel), budget=budget)
        with database.transaction() as connection:
            ChannelRepository(connection).add(channel)
            StrategyProfileRepository(connection).add(profile)

        sink = InMemoryAuditSink()
        audit = AuditLog(sink, clock=lambda: NOW)
        ledger = CostLedger(database, audit, clock=lambda: NOW)
        guard = BudgetGuard(database, audit, clock=lambda: NOW)

        # Spend 20.00 earlier this month + 5.00 today -> daily=5.00 (50%),
        # monthly=25.00 (50%)
        with database.transaction() as connection:
            CostRecordRepository(connection).add(
                CostRecord.create(
                    channel.id,
                    CostCategory.RENDER,
                    provider="ffmpeg",
                    amount=Decimal("20.00"),
                    currency="USD",
                    clock=lambda: EARLIER_THIS_MONTH,
                )
            )
        ledger.record(
            channel.id,
            CostCategory.LLM,
            provider="mock_llm",
            amount=Decimal("5.00"),
            currency="USD",
            ref="today:1",
        )

        # Projecting +3.00 -> projected daily=8.00 (80%), projected monthly=28.00 (56%)
        status = guard.enforce(channel.id, estimated_additional_cost=Decimal("3.00"))
        assert status.allowed is True
        assert status.daily_spend == Decimal("5.000000")
        assert status.monthly_spend == Decimal("25.000000")
        assert status.projected_daily_spend == Decimal("8.000000")
        assert status.projected_monthly_spend == Decimal("28.000000")
        assert status.daily_remaining == Decimal("2.000000")
        assert status.monthly_remaining == Decimal("22.000000")

        # Triggered alerts: daily 50% and 80%, monthly 50%
        alert_pairs = [(a.window, a.threshold_percent) for a in status.alerts]
        assert alert_pairs == [
            (BudgetWindowKind.DAILY, 50),
            (BudgetWindowKind.DAILY, 80),
            (BudgetWindowKind.MONTHLY, 50),
        ]
        assert sink.entries[-1].action == BUDGET_ALERT_TRIGGERED

        # Projecting +6.00 -> projected daily=11.00 > 10.00 -> blocked with
        # budget.projected_daily_exceeded
        with pytest.raises(BudgetGuardBlockedError) as exc_proj:
            guard.enforce(channel.id, estimated_additional_cost=Decimal("6.00"))
        assert [r.code for r in exc_proj.value.status.blocking_reasons] == [
            "budget.projected_daily_exceeded"
        ]
        assert sink.entries[-1].action == BUDGET_GUARD_BLOCKED
        assert sink.entries[-1].result is AuditResult.DENIED

    def test_blocks_when_actual_daily_or_monthly_spend_reaches_limit(
        self, database: Database
    ) -> None:
        channel = make_channel(status=ChannelStatus.ACTIVE)
        budget = Budget(
            currency="USD",
            daily_limit=Decimal("10.00"),
            monthly_limit=Decimal("10.00"),
            alert_thresholds=(50, 100),
        )
        profile = dataclasses.replace(make_strategy_profile(channel), budget=budget)
        with database.transaction() as connection:
            ChannelRepository(connection).add(channel)
            StrategyProfileRepository(connection).add(profile)

        audit = AuditLog(InMemoryAuditSink(), clock=lambda: NOW)
        ledger = CostLedger(database, audit, clock=lambda: NOW)
        guard = BudgetGuard(database, audit, clock=lambda: NOW)

        ledger.record(
            channel.id,
            CostCategory.TTS,
            provider="mock_tts",
            amount=Decimal("10.00"),
            currency="USD",
            ref="tts:full",
        )
        with pytest.raises(BudgetGuardBlockedError) as exc_info:
            guard.enforce(channel.id)
        assert [r.code for r in exc_info.value.status.blocking_reasons] == [
            "budget.daily_exceeded",
            "budget.monthly_exceeded",
        ]

    def test_refuses_missing_budget_and_cross_currency_costs(
        self, database: Database
    ) -> None:
        ch_no_budget = make_channel(status=ChannelStatus.ACTIVE)
        prof_no_budget = dataclasses.replace(
            make_strategy_profile(ch_no_budget), budget=None
        )
        ch_mismatch = make_channel(status=ChannelStatus.ACTIVE)
        prof_mismatch = make_strategy_profile(ch_mismatch)
        with database.transaction() as connection:
            ChannelRepository(connection).add(ch_no_budget)
            StrategyProfileRepository(connection).add(prof_no_budget)
            ChannelRepository(connection).add(ch_mismatch)
            StrategyProfileRepository(connection).add(prof_mismatch)
            CostRecordRepository(connection).add(
                CostRecord.create(
                    ch_mismatch.id,
                    CostCategory.API,
                    provider="ext_api",
                    amount=Decimal("1.00"),
                    currency="EUR",
                    clock=lambda: NOW - timedelta(hours=1),
                )
            )

        guard = BudgetGuard(database, AuditLog(InMemoryAuditSink()), clock=lambda: NOW)
        with pytest.raises(BudgetGuardError) as exc_nb:
            guard.evaluate(ch_no_budget.id)
        assert exc_nb.value.guard_code is BudgetGuardErrorCode.NO_BUDGET

        with pytest.raises(BudgetGuardError) as exc_curr:
            guard.evaluate(ch_mismatch.id)
        assert exc_curr.value.guard_code is BudgetGuardErrorCode.CURRENCY_MISMATCH
