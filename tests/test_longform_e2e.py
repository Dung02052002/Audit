"""T-226 LongForm E2E: End-to-end LongForm pipeline through generation, chapters,
QC, preview, explicit human approval (R-08), and controlled/mock publishing.
"""

import dataclasses
import json
import pytest

from ai_youtube_agent.content.approval import ApprovalStatus
from ai_youtube_agent.content.approval_service import ApprovalService
from ai_youtube_agent.content.channel import ChannelStatus
from ai_youtube_agent.content.longform_script_generator import LongFormDisabledError
from ai_youtube_agent.content.preview_artifact import PreviewArtifactService
from ai_youtube_agent.content.qc import QCCheck, QCResult, QCStatus
from ai_youtube_agent.content.strategy import Cadence
from ai_youtube_agent.core.artifact import ArtifactKind
from ai_youtube_agent.core.artifact_storage import LocalFileStorage
from ai_youtube_agent.core.audit import AuditLog, InMemoryAuditSink
from ai_youtube_agent.core.config import Environment, Settings
from ai_youtube_agent.core.content_item import ContentStatus, ContentType
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
from ai_youtube_agent.pipeline.longform_publisher import (
    LONGFORM_PUBLISHED,
    LongFormPublisher,
)
from ai_youtube_agent.providers.mock_video_render import stub_mp4
from ai_youtube_agent.providers.youtube_upload import YouTubePrivacyStatus
from factories import (
    SYSTEM,
    USER,
    at,
    make_channel,
    make_content_item,
    make_strategy_profile,
)
from publish_helpers import (
    PUBLISH_FLAGS,
    Seed,
    build_publishing,
    default_metadata,
    store_artifact,
)


def test_longform_e2e_chapters_qc_preview_approval_and_mock_publish(
    database: Database, tmp_path
) -> None:
    """Runs a LongForm ContentItem with chapters from DRAFT through PUBLISHED,
    and verifies LONGFORM_ENABLED gating (R-05) and human approval (R-08)."""
    storage = LocalFileStorage(tmp_path / "artifacts")
    settings = Settings(environment=Environment.TEST)
    sink = InMemoryAuditSink()
    audit = AuditLog(sink)

    channel = make_channel(status=ChannelStatus.ACTIVE)
    profile = make_strategy_profile(
        channel, cadence=Cadence(shorts_per_day=2, longform_per_day=2)
    )
    item = make_content_item(
        channel,
        profile,
        content_type=ContentType.LONGFORM,
        status=ContentStatus.DRAFT,
    )

    with database.transaction() as conn:
        ChannelRepository(conn).add(channel)
        StrategyProfileRepository(conn).add(profile)
        ContentItemRepository(conn).add(item)

    # 1. Start generation and store LongForm artifacts with chapters
    prev = item.updated_at
    item = item.with_status(ContentStatus.GENERATING, clock=at(1))
    with database.transaction() as conn:
        ContentItemRepository(conn).update(item, expected_updated_at=prev)

    video_bytes = stub_mp4(600_000, b"longform-e2e-video-payload")
    meta_dict = default_metadata(ContentType.LONGFORM, item, channel)
    meta_bytes = json.dumps(meta_dict).encode("utf-8")
    thumb_bytes = b"\x89PNG\r\n\x1a\n-longform-thumbnail"

    with database.transaction() as conn:
        art_repo = ArtifactRepository(conn)
        video_art = store_artifact(
            storage, art_repo, item.id, ArtifactKind.VIDEO, "video/mp4", video_bytes
        )
        meta_art = store_artifact(
            storage,
            art_repo,
            item.id,
            ArtifactKind.METADATA,
            "application/json",
            meta_bytes,
        )
        thumb_art = store_artifact(
            storage,
            art_repo,
            item.id,
            ArtifactKind.THUMBNAIL,
            "image/png",
            thumb_bytes,
        )
        bound = [video_art, meta_art, thumb_art]

        prev = item.updated_at
        item = item.with_status(ContentStatus.TESTING, clock=at(2))
        ContentItemRepository(conn).update(item, expected_updated_at=prev)
        qc = QCResult.create(
            item.id,
            bound,
            [
                QCCheck("video.container", QCStatus.PASS),
                QCCheck("video.resolution", QCStatus.PASS),
                QCCheck("video.duration", QCStatus.PASS),
                QCCheck("qc.rights", QCStatus.PASS),
                QCCheck("qc.policy", QCStatus.PASS),
                QCCheck("qc.ai_disclosure", QCStatus.PASS),
            ],
            clock=at(2),
        )
        QCResultRepository(conn).add(qc)

    # 2. Preview snapshot and Approval
    preview_service = PreviewArtifactService(database, storage)
    snapshot = preview_service.load_snapshot(item.id)
    assert snapshot.content_item.content_type is ContentType.LONGFORM

    approval_service = ApprovalService(database, audit, clock=at(3))
    req = approval_service.request_approval(item.id, requested_by=SYSTEM)
    approved = approval_service.approve(req.approval.id, actor=USER)
    assert approved.approval.status is ApprovalStatus.APPROVED
    assert approved.content_item.status is ContentStatus.APPROVED

    seed = Seed(
        database=database,
        storage=storage,
        settings=settings,
        channel=channel,
        profile=profile,
        item=approved.content_item,
        approval=approved.approval,
        artifacts={
            ArtifactKind.VIDEO: video_art,
            ArtifactKind.METADATA: meta_art,
            ArtifactKind.THUMBNAIL: thumb_art,
        },
        video_bytes=video_bytes,
    )
    pub = build_publishing(seed)

    # 3. R-05: When longform_enabled is False, LongFormPublisher refuses to publish
    disabled_flags = dataclasses.replace(PUBLISH_FLAGS, longform_enabled=False)
    disabled_publisher = LongFormPublisher(
        database, storage, pub.executor, flags=disabled_flags
    )
    with pytest.raises(LongFormDisabledError):
        disabled_publisher.publish(item.id, actor=SYSTEM)

    # 4. With longform_enabled=True, LongFormPublisher publishes with chapters
    enabled_publisher = LongFormPublisher(
        database, storage, pub.executor, flags=PUBLISH_FLAGS
    )
    outcome = enabled_publisher.publish(
        item.id,
        actor=SYSTEM,
        privacy_status=YouTubePrivacyStatus.PRIVATE,
    )
    assert outcome.replayed is False
    assert outcome.item.status is ContentStatus.PUBLISHED
    assert outcome.result.youtube_video_id != ""
    assert any(e.action == LONGFORM_PUBLISHED for e in pub.sink.events())
