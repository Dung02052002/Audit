"""Tests for L-131..L-135 preview services and `/previews` HTTP API endpoints."""

from datetime import UTC, datetime
from pathlib import Path

from fastapi.testclient import TestClient

from ai_youtube_agent.bootstrap import build_container, prepare_database
from ai_youtube_agent.content.preview_player import srt_to_vtt
from ai_youtube_agent.content.qc import QCCheck, QCResult, QCStatus
from ai_youtube_agent.content.qc_report import ALL_QC_CHECK_NAMES
from ai_youtube_agent.content.shorts_metadata import ShortsMetadata
from ai_youtube_agent.content.shorts_thumbnail import generate_thumbnail_png
from ai_youtube_agent.core.artifact import Artifact, ArtifactKind
from ai_youtube_agent.core.artifact_storage import ArtifactStorage
from ai_youtube_agent.core.config import Environment, Settings
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
from ai_youtube_agent.main import create_app
from ai_youtube_agent.providers.mock_video_render import stub_mp4

NOW = datetime(2026, 10, 19, 12, 0, tzinfo=UTC)


def _store_artifact(
    storage: ArtifactStorage,
    content_item_id: str,
    kind: ArtifactKind,
    data: bytes,
    media_type: str,
) -> Artifact:
    stored = storage.write(
        content_item_id, kind, 1, data, media_type=media_type
    )
    return Artifact.create(
        content_item_id,
        kind,
        uri=stored.uri,
        sha256=stored.sha256,
        size_bytes=stored.size_bytes,
        media_type=media_type,
        clock=lambda: NOW,
    )


def _seed_shorts_preview_item(
    db: Database, storage: ArtifactStorage
) -> tuple[ContentItem, dict[ArtifactKind, str]]:
    from ai_youtube_agent.content.channel import Channel
    from ai_youtube_agent.content.strategy import (
        Cadence,
        ContentPillar,
        FormatMix,
        LanguageTargets,
        StrategyProfile,
        ToneRules,
    )

    ch = Channel.create(
        title="Audit Tech Shorts",
        youtube_channel_id="UC" + "A" * 22,
        handle="@audittech",
        clock=lambda: NOW,
    )
    sp = StrategyProfile.create(
        channel_id=ch.id,
        niche="AI engineering",
        audience="Developers",
        pillars=(ContentPillar("Systems", "Architecture"),),
        tone=ToneRules(style="Analytical", allowed=("facts",), forbidden=("hype",)),
        cadence=Cadence(videos_per_week=3),
        format_mix=FormatMix(shorts_percent=100, longform_percent=0),
        languages=LanguageTargets(primary="en"),
        clock=lambda: NOW,
    )
    item = ContentItem.create(
        ch.id, sp.id, sp.version, ContentType.SHORTS, "Why Determinism Matters", clock=lambda: NOW
    ).with_status(ContentStatus.GENERATING, clock=lambda: NOW).with_status(
        ContentStatus.TESTING, clock=lambda: NOW
    ).with_status(
        ContentStatus.PREVIEW_READY, clock=lambda: NOW
    )

    video_bytes = stub_mp4(b"shorts-v1-seed", duration_ms=15_000, fps=30)
    video_art = _store_artifact(
        storage, item.id, ArtifactKind.VIDEO, video_bytes, "video/mp4"
    )

    srt_bytes = (
        "1\n00:00:00,000 --> 00:00:05,000\nDeterminism prevents silent drift.\n\n"
        "2\n00:00:05,000 --> 00:00:15,000\nEvery artifact is versioned.\n"
    ).encode("utf-8")
    sub_art = _store_artifact(
        storage, item.id, ArtifactKind.SUBTITLES, srt_bytes, "application/x-subrip"
    )

    thumb_bytes = generate_thumbnail_png("DETERMINISM", 1080, 1920)
    thumb_art = _store_artifact(
        storage, item.id, ArtifactKind.THUMBNAIL, thumb_bytes, "image/png"
    )

    meta = ShortsMetadata(
        content_item_id=item.id,
        project_id="proj-1",
        project_version=1,
        script_id="script-1",
        script_version=1,
        title="Why Determinism Matters #Shorts",
        description="Deterministic pipelines prevent silent state drift.\n\n#Shorts #AI",
        tags=("shorts", "ai", "determinism"),
        category_id="28",
        language="en",
        made_for_kids=False,
        contains_synthetic_media=True,
        disclosure_decision_id="disc-1",
        policy_status="pass",
        created_at=NOW,
    )
    meta_art = _store_artifact(
        storage,
        item.id,
        ArtifactKind.METADATA,
        meta.to_json_bytes(),
        "application/json",
    )

    checks = tuple(QCCheck(name, QCStatus.PASS, "ok") for name in ALL_QC_CHECK_NAMES)
    qc_res = QCResult.create(
        item.id,
        (video_art, sub_art, thumb_art, meta_art),
        checks,
        clock=lambda: NOW,
    )

    with db.transaction() as conn:
        ChannelRepository(conn).add(ch)
        StrategyProfileRepository(conn).add(sp)
        ContentItemRepository(conn).add(item)
        artifacts_repo = ArtifactRepository(conn)
        for art in (video_art, sub_art, thumb_art, meta_art):
            artifacts_repo.add(art)
        QCResultRepository(conn).add(qc_res)

    return item, {
        ArtifactKind.VIDEO: video_art.id,
        ArtifactKind.SUBTITLES: sub_art.id,
        ArtifactKind.THUMBNAIL: thumb_art.id,
        ArtifactKind.METADATA: meta_art.id,
    }


def test_srt_to_vtt_converts_subrip_timestamps() -> None:
    srt = "1\n00:00:00,000 --> 00:00:02,500\nHello world\n"
    vtt = srt_to_vtt(srt)
    assert vtt.startswith("WEBVTT\n\n")
    assert "00:00:00.000 --> 00:00:02.500" in vtt


def test_preview_endpoints_expose_snapshot_player_metadata_reports_and_compare(
    tmp_path: Path,
) -> None:
    settings = Settings(
        environment=Environment.TEST,
        database_path=tmp_path / "agent.db",
        artifact_dir=tmp_path / "artifacts",
    )
    prepare_database(settings)
    container = build_container(settings)
    db = container.resolve(Database)
    storage = container.resolve(ArtifactStorage)
    item, art_ids = _seed_shorts_preview_item(db, storage)

    client = TestClient(create_app(container))

    # L-131: Snapshot & byte-range streaming
    snap_res = client.get(f"/previews/{item.id}")
    assert snap_res.status_code == 200
    snap = snap_res.json()
    assert snap["content_item_id"] == item.id
    assert snap["video"]["artifact_id"] == art_ids[ArtifactKind.VIDEO]
    assert snap["video"]["version"] == 1

    range_res = client.get(
        f"/previews/{item.id}/artifacts/{art_ids[ArtifactKind.VIDEO]}/stream",
        headers={"Range": "bytes=0-15"},
    )
    assert range_res.status_code == 206
    assert len(range_res.content) == 16
    assert range_res.headers["Accept-Ranges"] == "bytes"
    assert range_res.headers["Content-Range"].startswith("bytes 0-15/")

    # L-132: Player config, HTML, and WebVTT subtitles
    player_res = client.get(f"/previews/{item.id}/player")
    assert player_res.status_code == 200
    player = player_res.json()
    assert player["aspect_ratio"] == "9:16"
    assert player["duration_ms"] == 15_000

    html_res = client.get(f"/previews/{item.id}/player.html")
    assert html_res.status_code == 200
    assert "preview-video-player" in html_res.text

    vtt_res = client.get(f"/previews/{item.id}/subtitles.vtt")
    assert vtt_res.status_code == 200
    assert vtt_res.text.startswith("WEBVTT")

    # L-133: Preview metadata
    meta_res = client.get(f"/previews/{item.id}/metadata")
    assert meta_res.status_code == 200
    meta_body = meta_res.json()
    assert meta_body["title"] == "Why Determinism Matters #Shorts"
    assert len(meta_body["subtitles"]["cues"]) == 2

    # L-134: Preview reports
    rep_res = client.get(f"/previews/{item.id}/reports")
    assert rep_res.status_code == 200
    rep_body = rep_res.json()
    assert rep_body["qc"]["status"] == "pass"

    # L-135: Version comparison (v1 has no previous version)
    cmp_res = client.get(f"/previews/{item.id}/compare")
    assert cmp_res.status_code == 200
    cmp_body = cmp_res.json()
    assert cmp_body["has_previous_version"] is False
