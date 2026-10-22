"""Tests for L-137..L-140 Approval Request, Email Approval, Approve Action, and Reject
Action."""

import dataclasses
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from tests.test_preview_api import _seed_shorts_preview_item

from ai_youtube_agent.bootstrap import build_container, prepare_database
from ai_youtube_agent.content.approval import (
    ApprovalConflictError,
    ApprovalNotAllowedError,
    ApprovalReasonRequiredError,
    ApprovalStateError,
    ApprovalStatus,
)
from ai_youtube_agent.content.approval_service import ApprovalService
from ai_youtube_agent.core.approval_gate import ApprovalGate
from ai_youtube_agent.core.artifact import ArtifactKind
from ai_youtube_agent.core.artifact_storage import ArtifactStorage
from ai_youtube_agent.core.audit import Actor, ActorKind, AuditLog
from ai_youtube_agent.core.config import Environment, Settings
from ai_youtube_agent.core.content_item import ContentStatus
from ai_youtube_agent.core.db.database import Database
from ai_youtube_agent.core.db.repositories.content import ArtifactRepository
from ai_youtube_agent.core.db.repositories.review import ApprovalRequestRepository
from ai_youtube_agent.core.gates import GateContext, GateOutcome
from ai_youtube_agent.main import create_app
from ai_youtube_agent.providers.approval_email import ApprovalEmailProvider
from ai_youtube_agent.providers.mock_approval_email import MockApprovalEmailProvider

NOW = datetime(2026, 10, 19, 12, 0, tzinfo=UTC)
HUMAN_REVIEWER = Actor(ActorKind.USER, "owner-1")
AI_ACTOR = Actor(ActorKind.AI, "agent-1")


def test_request_approval_sends_safe_email_and_approve_unlocks_approval_gate(
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
    item, art_ids = _seed_shorts_preview_item(db, storage)  # type: ignore[arg-type]

    approval_service = container.resolve(ApprovalService)
    email_provider = container.resolve(ApprovalEmailProvider)
    assert isinstance(email_provider, MockApprovalEmailProvider)

    # L-137 + L-138: Request approval bound to v1 and send safe approval email
    req_result = approval_service.request_approval(
        item.id,
        requested_by=HUMAN_REVIEWER,
        expected_video_version=1,
        expected_video_artifact_id=art_ids[ArtifactKind.VIDEO],
        recipient_email="owner@example.com",
        base_url="https://review.local",
    )
    assert req_result.approval.status is ApprovalStatus.PENDING
    assert req_result.content_item.status is ContentStatus.AWAITING_APPROVAL
    assert req_result.email_receipt is not None
    assert len(email_provider.outbox) == 1

    sent_msg = email_provider.outbox[0]
    assert sent_msg.recipient == "owner@example.com"
    assert sent_msg.approval_request_id == req_result.approval.id
    assert sent_msg.video_artifact_id == art_ids[ArtifactKind.VIDEO]
    assert sent_msg.video_artifact_version == 1
    assert sent_msg.preview_url == (
        f"https://review.local/previews/{item.id}"
        f"?version=1&approval_request_id={req_result.approval.id}"
    )
    assert art_ids[ArtifactKind.VIDEO] in sent_msg.text_body

    # Before approval, ApprovalGate blocks PUBLISHING
    with db.transaction() as conn:
        gate = ApprovalGate(ApprovalRequestRepository(conn), ArtifactRepository(conn))
        blocked = gate.evaluate(
            GateContext(
                # An approved-status copy of the item: only that status may
                # ask to publish, and the newest approval is still pending.
                dataclasses.replace(
                    req_result.content_item, status=ContentStatus.APPROVED
                ),
                ContentStatus.PUBLISHING,
                HUMAN_REVIEWER,
                NOW,
            )
        )
        assert blocked.outcome is GateOutcome.BLOCK

    # AI actor cannot approve (#139)
    with pytest.raises(ApprovalNotAllowedError):
        approval_service.approve(req_result.approval.id, actor=AI_ACTOR)

    # Wrong expected_video_version raises conflict (#139)
    with pytest.raises(ApprovalConflictError):
        approval_service.approve(
            req_result.approval.id,
            actor=HUMAN_REVIEWER,
            expected_video_version=99,
        )

    # Explicit human approval (#139)
    approved_res = approval_service.approve(
        req_result.approval.id,
        actor=HUMAN_REVIEWER,
        expected_video_artifact_id=art_ids[ArtifactKind.VIDEO],
        expected_video_version=1,
        note="Looks ready to publish",
    )
    assert approved_res.approval.status is ApprovalStatus.APPROVED
    assert approved_res.content_item.status is ContentStatus.APPROVED

    # After explicit human approval on current artifacts, ApprovalGate passes!
    with db.transaction() as conn:
        gate = ApprovalGate(ApprovalRequestRepository(conn), ArtifactRepository(conn))
        passed = gate.evaluate(
            GateContext(
                approved_res.content_item,
                ContentStatus.PUBLISHING,
                HUMAN_REVIEWER,
                NOW,
            )
        )
        assert passed.outcome is GateOutcome.PASS


def test_reject_action_requires_reason_and_transitions_item_to_rejected(
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
    item, _ = _seed_shorts_preview_item(db, storage)  # type: ignore[arg-type]

    approval_service = container.resolve(ApprovalService)
    req_result = approval_service.request_approval(item.id, requested_by=HUMAN_REVIEWER)

    with pytest.raises(ApprovalReasonRequiredError):
        approval_service.reject(
            req_result.approval.id, actor=HUMAN_REVIEWER, reason="   "
        )

    rejected_res = approval_service.reject(
        req_result.approval.id,
        actor=HUMAN_REVIEWER,
        reason="Audio pacing in second cue is too fast",
    )
    assert rejected_res.approval.status is ApprovalStatus.REJECTED
    assert rejected_res.content_item.status is ContentStatus.REJECTED
    assert rejected_res.reason == "Audio pacing in second cue is too fast"

    with pytest.raises(ApprovalStateError):
        approval_service.approve(req_result.approval.id, actor=HUMAN_REVIEWER)

    events = container.resolve(AuditLog)._sink.events()
    rejected_events = [e for e in events if e.action == "approval.rejected"]
    assert any(
        e.result.value == "success"
        and e.metadata.get("reason") == "Audio pacing in second cue is too fast"
        for e in rejected_events
    )


def test_approvals_http_api_routes(tmp_path: Path) -> None:
    settings = Settings(
        environment=Environment.TEST,
        database_path=tmp_path / "agent.db",
        artifact_dir=tmp_path / "artifacts",
    )
    prepare_database(settings)
    container = build_container(settings)
    db = container.resolve(Database)
    storage = container.resolve(ArtifactStorage)
    item, art_ids = _seed_shorts_preview_item(db, storage)  # type: ignore[arg-type]

    client = TestClient(create_app(container))

    create_res = client.post(
        "/approvals",
        json={
            "content_item_id": item.id,
            "expected_video_version": 1,
            "recipient_email": "reviewer@example.com",
        },
    )
    assert create_res.status_code == 201
    approval_id = create_res.json()["approval"]["id"]

    email_res = client.post(
        f"/approvals/{approval_id}/email",
        json={"recipient_email": "backup-reviewer@example.com"},
    )
    assert email_res.status_code == 200
    assert (
        email_res.json()["message"]["video_artifact_id"] == art_ids[ArtifactKind.VIDEO]
    )

    approve_res = client.post(
        f"/approvals/{approval_id}/approve",
        json={
            "expected_video_artifact_id": art_ids[ArtifactKind.VIDEO],
            "expected_video_version": 1,
            "note": "Approved via API",
        },
    )
    assert approve_res.status_code == 200
    assert approve_res.json()["approval"]["status"] == "approved"
    assert approve_res.json()["content_item"]["status"] == "approved"
