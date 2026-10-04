import os
from pathlib import Path

import pytest
from pydantic import SecretStr, ValidationError

from ai_youtube_agent.core.config import (
    ENV_PREFIX,
    ENVIRONMENT_VARIABLE,
    Environment,
    RightsBlockLevel,
    Settings,
    VoiceProviderKind,
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


# Rights block levels (G-078)

RIGHTS_VARIABLE = f"{ENV_PREFIX}RIGHTS_BLOCK_LEVELS"
HIGH, MEDIUM = RightsBlockLevel.HIGH, RightsBlockLevel.MEDIUM


def test_rights_block_levels_default_to_high(tmp_path: Path) -> None:
    levels = load_settings(tmp_path).rights_block_levels

    assert levels == frozenset({HIGH})
    assert isinstance(levels, frozenset)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("high", {HIGH}),
        ("medium,high", {MEDIUM, HIGH}),
        ("high,medium", {MEDIUM, HIGH}),
        (" medium , high ", {MEDIUM, HIGH}),
        ("high,high", {HIGH}),
        ('["high"]', {HIGH}),
        ('["medium", "high"]', {MEDIUM, HIGH}),
        ('  ["high","medium","high"] ', {MEDIUM, HIGH}),
        ('[" medium ", " high "]', {MEDIUM, HIGH}),
    ],
)
def test_rights_block_levels_read_a_comma_list_or_a_json_list_from_the_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, text: str, expected: set
) -> None:
    monkeypatch.setenv(RIGHTS_VARIABLE, text)

    assert load_settings(tmp_path).rights_block_levels == frozenset(expected)


def test_rights_block_levels_read_an_env_file(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text(f"{RIGHTS_VARIABLE}=medium,high\n")

    assert load_settings(tmp_path).rights_block_levels == {MEDIUM, HIGH}


@pytest.mark.parametrize(
    "value",
    [
        ["medium", "high"],
        ("high", "medium"),
        {"high", "medium"},
        frozenset({MEDIUM, HIGH}),
        [MEDIUM, HIGH],
        "medium,high",
        '["medium","high"]',
    ],
)
def test_rights_block_levels_accept_a_constructor_value(value) -> None:
    assert Settings(rights_block_levels=value).rights_block_levels == {MEDIUM, HIGH}


@pytest.mark.parametrize(
    "value",
    [
        "medium",
        "",
        "   ",
        ",",
        "[]",
        '["medium"]',
        [],
        ["medium"],
        frozenset(),
    ],
    ids=repr,
)
def test_rights_block_levels_must_contain_high(value) -> None:
    with pytest.raises(ValidationError, match="rights_block_levels") as caught:
        Settings(rights_block_levels=value)

    assert "contain" in str(caught.value) or "valid" in str(caught.value)


@pytest.mark.parametrize(
    "value",
    [
        "unknown,high",
        "low,high",
        "critical",
        "high,",
        "high medium",
        "HIGH",
        "high;medium",
        '["low", "high"]',
        '["unknown"]',
        "[1, 2]",
        "[not json",
        '{"high": true}',
        '"high"',
        "[",
        ["high", 3],
        ["high", None],
        None,
        7,
    ],
)
def test_rights_block_levels_refuse_unknown_low_and_garbage(value) -> None:
    with pytest.raises(ValidationError, match="rights_block_levels"):
        Settings(rights_block_levels=value)


def test_an_invalid_rights_block_levels_variable_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(RIGHTS_VARIABLE, "medium")

    with pytest.raises(ValidationError, match="rights_block_levels"):
        load_settings(tmp_path)


def test_the_block_level_enum_has_no_unknown_or_low() -> None:
    assert [level.value for level in RightsBlockLevel] == ["medium", "high"]


def test_env_example_documents_the_rights_block_levels(tmp_path: Path) -> None:
    example = (REPO_ROOT / ".env.example").read_text()
    assert f"# {RIGHTS_VARIABLE}=high" in example

    # With the commented line switched on, the example file still parses.
    (tmp_path / ".env").write_text(
        example.replace(f"# {RIGHTS_VARIABLE}=high", f"{RIGHTS_VARIABLE}=high")
    )
    assert load_settings(tmp_path).rights_block_levels == {HIGH}


# Policy rule set version (G-084)

POLICY_VERSION_VARIABLE = f"{ENV_PREFIX}POLICY_RULE_SET_VERSION"


def test_policy_rule_set_version_defaults_to_one(tmp_path: Path) -> None:
    assert load_settings(tmp_path).policy_rule_set_version == 1


def test_policy_rule_set_version_reads_the_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(POLICY_VERSION_VARIABLE, "3")

    assert load_settings(tmp_path).policy_rule_set_version == 3


def test_policy_rule_set_version_reads_an_env_file(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text(f"{POLICY_VERSION_VARIABLE}=2\n")

    assert load_settings(tmp_path).policy_rule_set_version == 2


@pytest.mark.parametrize("value", [0, -1, "x", "", True, False, None, 1.5])
def test_policy_rule_set_version_refuses_bad_values(value) -> None:
    with pytest.raises(ValidationError, match="policy_rule_set_version"):
        Settings(policy_rule_set_version=value)


@pytest.mark.parametrize("text", ["0", "-1", "x", "true"])
def test_an_invalid_policy_rule_set_version_variable_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, text: str
) -> None:
    monkeypatch.setenv(POLICY_VERSION_VARIABLE, text)

    with pytest.raises(ValidationError, match="policy_rule_set_version"):
        load_settings(tmp_path)


def test_env_example_documents_the_policy_rule_set_version(tmp_path: Path) -> None:
    example = (REPO_ROOT / ".env.example").read_text()
    line = f"# {POLICY_VERSION_VARIABLE}=1"
    assert line in example
    assert "(#084)" not in example

    # With the commented line switched on, the example file still parses.
    (tmp_path / ".env").write_text(example.replace(line, line[2:]))
    assert load_settings(tmp_path).policy_rule_set_version == 1


# Voice provider (H-086)

VOICE_PROVIDER_VARIABLE = f"{ENV_PREFIX}VOICE_PROVIDER"


def test_voice_provider_defaults_to_the_mock(tmp_path: Path) -> None:
    assert load_settings(tmp_path).voice_provider is VoiceProviderKind.MOCK


def test_voice_provider_reads_the_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(VOICE_PROVIDER_VARIABLE, "mock")

    assert load_settings(tmp_path).voice_provider is VoiceProviderKind.MOCK


def test_voice_provider_reads_an_env_file(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text(f"{VOICE_PROVIDER_VARIABLE}=mock\n")

    assert load_settings(tmp_path).voice_provider is VoiceProviderKind.MOCK


@pytest.mark.parametrize("value", ["elevenlabs", "", "MOCK ", None, 1])
def test_voice_provider_refuses_unknown_kinds(value) -> None:
    with pytest.raises(ValidationError, match="voice_provider"):
        Settings(voice_provider=value)


def test_an_unknown_voice_provider_variable_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(VOICE_PROVIDER_VARIABLE, "elevenlabs")

    with pytest.raises(ValidationError, match="voice_provider"):
        load_settings(tmp_path)


def test_env_example_documents_the_voice_provider(tmp_path: Path) -> None:
    example = (REPO_ROOT / ".env.example").read_text()
    line = f"# {VOICE_PROVIDER_VARIABLE}=mock"
    assert line in example

    # With the commented line switched on, the example file still parses.
    (tmp_path / ".env").write_text(example.replace(line, line[2:]))
    assert load_settings(tmp_path).voice_provider is VoiceProviderKind.MOCK
