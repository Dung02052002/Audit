"""D-050 Cadence Settings (Prompt Pack v8, prompt #050).

Rules the user approved on 2026-10-02:

- daily limits: ``shorts_per_day`` 0 to 20 and ``longform_per_day`` 0 to 5; a
  LongForm limit above 0 may be saved while ``LONGFORM_ENABLED`` is off;
- a channel ``time_zone`` (IANA name, ``tzdata`` dependency) in which
  ``DailyLimitGate`` counts the day and the publish times are meant;
- per content type a ``PublishSchedule``: weekdays (1 to 7, Monday first), up
  to 5 ``HH:MM`` times (ascending) and a minimum gap of 0 to 1440 minutes;
- the limits keep their columns; the time zone and schedules are stored in
  ``cadence_schedule_json`` (migration 0005), and a cadence stored before
  #050 reads as UTC with the default schedule;
- ``PUT /channels/{id}/strategy/cadence`` replaces the whole cadence and
  follows the market rules.
"""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ai_youtube_agent.bootstrap import build_container
from ai_youtube_agent.content.strategy import (
    ALL_WEEKDAYS,
    SETTING_TYPES,
    Cadence,
    PublishSchedule,
    Weekday,
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
    _strategy_row,
)
from ai_youtube_agent.core.flags import FeatureFlags
from ai_youtube_agent.main import create_app
from factories import make_channel, make_strategy_profile

T0 = datetime(2026, 10, 2, 12, 0, tzinfo=UTC)
USER = Actor(ActorKind.USER, "owner")
MON, WED, FRI = Weekday.MONDAY, Weekday.WEDNESDAY, Weekday.FRIDAY
FULL = Cadence(
    3,
    1,
    "Asia/Ho_Chi_Minh",
    PublishSchedule((MON, WED, FRI), ("07:00", "18:30"), 120),
    PublishSchedule((Weekday.SATURDAY,), ("20:00",), 0),
)


# Entity


def test_defaults_are_utc_every_day_no_times_no_gap() -> None:
    cadence = Cadence(2, 0)

    assert cadence.time_zone == "UTC"
    assert cadence.shorts_schedule == cadence.longform_schedule == PublishSchedule()
    assert PublishSchedule().weekdays == ALL_WEEKDAYS == tuple(Weekday)
    assert PublishSchedule().times == ()
    assert PublishSchedule().min_gap_minutes == 0


@pytest.mark.parametrize(
    "build",
    [
        lambda: Cadence(21, 0),
        lambda: Cadence(0, 6),
        lambda: Cadence(-1, 0),
        lambda: Cadence(True, 0),
        lambda: Cadence(1, 0, "Mars/Olympus"),
        lambda: Cadence(1, 0, "asia/ho_chi_minh"),
        lambda: Cadence(1, 0, "+07:00"),
        lambda: PublishSchedule(()),
        lambda: PublishSchedule((MON, MON)),
        lambda: PublishSchedule((FRI, MON)),
        lambda: PublishSchedule(times=("7:00",)),
        lambda: PublishSchedule(times=("24:00",)),
        lambda: PublishSchedule(times=("18:30", "07:00")),
        lambda: PublishSchedule(times=("07:00", "07:00")),
        lambda: PublishSchedule(times=tuple(f"0{n}:00" for n in range(6))),
        lambda: PublishSchedule(min_gap_minutes=-1),
        lambda: PublishSchedule(min_gap_minutes=1441),
    ],
)
def test_cadence_limits(build) -> None:
    with pytest.raises(ValueError):
        build()


@pytest.mark.parametrize(
    "build",
    [
        lambda: PublishSchedule(["monday"]),  # type: ignore[arg-type]
        lambda: PublishSchedule(("monday",)),  # type: ignore[arg-type]
        lambda: PublishSchedule(times=["07:00"]),  # type: ignore[arg-type]
        lambda: Cadence(1, 0, shorts_schedule={}),  # type: ignore[arg-type]
    ],
)
def test_cadence_types_are_checked(build) -> None:
    with pytest.raises(TypeError):
        build()


def test_limits_at_the_edge_are_allowed() -> None:
    cadence = Cadence(
        20,
        5,
        "America/New_York",
        PublishSchedule(
            ALL_WEEKDAYS, ("00:00", "06:00", "12:00", "18:00", "23:59"), 1440
        ),
    )
    assert cadence.shorts_per_day == 20
    assert cadence.longform_per_day == 5


def test_cadence_stays_a_required_setting() -> None:
    assert "cadence" in SETTING_TYPES


# Persistence


def test_a_full_cadence_round_trips(database: Database) -> None:
    channel = make_channel()
    profile = make_strategy_profile(channel, cadence=FULL)
    with database.transaction() as connection:
        ChannelRepository(connection).add(channel)
        StrategyProfileRepository(connection).add(profile)

    with database.transaction() as connection:
        assert StrategyProfileRepository(connection).get(profile.id).cadence == FULL
        row = connection.execute(
            "SELECT cadence_shorts_per_day, cadence_longform_per_day, "
            "cadence_schedule_json FROM strategy_profiles"
        ).fetchone()
    assert (row[0], row[1]) == (3, 1)
    assert json.loads(row[2])["time_zone"] == "Asia/Ho_Chi_Minh"
    assert json.loads(row[2])["shorts"]["weekdays"] == ["monday", "wednesday", "friday"]


def test_migration_0005_keeps_old_cadences_as_utc_defaults(tmp_path: Path) -> None:
    path = tmp_path / "app.db"
    migrate(path, migrations=default_migrations()[:4])
    database = Database(path)
    channel = make_channel()
    profile = make_strategy_profile(channel, cadence=Cadence(4, 1))
    row = _strategy_row(profile)
    del row["cadence_schedule_json"]
    with database.transaction() as connection:
        ChannelRepository(connection).add(channel)
        StrategyProfileRepository(connection)._insert("strategy_profiles", row)

    report = migrate(path)

    assert report.applied == (5,)
    with database.transaction() as connection:
        stored = StrategyProfileRepository(connection).get(profile.id)
    assert stored == profile
    assert stored.cadence == Cadence(4, 1, "UTC")


def test_there_is_no_schedule_without_limits(database: Database) -> None:
    channel = make_channel()
    profile = make_strategy_profile(channel, cadence=None)
    with database.transaction() as connection:
        ChannelRepository(connection).add(channel)
        StrategyProfileRepository(connection).add(profile)

    with pytest.raises(Exception, match="CHECK"), database.transaction() as conn:
        conn.execute("UPDATE strategy_profiles SET cadence_schedule_json = '{}'")


# Service


class Clock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> datetime:
        self.now += timedelta(seconds=1)
        return self.now


def test_setting_the_cadence_touches_nothing_else(database: Database) -> None:
    sink = InMemoryAuditSink()
    service = StrategySettings(database, AuditLog(sink), clock=Clock())
    channel = make_channel()
    full = make_strategy_profile(channel)
    with database.transaction() as connection:
        ChannelRepository(connection).add(channel)
        StrategyProfileRepository(connection).add(full)

    change = service.set_cadence(
        channel.id, FULL, expected_version=full.version, actor=USER
    )

    stored = service.get(channel.id)
    assert stored == change.profile
    assert stored.cadence == FULL
    for name in SETTING_TYPES:
        if name != "cadence":
            assert getattr(stored, name) == getattr(full, name)
    [event] = sink.events()
    assert event.action == "strategy.cadence_changed"
    assert json.loads(event.metadata["to"])["time_zone"] == "Asia/Ho_Chi_Minh"
    assert json.loads(event.metadata["from"])["shorts_per_day"] == 2


# HTTP API


@pytest.fixture
def client(tmp_path: Path):
    settings = Settings(environment=Environment.TEST, database_path=tmp_path / "a.db")
    container = build_container(settings)
    with TestClient(create_app(container)) as test_client:
        test_client.sink = container.resolve(AuditSink)
        test_client.flags = container.resolve(FeatureFlags)
        yield test_client


def cadence_url(client: TestClient) -> str:
    response = client.post(
        "/channels", json={"title": "Money", "youtube_channel_id": "UC" + "c" * 22}
    )
    return f"/channels/{response.json()['id']}/strategy/cadence"


def test_put_cadence_creates_and_replaces(client: TestClient) -> None:
    url = cadence_url(client)
    assert client.flags.longform_enabled is False

    created = client.put(url, json={"shorts_per_day": 2, "longform_per_day": 1})
    replaced = client.put(
        url,
        json={
            "shorts_per_day": 3,
            "longform_per_day": 0,
            "time_zone": " asia/HO_CHI_MINH ",
            "shorts_schedule": {
                "weekdays": ["friday", "monday", "wednesday"],
                "times": ["18:30", " 07:00 "],
                "min_gap_minutes": 120,
            },
            "expected_version": 1,
        },
    )

    assert created.status_code == 201
    every_day = [day.value for day in Weekday]
    default = {"weekdays": every_day, "times": [], "min_gap_minutes": 0}
    assert created.json()["cadence"] == {
        "shorts_per_day": 2,
        "longform_per_day": 1,
        "time_zone": "UTC",
        "shorts_schedule": default,
        "longform_schedule": default,
    }
    assert replaced.status_code == 200
    cadence = replaced.json()["cadence"]
    assert cadence["time_zone"] == "Asia/Ho_Chi_Minh"
    assert cadence["shorts_schedule"] == {
        "weekdays": ["monday", "wednesday", "friday"],
        "times": ["07:00", "18:30"],
        "min_gap_minutes": 120,
    }
    assert cadence["longform_schedule"] == default
    assert [e.action for e in client.sink.events()][-1] == "strategy.cadence_changed"


LIMITS = {"shorts_per_day": 1, "longform_per_day": 0}


@pytest.mark.parametrize(
    "body, field",
    [
        ({"longform_per_day": 0}, "shorts_per_day"),
        ({"shorts_per_day": 1}, "longform_per_day"),
        ({**LIMITS, "shorts_per_day": 21}, "shorts_per_day"),
        ({**LIMITS, "longform_per_day": 6}, "longform_per_day"),
        ({**LIMITS, "shorts_per_day": "2"}, "shorts_per_day"),
        ({**LIMITS, "shorts_per_day": 1.5}, "shorts_per_day"),
        ({**LIMITS, "time_zone": "Mars/Olympus"}, "time_zone"),
        ({**LIMITS, "time_zone": "+07:00"}, "time_zone"),
        ({**LIMITS, "shorts_schedule": {"weekdays": []}}, "shorts_schedule"),
        ({**LIMITS, "shorts_schedule": {"weekdays": ["funday"]}}, "shorts_schedule"),
        (
            {**LIMITS, "shorts_schedule": {"weekdays": ["monday", "monday"]}},
            "shorts_schedule",
        ),
        ({**LIMITS, "shorts_schedule": {"times": ["25:00"]}}, "shorts_schedule"),
        ({**LIMITS, "shorts_schedule": {"times": ["7pm"]}}, "shorts_schedule"),
        (
            {**LIMITS, "longform_schedule": {"times": ["09:00", "09:00"]}},
            "longform_schedule",
        ),
        (
            {
                **LIMITS,
                "longform_schedule": {
                    "times": ["01:00", "02:00", "03:00", "04:00", "05:00", "06:00"]
                },
            },
            "longform_schedule",
        ),
        (
            {**LIMITS, "longform_schedule": {"min_gap_minutes": 1441}},
            "longform_schedule",
        ),
        ({**LIMITS, "shorts_schedule": {"timezone": "UTC"}}, "shorts_schedule"),
        ({**LIMITS, "budget": {}}, "budget"),
    ],
)
def test_put_cadence_validation(client: TestClient, body: dict, field: str) -> None:
    response = client.put(cadence_url(client), json=body)

    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "validation.invalid_request"
    assert field in [f["field"].split(".")[0] for f in error["fields"]]


def test_put_cadence_conflict(client: TestClient) -> None:
    url = cadence_url(client)
    client.put(url, json=LIMITS)

    stale = client.put(url, json=LIMITS)

    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "domain.strategy_conflict"
