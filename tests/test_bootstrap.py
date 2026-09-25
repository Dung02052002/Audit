import logging
from collections.abc import Iterator
from typing import Annotated

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ai_youtube_agent import __version__
from ai_youtube_agent.bootstrap import build_container
from ai_youtube_agent.core.config import Environment, Settings
from ai_youtube_agent.core.di import Container
from ai_youtube_agent.core.flags import FeatureFlags
from ai_youtube_agent.core.log import Severity
from ai_youtube_agent.main import create_app, provide


@pytest.fixture(autouse=True)
def restore_root_logger() -> Iterator[None]:
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    yield
    root.handlers[:] = handlers
    root.setLevel(level)


@pytest.fixture
def settings() -> Settings:
    return Settings(
        environment=Environment.TEST,
        app_name="agent-under-test",
        log_level=Severity.WARNING,
        flags=FeatureFlags(longform_enabled=True),
    )


def test_container_registers_core_interfaces(settings: Settings) -> None:
    container = build_container(settings)

    assert container.resolve(Settings) is settings
    assert container.resolve(FeatureFlags) is settings.flags
    assert container.resolve(FeatureFlags).longform_enabled is True


def test_create_app_uses_the_given_container(settings: Settings) -> None:
    container = build_container(settings)

    app = create_app(container)

    assert app.state.container is container
    assert app.title == "agent-under-test"
    assert logging.getLogger().level == logging.WARNING


def test_health_is_unchanged(settings: Settings) -> None:
    client = TestClient(create_app(build_container(settings)))

    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "version": __version__}


def test_provide_resolves_from_the_app_container(settings: Settings) -> None:
    app = create_app(build_container(settings))

    @app.get("/flags")
    def flags(
        flags: Annotated[FeatureFlags, provide(FeatureFlags)],
    ) -> dict[str, bool]:
        return {"longform_enabled": flags.longform_enabled}

    assert TestClient(app).get("/flags").json() == {"longform_enabled": True}


def test_override_changes_what_the_app_receives(settings: Settings) -> None:
    container = build_container(settings)
    app: FastAPI = create_app(container)

    @app.get("/flags")
    def flags(
        flags: Annotated[FeatureFlags, provide(FeatureFlags)],
    ) -> dict[str, bool]:
        return {"longform_enabled": flags.longform_enabled}

    with container.override(FeatureFlags, lambda _: FeatureFlags()):
        assert TestClient(app).get("/flags").json() == {"longform_enabled": False}


def test_module_app_is_built_with_a_container() -> None:
    """`uvicorn ai_youtube_agent.main:app` still works and carries a container."""
    import ai_youtube_agent.main as main_module

    assert isinstance(main_module.app.state.container, Container)
    assert main_module.app.state.container.is_registered(Settings)
