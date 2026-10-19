"""Tests for the publish duplicate guard (M-152, prompt #152)."""

import sqlite3
from pathlib import Path

import pytest

from ai_youtube_agent.core.artifact import ArtifactKind
from ai_youtube_agent.core.content_item import ContentStatus, ContentType
from ai_youtube_agent.core.db.database import ConcurrencyError, Database
from ai_youtube_agent.core.db.repositories.content import (
    ArtifactRepository,
    ContentItemRepository,
)
from ai_youtube_agent.core.db.repositories.publish import PublishJobRepository
from ai_youtube_agent.core.db.repositories.review import ApprovalRequestRepository
from ai_youtube_agent.pipeline.duplicate_guard import (
    DuplicateDecision,
    DuplicateErrorCode,
    DuplicatePublishError,
    PublishDuplicateGuard,
    concurrent_publish,
)
from ai_youtube_agent.pipeline.idempotency import derive_publish_keys, publish_key
from ai_youtube_agent.pipeline.publish import PublishJob, PublishStatus
from factories import (
    USER,
    at,
    make_approval_request,
    make_artifact,
    make_content_item,
)
from publish_helpers import Seed, seed_approved_item

VIDEO_ID = "abcdefghijk"


def _keys(seed: Seed):
    video = seed.artifacts[ArtifactKind.VIDEO]
    return derive_publish_keys(
        seed.item.id,
        seed.approval.id,
        seed.channel.youtube.channel_id,
        video.sha256,
    )


def _add_job(seed: Seed, job: PublishJob) -> None:
    with seed.database.transaction() as connection:
        PublishJobRepository(connection).add(job)


def _job(seed: Seed, *, status: PublishStatus, key: str | None = None, approval=None):
    approval = approval or seed.approval
    job = PublishJob.create(
        approval, seed.item.content_type, key or _keys(seed).job_key, clock=at()
    )
    if status is PublishStatus.QUEUED:
        return job
    job = job.start(clock=at())
    if status is PublishStatus.FAILED:
        return job.fail("youtube_upload.timeout", clock=at())
    if status is PublishStatus.SUCCEEDED:
        return job.succeed(VIDEO_ID, clock=at())
    return job


def _check(seed: Seed, database: Database):
    return PublishDuplicateGuard(database).check(
        content_item_id=seed.item.id,
        channel_id=seed.channel.id,
        video_sha256=seed.artifacts[ArtifactKind.VIDEO].sha256,
        keys=_keys(seed),
    )


@pytest.fixture
def seed(database: Database, tmp_path: Path) -> Seed:
    return seed_approved_item(database, tmp_path)


def test_a_new_publish_proceeds(seed: Seed, database: Database) -> None:
    result = _check(seed, database)

    assert result.decision is DuplicateDecision.PROCEED
    assert result.job is None


def test_a_failed_job_is_resumed(seed: Seed, database: Database) -> None:
    failed = _job(seed, status=PublishStatus.FAILED)
    _add_job(seed, failed)

    result = _check(seed, database)

    assert result.decision is DuplicateDecision.RESUME
    assert result.job is not None and result.job.id == failed.id


def test_a_succeeded_job_is_replayed(seed: Seed, database: Database) -> None:
    done = _job(seed, status=PublishStatus.SUCCEEDED)
    _add_job(seed, done)

    result = _check(seed, database)

    assert result.decision is DuplicateDecision.REPLAY
    assert result.job is not None and result.job.result is not None
    assert result.job.result.youtube_video_id == VIDEO_ID


@pytest.mark.parametrize("status", [PublishStatus.QUEUED, PublishStatus.IN_PROGRESS])
def test_a_running_job_blocks_the_request(
    seed: Seed, database: Database, status: PublishStatus
) -> None:
    _add_job(seed, _job(seed, status=status))

    with pytest.raises(DuplicatePublishError) as caught:
        _check(seed, database)

    assert caught.value.duplicate_code is DuplicateErrorCode.IN_FLIGHT
    assert caught.value.to_public().http_status == 409


def test_another_approval_cannot_publish_an_item_twice(
    seed: Seed, database: Database
) -> None:
    second = make_approval_request(
        seed.item, list(seed.artifacts.values())
    ).approve(USER)
    with database.transaction() as connection:
        ApprovalRequestRepository(connection).add(second)
    other = _job(
        seed,
        status=PublishStatus.SUCCEEDED,
        key=publish_key(seed.item.id, second.id),
        approval=second,
    )
    _add_job(seed, other)

    with pytest.raises(DuplicatePublishError) as caught:
        _check(seed, database)

    assert caught.value.duplicate_code is DuplicateErrorCode.ALREADY_PUBLISHED


def test_the_same_video_bytes_are_not_published_twice_on_a_channel(
    seed: Seed, database: Database
) -> None:
    twin = make_content_item(seed.channel, seed.profile, status=ContentStatus.APPROVED)
    video = seed.artifacts[ArtifactKind.VIDEO]
    twin_video = make_artifact(
        twin, ArtifactKind.VIDEO, sha256=video.sha256, size_bytes=video.size_bytes
    )
    twin_approval = make_approval_request(twin, [twin_video]).approve(USER)
    with database.transaction() as connection:
        ContentItemRepository(connection).add(twin)
        ArtifactRepository(connection).add(twin_video)
        ApprovalRequestRepository(connection).add(twin_approval)
        twin_job = PublishJob.create(
            twin_approval,
            ContentType.SHORTS,
            publish_key(twin.id, twin_approval.id),
            clock=at(),
        )
        PublishJobRepository(connection).add(
            twin_job.start(clock=at()).succeed(VIDEO_ID, clock=at())
        )

    with pytest.raises(DuplicatePublishError) as caught:
        _check(seed, database)

    assert caught.value.duplicate_code is DuplicateErrorCode.SAME_VIDEO_PUBLISHED


def test_a_failed_job_of_another_item_with_the_same_video_does_not_block(
    seed: Seed, database: Database
) -> None:
    twin = make_content_item(seed.channel, seed.profile, status=ContentStatus.APPROVED)
    video = seed.artifacts[ArtifactKind.VIDEO]
    twin_video = make_artifact(
        twin, ArtifactKind.VIDEO, sha256=video.sha256, size_bytes=video.size_bytes
    )
    twin_approval = make_approval_request(twin, [twin_video]).approve(USER)
    with database.transaction() as connection:
        ContentItemRepository(connection).add(twin)
        ArtifactRepository(connection).add(twin_video)
        ApprovalRequestRepository(connection).add(twin_approval)
        twin_job = PublishJob.create(
            twin_approval,
            ContentType.SHORTS,
            publish_key(twin.id, twin_approval.id),
            clock=at(),
        )
        PublishJobRepository(connection).add(
            twin_job.start(clock=at()).fail("youtube_upload.invalid_payload", clock=at())
        )

    assert _check(seed, database).decision is DuplicateDecision.PROCEED


def test_the_guard_writes_nothing(seed: Seed, database: Database) -> None:
    _check(seed, database)

    with database.transaction() as connection:
        assert PublishJobRepository(connection).list_by_content_item(seed.item.id) == []


def test_a_lost_race_becomes_a_typed_duplicate_error() -> None:
    for error in (sqlite3.IntegrityError("UNIQUE"), ConcurrencyError("stale")):
        mapped = concurrent_publish(error, "item-1")
        assert mapped.duplicate_code is DuplicateErrorCode.CONCURRENT
        assert mapped.to_public().http_status == 409

    with pytest.raises(TypeError):
        concurrent_publish(RuntimeError("other"), "item-1")
