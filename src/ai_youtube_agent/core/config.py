"""Typed configuration contract.

Settings come from three sources. A later source overrides an earlier one:

1. ``.env``: shared local defaults.
2. ``.env.<environment>``: overrides for one environment.
3. Process environment variables.

Every variable uses the ``AI_YOUTUBE_AGENT_`` prefix. The environment itself
(``AI_YOUTUBE_AGENT_ENVIRONMENT``) must come from the process environment,
because it decides which ``.env.<environment>`` file is read.

Secrets never live in source control. A secret field must be typed as
``SecretStr`` and must have no default value.
"""

import json
import os
from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Any

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from ai_youtube_agent.core.flags import FeatureFlags
from ai_youtube_agent.core.log import Severity

ENV_PREFIX = "AI_YOUTUBE_AGENT_"
ENV_NESTED_DELIMITER = "__"
ENVIRONMENT_VARIABLE = f"{ENV_PREFIX}ENVIRONMENT"
# SQLite database file (#029), relative to the working directory by default.
DEFAULT_DATABASE_PATH = Path("data/ai_youtube_agent.db")


class Environment(StrEnum):
    DEVELOPMENT = "development"
    TEST = "test"
    PRODUCTION = "production"


class ResearchProviderKind(StrEnum):
    """Which research provider bootstrap registers (#054). Only the mock exists."""

    MOCK = "mock"


class TextProviderKind(StrEnum):
    """Which text (LLM) provider bootstrap registers (#065). Only the mock exists."""

    MOCK = "mock"


class RightsBlockLevel(StrEnum):
    """A rights risk level the rights gate may block on (#078).

    ``high`` always blocks and ``unknown`` always blocks, so only the optional
    ``medium`` is a choice. This enum has no import from ``content``.
    """

    MEDIUM = "medium"
    HIGH = "high"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix=ENV_PREFIX,
        env_nested_delimiter=ENV_NESTED_DELIMITER,
        env_file_encoding="utf-8",
        extra="ignore",
        frozen=True,
    )

    environment: Environment = Environment.DEVELOPMENT
    app_name: str = Field(default="ai_youtube_agent", min_length=1)
    debug: bool = False
    log_level: Severity = Severity.INFO
    database_path: Path = DEFAULT_DATABASE_PATH
    flags: FeatureFlags = Field(default_factory=FeatureFlags)
    research_provider: ResearchProviderKind = ResearchProviderKind.MOCK
    text_provider: TextProviderKind = TextProviderKind.MOCK
    # Rights levels that block a publish (#078): a comma separated list
    # ("medium,high") or a JSON list (["medium", "high"]). ``high`` is required.
    rights_block_levels: Annotated[frozenset[RightsBlockLevel], NoDecode] = frozenset(
        {RightsBlockLevel.HIGH}
    )
    # Version of the policy rule set the publish gate checks with (G-084). An
    # integer >= 1 (not a bool); a version the catalog does not know fails when
    # the publish gate is built, with no fallback to another version.
    policy_rule_set_version: int = Field(default=1, ge=1)

    @field_validator("rights_block_levels", mode="before")
    @classmethod
    def _parse_rights_block_levels(cls, value: Any) -> Any:
        if not isinstance(value, str):
            return value
        text = value.strip()
        if text.startswith("["):
            try:
                parsed = json.loads(text)
            except ValueError:
                raise ValueError(
                    "rights_block_levels is not a valid JSON list"
                ) from None
            if not isinstance(parsed, list):
                raise ValueError("rights_block_levels must be a list")
            return [item.strip() if isinstance(item, str) else item for item in parsed]
        return [part.strip() for part in text.split(",")] if text else []

    @field_validator("rights_block_levels", mode="after")
    @classmethod
    def _require_high_rights_block_level(
        cls, value: frozenset[RightsBlockLevel]
    ) -> frozenset[RightsBlockLevel]:
        if RightsBlockLevel.HIGH not in value:
            raise ValueError("rights_block_levels must contain high")
        return value

    @field_validator("policy_rule_set_version", mode="before")
    @classmethod
    def _refuse_bool_policy_rule_set_version(cls, value: Any) -> Any:
        if isinstance(value, bool):
            raise ValueError("policy_rule_set_version must be an integer, not a bool")
        return value

    @model_validator(mode="after")
    def _forbid_debug_in_production(self) -> "Settings":
        if self.environment is Environment.PRODUCTION and self.debug:
            raise ValueError("debug must be false in production")
        return self

    @model_validator(mode="after")
    def _require_control_stages_in_production(self) -> "Settings":
        if self.environment is Environment.PRODUCTION and not (
            self.flags.test_required and self.flags.approval_required
        ):
            raise ValueError(
                "test_required and approval_required must be true in production"
            )
        return self


def env_files_for(environment: Environment, directory: Path) -> tuple[Path, ...]:
    """Return the dotenv files for an environment, lowest priority first."""
    return (directory / ".env", directory / f".env.{environment.value}")


def load_settings(env_dir: Path | None = None) -> Settings:
    """Load and validate settings for the environment named by the process."""
    environment = Environment(
        os.environ.get(ENVIRONMENT_VARIABLE, Environment.DEVELOPMENT.value)
    )
    settings = Settings(_env_file=env_files_for(environment, env_dir or Path.cwd()))
    if settings.environment is not environment:
        raise ValueError(
            f"{ENVIRONMENT_VARIABLE} must be set in the process environment, "
            "not in a .env file"
        )
    return settings


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide settings, loaded once."""
    return load_settings()
