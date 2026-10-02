"""D-052 Monetization Settings (Prompt Pack v8, prompt #052).

Rules the user approved on 2026-10-02:

- revenue sources are a closed list (``RevenueSource``): ads, shorts_ads,
  memberships, super_thanks, super_chat, sponsorships, affiliate, merchandise;
- each tracked source is a ``RevenueGoal`` with an optional monthly target
  (0 to 1,000,000, at most 2 decimal places) and an optional note (at most
  200 characters); one ``currency`` is required once any target is set;
- 0 to 8 goals in the user's order, no source twice; there is no YouTube
  Partner Program field;
- targets are goals, not guaranteed outcomes: the strategy body labels the
  monetization object with a read-only ``goals_are_not_guaranteed`` and
  ``notice``;
- stored in ``monetization_json`` (no migration); rows from before #052 with
  plain source names still read;
- ``PUT /channels/{id}/strategy/monetization`` replaces the whole setting and
  follows the market rules.
"""

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ai_youtube_agent.bootstrap import build_container
from ai_youtube_agent.content.strategy import (
    MONETIZATION_NOTICE,
    SETTING_TYPES,
    Monetization,
    RevenueGoal,
    RevenueSource,
)
from ai_youtube_agent.content.strategy_settings import StrategySettings
from ai_youtube_agent.core.audit import (
    Actor,
    ActorKind,
    AuditLog,
    AuditSink,
    InMemoryAuditSink,
)
from ai_youtube_agent.core.config import Environment, Settings
from ai_youtube_agent.core.db.database import Database
from ai_youtube_agent.core.db.repositories.channel import (
    ChannelRepository,
    StrategyProfileRepository,
)
from ai_youtube_agent.main import create_app
from factories import make_channel, make_strategy_profile

T0 = datetime(2026, 10, 2, 12, 0, tzinfo=UTC)
USER = Actor(ActorKind.USER, "owner")
D = Decimal
S = RevenueSource
FULL = Monetization(
    (
        RevenueGoal(S.SHORTS_ADS, D("150.00"), "Shorts fund the channel at first"),
        RevenueGoal(S.AFFILIATE, D("75.5")),
        RevenueGoal(S.MEMBERSHIPS),
    ),
    "USD",
)


# Entity


def test_the_source_list_is_closed_and_keeps_the_old_names() -> None:
    assert [s.value for s in RevenueSource] == [
        "ads",
        "shorts_ads",
        "memberships",
        "super_thanks",
        "super_chat",
        "sponsorships",
        "affiliate",
        "merchandise",
    ]


def test_no_goals_is_a_valid_choice() -> None:
    monetization = Monetization()

    assert monetization.goals == ()
    assert monetization.currency is None
    assert monetization.tracked_sources == ()


def test_tracked_sources_keep_the_users_order() -> None:
    assert FULL.tracked_sources == (S.SHORTS_ADS, S.AFFILIATE, S.MEMBERSHIPS)


def test_every_source_may_be_tracked_once() -> None:
    monetization = Monetization(tuple(RevenueGoal(s) for s in RevenueSource))
    assert len(monetization.goals) == 8


def test_goals_without_targets_need_no_currency() -> None:
    assert Monetization((RevenueGoal(S.ADS, note="Track only"),)).currency is None


@pytest.mark.parametrize(
    "build",
    [
        lambda: Monetization((RevenueGoal(S.ADS), RevenueGoal(S.ADS))),
        lambda: Monetization((RevenueGoal(S.ADS, D("10")),)),
        lambda: Monetization((), "usd"),
        lambda: RevenueGoal(S.ADS, D("-1")),
        lambda: RevenueGoal(S.ADS, D("0.001")),
        lambda: RevenueGoal(S.ADS, D("1000000.01")),
        lambda: RevenueGoal(S.ADS, D("NaN")),
        lambda: RevenueGoal(S.ADS, 10),  # type: ignore[arg-type]
        lambda: RevenueGoal(S.ADS, note=" "),
        lambda: RevenueGoal(S.ADS, note="n" * 201),
        lambda: RevenueSource("donations"),
    ],
)
def test_monetization_limits(build) -> None:
    with pytest.raises(ValueError):
        build()


@pytest.mark.parametrize(
    "build",
    [
        lambda: RevenueGoal("ads"),  # type: ignore[arg-type]
        lambda: Monetization([RevenueGoal(S.ADS)]),  # type: ignore[arg-type]
        lambda: Monetization((S.ADS,)),  # type: ignore[arg-type]
    ],
)
def test_monetization_types_are_checked(build) -> None:
    with pytest.raises(TypeError):
        build()


def test_monetization_stays_a_required_setting() -> None:
    assert list(SETTING_TYPES)[-1] == "monetization"


# Persistence


def test_full_goals_round_trip(database: Database) -> None:
    channel = make_channel()
    profile = make_strategy_profile(channel, monetization=FULL)
    with database.transaction() as connection:
        ChannelRepository(connection).add(channel)
        StrategyProfileRepository(connection).add(profile)

    with database.transaction() as connection:
        stored = StrategyProfileRepository(connection).get(profile.id)
        raw = connection.execute(
            "SELECT monetization_json FROM strategy_profiles"
        ).fetchone()[0]
    assert stored.monetization == FULL
    assert json.loads(raw)["goals"][1]["monthly_target"] == "75.5"


def test_targets_are_stored_in_canonical_form(database: Database) -> None:
    channel = make_channel()
    goals = Monetization((RevenueGoal(S.ADS, D("1E+2")),), "USD")
    profile = make_strategy_profile(channel, monetization=goals)
    with database.transaction() as connection:
        ChannelRepository(connection).add(channel)
        StrategyProfileRepository(connection).add(profile)

    with database.transaction() as connection:
        stored = StrategyProfileRepository(connection).get(profile.id)
        raw = connection.execute(
            "SELECT monetization_json FROM strategy_profiles"
        ).fetchone()[0]
    assert stored.monetization == goals
    assert json.loads(raw)["goals"][0]["monthly_target"] == "100"


def test_monetization_stored_before_052_still_reads(database: Database) -> None:
    channel = make_channel()
    profile = make_strategy_profile(channel)
    with database.transaction() as connection:
        ChannelRepository(connection).add(channel)
        StrategyProfileRepository(connection).add(profile)
        connection.execute(
            "UPDATE strategy_profiles SET monetization_json = ?",
            (json.dumps({"tracked_sources": ["memberships", "ads"]}),),
        )

    with database.transaction() as connection:
        stored = StrategyProfileRepository(connection).get(profile.id)

    assert stored.monetization == Monetization(
        (RevenueGoal(S.MEMBERSHIPS), RevenueGoal(S.ADS))
    )


# Service


class Clock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> datetime:
        self.now += timedelta(seconds=1)
        return self.now


def test_setting_monetization_touches_nothing_else(database: Database) -> None:
    sink = InMemoryAuditSink()
    service = StrategySettings(database, AuditLog(sink), clock=Clock())
    channel = make_channel()
    full = make_strategy_profile(channel)
    with database.transaction() as connection:
        ChannelRepository(connection).add(channel)
        StrategyProfileRepository(connection).add(full)

    change = service.set_monetization(
        channel.id, FULL, expected_version=full.version, actor=USER
    )

    stored = service.get(channel.id)
    assert stored == change.profile
    assert stored.monetization == FULL
    for name in SETTING_TYPES:
        if name != "monetization":
            assert getattr(stored, name) == getattr(full, name)
    [event] = sink.events()
    assert event.action == "strategy.monetization_changed"
    to = json.loads(event.metadata["to"])
    assert to["currency"] == "USD"
    assert to["goals"][0] == {
        "source": "shorts_ads",
        "monthly_target": "150.00",
        "note": "Shorts fund the channel at first",
    }


# HTTP API


@pytest.fixture
def client(tmp_path: Path):
    settings = Settings(environment=Environment.TEST, database_path=tmp_path / "a.db")
    container = build_container(settings)
    with TestClient(create_app(container)) as test_client:
        test_client.sink = container.resolve(AuditSink)
        yield test_client


def channel_url(client: TestClient) -> str:
    response = client.post(
        "/channels", json={"title": "Money", "youtube_channel_id": "UC" + "m" * 22}
    )
    return f"/channels/{response.json()['id']}/strategy"


def test_put_monetization_creates_and_replaces(client: TestClient) -> None:
    url = channel_url(client) + "/monetization"

    created = client.put(url, json={})
    replaced = client.put(
        url,
        json={
            "goals": [
                {
                    "source": "sponsorships",
                    "monthly_target": "500",
                    "note": " One sponsor a month ",
                },
                {"source": "ads", "monthly_target": 120.5},
                {"source": "merchandise"},
            ],
            "currency": " usd ",
            "expected_version": 1,
        },
    )

    assert created.status_code == 201
    assert created.json()["monetization"] == {
        "goals": [],
        "currency": None,
        "goals_are_not_guaranteed": True,
        "notice": MONETIZATION_NOTICE,
    }
    assert "monetization" not in created.json()["missing_settings"]
    assert replaced.status_code == 200
    assert replaced.json()["monetization"] == {
        "goals": [
            {
                "source": "sponsorships",
                "monthly_target": "500.00",
                "note": "One sponsor a month",
            },
            {"source": "ads", "monthly_target": "120.50", "note": None},
            {"source": "merchandise", "monthly_target": None, "note": None},
        ],
        "currency": "USD",
        "goals_are_not_guaranteed": True,
        "notice": MONETIZATION_NOTICE,
    }
    last = client.sink.events()[-1]
    assert last.action == "strategy.monetization_changed"
    assert "goals_are_not_guaranteed" not in last.metadata["to"]


def test_the_notice_is_on_every_strategy_read(client: TestClient) -> None:
    url = channel_url(client)
    client.put(f"{url}/market", json={"country": "VN"})
    assert client.get(url).json()["monetization"] is None

    client.put(
        f"{url}/monetization",
        json={"goals": [{"source": "ads"}], "expected_version": 1},
    )

    assert client.get(url).json()["monetization"]["goals_are_not_guaranteed"] is True


@pytest.mark.parametrize(
    "body, field",
    [
        ({"goals": [{"source": "donations"}]}, "goals"),
        ({"goals": [{}]}, "goals"),
        ({"goals": [{"source": "ads"}, {"source": "ads"}]}, "goals"),
        ({"goals": [{"source": "ads", "monthly_target": "10"}]}, "body"),
        (
            {"goals": [{"source": "ads", "monthly_target": "-1"}], "currency": "USD"},
            "goals",
        ),
        (
            {
                "goals": [{"source": "ads", "monthly_target": "1.234"}],
                "currency": "USD",
            },
            "goals",
        ),
        (
            {
                "goals": [{"source": "ads", "monthly_target": "1000001"}],
                "currency": "USD",
            },
            "goals",
        ),
        ({"goals": [{"source": "ads", "note": "n" * 201}]}, "goals"),
        ({"goals": [{"source": "ads", "note": " "}]}, "goals"),
        ({"goals": [{"source": "ads", "expected": "1000"}]}, "goals"),
        ({"currency": "US"}, "currency"),
        ({"goals_are_not_guaranteed": True}, "goals_are_not_guaranteed"),
        ({"notice": "x"}, "notice"),
        ({"tracked_sources": ["ads"]}, "tracked_sources"),
        ({"ypp_status": "member"}, "ypp_status"),
    ],
)
def test_put_monetization_validation(
    client: TestClient, body: dict, field: str
) -> None:
    response = client.put(channel_url(client) + "/monetization", json=body)

    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "validation.invalid_request"
    assert field in [f["field"].split(".")[0] for f in error["fields"]]


def test_put_monetization_conflict(client: TestClient) -> None:
    url = channel_url(client) + "/monetization"
    client.put(url, json={})

    stale = client.put(url, json={})

    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "domain.strategy_conflict"
