"""Tests for Video Detail screen view and service (Prompt Pack v8, prompt Q-195)."""

from datetime import UTC, datetime
from pathlib import Path

import pytest

from ai_youtube_agent.content.channel import ChannelStatus
from ai_youtube_agent.content.dashboard_shell import DashboardError, DashboardErrorCode
from ai_youtube_agent.content.dashboard_video_detail import VideoDetailService
from ai_youtube_agent.content.qc import QCStatus
from ai_youtube_agent.core.artifact import Artifact, ArtifactKind
from ai_youtube_agent.core.artifact_storage import ArtifactStorage, LocalFileStorage
from ai_youtube_agent.core.config import Settings
from ai_youtube_agent.core.content_item import ContentItem, ContentStatus, ContentType
from ai_youtube_agent.core.db.database import Database
from ai_youtube_agent.core.db.repositories.channel import (
    ChannelRepository,
    StrategyProfileRepository,
)
from ai_youtube_agent.core.db.repositories.content import (
    ArtifactRepository,
    ContentItemRepository,
)
from ai_youtube_agent.core.db.repositories.review import QCResultRepository
from ai_youtube_agent.providers.mock_video_render import stub_mp4
from factories import (
    make_channel,
    make_content_item,
    make_qc_result,
    make_strategy_profile,
)

FIXED_NOW = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)


@pytest.fixture
def env(
    database: Database, tmp_path: Path
) -> tuple[Database, ArtifactStorage, Settings]:
    storage = LocalFileStorage(tmp_path / "artifacts")
    settings = Settings(
        database_path=tmp_path / "test.db",
        artifact_dir=tmp_path / "artifacts",
    )
    return database, storage, settings


def _store_first(
    storage: ArtifactStorage,
    content_item_id: str,
    kind: ArtifactKind,
    data: bytes,
    media_type: str,
) -> Artifact:
    """Write version 1 of an artifact to storage and build its ``Artifact``."""
    stored = storage.write(content_item_id, kind, 1, data, media_type=media_type)
    return Artifact.create(
        content_item_id,
        kind,
        uri=stored.uri,
        sha256=stored.sha256,
        size_bytes=stored.size_bytes,
        media_type=media_type,
        clock=lambda: FIXED_NOW,
    )


def _seed_channel_and_item(
    db: Database,
    *,
    content_type: ContentType = ContentType.SHORTS,
    status: ContentStatus = ContentStatus.PREVIEW_READY,
) -> ContentItem:
    channel = make_channel(title="Detail Channel", status=ChannelStatus.ACTIVE)
    profile = make_strategy_profile(channel)
    item = make_content_item(
        channel,
        profile,
        content_type=content_type,
        title="Quantum Computing in 45 Seconds",
        status=status,
    )
    with db.transaction() as conn:
        ChannelRepository(conn).add(channel)
        StrategyProfileRepository(conn).add(profile)
        ContentItemRepository(conn).add(item)
    return item


def test_video_detail_missing_item_raises_not_found(
    env: tuple[Database, ArtifactStorage, Settings],
) -> None:
    db, storage, settings = env
    service = VideoDetailService(db, storage, settings, clock=lambda: FIXED_NOW)
    with pytest.raises(DashboardError) as exc_info:
        service.load("missing-item-id")
    assert exc_info.value.dashboard_code is DashboardErrorCode.CONTENT_NOT_FOUND


def test_video_detail_draft_without_video_artifact(
    env: tuple[Database, ArtifactStorage, Settings],
) -> None:
    db, storage, settings = env
    item = _seed_channel_and_item(db, status=ContentStatus.DRAFT)
    service = VideoDetailService(db, storage, settings, clock=lambda: FIXED_NOW)

    view = service.load(item.id)
    assert view.content_item_id == item.id
    assert view.preview_snapshot is None
    assert view.player_config is None
    assert view.preview_unavailable_reason is not None
    html_out = service.render_html(view)
    assert 'data-testid="video-preview-unavailable"' in html_out


def test_video_detail_with_real_preview_player_artifacts_and_reports(
    env: tuple[Database, ArtifactStorage, Settings],
) -> None:
    db, storage, settings = env
    item = _seed_channel_and_item(db, status=ContentStatus.PREVIEW_READY)

    mp4_bytes = stub_mp4(45_000, b"quantum-video-payload")
    video_art = _store_first(
        storage, item.id, ArtifactKind.VIDEO, mp4_bytes, "video/mp4"
    )
    srt_bytes = b"1\n00:00:00,000 --> 00:00:02,000\nWelcome to quantum computing.\n"
    subs_art = _store_first(
        storage, item.id, ArtifactKind.SUBTITLES, srt_bytes, "application/x-subrip"
    )
    qc_res = make_qc_result(item, [video_art, subs_art])

    with db.transaction() as conn:
        ArtifactRepository(conn).add(video_art)
        ArtifactRepository(conn).add(subs_art)
        QCResultRepository(conn).add(qc_res)

    service = VideoDetailService(db, storage, settings, clock=lambda: FIXED_NOW)
    view = service.load(item.id)

    assert view.preview_snapshot is not None
    assert view.preview_snapshot.video.artifact_id == video_art.id
    assert view.player_config is not None
    assert view.player_config.duration_ms == 45_000
    assert view.player_config.aspect_ratio == "9:16"
    assert len(view.all_artifacts) == 2
    assert view.reports.qc is not None
    assert view.reports.qc.status is QCStatus.WARN
    assert view.version_comparison is not None

    rendered = service.render_html(view)
    assert 'data-testid="video-detail-screen"' in rendered
    assert 'data-testid="preview-player"' in rendered
    assert 'data-testid="video-artifacts-table"' in rendered
    assert video_art.sha256[:12] in rendered
