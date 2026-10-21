"""T-228 Email E2E: Controlled approval email notification, request-changes loop,
and human approval flow.
"""

import json

from ai_youtube_agent.content.approval import ApprovalStatus
from ai_youtube_agent.content.approval_service import ApprovalService
from ai_youtube_agent.content.channel import ChannelStatus
from ai_youtube_agent.content.email_approval import ApprovalEmailService
from ai_youtube_agent.content.qc import QCCheck, QCResult, QCStatus
from ai_youtube_agent.content.strategy import Cadence
from ai_youtube_agent.core.artifact import ArtifactKind
from ai_youtube_agent.core.artifact_storage import LocalFileStorage
from ai_youtube_agent.core.audit import AuditLog, InMemoryAuditSink
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
from ai_youtube_agent.providers.mock_approval_email import MockApprovalEmailProvider
from ai_youtube_agent.providers.mock_video_render import stub_mp4
from factories import (
    SYSTEM,
    USER,
    at,
    make_channel,
    make_content_item,
    make_strategy_profile,
)
from publish_helpers import default_metadata, store_artifact


def test_email_approval_e2e_request_changes_and_final_approve(
    database: Database, tmp_path
) -> None:
    """Verifies controlled email dispatch on approval request, request-changes loop,
    re-requesting approval on v2, and final human approval."""
    storage = LocalFileStorage(tmp_path / "artifacts")
    sink = InMemoryAuditSink()
    audit = AuditLog(sink)
    email_provider = MockApprovalEmailProvider(clock=at(1))
    email_service = ApprovalEmailService(
        database,
        email_provider,
        audit,
        default_base_url="https://review.command-center.local",
    )
    approval_service = ApprovalService(
        database,
        audit,
        email_service=email_service,
        clock=at(1),
    )

    channel = make_channel(status=ChannelStatus.ACTIVE)
    profile = make_strategy_profile(
        channel, cadence=Cadence(shorts_per_day=3, longform_per_day=1)
    )
    item = make_content_item(
        channel,
        profile,
        content_type=ContentType.SHORTS,
        status=ContentStatus.TESTING,
    )

    with database.transaction() as conn:
        ChannelRepository(conn).add(channel)
        StrategyProfileRepository(conn).add(profile)
        ContentItemRepository(conn).add(item)
        art_repo = ArtifactRepository(conn)
        video_v1 = store_artifact(
            storage,
            art_repo,
            item.id,
            ArtifactKind.VIDEO,
            "video/mp4",
            stub_mp4(20_000, b"email-e2e-v1"),
            version=1,
        )
        meta_v1 = store_artifact(
            storage,
            art_repo,
            item.id,
            ArtifactKind.METADATA,
            "application/json",
            json.dumps(default_metadata(ContentType.SHORTS, item, channel)).encode(
                "utf-8"
            ),
            version=1,
        )
        qc_v1 = QCResult.create(
            item.id,
            [video_v1, meta_v1],
            [QCCheck("video.fps", QCStatus.PASS)],
            clock=at(1),
        )
        QCResultRepository(conn).add(qc_v1)

    # 1. Request approval with recipient_email -> sends email for v1
    req_v1 = approval_service.request_approval(
        item.id,
        requested_by=SYSTEM,
        recipient_email="editor@example.com",
    )
    assert req_v1.approval.status is ApprovalStatus.PENDING
    assert req_v1.email_receipt is not None
    assert len(email_provider.outbox) == 1
    msg_v1 = email_provider.outbox[0]
    assert msg_v1.recipient == "editor@example.com"
    assert video_v1.id in msg_v1.text_body
    assert req_v1.approval.id in msg_v1.preview_url
    assert str(tmp_path) not in msg_v1.text_body

    # 2. Reviewer requests changes -> item moves back to GENERATING
    changes = approval_service.request_changes(
        req_v1.approval.id,
        actor=USER,
        reason="Please tighten the opening hook.",
        target_kinds=(ArtifactKind.VIDEO,),
    )
    assert changes.approval.status is ApprovalStatus.CHANGES_REQUESTED
    assert changes.content_item.status is ContentStatus.GENERATING

    # 3. Store v2 video artifact, run QC, and request approval again
    with database.transaction() as conn:
        art_repo = ArtifactRepository(conn)
        video_v2 = store_artifact(
            storage,
            art_repo,
            item.id,
            ArtifactKind.VIDEO,
            "video/mp4",
            stub_mp4(20_000, b"email-e2e-v2"),
            version=2,
        )
        cur_item = ContentItemRepository(conn).get(item.id)
        assert cur_item is not None
        testing_item = cur_item.with_status(ContentStatus.TESTING, clock=at(2))
        ContentItemRepository(conn).update(
            testing_item, expected_updated_at=cur_item.updated_at
        )
        qc_v2 = QCResult.create(
            item.id,
            [video_v2, meta_v1],
            [QCCheck("video.fps", QCStatus.PASS)],
            clock=at(2),
        )
        QCResultRepository(conn).add(qc_v2)

    req_v2 = approval_service.request_approval(
        item.id,
        requested_by=SYSTEM,
        recipient_email="editor@example.com",
        expected_video_version=2,
    )
    assert len(email_provider.outbox) == 2
    msg_v2 = email_provider.outbox[1]
    assert video_v2.id in msg_v2.text_body
    assert "version=2" in msg_v2.preview_url

    # 4. Human reviewer approves v2
    approved = approval_service.approve(
        req_v2.approval.id,
        actor=USER,
        expected_video_version=2,
        expected_video_artifact_id=video_v2.id,
    )
    assert approved.approval.status is ApprovalStatus.APPROVED
    assert approved.content_item.status is ContentStatus.APPROVED
