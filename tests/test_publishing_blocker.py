"""G-084 Publishing Blocker: rights and policy failures block a publish.

Rules the user approved on 2026-10-04:

- ``PublishGate`` runs approval, daily limit, rights, policy and idempotency (the
  C-042 order); only the kill switch is deferred;
- the policy gate judges the item passed to ``evaluate`` with the G-080
  ``PolicyChecker`` (title and the brand's banned phrases), against the rule set
  ``policy`` at ``Settings.policy_rule_set_version``; an unknown version raises
  ``PolicyRuleSetNotFoundError`` when the gate is built, with no fallback;
- each blocking finding is one ``policy.failed`` reason (catalog order), a
  warning passes, a channel without a strategy fails closed (``gate.error``);
- rights keeps its own codes; both gates block side by side with their own codes;
- the gate is deterministic and read-only, and no reason or log holds a value.
"""

import ast
import dataclasses
import json
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from ai_youtube_agent.bootstrap import build_container
from ai_youtube_agent.content.approval import ApprovalRequest, ApprovalStatus
from ai_youtube_agent.content.asset import Asset, AssetCategory, AssetKind
from ai_youtube_agent.content.asset_registry import AssetRegistry
from ai_youtube_agent.content.policy_check import (
    POLICY_RULES,
    PolicyChecker,
    PolicyRuleSetCatalog,
    PolicyRuleSetNotFoundError,
    default_catalog,
)
from ai_youtube_agent.content.policy_rule import (
    MockAlwaysPassRule,
    MockBannedPhraseRule,
    MockTitleLengthRule,
    RuleSet,
)
from ai_youtube_agent.content.provenance_recorder import ProvenanceRecorder
from ai_youtube_agent.content.rights import RightsRecord
from ai_youtube_agent.content.rights_risk_engine import RightsRiskEngine
from ai_youtube_agent.content.strategy import Brand, Cadence
from ai_youtube_agent.core.audit import (
    Actor,
    ActorKind,
    AuditLog,
    AuditSink,
    InMemoryAuditSink,
)
from ai_youtube_agent.core.config import Environment, Settings
from ai_youtube_agent.core.content_item import ContentItem, ContentStatus
from ai_youtube_agent.core.db.database import Database
from ai_youtube_agent.core.db.repositories.channel import (
    ChannelRepository,
    StrategyProfileRepository,
)
from ai_youtube_agent.core.db.repositories.content import (
    ArtifactRepository,
    ContentItemRepository,
)
from ai_youtube_agent.core.db.repositories.publish import PublishJobRepository
from ai_youtube_agent.core.db.repositories.review import (
    ApprovalRequestRepository,
    RightsRecordRepository,
)
from ai_youtube_agent.core.gates import GateContext, GateName, GateOutcome, GateReport
from ai_youtube_agent.core.publish_gate import (
    DEFERRED_GATES,
    CheckedPolicySource,
    PublishGate,
)
from ai_youtube_agent.pipeline.idempotency import publish_key
from factories import (
    make_artifact,
    make_channel,
    make_content_item,
    make_publish_job,
    make_strategy_profile,
)

T0 = datetime(2026, 10, 4, 12, 0, tzinfo=UTC)
EVALUATED = T0 + timedelta(days=1)
USER = Actor(ActorKind.USER, "owner")
SYSTEM = Actor(ActorKind.SYSTEM, "pipeline")
LICENSED, PUBLIC_DOMAIN, UNKNOWN = (
    AssetCategory.LICENSED,
    AssetCategory.PUBLIC_DOMAIN,
    AssetCategory.UNKNOWN,
)
GATE_ORDER = (
    GateName.APPROVAL,
    GateName.DAILY_LIMIT,
    GateName.RIGHTS,
    GateName.POLICY,
    GateName.IDEMPOTENCY,
)
LONG_TITLE = "A" * 101
TITLE_MESSAGE = (
    "Policy rule mock.title_length (version 1) failed: "
    "Title length is outside the allowed range."
)
PHRASE_MESSAGE = (
    "Policy rule mock.banned_phrase (version 1) failed: "
    "Content contains a banned phrase."
)
SECRET_TITLE = "zebratitle"
SECRET_PHRASE = "zebraphrase"
SECRET_PASSWORD = "hunter2pass"
SECRET_TOKEN = "zebratoken"
SECRET_URL = (
    f"https://admin:{SECRET_PASSWORD}@zebraurl.example/file?token={SECRET_TOKEN}"
)
SECRET_LICENCE = "zebralicence text"
SECRET_OWNER = "zebraowner"
SECRET_PROOF = "zebraproof invoice"
SECRET_SHA = f"{0xDEADBEEF:064x}"
SECRETS = (
    SECRET_TITLE,
    SECRET_PHRASE,
    SECRET_PASSWORD,
    SECRET_TOKEN,
    "zebraurl",
    SECRET_LICENCE,
    SECRET_OWNER,
    SECRET_PROOF,
    SECRET_SHA,
    "http",
)


def catalog_of(*entries: tuple[RuleSet, tuple]) -> PolicyRuleSetCatalog:
    catalog = PolicyRuleSetCatalog()
    for rule_set, rules in entries:
        catalog.register(rule_set, rules)
    return catalog


def default_rules() -> tuple:
    return (MockTitleLengthRule(), MockBannedPhraseRule(), MockAlwaysPassRule())


class Clock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> datetime:
        self.now += timedelta(seconds=1)
        return self.now


class World:
    """A channel with a strategy and one item, real services and a fake clock.

    ``title`` and ``banned`` set the item title and the brand's banned phrases;
    ``strategy`` overrides strategy fields, ``strategy_in_channel=False`` puts the
    strategy in another channel, ``catalog`` is passed to ``PublishGate`` and
    other keywords go to ``Settings``."""

    def __init__(
        self,
        database: Database,
        *,
        title: str | None = None,
        banned: tuple[str, ...] = (),
        catalog: PolicyRuleSetCatalog | None = None,
        strategy_in_channel: bool = True,
        strategy: dict | None = None,
        **settings,
    ) -> None:
        self.database = database
        self.clock = Clock()
        self.titles = 0
        self.channel = make_channel()
        owner = self.channel if strategy_in_channel else make_channel()
        self.strategy = make_strategy_profile(
            owner,
            **{"brand": Brand("Money Minute", "calm", banned_phrases=banned)}
            | (strategy or {}),
        )
        self.item = make_content_item(self.channel, self.strategy)
        if title is not None:
            self.item = dataclasses.replace(self.item, title=title)
        with database.transaction() as connection:
            channels = ChannelRepository(connection)
            channels.add(self.channel)
            if owner is not self.channel:
                channels.add(owner)
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
        self.gate = PublishGate(database, self.settings, catalog=catalog)
        self.approval: ApprovalRequest | None = None

    @property
    def approved(self) -> ContentItem:
        return dataclasses.replace(self.item, status=ContentStatus.APPROVED)

    def used(self, category: AssetCategory = UNKNOWN, **overrides) -> Asset:
        self.titles += 1
        arguments = {
            "title": f"Asset {self.titles}",
            "source": "stock.example",
            "actor": USER,
        } | overrides
        if category is LICENSED:
            arguments.setdefault("license_ref", "CC-BY-4.0")
        asset = self.registry.register(
            self.channel.id, AssetKind.IMAGE, category, **arguments
        )
        self.registry.attach(asset.id, self.item.id, actor=USER)
        return asset

    def documented(self, **provenance) -> Asset:
        """A licensed asset with a licence and a proof: assessed as low."""
        asset = self.used(LICENSED)
        details = {"license_name": "CC BY 4.0", "proof": "invoice 7"} | provenance
        self.recorder.record(asset.id, actor=USER, **details)
        return asset

    def assess(self):
        return self.engine.assess(self.item.id, actor=SYSTEM)

    def add_record(self, asset_ref: str, **fields) -> RightsRecord:
        record = RightsRecord.create(
            self.item.id, asset_ref, source="stock.example", clock=self.clock
        )
        record = dataclasses.replace(record, **fields)
        with self.database.transaction() as connection:
            RightsRecordRepository(connection).add(record)
        return record

    def approve(self) -> None:
        """An approved request over one video artifact, so the approval gate passes."""
        artifact = make_artifact(self.item)
        self.approval = dataclasses.replace(
            ApprovalRequest.create(
                self.item.id, [artifact], requested_by=SYSTEM, clock=self.clock
            ),
            status=ApprovalStatus.APPROVED,
        )
        with self.database.transaction() as connection:
            ArtifactRepository(connection).add(artifact)
            ApprovalRequestRepository(connection).add(self.approval)

    def evaluate(self) -> GateReport:
        return self.gate.evaluate(self.approved, actor=USER, at=EVALUATED)

    def counts(self) -> dict[str, int]:
        with self.database.transaction() as connection:
            names = [
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table' "
                    "AND name NOT LIKE 'sqlite_%' ORDER BY name"
                )
            ]
            return {
                name: connection.execute(f'SELECT count(*) FROM "{name}"').fetchone()[0]
                for name in names
            }


@pytest.fixture
def world(database: Database) -> World:
    return World(database)


def result_of(report: GateReport, name: GateName):
    return next(r for r in report.results if r.gate is name)


def codes(report: GateReport, name: GateName) -> list[str]:
    return [reason.code for reason in result_of(report, name).reasons]


def messages(report: GateReport, name: GateName) -> list[str]:
    return [reason.message for reason in result_of(report, name).reasons]


def blocked_gates(report: GateReport) -> list[GateName]:
    return [r.gate for r in report.blocked]


# composition and enforcement guards


def test_policy_runs_after_rights_and_only_the_kill_switch_is_deferred(
    world: World,
) -> None:
    report = world.evaluate()

    assert world.gate.gate_names == GATE_ORDER
    assert [r.gate for r in report.results] == list(GATE_ORDER)
    assert DEFERRED_GATES == (GateName.KILL_SWITCH,)
    assert not set(DEFERRED_GATES) & set(GATE_ORDER)


def test_removing_rights_or_policy_from_the_set_is_caught(database: Database) -> None:
    """Fails if POLICY or RIGHTS ever leaves the publish gate."""
    world = World(database, title=LONG_TITLE)
    world.used(UNKNOWN)
    world.approve()

    report = world.evaluate()

    assert GateName.RIGHTS in world.gate.gate_names
    assert GateName.POLICY in world.gate.gate_names
    assert blocked_gates(report) == [GateName.RIGHTS, GateName.POLICY]
    assert result_of(report, GateName.RIGHTS).outcome is GateOutcome.BLOCK
    assert result_of(report, GateName.POLICY).outcome is GateOutcome.BLOCK


def test_the_publish_gate_imports_no_reporter_ai_or_http() -> None:
    import ai_youtube_agent.core.publish_gate as module

    tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
            imported.update(f"{node.module}.{alias.name}" for alias in node.names)
        elif isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
    forbidden = (
        "report",
        "Reporter",
        "providers",
        "text_generat",
        "fastapi",
        "httpx",
        "requests",
        "urllib",
        "anthropic",
        "openai",
    )
    assert not [name for name in imported if any(f in name for f in forbidden)]


# unchanged gates


def test_approval_and_daily_limit_codes_are_unchanged(database: Database) -> None:
    world = World(database)
    limited = World(database, strategy={"cadence": Cadence(0, 0)})
    limited.approve()

    report = world.evaluate()
    capped = limited.evaluate()

    assert blocked_gates(report) == [GateName.APPROVAL]
    assert codes(report, GateName.APPROVAL) == ["approval.missing"]
    assert blocked_gates(capped) == [GateName.DAILY_LIMIT]
    assert codes(capped, GateName.DAILY_LIMIT) == ["daily_limit.publish_reached"]


def test_a_recorded_publish_job_blocks_idempotency_and_nothing_is_written(
    world: World,
) -> None:
    world.approve()
    job = make_publish_job(
        world.approval, idempotency_key=publish_key(world.item.id, world.approval.id)
    )
    with world.database.transaction() as connection:
        PublishJobRepository(connection).add(job)
    before = world.counts()

    report = world.evaluate()

    assert blocked_gates(report) == [GateName.IDEMPOTENCY]
    assert codes(report, GateName.IDEMPOTENCY) == ["idempotency.duplicate_publish"]
    assert world.counts() == before


# rights


def test_an_unknown_rights_record_blocks(world: World) -> None:
    world.used(UNKNOWN)
    world.approve()

    report = world.evaluate()

    assert blocked_gates(report) == [GateName.RIGHTS]
    assert codes(report, GateName.RIGHTS) == ["rights.unresolved_unknown"]


def test_a_documented_and_assessed_asset_passes(world: World) -> None:
    world.documented()
    world.assess()
    world.approve()

    report = world.evaluate()

    assert report.is_passed
    assert report.reasons == ()


@pytest.mark.parametrize(
    ("levels", "expected"),
    [("high", []), ("medium,high", ["rights.unresolved_medium"])],
)
def test_medium_blocks_only_when_configured(
    database: Database, levels: str, expected: list[str]
) -> None:
    world = World(database, rights_block_levels=levels)
    world.used(PUBLIC_DOMAIN)
    world.assess()
    world.approve()

    report = world.evaluate()

    assert codes(report, GateName.RIGHTS) == expected
    assert report.is_passed is (not expected)


def test_a_stale_assessment_blocks(world: World) -> None:
    asset = world.used(PUBLIC_DOMAIN)
    world.assess()
    world.recorder.record(asset.id, actor=USER, source_url="https://archive.example/a")
    world.approve()

    report = world.evaluate()

    assert codes(report, GateName.RIGHTS) == ["rights.assessment_stale"]
    assert blocked_gates(report) == [GateName.RIGHTS]


# policy


def test_a_title_over_the_limit_blocks_with_one_reason(database: Database) -> None:
    world = World(database, title=LONG_TITLE)
    world.approve()

    report = world.evaluate()

    assert blocked_gates(report) == [GateName.POLICY]
    assert codes(report, GateName.POLICY) == ["policy.failed"]
    assert messages(report, GateName.POLICY) == [TITLE_MESSAGE]


def test_a_banned_phrase_in_the_title_blocks(database: Database) -> None:
    world = World(
        database, title="Five bank fees: get rich quick", banned=("Rich Quick",)
    )
    world.approve()

    report = world.evaluate()

    assert blocked_gates(report) == [GateName.POLICY]
    assert codes(report, GateName.POLICY) == ["policy.failed"]
    assert messages(report, GateName.POLICY) == [PHRASE_MESSAGE]


def test_one_reason_per_blocking_finding_in_catalog_order(database: Database) -> None:
    world = World(database, title=LONG_TITLE + " banned", banned=("banned",))
    world.approve()

    report = world.evaluate()

    assert codes(report, GateName.POLICY) == ["policy.failed", "policy.failed"]
    assert messages(report, GateName.POLICY) == [TITLE_MESSAGE, PHRASE_MESSAGE]


def test_a_warning_passes_the_policy_gate(database: Database) -> None:
    catalog = catalog_of(
        (POLICY_RULES, (MockTitleLengthRule(), MockBannedPhraseRule(blocking=False)))
    )
    world = World(
        database, title="Bank fees, warned", banned=("warned",), catalog=catalog
    )
    world.approve()

    report = world.evaluate()

    assert result_of(report, GateName.POLICY).outcome is GateOutcome.PASS
    assert codes(report, GateName.POLICY) == []
    assert report.is_passed


def test_a_clean_item_passes_the_policy_gate(world: World) -> None:
    report = world.evaluate()

    assert result_of(report, GateName.POLICY).outcome is GateOutcome.PASS


def test_a_blocking_rule_followed_by_an_always_pass_rule_still_blocks(
    database: Database,
) -> None:
    catalog = catalog_of((POLICY_RULES, (MockTitleLengthRule(), MockAlwaysPassRule())))
    world = World(database, title=LONG_TITLE, catalog=catalog)
    world.approve()

    report = world.evaluate()

    assert codes(report, GateName.POLICY) == ["policy.failed"]
    assert not report.is_passed


def test_the_policy_source_only_checks_the_item_being_evaluated(
    world: World,
) -> None:
    profiles = StrategyProfileRepository(None)
    checker = PolicyChecker(default_catalog())
    without = CheckedPolicySource(profiles, checker, POLICY_RULES, None)
    context = GateContext(world.approved, ContentStatus.PUBLISHING, USER, EVALUATED)
    other = CheckedPolicySource(profiles, checker, POLICY_RULES, context)

    with pytest.raises(ValueError) as caught:
        without.list_by_content_item(world.item.id)
    assert world.item.id not in str(caught.value)
    with pytest.raises(ValueError):
        other.list_by_content_item("another-item")


# rule set version


def test_an_unknown_version_raises_from_the_constructor(database: Database) -> None:
    with pytest.raises(PolicyRuleSetNotFoundError):
        PublishGate(
            database,
            Settings(
                environment=Environment.TEST,
                database_path=database.path,
                policy_rule_set_version=2,
            ),
        )


def test_an_unknown_version_raises_from_the_container(tmp_path: Path) -> None:
    container = build_container(
        Settings(
            environment=Environment.TEST,
            database_path=tmp_path / "none.db",
            policy_rule_set_version=2,
        )
    )

    with pytest.raises(PolicyRuleSetNotFoundError):
        container.resolve(PublishGate)
    assert not (tmp_path / "none.db").exists()


def test_a_catalog_without_the_configured_version_has_no_fallback(
    database: Database,
) -> None:
    only_v2 = catalog_of((RuleSet("policy", 2), default_rules()))
    settings = Settings(environment=Environment.TEST, database_path=database.path)

    with pytest.raises(PolicyRuleSetNotFoundError):
        PublishGate(database, settings, catalog=only_v2)


def test_a_catalog_with_the_configured_version_is_used(database: Database) -> None:
    only_v2 = catalog_of((RuleSet("policy", 2), (MockTitleLengthRule(),)))
    world = World(
        database, title=LONG_TITLE, catalog=only_v2, policy_rule_set_version=2
    )
    world.approve()

    report = world.evaluate()

    assert messages(report, GateName.POLICY) == [TITLE_MESSAGE]


# rights and policy together


def test_a_rights_block_with_a_policy_pass_blocks_only_rights(world: World) -> None:
    world.used(UNKNOWN)
    world.approve()

    report = world.evaluate()

    assert blocked_gates(report) == [GateName.RIGHTS]
    assert result_of(report, GateName.POLICY).outcome is GateOutcome.PASS


def test_a_rights_pass_with_a_policy_block_blocks_only_policy(
    database: Database,
) -> None:
    world = World(database, title=LONG_TITLE)
    world.documented()
    world.assess()
    world.approve()

    report = world.evaluate()

    assert blocked_gates(report) == [GateName.POLICY]
    assert result_of(report, GateName.RIGHTS).outcome is GateOutcome.PASS


def test_both_blocks_keep_their_own_codes(database: Database) -> None:
    world = World(database, title=LONG_TITLE)
    world.used(UNKNOWN)
    world.approve()

    report = world.evaluate()

    assert blocked_gates(report) == [GateName.RIGHTS, GateName.POLICY]
    assert codes(report, GateName.RIGHTS) == ["rights.unresolved_unknown"]
    assert codes(report, GateName.POLICY) == ["policy.failed"]
    assert [r.code for r in report.reasons] == [
        "rights.unresolved_unknown",
        "policy.failed",
    ]


def test_a_medium_record_not_configured_and_a_policy_warning_are_allowed(
    database: Database,
) -> None:
    catalog = catalog_of(
        (POLICY_RULES, (MockTitleLengthRule(), MockBannedPhraseRule(blocking=False)))
    )
    world = World(database, title="Fees, warned", banned=("warned",), catalog=catalog)
    world.used(PUBLIC_DOMAIN)
    world.assess()
    world.approve()

    report = world.evaluate()

    assert report.is_passed
    assert report.reasons == ()


def test_every_gate_passing_allows_the_publish(world: World) -> None:
    world.documented()
    world.assess()
    world.approve()

    report = world.evaluate()

    assert report.is_passed
    assert world.gate.ensure_can_publish(world.approved, actor=USER, at=EVALUATED)


# no values in reasons or logs


def _plant_secrets(world: World) -> None:
    licensed = world.used(
        LICENSED, source=SECRET_URL, license_ref=SECRET_LICENCE, title=SECRET_TITLE
    )
    world.recorder.record(
        licensed.id,
        actor=USER,
        license_name=SECRET_LICENCE,
        owner=SECRET_OWNER,
        file_sha256=SECRET_SHA,
        proof=SECRET_PROOF,
    )
    world.used(UNKNOWN, source=SECRET_URL, title=f"{SECRET_TITLE} unknown")
    world.add_record("ghost-asset", source=SECRET_URL, license=SECRET_LICENCE)
    world.assess()


def _assert_no_secret(text: str) -> None:
    for secret in SECRETS:
        assert secret not in text, secret


def test_no_reason_holds_a_planted_value(database: Database) -> None:
    world = World(
        database,
        title=f"{SECRET_TITLE} {SECRET_PHRASE} " + "x" * 100,
        banned=(SECRET_PHRASE,),
    )
    _plant_secrets(world)
    world.approve()

    report = world.evaluate()

    assert blocked_gates(report) == [GateName.RIGHTS, GateName.POLICY]
    dumped = json.dumps(
        [(reason.code, reason.message) for reason in report.reasons],
        ensure_ascii=False,
    )
    _assert_no_secret(dumped)
    _assert_no_secret(json.dumps(report.as_dict(), ensure_ascii=False))


def test_a_channel_without_a_strategy_fails_closed_without_values(
    database: Database, caplog: pytest.LogCaptureFixture
) -> None:
    world = World(
        database,
        title=f"{SECRET_TITLE} {SECRET_PHRASE}",
        banned=(SECRET_PHRASE,),
        strategy_in_channel=False,
    )
    _plant_secrets(world)
    world.approve()
    caplog.clear()

    with caplog.at_level(logging.DEBUG):
        report = world.evaluate()

    assert codes(report, GateName.POLICY) == ["gate.error"]
    assert result_of(report, GateName.POLICY).outcome is GateOutcome.BLOCK
    assert not report.is_passed
    dumped = json.dumps(
        [(reason.code, reason.message) for reason in report.reasons],
        ensure_ascii=False,
    )
    _assert_no_secret(dumped)
    assert "gate failed" in caplog.text
    _assert_no_secret(caplog.text)
    for record in caplog.records:
        _assert_no_secret(json.dumps(getattr(record, "fields", {}), default=str))
        _assert_no_secret(record.getMessage())


# determinism and purity


def test_two_evaluations_at_the_same_time_are_equal(database: Database) -> None:
    world = World(database, title=LONG_TITLE)
    world.used(UNKNOWN)
    world.approve()

    first = world.evaluate()
    second = world.evaluate()

    assert first == second
    assert first.as_dict() == second.as_dict()


def test_the_gate_writes_nothing_and_audits_nothing(database: Database) -> None:
    world = World(database, title=LONG_TITLE, banned=("A",))
    world.used(UNKNOWN)
    world.approve()
    before = world.counts()
    events = len(world.sink.events())
    container = build_container(world.settings)
    gate = container.resolve(PublishGate)
    container_events = len(container.resolve(AuditSink).events())

    for _ in range(3):
        world.evaluate()
        gate.evaluate(world.approved, actor=USER, at=EVALUATED)

    assert world.counts() == before
    assert len(world.sink.events()) == events
    assert len(container.resolve(AuditSink).events()) == container_events
