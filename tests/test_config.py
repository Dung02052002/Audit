import os
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import SecretStr, ValidationError

from ai_youtube_agent.content.strategy import CURRENCY_PATTERN
from ai_youtube_agent.core.config import (
    DEFAULT_ARTIFACT_DIR,
    ENV_PREFIX,
    ENVIRONMENT_VARIABLE,
    Environment,
    RenderProviderKind,
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


# Artifact folder (H-089)

ARTIFACT_DIR_VARIABLE = f"{ENV_PREFIX}ARTIFACT_DIR"


def test_artifact_dir_defaults_to_data_artifacts(tmp_path: Path) -> None:
    settings = load_settings(tmp_path)

    assert settings.artifact_dir == DEFAULT_ARTIFACT_DIR == Path("data/artifacts")
    assert not (tmp_path / "data").exists()


def test_artifact_dir_reads_the_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ARTIFACT_DIR_VARIABLE, str(tmp_path / "files"))

    assert load_settings(tmp_path).artifact_dir == tmp_path / "files"


def test_artifact_dir_reads_an_env_file(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text(f"{ARTIFACT_DIR_VARIABLE}=media/out\n")

    assert load_settings(tmp_path).artifact_dir == Path("media/out")


def test_artifact_dir_keeps_the_database_path(tmp_path: Path) -> None:
    settings = Settings(artifact_dir=tmp_path / "files")

    assert settings.database_path == Path("data/ai_youtube_agent.db")
    assert settings.artifact_dir == tmp_path / "files"


def test_env_example_documents_the_artifact_dir(tmp_path: Path) -> None:
    example = (REPO_ROOT / ".env.example").read_text()
    line = f"# {ARTIFACT_DIR_VARIABLE}=data/artifacts"
    assert line in example

    # With the commented line switched on, the example file still parses.
    (tmp_path / ".env").write_text(example.replace(line, line[2:]))
    assert load_settings(tmp_path).artifact_dir == Path("data/artifacts")


# TTS price and cost currency (H-092)

TTS_PRICE_VARIABLE = f"{ENV_PREFIX}TTS_PRICE_PER_1K_CHARACTERS"
TTS_CURRENCY_VARIABLE = f"{ENV_PREFIX}TTS_COST_CURRENCY"


def test_tts_price_and_currency_have_defaults(tmp_path: Path) -> None:
    settings = load_settings(tmp_path)

    assert settings.tts_price_per_1k_characters == Decimal("0")
    assert isinstance(settings.tts_price_per_1k_characters, Decimal)
    assert settings.tts_cost_currency == "USD"


def test_tts_price_and_currency_read_the_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(TTS_PRICE_VARIABLE, "0.015")
    monkeypatch.setenv(TTS_CURRENCY_VARIABLE, "EUR")

    settings = load_settings(tmp_path)

    assert settings.tts_price_per_1k_characters == Decimal("0.015")
    assert settings.tts_cost_currency == "EUR"


def test_tts_price_and_currency_read_an_env_file(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text(
        f"{TTS_PRICE_VARIABLE}=2\n{TTS_CURRENCY_VARIABLE}=VND\n"
    )

    settings = load_settings(tmp_path)

    assert settings.tts_price_per_1k_characters == Decimal("2")
    assert settings.tts_cost_currency == "VND"


@pytest.mark.parametrize(
    "value",
    [0, 3, "0", "0.000001", "999999.999999", Decimal("0.015")],
)
def test_tts_price_accepts_valid_values(value) -> None:
    settings = Settings(tts_price_per_1k_characters=value)

    assert settings.tts_price_per_1k_characters == Decimal(str(value))


@pytest.mark.parametrize(
    "value",
    [
        -1,
        "-0.01",
        "NaN",
        "Infinity",
        "-Infinity",
        "abc",
        "",
        None,
        True,
        False,
        1.5,
        0.0,
        "0.0000001",
        "1000000.000000",
    ],
)
def test_tts_price_refuses_bad_values(value) -> None:
    with pytest.raises(ValidationError, match="tts_price_per_1k_characters"):
        Settings(tts_price_per_1k_characters=value)


@pytest.mark.parametrize("text", ["-1", "NaN", "Infinity", "abc", "true", "1e400"])
def test_an_invalid_tts_price_variable_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, text: str
) -> None:
    monkeypatch.setenv(TTS_PRICE_VARIABLE, text)

    with pytest.raises(ValidationError, match="tts_price_per_1k_characters"):
        load_settings(tmp_path)


@pytest.mark.parametrize("value", ["usd", "US", "USDD", "", "U1D", "US ", None, 1])
def test_tts_cost_currency_refuses_bad_values(value) -> None:
    with pytest.raises(ValidationError, match="tts_cost_currency"):
        Settings(tts_cost_currency=value)


def test_an_invalid_tts_cost_currency_variable_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(TTS_CURRENCY_VARIABLE, "usd")

    with pytest.raises(ValidationError, match="tts_cost_currency"):
        load_settings(tmp_path)


def test_tts_cost_currency_pattern_equals_the_strategy_currency_pattern() -> None:
    patterns = [
        getattr(item, "pattern", None)
        for item in Settings.model_fields["tts_cost_currency"].metadata
    ]

    assert CURRENCY_PATTERN.pattern in patterns


def test_env_example_documents_the_tts_price_and_currency(tmp_path: Path) -> None:
    example = (REPO_ROOT / ".env.example").read_text()
    price_line = f"# {TTS_PRICE_VARIABLE}=0"
    currency_line = f"# {TTS_CURRENCY_VARIABLE}=USD"
    assert price_line in example
    assert currency_line in example

    # With the commented lines switched on, the example file still parses.
    switched = example.replace(price_line, price_line[2:]).replace(
        currency_line, currency_line[2:]
    )
    (tmp_path / ".env").write_text(switched)
    settings = load_settings(tmp_path)
    assert settings.tts_price_per_1k_characters == Decimal("0")
    assert settings.tts_cost_currency == "USD"


# Video renderer (I-100)

RENDER_PROVIDER_VARIABLE = f"{ENV_PREFIX}RENDER_PROVIDER"
FFMPEG_BINARY_VARIABLE = f"{ENV_PREFIX}FFMPEG_BINARY"
RENDER_TIMEOUT_VARIABLE = f"{ENV_PREFIX}RENDER_TIMEOUT_SECONDS"
RENDER_FONT_VARIABLE = f"{ENV_PREFIX}RENDER_FONT_FILE"


def test_render_settings_have_defaults(tmp_path: Path) -> None:
    settings = load_settings(tmp_path)

    assert settings.render_provider is RenderProviderKind.MOCK
    assert settings.ffmpeg_binary == "ffmpeg"
    assert settings.render_timeout_seconds == 600
    assert settings.render_font_file is None


def test_render_settings_read_the_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(RENDER_PROVIDER_VARIABLE, "ffmpeg")
    monkeypatch.setenv(FFMPEG_BINARY_VARIABLE, "/opt/ffmpeg/bin/ffmpeg")
    monkeypatch.setenv(RENDER_TIMEOUT_VARIABLE, "120")
    monkeypatch.setenv(RENDER_FONT_VARIABLE, "fonts/captions.ttf")

    settings = load_settings(tmp_path)

    assert settings.render_provider is RenderProviderKind.FFMPEG
    assert settings.ffmpeg_binary == "/opt/ffmpeg/bin/ffmpeg"
    assert settings.render_timeout_seconds == 120
    assert settings.render_font_file == Path("fonts/captions.ttf")


def test_render_provider_reads_an_env_file(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text(f"{RENDER_PROVIDER_VARIABLE}=ffmpeg\n")

    assert load_settings(tmp_path).render_provider is RenderProviderKind.FFMPEG


@pytest.mark.parametrize("value", ["blender", "", "MOCK", "ffmpeg ", None, 1])
def test_render_provider_refuses_unknown_kinds(value) -> None:
    with pytest.raises(ValidationError, match="render_provider"):
        Settings(render_provider=value)


@pytest.mark.parametrize("value", ["", " ", "   ", None])
def test_ffmpeg_binary_refuses_blank_values(value) -> None:
    with pytest.raises(ValidationError, match="ffmpeg_binary"):
        Settings(ffmpeg_binary=value)


@pytest.mark.parametrize("value", [1, 600, 7200, "30"])
def test_render_timeout_accepts_whole_seconds(value) -> None:
    assert Settings(render_timeout_seconds=value).render_timeout_seconds == int(value)


@pytest.mark.parametrize("value", [0, -1, 7201, True, False, 1.5, "abc", None])
def test_render_timeout_refuses_bad_values(value) -> None:
    with pytest.raises(ValidationError, match="render_timeout_seconds"):
        Settings(render_timeout_seconds=value)


def test_env_example_documents_the_render_settings(tmp_path: Path) -> None:
    example = (REPO_ROOT / ".env.example").read_text()
    lines = [
        f"# {RENDER_PROVIDER_VARIABLE}=mock",
        f"# {FFMPEG_BINARY_VARIABLE}=ffmpeg",
        f"# {RENDER_TIMEOUT_VARIABLE}=600",
        f"# {RENDER_FONT_VARIABLE}=fonts/captions.ttf",
    ]
    for line in lines:
        assert line in example

    # With the commented lines switched on, the example file still parses.
    for line in lines:
        example = example.replace(line, line[2:])
    (tmp_path / ".env").write_text(example)
    settings = load_settings(tmp_path)
    assert settings.render_provider is RenderProviderKind.MOCK
    assert settings.ffmpeg_binary == "ffmpeg"
    assert settings.render_timeout_seconds == 600
    assert settings.render_font_file == Path("fonts/captions.ttf")
