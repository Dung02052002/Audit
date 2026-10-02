"""D-044 Market Settings (Prompt Pack v8, prompt #044).

Rules the user approved on 2026-10-01:

- a strategy is configured one setting at a time: every setting may be unset,
  migration 0003 makes the columns nullable, and the daily limit and budget
  gates block when their setting is missing;
- the market is only the ISO 3166-1 alpha-2 country;
- setting the market changes nothing else in the strategy;
- ``PUT /channels/{id}/strategy/market`` with ``expected_version``; the first
  setting creates the profile; changes are audited after commit.
"""

import dataclasses
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ai_youtube_agent.bootstrap import build_container
from ai_youtube_agent.content.channel import ChannelArchivedError, ChannelStatus
from ai_youtube_agent.content.channel_settings import ChannelNotFoundError
from ai_youtube_agent.content.strategy import (
    SETTING_TYPES,
    Market,
    StrategyChangeNotAllowedError,
    StrategyProfile,
)
from ai_youtube_agent.content.strategy_settings import (
    StrategyConflictError,
    StrategyNotFoundError,
    StrategySettings,
)
from ai_youtube_agent.core.audit import (
    Actor,
    ActorKind,
    AuditLog,
    AuditSink,
    InMemoryAuditSink,
)
from ai_youtube_agent.core.budget_gate import BudgetGate
from ai_youtube_agent.core.config import Environment, Settings
from ai_youtube_agent.core.content_item import ContentItem, ContentStatus, ContentType
from ai_youtube_agent.core.daily_limit_gate import DailyLimitGate
from ai_youtube_agent.core.db.database import Database
from ai_youtube_agent.core.db.migrate import (
    Migration,
    MigrationError,
    connect,
    current_version,
    default_migrations,
    migrate,
)
from ai_youtube_agent.core.db.repositories.channel import (
    ChannelRepository,
    StrategyProfileRepository,
    _strategy_row,
)
from ai_youtube_agent.core.db.repositories.content import ContentItemRepository
from ai_youtube_agent.core.gates import GateContext
from ai_youtube_agent.main import create_app
from factories import make_channel, make_content_item, make_strategy_profile

T0 = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
USER = Actor(ActorKind.USER, "owner")
AI = Actor(ActorKind.AI, "assistant")


class Clock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> datetime:
        self.now += timedelta(seconds=1)
        return self.now


# Entity


def test_a_new_profile_may_have_only_a_market() -> None:
    profile = StrategyProfile.create("channel-1", market=Market("VN"), actor=USER)

    assert profile.market == Market("VN")
    assert profile.version == 1
    assert profile.missing_settings == tuple(n for n in SETTING_TYPES if n != "market")
    assert not profile.is_complete
    body = profile.as_dict()
    assert body["market"] == {"country": "VN"}
    assert all(body[name] is None for name in profile.missing_settings)


def test_a_full_profile_is_complete() -> None:
    profile = make_strategy_profile(make_channel())

    assert profile.missing_settings == ()
    assert profile.is_complete


def test_a_configured_setting_cannot_be_removed() -> None:
    profile = make_strategy_profile(make_channel())

    with pytest.raises(ValueError):
        profile.update(market=None, actor=USER)


def test_the_market_change_touches_nothing_else() -> None:
    profile = make_strategy_profile(make_channel())

    changed = profile.update(market=Market("US"), actor=USER)

    assert changed.market == Market("US")
    assert changed.version == profile.version + 1
    for name in SETTING_TYPES:
        if name != "market":
            assert getattr(changed, name) == getattr(profile, name)


# Persistence


def test_a_partial_profile_round_trips(database: Database) -> None:
    channel = make_channel()
    profile = StrategyProfile.create(channel.id, market=Market("VN"), actor=USER)
    with database.transaction() as connection:
        ChannelRepository(connection).add(channel)
        StrategyProfileRepository(connection).add(profile)

    with database.transaction() as connection:
        assert StrategyProfileRepository(connection).get(profile.id) == profile


@pytest.mark.parametrize(
    "column", ["cadence_longform_per_day", "budget_monthly_limit", "primary_language"]
)
def test_a_setting_is_stored_fully_or_not_at_all(
    database: Database, column: str
) -> None:
    channel = make_channel()
    profile = make_strategy_profile(channel)
    with database.transaction() as connection:
        ChannelRepository(connection).add(channel)
        StrategyProfileRepository(connection).add(profile)

    with pytest.raises(sqlite3.IntegrityError), database.transaction() as connection:
        connection.execute(f"UPDATE strategy_profiles SET {column} = NULL")


def test_migration_0003_keeps_existing_profiles_and_references(tmp_path: Path) -> None:
    path = tmp_path / "app.db"
    migrate(path, migrations=default_migrations()[:2])
    database = Database(path)
    channel = make_channel()
    # A profile from before #049 has no format; 0004 (D-049) adds that column.
    profile = make_strategy_profile(channel, format=None)
    item = make_content_item(channel, profile)
    row = {k: v for k, v in _strategy_row(profile).items() if k != "format_json"}
    with database.transaction() as connection:
        ChannelRepository(connection).add(channel)
        StrategyProfileRepository(connection)._insert("strategy_profiles", row)
        ContentItemRepository(connection).add(item)

    report = migrate(path)

    assert report.applied == (3, 4)
    assert report.backup_path is not None
    assert current_version(path) == 4
    with database.transaction() as connection:
        assert StrategyProfileRepository(connection).get(profile.id) == profile
        assert ContentItemRepository(connection).get(item.id) == item
    orphan = dataclasses.replace(item, id="orphan", strategy_profile_id="missing")
    with pytest.raises(sqlite3.IntegrityError), database.transaction() as connection:
        ContentItemRepository(connection).add(orphan)
    connection = connect(path)
    try:
        assert connection.execute("PRAGMA foreign_keys").fetchone() == (1,)
    finally:
        connection.close()


def test_a_migration_that_breaks_a_reference_is_rolled_back(tmp_path: Path) -> None:
    path = tmp_path / "app.db"
    base = Migration.from_text(
        "0001_base.sql",
        "CREATE TABLE parent (id TEXT PRIMARY KEY) STRICT;\n"
        "CREATE TABLE child (parent_id TEXT REFERENCES parent (id)) STRICT;\n",
    )
    migrate(path, migrations=[base])
    connection = connect(path)
    connection.execute("INSERT INTO parent VALUES ('p')")
    connection.execute("INSERT INTO child VALUES ('p')")
    connection.close()
    breaking = Migration.from_text("0002_breaking.sql", "DELETE FROM parent;\n")

    with pytest.raises(MigrationError, match="foreign key"):
        migrate(path, migrations=[base, breaking])

    assert current_version(path) == 1
    connection = connect(path)
    try:
        assert connection.execute("SELECT id FROM parent").fetchall() == [("p",)]
        assert connection.execute("PRAGMA foreign_keys").fetchone() == (1,)
    finally:
        connection.close()


# Gates read a missing setting as a block


class Fixed:
    def __init__(self, **values) -> None:
        for name, value in values.items():
            setattr(self, name, lambda *a, _v=value, **k: _v)


def gate_context(status: ContentStatus, target: ContentStatus) -> GateContext:
    item = ContentItem.create(
        "channel-1", "strategy-1", 1, ContentType.SHORTS, "Video", clock=lambda: T0
    )
    return GateContext(
        item=dataclasses.replace(item, status=status),
        target_status=target,
        actor=USER,
        at=T0,
    )


def test_the_daily_limit_gate_blocks_without_a_cadence() -> None:
    partial = StrategyProfile.create("channel-1", market=Market("VN"), actor=USER)
    gate = DailyLimitGate(
        Fixed(get_by_channel=partial), Fixed(count_production_starts=0)
    )

    result = gate.evaluate(gate_context(ContentStatus.DRAFT, ContentStatus.GENERATING))

    assert [r.code for r in result.reasons] == ["daily_limit.no_cadence"]


def test_the_budget_gate_blocks_without_a_budget() -> None:
    partial = StrategyProfile.create("channel-1", market=Market("VN"), actor=USER)
    gate = BudgetGate(Fixed(get_by_channel=partial), Fixed(list_by_channel=[]))

    result = gate.evaluate(gate_context(ContentStatus.DRAFT, ContentStatus.GENERATING))

    assert [r.code for r in result.reasons] == ["budget.no_budget"]


# Service


@pytest.fixture
def sink() -> InMemoryAuditSink:
    return InMemoryAuditSink()


@pytest.fixture
def service(database: Database, sink: InMemoryAuditSink) -> StrategySettings:
    return StrategySettings(database, AuditLog(sink), clock=Clock())


def stored_channel(database: Database, **overrides):
    channel = make_channel(**overrides)
    with database.transaction() as connection:
        ChannelRepository(connection).add(channel)
    return channel


def test_the_first_market_creates_the_profile(
    service: StrategySettings, database: Database, sink: InMemoryAuditSink
) -> None:
    channel = stored_channel(database)

    change = service.set_market(channel.id, "VN", expected_version=None, actor=USER)

    assert change.created
    assert change.profile.market == Market("VN")
    assert change.profile.version == 1
    assert service.get(channel.id) == change.profile
    events = sink.events()
    assert [e.action for e in events] == ["strategy.created", "strategy.market_changed"]
    assert dict(events[1].metadata) == {
        "channel_id": channel.id,
        "from": None,
        "to": "VN",
        "version": 1,
    }


def test_changing_the_market_leaves_every_other_setting(
    service: StrategySettings, database: Database, sink: InMemoryAuditSink
) -> None:
    channel = stored_channel(database)
    full = make_strategy_profile(channel)
    with database.transaction() as connection:
        StrategyProfileRepository(connection).add(full)

    change = service.set_market(
        channel.id, "US", expected_version=full.version, actor=USER
    )

    stored = service.get(channel.id)
    assert not change.created
    assert stored == change.profile
    assert stored.market == Market("US")
    assert stored.version == full.version + 1
    for name in SETTING_TYPES:
        if name != "market":
            assert getattr(stored, name) == getattr(full, name)
    [event] = sink.events()
    assert dict(event.metadata)["from"] == "VN"


def test_the_same_market_changes_nothing(
    service: StrategySettings, database: Database, sink: InMemoryAuditSink
) -> None:
    channel = stored_channel(database)
    first = service.set_market(channel.id, "VN", expected_version=None, actor=USER)

    again = service.set_market(channel.id, "VN", expected_version=1, actor=USER)

    assert again.profile == first.profile
    assert len(sink.events()) == 2


@pytest.mark.parametrize("version", [None, 2])
def test_a_wrong_version_is_a_conflict(
    service: StrategySettings, database: Database, version
) -> None:
    channel = stored_channel(database)
    first = service.set_market(channel.id, "VN", expected_version=None, actor=USER)

    with pytest.raises(StrategyConflictError):
        service.set_market(channel.id, "US", expected_version=version, actor=USER)
    assert service.get(channel.id) == first.profile


def test_a_version_without_a_profile_is_a_conflict(
    service: StrategySettings, database: Database
) -> None:
    channel = stored_channel(database)

    with pytest.raises(StrategyConflictError):
        service.set_market(channel.id, "VN", expected_version=1, actor=USER)


def test_unknown_and_archived_channels_are_refused(
    service: StrategySettings, database: Database
) -> None:
    archived = stored_channel(database, status=ChannelStatus.ARCHIVED)

    with pytest.raises(ChannelNotFoundError):
        service.set_market("missing", "VN", expected_version=None, actor=USER)
    with pytest.raises(ChannelArchivedError):
        service.set_market(archived.id, "VN", expected_version=None, actor=USER)
    with pytest.raises(ChannelNotFoundError):
        service.get("missing")
    with pytest.raises(StrategyNotFoundError):
        service.get(archived.id)


def test_only_a_user_may_set_the_market(
    service: StrategySettings, database: Database, sink: InMemoryAuditSink
) -> None:
    channel = stored_channel(database)

    with pytest.raises(StrategyChangeNotAllowedError):
        service.set_market(channel.id, "VN", expected_version=None, actor=AI)
    assert sink.events() == ()


# HTTP API


@pytest.fixture
def client(tmp_path: Path):
    settings = Settings(environment=Environment.TEST, database_path=tmp_path / "a.db")
    container = build_container(settings)
    with TestClient(create_app(container)) as test_client:
        test_client.sink = container.resolve(AuditSink)
        yield test_client


def new_channel(client: TestClient) -> str:
    response = client.post(
        "/channels", json={"title": "Money", "youtube_channel_id": "UC" + "x" * 22}
    )
    return response.json()["id"]


def test_put_market_creates_then_updates(client: TestClient) -> None:
    channel_id = new_channel(client)
    url = f"/channels/{channel_id}/strategy"

    before = client.get(url)
    created = client.put(f"{url}/market", json={"country": "vn"})
    updated = client.put(f"{url}/market", json={"country": "US", "expected_version": 1})

    assert before.status_code == 404
    assert before.json()["error"]["code"] == "domain.strategy_not_found"
    assert created.status_code == 201
    assert created.json()["market"] == {"country": "VN"}
    assert created.json()["missing_settings"] == [
        n for n in SETTING_TYPES if n != "market"
    ]
    assert updated.status_code == 200
    assert updated.json()["market"] == {"country": "US"}
    assert updated.json()["version"] == 2
    assert client.get(url).json() == updated.json()
    assert updated.json()["updated_by"] == {"kind": "user", "id": "local-user"}


@pytest.mark.parametrize(
    "body, field",
    [
        ({"country": "VNM"}, "country"),
        ({"country": "1A"}, "country"),
        ({}, "country"),
        ({"country": "VN", "expected_version": 0}, "expected_version"),
        ({"country": "VN", "languages": {"primary": "vi"}}, "languages"),
    ],
)
def test_put_market_validation(client: TestClient, body: dict, field: str) -> None:
    channel_id = new_channel(client)

    response = client.put(f"/channels/{channel_id}/strategy/market", json=body)

    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "validation.invalid_request"
    assert field in [f["field"] for f in error["fields"]]


def test_put_market_conflicts_and_unknown_channel(client: TestClient) -> None:
    channel_id = new_channel(client)
    url = f"/channels/{channel_id}/strategy/market"
    client.put(url, json={"country": "VN"})

    stale = client.put(url, json={"country": "US"})
    unknown = client.put("/channels/missing/strategy/market", json={"country": "VN"})

    assert (stale.status_code, stale.json()["error"]["code"]) == (
        409,
        "domain.strategy_conflict",
    )
    assert (unknown.status_code, unknown.json()["error"]["code"]) == (
        404,
        "domain.channel_not_found",
    )
    assert [e.action for e in client.sink.events()][-2:] == [
        "strategy.created",
        "strategy.market_changed",
    ]
