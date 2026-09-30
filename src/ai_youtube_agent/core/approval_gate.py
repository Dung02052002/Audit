"""Approval Gate (Prompt Pack v8, prompt #034), context C3 Control Gates.

``ApprovalGate`` blocks a publish unless an explicit approval exists for the
current artifact versions (R-08). The rules were approved by the user on
2026-09-30:

- The gate only judges the move to ``PUBLISHING``. Any other move passes.
- Only the item's newest ``ApprovalRequest`` counts (latest ``created_at``,
  then ``id``, the order the repository uses). If it is not ``APPROVED`` the
  publish is blocked, even when an older request was approved.
- The approval must cover every artifact kind the item has, each bound to the
  latest version of that kind: same id, version and sha256. A newer version, a
  kind added after approval, or a bound artifact that no longer matches blocks.
- The gate always checks. It does not read ``APPROVAL_REQUIRED``, because
  publishing cannot be enabled without it (A-006) and production forces it on.

The gate reads through two small interfaces. ``ApprovalRequestRepository`` and
``ArtifactRepository`` already satisfy them. The version comparison is
``stale_kinds`` in ``content/approval.py``, shared with #035, which marks a
stale approval as invalidated; this gate only refuses to use it.
"""

from collections.abc import Sequence
from typing import Protocol

from ai_youtube_agent.content.approval import (
    ApprovalRequest,
    ApprovalStatus,
    stale_kinds,
)
from ai_youtube_agent.core.artifact import Artifact
from ai_youtube_agent.core.content_item import ContentStatus
from ai_youtube_agent.core.gates import GateContext, GateName, GateReason, GateResult


class ApprovalSource(Protocol):
    def list_by_content_item(
        self, content_item_id: str
    ) -> Sequence[ApprovalRequest]: ...


class ArtifactSource(Protocol):
    def list_by_content_item(self, content_item_id: str) -> Sequence[Artifact]: ...


class ApprovalGate:
    name = GateName.APPROVAL

    def __init__(self, approvals: ApprovalSource, artifacts: ArtifactSource) -> None:
        self._approvals = approvals
        self._artifacts = artifacts

    def evaluate(self, context: GateContext) -> GateResult:
        if context.target_status is not ContentStatus.PUBLISHING:
            return GateResult.passed(self.name, context.at)
        reason = self._check(context.item.id)
        if reason is None:
            return GateResult.passed(self.name, context.at)
        return GateResult.blocked(self.name, context.at, reason)

    def _check(self, item_id: str) -> GateReason | None:
        requests = self._approvals.list_by_content_item(item_id)
        if not requests:
            return GateReason(
                "approval.missing", "This content has no approval to publish."
            )
        newest = max(requests, key=lambda r: (r.created_at, r.id))
        if newest.status is not ApprovalStatus.APPROVED:
            return GateReason(
                "approval.not_approved",
                f"The latest approval request is {newest.status.value}, not approved.",
            )
        stale = stale_kinds(newest, self._artifacts.list_by_content_item(item_id))
        if stale:
            kinds = ", ".join(kind.value for kind in stale)
            return GateReason(
                "approval.not_current",
                f"The approval does not cover the current version of: {kinds}.",
            )
        return None
