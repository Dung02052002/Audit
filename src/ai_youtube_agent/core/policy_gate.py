"""Policy Gate (Prompt Pack v8, prompt #039), context C3 Control Gates.

``PolicyGate`` blocks a publish on configured policy failures. The rules were
approved by the user on 2026-10-01:

- The gate only judges the move to ``PUBLISHING``. Any other move passes.
- Only the item's newest ``PolicyCheck`` counts (latest ``checked_at``, then
  ``id``), so a failure fixed by a later check no longer blocks.
- Each blocking finding of that check gives its own ``policy.failed`` reason
  naming the rule and its version. Non-blocking findings are warnings and pass.
- An item without any policy check blocks with ``policy.not_checked``: the gate
  fails closed.

The gate reads through ``PolicySource``. No repository exists yet; the policy
check (#080) and report (#083) supply one, and #084 wires rights and policy
into the shared publish gate.
"""

from collections.abc import Sequence
from typing import Protocol

from ai_youtube_agent.content.policy import PolicyCheck, PolicyFinding
from ai_youtube_agent.core.content_item import ContentStatus
from ai_youtube_agent.core.gates import GateContext, GateName, GateReason, GateResult


class PolicySource(Protocol):
    def list_by_content_item(self, content_item_id: str) -> Sequence[PolicyCheck]: ...


class PolicyGate:
    name = GateName.POLICY

    def __init__(self, checks: PolicySource) -> None:
        self._checks = checks

    def evaluate(self, context: GateContext) -> GateResult:
        if context.target_status is not ContentStatus.PUBLISHING:
            return GateResult.passed(self.name, context.at)
        checks = self._checks.list_by_content_item(context.item.id)
        if not checks:
            return GateResult.blocked(
                self.name,
                context.at,
                GateReason(
                    "policy.not_checked",
                    "This content has not passed a policy check yet.",
                ),
            )
        newest = max(checks, key=lambda c: (c.checked_at, c.id))
        reasons = [_reason(f) for f in newest.blocking_findings]
        if not reasons:
            return GateResult.passed(self.name, context.at)
        return GateResult.blocked(self.name, context.at, *reasons)


def _reason(finding: PolicyFinding) -> GateReason:
    return GateReason(
        "policy.failed",
        f"Policy rule {finding.rule_id} (version {finding.rule_version}) failed: "
        f"{finding.message}",
    )
