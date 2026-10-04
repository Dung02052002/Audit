"""G-078b Rights Gate Integration: the publish gate over the real repositories.

Rules the user approved on 2026-10-04:

- ``PublishGate`` runs the approval, daily limit, rights and idempotency gates
  (the C-042 order) over one read snapshot of the database; policy and the kill
  switch are deferred (``DEFERRED_GATES``);
- the rights gate blocks on the levels of ``Settings.rights_block_levels`` and on
  a stale assessment: a record never assessed, or assessed on another asset or
  provenance than the current one, blocks with ``rights.assessment_stale``;
- an unregistered asset blocks as high (never as stale); a resolved record is
  never checked;
- the gate only reads: no row, no audit event, no write lock;
- a reason holds ids only, never a URL or a text.
"""

import dataclasses
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from ai_youtube_agent.bootstrap import build_container
from ai_youtube_agent.content.approval import ApprovalRequest, ApprovalStatus
from ai_youtube_agent.content.asset import Asset, AssetCategory, AssetKind
from ai_youtube_agent.content.asset_registry import AssetRegistry
from ai_youtube_agent.content.provenance_recorder import ProvenanceRecorder
from ai_youtube_agent.content.rights import RightsRecord, RiskLevel
from ai_youtube_agent.content.rights_assessment import (
    RULES_VERSION,
    RightsAssessment,
    classify,
)
from ai_youtube_agent.content.rights_risk_engine import (
    RightsRecordNotFoundError,
    RightsRiskEngine,
)
from ai_youtube_agent.content.strategy import Cadence
from ai_youtube_agent.core.audit import (
    Actor,
    ActorKind,
    AuditLog,
    AuditSink,
    InMemoryAuditSink,
)
from ai_youtube_agent.core.config import Environment, Settings
from ai_youtube_agent.core.content_item import (
    ContentItem,
    ContentStatus,
    ContentTransitionError,
)
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
from ai_youtube_agent.core.db.repositories.provenance import ProvenanceRepository
from ai_youtube_agent.core.db.repositories.review import (
    ApprovalRequestRepository,
    RightsRecordRepository,
)
from ai_youtube_agent.core.db.repositories.rights_assessment import (
    RightsAssessmentRepository,
)
from ai_youtube_agent.core.gates import (
    GateBlockedError,
    GateContext,
    GateName,
    GateOutcome,
    GateReport,
)
from ai_youtube_agent.core.publish_gate import (
    DEFERRED_GATES,
    PublishGate,
    RepositoryFreshness,
)
from ai_youtube_agent.core.rights_gate import RightsGate
from factories import (
    make_artifact,
    make_channel,
    make_content_item,
    make_strategy_profile,
)

T0 = datetime(2026, 10, 4, 12, 0, tzinfo=UTC)
EVALUATED = T0 + timedelta(days=1)
USER = Actor(ActorKind.USER, "owner")
SYSTEM = Actor(ActorKind.SYSTEM, "pipeline")
SECRET = "zebrafish"  # a word no reason may hold
IMAGE = AssetKind.IMAGE
LICENSED, PUBLIC_DOMAIN, UNKNOWN = (
    AssetCategory.LICENSED,
    AssetCategory.PUBLIC_DOMAIN,
    AssetCategory.UNKNOWN,
)
LOW, MEDIUM, HIGH = RiskLevel.LOW, RiskLevel.MEDIUM, RiskLevel.HIGH
GATE_ORDER = (
    GateName.APPROVAL,
    GateName.DAILY_LIMIT,
    GateName.RIGHTS,
    GateName.IDEMPOTENCY,
)
SETTINGS_VALUES = [
    "bogus",
    "high,bogus",
    "[bogus",
    '["medium", "bogus"]',
    5,
    {"high": 1},
    "medium",
    '["medium"]',
    "",
    "   ",
]


class Clock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> datetime:
        self.now += timedelta(seconds=1)
        return self.now


class World:
    """A channel with one item, real services and a fake increasing clock."""

    def __init__(self, database: Database, **settings) -> None:
        self.database = database
        self.clock = Clock()
        self.titles = 0
        self.channel = make_channel()
        self.strategy = make_strategy_profile(
            self.channel, **settings.pop("strategy", {})
        )
        self.item = make_content_item(self.channel, self.strategy)
        with database.transaction() as connection:
            ChannelRepository(connection).add(self.channel)
            StrategyProfileRepository(connection).add(self.strategy)
            ContentItemRepository(connection).add(self.item)
        self.sink = InMemoryAuditSink()
        audit = AuditLog(self.sink)
        self.registry = AssetRegistry(database, audit, clock=self.clock)
        self.recorder = ProvenanceRecorder(database, audit, clock=self.clock)
        self.engine = RightsRiskEngine(database, audit, clock=self.clock)
        self.container = build_container(
            Settings(
                environment=Environment.TEST,
                database_path=database.path,
                **settings,
            )
        )
        self.gate = self.container.resolve(PublishGate)

    @property
    def approved(self) -> ContentItem:
        return dataclasses.replace(self.item, status=ContentStatus.APPROVED)

    def asset(self, category: AssetCategory = UNKNOWN, **overrides) -> Asset:
        self.titles += 1
        arguments = {
            "title": f"Asset {self.titles}",
            "source": "stock.example",
            "actor": USER,
        } | overrides
        if category is LICENSED:
            arguments.setdefault("license_ref", "CC-BY-4.0")
        return self.registry.register(self.channel.id, IMAGE, category, **arguments)

    def used(self, category: AssetCategory = UNKNOWN, **overrides) -> Asset:
        asset = self.asset(category, **overrides)
        self.registry.attach(asset.id, self.item.id, actor=USER)
        return asset

    def documented(self) -> Asset:
        """A licensed asset with a licence and a proof: assessed as low."""
        asset = self.used(LICENSED)
        self.provenance(asset, license_name="CC BY 4.0", proof="invoice 7")
        return asset

    def provenance(self, asset: Asset, **details):
        return self.recorder.record(asset.id, actor=USER, **details)

    def assess(self):
        return self.engine.assess(self.item.id, actor=SYSTEM)

    def records(self) -> list[RightsRecord]:
        with self.database.transaction() as connection:
            return RightsRecordRepository(connection).list_by_content_item(self.item.id)

    def record_of(self, asset_ref: str) -> RightsRecord:
        return next(r for r in self.records() if r.asset_ref == asset_ref)

    def add_record(self, asset_ref: str, **fields) -> RightsRecord:
        record = RightsRecord.create(
            self.item.id, asset_ref, source="stock.example", clock=self.clock
        )
        record = dataclasses.replace(record, **fields)
        with self.database.transaction() as connection:
            RightsRecordRepository(connection).add(record)
        return record

    def resolve(self, record: RightsRecord) -> RightsRecord:
        resolved = record.resolve(actor=USER, clock=self.clock)
        with self.database.transaction() as connection:
            RightsRecordRepository(connection).update(
                resolved, expected_updated_at=record.updated_at
            )
        return resolved

    def approve(self) -> None:
        """An approved request over one video artifact, so the approval gate passes."""
        artifact = make_artifact(self.item)
        request = dataclasses.replace(
            ApprovalRequest.create(
                self.item.id, [artifact], requested_by=SYSTEM, clock=self.clock
            ),
            status=ApprovalStatus.APPROVED,
        )
        with self.database.transaction() as connection:
            ArtifactRepository(connection).add(artifact)
            ApprovalRequestRepository(connection).add(request)

    def evaluate(self) -> GateReport:
        return self.gate.evaluate(self.approved, actor=USER, at=EVALUATED)

    def snapshot(self) -> dict[str, list[tuple]]:
        """Every row of every table, to prove that a read changes nothing."""
        with self.database.transaction() as connection:
            tables = [
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table' "
                    "AND name NOT LIKE 'sqlite_%' ORDER BY name"
                )
            ]
            return {
                table: [
                    tuple(row)
                    for row in connection.execute(
                        f"SELECT * FROM {table} ORDER BY rowid"
                    )
                ]
                for table in tables
            }

    def count(self, table: str) -> int:
        with self.database.transaction() as connection:
            return connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]


@pytest.fixture
def world(database: Database) -> World:
    return World(database)


def result_of(report: GateReport, name: GateName):
    return next(r for r in report.results if r.gate is name)


def codes(report: GateReport, name: GateName = GateName.RIGHTS) -> list[str]:
    return [reason.code for reason in result_of(report, name).reasons]


def messages(report: GateReport, name: GateName = GateName.RIGHTS) -> list[str]:
    return [reason.message for reason in result_of(report, name).reasons]


# composition


def test_the_gate_runs_the_real_gates_in_the_matrix_order(world: World) -> None:
    report = world.evaluate()

    assert world.gate.gate_names == GATE_ORDER
    assert [r.gate for r in report.results] == list(GATE_ORDER)
    # The approval gate read the real (empty) repositories and blocked.
    assert codes(report, GateName.APPROVAL) == ["approval.missing"]
    assert result_of(report, GateName.DAILY_LIMIT).outcome is GateOutcome.PASS
    assert result_of(report, GateName.IDEMPOTENCY).outcome is GateOutcome.PASS
    assert report.outcome is GateOutcome.BLOCK
    assert all(r.evaluated_at == EVALUATED for r in report.results)


def test_the_deferred_gates_are_pinned() -> None:
    assert DEFERRED_GATES == (GateName.POLICY, GateName.KILL_SWITCH)
    assert not set(DEFERRED_GATES) & set(GATE_ORDER)


def test_the_container_gives_a_publish_gate(world: World) -> None:
    assert isinstance(world.gate, PublishGate)
    assert world.container.resolve(PublishGate) is world.gate


def test_a_world_that_is_clean_in_every_gate_passes(world: World) -> None:
    world.documented()
    world.assess()
    world.approve()

    report = world.evaluate()

    assert report.is_passed
    assert report.reasons == ()
    assert world.gate.ensure_can_publish(world.approved, actor=USER) is not None


def test_ensure_can_publish_raises_when_a_gate_blocks(world: World) -> None:
    world.used(UNKNOWN)
    world.approve()

    with pytest.raises(GateBlockedError) as caught:
        world.gate.ensure_can_publish(world.approved, actor=USER, at=EVALUATED)

    assert [r.gate for r in caught.value.report.blocked] == [GateName.RIGHTS]
    assert [r.code for r in caught.value.report.reasons] == [
        "rights.unresolved_unknown"
    ]


def test_an_item_that_is_not_approved_raises_as_the_contract_does(
    world: World,
) -> None:
    for status in (ContentStatus.DRAFT, ContentStatus.PREVIEW_READY):
        item = dataclasses.replace(world.item, status=status)
        with pytest.raises(ContentTransitionError):
            world.gate.evaluate(item, actor=USER)
        with pytest.raises(ContentTransitionError):
            world.gate.ensure_can_publish(item, actor=USER)


def test_the_time_defaults_to_now_in_utc(world: World) -> None:
    before = datetime.now(UTC)

    report = world.gate.evaluate(world.approved, actor=USER)

    assert before <= report.results[0].evaluated_at <= datetime.now(UTC)


def test_the_daily_limit_gate_reads_the_real_strategy(database: Database) -> None:
    world = World(database, strategy={"cadence": Cadence(0, 0)})

    report = world.evaluate()

    assert codes(report, GateName.DAILY_LIMIT) == ["daily_limit.publish_reached"]


# levels through the whole set


@pytest.mark.parametrize(
    ("level", "blocked_code"),
    [
        ("high", "rights.unresolved_high"),
        ("medium", None),
        ("low", None),
        ("unknown", "rights.unresolved_unknown"),
    ],
)
def test_every_level_through_the_whole_set(
    world: World, level: str, blocked_code: str | None
) -> None:
    if level == "high":
        world.used(UNKNOWN)
        world.assess()
    elif level == "medium":
        world.used(PUBLIC_DOMAIN)
        world.assess()
    elif level == "low":
        world.documented()
        world.assess()
    else:
        world.used(UNKNOWN)  # never assessed: the record is unknown
    world.approve()

    report = world.evaluate()

    assert codes(report) == ([blocked_code] if blocked_code else [])
    assert report.is_passed is (blocked_code is None)
    for name in (GateName.APPROVAL, GateName.DAILY_LIMIT, GateName.IDEMPOTENCY):
        assert result_of(report, name).outcome is GateOutcome.PASS


def test_unknown_blocks_without_an_assessment_and_medium_passes_by_default(
    world: World,
) -> None:
    unknown = world.used(UNKNOWN)
    medium = world.used(PUBLIC_DOMAIN)
    assert [r.risk_level for r in world.records()] == [RiskLevel.UNKNOWN] * 2

    before = world.evaluate()
    world.assess()
    after = world.evaluate()

    assert codes(before) == ["rights.unresolved_unknown"] * 2
    assert codes(after) == ["rights.unresolved_high"]
    assert world.record_of(medium.id).risk_level is MEDIUM
    assert unknown.id in messages(after)[0]


def test_the_default_configuration_keeps_the_exact_old_codes_and_messages(
    world: World,
) -> None:
    unknown = world.used(UNKNOWN)
    world.add_record("ghost-asset")

    report = world.evaluate()

    assert [
        (r.code, r.message) for r in result_of(report, GateName.RIGHTS).reasons
    ] == [
        (
            "rights.unresolved_unknown",
            f"Asset {unknown.id} has an unknown rights risk that is not resolved.",
        ),
        (
            "rights.unresolved_unknown",
            "Asset ghost-asset has an unknown rights risk that is not resolved.",
        ),
    ]
    world.assess()
    after = world.evaluate()
    reasons = result_of(after, GateName.RIGHTS).reasons
    assert [(r.code, r.message) for r in reasons] == [
        (
            "rights.unresolved_high",
            f"Asset {unknown.id} has a high rights risk that is not resolved.",
        ),
        (
            "rights.unresolved_high",
            "Asset ghost-asset has a high rights risk that is not resolved.",
        ),
    ]


# configuration


@pytest.mark.parametrize("value", SETTINGS_VALUES)
def test_a_bad_rights_block_levels_setting_is_a_validation_error(value) -> None:
    with pytest.raises(ValidationError):
        Settings(environment=Environment.TEST, rights_block_levels=value)


def test_a_setting_that_is_a_list_without_high_is_refused() -> None:
    with pytest.raises(ValidationError, match="must contain high"):
        Settings(environment=Environment.TEST, rights_block_levels=["medium"])


def test_high_alone_behaves_like_the_default(database: Database) -> None:
    default = World(database)
    default.used(PUBLIC_DOMAIN)
    default.used(UNKNOWN)
    default.assess()
    only_high = PublishGate(
        database,
        Settings(
            environment=Environment.TEST,
            database_path=database.path,
            rights_block_levels="high",
        ),
    )

    expected = default.evaluate()
    actual = only_high.evaluate(default.approved, actor=USER, at=EVALUATED)

    assert actual == expected
    assert codes(actual) == ["rights.unresolved_high"]


@pytest.mark.parametrize(
    "value", ["medium,high", ["medium", "high"], '["medium","high"]']
)
def test_medium_and_high_blocks_an_unresolved_medium(database: Database, value) -> None:
    world = World(database, rights_block_levels=value)
    medium = world.used(PUBLIC_DOMAIN)
    world.assess()

    report = world.evaluate()

    assert codes(report) == ["rights.unresolved_medium"]
    assert messages(report) == [
        f"Asset {medium.id} has a medium rights risk that is not resolved."
    ]
    world.resolve(world.record_of(medium.id))
    assert codes(world.evaluate()) == []


# purity and repeatability


def test_a_repeated_evaluation_gives_the_same_report(world: World) -> None:
    asset = world.used(PUBLIC_DOMAIN)
    world.assess()
    world.provenance(asset, source_url="https://archive.example/a")

    first = world.evaluate()
    second = world.evaluate()

    assert first == second
    assert codes(first) == ["rights.assessment_stale"]


def test_assessing_twice_changes_nothing_the_second_time(world: World) -> None:
    world.used(PUBLIC_DOMAIN)
    world.used(UNKNOWN)
    world.assess()
    world.evaluate()
    rows = world.count("rights_assessments")
    updated = [r.updated_at for r in world.records()]
    events = len(world.sink.events())

    again = world.assess()
    world.evaluate()

    assert again.changed == 0
    assert world.count("rights_assessments") == rows
    assert [r.updated_at for r in world.records()] == updated
    assert len(world.sink.events()) == events


def test_the_gate_writes_nothing_and_audits_nothing(world: World) -> None:
    asset = world.used(PUBLIC_DOMAIN)
    world.used(UNKNOWN)
    world.assess()
    world.provenance(asset, source_url="https://archive.example/a")
    world.approve()
    before = world.snapshot()
    events = len(world.sink.events())
    container_events = len(world.container.resolve(AuditSink).events())

    for _ in range(3):
        world.evaluate()
    with pytest.raises(GateBlockedError):
        world.gate.ensure_can_publish(world.approved, actor=USER, at=EVALUATED)

    assert world.snapshot() == before
    assert len(world.sink.events()) == events
    assert len(world.container.resolve(AuditSink).events()) == container_events


def test_the_snapshot_is_deferred_and_takes_no_write_lock(world: World) -> None:
    world.used(UNKNOWN)
    world.approve()
    other = world.database.connect()
    try:
        # Another writer holds the write lock; a BEGIN IMMEDIATE in the gate would
        # wait for it and fail, a deferred BEGIN only reads.
        other.execute("BEGIN IMMEDIATE")
        report = world.evaluate()
    finally:
        other.execute("ROLLBACK")
        other.close()

    assert "gate.error" not in codes(report)
    assert codes(report) == ["rights.unresolved_unknown"]
    assert codes(report, GateName.APPROVAL) == []
    # The gate released its snapshot: a writer can take the lock at once.
    with world.database.transaction() as connection:
        connection.execute("SELECT count(*) FROM rights_records").fetchone()


class TrackedConnection:
    """A connection that records its statements and how often it is closed."""

    def __init__(self, inner) -> None:
        self.inner = inner
        self.statements: list[str] = []
        self.closed = 0

    def __getattr__(self, name):
        return getattr(self.inner, name)

    def execute(self, sql, *arguments):
        self.statements.append(sql)
        return self.inner.execute(sql, *arguments)

    def close(self) -> None:
        self.closed += 1
        self.inner.close()


@pytest.fixture
def tracked(monkeypatch: pytest.MonkeyPatch) -> list[TrackedConnection]:
    connections: list[TrackedConnection] = []
    original = Database.connect

    def connect(self):
        connection = TrackedConnection(original(self))
        connections.append(connection)
        return connection

    monkeypatch.setattr(Database, "connect", connect)
    return connections


def test_one_evaluation_opens_one_connection_and_closes_it(
    world: World, tracked: list[TrackedConnection]
) -> None:
    world.used(UNKNOWN)
    tracked.clear()

    world.evaluate()
    first = list(tracked)
    world.evaluate()

    assert len(first) == 1
    assert len(tracked) == 2
    for connection in tracked:
        assert connection.closed == 1
        statements = connection.statements
        assert statements[0] == "BEGIN"
        assert statements[-1] == "ROLLBACK"
        assert "COMMIT" not in statements


def test_a_failing_source_blocks_that_gate_and_still_releases_the_connection(
    world: World, monkeypatch: pytest.MonkeyPatch, tracked: list[TrackedConnection]
) -> None:
    world.used(UNKNOWN)

    def boom(self, content_item_id):
        raise RuntimeError("database is locked")

    tracked.clear()
    monkeypatch.setattr(RightsRecordRepository, "list_by_content_item", boom)
    report = world.evaluate()
    monkeypatch.undo()

    assert codes(report) == ["gate.error"]
    assert codes(report, GateName.APPROVAL) == ["approval.missing"]
    assert len(tracked) == 1
    assert tracked[0].closed == 1
    assert tracked[0].statements[-1] == "ROLLBACK"
    with world.database.transaction():
        pass


def test_the_connection_is_closed_when_the_gate_set_itself_fails(
    world: World, monkeypatch: pytest.MonkeyPatch, tracked: list[TrackedConnection]
) -> None:
    def boom(self, connection):
        raise RuntimeError("cannot build the gates")

    tracked.clear()
    monkeypatch.setattr(PublishGate, "_gates", boom)
    with pytest.raises(RuntimeError):
        world.gate.evaluate(world.approved, actor=USER, at=EVALUATED)
    monkeypatch.undo()

    assert len(tracked) == 1
    assert tracked[0].closed == 1
    assert tracked[0].statements[-1] == "ROLLBACK"


def test_a_rollback_is_skipped_when_sqlite_ended_the_transaction(
    world: World, monkeypatch: pytest.MonkeyPatch, tracked: list[TrackedConnection]
) -> None:
    world.used(UNKNOWN)
    ended = {"done": False}
    original = RightsRecordRepository.list_by_content_item

    def end_the_transaction(self, content_item_id):
        if not ended["done"]:
            ended["done"] = True
            tracked[0].inner.execute("ROLLBACK")
        return original(self, content_item_id)

    tracked.clear()
    monkeypatch.setattr(
        RightsRecordRepository, "list_by_content_item", end_the_transaction
    )
    report = world.evaluate()
    monkeypatch.undo()

    assert "gate.error" not in codes(report)
    assert tracked[0].closed == 1
    assert "ROLLBACK" not in tracked[0].statements


# missing records


def test_a_missing_rights_record_is_a_404_and_an_item_without_records_passes(
    world: World,
) -> None:
    for read in (world.engine.latest, world.engine.history):
        with pytest.raises(RightsRecordNotFoundError):
            read("no-such-record")

    report = world.evaluate()

    assert codes(report) == []
    assert result_of(report, GateName.RIGHTS).outcome is GateOutcome.PASS


# freshness


def test_the_freshness_basis_is_what_the_engine_assessed(world: World) -> None:
    """The contract between the engine and the gate: after ``assess`` the current
    basis of every record is the basis of its newest assessment."""
    documented = world.documented()
    bare = world.used(PUBLIC_DOMAIN)
    world.add_record("ghost-asset")
    elsewhere = make_channel()
    with world.database.transaction() as connection:
        ChannelRepository(connection).add(elsewhere)
    foreign = world.registry.register(
        elsewhere.id,
        IMAGE,
        LICENSED,
        title="Foreign",
        source="x",
        license_ref="CC-BY-4.0",
        actor=USER,
    )
    world.provenance(foreign, license_name="CC BY 4.0", proof="invoice 9")
    world.add_record(foreign.id)
    world.assess()

    with world.database.transaction() as connection:
        assessments = RightsAssessmentRepository(connection)
        freshness = RepositoryFreshness(
            assessments,
            AssetRepository(connection),
            ProvenanceRepository(connection),
        )
        records = RightsRecordRepository(connection).list_by_content_item(world.item.id)
        assert {r.asset_ref for r in records} == {
            documented.id,
            bare.id,
            "ghost-asset",
            foreign.id,
        }
        for record in records:
            newest = assessments.latest(record.id)
            assert freshness.current_basis(record.asset_ref, world.channel.id) == (
                newest.asset_id,
                newest.provenance_id,
                newest.rules_version,
            )
            assert freshness.assessed_basis(record.id) == (
                newest.asset_id,
                newest.provenance_id,
                newest.rules_version,
            )
        assert freshness.current_basis(documented.id, world.channel.id)[1] is not None
        assert freshness.current_basis(bare.id, world.channel.id) == (
            bare.id,
            None,
            RULES_VERSION,
        )
        ghost = freshness.current_basis("ghost-asset", world.channel.id)
        assert ghost == (None, None, RULES_VERSION)
        assert freshness.current_basis(foreign.id, world.channel.id) == (
            None,
            None,
            RULES_VERSION,
        )


def _freshness(world: World, connection, **options) -> RepositoryFreshness:
    return RepositoryFreshness(
        RightsAssessmentRepository(connection),
        AssetRepository(connection),
        ProvenanceRepository(connection),
        **options,
    )


@pytest.mark.parametrize("value", ["", "  ", " v1", "v1 ", None, 1])
def test_freshness_refuses_a_bad_rules_version(world: World, value) -> None:
    with world.database.transaction() as connection, pytest.raises(ValueError):
        _freshness(world, connection, rules_version=value)


def test_another_rules_version_makes_the_assessment_outdated(world: World) -> None:
    asset = world.documented()
    world.assess()
    assert codes(world.evaluate()) == []

    with world.database.transaction() as connection:
        record = RightsRecordRepository(connection).list_by_content_item(world.item.id)[
            0
        ]
        freshness = _freshness(world, connection, rules_version="rights-rules-v2")
        assert freshness.current_basis(asset.id, world.channel.id).rules_version == (
            "rights-rules-v2"
        )
        assert freshness.assessed_basis(record.id).rules_version == RULES_VERSION
        default = _freshness(world, connection)
        assert default.current_basis(asset.id, world.channel.id).rules_version == (
            RULES_VERSION
        )

    context = GateContext(world.approved, ContentStatus.PUBLISHING, USER, EVALUATED)
    with world.database.transaction() as connection:
        records = RightsRecordRepository(connection)
        outdated = RightsGate(
            records,
            freshness=_freshness(world, connection, rules_version="rights-rules-v2"),
        ).evaluate(context)
        current = RightsGate(records, freshness=_freshness(world, connection)).evaluate(
            context
        )
    assert [r.code for r in outdated.reasons] == ["rights.rules_outdated"]
    assert outdated.reasons[0].message == (
        f"Asset {asset.id} was assessed with outdated rights rules."
    )
    assert current.is_passed


def test_an_old_rules_row_is_kept_and_a_new_assessment_is_appended(
    world: World,
) -> None:
    asset = world.documented()
    record = world.record_of(asset.id)
    with world.database.transaction() as connection:
        facts = (
            AssetRepository(connection).get(asset.id),
            ProvenanceRepository(connection).latest(asset.id),
        )
        outcome = classify(facts[0], world.channel.id, facts[1])
        old = dataclasses.replace(
            RightsAssessment.create(
                record.id, world.item.id, outcome, assessed_by=SYSTEM, clock=lambda: T0
            ),
            rules_version="rights-rules-v0",
        )
        RightsAssessmentRepository(connection).add(old)
        RightsRecordRepository(connection).update(
            record.with_risk_level(LOW, clock=world.clock),
            expected_updated_at=record.updated_at,
        )

    assert codes(world.evaluate()) == ["rights.rules_outdated"]

    world.assess()

    with world.database.transaction() as connection:
        history = RightsAssessmentRepository(connection).list_by_rights_record(
            record.id
        )
    assert [a.rules_version for a in history] == ["rights-rules-v0", RULES_VERSION]
    assert history[0] == old
    assert codes(world.evaluate()) == []


def test_an_unregistered_asset_current_basis_carries_the_version(
    world: World,
) -> None:
    with world.database.transaction() as connection:
        basis = _freshness(world, connection).current_basis("ghost", world.channel.id)
    assert tuple(basis) == (None, None, RULES_VERSION)


def test_a_changed_provenance_makes_the_assessment_stale_until_it_is_redone(
    world: World,
) -> None:
    asset = world.used(PUBLIC_DOMAIN)
    world.assess()
    assert codes(world.evaluate()) == []

    world.provenance(asset, source_url="https://archive.example/a", proof="page 3")
    stale = world.evaluate()

    assert codes(stale) == ["rights.assessment_stale"]
    assert messages(stale) == [f"Asset {asset.id} has no current rights assessment."]
    assert world.record_of(asset.id).risk_level is MEDIUM

    world.assess()
    assert codes(world.evaluate()) == []


def test_a_new_asset_provenance_after_a_low_assessment_is_stale_too(
    world: World,
) -> None:
    asset = world.documented()
    world.assess()
    assert codes(world.evaluate()) == []

    world.provenance(asset, license_name="CC BY 4.0", proof="invoice 8")

    assert codes(world.evaluate()) == ["rights.assessment_stale"]


def test_a_record_set_to_low_without_an_assessment_is_stale(world: World) -> None:
    asset = world.used(PUBLIC_DOMAIN)
    record = world.record_of(asset.id)
    with world.database.transaction() as connection:
        RightsRecordRepository(connection).update(
            record.with_risk_level(LOW, clock=world.clock),
            expected_updated_at=record.updated_at,
        )

    assert codes(world.evaluate()) == ["rights.assessment_stale"]


def test_a_level_reason_has_priority_over_a_stale_one(world: World) -> None:
    asset = world.used(UNKNOWN)
    world.assess()
    world.provenance(asset, proof="a proof")

    assert codes(world.evaluate()) == ["rights.unresolved_high"]


def test_an_unregistered_asset_is_high_and_never_stale(world: World) -> None:
    world.add_record("ghost-asset")

    before = world.evaluate()
    world.assess()
    after = world.evaluate()

    assert codes(before) == ["rights.unresolved_unknown"]
    assert codes(after) == ["rights.unresolved_high"]
    assert codes(world.evaluate()) == ["rights.unresolved_high"]


def test_an_unregistered_asset_resolved_by_a_user_passes(world: World) -> None:
    world.add_record("ghost-asset")
    world.assess()
    world.resolve(world.record_of("ghost-asset"))

    assert codes(world.evaluate()) == []


def test_a_resolved_record_passes_even_when_its_assessment_is_stale(
    world: World,
) -> None:
    asset = world.used(PUBLIC_DOMAIN)
    world.assess()
    world.resolve(world.record_of(asset.id))
    world.provenance(asset, source_url="https://archive.example/a")

    assert codes(world.evaluate()) == []


def test_a_resolved_record_that_was_never_assessed_passes(world: World) -> None:
    asset = world.used(UNKNOWN)
    world.resolve(world.record_of(asset.id))

    assert codes(world.evaluate()) == []


# safe messages


def test_no_reason_holds_a_url_or_a_text(world: World) -> None:
    secret_url = f"https://{SECRET}.example/path?id={SECRET}"
    licensed = world.used(
        LICENSED,
        title=f"The {SECRET} title",
        source=f"{SECRET}.example",
        license_ref=f"{SECRET} licence",
    )
    world.provenance(
        licensed,
        source_url=secret_url,
        license_name=f"{SECRET} licence",
        license_url=secret_url,
        proof=f"{SECRET} proof",
    )
    stale = world.used(PUBLIC_DOMAIN, title=f"{SECRET} public")
    world.used(UNKNOWN, title=f"{SECRET} unknown")
    world.add_record(f"{SECRET}-ghost")
    world.assess()
    world.provenance(stale, source_url=secret_url, proof=f"{SECRET} proof")

    report = world.evaluate()

    dumped = json.dumps(report.as_dict(), ensure_ascii=False)
    assert "rights.assessment_stale" in codes(report)
    assert "http" not in dumped
    assert f"{SECRET} " not in dumped
    assert f"{SECRET}.example" not in dumped
    for message in messages(report):
        assert message.startswith("Asset ")


def test_bootstrap_builds_the_gate_without_touching_the_database(
    tmp_path: Path,
) -> None:
    container = build_container(
        Settings(environment=Environment.TEST, database_path=tmp_path / "none.db")
    )

    gate = container.resolve(PublishGate)

    assert gate.gate_names == GATE_ORDER
    assert not (tmp_path / "none.db").exists()
