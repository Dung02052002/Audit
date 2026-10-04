"""Rights Gate (Prompt Pack v8, prompt #038), context C3 Control Gates.

``RightsGate`` blocks a publish while an asset of the item has an unresolved
high rights risk. The rules were approved by the user on 2026-10-01:

- The gate only judges the move to ``PUBLISHING``. Any other move passes.
- It reads every ``RightsRecord`` of the item. A record blocks when it is
  unresolved and its level is ``high``, or ``unknown`` (a level nobody has set
  yet counts as high, so the gate fails closed). Unresolved ``low`` and
  ``medium`` pass, and a record a user resolved passes at any level.
- An item with no rights records passes. Whether every asset has a record is
  for the asset registry (#076) and rights QC (#126).
- Each blocking record gives its own reason (``rights.unresolved_high`` or
  ``rights.unresolved_unknown``) naming its ``asset_ref``, in record order
  (``created_at``, then ``id``).

The gate reads through ``RightsSource``, which ``RightsRecordRepository``
already satisfies. Classifying risk is the risk engine (#078).

Since #078 the blocking levels are configurable: ``RightsGate(rights,
blocking_levels=...)`` takes a set that always holds ``high`` and ``unknown``
and may add ``medium`` (reason ``rights.unresolved_medium``). The default set is
``BLOCKING_LEVELS``, so the behaviour above is unchanged. ``blocking_levels_for``
maps the ``Settings.rights_block_levels`` value to such a set. Wiring rights and
policy into the shared publish gate, and building the gate from the settings,
is #084.

Since G-078b the gate can also check that a record was assessed on the current
facts. ``RightsGate(rights, freshness=...)`` takes a ``FreshnessSource`` and, for
each record, gives at most one reason, in this order:

1. the record is unresolved and its level is in ``blocking_levels``: the level
   reason above, which has priority;
2. else, with a ``freshness`` source and an unresolved record: if the record was
   never assessed, or the basis of its newest assessment (the pair ``asset_id``,
   ``provenance_id``) is not the current basis of its ``asset_ref`` for the
   channel of the item, ``rights.assessment_stale`` ("Asset X has no current
   rights assessment."), because the level on the record may no longer be true;
3. otherwise the record passes. A resolved record is never checked for freshness.

Without a ``freshness`` source the behaviour is exactly as before. The gate only
compares two bases it is given: how a basis is read, and the rules that turn it
into a level, stay outside it (``core/publish_gate.py`` reads the bases and
``content/rights_assessment.py`` holds the rules, which this module does not
import).
"""

from collections.abc import Collection, Iterable, Sequence
from typing import NamedTuple, Protocol

from ai_youtube_agent.content.rights import RightsRecord, RiskLevel
from ai_youtube_agent.core.config import RightsBlockLevel
from ai_youtube_agent.core.content_item import ContentStatus
from ai_youtube_agent.core.gates import GateContext, GateName, GateReason, GateResult

BLOCKING_LEVELS = frozenset({RiskLevel.HIGH, RiskLevel.UNKNOWN})
ALLOWED_BLOCKING_LEVELS = frozenset(
    {RiskLevel.MEDIUM, RiskLevel.HIGH, RiskLevel.UNKNOWN}
)


class AssessmentBasis(NamedTuple):
    """What an assessment was based on: the asset and its latest provenance.
    Both are ``None`` when the asset is not a registered asset of the channel."""

    asset_id: str | None
    provenance_id: str | None


class RightsSource(Protocol):
    def list_by_content_item(self, content_item_id: str) -> Sequence[RightsRecord]: ...


class FreshnessSource(Protocol):
    def assessed_basis(self, rights_record_id: str) -> AssessmentBasis | None:
        """The basis of the newest assessment, or ``None`` if never assessed."""
        ...

    def current_basis(self, asset_ref: str, channel_id: str) -> AssessmentBasis:
        """The basis an assessment made now would have."""
        ...


class RightsGate:
    name = GateName.RIGHTS

    def __init__(
        self,
        rights: RightsSource,
        *,
        blocking_levels: Collection[RiskLevel] = BLOCKING_LEVELS,
        freshness: FreshnessSource | None = None,
    ) -> None:
        levels = frozenset(blocking_levels)
        if not levels >= BLOCKING_LEVELS:
            raise ValueError("blocking_levels must contain high and unknown")
        if not levels <= ALLOWED_BLOCKING_LEVELS:
            raise ValueError("blocking_levels may only hold medium, high and unknown")
        self._rights = rights
        self._blocking_levels = levels
        self._freshness = freshness

    def evaluate(self, context: GateContext) -> GateResult:
        if context.target_status is not ContentStatus.PUBLISHING:
            return GateResult.passed(self.name, context.at)
        records = sorted(
            self._rights.list_by_content_item(context.item.id),
            key=lambda r: (r.created_at, r.id),
        )
        reasons = [
            reason
            for reason in (self._reason_for(r, context) for r in records)
            if reason is not None
        ]
        if not reasons:
            return GateResult.passed(self.name, context.at)
        return GateResult.blocked(self.name, context.at, *reasons)

    def _reason_for(
        self, record: RightsRecord, context: GateContext
    ) -> GateReason | None:
        if record.is_resolved:
            return None
        if record.risk_level in self._blocking_levels:
            return _reason(record)
        if self._freshness is not None and self._is_stale(record, context):
            return GateReason(
                "rights.assessment_stale",
                f"Asset {record.asset_ref} has no current rights assessment.",
            )
        return None

    def _is_stale(self, record: RightsRecord, context: GateContext) -> bool:
        assessed = self._freshness.assessed_basis(record.id)
        return assessed is None or assessed != self._freshness.current_basis(
            record.asset_ref, context.item.channel_id
        )


def blocking_levels_for(configured: Iterable[RightsBlockLevel]) -> frozenset[RiskLevel]:
    """The levels a gate blocks for ``Settings.rights_block_levels``: the
    configured levels and ``unknown``, which always blocks."""
    return frozenset({RiskLevel.UNKNOWN} | {RiskLevel(level) for level in configured})


def _reason(record: RightsRecord) -> GateReason:
    if record.risk_level is RiskLevel.HIGH:
        return GateReason(
            "rights.unresolved_high",
            f"Asset {record.asset_ref} has a high rights risk that is not resolved.",
        )
    if record.risk_level is RiskLevel.MEDIUM:
        return GateReason(
            "rights.unresolved_medium",
            f"Asset {record.asset_ref} has a medium rights risk that is not resolved.",
        )
    return GateReason(
        "rights.unresolved_unknown",
        f"Asset {record.asset_ref} has an unknown rights risk that is not resolved.",
    )
