"""Strategy Gate (Prompt Pack v8, prompt #053), context C3 Control Gates.

``StrategyGate`` (``GateName.STRATEGY``) stops a run when the channel's
strategy is missing or incompatible. The rules were approved by the user on
2026-10-02:

- Every move into ``GENERATING`` is a run: a new production (from draft) and a
  regeneration. Any other move passes.
- The gate runs ``validate_strategy`` (``content/strategy_validation.py``) for
  the item's channel, content type and strategy version with the current
  feature flags. Each blocking finding becomes one reason with the finding's
  code; warnings never block and are not reported by the gate (they are shown
  by ``GET /channels/{id}/strategy/validation``).

The gate reads through ``StrategySource``, which ``StrategyProfileRepository``
satisfies.
"""

from ai_youtube_agent.content.strategy_validation import validate_strategy
from ai_youtube_agent.core.content_item import ContentStatus
from ai_youtube_agent.core.daily_limit_gate import StrategySource
from ai_youtube_agent.core.flags import FeatureFlags
from ai_youtube_agent.core.gates import GateContext, GateName, GateReason, GateResult


class StrategyGate:
    name = GateName.STRATEGY

    def __init__(self, strategies: StrategySource, flags: FeatureFlags) -> None:
        self._strategies = strategies
        self._flags = flags

    def evaluate(self, context: GateContext) -> GateResult:
        if context.target_status is not ContentStatus.GENERATING:
            return GateResult.passed(self.name, context.at)
        item = context.item
        validation = validate_strategy(
            self._strategies.get_by_channel(item.channel_id),
            self._flags,
            content_type=item.content_type,
            item_strategy_version=item.strategy_version,
        )
        if validation.is_valid:
            return GateResult.passed(self.name, context.at)
        return GateResult.blocked(
            self.name,
            context.at,
            *(GateReason(f.code, f.message) for f in validation.blocking),
        )
