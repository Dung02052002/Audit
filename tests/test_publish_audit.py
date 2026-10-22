"""Tests for the publish audit (M-156, prompt #156)."""

import dataclasses
from pathlib import Path

import pytest

from ai_youtube_agent.content.approval_audit import (
    APPROVAL_APPROVED,
    ApprovalAuditService,
)
from ai_youtube_agent.core.audit import AuditLog, AuditResult, InMemoryAuditSink
from ai_youtube_agent.core.content_item import ContentType
from ai_youtube_agent.core.db.database import Database
from ai_youtube_agent.pipeline.publish import PublishJob
from ai_youtube_agent.pipeline.publish_audit import (
    PUBLISH_AUDIT_ACTIONS,
    PUBLISH_REQUESTED,
    PublishAuditNotFoundError,
    PublishAuditRecorder,
    PublishAuditService,
    publish_failed_action,
    published_action,
)
from ai_youtube_agent.pipeline.publish_context import PublishContextLoader
from factories import USER
from publish_helpers import Seed, clock, seed_approved_item

VIDEO_ID = "abcdefghijk"


@pytest.fixture
def seed(database: Database, tmp_path: Path) -> Seed:
    return seed_approved_item(database, tmp_path)


def _context(seed: Seed):
    return PublishContextLoader(
        seed.database, seed.storage, content_type=ContentType.SHORTS
    ).load(seed.item.id)


def _job(seed: Seed, context) -> PublishJob:
    return PublishJob.create(
        seed.approval, ContentType.SHORTS, context.keys.job_key, clock=clock
    ).start(clock=clock)


def test_the_action_names_are_stable() -> None:
    assert published_action("shorts") == "shorts.published"
    assert publish_failed_action("longform") == "longform.publish_failed"
    assert "publish.requested" in PUBLISH_AUDIT_ACTIONS
    assert "longform.published" in PUBLISH_AUDIT_ACTIONS
    assert "shorts.publish_failed" in PUBLISH_AUDIT_ACTIONS


def test_an_item_event_names_the_request_the_approval_and_the_bound_video(
    seed: Seed,
) -> None:
    sink = InMemoryAuditSink()
    context = _context(seed)

    event = PublishAuditRecorder(AuditLog(sink)).item_event(
        PUBLISH_REQUESTED, AuditResult.SUCCESS, context, USER, {"note": "x"}
    )

    assert event.entity.type == "content_item"
    assert event.entity.id == seed.item.id
    assert event.actor == USER
    meta = event.metadata
    assert meta["content_item_id"] == seed.item.id
    assert meta["approval_request_id"] == seed.approval.id
    assert meta["video_artifact_id"] == context.video_artifact.id
    assert meta["video_artifact_sha256"] == context.video_artifact.sha256
    assert meta["job_key"] == context.keys.job_key
    assert meta["upload_key"] == context.keys.upload_key
    assert meta["note"] == "x"
    assert not any("token" in key or "secret" in key for key in meta)


def test_a_job_event_adds_the_attempts_and_statuses(seed: Seed) -> None:
    sink = InMemoryAuditSink()
    context = _context(seed)
    job = _job(seed, context)

    event = PublishAuditRecorder(AuditLog(sink)).job_event(
        published_action("shorts"),
        AuditResult.SUCCESS,
        context,
        USER,
        job,
        "published",
        {"youtube_video_id": VIDEO_ID},
    )

    assert event.entity.type == "publish_job"
    assert event.entity.id == job.id
    assert event.metadata["attempts"] == 1
    assert event.metadata["job_status"] == "in_progress"
    assert event.metadata["item_status"] == "published"
    assert event.metadata["youtube_video_id"] == VIDEO_ID


def test_the_trail_joins_the_approval_the_request_and_the_result(seed: Seed) -> None:
    sink = InMemoryAuditSink()
    log = AuditLog(sink)
    approvals = ApprovalAuditService(seed.database, sink, log)
    recorder = PublishAuditRecorder(log)
    context = _context(seed)
    job = _job(seed, context)

    approvals.record_decision(
        APPROVAL_APPROVED,
        actor=USER,
        approval_request_id=seed.approval.id,
        result=AuditResult.SUCCESS,
        metadata={"content_item_id": seed.item.id},
    )
    recorder.item_event(PUBLISH_REQUESTED, AuditResult.SUCCESS, context, USER)
    recorder.job_event(
        published_action("shorts"),
        AuditResult.SUCCESS,
        context,
        USER,
        job,
        "published",
        {"youtube_video_id": VIDEO_ID},
    )
    # An event of another content item must not appear.
    other_context = dataclasses.replace(
        context, item=dataclasses.replace(context.item, id="another-item")
    )
    recorder.item_event(PUBLISH_REQUESTED, AuditResult.SUCCESS, other_context, USER)

    trail = PublishAuditService(seed.database, sink, approvals).trail_for_content_item(
        seed.item.id
    )

    assert [(entry.source, entry.action) for entry in trail.entries] == [
        ("approval", APPROVAL_APPROVED),
        ("publish", PUBLISH_REQUESTED),
        ("publish", "shorts.published"),
    ]
    assert trail.youtube_video_id == VIDEO_ID
    assert trail.action_counts == {
        APPROVAL_APPROVED: 1,
        PUBLISH_REQUESTED: 1,
        "shorts.published": 1,
    }
    assert trail.as_dict()["total_events"] == 3


def test_an_unknown_item_has_no_trail(seed: Seed) -> None:
    sink = InMemoryAuditSink()
    service = PublishAuditService(
        seed.database, sink, ApprovalAuditService(seed.database, sink, AuditLog(sink))
    )

    with pytest.raises(PublishAuditNotFoundError):
        service.trail_for_content_item("unknown-item")


def test_a_trail_without_a_result_has_no_video_id(seed: Seed) -> None:
    sink = InMemoryAuditSink()
    log = AuditLog(sink)
    PublishAuditRecorder(log).item_event(
        PUBLISH_REQUESTED, AuditResult.SUCCESS, _context(seed), USER
    )

    trail = PublishAuditService(
        seed.database, sink, ApprovalAuditService(seed.database, sink, log)
    ).trail_for_content_item(seed.item.id)

    assert trail.youtube_video_id is None
    assert len(trail.entries) == 1
