"""D-048 Brand Settings (Prompt Pack v8, prompt #048).

Rules the user approved on 2026-10-01:

- tone and written voice: ``tone``, up to 5 ``tone_keywords``, up to 10
  ``voice_dos`` and ``voice_donts``, up to 30 ``banned_phrases``;
- visual rules: a primary colour, up to 5 accent colours (``#RRGGBB``), a font
  name and notes; no files;
- the spoken TTS voice stays in ``VoiceProfile`` (#087), not in the brand;
- limits: name 100, tone 200, keyword 30, voice rule 200, banned phrase 50,
  font 100, notes 500; lists do not repeat ignoring case;
- ``PUT /channels/{id}/strategy/brand`` replaces the whole brand and follows
  the market rules.
"""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ai_youtube_agent.bootstrap import build_container
from ai_youtube_agent.content.strategy import SETTING_TYPES, Brand, BrandVisual
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
FULL = Brand(
    "Money Minute",
    "calm and clear",
    ("calm", "clear"),
    ("Explain one idea per video",),
    ("Promise returns",),
    ("get rich quick", "guaranteed profit"),
    BrandVisual("#0A7E5C", ("#F2C14E",), "Inter", "Clean flat graphics"),
)


# Entity


def test_only_the_name_is_required() -> None:
    brand = Brand("Money Minute")

    assert brand.tone is None
    assert brand.tone_keywords == brand.voice_dos == brand.banned_phrases == ()
    assert brand.visual is None


def test_the_brand_has_no_spoken_voice_fields() -> None:
    assert set(Brand.__dataclass_fields__) == {
        "name",
        "tone",
        "tone_keywords",
        "voice_dos",
        "voice_donts",
        "banned_phrases",
        "visual",
    }


@pytest.mark.parametrize(
    "build",
    [
        lambda: Brand("x" * 101),
        lambda: Brand("A", tone="t" * 201),
        lambda: Brand("A", tone_keywords=tuple(f"k{n}" for n in range(6))),
        lambda: Brand("A", tone_keywords=("k" * 31,)),
        lambda: Brand("A", tone_keywords=("Calm", "calm")),
        lambda: Brand("A", voice_dos=tuple(f"d{n}" for n in range(11))),
        lambda: Brand("A", voice_donts=("d" * 201,)),
        lambda: Brand("A", banned_phrases=tuple(f"b{n}" for n in range(31))),
        lambda: Brand("A", banned_phrases=("b" * 51,)),
        lambda: Brand("A", banned_phrases=(" ",)),
        lambda: BrandVisual("#12345G"),
        lambda: BrandVisual("#abcdef"),
        lambda: BrandVisual(accent_colors=tuple(f"#00000{n}" for n in range(6))),
        lambda: BrandVisual("#000000", ("#000000",)),
        lambda: BrandVisual(font_family="f" * 101),
        lambda: BrandVisual(notes="n" * 501),
        lambda: BrandVisual(notes=" "),
    ],
)
def test_brand_limits(build) -> None:
    with pytest.raises(ValueError):
        build()


def test_brand_types_are_checked() -> None:
    with pytest.raises(TypeError):
        Brand("A", tone_keywords=["calm"])  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        Brand("A", visual={"primary_color": "#000000"})  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        BrandVisual(accent_colors=["#000000"])  # type: ignore[arg-type]


def test_limits_at_the_edge_are_allowed() -> None:
    brand = Brand(
        "x" * 100,
        "t" * 200,
        tuple(f"{n}" + "k" * 29 for n in range(5)),
        tuple(f"{n}" + "d" * 199 for n in range(10)),
        tuple(f"{n}" + "d" * 199 for n in range(10)),
        tuple(f"{n:02}" + "b" * 48 for n in range(30)),
        BrandVisual(
            "#000000",
            tuple(f"#00000{n}" for n in range(1, 6)),
            "f" * 100,
            "n" * 500,
        ),
    )
    assert len(brand.banned_phrases) == 30


# Persistence


def test_a_full_brand_round_trips(database: Database) -> None:
    channel = make_channel()
    profile = make_strategy_profile(channel, brand=FULL)
    with database.transaction() as connection:
        ChannelRepository(connection).add(channel)
        StrategyProfileRepository(connection).add(profile)

    with database.transaction() as connection:
        assert StrategyProfileRepository(connection).get(profile.id).brand == FULL


def test_a_brand_stored_before_048_still_reads(database: Database) -> None:
    channel = make_channel()
    profile = make_strategy_profile(channel)
    with database.transaction() as connection:
        ChannelRepository(connection).add(channel)
        StrategyProfileRepository(connection).add(profile)
        connection.execute(
            "UPDATE strategy_profiles SET brand_json = ?",
            (json.dumps({"name": "Old", "tone": None}),),
        )

    with database.transaction() as connection:
        stored = StrategyProfileRepository(connection).get(profile.id)

    assert stored.brand == Brand("Old")


# Service


class Clock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> datetime:
        self.now += timedelta(seconds=1)
        return self.now


def test_setting_the_brand_touches_nothing_else(database: Database) -> None:
    sink = InMemoryAuditSink()
    service = StrategySettings(database, AuditLog(sink), clock=Clock())
    channel = make_channel()
    full = make_strategy_profile(channel)
    with database.transaction() as connection:
        ChannelRepository(connection).add(channel)
        StrategyProfileRepository(connection).add(full)

    change = service.set_brand(
        channel.id, FULL, expected_version=full.version, actor=USER
    )

    stored = service.get(channel.id)
    assert stored == change.profile
    assert stored.brand == FULL
    for name in SETTING_TYPES:
        if name != "brand":
            assert getattr(stored, name) == getattr(full, name)
    [event] = sink.events()
    assert event.action == "strategy.brand_changed"
    assert json.loads(event.metadata["to"])["visual"]["primary_color"] == "#0A7E5C"


# HTTP API


@pytest.fixture
def client(tmp_path: Path, database_copy):
    path = database_copy(tmp_path / "a.db")
    settings = Settings(environment=Environment.TEST, database_path=path)
    container = build_container(settings)
    with TestClient(create_app(container)) as test_client:
        test_client.sink = container.resolve(AuditSink)
        yield test_client


def brand_url(client: TestClient) -> str:
    response = client.post(
        "/channels", json={"title": "Money", "youtube_channel_id": "UC" + "b" * 22}
    )
    return f"/channels/{response.json()['id']}/strategy/brand"


def test_put_brand_creates_and_replaces(client: TestClient) -> None:
    url = brand_url(client)

    created = client.put(
        url,
        json={
            "name": " Money Minute ",
            "tone": "calm and clear",
            "tone_keywords": ["calm", " clear "],
            "voice_dos": ["Explain one idea per video"],
            "voice_donts": ["Promise returns"],
            "banned_phrases": ["get rich quick"],
            "visual": {
                "primary_color": "#0a7e5c",
                "accent_colors": [" #f2c14e "],
                "font_family": "Inter",
                "notes": "Clean flat graphics",
            },
        },
    )
    replaced = client.put(url, json={"name": "Money Minute", "expected_version": 1})

    assert created.status_code == 201
    brand = created.json()["brand"]
    assert brand["name"] == "Money Minute"
    assert brand["tone_keywords"] == ["calm", "clear"]
    assert brand["visual"] == {
        "primary_color": "#0A7E5C",
        "accent_colors": ["#F2C14E"],
        "font_family": "Inter",
        "notes": "Clean flat graphics",
    }
    assert replaced.status_code == 200
    assert replaced.json()["brand"] == {
        "name": "Money Minute",
        "tone": None,
        "tone_keywords": [],
        "voice_dos": [],
        "voice_donts": [],
        "banned_phrases": [],
        "visual": None,
    }
    assert client.sink.events()[-1].action == "strategy.brand_changed"


@pytest.mark.parametrize(
    "body, field",
    [
        ({}, "name"),
        ({"name": "x" * 101}, "name"),
        ({"name": "A", "tone": " "}, "tone"),
        (
            {"name": "A", "tone_keywords": ["a", "b", "c", "d", "e", "f"]},
            "tone_keywords",
        ),
        ({"name": "A", "tone_keywords": ["Calm", "calm"]}, "tone_keywords"),
        ({"name": "A", "voice_dos": ["d" * 201]}, "voice_dos"),
        ({"name": "A", "voice_donts": [str(n) for n in range(11)]}, "voice_donts"),
        ({"name": "A", "banned_phrases": ["b" * 51]}, "banned_phrases"),
        ({"name": "A", "visual": {"primary_color": "green"}}, "visual"),
        (
            {
                "name": "A",
                "visual": {"primary_color": "#000000", "accent_colors": ["#000000"]},
            },
            "visual",
        ),
        ({"name": "A", "visual": {"accent_colors": ["#000001"] * 2}}, "visual"),
        ({"name": "A", "visual": {"logo": "x.png"}}, "visual"),
        ({"name": "A", "voice_profile_id": "v1"}, "voice_profile_id"),
    ],
)
def test_put_brand_validation(client: TestClient, body: dict, field: str) -> None:
    response = client.put(brand_url(client), json=body)

    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "validation.invalid_request"
    assert field in [f["field"].split(".")[0] for f in error["fields"]]


def test_put_brand_conflict(client: TestClient) -> None:
    url = brand_url(client)
    client.put(url, json={"name": "A"})

    stale = client.put(url, json={"name": "B"})

    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "domain.strategy_conflict"
