"""Production Readiness Audit Service (Prompt Pack v8, prompt T-238), context C16.

Generates the final production readiness report (``READY`` vs ``NOT_READY``) with
explicit blockers, operational/technical risks, and a deterministic rollback plan.
Fails closed to ``NOT_READY`` whenever mandatory safety flags are weakened,
security audit findings exist, the kill switch is active, or automated verification
evidence (``verification_passed``) has not been confirmed.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Final

from ai_youtube_agent import __version__
from ai_youtube_agent.core.audit import (
    Actor,
    ActorKind,
    AuditLog,
    AuditResult,
    EntityRef,
)
from ai_youtube_agent.core.config import Settings
from ai_youtube_agent.core.health import HealthRegistry, HealthStatus
from ai_youtube_agent.core.kill_switch_gate import KillSwitchSource
from ai_youtube_agent.core.security_audit import SecurityAuditor

Clock = Callable[[], datetime]

PRODUCTION_AUDIT_ACTION: Final[str] = "production.readiness_audited"
AUDITOR_ACTOR: Final[Actor] = Actor(ActorKind.SYSTEM, "production_readiness_auditor")


class ReadinessVerdict(StrEnum):
    READY = "READY"
    NOT_READY = "NOT_READY"


@dataclass(frozen=True)
class ReadinessCheck:
    """Single check evaluated during the production readiness audit."""

    name: str
    passed: bool
    summary: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "passed": self.passed,
            "summary": self.summary,
        }


@dataclass(frozen=True)
class ReadinessBlocker:
    """Hard blocker preventing a READY verdict."""

    code: str
    title: str
    remediation: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "title": self.title,
            "remediation": self.remediation,
        }


@dataclass(frozen=True)
class ReadinessRisk:
    """Documented operational or technical risk and its mitigation."""

    id: str
    severity: str
    description: str
    mitigation: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "severity": self.severity,
            "description": self.description,
            "mitigation": self.mitigation,
        }


@dataclass(frozen=True)
class RollbackStep:
    """Ordered step in the production rollback runbook."""

    order: int
    action: str
    verification: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "order": self.order,
            "action": self.action,
            "verification": self.verification,
        }


DEFAULT_ROLLBACK_STEPS: Final[tuple[RollbackStep, ...]] = (
    RollbackStep(
        order=1,
        action=(
            "Activate KillSwitchService emergency stop (cancelling all queued AI jobs) "
            "and set PUBLISH_ENABLED=false, LONGFORM_ENABLED=false, "
            "AUTO_REPLY_ENABLED=false."
        ),
        verification=(
            "Verify KillSwitchService.is_active is True and JobScheduler/PublishGate "
            "block new actions."
        ),
    ),
    RollbackStep(
        order=2,
        action=(
            "Trigger GracefulShutdownService to drain in-flight operations and persist "
            "job checkpoints."
        ),
        verification=(
            "Verify all running workers exit cleanly and SQLite WAL checkpoints are "
            "flushed."
        ),
    ),
    RollbackStep(
        order=3,
        action=(
            "Restore the pre-release BackupManager snapshot (SQLite database + "
            "artifact storage) and run RestoreVerifier."
        ),
        verification=(
            "Confirm RestoreVerificationReport.passed is True with 100% SHA-256 "
            "artifact integrity."
        ),
    ),
    RollbackStep(
        order=4,
        action=(
            "Redeploy the previous known-good application tag with mock/read-only "
            "providers enabled."
        ),
        verification=(
            "Confirm /health and /ready endpoints report HEALTHY before resuming user "
            "sessions."
        ),
    ),
)

DEFAULT_OPERATIONAL_RISKS: Final[tuple[ReadinessRisk, ...]] = (
    ReadinessRisk(
        id="RISK-01",
        severity="HIGH",
        description=(
            "Unverified code paths from Build-First implementation if deployed before "
            "full automated test execution."
        ),
        mitigation=(
            "Run full pytest, ruff, mypy, and migration verification suites and "
            "resolve any failures before enabling production flags."
        ),
    ),
    ReadinessRisk(
        id="RISK-02",
        severity="MEDIUM",
        description=(
            "External YouTube API quota exhaustion or transient OAuth token expiration "
            "during publishing."
        ),
        mitigation=(
            "Enforce DailyLimitGate, TokenRecovery single-refresh retry, "
            "UploadRetryPolicy bounded backoff, and PublishDuplicateGuard."
        ),
    ),
    ReadinessRisk(
        id="RISK-03",
        severity="MEDIUM",
        description="Unexpected LLM/TTS/render spend spikes across active channels.",
        mitigation=(
            "Keep BudgetGuard and BudgetGate active on every channel StrategyProfile "
            "with 50%/80%/100% alert thresholds."
        ),
    ),
)


@dataclass(frozen=True)
class ProductionReadinessReport:
    """Final readiness report produced by T-238 ProductionReadinessAuditor."""

    verdict: ReadinessVerdict
    checks: tuple[ReadinessCheck, ...]
    blockers: tuple[ReadinessBlocker, ...]
    risks: tuple[ReadinessRisk, ...]
    rollback_plan: tuple[RollbackStep, ...]
    generated_at: datetime

    @property
    def is_ready(self) -> bool:
        return self.verdict is ReadinessVerdict.READY

    def as_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict.value,
            "is_ready": self.is_ready,
            "checks": [c.as_dict() for c in self.checks],
            "blockers": [b.as_dict() for b in self.blockers],
            "risks": [r.as_dict() for r in self.risks],
            "rollback_plan": [s.as_dict() for s in self.rollback_plan],
            "generated_at": self.generated_at.isoformat(),
        }

    def to_markdown(self) -> str:
        lines = [
            "# T-238 Production Readiness Audit Report",
            "",
            f"- **Verdict:** `{self.verdict.value}`",
            f"- **Generated At:** `{self.generated_at.isoformat()}`",
            "",
            "## 1. Readiness Checks",
            "",
            "| Check | Status | Summary |",
            "|---|---|---|",
        ]
        for check in self.checks:
            status = "PASS" if check.passed else "FAIL"
            lines.append(f"| `{check.name}` | `{status}` | {check.summary} |")

        lines.extend(["", "## 2. Blockers", ""])
        if not self.blockers:
            lines.append("- None. All readiness gates passed.")
        else:
            for b in self.blockers:
                lines.append(f"- **`{b.code}` — {b.title}**: {b.remediation}")

        lines.extend(
            [
                "",
                "## 3. Operational & Technical Risks",
                "",
                "| ID | Severity | Description | Mitigation |",
                "|---|---|---|---|",
            ]
        )
        for r in self.risks:
            lines.append(
                f"| `{r.id}` | `{r.severity}` | {r.description} | {r.mitigation} |"
            )

        lines.extend(
            [
                "",
                "## 4. Rollback Plan",
                "",
                "| Step | Action | Verification |",
                "|---|---|---|",
            ]
        )
        for step in self.rollback_plan:
            lines.append(f"| {step.order} | {step.action} | {step.verification} |")
        return "\n".join(lines) + "\n"


class ProductionReadinessAuditor:
    """Evaluates system safety, security, health, and verification evidence (T-238)."""

    def __init__(
        self,
        settings: Settings,
        *,
        audit_log: AuditLog | None = None,
        security_auditor: SecurityAuditor | None = None,
        health_registry: HealthRegistry | None = None,
        kill_switch: KillSwitchSource | None = None,
        clock: Clock | None = None,
    ) -> None:
        self._settings = settings
        self._audit = audit_log
        self._security = security_auditor or SecurityAuditor()
        self._health = health_registry
        self._kill_switch = kill_switch
        self._clock = clock

    def _now(self) -> datetime:
        return self._clock() if self._clock else datetime.now(UTC)

    def evaluate(
        self,
        *,
        verification_passed: bool = False,
        extra_blockers: Sequence[ReadinessBlocker] = (),
        extra_risks: Sequence[ReadinessRisk] = (),
    ) -> ProductionReadinessReport:
        """Runs all readiness checks and returns a ``ProductionReadinessReport``.

        By default ``verification_passed=False`` so Build-First runs honestly report
        ``NOT_READY`` until the automated test suite has been executed and verified.
        """
        now = self._now()
        checks: list[ReadinessCheck] = []
        blockers: list[ReadinessBlocker] = list(extra_blockers)

        # 1. Mandatory safety flags (R-08: approval_required and test_required)
        flags = self._settings.flags
        safety_ok = bool(flags.approval_required and flags.test_required)
        checks.append(
            ReadinessCheck(
                "safety_flags_enforced",
                safety_ok,
                "approval_required=True and test_required=True enforced"
                if safety_ok
                else (
                    "Mandatory safety flags (approval_required / test_required) "
                    "are disabled"
                ),
            )
        )
        if not safety_ok:
            blockers.append(
                ReadinessBlocker(
                    code="readiness.safety_flags_disabled",
                    title="Mandatory safety gates disabled",
                    remediation=(
                        "Enable FeatureFlags.approval_required=True and "
                        "FeatureFlags.test_required=True (R-08)."
                    ),
                )
            )

        # 2. Security audit (S-223)
        sec_report = self._security.run_audit(flags=self._settings.flags)
        checks.append(
            ReadinessCheck(
                "security_audit",
                sec_report.passed,
                f"Security audit passed ({len(sec_report.findings)} checks)"
                if sec_report.passed
                else (
                    f"Security audit failed ({len(sec_report.critical_failures)} "
                    "critical)"
                ),
            )
        )
        if not sec_report.passed:
            blockers.append(
                ReadinessBlocker(
                    code="readiness.security_audit_failed",
                    title="High or critical security findings detected",
                    remediation=(
                        "Resolve all CRITICAL findings reported by SecurityAuditor."
                    ),
                )
            )

        # 3. Kill switch inactive
        ks_active = bool(self._kill_switch and self._kill_switch.current().active)
        checks.append(
            ReadinessCheck(
                "kill_switch_inactive",
                not ks_active,
                "Emergency stop is inactive"
                if not ks_active
                else "Emergency stop (kill switch) is active",
            )
        )
        if ks_active:
            blockers.append(
                ReadinessBlocker(
                    code="readiness.kill_switch_active",
                    title="Emergency stop is active",
                    remediation=(
                        "Investigate the emergency stop reason and deactivate "
                        "KillSwitchService before release."
                    ),
                )
            )

        # 4. System & provider health
        if self._health is not None:
            health_report = self._health.run(__version__)
            health_ok = health_report.status is HealthStatus.OK
            checks.append(
                ReadinessCheck(
                    "system_health",
                    health_ok,
                    f"HealthRegistry status is {health_report.status.value}",
                )
            )
            if not health_ok:
                blockers.append(
                    ReadinessBlocker(
                        code="readiness.health_degraded",
                        title="System or provider health check failed",
                        remediation=(
                            "Restore all failing HealthRegistry checks to "
                            "HealthStatus.OK."
                        ),
                    )
                )

        # 5. Automated test & verification evidence
        checks.append(
            ReadinessCheck(
                "automated_verification_suite",
                verification_passed,
                (
                    "Full automated test, lint, type-check, and migration suites "
                    "executed and passed"
                )
                if verification_passed
                else (
                    "Automated test execution deferred in Build-First Mode "
                    "(H-089..T-238 IMPLEMENTED_UNTESTED)"
                ),
            )
        )
        if not verification_passed:
            blockers.append(
                ReadinessBlocker(
                    code="readiness.automated_verification_pending",
                    title="Automated test & verification suite not yet executed",
                    remediation=(
                        "Execute pytest, ruff, mypy, and migration verification on "
                        "tasks H-089..T-238 and confirm 100% pass rate."
                    ),
                )
            )

        verdict = (
            ReadinessVerdict.READY if len(blockers) == 0 else ReadinessVerdict.NOT_READY
        )
        risks = (*DEFAULT_OPERATIONAL_RISKS, *extra_risks)
        report = ProductionReadinessReport(
            verdict=verdict,
            checks=tuple(checks),
            blockers=tuple(blockers),
            risks=risks,
            rollback_plan=DEFAULT_ROLLBACK_STEPS,
            generated_at=now,
        )

        if self._audit is not None:
            self._audit.record(
                action=PRODUCTION_AUDIT_ACTION,
                actor=AUDITOR_ACTOR,
                entity=EntityRef("system", "production_readiness"),
                result=(
                    AuditResult.SUCCESS
                    if verdict is ReadinessVerdict.READY
                    else AuditResult.DENIED
                ),
                metadata={
                    "verdict": verdict.value,
                    "blocker_count": len(blockers),
                    "blockers": ",".join(b.code for b in blockers),
                },
            )
        return report
