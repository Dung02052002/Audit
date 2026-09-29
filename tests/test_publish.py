import dataclasses
from datetime import UTC, datetime, timedelta, timezone

import pytest

from ai_youtube_agent.content.approval import ApprovalRequest, ApprovalStatus
from ai_youtube_agent.core.artifact import Artifact, ArtifactKind
from ai_youtube_agent.core.audit import Actor, ActorKind
from ai_youtube_agent.core.content_item import ContentType
from ai_youtube_agent.core.errors import DomainError
from ai_youtube_agent.pipeline.publish import (
    PublishJob,
    PublishJobStateError,
    PublishNotApprovedError,
    PublishResult,
    PublishStatus,
)

T0 = datetime(2026, 9, 29, 10, 0, tzinfo=UTC)
T1 = T0 + timedelta(minutes=1)
T2 = T1 + timedelta(minutes=1)
T3 = T2 + timedelta(minutes=1)
VIDEO_ID = "dQw4w9WgXcQ"
KEY = "publish:item-1:approval-1"

VIDEO = Artifact.create(
    "item-1",
    ArtifactKind.VIDEO,
    uri="store://item-1/video",
    sha256="a" * 64,
    size_bytes=100,
    media_type="video/mp4",
    clock=lambda: T0,
)
PENDING = ApprovalRequest.create(
    "item-1", [VIDEO], requested_by=Actor(ActorKind.SYSTEM, "pipeline")
)
APPROVED = dataclasses.replace(PENDING, status=ApprovalStatus.APPROVED)


def at(moment: datetime):
    return lambda: moment


def new_job(content_type: ContentType = ContentType.SHORTS) -> PublishJob:
    return PublishJob.create(APPROVED, content_type, KEY, clock=at(T0))


def rebuild(entity, **changes):
    values = {f.name: getattr(entity, f.name) for f in dataclasses.fields(entity)}
    return type(entity)(**{**values, **changes})


def succeeded_job() -> PublishJob:
    return new_job().start(clock=at(T1)).succeed(VIDEO_ID, clock=at(T2))


def failed_job() -> PublishJob:
    return new_job().start(clock=at(T1)).fail("upload timed out", clock=at(T2))


# Statuses


def test_statuses() -> None:
    assert [s.value for s in PublishStatus] == [
        "queued",
        "in_progress",
        "succeeded",
        "failed",
    ]


# Creating a job (R-08)


@pytest.mark.parametrize("content_type", list(ContentType))
def test_create_queues_a_job_for_an_approved_item(content_type) -> None:
    job = new_job(content_type)

    assert len(job.id) == 32
    assert job.idempotency_key == KEY
    assert job.content_item_id == "item-1"
    assert job.approval_request_id == APPROVED.id
    assert job.content_type is content_type
    assert job.status is PublishStatus.QUEUED
    assert job.attempts == 0
    assert job.last_error is None
    assert job.result is None
    assert job.created_at == job.updated_at == T0


@pytest.mark.parametrize(
    "status", [s for s in ApprovalStatus if s is not ApprovalStatus.APPROVED]
)
def test_create_refuses_an_approval_that_is_not_approved(status) -> None:
    approval = dataclasses.replace(PENDING, status=status)
    with pytest.raises(PublishNotApprovedError):
        PublishJob.create(approval, ContentType.SHORTS, KEY)


def test_refusal_is_a_domain_error_with_a_safe_message() -> None:
    with pytest.raises(DomainError) as info:
        PublishJob.create(PENDING, ContentType.SHORTS, KEY)
    assert info.value.code == "domain.publish_not_approved"
    assert info.value.user_message == "This video has no valid approval to publish."


def test_the_job_always_publishes_the_approved_item() -> None:
    other = dataclasses.replace(APPROVED, content_item_id="item-2")
    assert PublishJob.create(other, ContentType.SHORTS, KEY).content_item_id == "item-2"


@pytest.mark.parametrize("key", ["", "has space", "tab\tkey"])
def test_idempotency_key_must_be_a_single_token(key: str) -> None:
    with pytest.raises(ValueError):
        PublishJob.create(APPROVED, ContentType.SHORTS, key)


def test_content_type_must_be_a_content_type() -> None:
    with pytest.raises(TypeError):
        PublishJob.create(APPROVED, "shorts", KEY)  # type: ignore[arg-type]


def test_create_defaults_to_the_current_utc_time() -> None:
    before = datetime.now(UTC)
    job = PublishJob.create(APPROVED, ContentType.SHORTS, KEY)
    assert before <= job.created_at <= datetime.now(UTC)


# Attempts


def test_start_begins_the_first_attempt() -> None:
    job = new_job().start(clock=at(T1))
    assert job.status is PublishStatus.IN_PROGRESS
    assert job.attempts == 1
    assert job.updated_at == T1


def test_fail_ends_an_attempt_with_a_reason() -> None:
    job = failed_job()
    assert job.status is PublishStatus.FAILED
    assert job.attempts == 1
    assert job.last_error == "upload timed out"
    assert job.updated_at == T2


def test_a_failed_job_can_start_again() -> None:
    job = failed_job().start(clock=at(T3))
    assert job.status is PublishStatus.IN_PROGRESS
    assert job.attempts == 2
    assert job.last_error is None


def test_succeed_records_the_result() -> None:
    job = succeeded_job()

    assert job.status is PublishStatus.SUCCEEDED
    assert job.attempts == 1
    assert job.result == PublishResult(job.id, VIDEO_ID, T2)
    assert job.updated_at == T2


def test_succeeding_after_a_retry_keeps_the_count() -> None:
    job = failed_job().start(clock=at(T3)).succeed(VIDEO_ID, clock=at(T3))
    assert (job.status, job.attempts) == (PublishStatus.SUCCEEDED, 2)


def test_every_change_returns_a_new_job() -> None:
    job = new_job()
    job.start(clock=at(T1))
    assert (job.status, job.attempts) == (PublishStatus.QUEUED, 0)


# Idempotency: a succeeded job is final


@pytest.mark.parametrize(
    "action",
    [
        lambda job: job.start(clock=at(T3)),
        lambda job: job.succeed("abcdefghijk", clock=at(T3)),
        lambda job: job.fail("late error", clock=at(T3)),
    ],
)
def test_a_succeeded_job_cannot_run_again(action) -> None:
    with pytest.raises(PublishJobStateError):
        action(succeeded_job())


@pytest.mark.parametrize(
    ("job", "action"),
    [
        (new_job, lambda job: job.fail("x")),
        (new_job, lambda job: job.succeed(VIDEO_ID)),
        (lambda: new_job().start(clock=at(T1)), lambda job: job.start()),
        (failed_job, lambda job: job.fail("x")),
        (failed_job, lambda job: job.succeed(VIDEO_ID)),
    ],
)
def test_actions_outside_an_attempt_are_refused(job, action) -> None:
    with pytest.raises(PublishJobStateError) as info:
        action(job())
    assert info.value.code == "domain.publish_job_state"


def test_fail_needs_a_reason() -> None:
    with pytest.raises(ValueError):
        new_job().start(clock=at(T1)).fail("  ", clock=at(T2))


@pytest.mark.parametrize("video_id", ["", "short", "dQw4w9WgXcQx", "dQw4w9WgX c"])
def test_succeed_needs_a_youtube_video_id(video_id: str) -> None:
    with pytest.raises(ValueError):
        new_job().start(clock=at(T1)).succeed(video_id, clock=at(T2))


# Stored state


@pytest.mark.parametrize(
    "changes",
    [
        {"id": ""},
        {"approval_request_id": " "},
        {"status": "queued"},
        {"attempts": -1},
        {"attempts": True},
        {"attempts": 1},
        {"last_error": "boom"},
        {"result": PublishResult("x", VIDEO_ID, T0)},
        {"created_at": datetime(2026, 9, 29, 10, 0)},
        {"updated_at": T0.astimezone(timezone(timedelta(hours=7)))},
        {"updated_at": T0 - timedelta(seconds=1)},
    ],
)
def test_queued_job_rejects_invalid_state(changes) -> None:
    with pytest.raises((ValueError, TypeError)):
        rebuild(new_job(), **changes)


@pytest.mark.parametrize(
    "changes",
    [
        {"result": None},
        {"result": PublishResult("other-job", VIDEO_ID, T2)},
        {"attempts": 0},
        {"last_error": "boom"},
    ],
)
def test_succeeded_job_rejects_invalid_state(changes) -> None:
    with pytest.raises(ValueError):
        rebuild(succeeded_job(), **changes)


def test_succeeded_job_result_must_fall_within_its_lifetime() -> None:
    job = succeeded_job()
    early = PublishResult(job.id, VIDEO_ID, T0 - timedelta(seconds=1))
    with pytest.raises(ValueError):
        rebuild(job, result=early)


def test_failed_job_needs_a_last_error() -> None:
    with pytest.raises(ValueError):
        rebuild(failed_job(), last_error=None)


def test_job_and_result_are_frozen() -> None:
    job = succeeded_job()
    with pytest.raises(dataclasses.FrozenInstanceError):
        job.status = PublishStatus.QUEUED  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        job.result.youtube_video_id = "x"  # type: ignore[misc, union-attr]


@pytest.mark.parametrize(
    "changes",
    [{"job_id": ""}, {"published_at": datetime(2026, 9, 29, 10, 0)}],
)
def test_result_rejects_invalid_state(changes) -> None:
    with pytest.raises(ValueError):
        rebuild(PublishResult("job-1", VIDEO_ID, T0), **changes)


# Serialisation


def test_as_dict_is_json_friendly() -> None:
    job = succeeded_job()

    assert job.as_dict() == {
        "id": job.id,
        "idempotency_key": KEY,
        "content_item_id": "item-1",
        "approval_request_id": APPROVED.id,
        "content_type": "shorts",
        "status": "succeeded",
        "attempts": 1,
        "last_error": None,
        "result": {
            "job_id": job.id,
            "youtube_video_id": VIDEO_ID,
            "published_at": "2026-09-29T10:02:00+00:00",
        },
        "created_at": "2026-09-29T10:00:00+00:00",
        "updated_at": "2026-09-29T10:02:00+00:00",
    }
    assert new_job().as_dict()["result"] is None
