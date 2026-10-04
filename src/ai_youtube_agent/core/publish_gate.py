"""Publish Gate (Prompt Pack v8, prompt G-078b), context C3 Control Gates.

``PublishGate`` runs the gates that guard the move of an approved content item to
``PUBLISHING`` against the real repositories, as the user approved on
2026-10-04:

- The gates run in the order of the C-042 gate matrix (``tests/test_gate_matrix.py``):
  approval, daily limit, rights, idempotency. ``DEFERRED_GATES`` lists the two
  that have no store yet.
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
from datetime import UTC, datetime

from ai_youtube_agent.content.rights_assessment import RULES_VERSION, current_facts
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
from ai_youtube_agent.core.rights_gate import (
    AssessmentBasis,
    RightsGate,
    blocking_levels_for,
)

DEFERRED_GATES = (GateName.POLICY, GateName.KILL_SWITCH)
"""Gates that guard a publish but are not part of the set yet. Policy has no store
before #080 and the kill switch none before #213. #084 and #213 add them. Budget
and strategy guard ``GENERATING`` only, so a publish does not run them."""


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


class PublishGate:
    def __init__(self, database: Database, settings: Settings) -> None:
        self._database = database
        self._blocking_levels = blocking_levels_for(settings.rights_block_levels)
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
                return evaluate_gates(self._gates(connection), context)
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

    def _gates(self, connection: sqlite3.Connection | None) -> list[PipelineGate]:
        approvals = ApprovalRequestRepository(connection)
        return [
            ApprovalGate(approvals, ArtifactRepository(connection)),
            DailyLimitGate(
                StrategyProfileRepository(connection), DailyUsageRepository(connection)
            ),
            RightsGate(
                RightsRecordRepository(connection),
                blocking_levels=self._blocking_levels,
                freshness=RepositoryFreshness(
                    RightsAssessmentRepository(connection),
                    AssetRepository(connection),
                    ProvenanceRepository(connection),
                ),
            ),
            IdempotencyGate(
                AIJobRepository(connection),
                PublishJobRepository(connection),
                approvals,
            ),
        ]
