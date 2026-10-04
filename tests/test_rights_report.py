"""G-082 Rights Report: a machine-readable rights report of one content item.

Rules the user approved on 2026-10-04:

- one report per content item at any status, computed on demand and never stored:
  no table, no route, no audit event, nothing written;
- everything is read in one transaction; the clock must be timezone-aware UTC;
- records, attached assets with provenance flags, the newest disclosure decision
  and the verdict of the real rights gate (with freshness) for a publish;
- a flag for every sensitive field, never a URL, licence, owner, proof, checksum,
  title or the free text of a record.
"""

import dataclasses
import json
from datetime import UTC, datetime, timedelta, timezone
from http import HTTPStatus
from pathlib import Path

import pytest

from ai_youtube_agent.bootstrap import build_container
from ai_youtube_agent.content.asset import Asset, AssetCategory, AssetKind
from ai_youtube_agent.content.asset_registry import AssetRegistry
from ai_youtube_agent.content.disclosure import FACT_NAMES
from ai_youtube_agent.content.disclosure_decider import DisclosureDecider
from ai_youtube_agent.content.disclosure_rule import DISCLOSURE_RULES, DisclosureFacts
from ai_youtube_agent.content.hook_generator import ContentItemNotFoundError
from ai_youtube_agent.content.provenance_recorder import ProvenanceRecorder
from ai_youtube_agent.content.rights import RightsRecord, RiskLevel
from ai_youtube_agent.content.rights_assessment import (
    RULES_VERSION,
    RightsAssessment,
    classify,
)
from ai_youtube_agent.content.rights_report import (
    REPORT_SCHEMA_VERSION,
    RightsReport,
    RightsReporter,
)
from ai_youtube_agent.content.rights_risk_engine import RightsRiskEngine
from ai_youtube_agent.core.audit import Actor, ActorKind, AuditLog, InMemoryAuditSink
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
from ai_youtube_agent.core.db.repositories.review import RightsRecordRepository
from ai_youtube_agent.core.db.repositories.rights_assessment import (
    RightsAssessmentRepository,
)
from ai_youtube_agent.core.errors import DomainError
from ai_youtube_agent.core.gates import GateName
from ai_youtube_agent.core.publish_gate import PublishGate
from factories import (
    make_artifact,
    make_channel,
    make_content_item,
    make_strategy_profile,
)

T0 = datetime(2026, 10, 4, 12, 0, tzinfo=UTC)
FIXED = T0 + timedelta(hours=1)
USER = Actor(ActorKind.USER, "owner")
SYSTEM = Actor(ActorKind.SYSTEM, "pipeline")
SECRET = "zebrafish"
SHA = "ab" * 32
IMAGE = AssetKind.IMAGE
GENERATED, LICENSED, PUBLIC_DOMAIN, USER_OWNED, UNKNOWN = (
    AssetCategory.GENERATED,
    AssetCategory.LICENSED,
    AssetCategory.PUBLIC_DOMAIN,
    AssetCategory.USER_OWNED,
    AssetCategory.UNKNOWN,
)
LOW, MEDIUM, HIGH, UNKNOWN_LEVEL = (
    RiskLevel.LOW,
    RiskLevel.MEDIUM,
    RiskLevel.HIGH,
    RiskLevel.UNKNOWN,
)
TOP_KEYS = [
    "schema_version",
    "content_item_id",
    "channel_id",
    "generated_at",
    "worst_level",
    "records",
    "assets",
    "disclosure",
    "gate",
]
ALL_MISSING = [
    "attribution",
    "file_sha256",
    "license",
    "license_url",
    "owner",
    "proof",
    "source_url",
]


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
        self.decider = DisclosureDecider(database, audit, clock=lambda: FIXED)
        self.reporter = self.with_levels()

    def with_levels(self, *levels: RightsBlockLevel, **kwargs) -> RightsReporter:
        settings = Settings(
            environment=Environment.TEST,
            database_path=self.database.path,
            rights_block_levels=frozenset(levels or {RightsBlockLevel.HIGH}),
        )
        kwargs.setdefault("clock", lambda: FIXED)
        return RightsReporter(self.database, settings, **kwargs)

    def new_item(self, **overrides) -> ContentItem:
        item = dataclasses.replace(
            make_content_item(self.channel, self.strategy), **overrides
        )
        with self.database.transaction() as connection:
            ContentItemRepository(connection).add(item)
        return item

    def asset(
        self, category: AssetCategory = UNKNOWN, item: ContentItem | None = None, **ov
    ) -> Asset:
        """A registered asset attached to ``item`` (so it has a rights record)."""
        self.titles += 1
        target = item or self.item
        arguments = {
            "title": f"Asset {self.titles}",
            "source": "stock.example",
            "actor": USER,
        } | ov
        if category is GENERATED:
            self.versions += 1
            artifact = make_artifact(target, version=self.versions)
            with self.database.transaction() as connection:
                ArtifactRepository(connection).add(artifact)
            arguments |= {"source": "mock-image", "artifact_id": artifact.id}
        if category is LICENSED:
            arguments.setdefault("license_ref", "CC-BY-4.0")
        if category is USER_OWNED:
            arguments |= {"source": "user", "owner": "Lan"}
        asset = self.registry.register(self.channel.id, IMAGE, category, **arguments)
        self.registry.attach(asset.id, target.id, actor=USER)
        return asset

    def provenance(self, asset: Asset, **details):
        return self.recorder.record(asset.id, actor=USER, **details)

    def records(self, item: ContentItem | None = None) -> list[RightsRecord]:
        with self.database.transaction() as connection:
            return RightsRecordRepository(connection).list_by_content_item(
                (item or self.item).id
            )

    def record_of(self, asset: Asset) -> RightsRecord:
        return next(r for r in self.records() if r.asset_ref == asset.id)

    def update_record(self, record: RightsRecord) -> None:
        with self.database.transaction() as connection:
            current = RightsRecordRepository(connection).get(record.id)
            RightsRecordRepository(connection).update(
                record, expected_updated_at=current.updated_at
            )

    def set_level(self, asset: Asset, level: RiskLevel) -> None:
        self.update_record(
            self.record_of(asset).with_risk_level(level, clock=self.clock)
        )

    def resolve(self, asset: Asset) -> None:
        self.update_record(self.record_of(asset).resolve(actor=USER, clock=self.clock))

    def assess(self) -> None:
        self.engine.assess(self.item.id, actor=SYSTEM)

    def report(self, item: ContentItem | None = None) -> RightsReport:
        return self.reporter.report((item or self.item).id)

    def as_dict(self, item: ContentItem | None = None) -> dict:
        return self.report(item).to_dict()

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
                table: [tuple(r) for r in connection.execute(f"SELECT * FROM {table}")]
                for table in tables
            }


@pytest.fixture
def world(database: Database) -> World:
    return World(database)


def gate_codes(report: RightsReport) -> list[str]:
    return [r.code for r in report.gate.blocking_records]


# an empty item and the shape


def test_an_item_without_records_gives_an_empty_report(world: World) -> None:
    report = world.report()

    assert report.records == ()
    assert report.assets == ()
    assert report.disclosure is None
    assert report.worst_level is None
    assert report.gate.blocks is False
    assert report.gate.blocking_records == ()
    assert report.content_item_id == world.item.id
    assert report.channel_id == world.channel.id
    assert report.generated_at == FIXED


def test_the_schema_version_is_a_constant(world: World) -> None:
    assert REPORT_SCHEMA_VERSION == "rights-report-v1"
    assert world.report().schema_version == REPORT_SCHEMA_VERSION
    assert world.as_dict()["schema_version"] == "rights-report-v1"


def test_the_dict_has_a_fixed_key_order_and_is_json_safe(world: World) -> None:
    world.asset(GENERATED)
    world.assess()

    data = world.as_dict()

    assert list(data) == TOP_KEYS
    assert list(data["records"][0]) == [
        "id",
        "asset_ref",
        "status",
        "level",
        "resolved_at",
        "has_license",
        "has_source",
        "assessment",
        "blocks",
        "gate_code",
    ]
    assert list(data["records"][0]["assessment"]) == [
        "id",
        "level",
        "rule_codes",
        "rules_version",
        "asset_id",
        "provenance_id",
        "created_at",
    ]
    assert list(data["gate"]) == ["blocks", "blocking_levels", "blocking_records"]
    assert json.loads(json.dumps(data)) == data
    assert data["generated_at"] == FIXED.isoformat()


@pytest.mark.parametrize("status", [ContentStatus.DRAFT, ContentStatus.PUBLISHED])
def test_any_item_status_gets_a_report(world: World, status) -> None:
    item = world.new_item(status=status)
    world.asset(UNKNOWN, item=item)

    report = world.report(item)

    assert report.content_item_id == item.id
    assert report.gate.blocks is True


def test_the_report_of_one_item_ignores_another_item(world: World) -> None:
    other = world.new_item()
    world.asset(UNKNOWN, item=other)

    assert world.report().records == ()
    assert len(world.report(other).records) == 1


# records and levels


@pytest.mark.parametrize(
    ("category", "details", "level"),
    [
        (GENERATED, None, "low"),
        (PUBLIC_DOMAIN, None, "medium"),
        (UNKNOWN, None, "high"),
        (LICENSED, None, "high"),
        (LICENSED, {"license_name": "CC", "proof": "inv"}, "low"),
    ],
)
def test_a_record_reports_the_level_the_engine_set(
    world: World, category, details, level
) -> None:
    asset = world.asset(category)
    if details:
        world.provenance(asset, **details)
    world.assess()

    (record,) = world.report().records

    assert record.level == level
    assert record.status == "unresolved"
    assert record.asset_ref == asset.id
    assert record.assessment is not None
    assert record.assessment.level == level
    assert record.assessment.rules_version == RULES_VERSION
    assert record.assessment.asset_id == asset.id


def test_an_unassessed_record_is_unknown_with_no_assessment(world: World) -> None:
    world.asset(GENERATED)

    (record,) = world.report().records

    assert record.level == "unknown"
    assert record.assessment is None
    assert record.blocks is True
    assert record.gate_code == "rights.unresolved_unknown"


def test_the_latest_of_several_assessments_is_reported(world: World) -> None:
    asset = world.asset(LICENSED)
    world.assess()
    provenance = world.provenance(asset, license_name="CC BY", proof="invoice")
    world.assess()

    (record,) = world.report().records

    assert record.assessment.provenance_id == provenance.id
    assert record.assessment.level == "low"
    assert record.assessment.rule_codes == ("licensed.documented",)
    with world.database.transaction() as connection:
        history = RightsAssessmentRepository(connection).list_by_rights_record(
            record.id
        )
    assert len(history) == 2
    assert record.assessment.id == history[-1].id


def test_records_are_ordered_by_creation_then_id(world: World) -> None:
    first = world.asset(GENERATED)
    second = world.asset(UNKNOWN)
    third = world.asset(PUBLIC_DOMAIN)

    refs = [r.asset_ref for r in world.report().records]

    assert refs == [r.asset_ref for r in world.records()]
    assert refs == [first.id, second.id, third.id]


def test_a_resolved_record_reports_status_and_time(world: World) -> None:
    asset = world.asset(UNKNOWN)
    world.assess()
    world.resolve(asset)

    (record,) = world.report().records

    assert record.status == "resolved"
    assert record.level == "high"
    assert record.resolved_at == world.record_of(asset).resolved_at
    assert record.blocks is False
    assert record.gate_code is None
    assert (
        world.as_dict()["records"][0]["resolved_at"] == record.resolved_at.isoformat()
    )


def test_the_record_flags_show_a_licence_and_a_source_not_their_text(
    world: World,
) -> None:
    asset = world.asset(GENERATED)
    record = world.record_of(asset)
    world.update_record(record.with_license(f"{SECRET}-licence", clock=world.clock))

    (reported,) = world.report().records

    assert reported.has_license is True
    assert reported.has_source is True
    assert SECRET not in json.dumps(world.as_dict())


def test_a_record_without_a_licence_has_no_licence_flag(world: World) -> None:
    world.asset(GENERATED)

    assert world.report().records[0].has_license is False


# worst level


@pytest.mark.parametrize(
    ("levels", "worst"),
    [
        ([LOW], "low"),
        ([LOW, MEDIUM], "medium"),
        ([LOW, MEDIUM, HIGH], "high"),
        ([LOW, HIGH, UNKNOWN_LEVEL], "unknown"),
        ([MEDIUM, UNKNOWN_LEVEL, LOW], "unknown"),
        ([UNKNOWN_LEVEL], "unknown"),
    ],
)
def test_the_worst_level_puts_unknown_last(world: World, levels, worst) -> None:
    for level in levels:
        world.set_level(world.asset(GENERATED), level)

    assert world.report().worst_level == worst


def test_the_worst_level_ignores_resolved_records(world: World) -> None:
    low = world.asset(GENERATED)
    high = world.asset(UNKNOWN)
    world.set_level(low, LOW)
    world.set_level(high, HIGH)
    world.resolve(high)

    assert world.report().worst_level == "low"


def test_the_worst_level_is_null_when_every_record_is_resolved(world: World) -> None:
    asset = world.asset(UNKNOWN)
    world.set_level(asset, HIGH)
    world.resolve(asset)

    assert world.report().worst_level is None


# the gate


@pytest.mark.parametrize(
    ("level", "blocks", "code"),
    [
        (LOW, False, None),
        (MEDIUM, False, None),
        (HIGH, True, "rights.unresolved_high"),
        (UNKNOWN_LEVEL, True, "rights.unresolved_unknown"),
    ],
)
def test_the_default_gate_blocks_high_and_unknown(
    world: World, level, blocks, code
) -> None:
    asset = world.asset(GENERATED)
    world.assess()
    world.set_level(asset, level)
    # Keep the assessment fresh: the level on the record is what the gate reads.
    report = world.report()

    gate_blocks = report.gate.blocks
    if code is None:
        # a fresh low or medium record passes (the assessment is current)
        assert gate_blocks is False
    else:
        assert gate_blocks is True
        assert gate_codes(report) == [code]
    assert report.records[0].blocks is blocks
    assert report.records[0].gate_code == code
    assert report.gate.blocking_levels == ("high", "unknown")


def test_medium_blocks_only_when_configured(world: World) -> None:
    asset = world.asset(PUBLIC_DOMAIN)
    world.assess()

    default = world.report()
    configured = world.with_levels(
        RightsBlockLevel.MEDIUM, RightsBlockLevel.HIGH
    ).report(world.item.id)

    assert default.records[0].level == "medium"
    assert default.gate.blocks is False
    assert configured.gate.blocks is True
    assert configured.gate.blocking_levels == ("high", "medium", "unknown")
    assert gate_codes(configured) == ["rights.unresolved_medium"]
    assert configured.gate.blocking_records[0].asset_ref == asset.id


def test_a_resolved_record_passes_the_gate(world: World) -> None:
    asset = world.asset(UNKNOWN)
    world.assess()
    assert world.report().gate.blocks is True

    world.resolve(asset)

    report = world.report()
    assert report.gate.blocks is False
    assert report.gate.blocking_records == ()


def test_a_new_provenance_makes_a_passing_record_stale(world: World) -> None:
    asset = world.asset(GENERATED)
    world.assess()
    assert world.report().gate.blocks is False

    world.provenance(asset, proof="a new proof")

    report = world.report()
    assert report.gate.blocks is True
    assert gate_codes(report) == ["rights.assessment_stale"]
    assert report.records[0].gate_code == "rights.assessment_stale"
    assert report.records[0].level == "low"


def test_an_unassessed_low_record_is_stale_not_high(world: World) -> None:
    asset = world.asset(GENERATED)
    world.set_level(asset, LOW)

    assert gate_codes(world.report()) == ["rights.assessment_stale"]


def test_an_old_rules_version_is_outdated(world: World) -> None:
    asset = world.asset(GENERATED)
    record = world.record_of(asset)
    with world.database.transaction() as connection:
        outcome = classify(
            AssetRepository(connection).get(asset.id),
            world.channel.id,
            ProvenanceRepository(connection).latest(asset.id),
        )
        old = dataclasses.replace(
            RightsAssessment.create(
                record.id, world.item.id, outcome, assessed_by=SYSTEM, clock=lambda: T0
            ),
            rules_version="rights-rules-v0",
        )
        RightsAssessmentRepository(connection).add(old)
    world.set_level(asset, LOW)

    report = world.report()

    assert gate_codes(report) == ["rights.rules_outdated"]
    assert report.records[0].assessment.rules_version == "rights-rules-v0"


def test_blocking_records_follow_record_order_with_their_own_codes(
    world: World,
) -> None:
    ok = world.asset(GENERATED)
    high = world.asset(UNKNOWN)
    stale = world.asset(GENERATED)
    world.assess()
    world.provenance(stale, proof="newer")

    report = world.report()

    assert [(r.asset_ref, r.code) for r in report.gate.blocking_records] == [
        (high.id, "rights.unresolved_high"),
        (stale.id, "rights.assessment_stale"),
    ]
    by_ref = {r.asset_ref: (r.blocks, r.gate_code) for r in report.records}
    assert by_ref[ok.id] == (False, None)
    assert by_ref[high.id] == (True, "rights.unresolved_high")
    assert by_ref[stale.id] == (True, "rights.assessment_stale")
    ids = {r.asset_ref: r.id for r in report.records}
    assert [r.record_id for r in report.gate.blocking_records] == [
        ids[high.id],
        ids[stale.id],
    ]


@pytest.mark.parametrize(
    "levels",
    [(RightsBlockLevel.HIGH,), (RightsBlockLevel.MEDIUM, RightsBlockLevel.HIGH)],
)
def test_the_gate_verdict_equals_the_publish_gate(
    world: World, database: Database, levels
) -> None:
    world.asset(GENERATED)
    world.asset(UNKNOWN)
    world.asset(PUBLIC_DOMAIN)
    stale = world.asset(GENERATED)
    resolved = world.asset(UNKNOWN)
    world.assess()
    world.provenance(stale, proof="newer")
    world.resolve(resolved)
    settings = Settings(
        environment=Environment.TEST,
        database_path=database.path,
        rights_block_levels=frozenset(levels),
    )
    approved = dataclasses.replace(world.item, status=ContentStatus.APPROVED)

    published = PublishGate(database, settings).evaluate(approved, actor=USER, at=FIXED)
    rights = next(r for r in published.results if r.gate is GateName.RIGHTS)
    report = world.with_levels(*levels).report(world.item.id)

    assert report.gate.blocks is (not rights.is_passed)
    assert gate_codes(report) == [r.code for r in rights.reasons]
    assert [r.asset_ref for r in report.gate.blocking_records] == [
        reason.message.split()[1] for reason in rights.reasons
    ]


def test_the_gate_output_has_no_free_text_message(world: World) -> None:
    world.asset(UNKNOWN)

    text = json.dumps(world.as_dict()["gate"])

    assert "Asset" not in text
    assert "not resolved" not in text


# assets and provenance flags


def test_no_attached_asset_gives_no_assets(world: World) -> None:
    world.asset(GENERATED, item=world.new_item())

    assert world.report().assets == ()


def test_an_asset_without_provenance_misses_everything(world: World) -> None:
    asset = world.asset(GENERATED)

    (reported,) = world.report().assets

    assert reported.asset_id == asset.id
    assert reported.kind == "image"
    assert reported.category == "generated"
    assert reported.provenance_id is None
    assert reported.missing == tuple(ALL_MISSING)
    assert not any(
        [
            reported.has_source_url,
            reported.has_license,
            reported.has_license_url,
            reported.has_proof,
            reported.has_owner,
            reported.has_file_sha256,
            reported.has_attribution,
        ]
    )


def test_a_partial_provenance_lists_what_is_missing(world: World) -> None:
    asset = world.asset(LICENSED)
    provenance = world.provenance(
        asset, source_url="https://archive.example/a", license_name="CC BY"
    )

    (reported,) = world.report().assets

    assert reported.provenance_id == provenance.id
    assert reported.has_source_url is True
    assert reported.has_license is True
    assert reported.has_proof is False
    assert reported.missing == (
        "attribution",
        "file_sha256",
        "license_url",
        "owner",
        "proof",
    )


def test_a_full_provenance_misses_nothing(world: World) -> None:
    asset = world.asset(LICENSED)
    world.provenance(
        asset,
        source_url="https://archive.example/a",
        license_ref="CC-BY-4.0",
        license_url="https://licence.example/by",
        attribution="Photo by someone",
        owner="Lan",
        file_sha256=SHA,
        proof="invoice 7",
    )

    (reported,) = world.report().assets

    assert reported.missing == ()
    assert world.as_dict()["assets"][0]["missing"] == []
    assert all(
        value is True
        for key, value in world.as_dict()["assets"][0].items()
        if key.startswith("has_")
    )


def test_the_newest_provenance_is_the_one_reported(world: World) -> None:
    asset = world.asset(LICENSED)
    world.provenance(asset, license_name="CC", proof="first")
    newest = world.provenance(asset, license_name="CC", owner="Lan")

    (reported,) = world.report().assets

    assert reported.provenance_id == newest.id
    assert reported.has_owner is True
    assert reported.has_proof is False


def test_assets_are_ordered_deterministically(world: World) -> None:
    created = [world.asset(GENERATED).id for _ in range(3)]

    ids = [a.asset_id for a in world.report().assets]

    assert sorted(ids) == sorted(created)
    assert ids == [a.asset_id for a in world.report().assets]


def test_no_sensitive_value_reaches_the_report(world: World) -> None:
    asset = world.asset(
        LICENSED,
        title=f"{SECRET} title",
        license_ref=f"{SECRET}-ref",
        attribution=f"{SECRET} attribution",
    )
    world.provenance(
        asset,
        source_url=f"https://{SECRET}.example/source",
        license_name=f"{SECRET} licence",
        license_url=f"https://{SECRET}.example/licence",
        license_ref=f"{SECRET}-ref",
        attribution=f"{SECRET} attribution",
        owner=f"{SECRET} owner",
        file_sha256=SHA,
        proof=f"{SECRET} proof",
    )
    record = world.record_of(asset)
    world.update_record(record.with_license(f"{SECRET}-record", clock=world.clock))
    world.assess()

    text = json.dumps(world.as_dict())

    assert SECRET not in text
    assert SHA not in text
    assert "http" not in text
    assert "stock.example" not in text


# disclosure


def test_no_decision_gives_a_null_disclosure(world: World) -> None:
    assert world.as_dict()["disclosure"] is None


def test_the_newest_disclosure_decision_is_reported(world: World) -> None:
    none = DisclosureFacts(**dict.fromkeys(FACT_NAMES, False))
    person = DisclosureFacts(
        **(dict.fromkeys(FACT_NAMES, False) | {"realistic_person": True})
    )
    world.decider.decide(world.item.id, none, rule_set=DISCLOSURE_RULES, actor=SYSTEM)
    first = world.report().disclosure
    newest = world.decider.decide(
        world.item.id, person, rule_set=DISCLOSURE_RULES, actor=SYSTEM
    )

    reported = world.report().disclosure

    assert first.decision == "not_required"
    assert first.rule_codes == ()
    assert reported.decision_id == newest.id
    assert reported.decision == "required"
    assert reported.rule_set_version == newest.rule_set_version
    assert reported.rule_codes == tuple(e.code for e in newest.rationale if e.triggered)
    assert len(reported.rule_codes) == 1
    assert reported.created_at == newest.created_at
    assert world.as_dict()["disclosure"]["created_at"] == newest.created_at.isoformat()


# errors, clock, determinism, read-only


def test_an_unknown_item_is_a_not_found_error(world: World) -> None:
    with pytest.raises(ContentItemNotFoundError) as caught:
        world.reporter.report("missing")

    assert isinstance(caught.value, DomainError)
    assert caught.value.to_public().http_status == HTTPStatus.NOT_FOUND


@pytest.mark.parametrize("value", [None, 7, b"x", ["a"]])
def test_a_non_text_id_is_a_type_error(world: World, value) -> None:
    with pytest.raises(TypeError, match="content_item_id"):
        world.reporter.report(value)


@pytest.mark.parametrize(
    "bad",
    [
        datetime(2026, 10, 4, 13, 0),
        datetime(2026, 10, 4, 13, 0, tzinfo=timezone(timedelta(hours=2))),
    ],
)
def test_a_naive_or_non_utc_clock_is_a_value_error(world: World, bad) -> None:
    reporter = world.with_levels(clock=lambda: bad)

    with pytest.raises(ValueError, match="clock") as caught:
        reporter.report(world.item.id)

    assert type(caught.value) is ValueError


def test_the_default_clock_gives_a_utc_time(world: World) -> None:
    reporter = world.with_levels(clock=None)

    assert reporter.report(world.item.id).generated_at.utcoffset() == timedelta(0)


def test_two_reports_are_equal(world: World) -> None:
    world.asset(GENERATED)
    world.asset(UNKNOWN)
    world.assess()
    world.decider.decide(
        world.item.id,
        DisclosureFacts(**dict.fromkeys(FACT_NAMES, False)),
        rule_set=DISCLOSURE_RULES,
        actor=SYSTEM,
    )

    first, second = world.report(), world.report()

    assert first == second
    assert first.to_dict() == second.to_dict()
    assert json.dumps(first.to_dict()) == json.dumps(second.to_dict())


def test_a_report_reads_and_writes_nothing(world: World) -> None:
    world.asset(GENERATED)
    world.asset(UNKNOWN)
    world.assess()
    before = world.snapshot()
    events = len(world.sink.events())

    world.report()
    world.report()

    assert world.snapshot() == before
    assert len(world.sink.events()) == events


def test_the_reports_are_frozen(world: World) -> None:
    world.asset(UNKNOWN)
    world.assess()
    report = world.report()

    for obj, name in [
        (report, "worst_level"),
        (report.records[0], "blocks"),
        (report.records[0].assessment, "level"),
        (report.gate, "blocks"),
        (report.gate.blocking_records[0], "code"),
    ]:
        with pytest.raises(dataclasses.FrozenInstanceError):
            setattr(obj, name, "x")


def test_the_reporter_does_not_change_the_gate_inputs(world: World) -> None:
    asset = world.asset(UNKNOWN)
    world.assess()
    before = world.record_of(asset)

    world.report()

    assert world.record_of(asset) == before


def test_bootstrap_registers_the_reporter(tmp_path: Path) -> None:
    container = build_container(
        Settings(environment=Environment.TEST, database_path=tmp_path / "a.db")
    )

    assert isinstance(container.resolve(RightsReporter), RightsReporter)
