"""G-076 Asset Registry: registering assets and attaching them to content items.

Rules the user approved on 2026-10-04:

- a service over the ``Asset`` entity with a migration (0023) and repositories,
  registered in the bootstrap, and no HTTP route;
- an asset is used by content items of its own channel (many to many, one
  usage per pair); a repeat attach returns the stored usage and writes nothing;
- the first attach of an asset to an item creates, in the same transaction, a
  ``RightsRecord`` (``asset_ref`` is ``Asset.id``, risk unknown, unresolved)
  unless the item already has a record for that asset, which stays untouched;
- duplicates in a channel: the same normalised source and title (or the same
  artifact of a generated asset) is one asset: an equal request returns it,
  a different one is a conflict (409);
- audit after commit with ids and counts only, never text.
"""

import dataclasses
import json
import sqlite3
import unicodedata
from datetime import UTC, datetime, timedelta
from http import HTTPStatus
from pathlib import Path

import pytest

from ai_youtube_agent.bootstrap import build_container
from ai_youtube_agent.content.asset import Asset, AssetCategory, AssetKind
from ai_youtube_agent.content.asset_registry import (
    AssetConflictError,
    AssetInputError,
    AssetNotFoundError,
    AssetRegistry,
    normalise_key,
)
from ai_youtube_agent.content.asset_usage import MAX_PURPOSE, AssetUsage
from ai_youtube_agent.content.channel_settings import ChannelNotFoundError
from ai_youtube_agent.content.hook_generator import ContentItemNotFoundError
from ai_youtube_agent.content.rights import RightsRecord, RiskLevel, RiskResolution
from ai_youtube_agent.core.artifact import Artifact
from ai_youtube_agent.core.audit import (
    Actor,
    ActorKind,
    AuditLog,
    AuditResult,
    InMemoryAuditSink,
)
from ai_youtube_agent.core.config import Environment, Settings
from ai_youtube_agent.core.content_item import ContentItem, ContentStatus
from ai_youtube_agent.core.db.database import Database
from ai_youtube_agent.core.db.repositories.asset import (
    AssetRepository,
    AssetUsageRepository,
)
from ai_youtube_agent.core.db.repositories.channel import (
    ChannelRepository,
    StrategyProfileRepository,
)
from ai_youtube_agent.core.db.repositories.content import (
    ArtifactRepository,
    ContentItemRepository,
)
from ai_youtube_agent.core.db.repositories.review import RightsRecordRepository
from ai_youtube_agent.core.gates import GateContext, GateOutcome
from ai_youtube_agent.core.rights_gate import RightsGate
from factories import (
    make_artifact,
    make_channel,
    make_content_item,
    make_strategy_profile,
)

T0 = datetime(2026, 10, 4, 12, 0, tzinfo=UTC)
USER = Actor(ActorKind.USER, "owner")
AI = Actor(ActorKind.AI, "mock/mock-1")
SYSTEM = Actor(ActorKind.SYSTEM, "pipeline")
SECRET = "zebrafish"  # a word no audit event may hold
IMAGE, MUSIC = AssetKind.IMAGE, AssetKind.MUSIC
GENERATED, LICENSED, PUBLIC_DOMAIN, USER_OWNED, UNKNOWN = (
    AssetCategory.GENERATED,
    AssetCategory.LICENSED,
    AssetCategory.PUBLIC_DOMAIN,
    AssetCategory.USER_OWNED,
    AssetCategory.UNKNOWN,
)


class Clock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> datetime:
        self.now += timedelta(seconds=1)
        return self.now


class World:
    def __init__(self, database: Database) -> None:
        self.database = database
        self.clock = Clock()
        self.versions = 0
        self.channel = make_channel()
        self.strategy = make_strategy_profile(self.channel)
        self.other_channel = make_channel()
        self.other_strategy = make_strategy_profile(self.other_channel)
        with database.transaction() as connection:
            channels = ChannelRepository(connection)
            strategies = StrategyProfileRepository(connection)
            channels.add(self.channel)
            strategies.add(self.strategy)
            channels.add(self.other_channel)
            strategies.add(self.other_strategy)
        self.item = self.new_item()
        self.other_item = self.new_item(other=True)
        self.sink = InMemoryAuditSink()
        self.registry = AssetRegistry(database, AuditLog(self.sink), clock=self.clock)

    def new_item(self, *, other: bool = False) -> ContentItem:
        channel, strategy = (
            (self.other_channel, self.other_strategy)
            if other
            else (self.channel, self.strategy)
        )
        item = make_content_item(channel, strategy)
        with self.database.transaction() as connection:
            ContentItemRepository(connection).add(item)
        return item

    def artifact(self, item: ContentItem | None = None) -> Artifact:
        self.versions += 1
        artifact = make_artifact(item or self.item, version=self.versions)
        with self.database.transaction() as connection:
            ArtifactRepository(connection).add(artifact)
        return artifact

    def register(self, **overrides) -> Asset:
        arguments = {
            "kind": IMAGE,
            "category": UNKNOWN,
            "title": "Logo",
            "source": "stock.example",
            "actor": USER,
        } | overrides
        channel_id = arguments.pop("channel_id", self.channel.id)
        kind = arguments.pop("kind")
        category = arguments.pop("category")
        return self.registry.register(channel_id, kind, category, **arguments)

    def licensed(self, **overrides) -> Asset:
        return self.register(category=LICENSED, license_ref="CC-BY-4.0", **overrides)

    def count(self, table: str) -> int:
        with self.database.transaction() as connection:
            return connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]

    def rows(self, *tables: str) -> list[tuple]:
        with self.database.transaction() as connection:
            return [
                tuple(row)
                for table in tables
                for row in connection.execute(f"SELECT * FROM {table} ORDER BY rowid")
            ]

    def records(self, item: ContentItem | None = None):
        with self.database.transaction() as connection:
            return RightsRecordRepository(connection).list_by_content_item(
                (item or self.item).id
            )

    def actions(self) -> list[str]:
        return [event.action for event in self.sink.events()]


@pytest.fixture
def world(database: Database) -> World:
    return World(database)


# normalise_key


@pytest.mark.parametrize(
    ("text", "key"),
    [
        ("Logo", "logo"),
        ("  LOGO\t", "logo"),
        ("Straße", "strasse"),
        ("İ", unicodedata.normalize("NFC", "i̇")),
    ],
)
def test_the_key_is_stripped_and_case_folded(text: str, key: str) -> None:
    assert normalise_key(text) == key


def test_the_key_is_the_same_for_nfc_and_nfd_text() -> None:
    nfc = unicodedata.normalize("NFC", "Hình nền Việt Nam")
    nfd = unicodedata.normalize("NFD", "HÌNH NỀN VIỆT NAM")

    assert nfc != nfd
    assert normalise_key(nfc) == normalise_key(nfd)
    assert unicodedata.is_normalized("NFC", normalise_key(nfd))


# AssetUsage entity


def usage(**overrides) -> AssetUsage:
    arguments = {
        "asset_id": "a1",
        "content_item_id": "c1",
        "attached_by": USER,
        "clock": lambda: T0,
    } | overrides
    return AssetUsage.create(
        arguments.pop("asset_id"), arguments.pop("content_item_id"), **arguments
    )


def test_a_usage_is_built_collapsed_and_frozen() -> None:
    made = usage(purpose="  B-roll \n  intro ")

    assert made.purpose == "B-roll intro"
    assert made.created_at == T0
    assert made.attached_by == USER
    assert made.as_dict() == {
        "id": made.id,
        "asset_id": "a1",
        "content_item_id": "c1",
        "purpose": "B-roll intro",
        "attached_by": {"kind": "user", "id": "owner"},
        "created_at": T0.isoformat(),
    }
    with pytest.raises(dataclasses.FrozenInstanceError):
        made.purpose = "x"  # type: ignore[misc]


def test_a_usage_without_a_purpose_keeps_none() -> None:
    assert usage().purpose is None


@pytest.mark.parametrize("purpose", ["", "   ", "x" * (MAX_PURPOSE + 1)])
def test_a_usage_purpose_is_one_to_200_characters(purpose: str) -> None:
    with pytest.raises(ValueError):
        usage(purpose=purpose)


def test_a_usage_purpose_of_200_characters_is_accepted() -> None:
    assert usage(purpose="p" * MAX_PURPOSE).purpose == "p" * MAX_PURPOSE


def test_a_usage_needs_ids_and_utc() -> None:
    with pytest.raises(ValueError):
        usage(asset_id=" ")
    with pytest.raises(ValueError):
        usage(content_item_id="")
    with pytest.raises(ValueError):
        usage(clock=lambda: datetime(2026, 10, 4))


def test_a_directly_built_usage_needs_a_collapsed_purpose() -> None:
    with pytest.raises(ValueError):
        AssetUsage("u", "a", "c", " x ", USER, T0)


# register: one asset per category


def test_a_generated_asset_is_registered_with_its_artifact(world: World) -> None:
    artifact = world.artifact()

    asset = world.register(
        category=GENERATED,
        source="mock-image",
        artifact_id=artifact.id,
        attribution="Made by the studio",
    )

    assert asset.channel_id == world.channel.id
    assert (asset.kind, asset.category) == (IMAGE, GENERATED)
    assert asset.artifact_id == artifact.id
    assert asset.created_at.tzinfo is not None
    assert world.registry.get(asset.id) == asset


def test_a_licensed_asset_is_registered(world: World) -> None:
    asset = world.licensed(kind=MUSIC, title="Calm  piano", attribution=" Lan ")

    assert asset.title == "Calm piano"
    assert asset.license_ref == "CC-BY-4.0"
    assert asset.attribution == "Lan"
    assert world.registry.get(asset.id) == asset


def test_a_public_domain_asset_is_registered(world: World) -> None:
    asset = world.register(category=PUBLIC_DOMAIN, source="archive.example")

    assert asset.category is PUBLIC_DOMAIN
    assert asset.license_ref is None
    assert world.registry.get(asset.id) == asset


def test_a_user_owned_asset_is_registered(world: World) -> None:
    asset = world.register(category=USER_OWNED, source="user", owner="Lan")

    assert (asset.owner, asset.source) == ("Lan", "user")
    assert world.registry.get(asset.id) == asset


def test_an_unknown_asset_needs_only_the_base_fields(world: World) -> None:
    asset = world.register()

    assert asset.category is UNKNOWN
    assert (asset.license_ref, asset.owner, asset.attribution) == (None, None, None)


@pytest.mark.parametrize("actor", [USER, AI, SYSTEM])
def test_any_actor_may_register_and_attach(world: World, actor: Actor) -> None:
    asset = world.register(actor=actor, title=f"By {actor.kind.value}")
    attached = world.registry.attach(asset.id, world.item.id, actor=actor)

    assert attached.attached_by == actor
    assert [event.actor for event in world.sink.events()] == [actor, actor]


def test_registering_writes_one_row_and_one_audit_event(world: World) -> None:
    asset = world.register()

    assert world.count("assets") == 1
    assert world.count("asset_usages") == 0
    assert world.count("rights_records") == 0
    (event,) = world.sink.events()
    assert event.action == "asset.registered"
    assert event.result is AuditResult.SUCCESS
    assert (event.entity.type, event.entity.id) == ("asset", asset.id)
    assert dict(event.metadata) == {
        "channel_id": world.channel.id,
        "kind": "image",
        "category": "unknown",
        "has_artifact": False,
    }


def test_an_audit_event_tells_when_there_is_an_artifact(world: World) -> None:
    artifact = world.artifact()
    world.register(category=GENERATED, source="mock", artifact_id=artifact.id)

    (event,) = world.sink.events()
    assert event.metadata["has_artifact"] is True


# register: validation


@pytest.mark.parametrize(
    ("field", "limit"),
    [("title", 200), ("source", 500), ("license_ref", 500), ("owner", 200)]
    + [("attribution", 500)],
)
def test_text_limits_are_inclusive(world: World, field: str, limit: int) -> None:
    fixed = {"category": LICENSED, "license_ref": "CC", "owner": "Lan"}
    ok = world.register(**(fixed | {field: "é" * limit}))
    assert getattr(ok, field) == "é" * limit

    too_long = fixed | {field: "é" * (limit + 1)}
    too_long.setdefault("title", "Other")
    with pytest.raises(AssetInputError) as caught:
        world.register(**too_long)
    assert caught.value.to_public().http_status == HTTPStatus.UNPROCESSABLE_ENTITY
    assert caught.value.code == "domain.asset_input"
    assert world.count("assets") == 1


@pytest.mark.parametrize(
    "overrides",
    [
        {"title": ""},
        {"title": "   \n"},
        {"source": ""},
        {"source": "  "},
        {"category": LICENSED, "license_ref": " "},
        {"category": USER_OWNED, "owner": ""},
        {"attribution": "  "},
    ],
)
def test_blank_text_is_rejected(world: World, overrides: dict) -> None:
    with pytest.raises(AssetInputError):
        world.register(**overrides)

    assert world.count("assets") == 0
    assert world.sink.events() == ()


@pytest.mark.parametrize(
    "overrides",
    [
        {"category": LICENSED},
        {"category": USER_OWNED},
        {"category": GENERATED, "source": "user"},
        {"category": GENERATED, "source": " USER "},
        {"category": PUBLIC_DOMAIN, "artifact_id": "some-artifact"},
        {"category": LICENSED, "license_ref": "CC", "artifact_id": "some-artifact"},
        {"category": UNKNOWN, "artifact_id": "some-artifact"},
        {"category": "licensed", "license_ref": "CC"},
        {"kind": "image"},
    ],
)
def test_category_rules_are_enforced_as_input_errors(
    world: World, overrides: dict
) -> None:
    with pytest.raises(AssetInputError) as caught:
        world.register(**overrides)

    assert caught.value.code == "domain.asset_input"
    assert world.count("assets") == 0
    assert world.sink.events() == ()


def test_the_channel_must_exist(world: World) -> None:
    with pytest.raises(ChannelNotFoundError) as caught:
        world.register(channel_id="missing")

    assert caught.value.to_public().http_status == HTTPStatus.NOT_FOUND
    assert world.count("assets") == 0
    assert world.sink.events() == ()


def test_an_artifact_must_exist(world: World) -> None:
    with pytest.raises(AssetInputError):
        world.register(category=GENERATED, source="mock", artifact_id="missing")

    assert world.count("assets") == 0


def test_an_artifact_of_another_channel_is_rejected(world: World) -> None:
    foreign = world.artifact(world.other_item)

    with pytest.raises(AssetInputError):
        world.register(category=GENERATED, source="mock", artifact_id=foreign.id)

    assert world.count("assets") == 0
    assert world.sink.events() == ()


# register: duplicates


def test_an_equal_request_returns_the_stored_asset(world: World) -> None:
    first = world.licensed(attribution="Lan")
    again = world.licensed(attribution="Lan")

    assert again == first
    assert world.count("assets") == 1
    assert world.actions() == ["asset.registered"]


@pytest.mark.parametrize(
    ("title", "source"),
    [
        ("LOGO", "stock.example"),
        ("  logo  ", "STOCK.EXAMPLE"),
        ("Logo", " stock.example "),
        ("Logo", "Stock.Example"),
    ],
)
def test_case_and_whitespace_variants_are_the_same_asset(
    world: World, title: str, source: str
) -> None:
    first = world.register()
    again = world.register(title=title, source=source)

    assert again.id == first.id
    assert again.title == "Logo"
    assert world.count("assets") == 1
    assert world.actions() == ["asset.registered"]


def test_inner_whitespace_runs_in_a_title_are_the_same_asset(world: World) -> None:
    first = world.register(title="Calm piano loop")
    again = world.register(title="calm   piano\tloop")

    assert again.id == first.id


def test_nfc_and_nfd_titles_are_the_same_asset(world: World) -> None:
    nfd = unicodedata.normalize("NFD", "Hình nền Việt")
    nfc = unicodedata.normalize("NFC", "HÌNH NỀN VIỆT")
    first = world.register(title=nfd)
    again = world.register(title=nfc)

    assert again.id == first.id
    assert world.count("assets") == 1


@pytest.mark.parametrize(
    "overrides",
    [
        {"kind": MUSIC},
        {"category": PUBLIC_DOMAIN},
        {"category": USER_OWNED, "owner": "Lan"},
        {"attribution": "someone"},
    ],
)
def test_a_different_asset_with_the_same_key_is_a_conflict(
    world: World, overrides: dict
) -> None:
    world.register()
    before = world.rows("assets")

    with pytest.raises(AssetConflictError) as caught:
        world.register(**overrides)

    assert caught.value.to_public().http_status == HTTPStatus.CONFLICT
    assert caught.value.code == "domain.asset_conflict"
    assert world.rows("assets") == before
    assert world.actions() == ["asset.registered"]


def test_a_different_licence_is_a_conflict(world: World) -> None:
    world.licensed()

    with pytest.raises(AssetConflictError):
        world.register(category=LICENSED, license_ref="CC-BY-SA-4.0")

    with pytest.raises(AssetConflictError):
        world.register(category=LICENSED, license_ref="cc-by-4.0")

    assert world.count("assets") == 1


def test_the_same_title_from_another_source_is_another_asset(world: World) -> None:
    first = world.register()
    other = world.register(source="other.example")

    assert other.id != first.id
    assert world.count("assets") == 2


def test_the_same_title_in_another_channel_is_another_asset(world: World) -> None:
    first = world.register()
    other = world.register(channel_id=world.other_channel.id)

    assert other.id != first.id
    assert other.channel_id == world.other_channel.id
    assert world.count("assets") == 2


def test_the_same_artifact_with_an_equal_request_is_the_same_asset(
    world: World,
) -> None:
    artifact = world.artifact()
    first = world.register(category=GENERATED, source="mock", artifact_id=artifact.id)
    again = world.register(
        category=GENERATED, source=" MOCK ", artifact_id=artifact.id, title="logo"
    )

    assert again.id == first.id
    assert world.count("assets") == 1
    assert world.actions() == ["asset.registered"]


def test_the_same_artifact_with_another_title_is_a_conflict(world: World) -> None:
    artifact = world.artifact()
    world.register(category=GENERATED, source="mock", artifact_id=artifact.id)

    with pytest.raises(AssetConflictError):
        world.register(
            category=GENERATED,
            source="mock",
            artifact_id=artifact.id,
            title="Another title",
        )

    assert world.count("assets") == 1


def test_a_key_of_one_asset_and_an_artifact_of_another_is_a_conflict(
    world: World,
) -> None:
    first_artifact, second_artifact = world.artifact(), world.artifact()
    world.register(
        category=GENERATED,
        source="mock",
        title="First",
        artifact_id=first_artifact.id,
    )
    world.register(
        category=GENERATED,
        source="mock",
        title="Second",
        artifact_id=second_artifact.id,
    )

    with pytest.raises(AssetConflictError):
        world.register(
            category=GENERATED,
            source="mock",
            title="First",
            artifact_id=second_artifact.id,
        )

    assert world.count("assets") == 2
    assert world.actions() == ["asset.registered", "asset.registered"]


def test_an_artifact_is_not_the_same_as_none(world: World) -> None:
    artifact = world.artifact()
    world.register(category=GENERATED, source="mock")

    with pytest.raises(AssetConflictError):
        world.register(category=GENERATED, source="mock", artifact_id=artifact.id)


# register: the unique constraint as a safety net


class SkipFirstLookup:
    """Makes ``find_by_key`` miss once, as if another writer committed after it."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.calls = 0
        real = AssetRepository.find_by_key

        def find(repository, *args):
            self.calls += 1
            if self.calls == 1:
                return None
            return real(repository, *args)

        monkeypatch.setattr(AssetRepository, "find_by_key", find)


def test_a_lost_race_returns_the_stored_equal_asset(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = world.register()
    lookup = SkipFirstLookup(monkeypatch)

    again = world.register()

    assert again == first
    assert lookup.calls == 2
    assert world.count("assets") == 1
    assert world.actions() == ["asset.registered"]


def test_a_lost_race_with_a_different_asset_is_a_conflict(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    world.register()
    SkipFirstLookup(monkeypatch)

    with pytest.raises(AssetConflictError):
        world.register(kind=MUSIC)

    assert world.count("assets") == 1


def test_an_integrity_error_with_no_stored_asset_is_raised(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(repository, asset, **keys):
        raise sqlite3.IntegrityError("something else")

    monkeypatch.setattr(AssetRepository, "add", refuse)

    with pytest.raises(sqlite3.IntegrityError, match="something else"):
        world.register()

    assert world.count("assets") == 0
    assert world.sink.events() == ()


# attach


def test_attach_creates_a_usage_and_a_rights_record(world: World) -> None:
    asset = world.licensed(title="Calm piano", source="music.example")

    made = world.registry.attach(
        asset.id, world.item.id, purpose="  Background \n music ", actor=USER
    )

    assert made.asset_id == asset.id
    assert made.content_item_id == world.item.id
    assert made.purpose == "Background music"
    assert made.attached_by == USER
    assert made.created_at.tzinfo is not None
    assert world.registry.usages_of(asset.id) == [made]
    (record,) = world.records()
    assert record.content_item_id == world.item.id
    assert record.asset_ref == asset.id
    assert record.source == "music.example"
    assert record.license == "CC-BY-4.0"
    assert record.risk_level is RiskLevel.UNKNOWN
    assert record.resolution is RiskResolution.UNRESOLVED
    assert (record.resolved_by, record.resolved_at) == (None, None)


def test_attach_without_a_licence_gives_a_record_without_one(world: World) -> None:
    asset = world.register(category=PUBLIC_DOMAIN, source="archive.example")

    made = world.registry.attach(asset.id, world.item.id, actor=SYSTEM)

    assert made.purpose is None
    (record,) = world.records()
    assert record.license is None
    assert record.source == "archive.example"


def test_attach_audits_ids_and_flags_only(world: World) -> None:
    asset = world.register()
    made = world.registry.attach(asset.id, world.item.id, purpose=SECRET, actor=AI)

    event = world.sink.events()[-1]
    (record,) = world.records()
    assert event.action == "asset.attached"
    assert event.actor == AI
    assert event.result is AuditResult.SUCCESS
    assert (event.entity.type, event.entity.id) == ("asset", asset.id)
    assert dict(event.metadata) == {
        "usage_id": made.id,
        "content_item_id": world.item.id,
        "rights_record_id": record.id,
        "rights_record_created": True,
    }


def test_a_repeat_attach_is_idempotent(world: World) -> None:
    asset = world.register()
    first = world.registry.attach(asset.id, world.item.id, purpose="Intro", actor=USER)
    rows = world.rows("asset_usages", "rights_records", "assets")
    events = world.actions()

    again = world.registry.attach(
        asset.id, world.item.id, purpose="Something else", actor=AI
    )

    assert again == first
    assert again.purpose == "Intro"
    assert world.rows("asset_usages", "rights_records", "assets") == rows
    assert world.actions() == events == ["asset.registered", "asset.attached"]
    assert len(world.records()) == 1


def test_attach_is_many_to_many_inside_one_channel(world: World) -> None:
    second_item = world.new_item()
    first, second = world.register(), world.register(title="Second")

    for asset in (first, second):
        for item in (world.item, second_item):
            world.registry.attach(asset.id, item.id, actor=USER)

    assert world.count("asset_usages") == 4
    assert [u.content_item_id for u in world.registry.usages_of(first.id)] == [
        world.item.id,
        second_item.id,
    ]
    assert world.registry.list_by_content_item(world.item.id) == [first, second]
    assert world.registry.list_by_content_item(second_item.id) == [first, second]
    assert {r.asset_ref for r in world.records(second_item)} == {first.id, second.id}
    assert world.count("rights_records") == 4


def test_an_asset_of_another_channel_cannot_be_attached(world: World) -> None:
    asset = world.register(channel_id=world.other_channel.id)

    with pytest.raises(AssetInputError) as caught:
        world.registry.attach(asset.id, world.item.id, actor=USER)

    assert caught.value.to_public().http_status == HTTPStatus.UNPROCESSABLE_ENTITY
    assert world.count("asset_usages") == 0
    assert world.count("rights_records") == 0
    assert world.actions() == ["asset.registered"]


def test_a_missing_asset_or_item_is_not_found(world: World) -> None:
    asset = world.register()

    with pytest.raises(AssetNotFoundError) as caught:
        world.registry.attach("missing", world.item.id, actor=USER)
    assert caught.value.to_public().http_status == HTTPStatus.NOT_FOUND
    assert caught.value.code == "domain.asset_not_found"
    with pytest.raises(ContentItemNotFoundError):
        world.registry.attach(asset.id, "missing", actor=USER)

    assert world.count("asset_usages") == 0
    assert world.count("rights_records") == 0


@pytest.mark.parametrize("purpose", ["", "   ", "p" * (MAX_PURPOSE + 1)])
def test_a_bad_purpose_is_rejected_and_writes_nothing(
    world: World, purpose: str
) -> None:
    asset = world.register()

    with pytest.raises(AssetInputError):
        world.registry.attach(asset.id, world.item.id, purpose=purpose, actor=USER)

    assert world.count("asset_usages") == 0
    assert world.count("rights_records") == 0


def test_a_purpose_of_200_characters_is_accepted(world: World) -> None:
    asset = world.register()

    made = world.registry.attach(
        asset.id, world.item.id, purpose="p" * MAX_PURPOSE, actor=USER
    )

    assert made.purpose == "p" * MAX_PURPOSE


# attach: the rights record


def test_an_existing_unresolved_record_is_left_untouched(world: World) -> None:
    asset = world.licensed()
    world.registry.attach(asset.id, world.item.id, actor=USER)
    second = world.new_item()
    # A record written by someone else (the risk engine) before the attach.
    existing = RightsRecord.create(
        second.id, asset.id, source="elsewhere", license="Other", clock=world.clock
    ).with_risk_level(RiskLevel.HIGH, clock=world.clock)
    with world.database.transaction() as connection:
        RightsRecordRepository(connection).add(existing)

    world.registry.attach(asset.id, second.id, actor=USER)

    assert world.records(second) == [existing]
    event = world.sink.events()[-1]
    assert event.metadata["rights_record_id"] == existing.id
    assert event.metadata["rights_record_created"] is False
    assert world.count("asset_usages") == 2


def test_a_record_a_user_resolved_is_left_untouched(world: World) -> None:
    asset = world.register()
    resolved = (
        RightsRecord.create(world.item.id, asset.id, source="x", clock=world.clock)
        .with_risk_level(RiskLevel.MEDIUM, clock=world.clock)
        .resolve(actor=USER, clock=world.clock)
    )
    with world.database.transaction() as connection:
        RightsRecordRepository(connection).add(resolved)

    world.registry.attach(asset.id, world.item.id, actor=SYSTEM)

    assert world.records() == [resolved]
    assert world.records()[0].is_resolved
    assert world.count("rights_records") == 1


def test_a_record_for_another_asset_does_not_count(world: World) -> None:
    asset = world.register()
    other = RightsRecord.create(
        world.item.id, "an-external-ref", source="x", clock=world.clock
    )
    with world.database.transaction() as connection:
        RightsRecordRepository(connection).add(other)

    world.registry.attach(asset.id, world.item.id, actor=USER)

    assert {r.asset_ref for r in world.records()} == {"an-external-ref", asset.id}


def publishing_context(item: ContentItem) -> GateContext:
    approved = dataclasses.replace(item, status=ContentStatus.APPROVED)
    return GateContext(approved, ContentStatus.PUBLISHING, USER, T0)


def test_the_rights_gate_blocks_the_new_record_until_a_user_resolves_it(
    world: World,
) -> None:
    asset = world.licensed()
    world.registry.attach(asset.id, world.item.id, actor=SYSTEM)
    context = publishing_context(world.item)

    with world.database.transaction() as connection:
        blocked = RightsGate(RightsRecordRepository(connection)).evaluate(context)
    assert blocked.outcome is GateOutcome.BLOCK
    assert [reason.code for reason in blocked.reasons] == ["rights.unresolved_unknown"]
    assert asset.id in blocked.reasons[0].message

    (record,) = world.records()
    with world.database.transaction() as connection:
        RightsRecordRepository(connection).update(
            record.resolve(actor=USER, clock=world.clock),
            expected_updated_at=record.updated_at,
        )
    with world.database.transaction() as connection:
        passed = RightsGate(RightsRecordRepository(connection)).evaluate(context)
    assert passed.outcome is GateOutcome.PASS


def test_a_failing_rights_insert_rolls_the_usage_back(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    asset = world.register()

    def boom(repository, record):
        raise RuntimeError("rights store down")

    monkeypatch.setattr(RightsRecordRepository, "add", boom)
    with pytest.raises(RuntimeError, match="rights store down"):
        world.registry.attach(asset.id, world.item.id, actor=USER)
    monkeypatch.undo()

    assert world.count("asset_usages") == 0
    assert world.count("rights_records") == 0
    assert world.actions() == ["asset.registered"]
    # The attach still works once the store is back.
    world.registry.attach(asset.id, world.item.id, actor=USER)
    assert world.count("asset_usages") == 1
    assert world.count("rights_records") == 1


# reads


def test_reads_return_assets_in_order_and_write_nothing(world: World) -> None:
    third = world.register(title="Third", source="b")
    first = world.register(title="First", source="a")
    other = world.register(channel_id=world.other_channel.id)
    world.registry.attach(first.id, world.item.id, actor=USER)
    world.registry.attach(third.id, world.item.id, actor=USER)
    before = world.rows("assets", "asset_usages", "rights_records")
    events = world.actions()

    assert world.registry.get(first.id) == first
    assert world.registry.list_by_channel(world.channel.id) == [third, first]
    assert world.registry.list_by_channel(world.other_channel.id) == [other]
    assert world.registry.list_by_content_item(world.item.id) == [first, third]
    assert world.registry.list_by_content_item(world.other_item.id) == []
    assert [u.asset_id for u in world.registry.usages_of(first.id)] == [first.id]
    assert world.registry.usages_of(other.id) == []

    assert world.rows("assets", "asset_usages", "rights_records") == before
    assert world.actions() == events


def test_reads_of_missing_things_are_not_found(world: World) -> None:
    with pytest.raises(AssetNotFoundError):
        world.registry.get("missing")
    with pytest.raises(AssetNotFoundError):
        world.registry.usages_of("missing")
    with pytest.raises(ChannelNotFoundError):
        world.registry.list_by_channel("missing")
    with pytest.raises(ContentItemNotFoundError):
        world.registry.list_by_content_item("missing")


# text, persistence, audit


def test_vietnamese_text_is_stored_verbatim(world: World) -> None:
    title = unicodedata.normalize("NFD", "Nhạc nền  Việt Nam")
    source = "Thư viện âm thanh"

    asset = world.register(
        kind=MUSIC,
        category=LICENSED,
        title=title,
        source=source,
        license_ref="Giấy phép CC-BY 4.0",
        attribution="Nhạc: Nguyễn Văn A",
    )

    assert asset.title == " ".join(title.split())
    assert unicodedata.is_normalized("NFD", asset.title)
    stored = world.registry.get(asset.id)
    assert stored == asset
    assert (stored.source, stored.license_ref, stored.attribution) == (
        source,
        "Giấy phép CC-BY 4.0",
        "Nhạc: Nguyễn Văn A",
    )


def test_assets_and_usages_survive_a_new_database_and_registry(
    world: World, database: Database
) -> None:
    asset = world.licensed(attribution="Lan")
    made = world.registry.attach(asset.id, world.item.id, purpose="Intro", actor=USER)

    reopened = AssetRegistry(Database(database.path), AuditLog(InMemoryAuditSink()))

    assert reopened.get(asset.id) == asset
    assert reopened.list_by_channel(world.channel.id) == [asset]
    assert reopened.usages_of(asset.id) == [made]
    assert reopened.attach(asset.id, world.item.id, actor=AI) == made
    assert (
        reopened.register(
            world.channel.id,
            IMAGE,
            LICENSED,
            title="logo",
            source="STOCK.example",
            license_ref="CC-BY-4.0",
            attribution="Lan",
            actor=AI,
        )
        == asset
    )


def test_no_audit_event_holds_a_title_a_source_or_a_licence(world: World) -> None:
    asset = world.register(
        category=LICENSED,
        title=f"The {SECRET} title",
        source=f"{SECRET}.example",
        license_ref=f"{SECRET} licence",
        attribution=f"{SECRET} credit",
        owner=f"{SECRET} owner",
    )
    world.registry.attach(asset.id, world.item.id, purpose=f"{SECRET} use", actor=USER)

    events = world.sink.events()
    assert len(events) == 2
    dumped = json.dumps([event.as_dict() for event in events], ensure_ascii=False)
    assert SECRET not in dumped
    assert world.count("rights_records") == 1


def test_the_stored_keys_are_the_normalised_text(world: World) -> None:
    world.register(title="  Hình  NỀN ", source=" Stock.Example ")

    with world.database.transaction() as connection:
        row = connection.execute("SELECT source_key, title_key FROM assets").fetchone()
    assert row == (
        "stock.example",
        unicodedata.normalize("NFC", "hình nền"),
    )


def test_repositories_find_assets_by_key_and_artifact(world: World) -> None:
    artifact = world.artifact()
    asset = world.register(category=GENERATED, source="mock", artifact_id=artifact.id)

    with world.database.transaction() as connection:
        assets = AssetRepository(connection)
        assert assets.find_by_key(world.channel.id, "mock", "logo") == asset
        assert assets.find_by_key(world.other_channel.id, "mock", "logo") is None
        assert assets.get_by_artifact(artifact.id) == asset
        assert assets.get_by_artifact("missing") is None
        assert assets.get("missing") is None
        usages = AssetUsageRepository(connection)
        assert usages.get_by_pair(asset.id, world.item.id) is None
        assert usages.list_by_asset(asset.id) == []
        assert usages.list_by_content_item(world.item.id) == []


# bootstrap


def test_bootstrap_registers_the_asset_registry(tmp_path: Path) -> None:
    container = build_container(
        Settings(environment=Environment.TEST, database_path=tmp_path / "a.db")
    )

    assert isinstance(container.resolve(AssetRegistry), AssetRegistry)
