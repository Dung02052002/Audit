"""Tests for S-216 Secret Boundary enforcement."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import SecretStr

from ai_youtube_agent.core.config import Settings
from ai_youtube_agent.core.secret_boundary import (
    SecretBoundaryError,
    SecretBoundaryValidator,
    scan_text_for_secrets,
    validate_settings_model,
)


def test_repository_secret_boundary_passes() -> None:
    validator = SecretBoundaryValidator(Path.cwd())
    report = validator.audit_repository()
    assert report.passed is True
    assert report.violations == ()
    report.raise_if_violated()


def test_settings_model_rejects_plaintext_secret_field() -> None:
    class BadSettings(Settings):
        youtube_client_secret: str = "plain-secret"

    violations = validate_settings_model(BadSettings)
    codes = {v.rule_code for v in violations}
    assert "secret.field_not_secret_str" in codes
    assert "secret.field_has_default" in codes

    class GoodSettings(Settings):
        youtube_client_secret: SecretStr | None = None

    assert validate_settings_model(GoodSettings) == ()


def test_scan_text_and_env_example_detects_leaked_secrets(tmp_path: Path) -> None:
    fake_google_key = "AIza" + "A" * 35
    fake_oauth_secret = "GOCSPX-" + "B" * 24

    leaked_findings = scan_text_for_secrets(
        f"key = '{fake_google_key}'\nsecret = '{fake_oauth_secret}'\n",
        location="config.py",
    )
    assert len(leaked_findings) == 2
    # Must never echo the secret value itself in the violation message
    for finding in leaked_findings:
        assert fake_google_key not in finding.message
        assert fake_oauth_secret not in finding.message

    (tmp_path / ".gitignore").write_text(".env\n", encoding="utf-8")
    (tmp_path / ".env.example").write_text(
        "AI_YOUTUBE_AGENT_CLIENT_SECRET=real_production_secret_999\n",
        encoding="utf-8",
    )
    validator = SecretBoundaryValidator(tmp_path)
    report = validator.audit_repository()
    assert not report.passed
    with pytest.raises(SecretBoundaryError):
        report.raise_if_violated()
