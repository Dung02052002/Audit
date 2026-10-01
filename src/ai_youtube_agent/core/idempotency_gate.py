"""Idempotency Gate (Prompt Pack v8, prompt #041), context C3 Control Gates.

``IdempotencyGate`` prevents duplicate generation and publish jobs using the
deterministic keys of ``pipeline/idempotency.py``. The rules were approved by
the user on 2026-10-01:

- A move into ``GENERATING`` derives ``generation_key(item)``; the move to
  ``PUBLISHING`` derives ``publish_key`` from the item's newest approval
  request, when it is approved. Any other move passes.
- A job already stored under that key blocks unless it failed: queued,
  running, waiting, in progress and succeeded are in flight or done, and a
  cancelled job is final while its key stays taken (UNIQUE). A failed job
  passes, because a retry restarts that same job; retry limits are #153 and
  #208.
- When the newest approval request is missing or not approved there is no
  publish key and the gate passes; the approval gate (#034) blocks that move.

The gate reads through three small interfaces. ``AIJobRepository``,
``PublishJobRepository`` and ``ApprovalRequestRepository`` already satisfy
them. #151 and #152 add upload-level publish guarantees.
"""

from collections.abc import Sequence
from typing import Protocol

from ai_youtube_agent.content.approval import ApprovalRequest, ApprovalStatus
from ai_youtube_agent.core.content_item import ContentStatus
from ai_youtube_agent.core.gates import GateContext, GateName, GateReason, GateResult
from ai_youtube_agent.pipeline.idempotency import generation_key, publish_key
from ai_youtube_agent.pipeline.job import AIJob, AIJobStatus
from ai_youtube_agent.pipeline.publish import PublishJob, PublishStatus


class AIJobSource(Protocol):
    def get_by_idempotency_key(self, key: str) -> AIJob | None: ...


class PublishJobSource(Protocol):
    def get_by_idempotency_key(self, key: str) -> PublishJob | None: ...


class ApprovalSource(Protocol):
    def list_by_content_item(
        self, content_item_id: str
    ) -> Sequence[ApprovalRequest]: ...


class IdempotencyGate:
    name = GateName.IDEMPOTENCY

    def __init__(
        self,
        jobs: AIJobSource,
        publishes: PublishJobSource,
        approvals: ApprovalSource,
    ) -> None:
        self._jobs = jobs
        self._publishes = publishes
        self._approvals = approvals

    def evaluate(self, context: GateContext) -> GateResult:
        reason = None
        if context.target_status is ContentStatus.GENERATING:
            reason = self._check_generation(context)
        elif context.target_status is ContentStatus.PUBLISHING:
            reason = self._check_publish(context)
        if reason is None:
            return GateResult.passed(self.name, context.at)
        return GateResult.blocked(self.name, context.at, reason)

    def _check_generation(self, context: GateContext) -> GateReason | None:
        job = self._jobs.get_by_idempotency_key(generation_key(context.item))
        if job is None or job.status is AIJobStatus.FAILED:
            return None
        return GateReason(
            "idempotency.duplicate_generation",
            f"A generation job for this content is already {job.status.value}.",
        )

    def _check_publish(self, context: GateContext) -> GateReason | None:
        requests = self._approvals.list_by_content_item(context.item.id)
        if not requests:
            return None
        newest = max(requests, key=lambda r: (r.created_at, r.id))
        if newest.status is not ApprovalStatus.APPROVED:
            return None
        job = self._publishes.get_by_idempotency_key(
            publish_key(context.item.id, newest.id)
        )
        if job is None or job.status is PublishStatus.FAILED:
            return None
        return GateReason(
            "idempotency.duplicate_publish",
            f"A publish job for this approval is already {job.status.value}.",
        )
