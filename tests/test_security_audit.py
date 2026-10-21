"""Tests for S-223 Security Audit."""

from __future__ import annotations

from pathlib import Path

import pytest

from ai_youtube_agent.core.flags import FeatureFlags
from ai_youtube_agent.core.security_audit import (
    SecurityAuditor,
    SecurityAuditError,
)


def test_security_auditor_passes_on_repository_and_default_flags() -> None:
    auditor = SecurityAuditor(Path.cwd())
    report = auditor.run_audit()
    assert report.passed is True
    assert report.critical_failures == ()
    report.raise_if_failed()


def test_security_auditor_detects_unsafe_feature_flags() -> None:
    auditor = SecurityAuditor(Path.cwd())
    unsafe_flags = FeatureFlags(
        publish_enabled=True,
        longform_enabled=True,
        auto_reply_enabled=True,
    )
    report = auditor.run_audit(flags=unsafe_flags)
    assert report.passed is False
    assert len(report.critical_failures) == 1
    assert report.critical_failures[0].check_id == "security.safe_defaults"
    with pytest.raises(SecurityAuditError):
        report.raise_if_failed()
