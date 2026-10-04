"""Policy check (Prompt Pack v8, prompt G-080), context C6 Rights & Policy.

A pure check of one content item against a versioned set of policy rules. The
design was approved by the user on 2026-10-04:

- ``PolicyInput``: what the caller knows about the item. It normalises text in one
  place (NFC, outer whitespace stripped, inner whitespace collapsed, case kept),
  so a rule never sees raw text. Missing metadata is empty text, not an error.
- ``PolicyStatus``: ``PASS``, ``WARN`` or ``BLOCK``, with ``worst`` (BLOCK over
  WARN over PASS; nothing gives PASS).
- ``RuleOutcome`` and ``PolicyCheckResult``: one outcome per rule, in rule order,
  passes included. The result status is the worst outcome status, and a result
  that downgrades it cannot be built. A code or a message never holds the text
  it judged.
- ``PolicyRuleSetCatalog``: the rules of each exact ``RuleSet``, pinned by rule id
  and version. An unknown set raises ``PolicyRuleSetNotFoundError``, with no
  fallback to another version.
- ``PolicyChecker.check``: runs each rule only through ``evaluate_rule``.

Nothing here touches the database, the audit log, logging or an AI provider. The
clock and a uuid4 are read only by ``PolicyCheckResult.to_policy_check`` (through
``PolicyCheck.create``), which stores nothing.
"""

import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum
from http import HTTPStatus

from ai_youtube_agent.content.policy import Clock, PolicyCheck, PolicyFinding
from ai_youtube_agent.content.policy_rule import (
    PolicyContext,
    PolicyRule,
    PolicyRuleError,
    PolicyRuleRegistry,
    RuleSet,
    evaluate_rule,
    mock_registry,
)
from ai_youtube_agent.core.errors import DomainError

POLICY_RULES = RuleSet("policy", 1)


class PolicyStatus(StrEnum):
    PASS = "pass"
    WARN = "warn"
    BLOCK = "block"


_SEVERITY = {PolicyStatus.PASS: 0, PolicyStatus.WARN: 1, PolicyStatus.BLOCK: 2}


def worst(statuses: Iterable[PolicyStatus]) -> PolicyStatus:
    return max(statuses, key=_SEVERITY.__getitem__, default=PolicyStatus.PASS)


class PolicyRuleSetNotFoundError(DomainError):
    default_code = "domain.policy_rule_set_not_found"
    default_user_message = "This policy rule set does not exist."
    default_http_status = HTTPStatus.NOT_FOUND


def normalize_text(value: object, field: str) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise TypeError(f"{field} must be text")
    return " ".join(unicodedata.normalize("NFC", value).split())


def _normalize_items(value: object, field: str) -> tuple[str, ...]:
    if not isinstance(value, tuple | list):
        raise TypeError(f"{field} must be a tuple or list of text values")
    items = []
    for item in value:
        if not isinstance(item, str):
            raise TypeError(f"{field} must hold text values only")
        items.append(normalize_text(item, field))
    return tuple(item for item in items if item)


def _normalize_id(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field} must be text")
    result = unicodedata.normalize("NFC", value).strip()
    if not result:
        raise ValueError(f"{field} must not be empty")
    return result


@dataclass(frozen=True)
class PolicyInput:
    content_item_id: str
    channel_id: str
    title: str = ""
    description: str = ""
    tags: tuple[str, ...] = ()
    banned_phrases: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        values = {
            "content_item_id": _normalize_id(self.content_item_id, "content_item_id"),
            "channel_id": _normalize_id(self.channel_id, "channel_id"),
            "title": normalize_text(self.title, "title"),
            "description": normalize_text(self.description, "description"),
            "tags": _normalize_items(self.tags, "tags"),
            "banned_phrases": _normalize_items(self.banned_phrases, "banned_phrases"),
        }
        for name, value in values.items():
            object.__setattr__(self, name, value)

    def to_context(self) -> PolicyContext:
        return PolicyContext(
            self.content_item_id,
            self.channel_id,
            self.title,
            self.description,
            self.tags,
            self.banned_phrases,
        )


@dataclass(frozen=True)
class RuleOutcome:
    rule_id: str
    version: int
    status: PolicyStatus
    code: str
    message: str
    field: str | None = None

    def __post_init__(self) -> None:
        for name in ("rule_id", "code", "message"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must not be empty")
        if self.code != self.code.strip():
            raise ValueError("code must not have outer whitespace")
        if (
            isinstance(self.version, bool)
            or not isinstance(self.version, int)
            or self.version < 1
        ):
            raise ValueError("version must be an integer >= 1")
        if not isinstance(self.status, PolicyStatus):
            raise TypeError("status must be a PolicyStatus")
        if self.field is not None:
            if not isinstance(self.field, str):
                raise TypeError("field must be text")
            if not self.field or self.field != self.field.strip():
                raise ValueError("field must be text without outer whitespace")

    def to_finding(self) -> PolicyFinding:
        if self.status is PolicyStatus.PASS:
            raise ValueError("a passed outcome has no finding")
        return PolicyFinding(
            self.rule_id,
            self.version,
            self.status is PolicyStatus.BLOCK,
            self.message,
        )


@dataclass(frozen=True)
class PolicyCheckResult:
    rule_set: RuleSet
    status: PolicyStatus
    outcomes: tuple[RuleOutcome, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.rule_set, RuleSet):
            raise TypeError("rule_set must be a RuleSet")
        if not isinstance(self.status, PolicyStatus):
            raise TypeError("status must be a PolicyStatus")
        if not isinstance(self.outcomes, tuple) or not all(
            isinstance(o, RuleOutcome) for o in self.outcomes
        ):
            raise ValueError("outcomes must be a tuple of RuleOutcome values")
        if self.status != worst(o.status for o in self.outcomes):
            raise ValueError("status must be the worst outcome status")

    def to_findings(self) -> tuple[PolicyFinding, ...]:
        return tuple(
            o.to_finding() for o in self.outcomes if o.status is not PolicyStatus.PASS
        )

    def to_policy_check(
        self, content_item_id: str, *, clock: Clock | None = None
    ) -> PolicyCheck:
        """Build a PolicyCheck from this result.

        The caller owns which content item the result belongs to; the id is only
        normalised here (NFC, stripped), the same way ``PolicyInput`` does it.
        """
        item_id = _normalize_id(content_item_id, "content_item_id")
        return PolicyCheck.create(item_id, self.to_findings(), clock=clock)


class PolicyRuleSetCatalog:
    def __init__(self) -> None:
        self._sets: dict[RuleSet, tuple[tuple[PolicyRule, str, int], ...]] = {}

    def register(self, rule_set: RuleSet, rules: Iterable[PolicyRule]) -> None:
        if not isinstance(rule_set, RuleSet):
            raise TypeError("rule_set must be a RuleSet")
        if rule_set in self._sets:
            raise ValueError("this rule set is already registered")
        checked = PolicyRuleRegistry()
        for rule in rules:
            checked.register(rule)  # refuses bad rules and duplicate rule ids
        if not len(checked):
            raise ValueError("a rule set needs at least one rule")
        self._sets[rule_set] = tuple((r, r.rule_id, r.version) for r in checked.rules)

    def rules_for(self, rule_set: RuleSet) -> tuple[PolicyRule, ...]:
        pinned = self._sets.get(rule_set)
        if pinned is None:
            raise PolicyRuleSetNotFoundError()
        for rule, rule_id, version in pinned:
            if rule.rule_id != rule_id or rule.version != version:
                raise PolicyRuleError("a rule changed after it was registered")
        return tuple(rule for rule, _, _ in pinned)


def default_catalog() -> PolicyRuleSetCatalog:
    catalog = PolicyRuleSetCatalog()
    catalog.register(POLICY_RULES, mock_registry().rules)
    return catalog


class PolicyChecker:
    def __init__(self, catalog: PolicyRuleSetCatalog | None = None) -> None:
        self._catalog = catalog if catalog is not None else default_catalog()

    def check(
        self, policy_input: PolicyInput, *, rule_set: RuleSet
    ) -> PolicyCheckResult:
        if not isinstance(policy_input, PolicyInput):
            raise TypeError("policy_input must be a PolicyInput")
        if not isinstance(rule_set, RuleSet):
            raise TypeError("rule_set must be a RuleSet")
        rules = self._catalog.rules_for(rule_set)
        context = policy_input.to_context()
        outcomes = []
        for rule in rules:
            result = evaluate_rule(rule, context)
            if result.passed:
                status = PolicyStatus.PASS
            elif result.blocking:
                status = PolicyStatus.BLOCK
            else:
                status = PolicyStatus.WARN
            outcomes.append(
                RuleOutcome(
                    result.rule_id,
                    result.version,
                    status,
                    result.code,
                    result.message,
                    result.field,
                )
            )
        return PolicyCheckResult(
            rule_set, worst(o.status for o in outcomes), tuple(outcomes)
        )
