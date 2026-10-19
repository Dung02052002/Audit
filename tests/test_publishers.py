"""Tests for PublishMetadataMapper (#148), ShortsPublisher (#149), and LongFormPublisher (#150)."""

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from ai_youtube_agent.content.approval import ApprovalRequest
from ai_youtube_agent.content.channel import ChannelStatus
from ai_youtube_agent.content.disclosure import (
    DisclosureDecision,
    DisclosureRecord,
    DisclosureSources,
    RationaleEntry,
)
from ai_youtube_agent.content.longform_script_generator import (
    LongFormDisabledError,
)
from ai_youtube_agent.content.qc import QCCheck, QCResult, QCStatus
from ai_youtube_agent.core.artifact import Artifact, ArtifactKind
from ai_youtube_agent.core.artifact_storage import LocalFileStorage
from ai_youtube_agent.core.audit import Actor, ActorKind, AuditLog, InMemoryAuditSink
from ai_youtube_agent.core.config import Environment, Settings
from ai_youtube_agent.core.content_item import (
    ContentItem,
    ContentStatus,
    ContentType,
)
from ai_youtube_agent.core.db.database import Database
from ai_youtube_agent.core.db.migrate import migrate
from ai_youtube_agent.core.db.repositories.channel import (
    ChannelRepository,
    StrategyProfileRepository,
)
from ai_youtube_agent.core.db.repositories.content import (
    ArtifactRepository,
    ContentItemRepository,
)
from ai_youtube_agent.core.db.repositories.disclosure import DisclosureRepository
from ai_youtube_agent.core.db.repositories.review import (
    ApprovalRequestRepository,
    QCResultRepository,
)
from ai_youtube_agent.core.flags import FeatureFlags
from ai_youtube_agent.core.publish_gate import PublishGate
from ai_youtube_agent.pipeline.longform_publisher import (
    LONGFORM_PUBLISHED,
    LongFormPublisher,
    LongFormPublishError,
    LongFormPublishErrorCode,
)
from ai_youtube_agent.pipeline.publish import PublishStatus
from ai_youtube_agent.pipeline.publish_metadata_mapper import (
    PublishMetadataErrorCode,
    PublishMetadataMapper,
    PublishMetadataMappingError,
)
from ai_youtube_agent.pipeline.shorts_publisher import (
    SHORTS_PUBLISH_FAILED,
    SHORTS_PUBLISHED,
    ShortsPublisher,
    ShortsPublishError,
    ShortsPublishErrorCode,
)
from ai_youtube_agent.providers.mock_video_render import stub_mp4
from ai_youtube_agent.providers.mock_youtube_channel import (
    MockYouTubeChannelProvider,
)
from ai_youtube_agent.providers.mock_youtube_oauth import MockYouTubeOAuthProvider
from ai_youtube_agent.providers.mock_youtube_upload import (
    MockYouTubeUploadProvider,
)
from ai_youtube_agent.providers.youtube_channel import RemoteChannelInfo
from ai_youtube_agent.providers.youtube_oauth import OAuthScope
from ai_youtube_agent.providers.youtube_upload import (
    UploadErrorCode,
    YouTubePrivacyStatus,
    YouTubeUploadError,
)
from factories import make_channel, make_content_item, make_strategy_profile

T0 = datetime(2026, 10, 19, 12, 0, tzinfo=UTC)
USER = Actor(ActorKind.USER, "owner")
SYSTEM = Actor(ActorKind.SYSTEM, "pipeline")


def _store_artifact(
    storage: LocalFileStorage,
    artifacts_repo: ArtifactRepository,
    content_item_id: str,
    kind: ArtifactKind,
    version: int,
    media_type: str,
    data: bytes,
) -> Artifact:
    stored = storage.write_bytes(
        content_item_id=content_item_id,
        kind=kind,
        version=version,
        media_type=media_type,
        data=data,
    )
    art = Artifact.create(
        content_item_id=content_item_id,
        kind=kind,
        version=version,
        storage_uri=stored.storage_uri,
        media_type=media_type,
        size_bytes=stored.size_bytes,
        sha256=stored.sha256,
        created_by=SYSTEM,
        clock=lambda: T0,
    )
    artifacts_repo.add(art)
    return art


def _seed_approved_item(
    tmp_path: Path,
    *,
    content_type: ContentType,
    duration_ms: int = 30_000,
    metadata_dict: dict | None = None,
) -> tuple[
    Database,
    LocalFileStorage,
    InMemoryAuditSink,
    PublishGate,
    ContentItem,
    ApprovalRequest,
]:
    db_path = tmp_path / "agent.db"
    migrate(db_path)
    db = Database(db_path)
    storage = LocalFileStorage(tmp_path / "artifacts")
    sink = InMemoryAuditSink()
    settings = Settings(
        environment=Environment.TEST,
        database_path=db_path,
        artifact_dir=tmp_path / "artifacts",
    )
    gate = PublishGate(db, settings)

    channel = make_channel(clock=lambda: T0).with_status(
        ChannelStatus.ACTIVE, clock=lambda: T0
    )
    profile = make_strategy_profile(channel_id=channel.id, clock=lambda: T0)
    item = make_content_item(
        channel_id=channel.id,
        strategy_profile_id=profile.id,
        strategy_version=profile.version,
        content_type=content_type,
        title="Clean Educational Science Video",
        status=ContentStatus.APPROVED,
        clock=lambda: T0,
    )

    mp4_bytes = stub_mp4(duration_ms, b"video-payload")
    if metadata_dict is None:
        if content_type is ContentType.SHORTS:
            metadata_dict = {
                "content_item_id": item.id,
                "channel_id": channel.id,
                "title": "Clean Educational Science Video #Shorts",
                "description": "Quick science explainer #Shorts",
                "tags": ["Shorts", "Science"],
                "language": "en",
                "made_for_kids": False,
                "contains_synthetic_media": True,
            }
        else:
            metadata_dict = {
                "content_item_id": item.id,
                "channel_id": channel.id,
                "title": "Clean Educational Science Deep Dive",
                "description": "Deep dive into physics.\n\n00:00 Intro\n02:00 Core Mechanics",
                "tags": ["Science", "Physics"],
                "language": "en",
                "made_for_kids": False,
                "contains_synthetic_media": True,
                "chapters": [
                    {"title": "Intro", "start_ms": 0},
                    {"title": "Core Mechanics", "start_ms": 120_000},
                ],
            }
    meta_bytes = json.dumps(metadata_dict).encode("utf-8")

    with db.transaction() as conn:
        ChannelRepository(conn).add(channel)
        StrategyProfileRepository(conn).add(profile)
        ContentItemRepository(conn).add(item)
        art_repo = ArtifactRepository(conn)
        v_art = _store_artifact(
            storage, art_repo, item.id, ArtifactKind.VIDEO, 1, "video/mp4", mp4_bytes
        )
        m_art = _store_artifact(
            storage,
            art_repo,
            item.id,
            ArtifactKind.METADATA,
            1,
            "application/json",
            meta_bytes,
        )
        qc = QCResult.create(
            item.id,
            [v_art.id, m_art.id],
            [QCCheck("video.integrity", QCStatus.PASSED, "ok")],
            clock=lambda: T0,
        )
        QCResultRepository(conn).add(qc)
        req = ApprovalRequest.create(
            item.id,
            [v_art, m_art],
            qc,
            requested_by=SYSTEM,
            clock=lambda: T0,
        ).approve(USER)
        ApprovalRequestRepository(conn).add(req)
        disc = DisclosureRecord.create(
            content_item_id=item.id,
            channel_id=channel.id,
            rule_set_id="disclosure",
            rule_set_version="1.0.0",
            decision=DisclosureDecision.REQUIRED,
            rationale=(
                RationaleEntry(
                    rule_id="synthetic_voice",
                    matched=True,
                    summary="Synthetic voice narration",
                ),
            ),
            sources=DisclosureSources(facts=("voice.synthetic",), asset_ids=()),
            decided_by=SYSTEM,
            clock=lambda: T0,
        )
        DisclosureRepository(conn).add(disc)

    return db, storage, sink, gate, item, req


def test_publish_metadata_mapper_enforces_shorts_marker_and_disclosure(
    tmp_path: Path,
) -> None:
    db, _, _, _, item, req = _seed_approved_item(
        tmp_path, content_type=ContentType.SHORTS
    )
    conn = db.connect()
    try:
        channel = ChannelRepository(conn).get(item.channel_id)
        arts = ArtifactRepository(conn).list_by_content_item(item.id)
        v_art = next(a for a in arts if a.kind is ArtifactKind.VIDEO)
        m_art = next(a for a in arts if a.kind is ArtifactKind.METADATA)
        disc = DisclosureRepository(conn).latest(item.id)
    finally:
        conn.close()

    assert channel is not None
    mapper = PublishMetadataMapper()

    with pytest.raises(PublishMetadataMappingError) as no_marker_exc:
        mapper.map_to_payload(
            channel=channel,
            content_type=ContentType.SHORTS,
            approval=req,
            video_artifact=v_art,
            metadata_artifact=m_art,
            metadata_source={
                "title": "No Marker At All",
                "description": "Plain description",
                "tags": ["Science"],
                "language": "en",
                "made_for_kids": False,
                "contains_synthetic_media": True,
            },
            disclosure_record=disc,
        )
    assert (
        no_marker_exc.value.mapping_code
        is PublishMetadataErrorCode.SHORTS_MARKER_MISSING
    )

    with pytest.raises(PublishMetadataMappingError) as disc_exc:
        mapper.map_to_payload(
            channel=channel,
            content_type=ContentType.SHORTS,
            approval=req,
            video_artifact=v_art,
            metadata_artifact=m_art,
            metadata_source={
                "title": "Valid #Shorts",
                "description": "Desc",
                "tags": ["Shorts"],
                "language": "en",
                "made_for_kids": False,
                "contains_synthetic_media": False,
            },
            disclosure_record=disc,
        )
    assert (
        disc_exc.value.mapping_code
        is PublishMetadataErrorCode.DISCLOSURE_MISMATCH
    )


def test_shorts_publisher_publishes_approved_short_and_replays_idempotently(
    tmp_path: Path,
) -> None:
    db, storage, sink, gate, item, _ = _seed_approved_item(
        tmp_path, content_type=ContentType.SHORTS
    )
    conn = db.connect()
    try:
        channel = ChannelRepository(conn).get(item.channel_id)
    finally:
        conn.close()
    assert channel is not None

    oauth = MockYouTubeOAuthProvider(clock=lambda: T0)
    oauth.authorize_channel(
        channel.id,
        scopes=(OAuthScope.YOUTUBE_UPLOAD, OAuthScope.YOUTUBE_READONLY),
    )
    channel_provider = MockYouTubeChannelProvider(
        oauth_provider=oauth, clock=lambda: T0
    )
    channel_provider.register_channel(
        RemoteChannelInfo(
            youtube_channel_id=channel.youtube.channel_id,
            title=channel.title,
            fetched_at=T0,
        )
    )
    uploader = MockYouTubeUploadProvider(clock=lambda: T0)

    disabled_publisher = ShortsPublisher(
        db,
        storage,
        AuditLog(sink),
        gate,
        oauth,
        channel_provider,
        uploader,
        flags=FeatureFlags(publish_enabled=False),
        clock=lambda: T0,
    )
    with pytest.raises(ShortsPublishError) as disabled_exc:
        disabled_publisher.publish(item.id, actor=USER)
    assert (
        disabled_exc.value.publish_code
        is ShortsPublishErrorCode.PUBLISH_DISABLED
    )

    publisher = ShortsPublisher(
        db,
        storage,
        AuditLog(sink),
        gate,
        oauth,
        channel_provider,
        uploader,
        flags=FeatureFlags(
            shorts_enabled=True,
            publish_enabled=True,
            test_required=True,
            approval_required=True,
        ),
        clock=lambda: T0,
    )
    outcome = publisher.publish(item.id, actor=USER)
    assert outcome.replayed is False
    assert outcome.job.status is PublishStatus.SUCCEEDED
    assert outcome.receipt is not None
    assert (
        outcome.job.result is not None
        and outcome.job.result.youtube_video_id
        == outcome.receipt.youtube_video_id
    )
    assert any(e.action == SHORTS_PUBLISHED for e in sink.events)

    replay = publisher.publish(item.id, actor=USER)
    assert replay.replayed is True
    assert replay.job.id == outcome.job.id
    assert len(uploader.uploads) == 1


def test_shorts_publisher_records_failed_job_and_item_on_upload_error(
    tmp_path: Path,
) -> None:
    db, storage, sink, gate, item, _ = _seed_approved_item(
        tmp_path, content_type=ContentType.SHORTS
    )
    conn = db.connect()
    try:
        channel = ChannelRepository(conn).get(item.channel_id)
    finally:
        conn.close()
    assert channel is not None

    oauth = MockYouTubeOAuthProvider(clock=lambda: T0)
    oauth.authorize_channel(channel.id)
    channel_provider = MockYouTubeChannelProvider(
        oauth_provider=oauth, clock=lambda: T0
    )
    channel_provider.register_channel(
        RemoteChannelInfo(
            youtube_channel_id=channel.youtube.channel_id,
            title=channel.title,
            fetched_at=T0,
        )
    )
    uploader = MockYouTubeUploadProvider(clock=lambda: T0)
    uploader.fail_next(UploadErrorCode.UNAVAILABLE)

    publisher = ShortsPublisher(
        db,
        storage,
        AuditLog(sink),
        gate,
        oauth,
        channel_provider,
        uploader,
        flags=FeatureFlags(
            shorts_enabled=True,
            publish_enabled=True,
            test_required=True,
            approval_required=True,
        ),
        clock=lambda: T0,
    )
    with pytest.raises(YouTubeUploadError):
        publisher.publish(item.id, actor=USER)

    conn = db.connect()
    try:
        updated_item = ContentItemRepository(conn).get(item.id)
    finally:
        conn.close()
    assert updated_item is not None and updated_item.status is ContentStatus.FAILED
    assert any(e.action == SHORTS_PUBLISH_FAILED for e in sink.events)


def test_longform_publisher_respects_longform_flag_and_publishes_with_chapters(
    tmp_path: Path,
) -> None:
    db, storage, sink, gate, item, _ = _seed_approved_item(
        tmp_path,
        content_type=ContentType.LONGFORM,
        duration_ms=20 * 60 * 1_000,
    )
    conn = db.connect()
    try:
        channel = ChannelRepository(conn).get(item.channel_id)
    finally:
        conn.close()
    assert channel is not None

    oauth = MockYouTubeOAuthProvider(clock=lambda: T0)
    oauth.authorize_channel(channel.id)
    channel_provider = MockYouTubeChannelProvider(
        oauth_provider=oauth, clock=lambda: T0
    )
    uploader = MockYouTubeUploadProvider(clock=lambda: T0)

    locked_publisher = LongFormPublisher(
        db,
        storage,
        AuditLog(sink),
        gate,
        oauth,
        channel_provider,
        uploader,
        flags=FeatureFlags(
            longform_enabled=False,
            publish_enabled=True,
            test_required=True,
            approval_required=True,
        ),
        clock=lambda: T0,
    )
    with pytest.raises(LongFormDisabledError):
        locked_publisher.publish(item.id, actor=USER)

    channel_provider.register_channel(
        RemoteChannelInfo(
            youtube_channel_id=channel.youtube.channel_id,
            title=channel.title,
            fetched_at=T0,
            long_uploads_status="disallowed",
        )
    )
    publisher = LongFormPublisher(
        db,
        storage,
        AuditLog(sink),
        gate,
        oauth,
        channel_provider,
        uploader,
        flags=FeatureFlags(
            longform_enabled=True,
            publish_enabled=True,
            test_required=True,
            approval_required=True,
        ),
        clock=lambda: T0,
    )
    with pytest.raises(LongFormPublishError) as long_disallowed_exc:
        publisher.publish(item.id, actor=USER)
    assert (
        long_disallowed_exc.value.publish_code
        is LongFormPublishErrorCode.LONG_UPLOADS_DISALLOWED
    )

    channel_provider.register_channel(
        RemoteChannelInfo(
            youtube_channel_id=channel.youtube.channel_id,
            title=channel.title,
            fetched_at=T0,
            long_uploads_status="allowed",
        )
    )
    outcome = publisher.publish(
        item.id,
        actor=USER,
        privacy_status=YouTubePrivacyStatus.UNLISTED,
    )
    assert outcome.job.status is PublishStatus.SUCCEEDED
    assert outcome.receipt is not None
    assert "00:00 Intro" in outcome.payload.snippet.description
    assert any(e.action == LONGFORM_PUBLISHED for e in sink.events)
