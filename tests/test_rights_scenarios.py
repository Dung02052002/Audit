"""G-085 Rights Tests: publishing for safe, uncertain and blocked rights scenarios.

Black box over the real services (asset registry, provenance recorder, rights
risk engine, rights gate, publish gate) on the migrated ``database`` fixture.
Every scenario states its expected level and code as literal data.
"""

import dataclasses
import json
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from ai_youtube_agent.content.approval import ApprovalRequest, ApprovalStatus
from ai_youtube_agent.content.asset import Asset, AssetCategory, AssetKind
from ai_youtube_agent.content.asset_registry import AssetRegistry
from ai_youtube_agent.content.provenance_recorder import ProvenanceRecorder
from ai_youtube_agent.content.rights import RightsRecord, RiskLevel
from ai_youtube_agent.content.rights_assessment import RULES_VERSION
from ai_youtube_agent.content.rights_risk_engine import RightsRiskEngine
from ai_youtube_agent.core.audit import (
    Actor,
    ActorKind,
    AuditLog,
    InMemoryAuditSink,
)
from ai_youtube_agent.core.config import Environment, RightsBlockLevel, Settings
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
from ai_youtube_agent.core.publish_gate import PublishGate, RepositoryFreshness
from ai_youtube_agent.core.rights_gate import RightsGate, blocking_levels_for
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
SHA = "cd" * 32
LONG_TITLE = "A" * 101
LOW, MEDIUM, HIGH, UNKNOWN_LEVEL = (
    RiskLevel.LOW,
    RiskLevel.MEDIUM,
    RiskLevel.HIGH,
    RiskLevel.UNKNOWN,
)
GENERATED, LICENSED, PUBLIC_DOMAIN, USER_OWNED, UNKNOWN = (
    AssetCategory.GENERATED,
    AssetCategory.LICENSED,
    AssetCategory.PUBLIC_DOMAIN,
    AssetCategory.USER_OWNED,
    AssetCategory.UNKNOWN,
)
HIGH_ONLY = "high"
HIGH_MEDIUM = "high,medium"

SECRET_PASSWORD = "hunter2pass"
SECRET_TOKEN = "zebratoken"
SECRET_URL = (
    f"https://admin:{SECRET_PASSWORD}@zebraurl.example/file?token={SECRET_TOKEN}"
)
SECRET_QUERY_URL = f"https://archive.example/{SECRET_TOKEN}/a"
SECRET_LICENCE = "zebralicence text"
SECRET_OWNER = "zebraowner"
SECRET_PROOF = "zebraproof invoice"
SECRET_SHA = f"{0xDEADBEEF:064x}"
SECRETS = (
    SECRET_PASSWORD,
    SECRET_TOKEN,
    SECRET_LICENCE,
    SECRET_OWNER,
    SECRET_PROOF,
    SECRET_SHA,
)

# (category, provenance details or None, expected rule code)
SAFE = [
    pytest.param(
        LICENSED,
        {"license_name": "CC BY 4.0", "proof": "invoice 7"},
        "licensed.documented",
        id="licensed-proof",
    ),
    pytest.param(
        LICENSED,
        {"license_name": "CC BY 4.0", "license_url": "https://cc.example/by"},
        "licensed.documented",
        id="licensed-licence-url",
    ),
    pytest.param(
        PUBLIC_DOMAIN,
        {"source_url": "https://archive.example/a", "proof": "page 3"},
        "public_domain.documented",
        id="public-domain-source-proof",
    ),
    pytest.param(
        USER_OWNED,
        {"owner": "Lan", "proof": "raw file"},
        "user_owned.documented",
        id="user-owned-proof",
    ),
    pytest.param(
        USER_OWNED,
        {"owner": "Lan", "file_sha256": SHA},
        "user_owned.documented",
        id="user-owned-sha",
    ),
    pytest.param(GENERATED, None, "generated.declared", id="generated"),
]
MEDIUMS = [
    pytest.param(
        LICENSED,
        {"license_name": "CC BY 4.0"},
        "licensed.licence_without_evidence",
        id="licensed-licence-only",
    ),
    pytest.param(
        PUBLIC_DOMAIN, None, "public_domain.no_provenance", id="public-domain-none"
    ),
    pytest.param(
        PUBLIC_DOMAIN,
        {"proof": "archive page"},
        "public_domain.no_source_url",
        id="public-domain-proof-only",
    ),
    pytest.param(
        PUBLIC_DOMAIN,
        {"source_url": "https://archive.example/a"},
        "public_domain.source_only",
        id="public-domain-source-only",
    ),
    pytest.param(USER_OWNED, None, "user_owned.no_provenance", id="user-owned-none"),
    pytest.param(
        USER_OWNED, {"owner": "Lan"}, "user_owned.unproven", id="user-owned-owner-only"
    ),
]
HIGHS = [
    pytest.param(UNKNOWN, None, "asset.category_unknown", id="unknown-category"),
    pytest.param(LICENSED, None, "licensed.no_provenance", id="licensed-no-provenance"),
]


class Clock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> datetime:
        self.now += timedelta(seconds=1)
        return self.now


class World:
    def __init__(
        self, database: Database, *, title: str | None = None, **settings
    ) -> None:
        self.database = database
        self.clock = Clock()
        self.titles = 0
        self.versions = 0
        self.channel = make_channel()
        self.strategy = make_strategy_profile(self.channel)
        self.item = make_content_item(self.channel, self.strategy)
        if title is not None:
            self.item = dataclasses.replace(self.item, title=title)
        with database.transaction() as connection:
            ChannelRepository(connection).add(self.channel)
            StrategyProfileRepository(connection).add(self.strategy)
            ContentItemRepository(connection).add(self.item)
        self.sink = InMemoryAuditSink()
        audit = AuditLog(self.sink)
        self.registry = AssetRegistry(database, audit, clock=self.clock)
        self.recorder = ProvenanceRecorder(database, audit, clock=self.clock)
        self.engine = RightsRiskEngine(database, audit, clock=self.clock)
        self.settings = Settings(
            environment=Environment.TEST, database_path=database.path, **settings
        )
        self.gate = PublishGate(database, self.settings)
        self.artifacts: list = []
        self.approved_once = False

    @property
    def approved(self) -> ContentItem:
        return dataclasses.replace(self.item, status=ContentStatus.APPROVED)

    def approve(self) -> None:
        """Approve the current artifacts (the generated assets' artifacts, or one
        video), once, before the first evaluation."""
        self.approved_once = True
        artifacts = self.artifacts or [make_artifact(self.item)]
        request = dataclasses.replace(
            ApprovalRequest.create(
                self.item.id, artifacts, requested_by=SYSTEM, clock=self.clock
            ),
            status=ApprovalStatus.APPROVED,
        )
        with self.database.transaction() as connection:
            for artifact in self.artifacts or artifacts:
                if artifact not in self.artifacts:
                    ArtifactRepository(connection).add(artifact)
            ApprovalRequestRepository(connection).add(request)

    def used(self, category: AssetCategory, **overrides) -> Asset:
        self.titles += 1
        arguments = {
            "title": f"Asset {self.titles}",
            "source": "stock.example",
            "actor": USER,
        } | overrides
        if category is GENERATED:
            self.versions += 1
            artifact = make_artifact(self.item, version=100 + self.versions)
            with self.database.transaction() as connection:
                ArtifactRepository(connection).add(artifact)
            self.artifacts.append(artifact)
            arguments |= {"source": "mock-image", "artifact_id": artifact.id}
        if category is LICENSED:
            arguments.setdefault("license_ref", "CC-BY-4.0")
        if category is USER_OWNED:
            arguments |= {"source": "user", "owner": "Lan"}
        asset = self.registry.register(
            self.channel.id, AssetKind.IMAGE, category, **arguments
        )
        self.registry.attach(asset.id, self.item.id, actor=USER)
        return asset

    def scenario(self, category: AssetCategory, details: dict | None) -> Asset:
        asset = self.used(category)
        if details:
            self.recorder.record(asset.id, actor=USER, **details)
        return asset

    def foreign_asset(self) -> Asset:
        elsewhere = make_channel()
        with self.database.transaction() as connection:
            ChannelRepository(connection).add(elsewhere)
        asset = self.registry.register(
            elsewhere.id,
            AssetKind.IMAGE,
            LICENSED,
            title="Foreign",
            source="x",
            license_ref="CC-BY-4.0",
            actor=USER,
        )
        self.recorder.record(
            asset.id, actor=USER, license_name="CC BY 4.0", proof="invoice 9"
        )
        return asset

    def add_record(self, asset_ref: str, **fields) -> RightsRecord:
        record = RightsRecord.create(
            self.item.id, asset_ref, source="stock.example", clock=self.clock
        )
        record = dataclasses.replace(record, **fields)
        with self.database.transaction() as connection:
            RightsRecordRepository(connection).add(record)
        return record

    def assess(self):
        return self.engine.assess(self.item.id, actor=SYSTEM)

    def records(self) -> list[RightsRecord]:
        with self.database.transaction() as connection:
            return RightsRecordRepository(connection).list_by_content_item(self.item.id)

    def record_of(self, asset_ref: str) -> RightsRecord:
        return next(r for r in self.records() if r.asset_ref == asset_ref)

    def history(self, record: RightsRecord) -> list:
        with self.database.transaction() as connection:
            return RightsAssessmentRepository(connection).list_by_rights_record(
                record.id
            )

    def resolve(self, record: RightsRecord) -> None:
        resolved = record.resolve(actor=USER, clock=self.clock)
        with self.database.transaction() as connection:
            RightsRecordRepository(connection).update(
                resolved, expected_updated_at=record.updated_at
            )

    def evaluate(self) -> GateReport:
        if not self.approved_once:
            self.approve()
        return self.gate.evaluate(self.approved, actor=USER, at=EVALUATED)

    def gate_result(self, rules_version: str):
        """The rights gate result with a freshness source of another rules version."""
        context = GateContext(self.approved, ContentStatus.PUBLISHING, USER, EVALUATED)
        with self.database.transaction() as connection:
            gate = RightsGate(
                RightsRecordRepository(connection),
                freshness=RepositoryFreshness(
                    RightsAssessmentRepository(connection),
                    AssetRepository(connection),
                    ProvenanceRepository(connection),
                    rules_version=rules_version,
                ),
            )
            return gate.evaluate(context)

    def snapshot(self) -> dict[str, list[tuple]]:
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
                        f'SELECT * FROM "{table}" ORDER BY rowid'
                    )
                ]
                for table in tables
            }

    def count(self, table: str) -> int:
        with self.database.transaction() as connection:
            return connection.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0]


@pytest.fixture
def world(database: Database) -> World:
    return World(database)


def result_of(report: GateReport, name: GateName):
    return next(r for r in report.results if r.gate is name)


def rights_codes(report: GateReport) -> list[str]:
    return [r.code for r in result_of(report, GateName.RIGHTS).reasons]


def blocked_names(report: GateReport) -> list[GateName]:
    return [r.gate for r in report.blocked]


# 1. safe


@pytest.mark.parametrize(("category", "details", "code"), SAFE)
def test_a_safe_asset_is_low_and_the_publish_is_allowed(
    world: World, category, details, code
) -> None:
    asset = world.scenario(category, details)

    run = world.assess()

    (assessment,) = run.assessments
    assert (assessment.level, assessment.rule_codes) == (LOW, (code,))
    assert world.record_of(asset.id).risk_level is LOW
    report = world.evaluate()
    assert report.is_passed
    assert rights_codes(report) == []
    assert blocked_names(report) == []


# 2. uncertain


@pytest.mark.parametrize(("category", "details", "code"), MEDIUMS)
def test_an_uncertain_asset_is_medium(world: World, category, details, code) -> None:
    world.scenario(category, details)

    (assessment,) = world.assess().assessments

    assert (assessment.level, assessment.rule_codes) == (MEDIUM, (code,))


@pytest.mark.parametrize(("category", "details", "code"), MEDIUMS)
def test_medium_is_allowed_under_the_default_config(
    world: World, category, details, code
) -> None:
    world.scenario(category, details)
    world.assess()

    report = world.evaluate()

    assert report.is_passed
    assert rights_codes(report) == []


@pytest.mark.parametrize(("category", "details", "code"), MEDIUMS)
def test_medium_blocks_when_configured(
    database: Database, category, details, code
) -> None:
    world = World(database, rights_block_levels=HIGH_MEDIUM)
    asset = world.scenario(category, details)
    world.assess()

    report = world.evaluate()

    assert blocked_names(report) == [GateName.RIGHTS]
    assert rights_codes(report) == ["rights.unresolved_medium"]
    assert asset.id in result_of(report, GateName.RIGHTS).reasons[0].message


@pytest.mark.parametrize(("category", "details", "code"), HIGHS)
@pytest.mark.parametrize("config", [HIGH_ONLY, HIGH_MEDIUM])
def test_high_blocks_under_every_config(
    database: Database, category, details, code, config
) -> None:
    world = World(database, rights_block_levels=config)
    world.scenario(category, details)

    (assessment,) = world.assess().assessments

    assert (assessment.level, assessment.rule_codes) == (HIGH, (code,))
    report = world.evaluate()
    assert blocked_names(report) == [GateName.RIGHTS]
    assert rights_codes(report) == ["rights.unresolved_high"]


# 3. blocked


def test_a_never_assessed_record_is_unknown_and_blocks(world: World) -> None:
    world.scenario(LICENSED, {"license_name": "CC BY 4.0", "proof": "invoice 7"})

    report = world.evaluate()

    assert rights_codes(report) == ["rights.unresolved_unknown"]


def test_a_never_assessed_record_set_to_low_is_stale(world: World) -> None:
    asset = world.scenario(PUBLIC_DOMAIN, None)
    record = world.record_of(asset.id)
    with world.database.transaction() as connection:
        RightsRecordRepository(connection).update(
            record.with_risk_level(LOW, clock=world.clock),
            expected_updated_at=record.updated_at,
        )

    assert rights_codes(world.evaluate()) == ["rights.assessment_stale"]


def test_an_asset_of_another_channel_is_high(world: World) -> None:
    foreign = world.foreign_asset()
    world.add_record(foreign.id)

    world.assess()

    assert world.record_of(foreign.id).risk_level is HIGH
    report = world.evaluate()
    assert blocked_names(report) == [GateName.RIGHTS]
    assert rights_codes(report) == ["rights.unresolved_high"]


def test_an_unregistered_asset_is_high(world: World) -> None:
    world.add_record("ghost-asset")

    (assessment,) = world.assess().assessments

    assert assessment.level is HIGH
    assert rights_codes(world.evaluate()) == ["rights.unresolved_high"]


def test_an_unregistered_asset_is_never_stale(world: World) -> None:
    world.add_record("ghost-asset")
    world.assess()

    assert "rights.assessment_stale" not in rights_codes(world.evaluate())


def test_a_resolved_high_record_does_not_block(world: World) -> None:
    asset = world.scenario(UNKNOWN, None)
    world.assess()
    assert rights_codes(world.evaluate()) == ["rights.unresolved_high"]

    world.resolve(world.record_of(asset.id))

    assert world.evaluate().is_passed


def test_a_resolved_record_is_skipped_by_the_engine(world: World) -> None:
    asset = world.scenario(UNKNOWN, None)
    world.resolve(world.record_of(asset.id))

    run = world.assess()

    assert run.changed == 0
    assert world.history(world.record_of(asset.id)) == []


def test_one_blocking_record_among_safe_ones_blocks(world: World) -> None:
    world.scenario(GENERATED, None)
    world.scenario(LICENSED, {"license_name": "CC BY 4.0", "proof": "invoice 7"})
    bad = world.scenario(UNKNOWN, None)
    world.assess()

    report = world.evaluate()

    assert rights_codes(report) == ["rights.unresolved_high"]
    assert bad.id in result_of(report, GateName.RIGHTS).reasons[0].message


def test_each_blocking_record_gives_its_own_reason(world: World) -> None:
    world.scenario(UNKNOWN, None)
    world.scenario(LICENSED, None)
    world.assess()

    assert rights_codes(world.evaluate()) == [
        "rights.unresolved_high",
        "rights.unresolved_high",
    ]


def test_ensure_can_publish_raises_for_a_rights_block(world: World) -> None:
    world.scenario(UNKNOWN, None)
    world.assess()
    world.approve()

    with pytest.raises(GateBlockedError) as raised:
        world.gate.ensure_can_publish(world.approved, actor=USER, at=EVALUATED)

    assert GateName.RIGHTS in blocked_names(raised.value.report)
    assert rights_codes(raised.value.report) == ["rights.unresolved_high"]


def test_ensure_can_publish_raises_for_a_medium_when_configured(
    database: Database,
) -> None:
    world = World(database, rights_block_levels=HIGH_MEDIUM)
    world.scenario(PUBLIC_DOMAIN, None)
    world.assess()
    world.approve()

    with pytest.raises(GateBlockedError) as raised:
        world.gate.ensure_can_publish(world.approved, actor=USER, at=EVALUATED)

    assert rights_codes(raised.value.report) == ["rights.unresolved_medium"]


def test_ensure_can_publish_passes_for_a_safe_asset(world: World) -> None:
    world.scenario(GENERATED, None)
    world.assess()
    world.approve()

    report = world.gate.ensure_can_publish(world.approved, actor=USER, at=EVALUATED)

    assert report.is_passed


# 4. config matrix


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("high", {RightsBlockLevel.HIGH}),
        ("high,medium", {RightsBlockLevel.HIGH, RightsBlockLevel.MEDIUM}),
        (" medium , high ", {RightsBlockLevel.HIGH, RightsBlockLevel.MEDIUM}),
        ('["high", "medium"]', {RightsBlockLevel.HIGH, RightsBlockLevel.MEDIUM}),
        (["high"], {RightsBlockLevel.HIGH}),
    ],
)
def test_the_settings_accept_these_levels(value, expected) -> None:
    settings = Settings(environment=Environment.TEST, rights_block_levels=value)

    assert settings.rights_block_levels == frozenset(expected)


@pytest.mark.parametrize(
    "value",
    [
        "medium",
        "",
        "   ",
        "[]",
        "low",
        "low,high",
        "unknown,high",
        "bogus,high",
        "[bad",
    ],
)
def test_the_settings_reject_these_levels(value) -> None:
    with pytest.raises(ValidationError):
        Settings(environment=Environment.TEST, rights_block_levels=value)


def test_the_default_blocks_only_high() -> None:
    settings = Settings(environment=Environment.TEST)

    assert settings.rights_block_levels == frozenset({RightsBlockLevel.HIGH})


@pytest.mark.parametrize(
    ("configured", "expected"),
    [
        ([RightsBlockLevel.HIGH], {HIGH, UNKNOWN_LEVEL}),
        (
            [RightsBlockLevel.HIGH, RightsBlockLevel.MEDIUM],
            {HIGH, MEDIUM, UNKNOWN_LEVEL},
        ),
        ([RightsBlockLevel.MEDIUM], {MEDIUM, UNKNOWN_LEVEL}),
        ([], {UNKNOWN_LEVEL}),
    ],
)
def test_unknown_always_blocks_and_low_never_does(configured, expected) -> None:
    levels = blocking_levels_for(configured)

    assert levels == frozenset(expected)
    assert LOW not in levels


@pytest.mark.parametrize("config", [HIGH_ONLY, HIGH_MEDIUM])
def test_unknown_blocks_under_every_config(database: Database, config) -> None:
    world = World(database, rights_block_levels=config)
    world.scenario(GENERATED, None)

    assert rights_codes(world.evaluate()) == ["rights.unresolved_unknown"]


@pytest.mark.parametrize("config", [HIGH_ONLY, HIGH_MEDIUM])
def test_low_never_blocks(database: Database, config) -> None:
    world = World(database, rights_block_levels=config)
    world.scenario(GENERATED, None)
    world.assess()

    assert world.evaluate().is_passed


# 5. freshness


def test_a_current_assessment_is_usable(world: World) -> None:
    world.scenario(PUBLIC_DOMAIN, None)
    world.assess()

    assert rights_codes(world.evaluate()) == []


@pytest.mark.parametrize(
    "later",
    [
        {"source_url": "https://archive.example/a", "proof": "page 3"},
        {"proof": "another proof"},
        {"source_url": "https://archive.example/b"},
    ],
)
def test_a_provenance_recorded_after_the_assessment_is_stale(
    world: World, later
) -> None:
    asset = world.scenario(PUBLIC_DOMAIN, None)
    world.assess()
    world.recorder.record(asset.id, actor=USER, **later)

    report = world.evaluate()

    assert blocked_names(report) == [GateName.RIGHTS]
    assert rights_codes(report) == ["rights.assessment_stale"]


def test_a_second_provenance_makes_a_low_assessment_stale(world: World) -> None:
    asset = world.scenario(LICENSED, {"license_name": "CC BY 4.0", "proof": "i7"})
    world.assess()
    assert world.evaluate().is_passed

    world.recorder.record(asset.id, actor=USER, license_name="CC BY 4.0", proof="i8")

    assert rights_codes(world.evaluate()) == ["rights.assessment_stale"]


def test_reassessment_appends_one_row_and_is_current_again(world: World) -> None:
    asset = world.scenario(PUBLIC_DOMAIN, None)
    world.assess()
    record = world.record_of(asset.id)
    assert len(world.history(record)) == 1
    world.recorder.record(
        asset.id, actor=USER, source_url="https://archive.example/a", proof="page 3"
    )
    assert rights_codes(world.evaluate()) == ["rights.assessment_stale"]

    world.assess()

    history = world.history(record)
    assert [a.level for a in history] == [MEDIUM, LOW]
    assert history[-1].rule_codes == ("public_domain.documented",)
    assert world.evaluate().is_passed


def test_a_stale_high_record_reports_the_level_not_stale(world: World) -> None:
    asset = world.scenario(UNKNOWN, None)
    world.assess()
    world.recorder.record(asset.id, actor=USER, proof="a proof")

    assert rights_codes(world.evaluate()) == ["rights.unresolved_high"]


def test_another_rules_version_is_outdated(world: World) -> None:
    world.scenario(LICENSED, {"license_name": "CC BY 4.0", "proof": "invoice 7"})
    world.assess()

    outdated = world.gate_result("rights-rules-v999")
    current = world.gate_result(RULES_VERSION)

    assert outdated.gate is GateName.RIGHTS
    assert [r.code for r in outdated.reasons] == ["rights.rules_outdated"]
    assert current.is_passed


def test_stale_has_priority_over_outdated(world: World) -> None:
    asset = world.scenario(PUBLIC_DOMAIN, None)
    world.assess()
    world.recorder.record(asset.id, actor=USER, proof="new")

    result = world.gate_result("rights-rules-v999")

    assert [r.code for r in result.reasons] == ["rights.assessment_stale"]


def test_the_engine_stamps_the_current_rules_version(world: World) -> None:
    asset = world.scenario(GENERATED, None)
    world.assess()

    (row,) = world.history(world.record_of(asset.id))

    assert row.rules_version == RULES_VERSION


# 6. determinism


def test_assessing_twice_on_the_same_facts_adds_no_row(world: World) -> None:
    asset = world.scenario(LICENSED, {"license_name": "CC BY 4.0"})
    first = world.assess()
    record = world.record_of(asset.id)
    before = world.history(record)

    second = world.assess()

    assert world.record_of(asset.id) == record
    assert world.history(record) == before
    assert second.changed == 0
    assert [(a.level, a.rule_codes) for a in second.assessments] == [
        (a.level, a.rule_codes) for a in first.assessments
    ]
    assert world.record_of(asset.id).risk_level is MEDIUM


@pytest.mark.parametrize(("category", "details", "code"), SAFE + MEDIUMS + HIGHS)
def test_every_scenario_is_deterministic(world: World, category, details, code) -> None:
    asset = world.scenario(category, details)
    world.assess()
    count = world.count("rights_assessments")

    again = world.assess()

    assert world.count("rights_assessments") == count
    assert again.changed == 0
    assert again.assessments[0].rule_codes == (code,)
    assert len(world.history(world.record_of(asset.id))) == 1


# 7. read only


@pytest.mark.parametrize("setup", ["safe", "medium", "high", "stale", "unknown"])
def test_evaluating_changes_no_row(world: World, setup) -> None:
    if setup == "safe":
        world.scenario(GENERATED, None)
        world.assess()
    elif setup == "medium":
        world.scenario(PUBLIC_DOMAIN, None)
        world.assess()
    elif setup == "high":
        world.scenario(UNKNOWN, None)
        world.assess()
    elif setup == "stale":
        asset = world.scenario(PUBLIC_DOMAIN, None)
        world.assess()
        world.recorder.record(asset.id, actor=USER, proof="later")
    else:
        world.scenario(UNKNOWN, None)
    world.approve()
    # The snapshot covers every table, including audit_events.
    before = world.snapshot()

    world.evaluate()
    world.evaluate()

    assert world.snapshot() == before


def test_evaluating_leaves_the_rights_tables_unchanged(world: World) -> None:
    world.scenario(UNKNOWN, None)
    world.assess()
    tables = ("assets", "asset_provenance", "rights_assessments", "rights_records")
    before = {table: world.count(table) for table in tables}

    world.evaluate()

    assert {table: world.count(table) for table in tables} == before


def test_the_rights_decision_ignores_the_title(database: Database) -> None:
    world = World(database, title="A clean title")
    world.scenario(UNKNOWN, None)
    world.assess()
    before = rights_codes(world.evaluate())
    world.item = dataclasses.replace(world.item, title="Another clean title")

    after = rights_codes(world.evaluate())

    assert before == after == ["rights.unresolved_high"]


# 8. publish gate integration


def test_the_rights_gate_is_in_the_gate_list(world: World) -> None:
    world.scenario(UNKNOWN, None)
    world.assess()

    report = world.evaluate()

    assert GateName.RIGHTS in world.gate.gate_names
    assert GateName.RIGHTS in [r.gate for r in report.results]
    assert result_of(report, GateName.RIGHTS).outcome is GateOutcome.BLOCK
    assert GateName.RIGHTS in blocked_names(report)


def test_a_rights_block_with_a_policy_pass_blocks_only_rights(world: World) -> None:
    world.scenario(UNKNOWN, None)
    world.assess()

    report = world.evaluate()

    assert blocked_names(report) == [GateName.RIGHTS]
    assert result_of(report, GateName.POLICY).outcome is GateOutcome.PASS


def test_both_rights_and_policy_blocks_are_reported(database: Database) -> None:
    world = World(database, title=LONG_TITLE)
    world.scenario(UNKNOWN, None)
    world.assess()

    report = world.evaluate()

    assert blocked_names(report) == [GateName.RIGHTS, GateName.POLICY]
    assert rights_codes(report) == ["rights.unresolved_high"]
    policy = [r.code for r in result_of(report, GateName.POLICY).reasons]
    assert policy
    assert all(code.startswith("policy.") for code in policy)


def test_a_policy_block_alone_leaves_a_safe_rights_gate_passing(
    database: Database,
) -> None:
    world = World(database, title=LONG_TITLE)
    world.scenario(GENERATED, None)
    world.assess()

    report = world.evaluate()

    assert blocked_names(report) == [GateName.POLICY]
    assert rights_codes(report) == []


# 9. idempotency


def test_a_rights_blocked_evaluation_writes_no_publish_state(world: World) -> None:
    world.scenario(UNKNOWN, None)
    world.assess()
    world.approve()
    before = world.snapshot()

    report = world.evaluate()

    assert blocked_names(report) == [GateName.RIGHTS]
    assert world.count("publish_jobs") == 0
    assert world.snapshot() == before


@pytest.mark.parametrize("setup", ["safe", "blocked"])
def test_two_evaluations_at_the_same_time_are_equal(world: World, setup) -> None:
    world.scenario(GENERATED if setup == "safe" else UNKNOWN, None)
    world.assess()

    assert world.evaluate() == world.evaluate()


# 10. secrets


@pytest.mark.parametrize("config", [HIGH_ONLY, HIGH_MEDIUM])
def test_no_planted_value_reaches_the_report(database: Database, config) -> None:
    world = World(database, rights_block_levels=config)
    licensed = world.used(LICENSED, source=SECRET_URL, license_ref=SECRET_LICENCE)
    world.recorder.record(
        licensed.id,
        actor=USER,
        source_url=SECRET_QUERY_URL,
        license_name=SECRET_LICENCE,
        owner=SECRET_OWNER,
        file_sha256=SECRET_SHA,
        proof=SECRET_PROOF,
    )
    world.used(UNKNOWN, source=SECRET_URL)
    world.used(PUBLIC_DOMAIN, source=SECRET_URL)
    world.add_record("ghost-asset", source=SECRET_URL, license=SECRET_LICENCE)
    world.assess()
    world.recorder.record(
        licensed.id, actor=USER, license_name=SECRET_LICENCE, proof=SECRET_PROOF + " 2"
    )

    report = world.evaluate()

    assert report.blocked
    text = json.dumps(
        [(r.code, r.message) for gate in report.results for r in gate.reasons]
    )
    for secret in SECRETS:
        assert secret not in text, secret
    assert "zebraurl" not in text
