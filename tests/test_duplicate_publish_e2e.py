"""T-230 Duplicate Test: E2E verification that duplicate publish attempts are
idempotently replayed (same approval) or hard-blocked (same video bytes on channel).
"""

import json
import pytest

from ai_youtube_agent.content.approval import ApprovalRequest
from ai_youtube_agent.content.qc import QCCheck, QCResult, QCStatus
from ai_youtube_agent.core.artifact import ArtifactKind
from ai_youtube_agent.core.content_item import ContentStatus, ContentType
from ai_youtube_agent.core.db.database import Database
from ai_youtube_agent.core.db.repositories.content import (
    ArtifactRepository,
    ContentItemRepository,
)
from ai_youtube_agent.core.db.repositories.review import (
    ApprovalRequestRepository,
    QCResultRepository,
)
from ai_youtube_agent.pipeline.duplicate_guard import (
    DuplicateErrorCode,
    DuplicatePublishError,
)
from ai_youtube_agent.pipeline.publish_audit import (
    PUBLISH_DUPLICATE_BLOCKED,
    PUBLISH_REPLAYED,
)
from ai_youtube_agent.pipeline.shorts_publisher import ShortsPublisher
from factories import SYSTEM, USER, at, make_content_item
from publish_helpers import (
    PUBLISH_FLAGS,
    build_publishing,
    default_metadata,
    seed_approved_item,
    store_artifact,
)


def test_duplicate_publish_replays_same_approval_and_blocks_same_video_bytes(
    database: Database, tmp_path
) -> None:
    """Verifies that:
    1. Re-running publish for the same approved item replays the existing result without
       a second upload.
    2. Attempting to publish another item on the same channel with identical video bytes
       is hard-blocked with DuplicateErrorCode.SAME_VIDEO_PUBLISHED.
    """
    seed = seed_approved_item(database, tmp_path, payload=b"identical-video-bytes")
    pub = build_publishing(seed)
    publisher = ShortsPublisher(
        database, seed.storage, pub.executor, flags=PUBLISH_FLAGS
    )

    # 1. First publish succeeds and uploads once
    first = publisher.publish(seed.item.id, actor=SYSTEM)
    assert first.replayed is False
    assert first.item.status is ContentStatus.PUBLISHED
    assert len(pub.uploader.uploads) == 1

    # 2. Second publish of the same item & approval replays idempotently without uploading
    second = publisher.publish(seed.item.id, actor=SYSTEM)
    assert second.replayed is True
    assert second.result.youtube_video_id == first.result.youtube_video_id
    assert len(pub.uploader.uploads) == 1
    assert any(e.action == PUBLISH_REPLAYED for e in pub.sink.events())

    # 3. Create a second approved item on the same channel with identical video bytes
    item2 = make_content_item(
        seed.channel,
        seed.profile,
        content_type=ContentType.SHORTS,
        status=ContentStatus.APPROVED,
    )
    meta2_bytes = json.dumps(
        default_metadata(ContentType.SHORTS, item2, seed.channel)
    ).encode("utf-8")

    with database.transaction() as conn:
        ContentItemRepository(conn).add(item2)
        art_repo = ArtifactRepository(conn)
        video2 = store_artifact(
            seed.storage,
            art_repo,
            item2.id,
            ArtifactKind.VIDEO,
            "video/mp4",
            seed.video_bytes,
        )
        meta2 = store_artifact(
            seed.storage,
            art_repo,
            item2.id,
            ArtifactKind.METADATA,
            "application/json",
            meta2_bytes,
        )
        qc2 = QCResult.create(
            item2.id,
            [video2, meta2],
            [QCCheck("video.fps", QCStatus.PASS)],
            clock=at(2),
        )
        QCResultRepository(conn).add(qc2)
        approval2 = ApprovalRequest.create(
            item2.id,
            [video2, meta2],
            requested_by=SYSTEM,
            qc_result=qc2,
            clock=at(2),
        ).approve(USER, clock=at(3))
        ApprovalRequestRepository(conn).add(approval2)

    with pytest.raises(DuplicatePublishError) as exc_info:
        publisher.publish(item2.id, actor=SYSTEM)
    assert exc_info.value.duplicate_code is DuplicateErrorCode.SAME_VIDEO_PUBLISHED
    assert len(pub.uploader.uploads) == 1
    assert any(e.action == PUBLISH_DUPLICATE_BLOCKED for e in pub.sink.events())
