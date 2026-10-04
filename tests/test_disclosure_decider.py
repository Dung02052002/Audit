"""G-081 AI Disclosure Decider: deciding and storing the disclosure of an item.

Rules the user approved on 2026-10-04:

- a service with an entity, rules, a migration (0026) and a repository, registered
  in the bootstrap, and no HTTP route; no gate is changed;
- deterministic rules (``disclosure-rules-v1``) over the facts the caller declares
  and the generated image and video assets attached to the item, run only through
  the G-080 policy checker; a triggered rule gives REQUIRED, never a block;
- the history is append only; a decision equal to the newest one (version,
  decision, rationale, sources) writes and audits nothing, whoever asks;
- the clock is read inside the BEGIN IMMEDIATE transaction and must not go
  backwards; an unknown rule set reads no clock and does not touch the database;
- ``disclosure.decided`` is audited after commit, only for a new row, with scalars;
- no title, URL, licence or text reaches a row, an event or an error.
"""

import dataclasses
import json
import sqlite3
from datetime import UTC, datetime, timedelta, timezone
from http import HTTPStatus
from pathlib import Path

import pytest

import ai_youtube_agent.content.disclosure_decider as decider_module
from ai_youtube_agent.bootstrap import build_container
from ai_youtube_agent.content.asset import Asset, AssetCategory, AssetKind
from ai_youtube_agent.content.asset_registry import AssetRegistry
from ai_youtube_agent.content.disclosure import (
    FACT_NAMES,
    DisclosureDecision,
    DisclosureRecord,
)
from ai_youtube_agent.content.disclosure_decider import DisclosureDecider
from ai_youtube_agent.content.disclosure_rule import (
    DISCLOSURE_RULES,
    DisclosureFacts,
    DisclosureRuleCatalog,
    DisclosureRuleSetNotFoundError,
    RealisticPersonRule,
    default_disclosure_catalog,
)
from ai_youtube_agent.content.hook_generator import ContentItemNotFoundError
from ai_youtube_agent.content.policy_rule import PolicyRuleError, RuleResult, RuleSet
from ai_youtube_agent.content.provenance_recorder import ProvenanceRecorder
from ai_youtube_agent.core.audit import (
    Actor,
    ActorKind,
    AuditError,
    AuditEvent,
    AuditLog,
    InMemoryAuditSink,
)
from ai_youtube_agent.core.config import Environment, Settings
from ai_youtube_agent.core.content_item import ContentItem, ContentStatus
from ai_youtube_agent.core.db.database import Database
from ai_youtube_agent.core.db.repositories.asset import AssetRepository
from ai_youtube_agent.core.db.repositories.channel import (
    ChannelRepository,
    StrategyProfileRepository,
)
from ai_youtube_agent.core.db.repositories.content import (
    ArtifactRepository,
    ContentItemRepository,
)
from ai_youtube_agent.core.db.repositories.disclosure import DisclosureRepository
from ai_youtube_agent.core.errors import DomainError
from ai_youtube_agent.core.publish_gate import PublishGate
from factories import (
    make_artifact,
    make_channel,
    make_content_item,
    make_strategy_profile,
)

T0 = datetime(2026, 10, 4, 12, 0, tzinfo=UTC)
START = T0 + timedelta(hours=1)  # the decider clock starts after the World clock
USER = Actor(ActorKind.USER, "owner")
SYSTEM = Actor(ActorKind.SYSTEM, "pipeline")
AI = Actor(ActorKind.AI, "mock/mock-1")
SECRET = "zebrafish"  # a word no row, event or error may hold
GENERATED, LICENSED = AssetCategory.GENERATED, AssetCategory.LICENSED
IMAGE, VIDEO, AUDIO = AssetKind.IMAGE, AssetKind.VIDEO_CLIP, AssetKind.AUDIO
RULE_IDS = [
    "disclosure.realistic_person",
    "disclosure.realistic_event",
    "disclosure.synthetic_voice_of_real_person",
    "disclosure.generated_realistic_visual",
]
COLUMNS = [
    "id",
    "content_item_id",
    "channel_id",
    "rule_set_id",
    "rule_set_version",
    "decision",
    "rationale_json",
    "sources_json",
    "decided_by_kind",
    "decided_by_id",
    "created_at",
]
AUDIT_KEYS = {
    "decision_id",
    "channel_id",
    "rule_set_id",
    "rule_set_version",
    "decision",
    "realistic_person",
    "realistic_event",
    "synthetic_voice_of_real_person",
    "realistic_visual",
    "generated_visual_count",
}


def facts(**overrides) -> DisclosureFacts:
    return DisclosureFacts(**(dict.fromkeys(FACT_NAMES, False) | overrides))


NONE = facts()
PERSON = facts(realistic_person=True)
EVENT = facts(realistic_event=True)


class Tick:
    """A clock that moves one second at each call."""

    def __init__(self, start: datetime = START) -> None:
        self.now = start
        self.calls = 0

    def __call__(self) -> datetime:
        self.calls += 1
        self.now += timedelta(seconds=1)
        return self.now


class ProbeClock(Tick):
    """A clock that reports whether another writer could start when it is read."""

    def __init__(self, path: Path) -> None:
        super().__init__()
        self.path = path
        self.locked: list[bool] = []

    def __call__(self) -> datetime:
        self.locked.append(is_write_locked(self.path))
        return super().__call__()


def is_write_locked(path: Path) -> bool:
    other = sqlite3.connect(path, timeout=0, isolation_level=None)
    try:
        other.execute("BEGIN IMMEDIATE")
        other.execute("ROLLBACK")
        return False
    except sqlite3.OperationalError:
        return True
    finally:
        other.close()


class WorldClock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> datetime:
        self.now += timedelta(seconds=1)
        return self.now


class World:
    def __init__(self, database: Database) -> None:
        self.database = database
        self.clock = Tick()
        self.versions = 0
        self.titles = 0
        self.channel = make_channel()
        self.strategy = make_strategy_profile(self.channel)
        with database.transaction() as connection:
            ChannelRepository(connection).add(self.channel)
            StrategyProfileRepository(connection).add(self.strategy)
        self.item = self.new_item()
        self.sink = InMemoryAuditSink()
        self.audit = AuditLog(self.sink)
        world_clock = WorldClock()
        self.registry = AssetRegistry(database, self.audit, clock=world_clock)
        self.recorder = ProvenanceRecorder(database, self.audit, clock=world_clock)
        self.decider = DisclosureDecider(database, self.audit, clock=self.clock)

    def new_item(self, **overrides) -> ContentItem:
        item = dataclasses.replace(
            make_content_item(self.channel, self.strategy), **overrides
        )
        with self.database.transaction() as connection:
            ContentItemRepository(connection).add(item)
        return item

    def asset(
        self,
        category: AssetCategory = GENERATED,
        kind: AssetKind = IMAGE,
        item: ContentItem | None = None,
        **overrides,
    ) -> Asset:
        """A registered asset attached to ``item`` (default: the main item)."""
        self.titles += 1
        arguments = {
            "title": f"Asset {self.titles}",
            "source": "stock.example",
            "actor": USER,
        } | overrides
        target = item or self.item
        if category is GENERATED:
            self.versions += 1
            artifact = make_artifact(target, version=self.versions)
            with self.database.transaction() as connection:
                ArtifactRepository(connection).add(artifact)
            arguments |= {
                "source": overrides.get("source", "mock-image"),
                "artifact_id": artifact.id,
            }
        if category is LICENSED:
            arguments.setdefault("license_ref", "CC-BY-4.0")
        asset = self.registry.register(self.channel.id, kind, category, **arguments)
        self.registry.attach(asset.id, target.id, actor=USER)
        return asset

    def decide(self, f=NONE, *, actor=SYSTEM, item=None, **kwargs):
        return self.decider.decide(
            (item or self.item).id,
            f,
            rule_set=kwargs.pop("rule_set", DISCLOSURE_RULES),
            actor=actor,
        )

    def count(self, table: str = "disclosure_decisions") -> int:
        with self.database.transaction() as connection:
            return connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]

    def rows(self, *tables: str) -> list[tuple]:
        with self.database.transaction() as connection:
            return [
                tuple(row)
                for table in tables or ("disclosure_decisions",)
                for row in connection.execute(f"SELECT * FROM {table} ORDER BY rowid")
            ]

    def events(self) -> list[AuditEvent]:
        return [e for e in self.sink.events() if e.action == "disclosure.decided"]

    def with_decider(self, **kwargs) -> DisclosureDecider:
        kwargs.setdefault("clock", self.clock)
        return DisclosureDecider(self.database, self.audit, **kwargs)


@pytest.fixture
def world(database: Database) -> World:
    return World(database)


class _Rule:
    rule_id = "custom.rule"
    version = 1

    def __init__(self, observation) -> None:
        self.observation = observation

    def evaluate(self, context) -> RuleResult:
        return RuleResult(self.rule_id, self.version, True, False, "c.ok", "ok")


class _BlockingRule(_Rule):
    rule_id = "custom.blocking"

    def evaluate(self, context) -> RuleResult:
        return RuleResult(self.rule_id, self.version, False, True, "c.block", "no")


V2 = RuleSet("disclosure", 2)


def catalog_with_v2() -> DisclosureRuleCatalog:
    catalog = default_disclosure_catalog()
    catalog.register(V2, [RealisticPersonRule])
    return catalog


# persistence


def test_decide_stores_the_exact_row(world: World) -> None:
    generated = world.asset(GENERATED, IMAGE)
    world.asset(LICENSED, IMAGE)  # attached but not generated

    record = world.decide(facts(realistic_person=True, realistic_visual=True))

    assert record.decision is DisclosureDecision.REQUIRED
    assert record.channel_id == world.item.channel_id == world.channel.id
    with world.database.transaction() as connection:
        cursor = connection.execute("SELECT * FROM disclosure_decisions")
        assert [d[0] for d in cursor.description] == COLUMNS
        (row,) = cursor.fetchall()
    assert row[:6] == (
        record.id,
        world.item.id,
        world.channel.id,
        "disclosure",
        "disclosure-rules-v1",
        "required",
    )
    assert row[8:10] == ("system", "pipeline")
    assert row[10] == record.created_at.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    assert record.created_at == START + timedelta(seconds=1)
    assert record.sources.facts == ("realistic_person", "realistic_visual")
    assert record.sources.asset_ids == (generated.id,)


def test_the_rationale_has_four_entries_in_order_with_exact_keys(
    world: World,
) -> None:
    world.asset(GENERATED, IMAGE)
    world.decide(facts(realistic_event=True, realistic_visual=True))

    with world.database.transaction() as connection:
        (record,) = DisclosureRepository(connection).list_by_content_item(world.item.id)
        raw = connection.execute(
            "SELECT rationale_json, sources_json FROM disclosure_decisions"
        ).fetchone()
    rationale = json.loads(raw[0])
    assert [e["rule_id"] for e in rationale] == RULE_IDS
    assert all(
        set(e) == {"rule_id", "version", "code", "message", "triggered"}
        for e in rationale
    )
    assert [e["triggered"] for e in rationale] == [False, True, False, True]
    assert [e["code"] for e in rationale] == [
        "disclosure.realistic_person.ok",
        "disclosure.realistic_event.required",
        "disclosure.synthetic_voice_of_real_person.ok",
        "disclosure.generated_realistic_visual.required",
    ]
    assert json.loads(raw[1]) == {
        "asset_ids": list(record.sources.asset_ids),
        "facts": ["realistic_event", "realistic_visual"],
    }


def test_a_decision_with_nothing_triggered_is_not_required(world: World) -> None:
    world.asset(GENERATED, IMAGE)

    record = world.decide(NONE)

    assert record.decision is DisclosureDecision.NOT_REQUIRED
    assert [e.triggered for e in record.rationale] == [False] * 4
    assert record.sources.facts == ()
    assert len(record.sources.asset_ids) == 1


@pytest.mark.parametrize("actor", [USER, SYSTEM, AI], ids=lambda a: a.kind.value)
def test_every_actor_kind_is_stored(world: World, actor: Actor) -> None:
    record = world.decide(PERSON, actor=actor)

    with world.database.transaction() as connection:
        stored = DisclosureRepository(connection).latest(world.item.id)
        raw = connection.execute(
            "SELECT decided_by_kind, decided_by_id FROM disclosure_decisions"
        ).fetchone()
    assert tuple(raw) == (actor.kind.value, actor.id)
    assert stored.decided_by == actor
    assert record.decided_by == actor


def test_the_repository_round_trips_the_record(world: World) -> None:
    world.asset(GENERATED, VIDEO)
    record = world.decide(facts(realistic_visual=True, realistic_person=True))

    with world.database.transaction() as connection:
        repository = DisclosureRepository(connection)
        assert repository.latest(world.item.id) == record
        assert repository.list_by_content_item(world.item.id) == [record]
    assert world.decider.latest(world.item.id) == record
    assert world.decider.history(world.item.id) == [record]


def test_the_repository_has_only_add_latest_and_list_by_content_item() -> None:
    public = {name for name in vars(DisclosureRepository) if not name.startswith("_")}

    assert public == {"table", "add", "latest", "list_by_content_item"}
    for name in ("update", "delete", "get", "remove"):
        assert not hasattr(DisclosureRepository, name)


# idempotency


def test_a_repeat_with_another_actor_returns_the_same_record(world: World) -> None:
    first = world.decide(PERSON, actor=USER)
    rows, events = world.rows(), len(world.sink.events())

    again = world.decide(PERSON, actor=AI)

    assert again == first
    assert again.id == first.id
    assert again.decided_by == USER
    assert world.rows() == rows
    assert len(world.sink.events()) == events
    assert len(world.events()) == 1


def test_a_changed_fact_gives_a_new_row(world: World) -> None:
    first = world.decide(PERSON)
    second = world.decide(facts(realistic_person=True, realistic_event=True))

    assert second.id != first.id
    assert world.count() == 2
    assert len(world.events()) == 2


def test_a_new_generated_visual_with_an_equal_decision_gives_a_new_row(
    world: World,
) -> None:
    first = world.decide(PERSON)
    generated = world.asset(GENERATED, IMAGE)

    second = world.decide(PERSON)

    assert second.id != first.id
    assert second.decision is first.decision
    assert second.sources.asset_ids == (generated.id,)
    assert world.count() == 2


def test_an_asset_that_is_not_a_generated_visual_changes_nothing(
    world: World,
) -> None:
    first = world.decide(PERSON)
    world.asset(LICENSED, IMAGE)
    world.asset(GENERATED, AUDIO)

    assert world.decide(PERSON) == first
    assert world.count() == 1


def test_an_injected_v2_rule_set_gives_a_new_row(world: World) -> None:
    decider = world.with_decider(catalog=catalog_with_v2())

    one = decider.decide(world.item.id, PERSON, rule_set=DISCLOSURE_RULES, actor=USER)
    two = decider.decide(world.item.id, PERSON, rule_set=V2, actor=USER)
    again = decider.decide(world.item.id, PERSON, rule_set=V2, actor=SYSTEM)

    assert (one.rule_set_version, two.rule_set_version) == (
        "disclosure-rules-v1",
        "disclosure-rules-v2",
    )
    assert again == two
    assert world.count() == 2
    assert decider.history(world.item.id) == [one, two]
    assert decider.latest(world.item.id) == two


def test_reads_never_consult_the_catalog(world: World) -> None:
    decider = world.with_decider(catalog=catalog_with_v2())
    one = decider.decide(world.item.id, PERSON, rule_set=DISCLOSURE_RULES, actor=USER)
    two = decider.decide(world.item.id, PERSON, rule_set=V2, actor=USER)
    only_v2 = DisclosureRuleCatalog()
    only_v2.register(V2, [RealisticPersonRule])

    reader = world.with_decider(catalog=only_v2)

    assert reader.history(world.item.id) == [one, two]
    assert reader.latest(world.item.id) == two
    with pytest.raises(DisclosureRuleSetNotFoundError):
        reader.decide(world.item.id, PERSON, rule_set=DISCLOSURE_RULES, actor=USER)


def test_a_b_a_gives_three_rows_and_the_old_rows_never_change(world: World) -> None:
    a1 = world.decide(PERSON)
    after_a = world.rows()
    b = world.decide(EVENT)
    after_b = world.rows()
    a2 = world.decide(PERSON)

    assert len({a1.id, b.id, a2.id}) == 3
    assert a1.content_key() == a2.content_key()
    assert world.count() == 3
    rows = world.rows()
    assert rows[:1] == after_a
    assert rows[:2] == after_b
    assert world.decider.latest(world.item.id) == a2
    assert world.decider.history(world.item.id) == [a1, b, a2]


def test_other_items_are_independent(world: World) -> None:
    other = world.new_item()
    first = world.decide(PERSON)
    second = world.decide(PERSON, item=other)

    assert second.id != first.id
    assert second.content_item_id == other.id
    assert world.count() == 2
    assert world.decide(PERSON, item=other) == second
    assert world.decider.history(world.item.id) == [first]
    assert world.decider.history(other.id) == [second]


def test_an_asset_of_another_item_is_not_a_source(world: World) -> None:
    other = world.new_item()
    world.asset(GENERATED, IMAGE, item=other)

    record = world.decide(facts(realistic_visual=True))

    assert record.sources.asset_ids == ()
    assert record.decision is DisclosureDecision.NOT_REQUIRED


# transaction and clock


def test_the_clock_and_the_reads_happen_inside_begin_immediate(
    database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = World(database)
    probe = ProbeClock(database.path)
    world.decider = world.with_decider(clock=probe)
    reads: list[bool] = []
    newest: list[bool] = []

    class Spy(AssetRepository):
        def list_by_content_item(self, content_item_id):
            reads.append(is_write_locked(database.path))
            return super().list_by_content_item(content_item_id)

    class RecordSpy(DisclosureRepository):
        def latest(self, content_item_id):
            newest.append(is_write_locked(database.path))
            return super().latest(content_item_id)

    monkeypatch.setattr(decider_module, "AssetRepository", Spy)
    monkeypatch.setattr(decider_module, "DisclosureRepository", RecordSpy)

    world.decide(PERSON)
    world.decide(PERSON)  # an idempotent repeat reads both too
    world.decide(EVENT)

    assert probe.locked == [True, True, True]
    assert reads == [True, True, True]
    assert newest == [True, True, True]


@pytest.mark.parametrize(
    "moment",
    [
        datetime(2026, 10, 4, 12, 0),
        datetime(2026, 10, 4, 12, 0, tzinfo=timezone(timedelta(hours=7))),
        "2026-10-04",
        None,
    ],
)
def test_a_clock_that_is_not_utc_is_a_plain_value_error(world: World, moment) -> None:
    decider = world.with_decider(clock=lambda: moment)

    with pytest.raises(ValueError, match="clock") as caught:
        decider.decide(world.item.id, PERSON, rule_set=DISCLOSURE_RULES, actor=USER)

    assert not isinstance(caught.value, DomainError)
    assert world.count() == 0
    assert world.sink.events() == ()


def test_a_backwards_clock_is_refused_even_for_equal_content(world: World) -> None:
    world.decide(PERSON)
    newest = world.decider.latest(world.item.id)
    earlier = world.with_decider(clock=lambda: newest.created_at - timedelta(seconds=1))
    rows, events = world.rows(), len(world.sink.events())

    for f in (PERSON, EVENT):  # equal content, then a new one
        with pytest.raises(ValueError, match="earlier") as caught:
            earlier.decide(world.item.id, f, rule_set=DISCLOSURE_RULES, actor=USER)
        assert not isinstance(caught.value, DomainError)
        assert world.item.id in str(caught.value)
        assert newest.id not in str(caught.value)

    assert world.rows() == rows
    assert len(world.sink.events()) == events


def test_an_equal_time_is_allowed(world: World) -> None:
    first = world.decide(PERSON)
    same_time = world.with_decider(clock=lambda: first.created_at)

    repeat = same_time.decide(
        world.item.id, PERSON, rule_set=DISCLOSURE_RULES, actor=AI
    )
    changed = same_time.decide(
        world.item.id, EVENT, rule_set=DISCLOSURE_RULES, actor=AI
    )

    assert repeat == first
    assert changed.created_at == first.created_at
    assert world.count() == 2
    assert world.decider.latest(world.item.id) == changed  # insertion order breaks ties


def test_an_unknown_rule_set_reads_no_clock_and_does_not_touch_the_database(
    world: World,
) -> None:
    clock = Tick()

    class NoDatabase:
        def transaction(self):
            raise AssertionError("the database must not be touched")

    decider = DisclosureDecider(NoDatabase(), world.audit, clock=clock)

    for rule_set in (V2, RuleSet("policy", 1), RuleSet("rights", 1)):
        with pytest.raises(DisclosureRuleSetNotFoundError) as caught:
            decider.decide("any-item", PERSON, rule_set=rule_set, actor=USER)
        assert caught.value.to_public().http_status == HTTPStatus.NOT_FOUND
    assert clock.calls == 0
    assert world.sink.events() == ()


# errors


def test_a_missing_item_is_a_404_for_decide_and_the_reads(world: World) -> None:
    clock_calls = world.clock.calls
    for call in (
        lambda: world.decider.decide(
            "missing", PERSON, rule_set=DISCLOSURE_RULES, actor=USER
        ),
        lambda: world.decider.latest("missing"),
        lambda: world.decider.history("missing"),
    ):
        with pytest.raises(ContentItemNotFoundError) as caught:
            call()
        assert caught.value.to_public().http_status == HTTPStatus.NOT_FOUND
        assert caught.value.code == "domain.content_item_not_found"

    assert world.count() == 0
    assert world.sink.events() == ()
    assert world.clock.calls == clock_calls + 1  # only decide reads the clock


@pytest.mark.parametrize(
    "arguments",
    [
        {"content_item_id": 1},
        {"content_item_id": None},
        {"facts": True},
        {"facts": {"realistic_person": True}},
        {"rule_set": ("disclosure", 1)},
        {"rule_set": "disclosure-rules-v1"},
        {"actor": "owner"},
        {"actor": None},
    ],
)
def test_a_wrong_argument_type_is_a_type_error_naming_the_argument(
    world: World, arguments
) -> None:
    call = {
        "content_item_id": world.item.id,
        "facts": PERSON,
        "rule_set": DISCLOSURE_RULES,
        "actor": USER,
    } | arguments
    name = next(iter(arguments))

    with pytest.raises(TypeError, match=name):
        world.decider.decide(
            call["content_item_id"],
            call["facts"],
            rule_set=call["rule_set"],
            actor=call["actor"],
        )

    assert world.count() == 0
    assert world.sink.events() == ()
    assert world.clock.calls == 0


def test_an_injected_blocking_rule_writes_nothing(world: World) -> None:
    catalog = DisclosureRuleCatalog()
    catalog.register(DISCLOSURE_RULES, [RealisticPersonRule, _BlockingRule])
    decider = world.with_decider(catalog=catalog)

    with pytest.raises(PolicyRuleError):
        decider.decide(world.item.id, PERSON, rule_set=DISCLOSURE_RULES, actor=USER)

    assert world.count() == 0
    assert world.sink.events() == ()


# audit


def test_one_audit_event_per_new_row_and_none_for_the_rest(world: World) -> None:
    world.decide(PERSON)
    world.decide(PERSON, actor=AI)  # idempotent
    world.decide(EVENT)
    with pytest.raises(ContentItemNotFoundError):
        world.decide(PERSON, item=dataclasses.replace(world.item, id="missing"))
    with pytest.raises(TypeError):
        world.decider.decide(world.item.id, True, rule_set=DISCLOSURE_RULES, actor=USER)

    assert len(world.events()) == 2
    assert world.count() == 2


def test_the_event_holds_exactly_the_scalar_metadata(world: World) -> None:
    world.asset(GENERATED, IMAGE)
    world.asset(GENERATED, VIDEO)
    record = world.decide(facts(realistic_person=True, realistic_visual=True), actor=AI)

    (event,) = world.events()
    assert event.actor == AI
    assert (event.entity.type, event.entity.id) == ("content_item", world.item.id)
    assert event.result.value == "success"
    assert set(event.metadata) == AUDIT_KEYS
    assert dict(event.metadata) == {
        "decision_id": record.id,
        "channel_id": world.channel.id,
        "rule_set_id": "disclosure",
        "rule_set_version": "disclosure-rules-v1",
        "decision": "required",
        "realistic_person": True,
        "realistic_event": False,
        "synthetic_voice_of_real_person": False,
        "realistic_visual": True,
        "generated_visual_count": 2,
    }
    assert all(isinstance(v, str | bool | int) for v in event.metadata.values())


def test_the_audit_is_recorded_after_the_commit(database: Database) -> None:
    seen: list[tuple[int, bool]] = []

    class CheckingSink(InMemoryAuditSink):
        def append(self, event) -> None:
            if event.action == "disclosure.decided":
                other = sqlite3.connect(database.path)
                try:
                    (count,) = other.execute(
                        "SELECT count(*) FROM disclosure_decisions"
                    ).fetchone()
                finally:
                    other.close()
                seen.append((count, is_write_locked(database.path)))
            super().append(event)

    world = World(database)
    world.audit = AuditLog(CheckingSink())
    world.decider = world.with_decider()

    world.decide(PERSON)

    assert seen == [(1, False)]  # the row is committed and the lock is released


def test_the_row_stays_when_the_audit_fails_after_the_commit(
    world: World,
) -> None:
    class FailingSink:
        def append(self, event) -> None:
            raise AuditError("the sink is down")

        def events(self):
            return ()

    decider = DisclosureDecider(
        world.database, AuditLog(FailingSink()), clock=world.clock
    )

    with pytest.raises(AuditError):
        decider.decide(world.item.id, PERSON, rule_set=DISCLOSURE_RULES, actor=USER)

    assert world.count() == 1
    assert world.decider.latest(world.item.id).decision is DisclosureDecision.REQUIRED


# leaks


def chain(error: BaseException) -> list[str]:
    texts, seen = [], set()
    current: BaseException | None = error
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        texts += [str(current), repr(current)]
        texts += [str(a) for a in current.args]
        current = current.__cause__ or current.__context__
    return texts


def test_no_input_text_reaches_a_row_an_event_or_an_error(database: Database) -> None:
    world = World(database)
    item = world.new_item(title=f"{SECRET} title token=abc")
    world.item = item
    generated = world.asset(
        GENERATED,
        IMAGE,
        title=f"{SECRET} image",
        source=f"mock-{SECRET}",
        attribution=f"credit {SECRET}",
        owner=f"owner {SECRET}",
    )
    licensed = world.registry.register(
        world.channel.id,
        IMAGE,
        LICENSED,
        title=f"{SECRET} licensed",
        source=f"https://stock.example/{SECRET}",
        license_ref=f"licence {SECRET} password=hunter2",
        attribution=f"credit {SECRET}",
        owner=f"owner {SECRET}",
        actor=USER,
    )
    world.registry.attach(licensed.id, item.id, purpose=f"purpose {SECRET}", actor=USER)
    world.recorder.record(
        generated.id, source_url=f"https://cdn.example/{SECRET}/x", actor=USER
    )
    errors: list[BaseException] = []

    required = world.decide(facts(realistic_person=True, realistic_visual=True))
    world.decide(NONE)
    world.decide(PERSON)
    catalog = DisclosureRuleCatalog()
    catalog.register(DISCLOSURE_RULES, [RealisticPersonRule, _BlockingRule])
    attempts = [
        lambda: DisclosureFacts(
            realistic_person=f"{SECRET} token=abc",  # type: ignore[arg-type]
            realistic_event=False,
            synthetic_voice_of_real_person=False,
            realistic_visual=False,
        ),
        lambda: world.with_decider(clock=lambda: datetime(2026, 1, 1)).decide(
            item.id, PERSON, rule_set=DISCLOSURE_RULES, actor=USER
        ),
        lambda: world.with_decider(clock=lambda: START - timedelta(days=9)).decide(
            item.id, EVENT, rule_set=DISCLOSURE_RULES, actor=USER
        ),
        lambda: world.with_decider(catalog=catalog).decide(
            item.id, PERSON, rule_set=DISCLOSURE_RULES, actor=USER
        ),
        lambda: world.decider.decide(item.id, PERSON, rule_set=V2, actor=USER),
    ]
    for attempt in attempts:
        with pytest.raises((TypeError, ValueError, DomainError)) as caught:
            attempt()
        errors.append(caught.value)

    dump = repr(world.rows("disclosure_decisions"))
    events = repr(
        [e.as_dict() for e in world.sink.events() if "disclosure" in e.action]
    )
    assert required.decision is DisclosureDecision.REQUIRED
    assert SECRET not in dump
    assert "hunter2" not in dump and "token=abc" not in dump
    assert SECRET not in events
    assert "hunter2" not in events and "token=abc" not in events
    assert len(errors) == len(attempts)
    for error in errors:
        for text in chain(error):
            assert SECRET not in text
            assert "token=abc" not in text and "hunter2" not in text


# reads


def test_the_reads_are_ordered_and_write_nothing(world: World) -> None:
    first = world.decide(PERSON)
    second = world.decide(EVENT)
    third = world.decide(PERSON)
    rows, events, calls = world.rows(), len(world.sink.events()), world.clock.calls

    assert world.decider.history(world.item.id) == [first, second, third]
    assert world.decider.latest(world.item.id) == third

    assert world.rows() == rows
    assert len(world.sink.events()) == events
    assert world.clock.calls == calls


def test_an_item_without_a_decision_reads_as_none_and_empty(world: World) -> None:
    assert world.decider.latest(world.item.id) is None
    assert world.decider.history(world.item.id) == []
    assert world.sink.events() == ()


# bootstrap and the publish gate


def test_bootstrap_registers_the_decider(tmp_path: Path) -> None:
    container = build_container(
        Settings(environment=Environment.TEST, database_path=tmp_path / "a.db")
    )

    assert isinstance(container.resolve(DisclosureDecider), DisclosureDecider)


def test_a_required_decision_does_not_change_the_publish_gate_verdict(
    world: World, database: Database
) -> None:
    approved = dataclasses.replace(world.item, status=ContentStatus.APPROVED)
    gate = PublishGate(
        database, Settings(environment=Environment.TEST, database_path=database.path)
    )
    before = gate.evaluate(approved, actor=USER, at=START)

    record = world.decide(facts(realistic_person=True))
    after = gate.evaluate(approved, actor=USER, at=START)

    assert record.decision is DisclosureDecision.REQUIRED
    assert after == before


def test_the_record_type_is_exported_by_the_decider(world: World) -> None:
    assert isinstance(world.decide(PERSON), DisclosureRecord)
