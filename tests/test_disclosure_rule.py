"""Tests for the AI disclosure rules (G-081)."""

import ast
import dataclasses
import itertools
from http import HTTPStatus
from pathlib import Path

import pytest

import ai_youtube_agent.content.disclosure_decider as decider_module
import ai_youtube_agent.content.disclosure_rule as rule_module
from ai_youtube_agent.content.asset import Asset, AssetCategory, AssetKind
from ai_youtube_agent.content.disclosure import (
    FACT_NAMES,
    DisclosureDecision,
    DisclosureEvaluation,
)
from ai_youtube_agent.content.disclosure_rule import (
    DISCLOSURE_RULES,
    GENERATED_VISUAL_KINDS,
    DisclosureFacts,
    DisclosureObservation,
    DisclosureRuleCatalog,
    DisclosureRuleSetNotFoundError,
    GeneratedRealisticVisualRule,
    RealisticEventRule,
    RealisticPersonRule,
    SyntheticVoiceOfRealPersonRule,
    default_disclosure_catalog,
    evaluate_disclosure,
)
from ai_youtube_agent.content.policy_check import (
    PolicyRuleSetNotFoundError,
    default_catalog,
)
from ai_youtube_agent.content.policy_rule import (
    PolicyContext,
    PolicyRuleError,
    RuleResult,
    RuleSet,
    mock_registry,
)
from ai_youtube_agent.core.errors import DomainError

SRC = Path(rule_module.__file__).parent.parent
RULE_IDS = (
    "disclosure.realistic_person",
    "disclosure.realistic_event",
    "disclosure.synthetic_voice_of_real_person",
    "disclosure.generated_realistic_visual",
)
GENERATED = AssetCategory.GENERATED
IMAGE = AssetKind.IMAGE
VIDEO = AssetKind.VIDEO_CLIP


def facts(**overrides) -> DisclosureFacts:
    values = dict.fromkeys(FACT_NAMES, False) | overrides
    return DisclosureFacts(**values)


def asset(
    category: AssetCategory = GENERATED,
    kind: AssetKind = IMAGE,
    asset_id: str = "asset-1",
) -> Asset:
    extra = {}
    if category is AssetCategory.GENERATED:
        extra = {"source": "mock-image"}
    elif category is AssetCategory.LICENSED:
        extra = {"source": "stock.example", "license_ref": "CC-BY-4.0"}
    elif category is AssetCategory.USER_OWNED:
        extra = {"source": "user", "owner": "Lan"}
    else:
        extra = {"source": "stock.example"}
    created = Asset.create("channel-1", kind, category, title=f"T {asset_id}", **extra)
    return dataclasses.replace(created, id=asset_id)


def evaluate(f=None, assets=(), **kwargs) -> DisclosureEvaluation:
    return evaluate_disclosure(
        "item-1",
        "channel-1",
        f or facts(),
        assets,
        rule_set=kwargs.pop("rule_set", DISCLOSURE_RULES),
        **kwargs,
    )


def codes(evaluation: DisclosureEvaluation) -> list[str]:
    return [entry.code for entry in evaluation.rationale]


def triggered(evaluation: DisclosureEvaluation) -> list[str]:
    return [e.rule_id for e in evaluation.rationale if e.triggered]


# the rule set and the catalog


def test_the_rule_set_is_stored_as_disclosure_rules_v1() -> None:
    assert RuleSet("disclosure", 1) == DISCLOSURE_RULES
    assert DISCLOSURE_RULES.stored_version == "disclosure-rules-v1"
    assert GENERATED_VISUAL_KINDS == (IMAGE, VIDEO)


def test_the_default_catalog_has_the_four_rules_in_order() -> None:
    evaluation = evaluate()

    assert [(e.rule_id, e.version, e.code) for e in evaluation.rationale] == [
        (RULE_IDS[0], 1, f"{RULE_IDS[0]}.ok"),
        (RULE_IDS[1], 1, f"{RULE_IDS[1]}.ok"),
        (RULE_IDS[2], 1, f"{RULE_IDS[2]}.ok"),
        (RULE_IDS[3], 1, f"{RULE_IDS[3]}.ok"),
    ]
    assert evaluation.rule_set == DISCLOSURE_RULES


def test_the_possible_codes_are_required_and_ok_per_rule() -> None:
    every = facts(
        realistic_person=True,
        realistic_event=True,
        synthetic_voice_of_real_person=True,
        realistic_visual=True,
    )
    seen = set(codes(evaluate(every, [asset()]))) | set(codes(evaluate()))

    assert seen == {
        f"{rule_id}.{kind}" for rule_id in RULE_IDS for kind in ("required", "ok")
    }


@pytest.mark.parametrize(
    "bits", list(itertools.product([False, True], repeat=4)), ids=str
)
@pytest.mark.parametrize("generated", [False, True])
def test_the_truth_table_of_the_four_flags_and_a_generated_visual(
    bits, generated
) -> None:
    person, event, voice, visual = bits
    evaluation = evaluate(
        facts(
            realistic_person=person,
            realistic_event=event,
            synthetic_voice_of_real_person=voice,
            realistic_visual=visual,
        ),
        [asset()] if generated else [],
    )

    expected = [
        rule_id
        for rule_id, on in zip(
            RULE_IDS, (person, event, voice, visual and generated), strict=True
        )
        if on
    ]
    assert triggered(evaluation) == expected
    assert evaluation.decision is (
        DisclosureDecision.REQUIRED if expected else DisclosureDecision.NOT_REQUIRED
    )


@pytest.mark.parametrize("flag", FACT_NAMES[:3])
def test_each_of_the_first_three_rules_alone_triggers(flag) -> None:
    evaluation = evaluate(facts(**{flag: True}))

    assert triggered(evaluation) == [f"disclosure.{flag}"]
    assert evaluation.decision is DisclosureDecision.REQUIRED


def test_the_fourth_rule_alone_triggers_with_a_generated_visual() -> None:
    evaluation = evaluate(facts(realistic_visual=True), [asset()])

    assert triggered(evaluation) == [RULE_IDS[3]]
    assert evaluation.decision is DisclosureDecision.REQUIRED


def test_a_generated_asset_alone_never_triggers() -> None:
    evaluation = evaluate(facts(), [asset(), asset(kind=VIDEO, asset_id="asset-2")])

    assert triggered(evaluation) == []
    assert evaluation.decision is DisclosureDecision.NOT_REQUIRED
    # but it is a source of the decision
    assert evaluation.sources.asset_ids == ("asset-1", "asset-2")


def test_the_fourth_rule_needs_a_realistic_visual_fact() -> None:
    assert triggered(evaluate(facts(realistic_visual=True), [])) == []


@pytest.mark.parametrize("kind", GENERATED_VISUAL_KINDS)
def test_the_fourth_rule_counts_generated_images_and_clips(kind) -> None:
    assert triggered(evaluate(facts(realistic_visual=True), [asset(kind=kind)]))


@pytest.mark.parametrize(
    "kind", [k for k in AssetKind if k not in GENERATED_VISUAL_KINDS]
)
def test_the_fourth_rule_ignores_other_generated_kinds(kind) -> None:
    evaluation = evaluate(facts(realistic_visual=True), [asset(kind=kind)])

    assert triggered(evaluation) == []
    assert evaluation.sources.asset_ids == ()


@pytest.mark.parametrize(
    "category", [c for c in AssetCategory if c is not AssetCategory.GENERATED]
)
@pytest.mark.parametrize("kind", GENERATED_VISUAL_KINDS)
def test_the_fourth_rule_ignores_assets_that_are_not_generated(category, kind) -> None:
    evaluation = evaluate(facts(realistic_visual=True), [asset(category, kind)])

    assert triggered(evaluation) == []
    assert evaluation.sources.asset_ids == ()


def test_the_sources_are_the_declared_facts_and_the_sorted_generated_ids() -> None:
    evaluation = evaluate(
        facts(realistic_visual=True, realistic_person=True),
        [
            asset(asset_id="b"),
            asset(asset_id="a"),
            asset(asset_id="b"),  # a duplicate is listed once
            asset(AssetCategory.LICENSED, asset_id="c"),
        ],
    )

    assert evaluation.sources.as_dict() == {
        "asset_ids": ["a", "b"],
        "facts": ["realistic_person", "realistic_visual"],
    }


def test_the_messages_are_static_and_hold_no_input() -> None:
    evaluation = evaluate(
        facts(realistic_person=True),
        [asset(asset_id="secret-asset-id")],
    )

    for entry in evaluation.rationale:
        assert "secret-asset-id" not in entry.message
        assert "item-1" not in entry.message
        assert "channel-1" not in entry.message
    assert evaluation.rationale[1].message == "Rule passed."


# results of one rule


def run_rule(rule_class, f, ids=()) -> RuleResult:
    rule = rule_class(DisclosureObservation(f, tuple(ids)))
    return rule.evaluate(PolicyContext("i", "c", "", "", (), ()))


@pytest.mark.parametrize(
    ("rule_class", "flag"),
    [
        (RealisticPersonRule, "realistic_person"),
        (RealisticEventRule, "realistic_event"),
        (SyntheticVoiceOfRealPersonRule, "synthetic_voice_of_real_person"),
    ],
)
def test_a_triggered_result_is_failed_non_blocking_and_has_no_field(
    rule_class, flag
) -> None:
    result = run_rule(rule_class, facts(**{flag: True}))

    assert (result.passed, result.blocking, result.field) == (False, False, None)
    assert result.code == f"{rule_class.rule_id}.required"
    assert result.version == 1


def test_a_passed_result_has_the_passed_message_and_no_field() -> None:
    result = run_rule(GeneratedRealisticVisualRule, facts(realistic_visual=True))

    assert (result.passed, result.blocking, result.field) == (True, False, None)
    assert (result.code, result.message) == (
        "disclosure.generated_realistic_visual.ok",
        "Rule passed.",
    )


def test_the_rules_ignore_the_text_of_the_context() -> None:
    observation = DisclosureObservation(facts(realistic_person=True), ())
    rules = [RealisticPersonRule(observation), RealisticEventRule(observation)]
    plain = PolicyContext("i", "c", "", "", (), ())
    texty = PolicyContext("i", "c", "AI voice", "deepfake", ("ai",), ("ai",))

    for rule in rules:
        assert rule.evaluate(plain) == rule.evaluate(texty)


def test_a_triggered_rule_gives_a_warning_through_the_checker() -> None:
    from ai_youtube_agent.content.policy_check import (
        PolicyChecker,
        PolicyInput,
        PolicyRuleSetCatalog,
        PolicyStatus,
    )

    observation = DisclosureObservation(facts(realistic_event=True), ())
    catalog = PolicyRuleSetCatalog()
    catalog.register(DISCLOSURE_RULES, [RealisticEventRule(observation)])

    result = PolicyChecker(catalog).check(
        PolicyInput("i", "c"), rule_set=DISCLOSURE_RULES
    )

    assert result.status is PolicyStatus.WARN


# bad facts


@pytest.mark.parametrize("bad", [1, 0, "yes", None, 1.0, "True", [True]])
@pytest.mark.parametrize("name", FACT_NAMES)
def test_a_fact_that_is_not_a_bool_is_a_type_error_naming_the_field(name, bad) -> None:
    values = dict.fromkeys(FACT_NAMES, False) | {name: bad}

    with pytest.raises(TypeError, match=name) as caught:
        DisclosureFacts(**values)

    assert str(caught.value) == f"{name} must be a bool"


def test_facts_have_no_defaults() -> None:
    with pytest.raises(TypeError):
        DisclosureFacts(realistic_person=True)  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        DisclosureFacts()  # type: ignore[call-arg]


def test_an_error_for_a_bad_fact_never_holds_the_value() -> None:
    with pytest.raises(TypeError) as caught:
        DisclosureFacts(
            realistic_person="token=hunter2",  # type: ignore[arg-type]
            realistic_event=False,
            synthetic_voice_of_real_person=False,
            realistic_visual=False,
        )

    assert "hunter2" not in str(caught.value)
    assert "hunter2" not in repr(caught.value)


def test_the_observation_and_evaluate_check_their_types() -> None:
    with pytest.raises(TypeError, match="facts"):
        DisclosureObservation("x", ())  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="generated_visual_asset_ids"):
        DisclosureObservation(facts(), ["a"])  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="observation"):
        RealisticPersonRule(facts())  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="facts"):
        evaluate_disclosure(
            "i",
            "c",
            True,
            [],
            rule_set=DISCLOSURE_RULES,  # type: ignore[arg-type]
        )
    with pytest.raises(TypeError, match="rule_set"):
        evaluate_disclosure("i", "c", facts(), [], rule_set=("disclosure", 1))  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="assets"):
        evaluate_disclosure("i", "c", facts(), ["x"], rule_set=DISCLOSURE_RULES)  # type: ignore[list-item]


# unknown rule sets


@pytest.mark.parametrize(
    "rule_set",
    [
        RuleSet("disclosure", 2),
        RuleSet("other", 1),
        RuleSet("policy", 1),
        RuleSet("rights", 1),
    ],
    ids=str,
)
def test_an_unknown_rule_set_is_a_404_with_a_static_message(rule_set) -> None:
    with pytest.raises(DisclosureRuleSetNotFoundError) as caught:
        evaluate(rule_set=rule_set)

    error = caught.value
    assert isinstance(error, DomainError)
    assert error.code == "domain.disclosure_rule_set_not_found"
    assert error.to_public().http_status == HTTPStatus.NOT_FOUND
    assert error.user_message == "This disclosure rule set does not exist."
    assert str(error) == "domain.disclosure_rule_set_not_found"  # no id or version
    assert error.detail == ""
    with pytest.raises(DisclosureRuleSetNotFoundError):
        default_disclosure_catalog().factory_for(rule_set)


# registration


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


def test_an_injected_v2_set_is_used_by_exact_lookup() -> None:
    catalog = default_disclosure_catalog()
    v2 = RuleSet("disclosure", 2)
    catalog.register(v2, [_Rule])

    one = evaluate(catalog=catalog)
    two = evaluate(rule_set=v2, catalog=catalog)

    assert [e.rule_id for e in two.rationale] == ["custom.rule"]
    assert two.rule_set == v2
    assert len(one.rationale) == 4  # v1 is unchanged


def test_an_injected_blocking_rule_is_refused() -> None:
    catalog = DisclosureRuleCatalog()
    catalog.register(DISCLOSURE_RULES, [RealisticPersonRule, _BlockingRule])

    with pytest.raises(PolicyRuleError):
        evaluate(catalog=catalog)


@pytest.mark.parametrize("attribute", ["rule_id", "version"])
def test_a_rule_changed_after_registration_raises(attribute) -> None:
    class Mutable(_Rule):
        pass

    catalog = DisclosureRuleCatalog()
    catalog.register(DISCLOSURE_RULES, [Mutable])
    factory = catalog.factory_for(DISCLOSURE_RULES)
    setattr(Mutable, attribute, "other.rule" if attribute == "rule_id" else 2)

    with pytest.raises(PolicyRuleError, match="changed"):
        factory(DisclosureObservation(facts(), ()))
    with pytest.raises(PolicyRuleError, match="changed"):
        catalog.factory_for(DISCLOSURE_RULES)
    with pytest.raises(PolicyRuleError, match="changed"):
        evaluate(catalog=catalog)


def test_the_registration_is_validated() -> None:
    catalog = DisclosureRuleCatalog()
    with pytest.raises(TypeError):
        catalog.register(("disclosure", 1), [_Rule])  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        catalog.register(DISCLOSURE_RULES, [_Rule(None)])
    with pytest.raises(ValueError, match="at least one"):
        catalog.register(DISCLOSURE_RULES, [])
    with pytest.raises(ValueError, match="already"):
        catalog.register(DISCLOSURE_RULES, [_Rule, _Rule])
    catalog.register(DISCLOSURE_RULES, [_Rule])
    with pytest.raises(ValueError, match="already"):
        catalog.register(DISCLOSURE_RULES, [_Rule])

    class NoId:
        version = 1

    class BadVersion:
        rule_id = "bad.version"
        version = 0

    class Padded:
        rule_id = " padded"
        version = 1

    for bad in (NoId, BadVersion, Padded):
        with pytest.raises(ValueError):
            DisclosureRuleCatalog().register(DISCLOSURE_RULES, [bad])


# separation from G-080 and the gates


def test_the_disclosure_rules_are_not_in_the_policy_defaults() -> None:
    policy_ids = {rule.rule_id for rule in mock_registry().rules}
    catalog_ids = {
        rule.rule_id for rule in default_catalog().rules_for(RuleSet("policy", 1))
    }

    assert not {rid for rid in policy_ids | catalog_ids if rid.startswith("disclosure")}
    with pytest.raises(PolicyRuleSetNotFoundError):
        default_catalog().rules_for(DISCLOSURE_RULES)


def test_the_deferred_gates_are_unchanged() -> None:
    from ai_youtube_agent.core.gates import GateName
    from ai_youtube_agent.core.publish_gate import DEFERRED_GATES

    assert DEFERRED_GATES == (GateName.KILL_SWITCH,)


def _attribute_calls(path: Path, name: str) -> list[int]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == name
    ]


@pytest.mark.parametrize(
    "module", [rule_module, decider_module], ids=lambda m: m.__name__
)
def test_the_rules_run_only_through_the_checker(module) -> None:
    assert _attribute_calls(Path(module.__file__), "evaluate") == []


def test_the_checker_is_the_evaluation_path() -> None:
    source = Path(rule_module.__file__).read_text(encoding="utf-8")

    assert "PolicyChecker(" in source
    assert ".check(" in source


@pytest.mark.parametrize(
    "name", ["publish_gate.py", "rights_gate.py", "policy_gate.py"]
)
def test_no_gate_imports_a_disclosure_module(name) -> None:
    tree = ast.parse((SRC / "core" / name).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            names = [node.module or "", *(alias.name for alias in node.names)]
        else:
            continue
        assert not any("disclosure" in name for name in names)
