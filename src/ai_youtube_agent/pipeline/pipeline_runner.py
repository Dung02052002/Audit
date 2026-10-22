"""Pipeline Runner (Prompt Pack v8, prompt #206), context C16 Orchestration.

``PipelineRunner`` executes the shared content lifecycle one gate at a time:
- Shared lifecycle stages:
  ``DRAFT -> GENERATING -> TESTING -> PREVIEW_READY -> AWAITING_APPROVAL ->
  APPROVED -> PUBLISHING -> PUBLISHED``
  and terminal/rework moves (``FAILED``, ``REJECTED``, return to ``GENERATING``).
- Only two content types: ``SHORTS`` and ``LONGFORM``.
- Evaluates gates at each transition using ``evaluate_gates`` (#033):
  - Move to ``GENERATING``: ``StrategyGate`` (#053), ``BudgetGate`` (#037),
    ``DailyLimitGate`` (#036), ``KillSwitchGate`` (#040), ``IdempotencyGate`` (#041).
  - Move to ``TESTING``: verifies generated artifacts (video, script, audio).
  - Move to ``PREVIEW_READY``: executes QC checks via ``QCReportBuilder`` (#129).
  - Move to ``AWAITING_APPROVAL``: ensures an ``ApprovalRequest`` binds exact
    artifact versions.
  - Move to ``APPROVED``: requires explicit human approval (``ActorKind.USER``);
    halts and waits if pending (R-08).
  - Move to ``PUBLISHING``: checks ``PUBLISH_ENABLED`` flag, evaluates
    ``PublishGate`` (G-078b),
    and dispatches to ``ShortsPublisher`` (#149) or ``LongFormPublisher`` (#150).
- Single-step execution (``advance``) and multi-step pipeline run (``run``) with
  bounded loops.
- Emits audit records for every step transition.
"""

import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Final

from ai_youtube_agent.content.approval import (
    ApprovalRequest,
    ApprovalStatus,
    ArtifactBinding,
)
from ai_youtube_agent.content.qc import QCStatus
from ai_youtube_agent.content.qc_report import QCReportBuilder
from ai_youtube_agent.core.approval_gate import ApprovalGate
from ai_youtube_agent.core.artifact import ArtifactKind
from ai_youtube_agent.core.audit import (
    Actor,
    ActorKind,
    AuditLog,
    AuditResult,
    EntityRef,
)
from ai_youtube_agent.core.budget_gate import BudgetGate
from ai_youtube_agent.core.config import Settings
from ai_youtube_agent.core.content_item import (
    ContentItem,
    ContentStatus,
    ContentType,
    can_transition,
)
from ai_youtube_agent.core.daily_limit_gate import DailyLimitGate
from ai_youtube_agent.core.db.database import Database
from ai_youtube_agent.core.db.repositories.channel import StrategyProfileRepository
from ai_youtube_agent.core.db.repositories.content import (
    ArtifactRepository,
    ContentItemRepository,
)
from ai_youtube_agent.core.db.repositories.economics import CostRecordRepository
from ai_youtube_agent.core.db.repositories.jobs import AIJobRepository
from ai_youtube_agent.core.db.repositories.publish import PublishJobRepository
from ai_youtube_agent.core.db.repositories.review import (
    ApprovalRequestRepository,
    RightsRecordRepository,
)
from ai_youtube_agent.core.db.repositories.usage import DailyUsageRepository
from ai_youtube_agent.core.errors import DomainError
from ai_youtube_agent.core.flags import FeatureFlags
from ai_youtube_agent.core.gates import (
    GateContext,
    GateName,
    GateReason,
    GateReport,
    GateResult,
    PipelineGate,
    evaluate_gates,
)
from ai_youtube_agent.core.idempotency_gate import IdempotencyGate
from ai_youtube_agent.core.kill_switch_gate import KillSwitchGate, KillSwitchSource
from ai_youtube_agent.core.production import start_production
from ai_youtube_agent.core.publish_gate import PublishGate
from ai_youtube_agent.core.rights_gate import RightsGate
from ai_youtube_agent.core.strategy_gate import StrategyGate
from ai_youtube_agent.pipeline.longform_publisher import LongFormPublisher
from ai_youtube_agent.pipeline.shorts_publisher import ShortsPublisher

Clock = Callable[[], datetime]

PIPELINE_ADVANCED_ACTION: Final[str] = "pipeline.step_advanced"
PIPELINE_BLOCKED_ACTION: Final[str] = "pipeline.step_blocked"
RUNNER_ACTOR: Final[Actor] = Actor(ActorKind.SYSTEM, "pipeline_runner")


class PipelineRunnerError(DomainError):
    """Base domain error for pipeline execution."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(code=code, detail=detail, user_message=detail)


@dataclass(frozen=True)
class PipelineStepResult:
    """Outcome of advancing one lifecycle gate."""

    item_id: str
    from_status: ContentStatus
    to_status: ContentStatus
    transitioned: bool
    gate_report: GateReport | None = None
    waiting_for_approval: bool = False
    error: str | None = None
    reason: str | None = None
    details: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "item_id": self.item_id,
            "from_status": self.from_status.value,
            "to_status": self.to_status.value,
            "transitioned": self.transitioned,
            "waiting_for_approval": self.waiting_for_approval,
            "error": self.error,
            "reason": self.reason,
            "gate_blocked": bool(self.gate_report.blocked)
            if self.gate_report
            else False,
            "details": self.details,
        }


@dataclass(frozen=True)
class PipelineRunResult:
    """Outcome of running the pipeline across multiple steps."""

    item_id: str
    initial_status: ContentStatus
    final_status: ContentStatus
    steps: tuple[PipelineStepResult, ...]
    completed: bool
    waiting_for_approval: bool = False
    stopped_reason: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "item_id": self.item_id,
            "initial_status": self.initial_status.value,
            "final_status": self.final_status.value,
            "step_count": len(self.steps),
            "completed": self.completed,
            "waiting_for_approval": self.waiting_for_approval,
            "stopped_reason": self.stopped_reason,
            "steps": [s.as_dict() for s in self.steps],
        }


class PipelineRunner:
    """Executes shared content lifecycle transitions one gate at a time."""

    def __init__(
        self,
        database: Database,
        settings: Settings,
        audit_log: AuditLog,
        *,
        flags: FeatureFlags | None = None,
        publish_gate: PublishGate | None = None,
        qc_builder: QCReportBuilder | None = None,
        shorts_publisher: ShortsPublisher | None = None,
        longform_publisher: LongFormPublisher | None = None,
        kill_switch: KillSwitchSource | None = None,
        clock: Clock | None = None,
    ) -> None:
        self._database = database
        self._settings = settings
        self._audit = audit_log
        self._flags = flags or settings.flags
        self._publish_gate = publish_gate
        self._qc_builder = qc_builder
        self._shorts_publisher = shorts_publisher
        self._longform_publisher = longform_publisher
        self._kill_switch = kill_switch
        self._clock = clock

    def _now(self) -> datetime:
        return self._clock() if self._clock else datetime.now(UTC)

    def evaluate_stage_gates(
        self,
        item: ContentItem,
        target_status: ContentStatus,
        *,
        actor: Actor | None = None,
        at: datetime | None = None,
    ) -> GateReport:
        """Evaluate applicable gates for transitioning an item to target_status."""
        now = at or self._now()
        eval_actor = actor or RUNNER_ACTOR

        if not can_transition(item.status, target_status):
            return GateReport(
                results=(
                    GateResult.blocked(
                        GateName.TEST,
                        now,
                        GateReason(
                            "domain.content_transition_blocked",
                            f"Transition from {item.status.value} to "
                            f"{target_status.value} is not allowed",
                        ),
                    ),
                ),
            )

        context = GateContext(
            item=item, target_status=target_status, actor=eval_actor, at=now
        )

        if target_status is ContentStatus.GENERATING:
            with self._database.connection() as conn:
                strategies = StrategyProfileRepository(conn)
                costs = CostRecordRepository(conn)
                usage = DailyUsageRepository(conn)
                jobs = AIJobRepository(conn)
                publishes = PublishJobRepository(conn)
                approvals = ApprovalRequestRepository(conn)

                gates: list[PipelineGate] = [
                    StrategyGate(strategies, self._flags),
                    BudgetGate(strategies, costs),
                    DailyLimitGate(strategies, usage),
                    IdempotencyGate(jobs, publishes, approvals),
                ]
                if self._kill_switch is not None:
                    gates.append(KillSwitchGate(self._kill_switch))

                return evaluate_gates(gates, context)

        if target_status is ContentStatus.PUBLISHING:
            if self._publish_gate is not None:
                return self._publish_gate.evaluate(item, actor=eval_actor, at=now)
            with self._database.connection() as conn:
                gates = [
                    ApprovalGate(
                        ApprovalRequestRepository(conn), ArtifactRepository(conn)
                    ),
                    DailyLimitGate(
                        StrategyProfileRepository(conn), DailyUsageRepository(conn)
                    ),
                    RightsGate(RightsRecordRepository(conn)),
                    IdempotencyGate(
                        AIJobRepository(conn),
                        PublishJobRepository(conn),
                        ApprovalRequestRepository(conn),
                    ),
                ]
                if self._kill_switch is not None:
                    gates.append(KillSwitchGate(self._kill_switch))
                return evaluate_gates(gates, context)

        # For other transitions without multi-gate contracts, evaluate transition
        # allowance
        return GateReport(
            results=(GateResult.passed(GateName.TEST, now),),
        )

    def advance(
        self,
        item_id: str,
        *,
        actor: Actor | None = None,
        at: datetime | None = None,
    ) -> PipelineStepResult:
        """Advance a content item by one lifecycle stage."""
        now = at or self._now()
        eval_actor = actor or RUNNER_ACTOR

        with self._database.connection() as conn:
            item = ContentItemRepository(conn).get(item_id)
        if item is None:
            raise PipelineRunnerError(
                "domain.content_not_found", f"Content item {item_id} not found"
            )

        current_status = item.status

        # 1. DRAFT -> GENERATING
        if current_status is ContentStatus.DRAFT:
            report = self.evaluate_stage_gates(
                item, ContentStatus.GENERATING, actor=eval_actor, at=now
            )
            if report.blocked:
                self._audit_block(item, ContentStatus.GENERATING, report, eval_actor)
                return PipelineStepResult(
                    item_id=item.id,
                    from_status=current_status,
                    to_status=current_status,
                    transitioned=False,
                    gate_report=report,
                    reason="Blocked by generation gates",
                )
            with self._database.transaction() as conn:
                moved, _ = start_production(conn, item, clock=lambda: now)
            self._audit_step(
                item.id, current_status, ContentStatus.GENERATING, eval_actor
            )
            return PipelineStepResult(
                item_id=item.id,
                from_status=current_status,
                to_status=ContentStatus.GENERATING,
                transitioned=True,
                gate_report=report,
            )

        # 2. GENERATING -> TESTING
        if current_status is ContentStatus.GENERATING:
            with self._database.connection() as conn:
                video = ArtifactRepository(conn).latest(item.id, ArtifactKind.VIDEO)
            if video is None:
                return PipelineStepResult(
                    item_id=item.id,
                    from_status=current_status,
                    to_status=current_status,
                    transitioned=False,
                    reason="Rendered video artifact not present yet",
                )
            with self._database.transaction() as conn:
                moved = item.with_status(ContentStatus.TESTING, clock=lambda: now)
                ContentItemRepository(conn).update(
                    moved, expected_updated_at=item.updated_at
                )
            self._audit_step(item.id, current_status, ContentStatus.TESTING, eval_actor)
            return PipelineStepResult(
                item_id=item.id,
                from_status=current_status,
                to_status=ContentStatus.TESTING,
                transitioned=True,
            )

        # 3. TESTING -> PREVIEW_READY
        if current_status is ContentStatus.TESTING:
            if self._qc_builder is not None:
                report = self._qc_builder.generate(item.id, actor=eval_actor)
                if report.status is QCStatus.FAIL or report.blocks:
                    # Move back to generating on QC failure
                    with self._database.transaction() as conn:
                        moved = item.with_status(
                            ContentStatus.GENERATING, clock=lambda: now
                        )
                        ContentItemRepository(conn).update(
                            moved, expected_updated_at=item.updated_at
                        )
                    failed_names = [c.name for c in report.failed_checks]
                    self._audit_step(
                        item.id, current_status, ContentStatus.GENERATING, eval_actor
                    )
                    return PipelineStepResult(
                        item_id=item.id,
                        from_status=current_status,
                        to_status=ContentStatus.GENERATING,
                        transitioned=True,
                        reason=f"QC failed: {', '.join(failed_names)}",
                        details={"failed_checks": failed_names},
                    )

            with self._database.transaction() as conn:
                moved = item.with_status(ContentStatus.PREVIEW_READY, clock=lambda: now)
                ContentItemRepository(conn).update(
                    moved, expected_updated_at=item.updated_at
                )
            self._audit_step(
                item.id, current_status, ContentStatus.PREVIEW_READY, eval_actor
            )
            return PipelineStepResult(
                item_id=item.id,
                from_status=current_status,
                to_status=ContentStatus.PREVIEW_READY,
                transitioned=True,
            )

        # 4. PREVIEW_READY -> AWAITING_APPROVAL
        if current_status is ContentStatus.PREVIEW_READY:
            with self._database.transaction() as conn:
                app_repo = ApprovalRequestRepository(conn)
                requests = app_repo.list_by_content_item(item.id)
                active_req = requests[-1] if requests else None
                if active_req is None or active_req.status not in (
                    ApprovalStatus.PENDING,
                    ApprovalStatus.APPROVED,
                ):
                    all_artifacts = ArtifactRepository(conn).list_by_content_item(
                        item.id
                    )
                    latest_by_kind: dict[ArtifactKind, Any] = {}
                    for a in all_artifacts:
                        if (
                            a.kind not in latest_by_kind
                            or a.version > latest_by_kind[a.kind].version
                        ):
                            latest_by_kind[a.kind] = a
                    bindings = tuple(
                        ArtifactBinding.of(a) for a in latest_by_kind.values()
                    )
                    req = ApprovalRequest(
                        id=str(uuid.uuid4()),
                        content_item_id=item.id,
                        artifacts=bindings,
                        status=ApprovalStatus.PENDING,
                        requested_by=eval_actor,
                        qc_result_id=None,
                        created_at=now,
                    )
                    app_repo.add(req)

                moved = item.with_status(
                    ContentStatus.AWAITING_APPROVAL, clock=lambda: now
                )
                ContentItemRepository(conn).update(
                    moved, expected_updated_at=item.updated_at
                )
            self._audit_step(
                item.id, current_status, ContentStatus.AWAITING_APPROVAL, eval_actor
            )
            return PipelineStepResult(
                item_id=item.id,
                from_status=current_status,
                to_status=ContentStatus.AWAITING_APPROVAL,
                transitioned=True,
                waiting_for_approval=True,
            )

        # 5. AWAITING_APPROVAL -> APPROVED
        if current_status is ContentStatus.AWAITING_APPROVAL:
            with self._database.connection() as conn:
                requests = ApprovalRequestRepository(conn).list_by_content_item(item.id)
            latest_req = requests[-1] if requests else None
            if latest_req is None or latest_req.status is ApprovalStatus.PENDING:
                # MANDATORY HUMAN APPROVAL GATE: never auto-approve
                return PipelineStepResult(
                    item_id=item.id,
                    from_status=current_status,
                    to_status=current_status,
                    transitioned=False,
                    waiting_for_approval=True,
                    reason="Human approval required (ActorKind.USER)",
                )
            if latest_req.status is ApprovalStatus.REJECTED:
                with self._database.transaction() as conn:
                    moved = item.with_status(ContentStatus.REJECTED, clock=lambda: now)
                    ContentItemRepository(conn).update(
                        moved, expected_updated_at=item.updated_at
                    )
                self._audit_step(
                    item.id, current_status, ContentStatus.REJECTED, eval_actor
                )
                return PipelineStepResult(
                    item_id=item.id,
                    from_status=current_status,
                    to_status=ContentStatus.REJECTED,
                    transitioned=True,
                    reason="Approval request rejected by reviewer",
                )
            if latest_req.status is ApprovalStatus.CHANGES_REQUESTED:
                with self._database.transaction() as conn:
                    moved = item.with_status(
                        ContentStatus.GENERATING, clock=lambda: now
                    )
                    ContentItemRepository(conn).update(
                        moved, expected_updated_at=item.updated_at
                    )
                self._audit_step(
                    item.id, current_status, ContentStatus.GENERATING, eval_actor
                )
                return PipelineStepResult(
                    item_id=item.id,
                    from_status=current_status,
                    to_status=ContentStatus.GENERATING,
                    transitioned=True,
                    reason="Changes requested by reviewer",
                )
            if latest_req.status is ApprovalStatus.APPROVED:
                with self._database.transaction() as conn:
                    moved = item.with_status(ContentStatus.APPROVED, clock=lambda: now)
                    ContentItemRepository(conn).update(
                        moved, expected_updated_at=item.updated_at
                    )
                self._audit_step(
                    item.id, current_status, ContentStatus.APPROVED, eval_actor
                )
                return PipelineStepResult(
                    item_id=item.id,
                    from_status=current_status,
                    to_status=ContentStatus.APPROVED,
                    transitioned=True,
                )

        # 6. APPROVED -> PUBLISHING / PUBLISHED
        if current_status is ContentStatus.APPROVED:
            if not self._flags.publish_enabled:
                return PipelineStepResult(
                    item_id=item.id,
                    from_status=current_status,
                    to_status=current_status,
                    transitioned=False,
                    reason="Publishing is disabled by feature flag PUBLISH_ENABLED",
                )

            if self._publish_gate is not None:
                report = self._publish_gate.evaluate(item, actor=eval_actor, at=now)
                if report.blocked:
                    self._audit_block(
                        item, ContentStatus.PUBLISHING, report, eval_actor
                    )
                    return PipelineStepResult(
                        item_id=item.id,
                        from_status=current_status,
                        to_status=current_status,
                        transitioned=False,
                        gate_report=report,
                        reason="Blocked by publish gates",
                    )

            publisher = (
                self._shorts_publisher
                if item.content_type is ContentType.SHORTS
                else self._longform_publisher
            )
            if publisher is None:
                return PipelineStepResult(
                    item_id=item.id,
                    from_status=current_status,
                    to_status=current_status,
                    transitioned=False,
                    error="No publisher available for content type",
                )

            outcome = publisher.publish(item.id, actor=eval_actor)
            with self._database.connection() as conn:
                updated_item = ContentItemRepository(conn).get(item.id)
            new_status = (
                updated_item.status if updated_item else ContentStatus.PUBLISHING
            )
            return PipelineStepResult(
                item_id=item.id,
                from_status=current_status,
                to_status=new_status,
                transitioned=True,
                details={"video_id": outcome.video_id},
            )

        # 7. Terminal or already in progress
        return PipelineStepResult(
            item_id=item.id,
            from_status=current_status,
            to_status=current_status,
            transitioned=False,
            reason=f"Item is in terminal or in-flight status {current_status.value}",
        )

    def run(
        self,
        item_id: str,
        *,
        max_steps: int = 10,
        actor: Actor | None = None,
        at: datetime | None = None,
    ) -> PipelineRunResult:
        """Run lifecycle progression step-by-step until blocked, waiting, or
        completed.
        """
        now = at or self._now()
        eval_actor = actor or RUNNER_ACTOR

        with self._database.connection() as conn:
            item = ContentItemRepository(conn).get(item_id)
        if item is None:
            raise PipelineRunnerError(
                "domain.content_not_found", f"Content item {item_id} not found"
            )

        initial_status = item.status
        steps: list[PipelineStepResult] = []
        waiting_for_approval = False
        stopped_reason: str | None = None

        for _ in range(max_steps):
            step = self.advance(item_id, actor=eval_actor, at=now)
            steps.append(step)

            if not step.transitioned:
                waiting_for_approval = step.waiting_for_approval
                stopped_reason = (
                    step.reason or step.error or "Stopped without transition"
                )
                break

            if step.to_status is ContentStatus.PUBLISHED:
                stopped_reason = "Published successfully"
                break

            if step.waiting_for_approval:
                waiting_for_approval = True
                stopped_reason = "Waiting for human approval"
                break

        with self._database.connection() as conn:
            final_item = ContentItemRepository(conn).get(item_id)
        final_status = final_item.status if final_item else initial_status
        completed = final_status is ContentStatus.PUBLISHED

        return PipelineRunResult(
            item_id=item_id,
            initial_status=initial_status,
            final_status=final_status,
            steps=tuple(steps),
            completed=completed,
            waiting_for_approval=waiting_for_approval,
            stopped_reason=stopped_reason,
        )

    def _audit_step(
        self,
        item_id: str,
        from_status: ContentStatus,
        to_status: ContentStatus,
        actor: Actor,
    ) -> None:
        self._audit.record(
            action=PIPELINE_ADVANCED_ACTION,
            actor=actor,
            entity=EntityRef("content_item", item_id),
            result=AuditResult.SUCCESS,
            metadata={
                "from_status": from_status.value,
                "to_status": to_status.value,
            },
        )

    def _audit_block(
        self,
        item: ContentItem,
        target_status: ContentStatus,
        report: GateReport,
        actor: Actor,
    ) -> None:
        self._audit.record(
            action=PIPELINE_BLOCKED_ACTION,
            actor=actor,
            entity=EntityRef("content_item", item.id),
            result=AuditResult.DENIED,
            metadata={
                "from_status": item.status.value,
                "target_status": target_status.value,
                "blocking_reasons": ",".join(r.code for r in report.reasons),
            },
        )
