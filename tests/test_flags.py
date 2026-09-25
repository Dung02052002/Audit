import os
from pathlib import Path

import pytest
from pydantic import ValidationError

from ai_youtube_agent.core.config import (
    ENV_PREFIX,
    ENVIRONMENT_VARIABLE,
    get_settings,
    load_settings,
)
from ai_youtube_agent.core.flags import FeatureFlags

FLAG_PREFIX = f"{ENV_PREFIX}FLAGS__"
REQUIRED_FLAGS = {
    "shorts_enabled",
    "longform_enabled",
    "publish_enabled",
    "test_required",
    "approval_required",
    "auto_reply_enabled",
}


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in list(os.environ):
        if name.startswith(ENV_PREFIX):
            monkeypatch.delenv(name)
    get_settings.cache_clear()


def test_exactly_the_required_flags_exist() -> None:
    assert set(FeatureFlags.model_fields) == REQUIRED_FLAGS


def test_safe_defaults() -> None:
    flags = FeatureFlags()

    assert flags.shorts_enabled is True
    assert flags.longform_enabled is False
    assert flags.publish_enabled is False
    assert flags.test_required is True
    assert flags.approval_required is True
    assert flags.auto_reply_enabled is False


def test_settings_carry_default_flags(tmp_path: Path) -> None:
    assert load_settings(tmp_path).flags == FeatureFlags()


def test_flag_set_from_process_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(f"{FLAG_PREFIX}LONGFORM_ENABLED", "true")

    flags = load_settings(tmp_path).flags

    assert flags.longform_enabled is True
    assert flags.shorts_enabled is True


def test_flag_set_from_env_file(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("AI_YOUTUBE_AGENT_FLAGS__SHORTS_ENABLED=false\n")

    assert load_settings(tmp_path).flags.shorts_enabled is False


def test_invalid_flag_value_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(f"{FLAG_PREFIX}PUBLISH_ENABLED", "maybe")

    with pytest.raises(ValidationError):
        load_settings(tmp_path)


def test_unknown_flag_is_rejected() -> None:
    with pytest.raises(ValidationError):
        FeatureFlags(unknown_enabled=True)


def test_flags_are_immutable() -> None:
    flags = FeatureFlags()

    with pytest.raises(ValidationError):
        flags.publish_enabled = True


@pytest.mark.parametrize("stage", ["test_required", "approval_required"])
def test_publish_requires_control_stages(stage: str) -> None:
    with pytest.raises(ValidationError, match="publish_enabled requires"):
        FeatureFlags(publish_enabled=True, **{stage: False})


def test_publish_allowed_with_control_stages() -> None:
    assert FeatureFlags(publish_enabled=True).publish_enabled is True


@pytest.mark.parametrize("stage", ["TEST_REQUIRED", "APPROVAL_REQUIRED"])
def test_control_stages_required_in_production(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stage: str
) -> None:
    monkeypatch.setenv(ENVIRONMENT_VARIABLE, "production")
    monkeypatch.setenv(f"{FLAG_PREFIX}{stage}", "false")

    with pytest.raises(ValidationError, match="must be true in production"):
        load_settings(tmp_path)


def test_control_stages_can_be_relaxed_outside_production(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(f"{FLAG_PREFIX}TEST_REQUIRED", "false")

    assert load_settings(tmp_path).flags.test_required is False
