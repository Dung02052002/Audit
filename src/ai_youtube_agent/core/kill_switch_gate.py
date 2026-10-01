"""Kill Switch Gate (Prompt Pack v8, prompt #040), context C3 Control Gates.

``KillSwitchGate`` blocks all new production actions while the emergency stop
is active. The rules were approved by the user on 2026-10-01:

- New production actions are every move into ``GENERATING`` (a new production
  or a regeneration) and the move to ``PUBLISHING``. Any other move passes, so
  work can still be wound down to draft, failed or rejected, or reviewed.
- The gate reads the current ``EmergencyStop`` on every evaluation, so a stop
  takes effect for the next move.
- An active stop blocks with one ``killswitch.active`` reason and a fixed
  message, plus the reason the activator gave, if any. Who activated it is not
  shown.

The gate reads through ``KillSwitchSource``. No store exists yet; the kill
switch (#213) supplies one with its user toggle. A source that fails blocks,
because gates fail closed (#033).
"""

from typing import Protocol

from ai_youtube_agent.core.content_item import ContentStatus
from ai_youtube_agent.core.gates import GateContext, GateName, GateReason, GateResult
from ai_youtube_agent.pipeline.kill_switch import EmergencyStop

STOPPED_MOVES = frozenset({ContentStatus.GENERATING, ContentStatus.PUBLISHING})
STOP_MESSAGE = (
    "An emergency stop is active, so new production and publishing are blocked."
)


class KillSwitchSource(Protocol):
    def current(self) -> EmergencyStop: ...


class KillSwitchGate:
    name = GateName.KILL_SWITCH

    def __init__(self, switch: KillSwitchSource) -> None:
        self._switch = switch

    def evaluate(self, context: GateContext) -> GateResult:
        if context.target_status not in STOPPED_MOVES:
            return GateResult.passed(self.name, context.at)
        stop = self._switch.current()
        if not stop.active:
            return GateResult.passed(self.name, context.at)
        message = STOP_MESSAGE
        if stop.reason:
            message = f"{message} Reason: {stop.reason}"
        return GateResult.blocked(
            self.name, context.at, GateReason("killswitch.active", message)
        )
