"""Tests for L-136 Preview Edit Loop (`PreviewEditLoopService` and `POST
/previews/{id}/edit-loop`)."""

from datetime import UTC, datetime
from pathlib import Path

import pytest
from tests.test_preview_api import _seed_shorts_preview_item

from ai_youtube_agent.bootstrap import build_container, prepare_database
from ai_youtube_agent.content.approval import ApprovalRequest, ApprovalStatus
from ai_youtube_agent.content.preview_edit_loop import (
    PreviewEditChanges,
    PreviewEditError,
    PreviewEditLoopService,
)
from ai_youtube_agent.core.artifact import ArtifactKind
from ai_youtube_agent.core.artifact_storage import ArtifactStorage
from ai_youtube_agent.core.audit import Actor, ActorKind
from ai_youtube_agent.core.config import Environment, Settings
from ai_youtube_agent.core.db.database import Database
from ai_youtube_agent.core.db.repositories.content import ArtifactRepository
from ai_youtube_agent.core.db.repositories.review import ApprovalRequestRepository

NOW = datetime(2026, 10, 19, 12, 0, tzinfo=UTC)
REVIEWER = Actor(ActorKind.USER, "owner-1")
AI_ACTOR = Actor(ActorKind.AI, "agent-1")


def test_preview_edit_loop_rerenders_retests_and_invalidates_prior_approval(
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

    with db.transaction() as conn:
        artifacts = ArtifactRepository(conn).list_by_content_item(item.id)
        pending_req = ApprovalRequest.create(
            item.id, artifacts, requested_by=REVIEWER, clock=lambda: NOW
        )
        ApprovalRequestRepository(conn).add(pending_req)

    edit_service = container.resolve(PreviewEditLoopService)
    result = edit_service.run_edit_loop(
        item.id,
        PreviewEditChanges(
            title="Updated Deterministic Title #Shorts",
            description="Updated deterministic description.\n\n#Shorts",
            tags=("shorts", "deterministic", "updated"),
            rerender_video=True,
            regenerate_thumbnail=True,
        ),
        actor=REVIEWER,
    )

    assert result.previous_video_version == 1
    assert result.new_video_version == 2
    assert result.preview.video.version == 2
    assert pending_req.id in result.invalidated_approval_ids
    assert ArtifactKind.VIDEO in result.comparison.changed_kinds
    assert ArtifactKind.METADATA in result.comparison.changed_kinds

    with db.transaction() as conn:
        reloaded = ApprovalRequestRepository(conn).get(pending_req.id)
        assert reloaded is not None
        assert reloaded.status is ApprovalStatus.INVALIDATED


def test_preview_edit_loop_refuses_ai_actor(tmp_path: Path) -> None:
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

    edit_service = container.resolve(PreviewEditLoopService)
    with pytest.raises(PreviewEditError):
        edit_service.run_edit_loop(
            item.id,
            PreviewEditChanges(title="AI edit attempt"),
            actor=AI_ACTOR,
        )
