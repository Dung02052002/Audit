"""D-051 Budget Settings (Prompt Pack v8, prompt #051).

Rules the user approved on 2026-10-02:

- daily and monthly limits are ``Decimal`` amounts from 0 to 1,000,000 with
  at most 2 decimal places, daily <= monthly, in one ISO 4217 currency;
- ``alert_thresholds``: 1 to 5 ascending whole percentages (1 to 100) shared by
  both limits, 50, 80 and 100 by default; this task only configures them, the
  budget guard (#180) raises alerts and #202 shows them;
- ``BudgetGate`` counts the day and month in the cadence time zone (tests in
  ``test_budget_gate.py``);
- the currency may change; costs in the old currency this month make the gate
  block (``budget.currency_mismatch``) until the month ends;
- the thresholds are stored in ``budget_alert_thresholds_json`` (migration
  0006), and a budget stored before #051 reads with the defaults;
- ``PUT /channels/{id}/strategy/budget`` replaces the whole budget and follows
  the market rules.
"""

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ai_youtube_agent.bootstrap import build_container
from ai_youtube_agent.content.strategy import (
    DEFAULT_ALERT_THRESHOLDS,
    SETTING_TYPES,
    Budget,
)
from ai_youtube_agent.content.strategy_settings import StrategySettings
from ai_youtube_agent.core.audit import (
    Actor,
    ActorKind,
    AuditLog,
    AuditSink,
    InMemoryAuditSink,
)
from ai_youtube_agent.core.budget_gate import BudgetGate
from ai_youtube_agent.core.config import Environment, Settings
from ai_youtube_agent.core.content_item import ContentStatus
from ai_youtube_agent.core.db.database import Database
from ai_youtube_agent.core.db.migrate import default_migrations, migrate
from ai_youtube_agent.core.db.repositories.channel import (
    ChannelRepository,
    StrategyProfileRepository,
    _strategy_row,
)
from ai_youtube_agent.core.db.repositories.economics import CostRecordRepository
from ai_youtube_agent.core.gates import GateContext
from ai_youtube_agent.main import create_app
from factories import (
    make_channel,
    make_content_item,
    make_cost_record,
    make_strategy_profile,
)

T0 = datetime(2026, 10, 2, 12, 0, tzinfo=UTC)
USER = Actor(ActorKind.USER, "owner")
SYSTEM = Actor(ActorKind.SYSTEM, "pipeline")
D = Decimal
FULL = Budget("VND", D("150000"), D("1000000.00"), (25, 75, 90, 100))


# Entity


def test_default_thresholds_are_50_80_100() -> None:
    assert DEFAULT_ALERT_THRESHOLDS == (50, 80, 100)
    assert Budget("USD", D("5"), D("100")).alert_thresholds == (50, 80, 100)


@pytest.mark.parametrize(
    "build",
    [
        lambda: Budget("USD", D("0.001"), D("10")),
        lambda: Budget("USD", D("1"), D("10.123")),
        lambda: Budget("USD", D("1"), D("1000000.01")),
        lambda: Budget("USD", D("1000001"), D("1000001")),
        lambda: Budget("USD", D("-0.01"), D("10")),
        lambda: Budget("USD", D("1"), D("10"), ()),
        lambda: Budget("USD", D("1"), D("10"), (10, 20, 30, 40, 50, 60)),
        lambda: Budget("USD", D("1"), D("10"), (0,)),
        lambda: Budget("USD", D("1"), D("10"), (101,)),
        lambda: Budget("USD", D("1"), D("10"), (80, 50)),
        lambda: Budget("USD", D("1"), D("10"), (50, 50)),
        lambda: Budget("USD", D("1"), D("10"), (True,)),
        lambda: Budget("usd", D("1"), D("10")),
    ],
)
def test_budget_limits(build) -> None:
    with pytest.raises(ValueError):
        build()


def test_thresholds_must_be_a_tuple() -> None:
    with pytest.raises(TypeError):
        Budget("USD", D("1"), D("10"), [50])  # type: ignore[arg-type]


def test_limits_at_the_edge_are_allowed() -> None:
    budget = Budget("USD", D("0.01"), D("1000000.00"), (1, 2, 3, 4, 100))
    assert budget.monthly_limit == D("1000000")
    assert Budget("USD", D("1E+2"), D("1E+6")).daily_limit == 100


def test_budget_stays_a_required_setting() -> None:
    assert "budget" in SETTING_TYPES


# Persistence


def test_a_full_budget_round_trips(database: Database) -> None:
    channel = make_channel()
    profile = make_strategy_profile(channel, budget=FULL)
    with database.transaction() as connection:
        ChannelRepository(connection).add(channel)
        StrategyProfileRepository(connection).add(profile)

    with database.transaction() as connection:
        assert StrategyProfileRepository(connection).get(profile.id).budget == FULL
        row = connection.execute(
            "SELECT budget_alert_thresholds_json FROM strategy_profiles"
        ).fetchone()
    assert json.loads(row[0]) == [25, 75, 90, 100]


def test_migration_0006_gives_old_budgets_the_default_thresholds(
    tmp_path: Path,
) -> None:
    path = tmp_path / "app.db"
    migrate(path, migrations=default_migrations()[:5])
    database = Database(path)
    channel = make_channel()
    profile = make_strategy_profile(channel)
    row = _strategy_row(profile)
    del row["budget_alert_thresholds_json"]
    with database.transaction() as connection:
        ChannelRepository(connection).add(channel)
        StrategyProfileRepository(connection)._insert("strategy_profiles", row)

    report = migrate(path)

    assert report.applied == tuple(range(6, len(default_migrations()) + 1))
    with database.transaction() as connection:
        stored = StrategyProfileRepository(connection).get(profile.id)
    assert stored == profile
    assert stored.budget.alert_thresholds == (50, 80, 100)


def test_there_are_no_thresholds_without_a_budget(database: Database) -> None:
    channel = make_channel()
    profile = make_strategy_profile(channel, budget=None)
    with database.transaction() as connection:
        ChannelRepository(connection).add(channel)
        StrategyProfileRepository(connection).add(profile)

    with pytest.raises(Exception, match="CHECK"), database.transaction() as conn:
        conn.execute("UPDATE strategy_profiles SET budget_alert_thresholds_json = '[]'")


# Service


class Clock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> datetime:
        self.now += timedelta(seconds=1)
        return self.now


def stored_profile(database: Database):
    channel = make_channel()
    profile = make_strategy_profile(channel)
    with database.transaction() as connection:
        ChannelRepository(connection).add(channel)
        StrategyProfileRepository(connection).add(profile)
    return channel, profile


def test_setting_the_budget_touches_nothing_else(database: Database) -> None:
    sink = InMemoryAuditSink()
    service = StrategySettings(database, AuditLog(sink), clock=Clock())
    channel, full = stored_profile(database)

    change = service.set_budget(
        channel.id, FULL, expected_version=full.version, actor=USER
    )

    stored = service.get(channel.id)
    assert stored == change.profile
    assert stored.budget == FULL
    for name in SETTING_TYPES:
        if name != "budget":
            assert getattr(stored, name) == getattr(full, name)
    [event] = sink.events()
    assert event.action == "strategy.budget_changed"
    assert json.loads(event.metadata["to"])["currency"] == "VND"
    assert json.loads(event.metadata["to"])["alert_thresholds"] == [25, 75, 90, 100]
    assert json.loads(event.metadata["from"])["daily_limit"] == "5.00"


def test_a_currency_change_is_saved_and_old_costs_block(database: Database) -> None:
    service = StrategySettings(database, AuditLog(InMemoryAuditSink()), clock=Clock())
    channel, full = stored_profile(database)
    with database.transaction() as connection:
        CostRecordRepository(connection).add(
            make_cost_record(
                channel, amount=D("1"), currency="USD", incurred_at=T0 - timedelta(1)
            )
        )

    change = service.set_budget(
        channel.id,
        Budget("EUR", D("5"), D("100")),
        expected_version=full.version,
        actor=USER,
    )

    assert change.profile.budget.currency == "EUR"
    item = make_content_item(channel, change.profile)
    with database.transaction() as connection:
        gate = BudgetGate(
            StrategyProfileRepository(connection), CostRecordRepository(connection)
        )
        result = gate.evaluate(GateContext(item, ContentStatus.GENERATING, SYSTEM, T0))
    assert [r.code for r in result.reasons] == ["budget.currency_mismatch"]


# HTTP API


@pytest.fixture
def client(tmp_path: Path):
    settings = Settings(environment=Environment.TEST, database_path=tmp_path / "a.db")
    container = build_container(settings)
    with TestClient(create_app(container)) as test_client:
        test_client.sink = container.resolve(AuditSink)
        yield test_client


def budget_url(client: TestClient) -> str:
    response = client.post(
        "/channels", json={"title": "Money", "youtube_channel_id": "UC" + "d" * 22}
    )
    return f"/channels/{response.json()['id']}/strategy/budget"


def test_put_budget_creates_and_replaces(client: TestClient) -> None:
    url = budget_url(client)

    created = client.put(
        url, json={"currency": " usd ", "daily_limit": 5, "monthly_limit": "100.5"}
    )
    replaced = client.put(
        url,
        json={
            "currency": "VND",
            "daily_limit": "150000",
            "monthly_limit": 1000000,
            "alert_thresholds": [90, 25, 100],
            "expected_version": 1,
        },
    )

    assert created.status_code == 201
    assert created.json()["budget"] == {
        "currency": "USD",
        "daily_limit": "5.00",
        "monthly_limit": "100.50",
        "alert_thresholds": [50, 80, 100],
    }
    assert "budget" not in created.json()["missing_settings"]
    assert replaced.status_code == 200
    assert replaced.json()["budget"] == {
        "currency": "VND",
        "daily_limit": "150000.00",
        "monthly_limit": "1000000.00",
        "alert_thresholds": [25, 90, 100],
    }
    assert [e.action for e in client.sink.events()][-1] == "strategy.budget_changed"


LIMITS = {"currency": "USD", "daily_limit": "5", "monthly_limit": "100"}


@pytest.mark.parametrize(
    "body, field",
    [
        ({"daily_limit": "5", "monthly_limit": "100"}, "currency"),
        ({**LIMITS, "currency": "US"}, "currency"),
        ({**LIMITS, "currency": "US1"}, "currency"),
        ({"currency": "USD", "monthly_limit": "100"}, "daily_limit"),
        ({**LIMITS, "daily_limit": "-1"}, "daily_limit"),
        ({**LIMITS, "daily_limit": "0.001"}, "daily_limit"),
        ({**LIMITS, "daily_limit": "abc"}, "daily_limit"),
        ({**LIMITS, "daily_limit": "NaN"}, "daily_limit"),
        ({**LIMITS, "monthly_limit": "Infinity"}, "monthly_limit"),
        ({**LIMITS, "monthly_limit": "1000000.01"}, "monthly_limit"),
        ({**LIMITS, "daily_limit": "200"}, "body"),
        ({**LIMITS, "alert_thresholds": []}, "alert_thresholds"),
        ({**LIMITS, "alert_thresholds": [0]}, "alert_thresholds"),
        ({**LIMITS, "alert_thresholds": [101]}, "alert_thresholds"),
        ({**LIMITS, "alert_thresholds": [50, 50]}, "alert_thresholds"),
        ({**LIMITS, "alert_thresholds": ["50"]}, "alert_thresholds"),
        ({**LIMITS, "alert_thresholds": [50.5]}, "alert_thresholds"),
        ({**LIMITS, "alert_thresholds": [1, 2, 3, 4, 5, 6]}, "alert_thresholds"),
        ({**LIMITS, "cadence": {}}, "cadence"),
    ],
)
def test_put_budget_validation(client: TestClient, body: dict, field: str) -> None:
    response = client.put(budget_url(client), json=body)

    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "validation.invalid_request"
    assert field in [f["field"].split(".")[0] for f in error["fields"]]


def test_put_budget_conflict(client: TestClient) -> None:
    url = budget_url(client)
    client.put(url, json=LIMITS)

    stale = client.put(url, json=LIMITS)

    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "domain.strategy_conflict"
