"""AI disclosure rules (Prompt Pack v8, prompt G-081), context C6 Rights & Policy.

The versioned rule set ``disclosure-rules-v1`` that decides whether a content item
needs an AI disclosure. The design was approved by the user on 2026-10-04:

- ``DISCLOSURE_RULES = RuleSet("disclosure", 1)``. The rules are deterministic,
  with no AI model, and run in a fixed order:

  1. ``disclosure.realistic_person``: the caller declares a realistic person;
  2. ``disclosure.realistic_event``: a realistic event;
  3. ``disclosure.synthetic_voice_of_real_person``: a synthetic voice of a real
     person;
  4. ``disclosure.generated_realistic_visual``: a realistic visual, and at least
     one attached asset of category ``generated`` and kind image or video clip.
     A generated asset alone never triggers it.

- Every result is non-blocking, with no field and a static message. A triggered
  rule gives ``<rule_id>.required`` (a failed, non-blocking result: WARN for the
  policy checker) and any other rule gives ``<rule_id>.ok``. A message or a code
  never holds an id, a title or a text.
- ``DisclosureFacts`` holds the four facts the caller declares; each must be a
  bool. ``DisclosureObservation`` adds the ids of the generated visual assets.
- ``evaluate_disclosure`` runs the rules only through the G-080 ``PolicyChecker``:
  a PASS status is ``NOT_REQUIRED``, a WARN status is ``REQUIRED`` and a BLOCK
  status is refused (``PolicyRuleError``), because a disclosure rule never blocks.
- ``DisclosureRuleCatalog`` maps an exact ``RuleSet`` to its rule classes, pinned by
  rule id and version. An unknown set is ``DisclosureRuleSetNotFoundError`` (404),
  with no fallback to another version. The rules are not in the G-080 default
  catalog, and no gate imports them.
"""

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from http import HTTPStatus

from ai_youtube_agent.content.asset import Asset, AssetCategory, AssetKind
from ai_youtube_agent.content.disclosure import (
    FACT_NAMES,
    DisclosureDecision,
    DisclosureEvaluation,
    DisclosureSources,
    RationaleEntry,
)
from ai_youtube_agent.content.policy_check import (
    PolicyChecker,
    PolicyInput,
    PolicyRuleSetCatalog,
    PolicyStatus,
)
from ai_youtube_agent.content.policy_rule import (
    PASSED_MESSAGE,
    PolicyContext,
    PolicyRule,
    PolicyRuleError,
    RuleResult,
    RuleSet,
)
from ai_youtube_agent.core.errors import DomainError

DISCLOSURE_RULES = RuleSet("disclosure", 1)
GENERATED_VISUAL_KINDS = (AssetKind.IMAGE, AssetKind.VIDEO_CLIP)


class DisclosureRuleSetNotFoundError(DomainError):
    default_code = "domain.disclosure_rule_set_not_found"
    default_user_message = "This disclosure rule set does not exist."
    default_http_status = HTTPStatus.NOT_FOUND


@dataclass(frozen=True)
class DisclosureFacts:
    realistic_person: bool
    realistic_event: bool
    synthetic_voice_of_real_person: bool
    realistic_visual: bool

    def __post_init__(self) -> None:
        for name in FACT_NAMES:
            if type(getattr(self, name)) is not bool:
                raise TypeError(f"{name} must be a bool")


@dataclass(frozen=True)
class DisclosureObservation:
    facts: DisclosureFacts
    generated_visual_asset_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.facts, DisclosureFacts):
            raise TypeError("facts must be DisclosureFacts")
        if not isinstance(self.generated_visual_asset_ids, tuple) or not all(
            isinstance(asset_id, str) for asset_id in self.generated_visual_asset_ids
        ):
            raise TypeError("generated_visual_asset_ids must be a tuple of text")


class _DisclosureRule:
    """A rule over one observation. It ignores the text of the context."""

    rule_id: str
    version: int
    required_message: str

    def __init__(self, observation: DisclosureObservation) -> None:
        if not isinstance(observation, DisclosureObservation):
            raise TypeError("observation must be a DisclosureObservation")
        self._observation = observation

    def _triggered(self) -> bool:
        raise NotImplementedError

    def evaluate(self, context: PolicyContext) -> RuleResult:
        if self._triggered():
            return RuleResult(
                self.rule_id,
                self.version,
                False,
                False,
                f"{self.rule_id}.required",
                self.required_message,
            )
        return RuleResult(
            self.rule_id,
            self.version,
            True,
            False,
            f"{self.rule_id}.ok",
            PASSED_MESSAGE,
        )


class RealisticPersonRule(_DisclosureRule):
    rule_id = "disclosure.realistic_person"
    version = 1
    required_message = "Disclosure is required: the content shows a realistic person."

    def _triggered(self) -> bool:
        return self._observation.facts.realistic_person


class RealisticEventRule(_DisclosureRule):
    rule_id = "disclosure.realistic_event"
    version = 1
    required_message = "Disclosure is required: the content shows a realistic event."

    def _triggered(self) -> bool:
        return self._observation.facts.realistic_event


class SyntheticVoiceOfRealPersonRule(_DisclosureRule):
    rule_id = "disclosure.synthetic_voice_of_real_person"
    version = 1
    required_message = (
        "Disclosure is required: the content uses a synthetic voice of a real person."
    )

    def _triggered(self) -> bool:
        return self._observation.facts.synthetic_voice_of_real_person


class GeneratedRealisticVisualRule(_DisclosureRule):
    rule_id = "disclosure.generated_realistic_visual"
    version = 1
    required_message = (
        "Disclosure is required: the content has a generated realistic visual."
    )

    def _triggered(self) -> bool:
        return self._observation.facts.realistic_visual and bool(
            self._observation.generated_visual_asset_ids
        )


RuleFactory = Callable[[DisclosureObservation], tuple[PolicyRule, ...]]


class DisclosureRuleCatalog:
    def __init__(self) -> None:
        self._sets: dict[RuleSet, tuple[tuple[type, str, int], ...]] = {}

    def register(self, rule_set: RuleSet, rule_classes: Iterable[type]) -> None:
        if not isinstance(rule_set, RuleSet):
            raise TypeError("rule_set must be a RuleSet")
        if rule_set in self._sets:
            raise ValueError("this rule set is already registered")
        pinned = []
        for rule_class in rule_classes:
            if not isinstance(rule_class, type):
                raise TypeError("a rule must be given as a class")
            rule_id = getattr(rule_class, "rule_id", None)
            version = getattr(rule_class, "version", None)
            if not isinstance(rule_id, str) or not rule_id.strip():
                raise ValueError("rule_id must not be empty")
            if rule_id != rule_id.strip():
                raise ValueError("rule_id must not have outer whitespace")
            if isinstance(version, bool) or not isinstance(version, int) or version < 1:
                raise ValueError("version must be an integer >= 1")
            if any(rule_id == other for _, other, _ in pinned):
                raise ValueError(f"rule {rule_id} is already registered")
            pinned.append((rule_class, rule_id, version))
        if not pinned:
            raise ValueError("a rule set needs at least one rule")
        self._sets[rule_set] = tuple(pinned)

    def factory_for(self, rule_set: RuleSet) -> RuleFactory:
        pinned = self._sets.get(rule_set)
        if pinned is None:
            raise DisclosureRuleSetNotFoundError()
        _check_pinned(pinned)

        def factory(observation: DisclosureObservation) -> tuple[PolicyRule, ...]:
            _check_pinned(pinned)
            return tuple(rule_class(observation) for rule_class, _, _ in pinned)

        return factory


def _check_pinned(pinned: tuple[tuple[type, str, int], ...]) -> None:
    for rule_class, rule_id, version in pinned:
        if rule_class.rule_id != rule_id or rule_class.version != version:
            raise PolicyRuleError("a rule changed after it was registered")


def default_disclosure_catalog() -> DisclosureRuleCatalog:
    catalog = DisclosureRuleCatalog()
    catalog.register(
        DISCLOSURE_RULES,
        (
            RealisticPersonRule,
            RealisticEventRule,
            SyntheticVoiceOfRealPersonRule,
            GeneratedRealisticVisualRule,
        ),
    )
    return catalog


def evaluate_disclosure(
    content_item_id: str,
    channel_id: str,
    facts: DisclosureFacts,
    assets: Iterable[Asset],
    *,
    rule_set: RuleSet,
    catalog: DisclosureRuleCatalog | None = None,
) -> DisclosureEvaluation:
    if not isinstance(facts, DisclosureFacts):
        raise TypeError("facts must be DisclosureFacts")
    if not isinstance(rule_set, RuleSet):
        raise TypeError("rule_set must be a RuleSet")
    attached = tuple(assets)
    if not all(isinstance(asset, Asset) for asset in attached):
        raise TypeError("assets must hold Asset values only")
    catalog = catalog if catalog is not None else default_disclosure_catalog()
    factory = catalog.factory_for(rule_set)
    generated_ids = tuple(
        sorted(
            {
                asset.id
                for asset in attached
                if asset.category is AssetCategory.GENERATED
                and asset.kind in GENERATED_VISUAL_KINDS
            }
        )
    )
    rules = factory(DisclosureObservation(facts, generated_ids))
    policy_catalog = PolicyRuleSetCatalog()
    policy_catalog.register(rule_set, rules)
    result = PolicyChecker(policy_catalog).check(
        PolicyInput(content_item_id, channel_id), rule_set=rule_set
    )
    if result.status is PolicyStatus.BLOCK:
        raise PolicyRuleError("a disclosure rule must not block")
    decision = (
        DisclosureDecision.NOT_REQUIRED
        if result.status is PolicyStatus.PASS
        else DisclosureDecision.REQUIRED
    )
    rationale = tuple(
        RationaleEntry(
            outcome.rule_id,
            outcome.version,
            outcome.code,
            outcome.message,
            outcome.status is not PolicyStatus.PASS,
        )
        for outcome in result.outcomes
    )
    sources = DisclosureSources(
        tuple(name for name in FACT_NAMES if getattr(facts, name)), generated_ids
    )
    return DisclosureEvaluation(rule_set, decision, rationale, sources)
