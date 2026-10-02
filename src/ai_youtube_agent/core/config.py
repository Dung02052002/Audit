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

import os
from enum import StrEnum
from functools import lru_cache
from pathlib import Path

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

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
