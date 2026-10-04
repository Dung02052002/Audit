"""G-083 Policy Report: a machine-readable policy report of one content item.

Rules approved by the user: a reporting layer over the real ``PolicyChecker``
(no rule evaluation of its own), computed on demand and never stored, one read
transaction, an unknown rule set fails before any read, and the report never
holds the title, description, tags or banned phrases.
"""

import dataclasses
import json
from datetime import UTC, datetime, timedelta, timezone
from http import HTTPStatus
from pathlib import Path

import pytest

from ai_youtube_agent.bootstrap import build_container
from ai_youtube_agent.content.hook_generator import ContentItemNotFoundError
from ai_youtube_agent.content.policy_check import (
    POLICY_RULES,
    PolicyRuleSetCatalog,
    PolicyRuleSetNotFoundError,
    PolicyStatus,
    default_catalog,
)
from ai_youtube_agent.content.policy_report import (
    REPORT_SCHEMA_VERSION,
    FindingReport,
    PolicyReport,
    PolicyReporter,
    RuleOutcomeReport,
)
from ai_youtube_agent.content.policy_rule import (
    MockAlwaysPassRule,
    MockTitleLengthRule,
    PolicyContext,
    RuleResult,
    RuleSet,
)
from ai_youtube_agent.content.strategy import Brand
from ai_youtube_agent.content.strategy_settings import StrategyNotFoundError
from ai_youtube_agent.core.config import Environment, Settings
from ai_youtube_agent.core.db.database import Database
from ai_youtube_agent.core.db.repositories.channel import (
    ChannelRepository,
    StrategyProfileRepository,
)
from ai_youtube_agent.core.db.repositories.content import ContentItemRepository
from factories import make_channel, make_content_item, make_strategy_profile

FIXED = datetime(2026, 10, 4, 13, 0, tzinfo=UTC)
PASS, WARN, BLOCK = PolicyStatus.PASS, PolicyStatus.WARN, PolicyStatus.BLOCK
TOP_KEYS = [
    "schema_version",
    "content_item_id",
    "channel_id",
    "generated_at",
    "rule_set",
    "status",
    "blocks",
    "outcomes",
    "findings",
    "blocking_findings",
]
OUTCOME_KEYS = ["rule_id", "version", "status", "code", "message", "field", "blocking"]
FINDING_KEYS = ["rule_id", "rule_version", "blocking", "message"]
CATALOG_ORDER = ["mock.title_length", "mock.banned_phrase", "mock.always_pass"]
SECRET_URL = "https://user:pass@host/x"
SECRET_TOKEN = "sk-abcdef1234567890abcdef"


def _outcome(status: PolicyStatus, rule_id: str = "r.one") -> RuleOutcomeReport:
    return RuleOutcomeReport(
        rule_id, 1, status.value, f"{rule_id}.code", "Message.", None, status is BLOCK
    )


def _finding(blocking: bool, rule_id: str = "r.one") -> FindingReport:
    return FindingReport(rule_id, 1, blocking, "Message.")


def _report(**overrides) -> PolicyReport:
    arguments = {
        "content_item_id": "item-1",
        "channel_id": "channel-1",
        "generated_at": FIXED,
        "rule_set": POLICY_RULES,
        "status": PASS,
        "blocks": False,
        "outcomes": (_outcome(PASS),),
        "findings": (),
        "blocking_findings": (),
    }
    return PolicyReport(**(arguments | overrides))


def _mixed() -> PolicyReport:
    warn, block = _finding(False, "r.warn"), _finding(True, "r.block")
    return _report(
        status=BLOCK,
        blocks=True,
        outcomes=(
            _outcome(PASS, "r.ok"),
            _outcome(WARN, "r.warn"),
            _outcome(BLOCK, "r.block"),
        ),
        findings=(warn, block),
        blocking_findings=(block,),
    )


# --- pure dataclass tests -------------------------------------------------------


def test_schema_version() -> None:
    assert REPORT_SCHEMA_VERSION == "policy-report-v1"
    assert _report().schema_version == REPORT_SCHEMA_VERSION


def test_to_dict_key_order() -> None:
    data = _mixed().to_dict()

    assert list(data) == TOP_KEYS
    assert list(data["rule_set"]) == ["id", "version", "stored_version"]
    assert list(data["outcomes"][0]) == OUTCOME_KEYS
    assert list(data["findings"][0]) == FINDING_KEYS
    assert list(data["blocking_findings"][0]) == FINDING_KEYS


def test_to_dict_values() -> None:
    data = _mixed().to_dict()

    assert data["schema_version"] == "policy-report-v1"
    assert data["generated_at"] == FIXED.isoformat()
    assert data["rule_set"] == {
        "id": "policy",
        "version": 1,
        "stored_version": "policy-rules-v1",
    }
    assert data["status"] == "block"
    assert data["blocks"] is True


def test_json_round_trip() -> None:
    data = _mixed().to_dict()

    assert json.loads(json.dumps(data)) == data


@pytest.mark.parametrize(
    ("statuses", "expected"),
    [
        ((PASS,), PASS),
        ((PASS, PASS), PASS),
        ((PASS, WARN), WARN),
        ((WARN, WARN), WARN),
        ((PASS, BLOCK), BLOCK),
        ((WARN, BLOCK), BLOCK),
        ((BLOCK, PASS, WARN), BLOCK),
    ],
)
def test_status_blocks_and_findings_follow_the_outcomes(
    statuses: tuple[PolicyStatus, ...], expected: PolicyStatus
) -> None:
    outcomes = tuple(_outcome(s, f"r.{i}") for i, s in enumerate(statuses))
    findings = tuple(
        _finding(s is BLOCK, f"r.{i}") for i, s in enumerate(statuses) if s is not PASS
    )
    report = _report(
        status=expected,
        blocks=expected is BLOCK,
        outcomes=outcomes,
        findings=findings,
        blocking_findings=tuple(f for f in findings if f.blocking),
    )

    assert report.status is expected
    assert report.blocks is (expected is BLOCK)
    assert report.to_dict()["blocks"] is (expected is BLOCK)
    assert [o["status"] for o in report.to_dict()["outcomes"]] == [
        s.value for s in statuses
    ]


@pytest.mark.parametrize(
    ("claimed", "statuses"),
    [
        (PASS, (WARN,)),
        (PASS, (BLOCK,)),
        (WARN, (BLOCK,)),
        (WARN, (PASS,)),
        (BLOCK, (PASS,)),
        (BLOCK, (WARN,)),
        (PASS, (PASS, WARN, BLOCK)),
    ],
)
def test_a_wrong_status_is_refused(
    claimed: PolicyStatus, statuses: tuple[PolicyStatus, ...]
) -> None:
    outcomes = tuple(_outcome(s, f"r.{i}") for i, s in enumerate(statuses))

    with pytest.raises(ValueError, match="worst outcome status"):
        _report(status=claimed, blocks=claimed is BLOCK, outcomes=outcomes)


def test_a_downgrade_cannot_be_constructed() -> None:
    block = _finding(True)

    with pytest.raises(ValueError, match="worst outcome status"):
        _report(
            status=WARN,
            blocks=False,
            outcomes=(_outcome(BLOCK),),
            findings=(block,),
            blocking_findings=(block,),
        )


@pytest.mark.parametrize(
    ("status", "blocks"), [(PASS, True), (WARN, True), (BLOCK, False)]
)
def test_blocks_must_match_the_status(status: PolicyStatus, blocks: bool) -> None:
    with pytest.raises(ValueError, match="blocks"):
        _report(status=status, blocks=blocks, outcomes=(_outcome(status),))


def test_blocking_findings_must_be_the_blocking_findings() -> None:
    block, warn = _finding(True, "r.b"), _finding(False, "r.w")
    outcomes = (_outcome(WARN, "r.w"), _outcome(BLOCK, "r.b"))
    common = {"status": BLOCK, "blocks": True, "outcomes": outcomes}

    with pytest.raises(ValueError, match="blocking_findings"):
        _report(**common, findings=(warn, block), blocking_findings=())
    with pytest.raises(ValueError, match="blocking_findings"):
        _report(**common, findings=(warn, block), blocking_findings=(warn, block))
    with pytest.raises(ValueError, match="blocking_findings"):
        _report(**common, findings=(warn, block), blocking_findings=(block, block))


def test_a_hidden_blocking_outcome_is_refused() -> None:
    with pytest.raises(ValueError, match="findings"):
        _report(
            status=BLOCK,
            blocks=True,
            outcomes=(_outcome(BLOCK, "r.block"),),
            findings=(),
            blocking_findings=(),
        )


def test_a_mismatched_finding_is_refused() -> None:
    with pytest.raises(ValueError, match="findings"):
        _report(
            status=WARN,
            outcomes=(_outcome(WARN, "r.warn"),),
            findings=(_finding(False, "r.other"),),
        )


def test_a_mismatched_outcome_blocking_flag_is_refused() -> None:
    outcome = dataclasses.replace(_outcome(WARN, "r.warn"), blocking=True)

    with pytest.raises(ValueError, match="blocking"):
        _report(status=WARN, outcomes=(outcome,), findings=(_finding(False, "r.warn"),))


def test_refusal_messages_hold_no_values() -> None:
    with pytest.raises(ValueError) as caught:
        _report(status=WARN, outcomes=(_outcome(BLOCK, "secret.rule"),))

    assert "secret.rule" not in str(caught.value)


def test_empty_findings() -> None:
    data = _report().to_dict()

    assert data["findings"] == []
    assert data["blocking_findings"] == []
    assert data["status"] == "pass"
    assert data["blocks"] is False


def test_warn_only_report_has_no_blocking_findings() -> None:
    warn = _finding(False, "r.warn")
    report = _report(
        status=WARN,
        outcomes=(_outcome(WARN, "r.warn"),),
        findings=(warn,),
        blocking_findings=(),
    )

    data = report.to_dict()
    assert data["blocks"] is False
    assert len(data["findings"]) == 1
    assert data["blocking_findings"] == []


def test_order_is_kept() -> None:
    data = _mixed().to_dict()

    assert [o["rule_id"] for o in data["outcomes"]] == ["r.ok", "r.warn", "r.block"]
    assert [f["rule_id"] for f in data["findings"]] == ["r.warn", "r.block"]


def test_reports_are_frozen() -> None:
    with pytest.raises(dataclasses.FrozenInstanceError):
        _report().status = BLOCK  # type: ignore[misc]


def test_outcome_report_blocking_follows_the_status() -> None:
    assert _outcome(BLOCK).blocking is True
    assert _outcome(WARN).blocking is False
    assert _outcome(PASS).blocking is False


# --- database tests ---------------------------------------------------------------


class _WarnRule:
    rule_id = "test.warn"
    version = 1

    def evaluate(self, context: PolicyContext) -> RuleResult:
        return RuleResult(
            self.rule_id, self.version, False, False, "test.warn.hit", "Be careful."
        )


class _BlockRule:
    rule_id = "test.block"
    version = 2

    def evaluate(self, context: PolicyContext) -> RuleResult:
        return RuleResult(
            self.rule_id,
            self.version,
            False,
            True,
            "test.block.hit",
            "Not allowed.",
            "description",
        )


V2 = RuleSet("policy", 2)
V1_CUSTOM = RuleSet("custom", 1)
V1_OTHER = RuleSet("other", 1)


def _test_catalog() -> PolicyRuleSetCatalog:
    catalog = default_catalog()
    catalog.register(V1_CUSTOM, [_WarnRule(), MockAlwaysPassRule(), _BlockRule()])
    catalog.register(V2, [MockTitleLengthRule(), _WarnRule()])
    catalog.register(V1_OTHER, [MockAlwaysPassRule()])
    return catalog


class World:
    def __init__(self, database: Database) -> None:
        self.database = database
        self.channel = make_channel()
        self.strategy = make_strategy_profile(self.channel)
        with database.transaction() as connection:
            ChannelRepository(connection).add(self.channel)
            StrategyProfileRepository(connection).add(self.strategy)
        self.item = self.new_item()
        self.reporter = PolicyReporter(
            database, clock=lambda: FIXED, catalog=_test_catalog()
        )

    def new_item(self, **overrides):
        item = dataclasses.replace(
            make_content_item(self.channel, self.strategy), **overrides
        )
        with self.database.transaction() as connection:
            ContentItemRepository(connection).add(item)
        return item

    def counts(self) -> dict[str, int]:
        with self.database.transaction() as connection:
            names = [
                r[0]
                for r in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                    " AND name NOT LIKE 'sqlite_%'"
                )
            ]
            return {
                n: connection.execute(f'SELECT COUNT(*) FROM "{n}"').fetchone()[0]
                for n in names
            }


@pytest.fixture
def world(database: Database) -> World:
    return World(database)


def _branded(database: Database, *phrases: str, title: str | None = None) -> World:
    """A world whose strategy brand bans ``phrases``."""
    channel = make_channel()
    strategy = make_strategy_profile(
        channel, brand=Brand("Money Minute", "calm", banned_phrases=phrases)
    )
    with database.transaction() as connection:
        ChannelRepository(connection).add(channel)
        StrategyProfileRepository(connection).add(strategy)
    w = World.__new__(World)
    w.database, w.channel, w.strategy = database, channel, strategy
    w.item = w.new_item(**({"title": title} if title else {}))
    w.reporter = PolicyReporter(database, clock=lambda: FIXED, catalog=_test_catalog())
    return w


def _default(database: Database) -> PolicyReporter:
    return PolicyReporter(database, clock=lambda: FIXED)


def test_default_catalog_good_title_passes(world: World) -> None:
    report = _default(world.database).report(world.item.id, rule_set=POLICY_RULES)

    assert report.status is PASS
    assert report.blocks is False
    assert [o.rule_id for o in report.outcomes] == CATALOG_ORDER
    assert {o.status for o in report.outcomes} == {"pass"}
    assert report.findings == ()
    assert report.blocking_findings == ()
    assert report.content_item_id == world.item.id
    assert report.channel_id == world.channel.id
    assert report.generated_at == FIXED
    assert report.rule_set == POLICY_RULES


def test_long_title_blocks_on_the_title(database: Database) -> None:
    w = _branded(database, title="T" * 101)

    report = _default(database).report(w.item.id, rule_set=POLICY_RULES)

    assert report.status is BLOCK
    assert report.blocks is True
    blocked = [o for o in report.outcomes if o.status == "block"]
    assert [(o.rule_id, o.field, o.blocking) for o in blocked] == [
        ("mock.title_length", "title", True)
    ]
    assert [f.rule_id for f in report.blocking_findings] == ["mock.title_length"]
    assert report.findings == report.blocking_findings
    assert [o.rule_id for o in report.outcomes] == CATALOG_ORDER


def test_banned_phrase_in_the_title_blocks(database: Database) -> None:
    w = _branded(database, "bank fees")

    report = _default(database).report(w.item.id, rule_set=POLICY_RULES)

    assert report.status is BLOCK
    outcome = next(o for o in report.outcomes if o.rule_id == "mock.banned_phrase")
    assert (outcome.status, outcome.field) == ("block", "title")


def test_banned_phrase_in_the_description_blocks(database: Database) -> None:
    w = _branded(database, "forbidden")

    report = _default(database).report(
        w.item.id, rule_set=POLICY_RULES, description="This is FORBIDDEN stuff"
    )

    assert report.status is BLOCK
    outcome = next(o for o in report.outcomes if o.rule_id == "mock.banned_phrase")
    assert outcome.field == "description"


def test_banned_phrase_in_the_tags_only_passes(database: Database) -> None:
    w = _branded(database, "forbidden")

    report = _default(database).report(
        w.item.id, rule_set=POLICY_RULES, tags=("forbidden",)
    )

    assert report.status is PASS


def test_a_brand_without_banned_phrases_passes(database: Database) -> None:
    w = _branded(database)

    report = _default(database).report(
        w.item.id, rule_set=POLICY_RULES, description="anything"
    )

    assert report.status is PASS


def test_no_brand_passes(database: Database) -> None:
    channel = make_channel()
    strategy = make_strategy_profile(channel, brand=None)
    with database.transaction() as connection:
        ChannelRepository(connection).add(channel)
        StrategyProfileRepository(connection).add(strategy)
        item = make_content_item(channel, strategy)
        ContentItemRepository(connection).add(item)

    report = _default(database).report(
        item.id, rule_set=POLICY_RULES, description="bank fees"
    )

    assert report.status is PASS


def test_mixed_warn_and_block(world: World) -> None:
    report = world.reporter.report(world.item.id, rule_set=V1_CUSTOM)

    assert report.status is BLOCK
    assert report.blocks is True
    assert [(o.rule_id, o.version, o.status) for o in report.outcomes] == [
        ("test.warn", 1, "warn"),
        ("mock.always_pass", 1, "pass"),
        ("test.block", 2, "block"),
    ]
    assert [(f.rule_id, f.rule_version, f.blocking) for f in report.findings] == [
        ("test.warn", 1, False),
        ("test.block", 2, True),
    ]
    assert [f.rule_id for f in report.blocking_findings] == ["test.block"]
    assert report.findings[0].message == "Be careful."
    assert report.outcomes[2].message == "Not allowed."
    assert report.outcomes[2].field == "description"


def test_warn_only_set_does_not_block(world: World) -> None:
    report = world.reporter.report(world.item.id, rule_set=V2)

    assert report.status is WARN
    assert report.blocks is False
    assert len(report.findings) == 1
    assert report.blocking_findings == ()


def test_rule_set_version_is_preserved(world: World) -> None:
    report = world.reporter.report(world.item.id, rule_set=V2)

    assert report.rule_set == V2
    assert report.to_dict()["rule_set"] == {
        "id": "policy",
        "version": 2,
        "stored_version": "policy-rules-v2",
    }
    assert [o.rule_id for o in report.outcomes] == ["mock.title_length", "test.warn"]


def test_report_dict_is_json_safe_and_ordered(world: World) -> None:
    data = world.reporter.report(world.item.id, rule_set=V1_CUSTOM).to_dict()

    assert list(data) == TOP_KEYS
    assert json.loads(json.dumps(data)) == data
    assert data["generated_at"] == FIXED.isoformat()
    assert data["schema_version"] == REPORT_SCHEMA_VERSION


@pytest.mark.parametrize(
    "unknown", [RuleSet("nope", 1), RuleSet("policy", 99), RuleSet("policy", 3)]
)
def test_unknown_rule_set_is_not_found(world: World, unknown: RuleSet) -> None:
    with pytest.raises(PolicyRuleSetNotFoundError) as caught:
        world.reporter.report(world.item.id, rule_set=unknown)

    assert caught.value.default_http_status == HTTPStatus.NOT_FOUND


def test_there_is_no_fallback_to_another_version(database: Database) -> None:
    catalog = PolicyRuleSetCatalog()
    catalog.register(RuleSet("policy", 1), [MockAlwaysPassRule()])
    catalog.register(RuleSet("policy", 2), [MockAlwaysPassRule()])
    channel = make_channel()
    strategy = make_strategy_profile(channel)
    with database.transaction() as connection:
        ChannelRepository(connection).add(channel)
        StrategyProfileRepository(connection).add(strategy)
        item = make_content_item(channel, strategy)
        ContentItemRepository(connection).add(item)
    reporter = PolicyReporter(database, clock=lambda: FIXED, catalog=catalog)

    with pytest.raises(PolicyRuleSetNotFoundError):
        reporter.report(item.id, rule_set=RuleSet("policy", 3))


def test_bad_rule_set_is_raised_before_the_database(database: Database) -> None:
    reporter = PolicyReporter(database, clock=lambda: FIXED)

    with pytest.raises(PolicyRuleSetNotFoundError):
        reporter.report("no-such-item", rule_set=RuleSet("nope", 1))


def test_bad_rule_set_is_raised_before_the_clock(database: Database) -> None:
    def broken() -> datetime:
        raise AssertionError("the clock must not be read")

    reporter = PolicyReporter(database, clock=broken)

    with pytest.raises(PolicyRuleSetNotFoundError):
        reporter.report("no-such-item", rule_set=RuleSet("nope", 1))


def test_unknown_item_is_not_found(world: World) -> None:
    with pytest.raises(ContentItemNotFoundError):
        world.reporter.report("no-such-item", rule_set=V1_OTHER)


@pytest.mark.parametrize(
    ("kwargs", "name"),
    [
        ({"content_item_id": 5}, "content_item_id"),
        ({"content_item_id": None}, "content_item_id"),
        ({"rule_set": "policy"}, "rule_set"),
        ({"rule_set": ("policy", 1)}, "rule_set"),
        ({"description": 5}, "description"),
        ({"description": b"bytes"}, "description"),
        ({"tags": "tag"}, "tags"),
        ({"tags": {"tag"}}, "tags"),
        ({"tags": (1,)}, "tags"),
        ({"tags": ["ok", None]}, "tags"),
    ],
)
def test_wrong_argument_types(world: World, kwargs: dict, name: str) -> None:
    arguments = {"content_item_id": world.item.id, "rule_set": POLICY_RULES} | kwargs
    item_id = arguments.pop("content_item_id")

    with pytest.raises(TypeError, match=name):
        _default(world.database).report(item_id, **arguments)


def test_unknown_item_with_bad_tags_is_a_type_error(world: World) -> None:
    with pytest.raises(TypeError, match="tags"):
        world.reporter.report("no-such-item", rule_set=V1_OTHER, tags=(1,))


def test_a_channel_without_a_strategy_fails_closed(database: Database) -> None:
    channel_a, channel_b = make_channel(), make_channel()
    strategy_b = make_strategy_profile(channel_b)
    secret_title = f"Title {SECRET_TOKEN}"
    with database.transaction() as connection:
        ChannelRepository(connection).add(channel_a)
        ChannelRepository(connection).add(channel_b)
        StrategyProfileRepository(connection).add(strategy_b)
        item = dataclasses.replace(
            make_content_item(channel_a, strategy_b), title=secret_title
        )
        ContentItemRepository(connection).add(item)
    reporter = PolicyReporter(database, clock=lambda: FIXED)
    with database.transaction() as connection:
        before = connection.execute("SELECT COUNT(*) FROM content_items").fetchone()[0]

    with pytest.raises(StrategyNotFoundError) as caught:
        reporter.report(item.id, rule_set=POLICY_RULES, description=SECRET_URL)

    assert channel_a.id in str(caught.value)
    assert secret_title not in str(caught.value)
    assert SECRET_TOKEN not in str(caught.value)
    assert SECRET_URL not in str(caught.value)
    with database.transaction() as connection:
        after = connection.execute("SELECT COUNT(*) FROM content_items").fetchone()[0]
    assert after == before


def test_type_errors_hold_no_values(world: World) -> None:
    with pytest.raises(TypeError) as caught:
        world.reporter.report(world.item.id, rule_set=V1_OTHER, tags=(SECRET_TOKEN, 1))

    assert SECRET_TOKEN not in str(caught.value)


def test_tags_may_be_a_list(world: World) -> None:
    report = world.reporter.report(world.item.id, rule_set=V1_OTHER, tags=["a", "b"])

    assert report.status is PASS


def test_injected_clock_is_used(world: World) -> None:
    moment = datetime(2030, 1, 2, 3, 4, 5, tzinfo=UTC)
    reporter = PolicyReporter(world.database, clock=lambda: moment)

    report = reporter.report(world.item.id, rule_set=POLICY_RULES)

    assert report.generated_at == moment


def test_default_clock_is_aware_utc(world: World) -> None:
    before = datetime.now(UTC)

    report = PolicyReporter(world.database).report(world.item.id, rule_set=POLICY_RULES)

    assert before <= report.generated_at <= datetime.now(UTC)
    assert report.generated_at.utcoffset() == timedelta(0)


@pytest.mark.parametrize(
    "bad",
    [
        datetime(2026, 10, 4, 13, 0),
        datetime(2026, 10, 4, 13, 0, tzinfo=timezone(timedelta(hours=7))),
        "2026-10-04",
    ],
)
def test_bad_clock_is_a_value_error(world: World, bad: object) -> None:
    reporter = PolicyReporter(world.database, clock=lambda: bad)  # type: ignore[arg-type,return-value]

    with pytest.raises(ValueError, match="clock"):
        reporter.report(world.item.id, rule_set=POLICY_RULES)


def test_secrets_never_appear(database: Database) -> None:
    w = _branded(
        database,
        SECRET_URL,
        SECRET_TOKEN,
        title=f"Title {SECRET_TOKEN} with {SECRET_URL}",
    )

    report = _default(database).report(
        w.item.id,
        rule_set=POLICY_RULES,
        description=f"See {SECRET_URL} now",
        tags=(SECRET_TOKEN, "plain"),
    )

    dumped = json.dumps(report.to_dict())
    assert report.status is BLOCK
    for secret in (SECRET_URL, SECRET_TOKEN, "user:pass", "Title"):
        assert secret not in dumped


def test_secrets_never_appear_in_exceptions(world: World) -> None:
    with pytest.raises(TypeError) as type_error:
        world.reporter.report(
            world.item.id, rule_set=POLICY_RULES, tags=(SECRET_URL, 5)
        )
    with pytest.raises(ContentItemNotFoundError) as missing:
        _default(world.database).report(
            "no-such-item", rule_set=POLICY_RULES, description=SECRET_URL
        )

    assert SECRET_URL not in str(type_error.value)
    assert SECRET_URL not in str(missing.value)
    assert SECRET_TOKEN not in str(missing.value)


def test_report_is_read_only(world: World) -> None:
    before = world.counts()

    world.reporter.report(world.item.id, rule_set=V1_CUSTOM, description="x")
    with pytest.raises(ContentItemNotFoundError):
        world.reporter.report("no-such-item", rule_set=V1_OTHER)

    assert world.counts() == before
    assert "audit_events" not in before or before["audit_events"] == 0


def test_report_is_deterministic(world: World) -> None:
    first = world.reporter.report(world.item.id, rule_set=V1_CUSTOM)
    second = world.reporter.report(world.item.id, rule_set=V1_CUSTOM)

    assert first == second
    assert first.to_dict() == second.to_dict()


def test_report_works_at_any_item_status(world: World) -> None:
    other = world.new_item(title="Another item")

    report = world.reporter.report(other.id, rule_set=V1_OTHER)

    assert report.content_item_id == other.id


def test_checker_uses_the_resolved_catalog(world: World) -> None:
    """Rules registered only in the custom catalog are evaluated by the real checker."""
    report = world.reporter.report(world.item.id, rule_set=V1_CUSTOM)

    assert {o.rule_id for o in report.outcomes} == {
        "test.warn",
        "mock.always_pass",
        "test.block",
    }


def test_default_reporter_does_not_know_custom_sets(world: World) -> None:
    with pytest.raises(PolicyRuleSetNotFoundError):
        _default(world.database).report(world.item.id, rule_set=V1_CUSTOM)


def test_bootstrap_registers_the_policy_reporter(tmp_path: Path) -> None:
    container = build_container(
        Settings(environment=Environment.TEST, database_path=tmp_path / "a.db")
    )

    assert isinstance(container.resolve(PolicyReporter), PolicyReporter)
