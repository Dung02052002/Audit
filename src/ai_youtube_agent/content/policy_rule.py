"""Policy rule interface (Prompt Pack v8, prompt G-079), context C6 Rights & Policy.

The contract every policy rule meets, before the policy check (#080) runs real
rules. The design was approved by the user on 2026-10-04:

- ``PolicyContext``: what a rule may look at (ids, title, description, tags and
  the banned phrases of the brand). The caller passes the phrases, so a rule never
  reads the strategy.
- ``PolicyRule``: a protocol with ``rule_id``, ``version`` and
  ``evaluate(context) -> RuleResult``. ``evaluate_rule`` runs a rule and refuses
  a result that is not a ``RuleResult`` or that names another rule or version.
- ``RuleResult``: the verdict of one rule, with a stable ``code`` and a safe
  ``message``. ``to_finding`` turns a failed result into a ``PolicyFinding``.
- ``RuleSet``: the id and version of a family of rules. ``stored_version`` is the
  one place that builds the string stored with a result, for example
  ``rights-rules-v1``.
- ``PolicyRuleRegistry``: rules by id, in registration order, with no global state.
- Three mock rules and ``mock_registry`` for tests. No real policy lives here.

A code or a message never holds the text it judged.
"""

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from ai_youtube_agent.content.policy import PolicyFinding

PASSED_MESSAGE = "Rule passed."
MAX_TITLE_LENGTH = 100
MAX_STORED_VERSION_LENGTH = 50
"""The CHECK of ``rights_assessments.rules_version`` is 1 to 50 characters."""


def _is_version(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 1


def _require_text(owner: object, *names: str) -> None:
    for name in names:
        value = getattr(owner, name)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{name} must not be empty")


def _require_texts(owner: object, *names: str) -> None:
    for name in names:
        value = getattr(owner, name)
        if not isinstance(value, tuple) or not all(isinstance(v, str) for v in value):
            raise TypeError(f"{name} must be a tuple of text values")


@dataclass(frozen=True)
class RuleResult:
    rule_id: str
    version: int
    passed: bool
    blocking: bool
    code: str
    message: str

    def __post_init__(self) -> None:
        _require_text(self, "rule_id", "code", "message")
        if self.code != self.code.strip():
            raise ValueError("code must not have outer whitespace")
        if not _is_version(self.version):
            raise ValueError("version must be an integer >= 1")
        for name in ("passed", "blocking"):
            if not isinstance(getattr(self, name), bool):
                raise TypeError(f"{name} must be a bool")
        if self.passed and self.blocking:
            raise ValueError("a passed result is never blocking")

    def to_finding(self) -> PolicyFinding:
        if self.passed:
            raise ValueError("a passed result has no finding")
        return PolicyFinding(self.rule_id, self.version, self.blocking, self.message)


@dataclass(frozen=True)
class PolicyContext:
    content_item_id: str
    channel_id: str
    title: str
    description: str
    tags: tuple[str, ...]
    banned_phrases: tuple[str, ...]

    def __post_init__(self) -> None:
        _require_text(self, "content_item_id", "channel_id")
        for name in ("title", "description"):
            if not isinstance(getattr(self, name), str):
                raise TypeError(f"{name} must be text")
        _require_texts(self, "tags", "banned_phrases")


@runtime_checkable
class PolicyRule(Protocol):
    rule_id: str
    version: int

    def evaluate(self, context: PolicyContext) -> RuleResult: ...


class PolicyRuleError(ValueError):
    """A rule broke the ``PolicyRule`` contract."""


def evaluate_rule(rule: PolicyRule, context: PolicyContext) -> RuleResult:
    result = rule.evaluate(context)
    if not isinstance(result, RuleResult):
        raise PolicyRuleError("a rule must return a RuleResult")
    if result.rule_id != rule.rule_id:
        raise PolicyRuleError("the result names another rule")
    if result.version != rule.version:
        raise PolicyRuleError("the result names another rule version")
    return result


@dataclass(frozen=True)
class RuleSet:
    id: str
    version: int

    def __post_init__(self) -> None:
        if (
            not isinstance(self.id, str)
            or not self.id
            or self.id != "".join(self.id.split())
        ):
            raise ValueError("id must be text without whitespace")
        if not _is_version(self.version):
            raise ValueError("version must be an integer >= 1")
        if len(self.stored_version) > MAX_STORED_VERSION_LENGTH:
            raise ValueError(
                f"stored_version must be at most {MAX_STORED_VERSION_LENGTH} characters"
            )

    @property
    def stored_version(self) -> str:
        return f"{self.id}-rules-v{self.version}"


class PolicyRuleRegistry:
    def __init__(self) -> None:
        self._rules: dict[str, PolicyRule] = {}

    def register(self, rule: PolicyRule) -> None:
        rule_id = getattr(rule, "rule_id", None)
        if not isinstance(rule_id, str) or not rule_id.strip():
            raise ValueError("rule_id must not be empty")
        if rule_id != rule_id.strip():
            raise ValueError("rule_id must not have outer whitespace")
        if not _is_version(getattr(rule, "version", None)):
            raise ValueError("version must be an integer >= 1")
        if not callable(getattr(rule, "evaluate", None)):
            raise ValueError("a rule needs an evaluate method")
        if rule_id in self._rules:
            raise ValueError(f"rule {rule_id} is already registered")
        self._rules[rule_id] = rule

    @property
    def rules(self) -> tuple[PolicyRule, ...]:
        return tuple(self._rules.values())

    def get(self, rule_id: str) -> PolicyRule:
        try:
            return self._rules[rule_id]
        except KeyError:
            raise KeyError(f"unknown rule {rule_id}") from None

    def __len__(self) -> int:
        return len(self._rules)


def _result(
    rule: PolicyRule, passed: bool, blocking: bool, code: str, message: str
) -> RuleResult:
    return RuleResult(rule.rule_id, rule.version, passed, blocking, code, message)


class MockTitleLengthRule:
    rule_id = "mock.title_length"
    version = 1

    def evaluate(self, context: PolicyContext) -> RuleResult:
        # The length is measured after strip(), so outer whitespace does not count.
        length = len(context.title.strip())
        if 0 < length <= MAX_TITLE_LENGTH:
            return _result(self, True, False, "mock.title_length.ok", PASSED_MESSAGE)
        return _result(
            self,
            False,
            True,
            "mock.title_length.out_of_range",
            "Title length is outside the allowed range.",
        )


class MockBannedPhraseRule:
    rule_id = "mock.banned_phrase"
    version = 1

    def __init__(self, *, blocking: bool = True) -> None:
        if not isinstance(blocking, bool):
            raise TypeError("blocking must be a bool")
        self._blocking = blocking

    def evaluate(self, context: PolicyContext) -> RuleResult:
        # Title and description (not tags), each checked on its own so a phrase
        # cannot span the boundary between them.
        fields = (context.title.casefold(), context.description.casefold())
        if any(
            p.strip() and p.casefold() in field
            for p in context.banned_phrases
            for field in fields
        ):
            return _result(
                self,
                False,
                self._blocking,
                "mock.banned_phrase.found",
                "Content contains a banned phrase.",
            )
        return _result(self, True, False, "mock.banned_phrase.ok", PASSED_MESSAGE)


class MockAlwaysPassRule:
    rule_id = "mock.always_pass"
    version = 1

    def evaluate(self, context: PolicyContext) -> RuleResult:
        return _result(self, True, False, "mock.always_pass.ok", PASSED_MESSAGE)


def mock_registry() -> PolicyRuleRegistry:
    registry = PolicyRuleRegistry()
    for rule in (MockTitleLengthRule(), MockBannedPhraseRule(), MockAlwaysPassRule()):
        registry.register(rule)
    return registry
