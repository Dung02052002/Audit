"""D-049 Format Settings (Prompt Pack v8, prompt #049).

Rules the user approved on 2026-10-02:

- one strategy setting, ``format``, holds the Shorts and LongForm production
  defaults together, stored in ``format_json`` (migration 0004);
- each format has a target duration (``min_seconds`` to ``max_seconds``:
  Shorts 1 to 180, LongForm 181 to 14400), a resolution (720p, 1080p, 2160p)
  and captions on or off; LongForm adds chapters on or off;
- aspect ratios are fixed: Shorts 9:16, LongForm 16:9;
- LongForm defaults can be saved while ``LONGFORM_ENABLED`` is off; the flag
  controls production, not the setting;
- ``format`` is required like every other setting: it shows in
  ``missing_settings`` until saved;
- ``PUT /channels/{id}/strategy/format`` replaces the whole setting and follows
  the market rules.
"""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ai_youtube_agent.bootstrap import build_container
from ai_youtube_agent.content.strategy import (
    SETTING_TYPES,
    AspectRatio,
    FormatSettings,
    LongFormFormat,
    Market,
    Resolution,
    ShortsFormat,
    StrategyProfile,
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
from ai_youtube_agent.core.db.migrate import default_migrations, migrate
from ai_youtube_agent.core.db.repositories.channel import (
    ChannelRepository,
    StrategyProfileRepository,
)
from ai_youtube_agent.core.flags import FeatureFlags
from ai_youtube_agent.main import create_app
from factories import make_channel, make_strategy_profile

T0 = datetime(2026, 10, 2, 12, 0, tzinfo=UTC)
USER = Actor(ActorKind.USER, "owner")
FULL = FormatSettings(
    ShortsFormat(20, 45, Resolution.UHD_2160, captions=False),
    LongFormFormat(600, 1200, Resolution.HD_720, captions=True, chapters=False),
)


# Entity


def test_defaults_are_1080p_with_captions_and_fixed_aspect_ratios() -> None:
    shorts = ShortsFormat(15, 60)
    longform = LongFormFormat(480, 900)

    assert shorts.resolution is longform.resolution is Resolution.FULL_HD_1080
    assert shorts.captions and longform.captions and longform.chapters
    assert shorts.aspect_ratio is AspectRatio.VERTICAL
    assert longform.aspect_ratio is AspectRatio.HORIZONTAL


def test_only_longform_has_chapters() -> None:
    assert "chapters" not in ShortsFormat.__dataclass_fields__
    assert "chapters" in LongFormFormat.__dataclass_fields__


@pytest.mark.parametrize(
    "build",
    [
        lambda: ShortsFormat(0, 60),
        lambda: ShortsFormat(15, 181),
        lambda: ShortsFormat(61, 60),
        lambda: ShortsFormat(15, 60, aspect_ratio=AspectRatio.HORIZONTAL),
        lambda: LongFormFormat(180, 900),
        lambda: LongFormFormat(480, 14401),
        lambda: LongFormFormat(901, 900),
        lambda: LongFormFormat(480, 900, aspect_ratio=AspectRatio.VERTICAL),
    ],
)
def test_format_limits(build) -> None:
    with pytest.raises(ValueError):
        build()


@pytest.mark.parametrize(
    "build",
    [
        lambda: ShortsFormat(15, 60, resolution="1080p"),  # type: ignore[arg-type]
        lambda: ShortsFormat(15, 60, captions=1),  # type: ignore[arg-type]
        lambda: LongFormFormat(480, 900, chapters="yes"),  # type: ignore[arg-type]
        lambda: FormatSettings(LongFormFormat(480, 900), LongFormFormat(480, 900)),  # type: ignore[arg-type]
        lambda: FormatSettings(ShortsFormat(15, 60), ShortsFormat(15, 60)),  # type: ignore[arg-type]
    ],
)
def test_format_types_are_checked(build) -> None:
    with pytest.raises(TypeError):
        build()


def test_durations_must_be_whole_numbers() -> None:
    with pytest.raises(ValueError):
        ShortsFormat(True, 60)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        LongFormFormat(480.0, 900)  # type: ignore[arg-type]


def test_limits_at_the_edge_are_allowed() -> None:
    assert ShortsFormat(1, 180).max_seconds == 180
    assert LongFormFormat(181, 14400).max_seconds == 14400
    assert ShortsFormat(60, 60).min_seconds == 60


def test_format_is_a_required_setting() -> None:
    assert list(SETTING_TYPES).index("format") == list(SETTING_TYPES).index("brand") + 1
    profile = StrategyProfile.create("channel-1", market=Market("VN"), actor=USER)

    assert "format" in profile.missing_settings
    assert make_strategy_profile(make_channel()).is_complete


# Persistence


def test_a_full_format_round_trips(database: Database) -> None:
    channel = make_channel()
    profile = make_strategy_profile(channel, format=FULL)
    with database.transaction() as connection:
        ChannelRepository(connection).add(channel)
        StrategyProfileRepository(connection).add(profile)

    with database.transaction() as connection:
        assert StrategyProfileRepository(connection).get(profile.id).format == FULL


def test_migration_0004_leaves_existing_profiles_without_a_format(
    tmp_path: Path,
) -> None:
    path = tmp_path / "app.db"
    migrate(path, migrations=default_migrations()[:3])
    database = Database(path)
    channel = make_channel()
    profile = StrategyProfile.create(
        channel.id, market=Market("VN"), actor=USER, clock=lambda: T0
    )
    with database.transaction() as connection:
        ChannelRepository(connection).add(channel)
        connection.execute(
            "INSERT INTO strategy_profiles (id, channel_id, market_country, "
            "version, updated_by_kind, updated_by_id, created_at, updated_at) "
            "VALUES (?, ?, 'VN', 1, 'user', 'owner', ?, ?)",
            (
                profile.id,
                channel.id,
                "2026-10-02T12:00:00.000000Z",
                "2026-10-02T12:00:00.000000Z",
            ),
        )

    report = migrate(path)

    assert report.applied == (4,)
    with database.transaction() as connection:
        stored = StrategyProfileRepository(connection).get(profile.id)
    assert stored == profile
    assert stored.format is None
    assert "format" in stored.missing_settings


def test_format_json_must_be_valid_json(database: Database) -> None:
    channel = make_channel()
    profile = make_strategy_profile(channel)
    with database.transaction() as connection:
        ChannelRepository(connection).add(channel)
        StrategyProfileRepository(connection).add(profile)

    with pytest.raises(Exception, match="CHECK"), database.transaction() as conn:
        conn.execute("UPDATE strategy_profiles SET format_json = 'not json'")


# Service


class Clock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> datetime:
        self.now += timedelta(seconds=1)
        return self.now


def test_setting_the_format_touches_nothing_else(database: Database) -> None:
    sink = InMemoryAuditSink()
    service = StrategySettings(database, AuditLog(sink), clock=Clock())
    channel = make_channel()
    full = make_strategy_profile(channel)
    with database.transaction() as connection:
        ChannelRepository(connection).add(channel)
        StrategyProfileRepository(connection).add(full)

    change = service.set_format(
        channel.id, FULL, expected_version=full.version, actor=USER
    )

    stored = service.get(channel.id)
    assert stored == change.profile
    assert stored.format == FULL
    assert stored.version == full.version + 1
    for name in SETTING_TYPES:
        if name != "format":
            assert getattr(stored, name) == getattr(full, name)
    [event] = sink.events()
    assert event.action == "strategy.format_changed"
    assert json.loads(event.metadata["to"])["longform"]["chapters"] is False
    assert json.loads(event.metadata["from"])["shorts"]["max_seconds"] == 60


def test_saving_the_same_format_records_nothing(database: Database) -> None:
    sink = InMemoryAuditSink()
    service = StrategySettings(database, AuditLog(sink), clock=Clock())
    channel = make_channel()
    full = make_strategy_profile(channel)
    with database.transaction() as connection:
        ChannelRepository(connection).add(channel)
        StrategyProfileRepository(connection).add(full)

    change = service.set_format(
        channel.id, full.format, expected_version=full.version, actor=USER
    )

    assert change.profile == full
    assert sink.events() == ()


# HTTP API


@pytest.fixture
def client(tmp_path: Path):
    settings = Settings(environment=Environment.TEST, database_path=tmp_path / "a.db")
    container = build_container(settings)
    with TestClient(create_app(container)) as test_client:
        test_client.sink = container.resolve(AuditSink)
        test_client.flags = container.resolve(FeatureFlags)
        yield test_client


def format_url(client: TestClient) -> str:
    response = client.post(
        "/channels", json={"title": "Money", "youtube_channel_id": "UC" + "f" * 22}
    )
    return f"/channels/{response.json()['id']}/strategy/format"


def test_put_format_creates_and_replaces(client: TestClient) -> None:
    url = format_url(client)
    assert client.flags.longform_enabled is False

    created = client.put(
        url,
        json={
            "shorts": {"min_seconds": 15, "max_seconds": 60},
            "longform": {"min_seconds": 480, "max_seconds": 900},
        },
    )
    replaced = client.put(
        url,
        json={
            "shorts": {
                "min_seconds": 20,
                "max_seconds": 45,
                "resolution": "2160p",
                "captions": False,
                "aspect_ratio": "9:16",
            },
            "longform": {
                "min_seconds": 600,
                "max_seconds": 1200,
                "resolution": "720p",
                "chapters": False,
            },
            "expected_version": 1,
        },
    )

    assert created.status_code == 201
    assert created.json()["format"] == {
        "shorts": {
            "min_seconds": 15,
            "max_seconds": 60,
            "resolution": "1080p",
            "captions": True,
            "aspect_ratio": "9:16",
        },
        "longform": {
            "min_seconds": 480,
            "max_seconds": 900,
            "resolution": "1080p",
            "captions": True,
            "chapters": True,
            "aspect_ratio": "16:9",
        },
    }
    assert "format" not in created.json()["missing_settings"]
    assert replaced.status_code == 200
    assert replaced.json()["version"] == 2
    assert replaced.json()["format"]["shorts"]["resolution"] == "2160p"
    assert replaced.json()["format"]["longform"]["chapters"] is False
    assert [e.action for e in client.sink.events()][-1] == "strategy.format_changed"


def test_get_strategy_lists_format_as_missing(client: TestClient) -> None:
    url = format_url(client).removesuffix("/format")
    client.put(f"{url}/market", json={"country": "VN"})

    body = client.get(url).json()

    assert body["format"] is None
    assert "format" in body["missing_settings"]


SHORTS = {"min_seconds": 15, "max_seconds": 60}
LONGFORM = {"min_seconds": 480, "max_seconds": 900}


@pytest.mark.parametrize(
    "body, field",
    [
        ({"longform": LONGFORM}, "shorts"),
        ({"shorts": SHORTS}, "longform"),
        ({"shorts": {**SHORTS, "min_seconds": 0}, "longform": LONGFORM}, "shorts"),
        ({"shorts": {**SHORTS, "max_seconds": 181}, "longform": LONGFORM}, "shorts"),
        ({"shorts": {**SHORTS, "min_seconds": 61}, "longform": LONGFORM}, "shorts"),
        ({"shorts": {**SHORTS, "max_seconds": "60"}, "longform": LONGFORM}, "shorts"),
        ({"shorts": {**SHORTS, "resolution": "480p"}, "longform": LONGFORM}, "shorts"),
        ({"shorts": {**SHORTS, "captions": "yes"}, "longform": LONGFORM}, "shorts"),
        (
            {"shorts": {**SHORTS, "aspect_ratio": "16:9"}, "longform": LONGFORM},
            "shorts",
        ),
        ({"shorts": {**SHORTS, "chapters": True}, "longform": LONGFORM}, "shorts"),
        ({"shorts": SHORTS, "longform": {**LONGFORM, "min_seconds": 180}}, "longform"),
        (
            {"shorts": SHORTS, "longform": {**LONGFORM, "max_seconds": 14401}},
            "longform",
        ),
        ({"shorts": SHORTS, "longform": {**LONGFORM, "min_seconds": 901}}, "longform"),
        (
            {"shorts": SHORTS, "longform": {**LONGFORM, "aspect_ratio": "9:16"}},
            "longform",
        ),
        ({"shorts": SHORTS, "longform": LONGFORM, "cadence": {}}, "cadence"),
    ],
)
def test_put_format_validation(client: TestClient, body: dict, field: str) -> None:
    response = client.put(format_url(client), json=body)

    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "validation.invalid_request"
    assert field in [f["field"].split(".")[0] for f in error["fields"]]


def test_put_format_conflict(client: TestClient) -> None:
    url = format_url(client)
    body = {"shorts": SHORTS, "longform": LONGFORM}
    client.put(url, json=body)

    stale = client.put(url, json=body)

    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "domain.strategy_conflict"
