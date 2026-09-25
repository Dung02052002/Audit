import os
from pathlib import Path

import pytest
from pydantic import SecretStr, ValidationError

from ai_youtube_agent.core.config import (
    ENV_PREFIX,
    ENVIRONMENT_VARIABLE,
    Environment,
    Settings,
    get_settings,
    load_settings,
)
from ai_youtube_agent.core.flags import FeatureFlags

REPO_ROOT = Path(__file__).resolve().parents[1]
SECRET_MARKERS = ("secret", "token", "password", "key", "credential")


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in list(os.environ):
        if name.startswith(ENV_PREFIX):
            monkeypatch.delenv(name)
    get_settings.cache_clear()


def test_defaults_without_env_files(tmp_path: Path) -> None:
    settings = load_settings(tmp_path)

    assert settings.environment is Environment.DEVELOPMENT
    assert settings.app_name == "ai_youtube_agent"
    assert settings.debug is False


def test_environment_selects_its_env_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / ".env").write_text("AI_YOUTUBE_AGENT_APP_NAME=base\n")
    (tmp_path / ".env.test").write_text("AI_YOUTUBE_AGENT_APP_NAME=from-test\n")
    (tmp_path / ".env.production").write_text("AI_YOUTUBE_AGENT_APP_NAME=from-prod\n")
    monkeypatch.setenv(ENVIRONMENT_VARIABLE, "test")

    settings = load_settings(tmp_path)

    assert settings.environment is Environment.TEST
    assert settings.app_name == "from-test"


def test_base_env_file_applies_when_no_override(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("AI_YOUTUBE_AGENT_DEBUG=true\n")

    assert load_settings(tmp_path).debug is True


def test_process_env_overrides_env_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / ".env").write_text("AI_YOUTUBE_AGENT_APP_NAME=from-file\n")
    monkeypatch.setenv(f"{ENV_PREFIX}APP_NAME", "from-process")

    assert load_settings(tmp_path).app_name == "from-process"


def test_invalid_environment_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ENVIRONMENT_VARIABLE, "staging")

    with pytest.raises(ValueError):
        load_settings(tmp_path)


def test_environment_in_env_file_is_rejected(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("AI_YOUTUBE_AGENT_ENVIRONMENT=production\n")

    with pytest.raises(ValueError, match="process environment"):
        load_settings(tmp_path)


def test_invalid_value_type_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(f"{ENV_PREFIX}DEBUG", "not-a-bool")

    with pytest.raises(ValidationError):
        load_settings(tmp_path)


def test_empty_app_name_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(f"{ENV_PREFIX}APP_NAME", "")

    with pytest.raises(ValidationError):
        load_settings(tmp_path)


def test_debug_is_forbidden_in_production(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ENVIRONMENT_VARIABLE, "production")
    monkeypatch.setenv(f"{ENV_PREFIX}DEBUG", "true")

    with pytest.raises(ValidationError, match="debug must be false in production"):
        load_settings(tmp_path)


def test_settings_are_immutable(tmp_path: Path) -> None:
    settings = load_settings(tmp_path)

    with pytest.raises(ValidationError):
        settings.debug = True


def test_get_settings_is_cached() -> None:
    assert get_settings() is get_settings()


def test_secret_fields_are_secretstr_without_default() -> None:
    for name, field in Settings.model_fields.items():
        if any(marker in name for marker in SECRET_MARKERS):
            assert field.annotation is SecretStr, name
            assert field.is_required(), name


def test_env_files_are_ignored_but_example_is_not() -> None:
    lines = (REPO_ROOT / ".gitignore").read_text().splitlines()

    assert ".env" in lines
    assert ".env.*" in lines
    assert "!.env.example" in lines


def test_env_example_lists_only_known_settings() -> None:
    example = (REPO_ROOT / ".env.example").read_text().splitlines()
    keys = {
        line.split("=", 1)[0].strip()
        for line in example
        if line.strip() and not line.lstrip().startswith("#")
    }
    known = {f"{ENV_PREFIX}{name.upper()}" for name in Settings.model_fields}
    known |= {
        f"{ENV_PREFIX}FLAGS__{name.upper()}" for name in FeatureFlags.model_fields
    }

    assert keys <= known
