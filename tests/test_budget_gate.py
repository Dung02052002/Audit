"""C-037 Budget Gate (Prompt Pack v8, prompt #037).

Rules the user approved on 2026-09-30:

- every move into generating (a new production or a regeneration) is a
  cost-incurring job and is checked; other moves pass;
- the budget is the channel's ``StrategyProfile.budget``: a daily and a monthly
  limit in one currency; actual spend comes from ``CostRecord``s;
- the budget is exceeded when spend >= limit;
- a cost in another currency blocks (``budget.currency_mismatch``);
- a day is a UTC day and a month a UTC calendar month.
"""

import dataclasses
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from ai_youtube_agent.content.strategy import Budget
from ai_youtube_agent.core.audit import Actor, ActorKind
from ai_youtube_agent.core.budget_gate import BudgetGate, month_window
from ai_youtube_agent.core.content_item import ContentItem, ContentStatus
from ai_youtube_agent.core.db.database import Database
from ai_youtube_agent.core.db.repositories.channel import (
    ChannelRepository,
    StrategyProfileRepository,
)
from ai_youtube_agent.core.db.repositories.economics import CostRecordRepository
from ai_youtube_agent.core.gates import (
    GateContext,
    GateName,
    GateOutcome,
    PipelineGate,
    evaluate_gates,
)
from factories import (
    make_channel,
    make_content_item,
    make_cost_record,
    make_strategy_profile,
)

MONTH = datetime(2026, 9, 1, tzinfo=UTC)
DAY = datetime(2026, 9, 30, tzinfo=UTC)
NOON = DAY + timedelta(hours=12)
SYSTEM = Actor(ActorKind.SYSTEM, "pipeline")
D = Decimal


class Graph:
    def __init__(
        self,
        database: Database,
        daily: str = "5.00",
        monthly: str = "100.00",
        currency: str = "USD",
        strategy: bool = True,
    ) -> None:
        self.database = database
        self.channel = make_channel()
        profile = make_strategy_profile(self.channel)
        self.profile = dataclasses.replace(
            profile, budget=Budget(currency, D(daily), D(monthly))
        )
        with database.transaction() as connection:
            ChannelRepository(connection).add(self.channel)
            if strategy:
                StrategyProfileRepository(connection).add(self.profile)

    def item(self, status: ContentStatus = ContentStatus.DRAFT) -> ContentItem:
        return make_content_item(self.channel, self.profile, status=status)

    def spent(self, amount: str, when: datetime, currency: str = "USD") -> None:
        record = make_cost_record(
            self.channel, amount=D(amount), currency=currency, incurred_at=when
        )
        with self.database.transaction() as connection:
            CostRecordRepository(connection).add(record)

    def evaluate(
        self,
        item: ContentItem | None = None,
        target: ContentStatus = ContentStatus.GENERATING,
        when: datetime = NOON,
    ):
        item = item or self.item()
        with self.database.transaction() as connection:
            gate = BudgetGate(
                StrategyProfileRepository(connection), CostRecordRepository(connection)
            )
            return gate.evaluate(GateContext(item, target, SYSTEM, when))


def codes(result) -> list[str]:
    return [r.code for r in result.reasons]


# The month


@pytest.mark.parametrize(
    ("moment", "start", "end"),
    [
        (NOON, MONTH, datetime(2026, 10, 1, tzinfo=UTC)),
        (MONTH, MONTH, datetime(2026, 10, 1, tzinfo=UTC)),
        (
            datetime(2026, 12, 31, 23, 59, 59, 999999, tzinfo=UTC),
            datetime(2026, 12, 1, tzinfo=UTC),
            datetime(2027, 1, 1, tzinfo=UTC),
        ),
        (
            datetime(2028, 2, 29, 12, tzinfo=UTC),
            datetime(2028, 2, 1, tzinfo=UTC),
            datetime(2028, 3, 1, tzinfo=UTC),
        ),
    ],
)
def test_a_month_is_a_utc_calendar_month(moment, start, end) -> None:
    assert month_window(moment) == (start, end)


def test_the_month_of_a_non_utc_time_is_refused() -> None:
    with pytest.raises(ValueError):
        month_window(NOON.astimezone(timezone(timedelta(hours=7))))


# Contract and scope


def test_the_gate_follows_the_contract(database: Database) -> None:
    with database.transaction() as connection:
        gate = BudgetGate(
            StrategyProfileRepository(connection), CostRecordRepository(connection)
        )
    assert isinstance(gate, PipelineGate)
    assert gate.name is GateName.BUDGET


@pytest.mark.parametrize(
    "status",
    [
        ContentStatus.DRAFT,
        ContentStatus.TESTING,
        ContentStatus.PREVIEW_READY,
        ContentStatus.AWAITING_APPROVAL,
        ContentStatus.APPROVED,
    ],
)
def test_every_move_into_generating_is_checked(database: Database, status) -> None:
    graph = Graph(database, daily="1.00")
    graph.spent("1.00", NOON - timedelta(hours=1))

    result = graph.evaluate(graph.item(status))

    assert codes(result) == ["budget.daily_exceeded"]


@pytest.mark.parametrize(
    ("status", "target"),
    [
        (ContentStatus.DRAFT, ContentStatus.FAILED),
        (ContentStatus.GENERATING, ContentStatus.TESTING),
        (ContentStatus.APPROVED, ContentStatus.PUBLISHING),
        (ContentStatus.PUBLISHING, ContentStatus.PUBLISHED),
        (ContentStatus.FAILED, ContentStatus.DRAFT),
    ],
)
def test_other_moves_are_not_this_gates_concern(
    database: Database, status, target
) -> None:
    graph = Graph(database, daily="0", monthly="0", strategy=False)
    assert graph.evaluate(graph.item(status), target).is_passed


# Daily and monthly limits


def test_passes_under_both_limits(database: Database) -> None:
    graph = Graph(database, daily="5.00", monthly="100.00")
    graph.spent("4.99", NOON - timedelta(hours=1))

    result = graph.evaluate()

    assert result.outcome is GateOutcome.PASS
    assert result.gate is GateName.BUDGET


def test_blocks_when_todays_spend_reaches_the_daily_limit(database: Database) -> None:
    graph = Graph(database, daily="5.00")
    graph.spent("2.50", DAY)
    graph.spent("2.50", NOON - timedelta(hours=1))

    result = graph.evaluate()

    assert codes(result) == ["budget.daily_exceeded"]
    assert "5.00 USD" in result.reasons[0].message


def test_spend_over_the_daily_limit_also_blocks(database: Database) -> None:
    graph = Graph(database, daily="5.00")
    graph.spent("7.00", NOON - timedelta(hours=1))
    assert codes(graph.evaluate()) == ["budget.daily_exceeded"]


def test_yesterdays_spend_only_counts_for_the_month(database: Database) -> None:
    graph = Graph(database, daily="5.00", monthly="100.00")
    graph.spent("50.00", DAY - timedelta(microseconds=1))

    assert graph.evaluate().is_passed


def test_blocks_when_the_months_spend_reaches_the_monthly_limit(
    database: Database,
) -> None:
    graph = Graph(database, daily="5.00", monthly="20.00")
    graph.spent("10.00", MONTH)
    graph.spent("10.00", DAY - timedelta(days=1))

    result = graph.evaluate()

    assert codes(result) == ["budget.monthly_exceeded"]
    assert "20.00 USD" in result.reasons[0].message


def test_last_months_spend_does_not_count(database: Database) -> None:
    graph = Graph(database, daily="5.00", monthly="20.00")
    graph.spent("100.00", MONTH - timedelta(microseconds=1))
    graph.spent("100.00", datetime(2026, 10, 1, tzinfo=UTC))  # next month

    assert graph.evaluate().is_passed


def test_both_limits_give_two_reasons(database: Database) -> None:
    graph = Graph(database, daily="5.00", monthly="5.00")
    graph.spent("5.00", NOON - timedelta(hours=1))

    assert codes(graph.evaluate()) == [
        "budget.daily_exceeded",
        "budget.monthly_exceeded",
    ]


def test_a_zero_budget_blocks_before_any_spend(database: Database) -> None:
    graph = Graph(database, daily="0", monthly="0")
    assert codes(graph.evaluate()) == [
        "budget.daily_exceeded",
        "budget.monthly_exceeded",
    ]


def test_other_channels_spend_does_not_count(database: Database) -> None:
    graph, other = Graph(database, daily="1.00"), Graph(database, daily="1.00")
    other.spent("9.00", NOON - timedelta(hours=1))

    assert graph.evaluate().is_passed


def test_decimal_spend_is_summed_exactly(database: Database) -> None:
    graph = Graph(database, daily="0.3", monthly="100")
    graph.spent("0.1", NOON - timedelta(hours=3))
    graph.spent("0.1", NOON - timedelta(hours=2))
    graph.spent("0.1", NOON - timedelta(hours=1))

    # With floats 0.1 + 0.1 + 0.1 < 0.3; with Decimal it reaches the limit.
    assert codes(graph.evaluate()) == ["budget.daily_exceeded"]


# Fail closed


def test_a_cost_in_another_currency_blocks(database: Database) -> None:
    graph = Graph(database, currency="USD")
    graph.spent("0.01", NOON - timedelta(hours=1), currency="EUR")

    result = graph.evaluate()

    assert codes(result) == ["budget.currency_mismatch"]
    assert "EUR" in result.reasons[0].message
    assert "USD" in result.reasons[0].message


def test_another_currency_outside_the_month_is_ignored(database: Database) -> None:
    graph = Graph(database, currency="USD")
    graph.spent("0.01", MONTH - timedelta(days=1), currency="EUR")

    assert graph.evaluate().is_passed


def test_a_channel_without_strategy_is_blocked(database: Database) -> None:
    graph = Graph(database, strategy=False)
    assert codes(graph.evaluate()) == ["budget.no_strategy"]


def test_a_broken_source_blocks_through_evaluate_gates(database: Database) -> None:
    class Broken:
        def list_by_channel(self, channel_id, *, start=None, end=None):
            raise RuntimeError("database is locked")

    graph = Graph(database)
    item = graph.item()
    with database.transaction() as connection:
        gate = BudgetGate(StrategyProfileRepository(connection), Broken())
        report = evaluate_gates(
            [gate], GateContext(item, ContentStatus.GENERATING, SYSTEM, NOON)
        )
    assert [r.code for r in report.reasons] == ["gate.error"]
