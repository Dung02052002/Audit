"""Publish Gate (Prompt Pack v8, prompt G-078b), context C3 Control Gates.

``PublishGate`` runs the gates that guard the move of an approved content item to
``PUBLISHING`` against the real repositories, as the user approved on
2026-10-04:

- The gates run in the order of the C-042 gate matrix (``tests/test_gate_matrix.py``):
  approval, daily limit, rights, policy, idempotency. ``DEFERRED_GATES`` lists the
  kill switch, which has no store yet.
- The rights gate uses the levels of ``Settings.rights_block_levels`` and checks
  freshness (``RepositoryFreshness``): a record whose newest assessment was made
  on other facts than the current asset and provenance blocks with
  ``rights.assessment_stale`` until it is assessed again. The current facts come
  from ``current_facts``, the same function the risk engine uses: an asset that
  is not registered in the channel of the item has the basis ``(None, None)``, so
  an unregistered asset is never stale (it blocks as high), and registering it
  later makes the record stale until it is assessed.
- Since G-079 the basis also holds the rules version (``RULES_VERSION`` by
  default). A record whose newest assessment used another version blocks with
  ``rights.rules_outdated`` until it is assessed again; a stale basis has priority.
- Since G-084 the policy gate judges the item with the G-080 ``PolicyChecker``
  (``CheckedPolicySource``): the item title and the banned phrases of its
  channel strategy, checked against the rule set ``policy`` at
  ``Settings.policy_rule_set_version`` when the gate is evaluated. Nothing is
  stored. A version the catalog does not know raises
  ``PolicyRuleSetNotFoundError`` when ``PublishGate`` is built, with no fallback.
  ``PolicyGate`` then gives one ``policy.failed`` reason per blocking finding,
  in catalog order, and a warning passes. A channel without a strategy profile
  fails closed (``gate.error``). The item has no description or tags at publish
  time, so the gate checks the title only; ``PolicyReporter`` (G-083) may also
  check a caller's description and tags and can therefore block where the gate
  passes.
- ``evaluate`` only reads. It opens one connection with a deferred ``BEGIN``, so
  every gate sees the same snapshot, shares it between all repositories and rolls
  it back and closes it at the end. It takes no write lock, writes nothing and
  writes no audit event.
- The item must be ``APPROVED``: any other status raises, as the gate contract
  does. A gate that fails is a block (``evaluate_gates`` fails closed).
  ``ensure_can_publish`` raises ``GateBlockedError`` when the report blocks.

Nothing calls ``PublishGate`` yet: the pipeline runner (#206) does.
"""

import sqlite3
from collections.abc import Sequence
from datetime import UTC, datetime

from ai_youtube_agent.content.policy import PolicyCheck
from ai_youtube_agent.content.policy_check import (
    POLICY_RULES,
    PolicyChecker,
    PolicyInput,
    PolicyRuleSetCatalog,
    default_catalog,
)
from ai_youtube_agent.content.policy_rule import RuleSet
from ai_youtube_agent.content.rights_assessment import RULES_VERSION, current_facts
from ai_youtube_agent.content.strategy_settings import StrategyNotFoundError
from ai_youtube_agent.content.text_prompts import banned_phrases
from ai_youtube_agent.core.approval_gate import ApprovalGate
from ai_youtube_agent.core.audit import Actor
from ai_youtube_agent.core.config import Settings
from ai_youtube_agent.core.content_item import ContentItem, ContentStatus
from ai_youtube_agent.core.daily_limit_gate import DailyLimitGate
from ai_youtube_agent.core.db.database import Database
from ai_youtube_agent.core.db.repositories.asset import AssetRepository
from ai_youtube_agent.core.db.repositories.channel import StrategyProfileRepository
from ai_youtube_agent.core.db.repositories.content import ArtifactRepository
from ai_youtube_agent.core.db.repositories.jobs import AIJobRepository
from ai_youtube_agent.core.db.repositories.provenance import ProvenanceRepository
from ai_youtube_agent.core.db.repositories.publish import PublishJobRepository
from ai_youtube_agent.core.db.repositories.review import (
    ApprovalRequestRepository,
    RightsRecordRepository,
)
from ai_youtube_agent.core.db.repositories.rights_assessment import (
    RightsAssessmentRepository,
)
from ai_youtube_agent.core.db.repositories.usage import DailyUsageRepository
from ai_youtube_agent.core.gates import (
    GateContext,
    GateName,
    GateReport,
    PipelineGate,
    evaluate_gates,
)
from ai_youtube_agent.core.idempotency_gate import IdempotencyGate
from ai_youtube_agent.core.policy_gate import PolicyGate
from ai_youtube_agent.core.rights_gate import (
    AssessmentBasis,
    RightsGate,
    blocking_levels_for,
)

DEFERRED_GATES = (GateName.KILL_SWITCH,)
"""Gates that guard a publish but are not part of the set yet. The kill switch has
no store before #213, which adds it. Budget and strategy guard ``GENERATING``
only, so a publish does not run them."""


class RepositoryFreshness:
    """``FreshnessSource`` over the assessment, asset and provenance repositories
    of one connection.

    ``rules_version`` exists to simulate a rules change in tests. In production it
    must equal what the engine writes (``RULES_VERSION``)."""

    def __init__(
        self,
        assessments: RightsAssessmentRepository,
        assets: AssetRepository,
        provenances: ProvenanceRepository,
        *,
        rules_version: str = RULES_VERSION,
    ) -> None:
        if (
            not isinstance(rules_version, str)
            or not rules_version.strip()
            or rules_version != rules_version.strip()
        ):
            raise ValueError("rules_version must be text without outer whitespace")
        self._assessments = assessments
        self._assets = assets
        self._provenances = provenances
        self._rules_version = rules_version

    def assessed_basis(self, rights_record_id: str) -> AssessmentBasis | None:
        newest = self._assessments.latest(rights_record_id)
        if newest is None:
            return None
        return AssessmentBasis(
            newest.asset_id, newest.provenance_id, newest.rules_version
        )

    def current_basis(self, asset_ref: str, channel_id: str) -> AssessmentBasis:
        asset, provenance = current_facts(
            self._assets, self._provenances, asset_ref, channel_id
        )
        if asset is None:
            return AssessmentBasis(None, None, self._rules_version)
        return AssessmentBasis(
            asset.id, provenance.id if provenance else None, self._rules_version
        )


class CheckedPolicySource:
    """``PolicySource`` that checks the item being evaluated with the G-080
    ``PolicyChecker`` instead of reading stored checks.

    It judges ``context.item`` (the item passed to ``evaluate``, as the other
    gates do) and answers only for that item: without a context or for another
    id it raises. The banned phrases come from the strategy profile of the
    item's channel; a channel without one raises ``StrategyNotFoundError``, so
    the gate fails closed. The check is built at ``context.at`` and never
    stored."""

    def __init__(
        self,
        profiles: StrategyProfileRepository,
        checker: PolicyChecker,
        rule_set: RuleSet,
        context: GateContext | None,
    ) -> None:
        self._profiles = profiles
        self._checker = checker
        self._rule_set = rule_set
        self._context = context

    def list_by_content_item(self, content_item_id: str) -> Sequence[PolicyCheck]:
        context = self._context
        if context is None or content_item_id != context.item.id:
            raise ValueError("the policy source only checks the item being evaluated")
        item = context.item
        profile = self._profiles.get_by_channel(item.channel_id)
        if profile is None:
            raise StrategyNotFoundError(f"channel {item.channel_id} has no strategy")
        policy_input = PolicyInput(
            item.id,
            item.channel_id,
            item.title,
            banned_phrases=banned_phrases(profile),
        )
        result = self._checker.check(policy_input, rule_set=self._rule_set)
        return (result.to_policy_check(item.id, clock=lambda: context.at),)


class PublishGate:
    def __init__(
        self,
        database: Database,
        settings: Settings,
        *,
        catalog: PolicyRuleSetCatalog | None = None,
    ) -> None:
        self._database = database
        self._blocking_levels = blocking_levels_for(settings.rights_block_levels)
        self._catalog = catalog if catalog is not None else default_catalog()
        self._rule_set = RuleSet(POLICY_RULES.id, settings.policy_rule_set_version)
        # An unknown rule set raises PolicyRuleSetNotFoundError here, not at the
        # first publish, and never falls back to another version.
        self._catalog.rules_for(self._rule_set)
        self._checker = PolicyChecker(self._catalog)
        # Build the set once without a connection, so that a bad configuration
        # fails here and not at the first publish.
        self._gates(None)

    @property
    def gate_names(self) -> tuple[GateName, ...]:
        return tuple(gate.name for gate in self._gates(None))

    def evaluate(
        self, item: ContentItem, *, actor: Actor, at: datetime | None = None
    ) -> GateReport:
        context = GateContext(
            item, ContentStatus.PUBLISHING, actor, at or datetime.now(UTC)
        )
        connection = self._database.connect()
        try:
            connection.execute("BEGIN")
            try:
                return evaluate_gates(self._gates(connection, context), context)
            finally:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
        finally:
            connection.close()

    def ensure_can_publish(
        self, item: ContentItem, *, actor: Actor, at: datetime | None = None
    ) -> GateReport:
        report = self.evaluate(item, actor=actor, at=at)
        report.raise_if_blocked()
        return report

    def _gates(
        self,
        connection: sqlite3.Connection | None,
        context: GateContext | None = None,
    ) -> list[PipelineGate]:
        approvals = ApprovalRequestRepository(connection)
        profiles = StrategyProfileRepository(connection)
        return [
            ApprovalGate(approvals, ArtifactRepository(connection)),
            DailyLimitGate(profiles, DailyUsageRepository(connection)),
            RightsGate(
                RightsRecordRepository(connection),
                blocking_levels=self._blocking_levels,
                freshness=RepositoryFreshness(
                    RightsAssessmentRepository(connection),
                    AssetRepository(connection),
                    ProvenanceRepository(connection),
                ),
            ),
            PolicyGate(
                CheckedPolicySource(profiles, self._checker, self._rule_set, context)
            ),
            IdempotencyGate(
                AIJobRepository(connection),
                PublishJobRepository(connection),
                approvals,
            ),
        ]
