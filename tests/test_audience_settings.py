"""D-046 Audience Settings (Prompt Pack v8, prompt #046).

Rules the user approved on 2026-10-01:

- an audience has a required description and optional age range, ordered
  interests and level (beginner, intermediate, advanced, mixed);
- description at most 500 characters; ages 13 to 100 with min <= max; at most
  10 interests of at most 50 characters each, no repeats;
- no field for sensitive traits and no targeting under 13;
- ``PUT /channels/{id}/strategy/audience`` follows the market rules.
"""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ai_youtube_agent.bootstrap import build_container
from ai_youtube_agent.content.strategy import (
    SETTING_TYPES,
    AgeRange,
    Audience,
    AudienceLevel,
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

T0 = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
USER = Actor(ActorKind.USER, "owner")
FULL = Audience(
    "Young professionals in Vietnam who want to save money",
    AgeRange(22, 35),
    ("budgeting", "side income"),
    AudienceLevel.BEGINNER,
)


# Entity


def test_only_the_description_is_required() -> None:
    audience = Audience("Adults interested in personal finance")

    assert audience.age_range is None
    assert audience.interests == ()
    assert audience.level is None


def test_the_audience_has_no_sensitive_fields() -> None:
    names = {f for f in Audience.__dataclass_fields__}
    assert names == {"description", "age_range", "interests", "level"}
    assert [level.value for level in AudienceLevel] == [
        "beginner",
        "intermediate",
        "advanced",
        "mixed",
    ]


@pytest.mark.parametrize(
    "low, high", [(12, 30), (13, 101), (30, 20), (0, 0), (True, 20)]
)
def test_age_range_limits(low, high) -> None:
    with pytest.raises(ValueError):
        AgeRange(low, high)


def test_age_range_edges_are_allowed() -> None:
    assert AgeRange(13, 100) == AgeRange(13, 100)
    assert AgeRange(18, 18).max == 18


@pytest.mark.parametrize(
    "kwargs",
    [
        {"description": "x" * 501},
        {"description": "   "},
        {"interests": tuple(f"topic {n}" for n in range(11))},
        {"interests": ("x" * 51,)},
        {"interests": ("saving", "Saving")},
        {"interests": ("",)},
    ],
)
def test_audience_limits(kwargs: dict) -> None:
    values = {"description": "Savers"} | kwargs
    with pytest.raises(ValueError):
        Audience(**values)


def test_audience_types_are_checked() -> None:
    with pytest.raises(TypeError):
        Audience("Savers", age_range=(18, 30))  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        Audience("Savers", interests=["a"])  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        Audience("Savers", level="beginner")  # type: ignore[arg-type]


def test_limits_at_the_edge_are_allowed() -> None:
    audience = Audience(
        "x" * 500, interests=tuple(f"t{n}" for n in range(9)) + ("y" * 50,)
    )
    assert len(audience.interests) == 10


# Persistence


def test_a_full_audience_round_trips(database: Database) -> None:
    channel = make_channel()
    profile = make_strategy_profile(channel, audience=FULL)
    with database.transaction() as connection:
        ChannelRepository(connection).add(channel)
        StrategyProfileRepository(connection).add(profile)

    with database.transaction() as connection:
        stored = StrategyProfileRepository(connection).get(profile.id)

    assert stored.audience == FULL


def test_an_audience_stored_before_046_still_reads(database: Database) -> None:
    channel = make_channel()
    profile = make_strategy_profile(channel)
    with database.transaction() as connection:
        ChannelRepository(connection).add(channel)
        StrategyProfileRepository(connection).add(profile)
        connection.execute(
            "UPDATE strategy_profiles SET audience_json = ?",
            (json.dumps({"description": "Old style"}),),
        )

    with database.transaction() as connection:
        stored = StrategyProfileRepository(connection).get(profile.id)

    assert stored.audience == Audience("Old style")


# Service


class Clock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> datetime:
        self.now += timedelta(seconds=1)
        return self.now


@pytest.fixture
def sink() -> InMemoryAuditSink:
    return InMemoryAuditSink()


@pytest.fixture
def service(database: Database, sink: InMemoryAuditSink) -> StrategySettings:
    return StrategySettings(database, AuditLog(sink), clock=Clock())


def test_setting_the_audience_touches_nothing_else(
    service: StrategySettings, database: Database, sink: InMemoryAuditSink
) -> None:
    channel = make_channel()
    full = make_strategy_profile(channel)
    with database.transaction() as connection:
        ChannelRepository(connection).add(channel)
        StrategyProfileRepository(connection).add(full)

    change = service.set_audience(
        channel.id, FULL, expected_version=full.version, actor=USER
    )

    stored = service.get(channel.id)
    assert stored == change.profile
    assert stored.audience == FULL
    assert stored.version == full.version + 1
    for name in SETTING_TYPES:
        if name != "audience":
            assert getattr(stored, name) == getattr(full, name)
    [event] = sink.events()
    assert event.action == "strategy.audience_changed"
    assert json.loads(event.metadata["to"]) == {
        "age_range": {"max": 35, "min": 22},
        "description": FULL.description,
        "interests": ["budgeting", "side income"],
        "level": "beginner",
    }
    assert json.loads(event.metadata["from"])["description"] == (
        "Adults interested in personal finance"
    )


def test_the_audience_can_create_the_profile(
    service: StrategySettings, database: Database, sink: InMemoryAuditSink
) -> None:
    channel = make_channel()
    with database.transaction() as connection:
        ChannelRepository(connection).add(channel)

    change = service.set_audience(
        channel.id, Audience("Savers"), expected_version=None, actor=USER
    )

    assert change.created
    assert change.profile.audience == Audience("Savers")
    assert [e.action for e in sink.events()] == [
        "strategy.created",
        "strategy.audience_changed",
    ]
    assert sink.events()[1].metadata["from"] is None


# HTTP API


@pytest.fixture
def client(tmp_path: Path, database_copy):
    path = database_copy(tmp_path / "a.db")
    settings = Settings(environment=Environment.TEST, database_path=path)
    container = build_container(settings)
    with TestClient(create_app(container)) as test_client:
        test_client.sink = container.resolve(AuditSink)
        yield test_client


def audience_url(client: TestClient) -> str:
    response = client.post(
        "/channels", json={"title": "Money", "youtube_channel_id": "UC" + "z" * 22}
    )
    return f"/channels/{response.json()['id']}/strategy/audience"


def test_put_audience_creates_and_replaces(client: TestClient) -> None:
    url = audience_url(client)

    created = client.put(
        url,
        json={
            "description": "  Young savers  ",
            "age_range": {"min": 22, "max": 35},
            "interests": [" budgeting ", "side income"],
            "level": "beginner",
        },
    )
    replaced = client.put(
        url, json={"description": "Everyone who saves", "expected_version": 1}
    )

    assert created.status_code == 201
    assert created.json()["audience"] == {
        "description": "Young savers",
        "age_range": {"min": 22, "max": 35},
        "interests": ["budgeting", "side income"],
        "level": "beginner",
    }
    assert replaced.status_code == 200
    assert replaced.json()["audience"] == {
        "description": "Everyone who saves",
        "age_range": None,
        "interests": [],
        "level": None,
    }
    assert replaced.json()["version"] == 2
    assert [e.action for e in client.sink.events()][-1] == "strategy.audience_changed"


@pytest.mark.parametrize(
    "body, field",
    [
        ({}, "description"),
        ({"description": " "}, "description"),
        ({"description": "x" * 501}, "description"),
        ({"description": "A", "age_range": {"min": 12, "max": 20}}, "age_range"),
        ({"description": "A", "age_range": {"min": 30, "max": 20}}, "age_range"),
        ({"description": "A", "age_range": {"min": 20}}, "age_range"),
        ({"description": "A", "interests": ["x"] * 2}, "interests"),
        ({"description": "A", "interests": [str(n) for n in range(11)]}, "interests"),
        ({"description": "A", "interests": ["y" * 51]}, "interests"),
        ({"description": "A", "level": "expert"}, "level"),
        ({"description": "A", "religion": "any"}, "religion"),
        ({"description": "A", "expected_version": 0}, "expected_version"),
    ],
)
def test_put_audience_validation(client: TestClient, body: dict, field: str) -> None:
    response = client.put(audience_url(client), json=body)

    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "validation.invalid_request"
    assert field in [f["field"].split(".")[0] for f in error["fields"]]


def test_put_audience_conflict(client: TestClient) -> None:
    url = audience_url(client)
    client.put(url, json={"description": "Savers"})

    stale = client.put(url, json={"description": "Others"})

    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "domain.strategy_conflict"
