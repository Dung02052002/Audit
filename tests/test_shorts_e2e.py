"""T-225 Shorts E2E: End-to-end Shorts pipeline through generation, QC, preview,
explicit human approval (R-08), and controlled/mock publishing.
"""

import json
import pytest

from ai_youtube_agent.content.approval import (
    ApprovalNotAllowedError,
    ApprovalStatus,
)
from ai_youtube_agent.content.approval_service import ApprovalService
from ai_youtube_agent.content.channel import ChannelStatus
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
from ai_youtube_agent.pipeline.shorts_publisher import (
    SHORTS_PUBLISHED,
    ShortsPublisher,
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


def test_shorts_e2e_generation_qc_preview_approval_and_mock_publish(
    database: Database, tmp_path
) -> None:
    """Runs a Shorts ContentItem from DRAFT through GENERATING -> TESTING ->
    PREVIEW_READY -> AWAITING_APPROVAL -> APPROVED -> PUBLISHED using controlled mocks."""
    storage = LocalFileStorage(tmp_path / "artifacts")
    settings = Settings(environment=Environment.TEST)
    sink = InMemoryAuditSink()
    audit = AuditLog(sink)

    channel = make_channel(status=ChannelStatus.ACTIVE)
    profile = make_strategy_profile(
        channel, cadence=Cadence(shorts_per_day=3, longform_per_day=1)
    )
    item = make_content_item(
        channel,
        profile,
        content_type=ContentType.SHORTS,
        status=ContentStatus.DRAFT,
    )

    with database.transaction() as conn:
        ChannelRepository(conn).add(channel)
        StrategyProfileRepository(conn).add(profile)
        ContentItemRepository(conn).add(item)

    # 1. Start generation and store Shorts artifacts (VIDEO, METADATA, THUMBNAIL, SUBTITLES)
    prev = item.updated_at
    item = item.with_status(ContentStatus.GENERATING, clock=at(1))
    with database.transaction() as conn:
        ContentItemRepository(conn).update(item, expected_updated_at=prev)

    video_bytes = stub_mp4(30_000, b"shorts-e2e-video-payload")
    meta_dict = default_metadata(ContentType.SHORTS, item, channel)
    meta_bytes = json.dumps(meta_dict).encode("utf-8")
    srt_bytes = b"1\n00:00:00,000 --> 00:00:05,000\nWelcome to Shorts!\n"
    thumb_bytes = b"\x89PNG\r\n\x1a\n-shorts-thumbnail"

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
        sub_art = store_artifact(
            storage,
            art_repo,
            item.id,
            ArtifactKind.SUBTITLES,
            "application/x-subrip",
            srt_bytes,
        )
        bound_artifacts = [video_art, meta_art, thumb_art, sub_art]

        # 2. Move to TESTING and record passing QCResult
        prev = item.updated_at
        item = item.with_status(ContentStatus.TESTING, clock=at(2))
        ContentItemRepository(conn).update(item, expected_updated_at=prev)
        qc = QCResult.create(
            item.id,
            bound_artifacts,
            [
                QCCheck("video.container", QCStatus.PASS),
                QCCheck("video.resolution", QCStatus.PASS),
                QCCheck("video.fps", QCStatus.PASS),
                QCCheck("video.duration", QCStatus.PASS),
                QCCheck("qc.rights", QCStatus.PASS),
                QCCheck("qc.policy", QCStatus.PASS),
                QCCheck("qc.ai_disclosure", QCStatus.PASS),
            ],
            clock=at(2),
        )
        QCResultRepository(conn).add(qc)

    # 3. Preview snapshot loads exact artifact versions
    preview_service = PreviewArtifactService(database, storage)
    snapshot = preview_service.load_snapshot(item.id)
    assert snapshot.content_item.id == item.id
    assert snapshot.video.id == video_art.id

    # 4. Request approval -> transitions TESTING -> PREVIEW_READY -> AWAITING_APPROVAL
    approval_service = ApprovalService(database, audit, clock=at(3))
    req_decision = approval_service.request_approval(
        item.id,
        requested_by=SYSTEM,
        expected_video_version=1,
        expected_video_artifact_id=video_art.id,
    )
    assert req_decision.approval.status is ApprovalStatus.PENDING
    assert req_decision.content_item.status is ContentStatus.AWAITING_APPROVAL

    # 5. R-08 invariant: SYSTEM cannot approve its own request
    with pytest.raises(ApprovalNotAllowedError):
        approval_service.approve(req_decision.approval.id, actor=SYSTEM)

    # 6. Explicit human reviewer (USER) approves
    approve_decision = approval_service.approve(
        req_decision.approval.id,
        actor=USER,
        expected_video_version=1,
        expected_video_artifact_id=video_art.id,
    )
    assert approve_decision.approval.status is ApprovalStatus.APPROVED
    assert approve_decision.content_item.status is ContentStatus.APPROVED

    # 7. Controlled/mock Shorts publish
    seed = Seed(
        database=database,
        storage=storage,
        settings=settings,
        channel=channel,
        profile=profile,
        item=approve_decision.content_item,
        approval=approve_decision.approval,
        artifacts={
            ArtifactKind.VIDEO: video_art,
            ArtifactKind.METADATA: meta_art,
            ArtifactKind.THUMBNAIL: thumb_art,
            ArtifactKind.SUBTITLES: sub_art,
        },
        video_bytes=video_bytes,
    )
    pub = build_publishing(seed)
    publisher = ShortsPublisher(
        database, storage, pub.executor, flags=PUBLISH_FLAGS
    )
    outcome = publisher.publish(
        item.id,
        actor=SYSTEM,
        privacy_status=YouTubePrivacyStatus.PRIVATE,
    )

    assert outcome.replayed is False
    assert outcome.item.status is ContentStatus.PUBLISHED
    assert outcome.result.youtube_video_id != ""
    assert any(e.action == SHORTS_PUBLISHED for e in pub.sink.events())
