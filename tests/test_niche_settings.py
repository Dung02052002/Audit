"""D-047 Niche Settings (Prompt Pack v8, prompt #047).

Rules the user approved on 2026-10-01:

- a niche has a name and 1 to 10 ordered content pillars; each pillar has a
  name and an optional description;
- niche name at most 100 characters, pillar name at most 60, description at
  most 300, no two pillar names equal ignoring case;
- ``PUT /channels/{id}/strategy/niche`` replaces the whole niche and follows
  the market rules.
"""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ai_youtube_agent.bootstrap import build_container
from ai_youtube_agent.content.strategy import SETTING_TYPES, Niche, Pillar
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
TECH = Niche(
    "Consumer tech",
    (
        Pillar("Reviews", "Honest reviews of budget phones"),
        Pillar("How-to"),
        Pillar("News", "Weekly recap"),
    ),
)


# Entity


def test_pillars_keep_order_and_optional_descriptions() -> None:
    assert [p.name for p in TECH.pillars] == ["Reviews", "How-to", "News"]
    assert TECH.pillars[1].description is None


@pytest.mark.parametrize(
    "build",
    [
        lambda: Niche("x" * 101, (Pillar("a"),)),
        lambda: Niche("Tech", ()),
        lambda: Niche("Tech", tuple(Pillar(f"p{n}") for n in range(11))),
        lambda: Niche("Tech", (Pillar("Reviews"), Pillar("reviews"))),
        lambda: Pillar("x" * 61),
        lambda: Pillar("a", "x" * 301),
        lambda: Pillar("a", "  "),
    ],
)
def test_niche_limits(build) -> None:
    with pytest.raises(ValueError):
        build()


def test_pillars_must_be_pillar_values() -> None:
    with pytest.raises(TypeError):
        Niche("Tech", ("Reviews",))  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        Niche("Tech", [Pillar("Reviews")])  # type: ignore[arg-type]


def test_limits_at_the_edge_are_allowed() -> None:
    niche = Niche(
        "x" * 100,
        tuple(Pillar(f"{n}" + "y" * 58, "z" * 300) for n in range(10)),
    )
    assert len(niche.pillars) == 10


# Persistence


def test_a_niche_round_trips(database: Database) -> None:
    channel = make_channel()
    profile = make_strategy_profile(channel, niche=TECH)
    with database.transaction() as connection:
        ChannelRepository(connection).add(channel)
        StrategyProfileRepository(connection).add(profile)

    with database.transaction() as connection:
        assert StrategyProfileRepository(connection).get(profile.id).niche == TECH


def test_a_niche_stored_before_047_still_reads(database: Database) -> None:
    channel = make_channel()
    profile = make_strategy_profile(channel)
    with database.transaction() as connection:
        ChannelRepository(connection).add(channel)
        StrategyProfileRepository(connection).add(profile)
        connection.execute(
            "UPDATE strategy_profiles SET niche_json = ?",
            (json.dumps({"name": "Finance", "pillars": ["budgeting", "investing"]}),),
        )

    with database.transaction() as connection:
        stored = StrategyProfileRepository(connection).get(profile.id)

    assert stored.niche == Niche("Finance", (Pillar("budgeting"), Pillar("investing")))


# Service


class Clock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> datetime:
        self.now += timedelta(seconds=1)
        return self.now


def test_setting_the_niche_touches_nothing_else(database: Database) -> None:
    sink = InMemoryAuditSink()
    service = StrategySettings(database, AuditLog(sink), clock=Clock())
    channel = make_channel()
    full = make_strategy_profile(channel)
    with database.transaction() as connection:
        ChannelRepository(connection).add(channel)
        StrategyProfileRepository(connection).add(full)

    change = service.set_niche(
        channel.id, TECH, expected_version=full.version, actor=USER
    )

    stored = service.get(channel.id)
    assert stored == change.profile
    assert stored.niche == TECH
    for name in SETTING_TYPES:
        if name != "niche":
            assert getattr(stored, name) == getattr(full, name)
    [event] = sink.events()
    assert event.action == "strategy.niche_changed"
    assert json.loads(event.metadata["to"])["pillars"][0] == {
        "name": "Reviews",
        "description": "Honest reviews of budget phones",
    }


# HTTP API


@pytest.fixture
def client(tmp_path: Path):
    settings = Settings(environment=Environment.TEST, database_path=tmp_path / "a.db")
    container = build_container(settings)
    with TestClient(create_app(container)) as test_client:
        test_client.sink = container.resolve(AuditSink)
        yield test_client


def niche_url(client: TestClient) -> str:
    response = client.post(
        "/channels", json={"title": "Tech", "youtube_channel_id": "UC" + "n" * 22}
    )
    return f"/channels/{response.json()['id']}/strategy/niche"


def test_put_niche_creates_then_reorders(client: TestClient) -> None:
    url = niche_url(client)

    created = client.put(
        url,
        json={
            "name": "  Consumer tech ",
            "pillars": [
                {"name": " Reviews ", "description": " Honest reviews "},
                {"name": "How-to"},
            ],
        },
    )
    reordered = client.put(
        url,
        json={
            "name": "Consumer tech",
            "pillars": [{"name": "How-to"}, {"name": "Reviews"}],
            "expected_version": 1,
        },
    )

    assert created.status_code == 201
    assert created.json()["niche"] == {
        "name": "Consumer tech",
        "pillars": [
            {"name": "Reviews", "description": "Honest reviews"},
            {"name": "How-to", "description": None},
        ],
    }
    assert reordered.status_code == 200
    assert [p["name"] for p in reordered.json()["niche"]["pillars"]] == [
        "How-to",
        "Reviews",
    ]
    assert reordered.json()["niche"]["pillars"][1]["description"] is None
    assert reordered.json()["version"] == 2
    assert client.sink.events()[-1].action == "strategy.niche_changed"


@pytest.mark.parametrize(
    "body, field",
    [
        ({"pillars": [{"name": "a"}]}, "name"),
        ({"name": " ", "pillars": [{"name": "a"}]}, "name"),
        ({"name": "x" * 101, "pillars": [{"name": "a"}]}, "name"),
        ({"name": "Tech"}, "pillars"),
        ({"name": "Tech", "pillars": []}, "pillars"),
        ({"name": "Tech", "pillars": [{"name": str(n)} for n in range(11)]}, "pillars"),
        ({"name": "Tech", "pillars": [{"name": "A"}, {"name": "a"}]}, "pillars"),
        ({"name": "Tech", "pillars": [{"name": "y" * 61}]}, "pillars"),
        (
            {"name": "Tech", "pillars": [{"name": "a", "description": "d" * 301}]},
            "pillars",
        ),
        ({"name": "Tech", "pillars": [{"name": "a", "weight": 50}]}, "pillars"),
        ({"name": "Tech", "pillars": ["Reviews"]}, "pillars"),
        ({"name": "Tech", "pillars": [{"name": "a"}], "audience": {}}, "audience"),
    ],
)
def test_put_niche_validation(client: TestClient, body: dict, field: str) -> None:
    response = client.put(niche_url(client), json=body)

    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "validation.invalid_request"
    assert field in [f["field"].split(".")[0] for f in error["fields"]]


def test_put_niche_conflict(client: TestClient) -> None:
    url = niche_url(client)
    body = {"name": "Tech", "pillars": [{"name": "Reviews"}]}
    client.put(url, json=body)

    stale = client.put(url, json=body | {"name": "Other"})

    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "domain.strategy_conflict"
