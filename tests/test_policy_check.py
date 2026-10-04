"""Tests for the policy check (G-080)."""

import dataclasses
import logging
import unicodedata
from datetime import UTC, datetime

import pytest

from ai_youtube_agent.content.policy import PolicyFinding
from ai_youtube_agent.content.policy_check import (
    POLICY_RULES,
    PolicyChecker,
    PolicyCheckResult,
    PolicyInput,
    PolicyRuleSetCatalog,
    PolicyRuleSetNotFoundError,
    PolicyStatus,
    RuleOutcome,
    default_catalog,
    normalize_text,
    worst,
)
from ai_youtube_agent.content.policy_rule import (
    MockAlwaysPassRule,
    MockBannedPhraseRule,
    MockTitleLengthRule,
    PolicyRuleError,
    RuleResult,
    RuleSet,
    mock_registry,
)

NBSP = " "
FIXED = datetime(2026, 10, 4, 12, 0, tzinfo=UTC)


def make_input(**overrides) -> PolicyInput:
    fields = {"content_item_id": "item-1", "channel_id": "channel-1"}
    return PolicyInput(**(fields | overrides))


def check(**overrides) -> PolicyCheckResult:
    return PolicyChecker().check(make_input(**overrides), rule_set=POLICY_RULES)


class FakeRule:
    def __init__(self, rule_id="fake.rule", version=1, passed=True, blocking=False):
        self.rule_id = rule_id
        self.version = version
        self._passed = passed
        self._blocking = blocking

    def evaluate(self, context):
        return RuleResult(
            self.rule_id,
            self.version,
            self._passed,
            self._blocking,
            "fake.code",
            "Fake message.",
        )


class WrongIdentityRule:
    rule_id = "fake.rule"
    version = 1

    def __init__(self, result_id="fake.rule", result_version=1):
        self._id, self._version = result_id, result_version

    def evaluate(self, context):
        return RuleResult(self._id, self._version, True, False, "c", "m")


def checker_with(*rules) -> PolicyChecker:
    catalog = PolicyRuleSetCatalog()
    catalog.register(POLICY_RULES, list(rules))
    return PolicyChecker(catalog)


# Normalisation


def test_none_becomes_empty_text() -> None:
    assert normalize_text(None, "title") == ""
    assert make_input(title=None, description=None).title == ""


def test_nfd_becomes_nfc() -> None:
    nfd = unicodedata.normalize("NFD", "Việt Nam")
    assert nfd != "Việt Nam"
    assert make_input(title=nfd).title == unicodedata.normalize("NFC", nfd)


def test_text_is_stripped_and_inner_whitespace_collapsed() -> None:
    raw = f"  a \t b\n\nc{NBSP}{NBSP}d  "
    assert normalize_text(raw, "title") == "a b c d"
    assert make_input(description=raw).description == "a b c d"


def test_case_is_kept() -> None:
    assert make_input(title="  MiXed Case ").title == "MiXed Case"


def test_empty_items_are_dropped_but_order_and_duplicates_are_kept() -> None:
    result = make_input(
        tags=["b", " ", "a", "", "b", f" x{NBSP} y "],
        banned_phrases=("z", "", "z"),
    )
    assert result.tags == ("b", "a", "b", "x y")
    assert result.banned_phrases == ("z", "z")
    assert isinstance(result.tags, tuple)


def test_ids_are_stripped_but_not_collapsed() -> None:
    result = make_input(content_item_id="  it  em \n", channel_id="\tch\t")
    assert result.content_item_id == "it  em"
    assert result.channel_id == "ch"


@pytest.mark.parametrize("name", ["content_item_id", "channel_id"])
@pytest.mark.parametrize("value", ["", "   ", "\n"])
def test_an_empty_id_is_refused_naming_the_field(name: str, value: str) -> None:
    with pytest.raises(ValueError, match=name):
        make_input(**{name: value})


@pytest.mark.parametrize("name", ["tags", "banned_phrases"])
def test_a_bare_string_for_items_is_refused(name: str) -> None:
    with pytest.raises(TypeError, match=name):
        make_input(**{name: "abc"})
    with pytest.raises(TypeError, match=name):
        make_input(**{name: (1,)})


@pytest.mark.parametrize("name", ["title", "description"])
def test_a_non_text_title_or_description_is_refused(name: str) -> None:
    with pytest.raises(TypeError, match=name):
        make_input(**{name: 5})


def test_a_policy_input_is_frozen() -> None:
    with pytest.raises(dataclasses.FrozenInstanceError):
        make_input().title = "x"


def test_to_context_carries_the_normalised_values() -> None:
    context = make_input(
        title=" T  t ", description=" D ", tags=[" a "], banned_phrases=["p "]
    ).to_context()
    assert (context.content_item_id, context.channel_id) == ("item-1", "channel-1")
    assert context.title == "T t"
    assert context.description == "D"
    assert context.tags == ("a",)
    assert context.banned_phrases == ("p",)


def test_missing_metadata_still_gets_a_result() -> None:
    result = check()
    assert result.outcomes[0].status is PolicyStatus.BLOCK  # an empty title
    assert [o.status for o in result.outcomes[1:]] == [PolicyStatus.PASS] * 2


def test_the_title_boundary_is_measured_after_normalisation() -> None:
    assert check(title="x" * 100).status is PolicyStatus.PASS
    assert check(title="x" * 101).status is PolicyStatus.BLOCK
    assert check(title=f"  {'x' * 100}  ").status is PolicyStatus.PASS
    assert check(title="x" + " " * 200 + "x").status is PolicyStatus.PASS  # collapsed


# Matching unchanged


def test_matching_is_case_insensitive() -> None:
    result = check(title="a bad word", banned_phrases=("BAD",))
    assert result.status is PolicyStatus.BLOCK


def test_an_nfd_phrase_matches_an_nfc_title() -> None:
    phrase = unicodedata.normalize("NFD", "việt")
    result = check(title="Xin Việt Nam", banned_phrases=(phrase,))
    assert result.status is PolicyStatus.BLOCK


# Status


def test_a_clean_item_passes_with_every_outcome_listed() -> None:
    result = check(title="Fine")
    assert result.status is PolicyStatus.PASS
    assert len(result.outcomes) == 3
    assert all(o.status is PolicyStatus.PASS for o in result.outcomes)


def test_a_non_blocking_failure_warns() -> None:
    checker = checker_with(MockBannedPhraseRule(blocking=False), MockAlwaysPassRule())
    result = checker.check(
        make_input(title="bad", banned_phrases=("bad",)), rule_set=POLICY_RULES
    )
    assert result.status is PolicyStatus.WARN
    assert [o.status for o in result.outcomes] == [PolicyStatus.WARN, PolicyStatus.PASS]
    assert result.to_findings()[0].blocking is False


def test_a_blocking_failure_blocks() -> None:
    result = check(title="bad", banned_phrases=("bad",))
    assert result.status is PolicyStatus.BLOCK
    assert result.outcomes[1].status is PolicyStatus.BLOCK
    assert result.to_findings()[0].blocking is True


@pytest.mark.parametrize(
    ("statuses", "expected"),
    [
        ((), PolicyStatus.PASS),
        ((PolicyStatus.PASS,), PolicyStatus.PASS),
        ((PolicyStatus.PASS, PolicyStatus.WARN), PolicyStatus.WARN),
        (
            (PolicyStatus.WARN, PolicyStatus.BLOCK, PolicyStatus.PASS),
            PolicyStatus.BLOCK,
        ),
        ((PolicyStatus.BLOCK, PolicyStatus.WARN), PolicyStatus.BLOCK),
    ],
)
def test_worst_orders_block_over_warn_over_pass(statuses, expected) -> None:
    assert worst(statuses) is expected


def test_warn_and_block_together_give_block() -> None:
    checker = checker_with(
        FakeRule("w.rule", passed=False),
        FakeRule("b.rule", passed=False, blocking=True),
    )
    result = checker.check(make_input(), rule_set=POLICY_RULES)
    assert [o.status for o in result.outcomes] == [
        PolicyStatus.WARN,
        PolicyStatus.BLOCK,
    ]
    assert result.status is PolicyStatus.BLOCK


# Catalog


def test_the_default_catalog_runs_the_mock_rules_in_order() -> None:
    rules = default_catalog().rules_for(POLICY_RULES)
    assert [r.rule_id for r in rules] == [
        "mock.title_length",
        "mock.banned_phrase",
        "mock.always_pass",
    ]
    assert isinstance(rules[0], MockTitleLengthRule)
    assert [o.rule_id for o in check(title="Fine").outcomes] == [
        r.rule_id for r in rules
    ]


@pytest.mark.parametrize("rule_set", [RuleSet("policy", 2), RuleSet("rights", 1)])
def test_an_unknown_rule_set_is_not_found_without_a_fallback(rule_set) -> None:
    with pytest.raises(PolicyRuleSetNotFoundError) as info:
        PolicyChecker().check(make_input(), rule_set=rule_set)
    assert info.value.code == "domain.policy_rule_set_not_found"
    assert info.value.to_public().http_status == 404
    assert str(info.value) == "domain.policy_rule_set_not_found"
    assert "rights" not in repr(info.value) and "version" not in repr(info.value)


def test_rule_set_is_required() -> None:
    with pytest.raises(TypeError):
        PolicyChecker().check(make_input())


def test_the_arguments_are_type_checked() -> None:
    with pytest.raises(TypeError, match="policy_input"):
        PolicyChecker().check("item", rule_set=POLICY_RULES)
    with pytest.raises(TypeError, match="rule_set"):
        PolicyChecker().check(make_input(), rule_set="policy")


def test_a_duplicate_registration_is_refused() -> None:
    catalog = PolicyRuleSetCatalog()
    catalog.register(POLICY_RULES, [FakeRule()])
    with pytest.raises(ValueError):
        catalog.register(POLICY_RULES, [FakeRule("other.rule")])


def test_an_empty_rule_set_is_refused() -> None:
    with pytest.raises(ValueError):
        PolicyRuleSetCatalog().register(POLICY_RULES, [])


def test_duplicate_rule_ids_in_a_set_are_refused() -> None:
    with pytest.raises(ValueError):
        PolicyRuleSetCatalog().register(POLICY_RULES, [FakeRule(), FakeRule()])


def test_a_rule_whose_version_changes_after_registration_is_refused() -> None:
    rule = FakeRule()
    catalog = PolicyRuleSetCatalog()
    catalog.register(POLICY_RULES, [rule])
    rule.version = 2
    with pytest.raises(PolicyRuleError):
        catalog.rules_for(POLICY_RULES)
    with pytest.raises(PolicyRuleError):
        PolicyChecker(catalog).check(make_input(), rule_set=POLICY_RULES)


def test_catalogs_are_independent() -> None:
    first, second = default_catalog(), default_catalog()
    assert first is not second
    first.register(RuleSet("extra", 1), [FakeRule()])
    with pytest.raises(PolicyRuleSetNotFoundError):
        second.rules_for(RuleSet("extra", 1))


# Results


def test_to_findings_holds_failures_only() -> None:
    result = check(title="x" * 101, banned_phrases=("x",))
    findings = result.to_findings()
    assert [f.rule_id for f in findings] == ["mock.title_length", "mock.banned_phrase"]
    assert all(f.blocking for f in findings)
    assert check(title="Fine").to_findings() == ()


def test_to_policy_check_uses_the_injected_clock() -> None:
    result = check(title="x" * 101)
    policy_check = result.to_policy_check("item-1", clock=lambda: FIXED)
    assert policy_check.checked_at == FIXED
    assert policy_check.content_item_id == "item-1"
    assert policy_check.findings == result.to_findings()
    assert len(policy_check.blocking_findings) == 1


def test_repeated_calls_give_equal_results() -> None:
    assert check(title="Fine", tags=["a"]) == check(title="Fine", tags=["a"])


def test_an_outcome_to_finding_refuses_a_pass() -> None:
    outcome = RuleOutcome("r", 1, PolicyStatus.PASS, "c", "m")
    with pytest.raises(ValueError):
        outcome.to_finding()
    warn = RuleOutcome("r", 2, PolicyStatus.WARN, "c", "m")
    assert warn.to_finding() == PolicyFinding("r", 2, False, "m")


# Contract


def test_a_rule_returning_another_version_is_refused() -> None:
    checker = checker_with(WrongIdentityRule(result_version=2))
    with pytest.raises(PolicyRuleError):
        checker.check(make_input(), rule_set=POLICY_RULES)


def test_a_rule_returning_another_id_is_refused() -> None:
    checker = checker_with(WrongIdentityRule(result_id="other.rule"))
    with pytest.raises(PolicyRuleError):
        checker.check(make_input(), rule_set=POLICY_RULES)


@pytest.mark.parametrize("rule_id", ["", "   ", None])
def test_an_outcome_needs_a_rule_id(rule_id) -> None:
    with pytest.raises(ValueError):
        RuleOutcome(rule_id, 1, PolicyStatus.PASS, "c", "m")


@pytest.mark.parametrize("version", [0, -1, True, "1", None])
def test_an_outcome_needs_a_version(version) -> None:
    with pytest.raises(ValueError):
        RuleOutcome("r", version, PolicyStatus.PASS, "c", "m")


def test_an_outcome_status_must_be_a_policy_status() -> None:
    with pytest.raises(TypeError):
        RuleOutcome("r", 1, "pass", "c", "m")


def test_an_outcome_field_must_be_clean_text() -> None:
    for value in ("", " title"):
        with pytest.raises(ValueError):
            RuleOutcome("r", 1, PolicyStatus.PASS, "c", "m", value)
    with pytest.raises(TypeError):
        RuleOutcome("r", 1, PolicyStatus.PASS, "c", "m", 3)


def test_every_default_outcome_names_its_rule() -> None:
    for outcome in check(title="Fine").outcomes:
        assert outcome.rule_id.strip()
        assert outcome.version >= 1


@pytest.mark.parametrize("status", [PolicyStatus.PASS, PolicyStatus.WARN])
def test_a_downgraded_status_cannot_be_built(status: PolicyStatus) -> None:
    block = RuleOutcome("r", 1, PolicyStatus.BLOCK, "c", "m")
    with pytest.raises(ValueError):
        PolicyCheckResult(POLICY_RULES, status, (block,))


def test_a_result_needs_a_tuple_of_outcomes_and_the_exact_worst_status() -> None:
    block = RuleOutcome("r", 1, PolicyStatus.BLOCK, "c", "m")
    with pytest.raises(ValueError):
        PolicyCheckResult(POLICY_RULES, PolicyStatus.BLOCK, [block])
    with pytest.raises(ValueError):
        PolicyCheckResult(POLICY_RULES, PolicyStatus.BLOCK, ("x",))
    with pytest.raises(ValueError):
        PolicyCheckResult(POLICY_RULES, PolicyStatus.BLOCK, ())
    empty = PolicyCheckResult(POLICY_RULES, PolicyStatus.PASS, ())
    assert empty.status is PolicyStatus.PASS


def test_a_blocking_failure_always_blocks_and_gives_a_blocking_finding() -> None:
    result = check(title="x" * 101)
    assert result.status is PolicyStatus.BLOCK
    assert any(f.blocking for f in result.to_findings())


def test_a_result_is_frozen() -> None:
    with pytest.raises(dataclasses.FrozenInstanceError):
        check().status = PolicyStatus.PASS


def test_the_run_is_deterministic_and_follows_the_registry_order() -> None:
    results = [check(title="x" * 101, banned_phrases=("x",)) for _ in range(20)]
    assert all(r == results[0] for r in results)
    assert [o.rule_id for o in results[0].outcomes] == [
        r.rule_id for r in mock_registry().rules
    ]


def test_reordering_tags_or_phrases_keeps_the_outcome_order() -> None:
    first = check(title="bad", tags=["a", "b"], banned_phrases=["bad", "worse"])
    second = check(title="bad", tags=["b", "a"], banned_phrases=["worse", "bad"])
    assert [o.rule_id for o in first.outcomes] == [o.rule_id for o in second.outcomes]
    assert first == second


# field


def test_the_outcome_field_follows_the_rule_result() -> None:
    assert check(title="x" * 101).outcomes[0].field == "title"
    assert check(title="bad", banned_phrases=("bad",)).outcomes[1].field == "title"
    description_only = check(
        title="ok", description="a bad one", banned_phrases=("bad",)
    )
    assert description_only.outcomes[1].field == "description"
    both = check(title="bad", description="bad", banned_phrases=("bad",))
    assert both.outcomes[1].field == "title"
    passing = check(title="ok", banned_phrases=("absent",))
    assert passing.outcomes[1].field is None
    tags_only = check(title="ok", tags=["bad"], banned_phrases=("bad",))
    assert tags_only.outcomes[1].field is None


# Security

SECRETS = {
    "token": "tok_s3cr3t_abc123",
    "password": "hunter2-p4ssw0rd",
    "api_key": "sk-live-9f8e7d6c5b4a",
    "url": "https://user:pw@host/x",
    "query": "https://host/x?api_key=ZZtopSecret99",
}
SECRET_IDS = list(SECRETS)
FIELDS = ["title", "description", "tags", "banned_phrases"]


def secret_value(field: str, secret: str):
    return (secret,) if field in ("tags", "banned_phrases") else secret


def assert_clean(secret: str, *texts: str) -> None:
    needles = [secret]
    needles += [p for p in ("pw@host", "ZZtopSecret99") if p in secret]
    for text in texts:
        for needle in needles:
            assert needle not in text


def all_texts(result: PolicyCheckResult, caplog) -> list[str]:
    texts = [repr(result), caplog.text]
    texts += [repr(o) for o in result.outcomes]
    texts += [str(f.as_dict()) for f in result.to_findings()]
    policy_check = result.to_policy_check("item-1", clock=lambda: FIXED)
    texts.append(str(policy_check.as_dict()))
    return texts


@pytest.mark.parametrize("secret_id", SECRET_IDS)
@pytest.mark.parametrize("field", FIELDS)
def test_no_secret_leaks_from_a_passing_check(
    field: str, secret_id: str, caplog: pytest.LogCaptureFixture
) -> None:
    secret = SECRETS[secret_id]
    caplog.set_level(logging.DEBUG)
    result = check(**{field: secret_value(field, secret)})
    assert_clean(secret, *all_texts(result, caplog))


@pytest.mark.parametrize("secret_id", SECRET_IDS)
def test_no_secret_leaks_from_failing_checks(
    secret_id: str, caplog: pytest.LogCaptureFixture
) -> None:
    secret = SECRETS[secret_id]
    caplog.set_level(logging.DEBUG)
    cases = [
        check(title=f"see {secret}", banned_phrases=(secret,)),
        check(description=f"see {secret}", banned_phrases=(secret,)),
        check(title=(secret + " ") * 10),
    ]
    assert all(r.status is PolicyStatus.BLOCK for r in cases)
    for result in cases:
        assert_clean(secret, *all_texts(result, caplog))


@pytest.mark.parametrize("secret_id", SECRET_IDS)
@pytest.mark.parametrize("field", FIELDS)
def test_a_bad_type_error_holds_no_secret(field: str, secret_id: str) -> None:
    secret = SECRETS[secret_id]
    bad = {
        "title": [secret],
        "description": {secret: 1},
        "tags": secret,
        "banned_phrases": (secret, 5),
    }[field]
    with pytest.raises(TypeError) as info:
        make_input(**{field: bad})
    assert_clean(secret, str(info.value), repr(info.value))


@pytest.mark.parametrize("secret_id", SECRET_IDS)
def test_a_bad_id_error_holds_no_secret(secret_id: str) -> None:
    secret = SECRETS[secret_id]
    with pytest.raises(TypeError) as info:
        make_input(content_item_id=[secret])
    assert_clean(secret, str(info.value), repr(info.value))


@pytest.mark.parametrize("secret_id", SECRET_IDS)
def test_a_rule_error_holds_no_secret(secret_id: str) -> None:
    secret = SECRETS[secret_id]
    checker = checker_with(WrongIdentityRule(result_id="other.rule"))
    with pytest.raises(PolicyRuleError) as info:
        checker.check(make_input(title=secret), rule_set=POLICY_RULES)
    assert_clean(secret, str(info.value), repr(info.value))


@pytest.mark.parametrize("secret_id", SECRET_IDS)
def test_a_missing_rule_set_error_holds_no_secret(secret_id: str) -> None:
    secret = SECRETS[secret_id]
    with pytest.raises(PolicyRuleSetNotFoundError) as info:
        PolicyChecker().check(make_input(title=secret), rule_set=RuleSet("rights", 1))
    assert_clean(secret, str(info.value), repr(info.value))


# Fix round

NFD_ID = unicodedata.normalize("NFD", "mục-1")


def test_to_policy_check_normalises_the_id() -> None:
    result = check()
    padded = result.to_policy_check("  item-1 \n", clock=lambda: FIXED)
    assert padded.content_item_id == "item-1"
    nfd = result.to_policy_check(f" {NFD_ID} ", clock=lambda: FIXED)
    assert nfd.content_item_id == unicodedata.normalize("NFC", NFD_ID)
    assert nfd.content_item_id != NFD_ID


@pytest.mark.parametrize("value", ["", "   ", "\n"])
def test_to_policy_check_refuses_an_empty_id_naming_the_field(value: str) -> None:
    with pytest.raises(ValueError, match="content_item_id") as info:
        check().to_policy_check(value, clock=lambda: FIXED)
    assert "None" not in str(info.value)


@pytest.mark.parametrize("value", [None, 5, ["item"], b"item"])
def test_to_policy_check_refuses_a_non_text_id(value) -> None:
    with pytest.raises(TypeError, match="content_item_id"):
        check().to_policy_check(value, clock=lambda: FIXED)


def test_a_not_found_error_has_no_cause_or_context() -> None:
    with pytest.raises(PolicyRuleSetNotFoundError) as info:
        PolicyRuleSetCatalog().rules_for(RuleSet("none", 1))
    assert info.value.__cause__ is None
    assert info.value.__context__ is None


def test_nfd_ids_and_tags_are_stored_as_nfc() -> None:
    nfd = unicodedata.normalize("NFD", "Việt")
    nfc = unicodedata.normalize("NFC", nfd)
    assert nfd != nfc
    result = make_input(content_item_id=nfd, channel_id=nfd, tags=[nfd])
    assert result.content_item_id == nfc
    assert result.channel_id == nfc
    assert result.tags == (nfc,)


class NotARuleResultRule:
    rule_id = "fake.rule"
    version = 1

    def evaluate(self, context):
        return {"passed": True}


def test_a_rule_returning_a_non_rule_result_is_refused() -> None:
    checker = checker_with(NotARuleResultRule())
    with pytest.raises(PolicyRuleError):
        checker.check(make_input(), rule_set=POLICY_RULES)


def test_a_rule_whose_id_changes_after_registration_is_refused() -> None:
    rule = FakeRule()
    catalog = PolicyRuleSetCatalog()
    catalog.register(POLICY_RULES, [rule])
    rule.rule_id = "renamed.rule"
    with pytest.raises(PolicyRuleError):
        catalog.rules_for(POLICY_RULES)


@pytest.mark.parametrize("secret_id", SECRET_IDS)
def test_a_checker_rule_error_carries_no_input_anywhere(secret_id: str) -> None:
    secret = SECRETS[secret_id]
    for rule in (WrongIdentityRule(result_id="other.rule"), NotARuleResultRule()):
        checker = checker_with(rule)
        with pytest.raises(PolicyRuleError) as info:
            checker.check(make_input(title=secret), rule_set=POLICY_RULES)
        error = info.value
        assert_clean(secret, str(error), repr(error))
        assert error.__cause__ is None
        assert error.__context__ is None
