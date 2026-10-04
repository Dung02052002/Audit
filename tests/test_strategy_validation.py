"""D-053 Strategy Validation (Prompt Pack v8, prompt #053).

Rules the user approved on 2026-10-02:

- a run is a move into generating; ``StrategyGate`` (``GateName.STRATEGY``)
  blocks it on blocking findings, and ``GET /channels/{id}/strategy/validation``
  shows every finding beforehand;
- missing configuration and conflicts that make a run impossible block;
  everything else is a warning and never blocks;
- blocking: no strategy, each unset setting, the item's content type switched
  off, a daily limit of 0 for the item's type, a daily or monthly budget of 0;
- warnings: primary language region differs from the market, monetization
  and budget currencies differ, a limit above 0 for a switched-off type, every
  limit 0, and an item created from an older strategy version.
"""

import dataclasses
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ai_youtube_agent.bootstrap import build_container
from ai_youtube_agent.content.strategy import (
    SETTING_TYPES,
    Budget,
    Cadence,
    LanguageSettings,
    Market,
    Monetization,
    RevenueGoal,
    RevenueSource,
    StrategyProfile,
)
from ai_youtube_agent.content.strategy_validation import (
    FindingSeverity,
    validate_strategy,
)
from ai_youtube_agent.core.audit import Actor, ActorKind
from ai_youtube_agent.core.config import Environment, Settings
from ai_youtube_agent.core.content_item import ContentItem, ContentStatus, ContentType
from ai_youtube_agent.core.db.database import Database
from ai_youtube_agent.core.db.repositories.channel import (
    ChannelRepository,
    StrategyProfileRepository,
)
from ai_youtube_agent.core.flags import FeatureFlags
from ai_youtube_agent.core.gates import (
    GateContext,
    GateName,
    GateOutcome,
    PipelineGate,
)
from ai_youtube_agent.core.strategy_gate import StrategyGate
from ai_youtube_agent.main import create_app
from factories import make_channel, make_content_item, make_strategy_profile

T0 = datetime(2026, 10, 2, 12, 0, tzinfo=UTC)
USER = Actor(ActorKind.USER, "owner")
SYSTEM = Actor(ActorKind.SYSTEM, "pipeline")
FLAGS = FeatureFlags()  # Shorts on, LongForm off
BOTH = FeatureFlags(longform_enabled=True)
SHORTS, LONGFORM = ContentType.SHORTS, ContentType.LONGFORM
D = Decimal


def full(**changes) -> StrategyProfile:
    """The factory profile: complete, valid, Shorts 2 a day, LongForm 0."""
    return dataclasses.replace(make_strategy_profile(make_channel()), **changes)


def codes(validation) -> list[str]:
    return [f.code for f in validation.findings]


# Validator: missing configuration


def test_a_complete_compatible_strategy_has_no_findings() -> None:
    validation = validate_strategy(full(), FLAGS, content_type=SHORTS)

    assert validation.findings == ()
    assert validation.is_valid


def test_no_strategy_blocks() -> None:
    validation = validate_strategy(None, FLAGS, content_type=SHORTS)

    assert codes(validation) == ["strategy.missing"]
    assert not validation.is_valid


def test_each_missing_setting_blocks_in_setting_order() -> None:
    profile = StrategyProfile.create("channel-1", market=Market("VN"), actor=USER)

    validation = validate_strategy(profile, FLAGS)

    missing = [f for f in validation.findings if f.code == "strategy.missing_setting"]
    assert [f.setting for f in missing] == [n for n in SETTING_TYPES if n != "market"]
    assert all(f.severity is FindingSeverity.BLOCKING for f in missing)
    assert "cadence" in missing[5].message


# Validator: blocking conflicts


def test_a_switched_off_content_type_blocks() -> None:
    validation = validate_strategy(
        full(cadence=Cadence(2, 1)), FLAGS, content_type=LONGFORM
    )

    assert [f.code for f in validation.blocking] == ["strategy.content_type_disabled"]
    assert "LongForm" in validation.blocking[0].message


def test_a_zero_limit_for_the_items_type_blocks() -> None:
    validation = validate_strategy(full(), BOTH, content_type=LONGFORM)

    assert [f.code for f in validation.blocking] == ["strategy.no_daily_limit"]
    assert validation.blocking[0].setting == "cadence"


@pytest.mark.parametrize(("daily", "monthly"), [("0", "100"), ("0", "0")])
def test_a_zero_budget_blocks(daily: str, monthly: str) -> None:
    budget = Budget("USD", D(daily), D(monthly))

    validation = validate_strategy(full(budget=budget), FLAGS)

    assert [f.code for f in validation.blocking] == ["strategy.zero_budget"]


def test_without_a_content_type_the_per_type_rules_are_skipped() -> None:
    validation = validate_strategy(full(cadence=Cadence(2, 1)), FLAGS)

    assert validation.is_valid
    assert codes(validation) == ["strategy.limit_for_disabled_type"]


# Validator: warnings


@pytest.mark.parametrize(
    ("primary", "market", "warned"),
    [
        ("en-GB", "US", True),
        ("vi-VN", "VN", False),
        ("vi", "US", False),
        ("zh-Hant-TW", "TW", False),
        ("zh-Hant-HK", "TW", True),
    ],
)
def test_language_region_versus_market(primary, market, warned) -> None:
    profile = full(languages=LanguageSettings(primary), market=Market(market))

    validation = validate_strategy(profile, FLAGS, content_type=SHORTS)

    assert validation.is_valid
    expected = ["strategy.language_region_mismatch"] if warned else []
    assert codes(validation) == expected


def test_a_monetization_currency_other_than_the_budget_warns() -> None:
    money = Monetization((RevenueGoal(RevenueSource.ADS, D("10")),), "EUR")

    validation = validate_strategy(full(monetization=money), FLAGS)

    assert validation.is_valid
    assert codes(validation) == ["strategy.currency_mismatch"]
    assert "EUR" in validation.warnings[0].message


def test_goals_without_a_currency_do_not_warn() -> None:
    money = Monetization((RevenueGoal(RevenueSource.ADS),))

    assert validate_strategy(full(monetization=money), FLAGS).findings == ()


def test_a_limit_for_a_switched_off_type_warns() -> None:
    off = FeatureFlags(shorts_enabled=False, longform_enabled=False)

    validation = validate_strategy(full(cadence=Cadence(1, 1)), off)

    assert codes(validation) == ["strategy.limit_for_disabled_type"] * 2


def test_all_limits_zero_warns_and_blocks_the_items_type() -> None:
    validation = validate_strategy(
        full(cadence=Cadence(0, 0)), FLAGS, content_type=SHORTS
    )

    assert codes(validation) == ["strategy.no_daily_limit", "strategy.nothing_can_run"]
    assert [f.severity for f in validation.findings] == [
        FindingSeverity.BLOCKING,
        FindingSeverity.WARNING,
    ]


def test_an_item_from_an_older_strategy_version_warns() -> None:
    profile = full(version=3)

    validation = validate_strategy(
        profile, FLAGS, content_type=SHORTS, item_strategy_version=2
    )

    assert validation.is_valid
    assert codes(validation) == ["strategy.version_changed"]
    assert (
        validate_strategy(
            profile, FLAGS, content_type=SHORTS, item_strategy_version=3
        ).findings
        == ()
    )


def test_findings_put_missing_configuration_first() -> None:
    profile = dataclasses.replace(
        full(cadence=Cadence(0, 0), languages=LanguageSettings("en-GB")),
        budget=None,
    )

    validation = validate_strategy(
        profile, FLAGS, content_type=SHORTS, item_strategy_version=0
    )

    assert codes(validation) == [
        "strategy.missing_setting",
        "strategy.no_daily_limit",
        "strategy.language_region_mismatch",
        "strategy.nothing_can_run",
        "strategy.version_changed",
    ]


def test_finding_as_dict() -> None:
    finding = validate_strategy(None, FLAGS).findings[0]

    assert finding.as_dict() == {
        "code": "strategy.missing",
        "severity": "blocking",
        "message": "This channel has no strategy yet.",
        "setting": None,
    }


# Gate


class Strategies:
    def __init__(self, profile: StrategyProfile | None) -> None:
        self.profile = profile

    def get_by_channel(self, channel_id: str) -> StrategyProfile | None:
        return self.profile


def item(content_type=SHORTS, status=ContentStatus.DRAFT, version=1) -> ContentItem:
    return dataclasses.replace(
        ContentItem.create("channel-1", "strategy-1", version, content_type, "Video"),
        status=status,
    )


def run(gate: StrategyGate, item: ContentItem, target=ContentStatus.GENERATING):
    return gate.evaluate(GateContext(item, target, SYSTEM, T0))


def test_the_gate_is_a_pipeline_gate_named_strategy() -> None:
    gate = StrategyGate(Strategies(full()), FLAGS)

    assert isinstance(gate, PipelineGate)
    assert gate.name is GateName.STRATEGY


@pytest.mark.parametrize(
    "status",
    # A production (draft) and regenerations (C-032: testing..approved).
    [ContentStatus.DRAFT, ContentStatus.TESTING, ContentStatus.APPROVED],
)
def test_the_gate_blocks_every_run_on_blocking_findings(status) -> None:
    gate = StrategyGate(Strategies(None), FLAGS)

    result = run(gate, item(status=status))

    assert result.outcome is GateOutcome.BLOCK
    assert [r.code for r in result.reasons] == ["strategy.missing"]


def test_the_gate_reports_one_reason_per_blocking_finding() -> None:
    profile = full(budget=Budget("USD", D("0"), D("0")), cadence=Cadence(2, 1))
    gate = StrategyGate(Strategies(profile), FLAGS)

    result = run(gate, item(LONGFORM))

    assert [r.code for r in result.reasons] == [
        "strategy.content_type_disabled",
        "strategy.zero_budget",
    ]


def test_warnings_do_not_block() -> None:
    profile = full(languages=LanguageSettings("en-GB"), version=5)
    gate = StrategyGate(Strategies(profile), FLAGS)

    assert run(gate, item(version=1)).is_passed


def test_other_moves_pass_without_reading_the_strategy() -> None:
    class Unreadable:
        def get_by_channel(self, channel_id: str):
            raise AssertionError("not read")

    gate = StrategyGate(Unreadable(), FLAGS)

    assert run(gate, item(status=ContentStatus.APPROVED), ContentStatus.PUBLISHING)


def test_the_gate_reads_the_stored_strategy(database: Database) -> None:
    channel = make_channel()
    profile = make_strategy_profile(channel, cadence=Cadence(0, 0))
    with database.transaction() as connection:
        ChannelRepository(connection).add(channel)
        StrategyProfileRepository(connection).add(profile)
        gate = StrategyGate(StrategyProfileRepository(connection), FLAGS)
        result = run(gate, make_content_item(channel, profile))

    assert [r.code for r in result.reasons] == ["strategy.no_daily_limit"]


# HTTP API


@pytest.fixture
def client(tmp_path: Path, database_copy):
    path = database_copy(tmp_path / "a.db")
    settings = Settings(environment=Environment.TEST, database_path=path)
    with TestClient(create_app(build_container(settings))) as test_client:
        yield test_client


def strategy_url(client: TestClient) -> str:
    response = client.post(
        "/channels", json={"title": "Money", "youtube_channel_id": "UC" + "v" * 22}
    )
    return f"/channels/{response.json()['id']}/strategy"


def test_validation_without_a_strategy(client: TestClient) -> None:
    url = strategy_url(client)

    response = client.get(f"{url}/validation")

    assert response.status_code == 200
    assert response.json() == {
        "channel_id": url.split("/")[2],
        "strategy_version": None,
        "content_type": None,
        "is_valid": False,
        "findings": [
            {
                "code": "strategy.missing",
                "severity": "blocking",
                "message": "This channel has no strategy yet.",
                "setting": None,
            }
        ],
    }


def test_validation_lists_missing_settings_and_warnings(client: TestClient) -> None:
    url = strategy_url(client)
    client.put(f"{url}/market", json={"country": "US"})
    client.put(f"{url}/languages", json={"primary": "en-GB", "expected_version": 1})

    body = client.get(f"{url}/validation", params={"content_type": "longform"}).json()

    assert body["strategy_version"] == 2
    assert body["content_type"] == "longform"
    assert body["is_valid"] is False
    assert [f["code"] for f in body["findings"]] == [
        *["strategy.missing_setting"] * (len(SETTING_TYPES) - 2),
        "strategy.content_type_disabled",
        "strategy.language_region_mismatch",
    ]
    assert body["findings"][-1]["severity"] == "warning"


def test_validation_of_an_unknown_channel_is_404(client: TestClient) -> None:
    response = client.get("/channels/nope/strategy/validation")

    assert response.status_code == 404


def test_validation_refuses_an_unknown_content_type(client: TestClient) -> None:
    response = client.get(
        f"{strategy_url(client)}/validation", params={"content_type": "reels"}
    )

    assert response.status_code == 422
    fields = [f["field"] for f in response.json()["error"]["fields"]]
    assert any("content_type" in field for field in fields)
