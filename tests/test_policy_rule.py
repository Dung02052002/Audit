"""Tests for the policy rule interface (G-079)."""

import dataclasses

import pytest

from ai_youtube_agent.content.policy import PolicyFinding
from ai_youtube_agent.content.policy_rule import (
    MockAlwaysPassRule,
    MockBannedPhraseRule,
    MockTitleLengthRule,
    PolicyContext,
    PolicyRule,
    PolicyRuleError,
    PolicyRuleRegistry,
    RuleResult,
    RuleSet,
    evaluate_rule,
    mock_registry,
)
from ai_youtube_agent.content.rights_assessment import RIGHTS_RULES, RULES_VERSION

URL = "https://evil.example/path?token=s3cr3t-token"


def make_context(**overrides) -> PolicyContext:
    fields = {
        "content_item_id": "item-1",
        "channel_id": "channel-1",
        "title": "A fine title",
        "description": "A fine description",
        "tags": ("a", "b"),
        "banned_phrases": (),
    }
    return PolicyContext(**(fields | overrides))


def make_result(**overrides) -> RuleResult:
    fields = {
        "rule_id": "r.one",
        "version": 1,
        "passed": False,
        "blocking": True,
        "code": "r.one.failed",
        "message": "It failed.",
    }
    return RuleResult(**(fields | overrides))


class FakeRule:
    def __init__(self, rule_id="fake.rule", version=1, returns=None):
        self.rule_id = rule_id
        self.version = version
        self._returns = returns

    def evaluate(self, context):
        return self._returns


# RuleResult


@pytest.mark.parametrize("name", ["rule_id", "code", "message"])
@pytest.mark.parametrize("value", ["", "   "])
def test_a_result_text_field_must_not_be_empty(name: str, value: str) -> None:
    with pytest.raises(ValueError):
        make_result(**{name: value})


@pytest.mark.parametrize("value", [" code", "code ", "\tcode"])
def test_a_code_has_no_outer_whitespace(value: str) -> None:
    with pytest.raises(ValueError):
        make_result(code=value)


@pytest.mark.parametrize("version", [0, -1, True, "1", 1.0, None])
def test_a_result_version_must_be_an_int_of_at_least_one(version) -> None:
    with pytest.raises(ValueError):
        make_result(version=version)


def test_a_passed_result_is_never_blocking() -> None:
    with pytest.raises(ValueError):
        make_result(passed=True, blocking=True)


@pytest.mark.parametrize("name", ["passed", "blocking"])
@pytest.mark.parametrize("value", [1, 0, "yes", None])
def test_the_flags_of_a_result_must_be_bools(name: str, value) -> None:
    with pytest.raises(TypeError):
        make_result(**{name: value})


def test_a_result_is_frozen() -> None:
    result = make_result()
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.passed = True


def test_to_finding_copies_the_fields() -> None:
    finding = make_result(blocking=True).to_finding()
    assert finding == PolicyFinding("r.one", 1, True, "It failed.")
    assert make_result(blocking=False).to_finding().blocking is False


def test_to_finding_refuses_a_passed_result() -> None:
    with pytest.raises(ValueError):
        make_result(passed=True, blocking=False).to_finding()


def test_the_finding_shape_is_unchanged() -> None:
    assert PolicyFinding("r.one", 2, True, "m").as_dict() == {
        "rule_id": "r.one",
        "rule_version": 2,
        "blocking": True,
        "message": "m",
    }


# PolicyContext


def test_a_context_is_frozen_and_validated() -> None:
    context = make_context()
    with pytest.raises(dataclasses.FrozenInstanceError):
        context.title = "x"
    for name in ("content_item_id", "channel_id"):
        with pytest.raises(ValueError):
            make_context(**{name: " "})
    for name in ("tags", "banned_phrases"):
        with pytest.raises(TypeError):
            make_context(**{name: ["a"]})
        with pytest.raises(TypeError):
            make_context(**{name: (1,)})
    with pytest.raises(TypeError):
        make_context(title=None)


# Protocol and evaluate_rule


def test_the_mocks_are_policy_rules() -> None:
    for rule in (MockTitleLengthRule(), MockBannedPhraseRule(), MockAlwaysPassRule()):
        assert isinstance(rule, PolicyRule)


def test_an_object_without_evaluate_is_not_a_policy_rule() -> None:
    class NoEvaluate:
        rule_id = "x"
        version = 1

    assert not isinstance(NoEvaluate(), PolicyRule)


def test_evaluate_rule_returns_a_matching_result() -> None:
    result = evaluate_rule(MockAlwaysPassRule(), make_context())
    assert result.passed and result.rule_id == "mock.always_pass"


def test_evaluate_rule_rejects_another_rule_id() -> None:
    rule = FakeRule(returns=make_result(rule_id="other.rule"))
    with pytest.raises(PolicyRuleError):
        evaluate_rule(rule, make_context())


def test_evaluate_rule_rejects_another_version() -> None:
    rule = FakeRule(returns=make_result(rule_id="fake.rule", version=2))
    with pytest.raises(PolicyRuleError):
        evaluate_rule(rule, make_context())


@pytest.mark.parametrize("returned", [None, True, "passed", {"passed": True}])
def test_evaluate_rule_rejects_a_non_result(returned) -> None:
    with pytest.raises(PolicyRuleError):
        evaluate_rule(FakeRule(returns=returned), make_context())


def test_a_rule_error_is_a_value_error() -> None:
    assert issubclass(PolicyRuleError, ValueError)


# RuleSet


def test_a_rule_set_derives_the_stored_version() -> None:
    assert RuleSet("rights", 1).stored_version == "rights-rules-v1"
    assert RuleSet("policy", 12).stored_version == "policy-rules-v12"


@pytest.mark.parametrize("id_", ["", " ", "a b", " a", "a\t", None, 1])
def test_a_rule_set_id_is_text_without_whitespace(id_) -> None:
    with pytest.raises(ValueError):
        RuleSet(id_, 1)


def test_a_rule_set_stored_version_fits_the_column() -> None:
    fits = "a" * (50 - len("-rules-v1"))
    assert len(RuleSet(fits, 1).stored_version) == 50
    with pytest.raises(ValueError):
        RuleSet(fits + "a", 1)


@pytest.mark.parametrize("version", [0, -1, True, "1", 1.5, None])
def test_a_rule_set_version_is_an_int_of_at_least_one(version) -> None:
    with pytest.raises(ValueError):
        RuleSet("rights", version)


def test_the_rights_rules_version_is_pinned() -> None:
    assert RULES_VERSION == "rights-rules-v1"
    assert RuleSet("rights", 1) == RIGHTS_RULES
    assert RIGHTS_RULES.stored_version == RULES_VERSION


# Registry


def test_the_registry_keeps_the_registration_order() -> None:
    registry = mock_registry()
    assert [r.rule_id for r in registry.rules] == [
        "mock.title_length",
        "mock.banned_phrase",
        "mock.always_pass",
    ]
    assert len(registry) == 3
    assert isinstance(registry.rules, tuple)


def test_get_returns_the_registered_rule() -> None:
    registry = mock_registry()
    assert registry.get("mock.banned_phrase") is registry.rules[1]


def test_get_of_an_unknown_id_raises_key_error() -> None:
    with pytest.raises(KeyError):
        mock_registry().get("nope")


def test_a_duplicate_id_is_refused_even_with_another_version() -> None:
    registry = PolicyRuleRegistry()
    registry.register(FakeRule("dup.rule", 1))
    with pytest.raises(ValueError):
        registry.register(FakeRule("dup.rule", 1))
    with pytest.raises(ValueError):
        registry.register(FakeRule("dup.rule", 2))
    assert len(registry) == 1


def test_a_rule_with_a_bad_version_is_refused() -> None:
    registry = PolicyRuleRegistry()
    for version in (0, -1, True, "1"):
        with pytest.raises(ValueError):
            registry.register(FakeRule("v.rule", version))
    assert len(registry) == 0


@pytest.mark.parametrize("rule_id", [" x.rule", "x.rule ", "\tx.rule"])
def test_a_rule_id_with_outer_whitespace_is_refused(rule_id) -> None:
    registry = PolicyRuleRegistry()
    with pytest.raises(ValueError):
        registry.register(FakeRule(rule_id, 1))
    assert len(registry) == 0


@pytest.mark.parametrize("rule_id", ["", "  ", None])
def test_a_rule_with_an_empty_id_is_refused(rule_id) -> None:
    with pytest.raises(ValueError):
        PolicyRuleRegistry().register(FakeRule(rule_id, 1))


def test_a_rule_without_evaluate_is_refused() -> None:
    class NoEvaluate:
        rule_id = "x.rule"
        version = 1

    with pytest.raises(ValueError):
        PolicyRuleRegistry().register(NoEvaluate())


def test_registries_are_independent() -> None:
    first, second = mock_registry(), mock_registry()
    extra = PolicyRuleRegistry()
    extra.register(FakeRule("only.here"))
    assert len(extra) == 1 and len(first) == len(second) == 3
    assert first.rules is not second.rules
    assert len(PolicyRuleRegistry()) == 0


# Mocks


def test_a_title_of_exactly_100_characters_passes() -> None:
    result = MockTitleLengthRule().evaluate(make_context(title="x" * 100))
    assert result.passed and not result.blocking


@pytest.mark.parametrize("title", ["x" * 101, "", "   "])
def test_a_title_out_of_range_fails_and_blocks(title: str) -> None:
    result = MockTitleLengthRule().evaluate(make_context(title=title))
    assert not result.passed and result.blocking
    assert result.code == "mock.title_length.out_of_range"
    assert result.message == "Title length is outside the allowed range."
    assert result.to_finding().blocking is True


def test_a_banned_phrase_is_found_in_the_title_case_insensitively() -> None:
    context = make_context(title="Buy FAKE Followers", banned_phrases=("fake follow",))
    result = MockBannedPhraseRule().evaluate(context)
    assert not result.passed and result.blocking
    assert result.code == "mock.banned_phrase.found"
    assert result.message == "Content contains a banned phrase."


def test_a_banned_phrase_is_found_in_the_description() -> None:
    context = make_context(
        description="so Clickbait here", banned_phrases=("CLICKBAIT",)
    )
    assert not MockBannedPhraseRule().evaluate(context).passed


def test_a_banned_phrase_cannot_span_the_title_and_description() -> None:
    rule = MockBannedPhraseRule()
    spanning = make_context(
        title="Buy fake", description="followers", banned_phrases=("fake\nfollowers",)
    )
    assert rule.evaluate(spanning).passed is True
    joined = make_context(
        title="Buy fake", description="followers", banned_phrases=("fake followers",)
    )
    assert rule.evaluate(joined).passed is True


def test_a_banned_phrase_in_the_tags_alone_is_not_matched() -> None:
    context = make_context(tags=("fake",), banned_phrases=("fake",))
    assert MockBannedPhraseRule().evaluate(context).passed is True


def test_no_banned_phrase_passes() -> None:
    assert MockBannedPhraseRule().evaluate(make_context()).passed
    other = make_context(banned_phrases=("absent",))
    assert MockBannedPhraseRule().evaluate(other).passed


def test_the_banned_phrase_blocking_flag_is_a_constructor_flag() -> None:
    context = make_context(title="bad word", banned_phrases=("bad",))
    assert MockBannedPhraseRule().evaluate(context).blocking is True
    warning = MockBannedPhraseRule(blocking=False).evaluate(context)
    assert not warning.passed and warning.blocking is False
    with pytest.raises(TypeError):
        MockBannedPhraseRule(blocking="yes")


def test_always_pass_passes_without_blocking() -> None:
    result = MockAlwaysPassRule().evaluate(make_context())
    assert result.passed and not result.blocking
    assert result.code == "mock.always_pass.ok"


def test_mock_results_are_deterministic_and_follow_the_contract() -> None:
    context = make_context(title="x" * 101, banned_phrases=("fine",))
    for rule in mock_registry().rules:
        assert rule.evaluate(context) == rule.evaluate(context)
        assert evaluate_rule(rule, context).rule_id == rule.rule_id


def test_hostile_input_never_appears_in_a_code_or_a_message() -> None:
    context = make_context(
        title=f"{URL} " + "x" * 100,
        description=f"see {URL}",
        banned_phrases=(URL,),
    )
    for rule in mock_registry().rules:
        result = rule.evaluate(context)
        for text in (result.code, result.message):
            assert "http" not in text
            assert "s3cr3t" not in text
            assert "evil.example" not in text


# RuleResult.field and the mock fields (G-080)


def test_a_result_field_defaults_to_none_and_accepts_text() -> None:
    assert make_result().field is None
    assert make_result(field="title").field == "title"
    assert make_result(field="title").to_finding() == make_result().to_finding()


@pytest.mark.parametrize("value", ["", "   ", " title", "title ", "\ttitle"])
def test_a_result_field_must_be_clean_text(value: str) -> None:
    with pytest.raises(ValueError):
        make_result(field=value)


@pytest.mark.parametrize("value", [1, True, b"title", ["title"]])
def test_a_result_field_must_be_text(value) -> None:
    with pytest.raises(TypeError):
        make_result(field=value)


def test_the_mock_rules_name_the_field() -> None:
    title_rule, banned_rule = MockTitleLengthRule(), MockBannedPhraseRule()
    assert title_rule.evaluate(make_context()).field == "title"
    assert title_rule.evaluate(make_context(title="x" * 101)).field == "title"
    in_title = make_context(title="bad word", banned_phrases=("bad",))
    assert banned_rule.evaluate(in_title).field == "title"
    in_description = make_context(description="bad word", banned_phrases=("bad",))
    assert banned_rule.evaluate(in_description).field == "description"
    in_both = make_context(title="bad", description="bad word", banned_phrases=("bad",))
    assert banned_rule.evaluate(in_both).field == "title"
    assert banned_rule.evaluate(make_context()).field is None
    assert MockAlwaysPassRule().evaluate(make_context()).field is None
