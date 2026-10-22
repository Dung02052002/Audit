"""T-231 Stale Approval Test: Modifying an artifact after approval invalidates
the old approval, resets the item to GENERATING, and blocks publishing.
"""

import pytest

from ai_youtube_agent.content.approval import ApprovalStatus
from ai_youtube_agent.core.artifact import Artifact, ArtifactKind
from ai_youtube_agent.core.audit import AuditLog, InMemoryAuditSink
from ai_youtube_agent.core.content_item import ContentStatus
from ai_youtube_agent.core.db.database import Database
from ai_youtube_agent.core.db.repositories.content import ArtifactRepository
from ai_youtube_agent.core.db.repositories.review import ApprovalRequestRepository
from ai_youtube_agent.core.gates import GateBlockedError
from ai_youtube_agent.core.publish_gate import PublishGate
from ai_youtube_agent.core.version_invalidation import VersionInvalidation
from ai_youtube_agent.pipeline.publish_errors import PublishError
from ai_youtube_agent.pipeline.shorts_publisher import ShortsPublisher
from ai_youtube_agent.providers.mock_video_render import stub_mp4
from factories import SYSTEM, at
from publish_helpers import (
    PUBLISH_FLAGS,
    build_publishing,
    seed_approved_item,
    store_artifact,
)


def test_modifying_artifact_after_approval_invalidates_and_blocks_publish(
    database: Database, tmp_path
) -> None:
    """Verifies that storing a new artifact version via VersionInvalidation marks the
    prior approval INVALIDATED, resets the item to GENERATING, and prevents
    publishing."""
    seed = seed_approved_item(database, tmp_path)
    pub = build_publishing(seed)
    publisher = ShortsPublisher(
        database, seed.storage, pub.executor, flags=PUBLISH_FLAGS
    )

    inv_sink = InMemoryAuditSink()
    invalidation = VersionInvalidation(database, AuditLog(inv_sink), clock=at(5))

    new_video_bytes = stub_mp4(30_000, b"modified-after-approval-v2")
    stored_v2 = seed.storage.write(
        seed.item.id, ArtifactKind.VIDEO, 2, new_video_bytes, media_type="video/mp4"
    )
    video_v2 = Artifact.create(
        seed.item.id,
        ArtifactKind.VIDEO,
        version=2,
        uri=stored_v2.uri,
        sha256=stored_v2.sha256,
        size_bytes=stored_v2.size_bytes,
        media_type="video/mp4",
        clock=at(5),
    )

    result = invalidation.store_artifact_version(video_v2)
    assert result.item_reset is True
    assert result.item.status is ContentStatus.GENERATING
    assert len(result.invalidated) == 1
    assert result.invalidated[0].approval.status is ApprovalStatus.INVALIDATED
    assert any(e.action == "approval.invalidated" for e in inv_sink.events())

    with database.connection() as conn:
        reloaded_req = ApprovalRequestRepository(conn).get(seed.approval.id)
        assert reloaded_req is not None
        assert reloaded_req.status is ApprovalStatus.INVALIDATED

    with pytest.raises(PublishError):
        publisher.publish(seed.item.id, actor=SYSTEM)
    assert len(pub.uploader.uploads) == 0


def test_publish_gate_blocks_stale_approval_even_without_invalidation_hook(
    database: Database, tmp_path
) -> None:
    """Defense-in-depth: even if a v2 artifact row is inserted without running
    VersionInvalidation, ApprovalGate / PublishGate and ShortsPublisher reject the
    stale v1 approval before any upload can happen."""
    seed = seed_approved_item(database, tmp_path)
    pub = build_publishing(seed)
    publisher = ShortsPublisher(
        database, seed.storage, pub.executor, flags=PUBLISH_FLAGS
    )

    with database.transaction() as conn:
        store_artifact(
            seed.storage,
            ArtifactRepository(conn),
            seed.item.id,
            ArtifactKind.VIDEO,
            "video/mp4",
            stub_mp4(30_000, b"unapproved-v2-bytes"),
            version=2,
        )

    gate = PublishGate(database, seed.settings)
    with pytest.raises(GateBlockedError) as exc_info:
        gate.ensure_can_publish(seed.item, actor=SYSTEM, at=at(5))
    assert any(r.code == "approval.not_current" for r in exc_info.value.report.reasons)

    with pytest.raises((PublishError, GateBlockedError)):
        publisher.publish(seed.item.id, actor=SYSTEM)
    assert len(pub.uploader.uploads) == 0
