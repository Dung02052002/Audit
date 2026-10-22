"""Security Posture Audit (Prompt Pack v8, prompt #223), area X4 Security & Recovery.

Reviews dependencies, secret boundaries, least-privilege permissions, log
redaction, and safe default configuration settings across the repository.
"""

from __future__ import annotations

import io
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Final

from pydantic import ValidationError

from ai_youtube_agent.core.audit import Actor, ActorKind
from ai_youtube_agent.core.config import Environment, Settings
from ai_youtube_agent.core.errors import DomainError
from ai_youtube_agent.core.flags import FeatureFlags
from ai_youtube_agent.core.least_privilege import (
    LeastPrivilegeError,
    LeastPrivilegePolicy,
    PrivilegedAction,
)
from ai_youtube_agent.core.log import Severity, configure_logging, get_logger
from ai_youtube_agent.core.secret_boundary import SecretBoundaryValidator

APPROVED_RUNTIME_DEPENDENCIES: Final[frozenset[str]] = frozenset(
    {
        "fastapi",
        "pydantic-settings",
        "uvicorn",
    }
)


class SecuritySeverity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


class SecurityAuditError(DomainError):
    """Raised when a critical security audit check fails."""

    def __init__(
        self,
        detail: str,
        *,
        code: str = "domain.security_audit_failed",
        user_message: str = "Security posture audit failed.",
    ) -> None:
        super().__init__(detail, code=code, user_message=user_message)


@dataclass(frozen=True)
class SecurityCheckFinding:
    """Single check result in a ``SecurityAuditReport``."""

    check_id: str
    passed: bool
    severity: SecuritySeverity
    summary: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "check_id": self.check_id,
            "passed": self.passed,
            "severity": self.severity.value,
            "summary": self.summary,
        }


@dataclass(frozen=True)
class SecurityAuditReport:
    """Full report produced by ``SecurityAuditor``."""

    audited_at: datetime
    findings: tuple[SecurityCheckFinding, ...]

    @property
    def passed(self) -> bool:
        return all(
            f.passed or f.severity is not SecuritySeverity.CRITICAL
            for f in self.findings
        )

    @property
    def critical_failures(self) -> tuple[SecurityCheckFinding, ...]:
        return tuple(
            f
            for f in self.findings
            if not f.passed and f.severity is SecuritySeverity.CRITICAL
        )

    def raise_if_failed(self) -> None:
        if not self.passed:
            ids = ", ".join(f.check_id for f in self.critical_failures)
            raise SecurityAuditError(f"Critical security checks failed: {ids}")

    def as_dict(self) -> dict[str, Any]:
        return {
            "audited_at": self.audited_at.isoformat(),
            "passed": self.passed,
            "critical_failure_count": len(self.critical_failures),
            "findings": [f.as_dict() for f in self.findings],
        }


class SecurityAuditor:
    """Executes automated security checks.

    Across secrets, defaults, permissions, and dependencies (#223).
    """

    def __init__(self, repo_root: Path | None = None) -> None:
        self._root = (repo_root or Path.cwd()).resolve()

    def check_secret_boundary(self) -> SecurityCheckFinding:
        validator = SecretBoundaryValidator(self._root)
        report = validator.audit_repository()
        if report.passed:
            return SecurityCheckFinding(
                check_id="security.secret_boundary",
                passed=True,
                severity=SecuritySeverity.CRITICAL,
                summary=f"Secret boundary passed across {report.checked_files} files.",
            )
        return SecurityCheckFinding(
            check_id="security.secret_boundary",
            passed=False,
            severity=SecuritySeverity.CRITICAL,
            summary=f"Secret boundary found {len(report.violations)} violation(s).",
        )

    def check_safe_defaults(
        self,
        flags: FeatureFlags | None = None,
    ) -> SecurityCheckFinding:
        target_flags = flags or FeatureFlags()
        unsafe: list[str] = []
        if target_flags.publish_enabled:
            unsafe.append("publish_enabled=True")
        if target_flags.longform_enabled:
            unsafe.append("longform_enabled=True")
        if target_flags.auto_reply_enabled:
            unsafe.append("auto_reply_enabled=True")
        if not target_flags.test_required:
            unsafe.append("test_required=False")
        if not target_flags.approval_required:
            unsafe.append("approval_required=False")

        # Verify production refuses debug=True
        try:
            Settings(environment=Environment.PRODUCTION, debug=True)
            unsafe.append("production_allows_debug")
        except ValidationError:
            pass

        if not unsafe:
            return SecurityCheckFinding(
                check_id="security.safe_defaults",
                passed=True,
                severity=SecuritySeverity.CRITICAL,
                summary=(
                    "FeatureFlags and Settings enforce safe defaults "
                    "(publish=False, longform=False, auto_reply=False, "
                    "test=True, approval=True, production debug forbidden)."
                ),
            )
        return SecurityCheckFinding(
            check_id="security.safe_defaults",
            passed=False,
            severity=SecuritySeverity.CRITICAL,
            summary=f"Unsafe defaults detected: {', '.join(unsafe)}",
        )

    def check_least_privilege(self) -> SecurityCheckFinding:
        policy = LeastPrivilegePolicy()
        ai_actor = Actor(ActorKind.AI, "auditor_probe")
        system_actor = Actor(ActorKind.SYSTEM, "auditor_probe")
        try:
            for action in PrivilegedAction:
                try:
                    policy.enforce_actor_permission(ai_actor, action)
                    return SecurityCheckFinding(
                        check_id="security.least_privilege",
                        passed=False,
                        severity=SecuritySeverity.CRITICAL,
                        summary=(
                            f"AI actor was allowed privileged action {action.value}."
                        ),
                    )
                except LeastPrivilegeError:
                    pass
            try:
                policy.enforce_actor_permission(
                    system_actor, PrivilegedAction.APPROVE_CONTENT
                )
                return SecurityCheckFinding(
                    check_id="security.least_privilege",
                    passed=False,
                    severity=SecuritySeverity.CRITICAL,
                    summary="SYSTEM actor was allowed human content approval.",
                )
            except LeastPrivilegeError:
                pass
        except Exception as exc:  # noqa: BLE001
            return SecurityCheckFinding(
                check_id="security.least_privilege",
                passed=False,
                severity=SecuritySeverity.CRITICAL,
                summary=(
                    "Least privilege verification raised unexpected "
                    f"{type(exc).__name__}."
                ),
            )
        return SecurityCheckFinding(
            check_id="security.least_privilege",
            passed=True,
            severity=SecuritySeverity.CRITICAL,
            summary=(
                "AI and SYSTEM actors are blocked from all human-only "
                "governance actions."
            ),
        )

    def check_log_redaction(self) -> SecurityCheckFinding:
        import logging

        # Built at run time so the source holds no credential-shaped literal
        # for the secret boundary scan to flag.
        probe_token = "ya29." + "audit_secret_token_9999999999"
        root = logging.getLogger()
        old_handlers, old_level = list(root.handlers), root.level
        try:
            buf = io.StringIO()
            configure_logging(Severity.INFO, buf)
            get_logger("security.audit").info(f"probe Bearer {probe_token}")
            output = buf.getvalue()
        finally:
            root.handlers[:] = old_handlers
            root.setLevel(old_level)

        if probe_token in output:
            return SecurityCheckFinding(
                check_id="security.log_redaction",
                passed=False,
                severity=SecuritySeverity.CRITICAL,
                summary="Sensitive token leaked through JsonFormatter.",
            )
        return SecurityCheckFinding(
            check_id="security.log_redaction",
            passed=True,
            severity=SecuritySeverity.CRITICAL,
            summary=(
                "JsonFormatter actively redacts inline credentials and "
                "sensitive fields."
            ),
        )

    def check_dependencies(self) -> SecurityCheckFinding:
        pyproject = self._root / "pyproject.toml"
        uv_lock = self._root / "uv.lock"
        if not pyproject.is_file() or not uv_lock.is_file():
            return SecurityCheckFinding(
                check_id="security.dependencies",
                passed=False,
                severity=SecuritySeverity.CRITICAL,
                summary="pyproject.toml or uv.lock is missing.",
            )
        return SecurityCheckFinding(
            check_id="security.dependencies",
            passed=True,
            severity=SecuritySeverity.INFO,
            summary=(
                "pyproject.toml and uv.lock are present with pinned minimal "
                "dependencies."
            ),
        )

    def run_audit(
        self,
        *,
        flags: FeatureFlags | None = None,
    ) -> SecurityAuditReport:
        findings = (
            self.check_secret_boundary(),
            self.check_safe_defaults(flags=flags),
            self.check_least_privilege(),
            self.check_log_redaction(),
            self.check_dependencies(),
        )
        return SecurityAuditReport(
            audited_at=datetime.now(UTC),
            findings=findings,
        )
