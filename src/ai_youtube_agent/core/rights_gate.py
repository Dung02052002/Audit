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
"""

from collections.abc import Collection, Iterable, Sequence
from typing import Protocol

from ai_youtube_agent.content.rights import RightsRecord, RiskLevel
from ai_youtube_agent.core.config import RightsBlockLevel
from ai_youtube_agent.core.content_item import ContentStatus
from ai_youtube_agent.core.gates import GateContext, GateName, GateReason, GateResult

BLOCKING_LEVELS = frozenset({RiskLevel.HIGH, RiskLevel.UNKNOWN})
ALLOWED_BLOCKING_LEVELS = frozenset(
    {RiskLevel.MEDIUM, RiskLevel.HIGH, RiskLevel.UNKNOWN}
)


class RightsSource(Protocol):
    def list_by_content_item(self, content_item_id: str) -> Sequence[RightsRecord]: ...


class RightsGate:
    name = GateName.RIGHTS

    def __init__(
        self,
        rights: RightsSource,
        *,
        blocking_levels: Collection[RiskLevel] = BLOCKING_LEVELS,
    ) -> None:
        levels = frozenset(blocking_levels)
        if not levels >= BLOCKING_LEVELS:
            raise ValueError("blocking_levels must contain high and unknown")
        if not levels <= ALLOWED_BLOCKING_LEVELS:
            raise ValueError("blocking_levels may only hold medium, high and unknown")
        self._rights = rights
        self._blocking_levels = levels

    def evaluate(self, context: GateContext) -> GateResult:
        if context.target_status is not ContentStatus.PUBLISHING:
            return GateResult.passed(self.name, context.at)
        records = sorted(
            self._rights.list_by_content_item(context.item.id),
            key=lambda r: (r.created_at, r.id),
        )
        reasons = [_reason(r) for r in records if self._blocks(r)]
        if not reasons:
            return GateResult.passed(self.name, context.at)
        return GateResult.blocked(self.name, context.at, *reasons)

    def _blocks(self, record: RightsRecord) -> bool:
        return not record.is_resolved and record.risk_level in self._blocking_levels


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
