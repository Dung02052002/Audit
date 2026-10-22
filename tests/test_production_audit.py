"""Tests for T-238 ProductionReadinessAuditor."""

from ai_youtube_agent.core.audit import AuditLog, InMemoryAuditSink
from ai_youtube_agent.core.config import Environment, Settings
from ai_youtube_agent.core.flags import FeatureFlags
from ai_youtube_agent.pipeline.kill_switch_service import KillSwitchService
from ai_youtube_agent.pipeline.production_audit import (
    PRODUCTION_AUDIT_ACTION,
    ProductionReadinessAuditor,
    ReadinessVerdict,
)
from factories import USER, at


def test_production_readiness_auditor_defaults_to_not_ready_without_verification() -> (
    None
):
    """Verifies that ProductionReadinessAuditor honestly reports NOT_READY when
    verification_passed=False (Build-First Mode) and READY only when all safety,
    security, kill-switch, and verification checks pass."""
    sink = InMemoryAuditSink()
    audit = AuditLog(sink)
    settings = Settings(environment=Environment.TEST)
    kill_switch = KillSwitchService(audit, clock=at(1))

    auditor = ProductionReadinessAuditor(
        settings,
        audit_log=audit,
        kill_switch=kill_switch,
        clock=at(1),
    )

    # 1. Default (verification_passed=False) -> NOT_READY
    unverified_report = auditor.evaluate(verification_passed=False)
    assert unverified_report.verdict is ReadinessVerdict.NOT_READY
    assert unverified_report.is_ready is False
    assert any(
        b.code == "readiness.automated_verification_pending"
        for b in unverified_report.blockers
    )
    assert (
        "# T-238 Production Readiness Audit Report" in unverified_report.to_markdown()
    )

    # 2. When verification_passed=True and all safety/security gates pass -> READY
    ready_report = auditor.evaluate(verification_passed=True)
    assert ready_report.verdict is ReadinessVerdict.READY
    assert ready_report.is_ready is True
    assert ready_report.blockers == ()
    assert len(ready_report.rollback_plan) == 4
    assert any(e.action == PRODUCTION_AUDIT_ACTION for e in sink.events())

    # 3. Activating kill switch or disabling approval_required forces NOT_READY
    kill_switch.activate(actor=USER, reason="Incident")
    ks_report = auditor.evaluate(verification_passed=True)
    assert ks_report.verdict is ReadinessVerdict.NOT_READY
    assert any(b.code == "readiness.kill_switch_active" for b in ks_report.blockers)

    unsafe_settings = Settings(
        environment=Environment.TEST,
        flags=FeatureFlags(approval_required=False, test_required=True),
    )
    unsafe_auditor = ProductionReadinessAuditor(unsafe_settings, clock=at(2))
    unsafe_report = unsafe_auditor.evaluate(verification_passed=True)
    assert unsafe_report.verdict is ReadinessVerdict.NOT_READY
    assert any(
        b.code == "readiness.safety_flags_disabled" for b in unsafe_report.blockers
    )
