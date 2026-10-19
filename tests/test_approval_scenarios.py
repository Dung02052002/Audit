"""L-141..L-144 Approval Tests: request changes, expiry/revalidation, audit trail, stale approval & version invalidation."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ai_youtube_agent.bootstrap import build_container
from ai_youtube_agent.content.approval import (
    ApprovalExpiredError,
    ApprovalNotAllowedError,
    ApprovalReasonRequiredError,
    ApprovalStateError,
    ApprovalStatus,
)
from ai_youtube_agent.content.approval_audit import (
    APPROVAL_APPROVED,
    APPROVAL_CHANGES_REQUESTED,
    APPROVAL_EXPIRED,
    APPROVAL_REJECTED,
    APPROVAL_REQUESTED,
    APPROVAL_REVALIDATED,
    ApprovalAuditService,
)
from ai_youtube_agent.content.approval_service import ApprovalService
from ai_youtube_agent.content.qc_regression import build_shorts_qc_fixture
from ai_youtube_agent.content.qc_report import QCReportBuilder
from ai_youtube_agent.core.approval_gate import ApprovalGate
from ai_youtube_agent.core.artifact import ArtifactKind
from ai_youtube_agent.core.audit import Actor, ActorKind, AuditLog, AuditSink
from ai_youtube_agent.core.config import Environment, Settings
from ai_youtube_agent.core.content_item import ContentStatus
from ai_youtube_agent.core.db.database import Database
from ai_youtube_agent.core.db.repositories.content import (
    ArtifactRepository,
    ContentItemRepository,
)
from ai_youtube_agent.core.db.repositories.review import ApprovalRequestRepository
from ai_youtube_agent.core.gates import GateContext
from ai_youtube_agent.core.version_invalidation import VersionInvalidation
from ai_youtube_agent.main import create_app

USER = Actor(ActorKind.USER, "owner-1")
AI_ACTOR = Actor(ActorKind.AI, "agent-1")
SYSTEM = Actor(ActorKind.SYSTEM, "pipeline")


class MutableClock:
    def __init__(self, initial: datetime) -> None:
        self.now = initial

    def __call__(self) -> datetime:
        self.now += timedelta(seconds=1)
        return self.now

    def advance(self, delta: timedelta) -> datetime:
        self.now += delta
        return self.now


from ai_youtube_agent.core.artifact_storage import LocalFileStorage


def _prepare_shorts_ready_for_approval(
    database: Database, tmp_path: Path
):
    storage = LocalFileStorage(tmp_path / "artifacts")
    fixture = build_shorts_qc_fixture(database, storage)
    container = build_container(
        Settings(
            environment=Environment.TEST,
            database_path=database.path,
            artifact_dir=tmp_path / "artifacts",
        )
    )
    qc_builder = container.resolve(QCReportBuilder)
    qc_builder.evaluate(fixture.item.id, actor=USER)
    with database.transaction() as conn:
        items = ContentItemRepository(conn)
        item = items.get(fixture.item.id)
        assert item is not None
        prev = item.updated_at
        item = item.with_status(ContentStatus.TESTING)
        items.update(item, expected_updated_at=prev)
        prev = item.updated_at
        item = item.with_status(ContentStatus.PREVIEW_READY)
        items.update(item, expected_updated_at=prev)
    return fixture, container


def test_request_changes_transitions_to_changes_requested_and_generating(
    database: Database, tmp_path: Path
) -> None:
    fixture, container = _prepare_shorts_ready_for_approval(database, tmp_path)
    service = container.resolve(ApprovalService)
    audit_service = container.resolve(ApprovalAuditService)

    created = service.request_approval(fixture.item.id, requested_by=USER)
    req_id = created.approval.id

    with pytest.raises(ApprovalNotAllowedError):
        service.request_changes(req_id, actor=AI_ACTOR, reason="Change hook")

    with pytest.raises(ApprovalReasonRequiredError):
        service.request_changes(req_id, actor=USER, reason="   ")

    changed = service.request_changes(
        req_id,
        actor=USER,
        reason="Tighten scene 1 hook and increase caption contrast",
        target_kinds=[ArtifactKind.VIDEO, ArtifactKind.SUBTITLES],
    )
    assert changed.approval.status is ApprovalStatus.CHANGES_REQUESTED
    assert changed.content_item.status is ContentStatus.GENERATING
    assert changed.decision == "changes_requested"
    assert changed.target_kinds == (ArtifactKind.VIDEO, ArtifactKind.SUBTITLES)

    # ApprovalGate blocks publishing when latest request is changes_requested
    with database.transaction() as conn:
        gate = ApprovalGate(
            ApprovalRequestRepository(conn), ArtifactRepository(conn)
        )
        res = gate.evaluate(
            GateContext(
                changed.content_item,
                ContentStatus.PUBLISHING,
                USER,
                datetime.now(UTC),
            )
        )
    assert res.is_blocked
    assert [r.code for r in res.reasons] == ["approval.not_approved"]

    trail = audit_service.trail_for_request(req_id)
    actions = [e.action for e in trail.entries]
    assert APPROVAL_REQUESTED in actions
    assert APPROVAL_CHANGES_REQUESTED in actions


def test_approval_expiry_and_revalidation_workflow(
    database: Database, tmp_path: Path
) -> None:
    fixture, container = _prepare_shorts_ready_for_approval(database, tmp_path)
    clock = MutableClock(datetime(2026, 10, 20, 10, 0, tzinfo=UTC))
    ttl = timedelta(hours=24)
    service = ApprovalService(
        database,
        container.resolve(AuditLog),
        ttl=ttl,
        clock=clock,
    )
    audit_service = ApprovalAuditService(
        database,
        container.resolve(AuditSink),
        container.resolve(AuditLog),
    )

    created = service.request_approval(fixture.item.id, requested_by=USER)
    req_id = created.approval.id

    # Advance past the 24h TTL while still PENDING
    clock.advance(timedelta(hours=25))

    with pytest.raises(ApprovalExpiredError):
        service.approve(req_id, actor=USER)

    stored = service.get(req_id)
    assert stored.status is ApprovalStatus.EXPIRED

    # Revalidate issues a fresh PENDING request bound to the same current artifacts
    revalidated = service.revalidate(req_id, requested_by=USER)
    new_req_id = revalidated.approval.id
    assert new_req_id != req_id
    assert revalidated.approval.status is ApprovalStatus.PENDING
    assert revalidated.content_item.status is ContentStatus.AWAITING_APPROVAL

    # Approve the revalidated request within TTL
    approved = service.approve(new_req_id, actor=USER)
    assert approved.approval.status is ApprovalStatus.APPROVED
    assert approved.content_item.status is ContentStatus.APPROVED

    # Advance clock past TTL after approval: ApprovalGate with ttl blocks as approval.expired
    clock.advance(timedelta(hours=25))
    with database.transaction() as conn:
        gate = ApprovalGate(
            ApprovalRequestRepository(conn),
            ArtifactRepository(conn),
            ttl=ttl,
        )
        gate_res = gate.evaluate(
            GateContext(
                approved.content_item,
                ContentStatus.PUBLISHING,
                USER,
                clock.now,
            )
        )
    assert gate_res.is_blocked
    assert [r.code for r in gate_res.reasons] == ["approval.expired"]

    # expire_stale transitions the expired APPROVED request to EXPIRED
    expired_list = service.expire_stale(
        fixture.item.id, actor=SYSTEM, at=clock.now
    )
    assert len(expired_list) == 1
    assert expired_list[0].id == new_req_id
    assert expired_list[0].status is ApprovalStatus.EXPIRED

    # Revalidate from APPROVED item state steps legally back to AWAITING_APPROVAL
    revalidated_2 = service.revalidate(new_req_id, requested_by=USER)
    assert revalidated_2.approval.status is ApprovalStatus.PENDING
    assert revalidated_2.content_item.status is ContentStatus.AWAITING_APPROVAL

    trail = audit_service.trail_for_content_item(fixture.item.id)
    assert trail.action_counts.get(APPROVAL_EXPIRED, 0) >= 2
    assert trail.action_counts.get(APPROVAL_REVALIDATED, 0) == 2
    assert trail.action_counts.get(APPROVAL_APPROVED, 0) >= 2


def test_stale_approval_and_version_invalidation_blocks_publish(
    database: Database, tmp_path: Path
) -> None:
    fixture, container = _prepare_shorts_ready_for_approval(database, tmp_path)
    service = container.resolve(ApprovalService)

    created = service.request_approval(fixture.item.id, requested_by=USER)
    approved = service.approve(created.approval.id, actor=USER)
    assert approved.approval.status is ApprovalStatus.APPROVED

    # Storing a new VIDEO artifact version invalidates the approval and moves item to GENERATING
    invalidation = VersionInvalidation(database, container.resolve(AuditLog))
    new_video = fixture.video_artifact.next_version(
        uri="file:///d:/Project_Audit/data/artifacts/v2.mp4",
        sha256="b" * 64,
        size_bytes=fixture.video_artifact.size_bytes + 128,
        media_type="video/mp4",
    )
    inv_result = invalidation.store_artifact_version(new_video)
    assert approved.approval.id in [r.id for r in inv_result.invalidated_requests]
    assert inv_result.content_item.status is ContentStatus.GENERATING

    reloaded_req = service.get(approved.approval.id)
    assert reloaded_req.status is ApprovalStatus.INVALIDATED


def test_approval_api_request_changes_expire_revalidate_and_audit_routes(
    database: Database, tmp_path: Path
) -> None:
    fixture, container = _prepare_shorts_ready_for_approval(database, tmp_path)
    client = TestClient(create_app(container))

    create_resp = client.post(
        "/approvals",
        json={"content_item_id": fixture.item.id},
    )
    assert create_resp.status_code == 201
    req_id = create_resp.json()["approval"]["id"]

    changes_resp = client.post(
        f"/approvals/{req_id}/request-changes",
        json={
            "reason": "Update thumbnail and title",
            "target_kinds": ["thumbnail", "metadata"],
        },
    )
    assert changes_resp.status_code == 200
    body = changes_resp.json()
    assert body["approval"]["status"] == "changes_requested"
    assert body["content_item"]["status"] == "generating"
    assert body["target_kinds"] == ["thumbnail", "metadata"]

    audit_req_resp = client.get(f"/approvals/{req_id}/audit")
    assert audit_req_resp.status_code == 200
    assert audit_req_resp.json()["total_events"] >= 2

    audit_item_resp = client.get(
        f"/approvals/by-item/{fixture.item.id}/audit"
    )
    assert audit_item_resp.status_code == 200
    assert audit_item_resp.json()["content_item_id"] == fixture.item.id

    expire_resp = client.post(
        f"/approvals/by-item/{fixture.item.id}/expire"
    )
    assert expire_resp.status_code == 200
    assert expire_resp.json()["expired_count"] == 0
