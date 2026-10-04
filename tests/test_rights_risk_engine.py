"""G-078 Rights Risk Engine: assessing the rights records of a content item.

Rules the user approved on 2026-10-04:

- a service with an entity, a migration (0025) and a repository, registered in
  the bootstrap, and no HTTP route; the shared publish gate is not changed;
- deterministic rules over the asset category and the current provenance, run
  on demand; the level goes into ``RightsRecord.risk_level`` and the record
  stays unresolved;
- a record a user resolved is skipped (no read, no row, no update);
- the history is append only; an outcome equal to the newest one writes nothing,
  a new provenance or a drifted record level gives a new row;
- ``changed`` counts the new rows; the audit holds counts only;
- a missing rights record on a read is a 404; blocking is the configured set of
  the rights gate.
"""

import dataclasses
import json
import sqlite3
from datetime import UTC, datetime, timedelta, timezone
from http import HTTPStatus
from pathlib import Path

import pytest

from ai_youtube_agent.bootstrap import build_container
from ai_youtube_agent.content.asset import Asset, AssetCategory, AssetKind
from ai_youtube_agent.content.asset_registry import AssetRegistry
from ai_youtube_agent.content.hook_generator import ContentItemNotFoundError
from ai_youtube_agent.content.provenance_recorder import ProvenanceRecorder
from ai_youtube_agent.content.rights import RightsRecord, RiskLevel, RiskResolution
from ai_youtube_agent.content.rights_assessment import RULES_VERSION
from ai_youtube_agent.content.rights_risk_engine import (
    RightsAssessmentRun,
    RightsRecordNotFoundError,
    RightsRiskEngine,
)
from ai_youtube_agent.core.audit import (
    Actor,
    ActorKind,
    AuditLog,
    InMemoryAuditSink,
)
from ai_youtube_agent.core.config import Environment, RightsBlockLevel, Settings
from ai_youtube_agent.core.content_item import ContentItem, ContentStatus
from ai_youtube_agent.core.db.database import ConcurrencyError, Database
from ai_youtube_agent.core.db.repositories.asset import AssetRepository
from ai_youtube_agent.core.db.repositories.channel import (
    ChannelRepository,
    StrategyProfileRepository,
)
from ai_youtube_agent.core.db.repositories.content import (
    ArtifactRepository,
    ContentItemRepository,
)
from ai_youtube_agent.core.db.repositories.provenance import ProvenanceRepository
from ai_youtube_agent.core.db.repositories.review import RightsRecordRepository
from ai_youtube_agent.core.db.repositories.rights_assessment import (
    RightsAssessmentRepository,
)
from ai_youtube_agent.core.gates import GateContext, GateName
from ai_youtube_agent.core.rights_gate import RightsGate, blocking_levels_for
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
FIXED = T0 + timedelta(hours=1)  # after every time the World clock gives
SECRET = "zebrafish"  # a word no audit event may hold
SHA = "cd" * 32
IMAGE = AssetKind.IMAGE
GENERATED, LICENSED, PUBLIC_DOMAIN, USER_OWNED, UNKNOWN = (
    AssetCategory.GENERATED,
    AssetCategory.LICENSED,
    AssetCategory.PUBLIC_DOMAIN,
    AssetCategory.USER_OWNED,
    AssetCategory.UNKNOWN,
)
LOW, MEDIUM, HIGH = RiskLevel.LOW, RiskLevel.MEDIUM, RiskLevel.HIGH


class Clock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> datetime:
        self.now += timedelta(seconds=1)
        return self.now


class RecordSource:
    """A ``RightsSource`` over the database for the rights gate."""

    def __init__(self, database: Database) -> None:
        self._database = database

    def list_by_content_item(self, content_item_id: str) -> list[RightsRecord]:
        with self._database.transaction() as connection:
            return RightsRecordRepository(connection).list_by_content_item(
                content_item_id
            )


class World:
    def __init__(self, database: Database) -> None:
        self.database = database
        self.clock = Clock()
        self.versions = 0
        self.titles = 0
        self.channel = make_channel()
        self.strategy = make_strategy_profile(self.channel)
        with database.transaction() as connection:
            ChannelRepository(connection).add(self.channel)
            StrategyProfileRepository(connection).add(self.strategy)
        self.item = self.new_item()
        self.sink = InMemoryAuditSink()
        audit = AuditLog(self.sink)
        self.registry = AssetRegistry(database, audit, clock=self.clock)
        self.recorder = ProvenanceRecorder(database, audit, clock=self.clock)
        self.engine = RightsRiskEngine(database, audit, clock=self.clock)

    def new_item(self) -> ContentItem:
        item = make_content_item(self.channel, self.strategy)
        with self.database.transaction() as connection:
            ContentItemRepository(connection).add(item)
        return item

    def asset(self, category: AssetCategory = UNKNOWN, **overrides) -> Asset:
        self.titles += 1
        arguments = {
            "title": f"Asset {self.titles}",
            "source": "stock.example",
            "actor": USER,
        } | overrides
        if category is GENERATED:
            self.versions += 1
            artifact = make_artifact(self.item, version=self.versions)
            with self.database.transaction() as connection:
                ArtifactRepository(connection).add(artifact)
            arguments |= {"source": "mock-image", "artifact_id": artifact.id}
        if category is LICENSED:
            arguments.setdefault("license_ref", "CC-BY-4.0")
        if category is USER_OWNED:
            arguments |= {"source": "user", "owner": "Lan"}
        return self.registry.register(self.channel.id, IMAGE, category, **arguments)

    def used(self, category: AssetCategory = UNKNOWN, **overrides) -> Asset:
        """An asset attached to the item, so that the item has a rights record."""
        asset = self.asset(category, **overrides)
        self.registry.attach(asset.id, self.item.id, actor=USER)
        return asset

    def provenance(self, asset: Asset, **details):
        return self.recorder.record(asset.id, actor=USER, **details)

    def record_of(self, asset_ref: str) -> RightsRecord:
        return next(r for r in self.records() if r.asset_ref == asset_ref)

    def records(self, item: ContentItem | None = None) -> list[RightsRecord]:
        return RecordSource(self.database).list_by_content_item((item or self.item).id)

    def add_record(self, asset_ref: str, **fields) -> RightsRecord:
        record = RightsRecord.create(
            self.item.id, asset_ref, source="stock.example", clock=self.clock
        )
        record = dataclasses.replace(record, **fields)
        with self.database.transaction() as connection:
            RightsRecordRepository(connection).add(record)
        return record

    def update_record(self, record: RightsRecord) -> None:
        with self.database.transaction() as connection:
            current = RightsRecordRepository(connection).get(record.id)
            RightsRecordRepository(connection).update(
                record, expected_updated_at=current.updated_at
            )

    def assess(self, actor: Actor = SYSTEM) -> RightsAssessmentRun:
        return self.engine.assess(self.item.id, actor=actor)

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

    def actions(self) -> list[str]:
        return [event.action for event in self.sink.events()]

    def assessed_events(self) -> list:
        return [e for e in self.sink.events() if e.action == "rights.assessed"]


@pytest.fixture
def world(database: Database) -> World:
    return World(database)


# one outcome per category, end to end


def test_a_generated_asset_is_low(world: World) -> None:
    asset = world.used(GENERATED)

    run = world.assess()

    assert [(a.level, a.rule_codes) for a in run.assessments] == [
        (LOW, ("generated.declared",))
    ]
    record = world.record_of(asset.id)
    assert record.risk_level is LOW
    assert record.resolution is RiskResolution.UNRESOLVED


def test_an_unknown_category_is_high(world: World) -> None:
    asset = world.used(UNKNOWN)

    run = world.assess()

    assert [(a.level, a.rule_codes) for a in run.assessments] == [
        (HIGH, ("asset.category_unknown",))
    ]
    assert world.record_of(asset.id).risk_level is HIGH


@pytest.mark.parametrize(
    ("category", "details", "level", "code"),
    [
        (LICENSED, None, HIGH, "licensed.no_provenance"),
        (
            LICENSED,
            {"license_name": "CC BY 4.0"},
            MEDIUM,
            "licensed.licence_without_evidence",
        ),
        (
            LICENSED,
            {"license_name": "CC BY 4.0", "proof": "invoice 7"},
            LOW,
            "licensed.documented",
        ),
        (PUBLIC_DOMAIN, None, MEDIUM, "public_domain.no_provenance"),
        (
            PUBLIC_DOMAIN,
            {"proof": "archive page"},
            MEDIUM,
            "public_domain.no_source_url",
        ),
        (
            PUBLIC_DOMAIN,
            {"source_url": "https://archive.example/a"},
            MEDIUM,
            "public_domain.source_only",
        ),
        (
            PUBLIC_DOMAIN,
            {"source_url": "https://archive.example/a", "proof": "page 3"},
            LOW,
            "public_domain.documented",
        ),
        (USER_OWNED, None, MEDIUM, "user_owned.no_provenance"),
        (USER_OWNED, {"owner": "Lan"}, MEDIUM, "user_owned.unproven"),
        (
            USER_OWNED,
            {"owner": "Lan", "file_sha256": SHA},
            LOW,
            "user_owned.documented",
        ),
    ],
)
def test_a_category_and_its_provenance_give_a_level_and_a_code(
    world: World, category, details, level, code
) -> None:
    asset = world.used(category)
    current = world.provenance(asset, **details) if details else None

    run = world.assess()

    (assessment,) = run.assessments
    assert (assessment.level, assessment.rule_codes) == (level, (code,))
    assert assessment.rules_version == RULES_VERSION
    assert assessment.asset_id == asset.id
    assert assessment.provenance_id == (current.id if current else None)
    assert assessment.rights_record_id == world.record_of(asset.id).id
    assert assessment.content_item_id == world.item.id
    record = world.record_of(asset.id)
    assert record.risk_level is level
    assert record.resolution is RiskResolution.UNRESOLVED
    assert (run.assessed, run.changed, run.skipped_resolved) == (1, 1, ())


def test_the_run_covers_every_record_of_the_item(world: World) -> None:
    low = world.used(GENERATED)
    medium = world.used(PUBLIC_DOMAIN)
    high = world.used(UNKNOWN)
    other_item = world.new_item()
    other = world.asset(UNKNOWN)
    world.registry.attach(other.id, other_item.id, actor=USER)

    run = world.assess()

    assert run.content_item_id == world.item.id
    assert [a.asset_id for a in run.assessments] == [low.id, medium.id, high.id]
    assert run.assessed == 3
    assert run.changed == 3
    assert run.level_counts == {LOW: 1, MEDIUM: 1, HIGH: 1}
    # The other item is not touched.
    assert world.records(other_item)[0].risk_level is RiskLevel.UNKNOWN
    assert world.count("rights_assessments") == 3


def test_the_actor_is_stored_for_any_kind(world: World) -> None:
    world.used(UNKNOWN)

    for actor in (USER, SYSTEM, AI):
        # A new provenance each round gives a new row for the same record.
        asset = world.registry.list_by_content_item(world.item.id)[0]
        world.provenance(asset, proof=f"note by {actor.kind.value}")
        (assessment,) = world.assess(actor).assessments
        assert assessment.assessed_by == actor

    kinds = [a.assessed_by.kind for a in world.engine.history(world.records()[0].id)]
    assert kinds == [ActorKind.USER, ActorKind.SYSTEM, ActorKind.AI]


# resolved records


def resolve(world: World, record: RightsRecord) -> RightsRecord:
    resolved = record.resolve(actor=USER, clock=world.clock)
    world.update_record(resolved)
    return resolved


def test_a_resolved_record_is_skipped_and_left_untouched(world: World) -> None:
    asset = world.used(UNKNOWN)
    resolved = resolve(world, world.record_of(asset.id))
    rights_before = world.rows("rights_records")

    run = world.assess()

    assert run.skipped_resolved == (resolved.id,)
    assert run.assessments == ()
    assert (run.assessed, run.changed) == (0, 0)
    assert world.count("rights_assessments") == 0
    assert world.rows("rights_records") == rights_before
    assert world.assessed_events() == []
    assert world.record_of(asset.id).risk_level is RiskLevel.UNKNOWN


def test_a_resolved_record_with_an_unregistered_asset_is_not_read(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    resolved = world.add_record("ghost-asset")
    resolved = resolve(world, resolved)
    kept = world.used(GENERATED)
    asset_reads: list[str] = []
    provenance_reads: list[str] = []
    asset_get = AssetRepository.get
    provenance_latest = ProvenanceRepository.latest

    def spy_asset(self, asset_id):
        asset_reads.append(asset_id)
        return asset_get(self, asset_id)

    def spy_provenance(self, asset_id):
        provenance_reads.append(asset_id)
        return provenance_latest(self, asset_id)

    monkeypatch.setattr(AssetRepository, "get", spy_asset)
    monkeypatch.setattr(ProvenanceRepository, "latest", spy_provenance)

    run = world.assess()

    assert "ghost-asset" not in asset_reads
    assert "ghost-asset" not in provenance_reads
    assert kept.id in asset_reads

    assert run.skipped_resolved == (resolved.id,)
    assert [a.asset_id for a in run.assessments] == [kept.id]
    assert world.engine.history(resolved.id) == []
    assert world.engine.latest(resolved.id) is None


def test_a_record_resolved_after_an_assessment_keeps_its_level(world: World) -> None:
    asset = world.used(PUBLIC_DOMAIN)
    world.assess()
    resolved = resolve(world, world.record_of(asset.id))
    world.provenance(asset, source_url="https://archive.example/a", proof="page")

    run = world.assess()

    assert run.skipped_resolved == (resolved.id,)
    assert world.count("rights_assessments") == 1
    assert world.record_of(asset.id) == resolved
    assert resolved.is_resolved and resolved.risk_level is MEDIUM


def test_the_engine_never_resolves_a_record(
    world: World,
) -> None:
    # The engine never resolves; a level it sets keeps the record unresolved.
    asset = world.used(UNKNOWN)
    world.assess()

    assert world.record_of(asset.id).resolution is RiskResolution.UNRESOLVED
    assert world.record_of(asset.id).resolved_by is None


# registered and unregistered assets


def test_an_asset_ref_that_is_not_registered_is_high(world: World) -> None:
    record = world.add_record("ghost-asset")

    (assessment,) = world.assess().assessments

    assert (assessment.level, assessment.rule_codes) == (
        HIGH,
        ("asset.not_registered",),
    )
    assert (assessment.asset_id, assessment.provenance_id) == (None, None)
    assert assessment.rights_record_id == record.id
    assert world.records()[0].risk_level is HIGH


def test_an_asset_of_another_channel_is_high_and_names_no_asset(
    world: World, database: Database
) -> None:
    other_channel = make_channel()
    other_strategy = make_strategy_profile(other_channel)
    with database.transaction() as connection:
        ChannelRepository(connection).add(other_channel)
        StrategyProfileRepository(connection).add(other_strategy)
    foreign = world.registry.register(
        other_channel.id,
        IMAGE,
        AssetCategory.LICENSED,
        title="Foreign",
        source="stock.example",
        license_ref="CC-BY-4.0",
        actor=USER,
    )
    world.recorder.record(
        foreign.id,
        license_name="CC BY",
        license_url="https://licences.example/x",
        actor=USER,
    )
    world.add_record(foreign.id)

    (assessment,) = world.assess().assessments

    assert (assessment.level, assessment.rule_codes) == (
        HIGH,
        ("asset.not_registered",),
    )
    assert (assessment.asset_id, assessment.provenance_id) == (None, None)


# idempotency, history and drift


def test_a_second_run_writes_and_audits_nothing(world: World) -> None:
    asset = world.used(PUBLIC_DOMAIN)
    first = world.assess()
    rows = world.rows("rights_assessments", "rights_records")
    events = len(world.sink.events())
    updated_at = world.record_of(asset.id).updated_at

    second = world.assess(USER)

    assert second.changed == 0
    assert second.assessments == first.assessments
    assert second.assessments[0].assessed_by == SYSTEM
    assert world.rows("rights_assessments", "rights_records") == rows
    assert len(world.sink.events()) == events
    assert world.record_of(asset.id).updated_at == updated_at


def test_a_new_provenance_gives_a_new_row(world: World) -> None:
    asset = world.used(PUBLIC_DOMAIN)
    world.assess()
    current = world.provenance(asset, source_url="https://archive.example/a")

    run = world.assess()

    assert run.changed == 1
    (assessment,) = run.assessments
    assert assessment.provenance_id == current.id
    assert assessment.rule_codes == ("public_domain.source_only",)
    history = world.engine.history(world.record_of(asset.id).id)
    assert [a.rule_codes for a in history] == [
        ("public_domain.no_provenance",),
        ("public_domain.source_only",),
    ]
    # The level stayed medium, so the record itself was not rewritten.
    assert world.record_of(asset.id).risk_level is MEDIUM


def test_a_new_provenance_with_the_same_content_still_gives_a_new_row(
    world: World,
) -> None:
    asset = world.used(PUBLIC_DOMAIN)
    world.provenance(asset, source_url="https://archive.example/a")
    world.assess()
    world.provenance(asset, source_url="https://archive.example/b")
    world.provenance(asset, source_url="https://archive.example/a")

    run = world.assess()

    assert run.changed == 1
    assert world.count("rights_assessments") == 2


def test_a_new_provenance_that_changes_the_level_updates_the_record(
    world: World,
) -> None:
    asset = world.used(LICENSED)
    world.assess()
    assert world.record_of(asset.id).risk_level is HIGH
    before = world.record_of(asset.id)

    world.provenance(asset, license_name="CC BY 4.0", proof="invoice 7")
    run = world.assess()

    assert run.changed == 1
    record = world.record_of(asset.id)
    assert record.risk_level is LOW
    assert record.updated_at > before.updated_at
    assert record.resolution is RiskResolution.UNRESOLVED


def test_a_drifted_record_level_gets_a_new_row_and_is_applied_again(
    world: World,
) -> None:
    asset = world.used(PUBLIC_DOMAIN)
    world.assess()
    record = world.record_of(asset.id)
    world.update_record(record.with_risk_level(LOW, clock=world.clock))
    assert world.record_of(asset.id).risk_level is LOW

    run = world.assess()

    assert run.changed == 1
    assert world.record_of(asset.id).risk_level is MEDIUM
    history = world.engine.history(record.id)
    assert [a.level for a in history] == [MEDIUM, MEDIUM]
    assert history[0].outcome_key() == history[1].outcome_key()
    # Now it is in sync, so a further run changes nothing.
    assert world.assess().changed == 0


def test_a_record_already_at_the_computed_level_is_not_rewritten(
    world: World,
) -> None:
    asset = world.used(PUBLIC_DOMAIN)
    record = world.record_of(asset.id)
    world.update_record(record.with_risk_level(MEDIUM, clock=world.clock))
    before = world.record_of(asset.id)

    run = world.assess()

    assert run.changed == 1  # the first assessment is a new row
    assert world.record_of(asset.id) == before  # but the record is the same
    assert world.count("rights_assessments") == 1


def test_a_run_with_nothing_unresolved_writes_nothing(world: World) -> None:
    run = world.assess()

    assert run == RightsAssessmentRun(world.item.id, (), 0, ())
    assert run.level_counts == {LOW: 0, MEDIUM: 0, HIGH: 0}
    assert world.count("rights_assessments") == 0
    assert world.actions() == []


def test_the_history_never_loses_a_row(world: World) -> None:
    asset = world.used(LICENSED)
    world.assess()
    world.provenance(asset, license_name="CC BY 4.0")
    world.assess()
    world.provenance(asset, license_name="CC BY 4.0", proof="invoice 7")
    world.assess()

    history = world.engine.history(world.record_of(asset.id).id)

    assert [a.level for a in history] == [HIGH, MEDIUM, LOW]
    assert world.engine.latest(world.record_of(asset.id).id) == history[-1]


# errors and the clock


def test_a_missing_item_is_a_404(world: World) -> None:
    with pytest.raises(ContentItemNotFoundError) as caught:
        world.engine.assess("no-such-item", actor=USER)

    assert caught.value.to_public().http_status == HTTPStatus.NOT_FOUND
    assert world.count("rights_assessments") == 0
    assert world.actions() == []


def test_an_item_without_records_writes_and_audits_nothing(world: World) -> None:
    other = world.new_item()
    before = world.rows("rights_records", "rights_assessments")

    run = world.engine.assess(other.id, actor=USER)

    assert run.assessments == () and run.changed == 0
    assert world.rows("rights_records", "rights_assessments") == before
    assert world.actions() == []


@pytest.mark.parametrize(
    "bad_time",
    [
        datetime(2026, 10, 4, 12, 0),
        datetime(2026, 10, 4, 12, 0, tzinfo=timezone(timedelta(hours=7))),
    ],
    ids=["naive", "not-utc"],
)
def test_a_clock_that_is_not_utc_is_a_plain_value_error(
    world: World, database: Database, bad_time: datetime
) -> None:
    world.used(UNKNOWN)
    engine = RightsRiskEngine(database, AuditLog(world.sink), clock=lambda: bad_time)
    rows = world.rows("rights_records", "rights_assessments")

    with pytest.raises(ValueError, match="UTC") as caught:
        engine.assess(world.item.id, actor=USER)

    assert caught.value.__class__ is ValueError
    assert world.rows("rights_records", "rights_assessments") == rows
    assert world.assessed_events() == []


def test_the_time_is_read_inside_the_transaction(world: World) -> None:
    world.used(UNKNOWN)
    world.used(PUBLIC_DOMAIN)
    calls = []

    def clock() -> datetime:
        calls.append(1)
        return world.clock()

    engine = RightsRiskEngine(world.database, AuditLog(world.sink), clock=clock)
    run = engine.assess(world.item.id, actor=USER)

    assert len(calls) == 1  # one time for the whole run
    assert len({a.created_at for a in run.assessments}) == 1
    assert {r.updated_at for r in world.records()} == {run.assessments[0].created_at}


def test_a_failing_update_rolls_back_every_row(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = world.used(UNKNOWN)
    second = world.used(PUBLIC_DOMAIN)
    rows = world.rows("rights_records", "rights_assessments")
    events = len(world.sink.events())
    original = RightsRecordRepository.update
    calls = []

    def flaky(repository, record, *, expected_updated_at):
        calls.append(record.asset_ref)
        if len(calls) == 2:
            raise ConcurrencyError("rights record changed")
        return original(repository, record, expected_updated_at=expected_updated_at)

    monkeypatch.setattr(RightsRecordRepository, "update", flaky)
    with pytest.raises(ConcurrencyError):
        world.assess()
    monkeypatch.undo()

    assert calls == [first.id, second.id]
    assert world.rows("rights_records", "rights_assessments") == rows
    assert len(world.sink.events()) == events
    # The run works once the conflict is gone.
    assert world.assess().changed == 2


def test_a_failing_insert_stores_and_audits_nothing(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    world.used(UNKNOWN)
    events = len(world.sink.events())

    def boom(repository, assessment):
        raise sqlite3.OperationalError("store down")

    monkeypatch.setattr(RightsAssessmentRepository, "add", boom)
    with pytest.raises(sqlite3.OperationalError, match="store down"):
        world.assess()
    monkeypatch.undo()

    assert world.count("rights_assessments") == 0
    assert world.records()[0].risk_level is RiskLevel.UNKNOWN
    assert len(world.sink.events()) == events
    assert world.assess().changed == 1


# audit


def test_the_audit_holds_counts_only(world: World) -> None:
    world.used(
        GENERATED,
        title=f"The {SECRET} title",
        source=f"{SECRET}.example",
    )
    licensed = world.used(
        LICENSED,
        title=f"Licensed {SECRET}",
        license_ref=f"{SECRET} licence",
    )
    world.provenance(
        licensed,
        source_url=f"https://{SECRET}.example/path?id={SECRET}",
        license_name=f"{SECRET} licence",
        license_url=f"https://{SECRET}.example/licence",
        proof=f"{SECRET} proof",
    )
    world.used(PUBLIC_DOMAIN)
    world.used(UNKNOWN)
    resolved_target = world.used(UNKNOWN)
    resolve(world, world.record_of(resolved_target.id))

    run = world.assess(AI)

    (event,) = world.assessed_events()
    assert event.entity.type == "content_item"
    assert event.entity.id == world.item.id
    assert event.actor == AI
    assert dict(event.metadata) == {
        "assessed": 4,
        "changed": 4,
        "skipped_resolved": 1,
        "low": 2,
        "medium": 1,
        "high": 1,
    }
    assert run.level_counts == {LOW: 2, MEDIUM: 1, HIGH: 1}
    dumped = json.dumps([e.as_dict() for e in world.sink.events()], ensure_ascii=False)
    assert SECRET not in dumped


def test_the_audit_counts_the_unchanged_records_as_assessed(world: World) -> None:
    asset = world.used(PUBLIC_DOMAIN)
    world.used(UNKNOWN)
    world.assess()
    world.provenance(asset, source_url="https://archive.example/a", proof="page")

    world.assess()

    first, second = world.assessed_events()
    assert dict(first.metadata)["changed"] == 2
    assert dict(second.metadata) == {
        "assessed": 2,
        "changed": 1,
        "skipped_resolved": 0,
        "low": 1,
        "medium": 0,
        "high": 1,
    }


def test_reads_write_and_audit_nothing(world: World) -> None:
    asset = world.used(UNKNOWN)
    world.assess()
    rows = world.rows("rights_records", "rights_assessments")
    events = len(world.sink.events())
    record_id = world.record_of(asset.id).id

    world.engine.latest(record_id)
    world.engine.history(record_id)

    assert world.rows("rights_records", "rights_assessments") == rows
    assert len(world.sink.events()) == events


# latest and history


def test_latest_and_history_of_a_record_never_assessed(world: World) -> None:
    asset = world.used(UNKNOWN)
    record_id = world.record_of(asset.id).id

    assert world.engine.latest(record_id) is None
    assert world.engine.history(record_id) == []


def test_latest_and_history_follow_the_order_of_assessment(world: World) -> None:
    asset = world.used(PUBLIC_DOMAIN)
    world.assess()
    world.provenance(asset, source_url="https://archive.example/a")
    world.assess()
    world.provenance(asset, source_url="https://archive.example/a", proof="page")
    world.assess()
    record_id = world.record_of(asset.id).id

    history = world.engine.history(record_id)

    assert [a.rule_codes[0] for a in history] == [
        "public_domain.no_provenance",
        "public_domain.source_only",
        "public_domain.documented",
    ]
    assert [a.created_at for a in history] == sorted(a.created_at for a in history)
    assert world.engine.latest(record_id) == history[-1]


def test_history_orders_equal_times_by_insertion(world: World) -> None:
    asset = world.used(PUBLIC_DOMAIN)
    record_id = world.record_of(asset.id).id
    fixed = RightsRiskEngine(world.database, AuditLog(world.sink), clock=lambda: FIXED)
    fixed.assess(world.item.id, actor=USER)
    world.provenance(asset, source_url="https://archive.example/a")
    fixed.assess(world.item.id, actor=USER)

    history = world.engine.history(record_id)

    assert [a.rule_codes[0] for a in history] == [
        "public_domain.no_provenance",
        "public_domain.source_only",
    ]
    assert world.engine.latest(record_id) == history[-1]


def test_the_history_of_other_records_is_separate(world: World) -> None:
    one = world.used(UNKNOWN)
    two = world.used(GENERATED)
    world.assess()

    assert [a.asset_id for a in world.engine.history(world.record_of(one.id).id)] == [
        one.id
    ]
    assert [a.asset_id for a in world.engine.history(world.record_of(two.id).id)] == [
        two.id
    ]


def test_a_missing_rights_record_is_a_404_and_writes_nothing(world: World) -> None:
    world.used(UNKNOWN)
    rows = world.rows("rights_records", "rights_assessments")

    for read in (world.engine.latest, world.engine.history):
        with pytest.raises(RightsRecordNotFoundError) as caught:
            read("no-such-record")
        assert caught.value.to_public().http_status == HTTPStatus.NOT_FOUND
        assert caught.value.code == "domain.rights_record_not_found"

    assert world.rows("rights_records", "rights_assessments") == rows
    assert world.actions().count("rights.assessed") == 0


def test_the_error_message_holds_no_value(world: World) -> None:
    with pytest.raises(RightsRecordNotFoundError) as caught:
        world.engine.latest(f"{SECRET}-record")

    assert SECRET not in caught.value.to_public().message


# other state is unchanged


def test_assessing_changes_no_asset_provenance_or_usage(world: World) -> None:
    asset = world.used(LICENSED)
    world.provenance(asset, license_name="CC BY")
    before = world.rows("assets", "asset_usages", "asset_provenance")

    world.assess()

    assert world.rows("assets", "asset_usages", "asset_provenance") == before


# bootstrap


def test_bootstrap_registers_the_risk_engine(tmp_path: Path) -> None:
    container = build_container(
        Settings(environment=Environment.TEST, database_path=tmp_path / "a.db")
    )

    assert isinstance(container.resolve(RightsRiskEngine), RightsRiskEngine)


# the rights gate with the result of the engine


def context(world: World) -> GateContext:
    item = dataclasses.replace(world.item, status=ContentStatus.APPROVED)
    return GateContext(
        item=item, target_status=ContentStatus.PUBLISHING, actor=USER, at=T0
    )


def gate_codes(world: World, gate: RightsGate) -> list[str]:
    result = gate.evaluate(context(world))
    assert result.gate is GateName.RIGHTS
    return [reason.code for reason in result.reasons]


def test_the_default_gate_blocks_high_and_unknown_and_passes_low_and_medium(
    world: World,
) -> None:
    gate = RightsGate(RecordSource(world.database))
    world.used(GENERATED)
    world.used(PUBLIC_DOMAIN)
    unassessed = world.records()

    # Before the engine runs every record is unknown, which blocks.
    assert gate_codes(world, gate) == ["rights.unresolved_unknown"] * len(unassessed)

    world.assess()
    assert gate.evaluate(context(world)).is_passed

    world.used(UNKNOWN)
    world.assess()
    assert gate_codes(world, gate) == ["rights.unresolved_high"]


def test_a_configured_gate_blocks_medium(world: World) -> None:
    gate = RightsGate(
        RecordSource(world.database),
        blocking_levels=blocking_levels_for(
            {RightsBlockLevel.MEDIUM, RightsBlockLevel.HIGH}
        ),
    )
    world.used(GENERATED)
    world.used(PUBLIC_DOMAIN)
    world.assess()

    assert gate_codes(world, gate) == ["rights.unresolved_medium"]


def test_a_user_resolution_lets_the_configured_gate_pass(world: World) -> None:
    gate = RightsGate(
        RecordSource(world.database),
        blocking_levels=blocking_levels_for(
            {RightsBlockLevel.MEDIUM, RightsBlockLevel.HIGH}
        ),
    )
    medium = world.used(PUBLIC_DOMAIN)
    high = world.used(UNKNOWN)
    world.assess()
    assert gate_codes(world, gate) == [
        "rights.unresolved_medium",
        "rights.unresolved_high",
    ]

    resolve(world, world.record_of(medium.id))
    resolve(world, world.record_of(high.id))

    assert gate.evaluate(context(world)).is_passed
