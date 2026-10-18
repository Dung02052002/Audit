import logging
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path
from typing import Annotated

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ai_youtube_agent import __version__
from ai_youtube_agent.bootstrap import build_container
from ai_youtube_agent.content.audio_artifact import AudioArtifactRecorder
from ai_youtube_agent.content.audio_cost import AudioCostRecorder
from ai_youtube_agent.content.audio_validation import AudioValidator
from ai_youtube_agent.content.shorts_audio_mix import (
    ArtifactAssetSource,
    ShortsAudioMixer,
)
from ai_youtube_agent.content.shorts_render import (
    ArtifactVisualSource,
    ShortsRenderService,
)
from ai_youtube_agent.content.shorts_service import ShortsProjectService
from ai_youtube_agent.content.shorts_subtitles import ShortsSubtitleRecorder
from ai_youtube_agent.core.artifact_storage import ArtifactStorage, LocalFileStorage
from ai_youtube_agent.core.config import Environment, RenderProviderKind, Settings
from ai_youtube_agent.core.di import Container
from ai_youtube_agent.core.flags import FeatureFlags
from ai_youtube_agent.core.log import Severity
from ai_youtube_agent.main import create_app, provide
from ai_youtube_agent.providers.ffmpeg_video_render import FfmpegVideoRenderer
from ai_youtube_agent.providers.mock_video_render import MockVideoRenderer
from ai_youtube_agent.providers.video_render import VideoRenderer


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


def test_health_keeps_status_and_version(settings: Settings) -> None:
    client = TestClient(create_app(build_container(settings)))

    response = client.get("/health")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["version"] == __version__


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


def test_container_resolves_the_artifact_storage_and_audio_recorder(
    tmp_path: Path,
) -> None:
    root = tmp_path / "artifacts"
    settings = Settings(
        environment=Environment.TEST,
        database_path=tmp_path / "agent.db",
        artifact_dir=root,
    )
    container = build_container(settings)

    storage = container.resolve(ArtifactStorage)
    recorder = container.resolve(AudioArtifactRecorder)

    assert isinstance(storage, LocalFileStorage)
    assert isinstance(storage, ArtifactStorage)
    assert storage.root == root.resolve()
    assert isinstance(recorder, AudioArtifactRecorder)
    assert container.resolve(ArtifactStorage) is storage
    assert not root.exists()
    assert not (tmp_path / "agent.db").exists()


def test_container_resolves_the_audio_validator_and_cost_recorder(
    tmp_path: Path,
) -> None:
    settings = Settings(
        environment=Environment.TEST,
        database_path=tmp_path / "agent.db",
        artifact_dir=tmp_path / "artifacts",
        tts_price_per_1k_characters=Decimal("0.015"),
        tts_cost_currency="EUR",
    )
    container = build_container(settings)

    validator = container.resolve(AudioValidator)
    cost_recorder = container.resolve(AudioCostRecorder)

    assert isinstance(validator, AudioValidator)
    assert isinstance(cost_recorder, AudioCostRecorder)
    assert cost_recorder._price_per_1k == Decimal("0.015")
    assert cost_recorder._currency == "EUR"
    assert container.resolve(AudioValidator) is validator
    assert not (tmp_path / "agent.db").exists()


def test_container_resolves_the_shorts_project_service(tmp_path: Path) -> None:
    settings = Settings(
        environment=Environment.TEST, database_path=tmp_path / "agent.db"
    )
    container = build_container(settings)

    service = container.resolve(ShortsProjectService)

    assert isinstance(service, ShortsProjectService)
    assert container.resolve(ShortsProjectService) is service
    assert not (tmp_path / "agent.db").exists()


def test_container_resolves_the_shorts_subtitle_recorder(tmp_path: Path) -> None:
    settings = Settings(
        environment=Environment.TEST,
        database_path=tmp_path / "agent.db",
        artifact_dir=tmp_path / "artifacts",
    )
    container = build_container(settings)

    recorder = container.resolve(ShortsSubtitleRecorder)

    assert isinstance(recorder, ShortsSubtitleRecorder)
    assert container.resolve(ShortsSubtitleRecorder) is recorder
    assert not (tmp_path / "agent.db").exists()
    assert not (tmp_path / "artifacts").exists()


def test_container_resolves_the_shorts_audio_mixer(tmp_path: Path) -> None:
    settings = Settings(
        environment=Environment.TEST,
        database_path=tmp_path / "agent.db",
        artifact_dir=tmp_path / "artifacts",
    )
    container = build_container(settings)

    mixer = container.resolve(ShortsAudioMixer)

    assert isinstance(mixer, ShortsAudioMixer)
    assert container.resolve(ShortsAudioMixer) is mixer
    assert isinstance(mixer._assets, ArtifactAssetSource)
    assert not (tmp_path / "agent.db").exists()
    assert not (tmp_path / "artifacts").exists()


def test_container_resolves_the_mock_renderer_and_the_render_service(
    tmp_path: Path,
) -> None:
    settings = Settings(
        environment=Environment.TEST,
        database_path=tmp_path / "agent.db",
        artifact_dir=tmp_path / "artifacts",
    )
    container = build_container(settings)

    service = container.resolve(ShortsRenderService)

    assert isinstance(service, ShortsRenderService)
    assert container.resolve(ShortsRenderService) is service
    renderer = container.resolve(VideoRenderer)
    assert isinstance(renderer, MockVideoRenderer)
    assert service._renderer is renderer
    assert service._mixer is container.resolve(ShortsAudioMixer)
    assert isinstance(service._visuals, ArtifactVisualSource)
    assert not (tmp_path / "agent.db").exists()
    assert not (tmp_path / "artifacts").exists()


def test_container_builds_the_ffmpeg_renderer_from_the_settings(
    tmp_path: Path,
) -> None:
    settings = Settings(
        environment=Environment.TEST,
        database_path=tmp_path / "agent.db",
        render_provider=RenderProviderKind.FFMPEG,
        ffmpeg_binary="/opt/ffmpeg",
        render_timeout_seconds=90,
        render_font_file=tmp_path / "captions.ttf",
    )

    renderer = build_container(settings).resolve(VideoRenderer)

    assert isinstance(renderer, FfmpegVideoRenderer)
    assert renderer.name == "ffmpeg"
    assert renderer._binary == "/opt/ffmpeg"
    assert renderer._timeout_s == 90
    assert renderer._font_file == tmp_path / "captions.ttf"
