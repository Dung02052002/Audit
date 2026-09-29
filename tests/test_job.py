import dataclasses
from datetime import UTC, datetime, timedelta, timezone

import pytest

from ai_youtube_agent.core.audit import Actor, ActorKind
from ai_youtube_agent.core.errors import DomainError
from ai_youtube_agent.pipeline.job import (
    AIJob,
    AIJobStateError,
    AIJobStatus,
    JobCheckpoint,
    Session,
    SessionNotAllowedError,
    SessionStatus,
)

T0 = datetime(2026, 9, 29, 10, 0, tzinfo=UTC)
T1 = T0 + timedelta(minutes=1)
T2 = T1 + timedelta(minutes=1)
T3 = T2 + timedelta(minutes=1)
T4 = T3 + timedelta(minutes=1)
USER = Actor(ActorKind.USER, "owner-1")
KEY = "script.generate:item-1:v1"


def at(moment: datetime):
    return lambda: moment


def rebuild(entity, **changes):
    values = {f.name: getattr(entity, f.name) for f in dataclasses.fields(entity)}
    return type(entity)(**{**values, **changes})


def new_job(**kwargs) -> AIJob:
    return AIJob.create("script.generate", KEY, clock=at(T0), **kwargs)


def running() -> AIJob:
    return new_job().start(clock=at(T1))


def failed() -> AIJob:
    return (
        running()
        .checkpoint("script.outline_saved", {"sections": 3}, clock=at(T2))
        .fail("provider timeout", clock=at(T3))
    )


# Sessions


def test_a_user_starts_a_session() -> None:
    session = Session.start(actor=USER, clock=at(T0))

    assert len(session.id) == 32
    assert session.started_by == USER
    assert session.status is SessionStatus.ACTIVE
    assert session.is_active
    assert session.started_at == T0
    assert session.stopped_at is None


@pytest.mark.parametrize("kind", [ActorKind.AI, ActorKind.SYSTEM])
def test_only_a_user_can_start_a_session(kind: ActorKind) -> None:
    with pytest.raises(SessionNotAllowedError) as info:
        Session.start(actor=Actor(kind, "bot"))
    assert isinstance(info.value, DomainError)
    assert info.value.code == "domain.session_not_allowed"


def test_stop_ends_the_session() -> None:
    session = Session.start(actor=USER, clock=at(T0))

    stopped = session.stop(clock=at(T1))

    assert stopped.status is SessionStatus.STOPPED
    assert not stopped.is_active
    assert stopped.stopped_at == T1
    assert session.is_active


def test_stopping_twice_returns_the_same_session() -> None:
    stopped = Session.start(actor=USER, clock=at(T0)).stop(clock=at(T1))
    assert stopped.stop(clock=at(T2)) is stopped


@pytest.mark.parametrize(
    "changes",
    [
        {"id": ""},
        {"status": "active"},
        {"started_at": datetime(2026, 9, 29, 10, 0)},
        {"stopped_at": T1},
        {"status": SessionStatus.STOPPED},
        {"status": SessionStatus.STOPPED, "stopped_at": T0 - timedelta(seconds=1)},
    ],
)
def test_session_rejects_invalid_state(changes) -> None:
    with pytest.raises((ValueError, TypeError)):
        rebuild(Session.start(actor=USER, clock=at(T0)), **changes)


def test_stored_session_needs_a_user() -> None:
    session = Session.start(actor=USER, clock=at(T0))
    with pytest.raises(SessionNotAllowedError):
        rebuild(session, started_by=Actor(ActorKind.AI, "bot"))


# Creating a job


def test_statuses() -> None:
    assert [s.value for s in AIJobStatus] == [
        "queued",
        "running",
        "waiting",
        "succeeded",
        "failed",
        "cancelled",
    ]


def test_create_queues_a_job() -> None:
    job = new_job(content_item_id="item-1", session_id="session-1")

    assert len(job.id) == 32
    assert job.kind == "script.generate"
    assert job.idempotency_key == KEY
    assert job.content_item_id == "item-1"
    assert job.session_id == "session-1"
    assert job.status is AIJobStatus.QUEUED
    assert job.attempts == 0
    assert job.last_error is None
    assert job.checkpoints == ()
    assert job.last_checkpoint is None
    assert not job.is_final
    assert job.created_at == job.updated_at == T0


def test_item_and_session_are_optional() -> None:
    job = new_job()
    assert (job.content_item_id, job.session_id) == (None, None)


@pytest.mark.parametrize(
    ("kind", "key"),
    [
        ("", KEY),
        ("Script.generate", KEY),
        ("script generate", KEY),
        ("script.", KEY),
        ("script.generate", ""),
        ("script.generate", "has space"),
    ],
)
def test_create_rejects_bad_kind_or_key(kind: str, key: str) -> None:
    with pytest.raises(ValueError):
        AIJob.create(kind, key)


@pytest.mark.parametrize("name", ["content_item_id", "session_id"])
def test_optional_links_must_not_be_empty(name: str) -> None:
    with pytest.raises(ValueError):
        new_job(**{name: " "})


# Attempts


def test_start_begins_an_attempt() -> None:
    job = running()
    assert (job.status, job.attempts, job.updated_at) == (
        AIJobStatus.RUNNING,
        1,
        T1,
    )


def test_fail_keeps_the_reason_and_the_checkpoints() -> None:
    job = failed()

    assert job.status is AIJobStatus.FAILED
    assert job.last_error == "provider timeout"
    assert job.last_checkpoint.step == "script.outline_saved"


def test_a_retry_resumes_after_the_last_checkpoint() -> None:
    job = failed().start(clock=at(T4))

    assert job.status is AIJobStatus.RUNNING
    assert job.attempts == 2
    assert job.last_error is None
    assert job.last_checkpoint.sequence == 1
    assert dict(job.last_checkpoint.data) == {"sections": 3}


def test_succeed_finishes_the_job() -> None:
    job = running().succeed(clock=at(T2))
    assert job.status is AIJobStatus.SUCCEEDED
    assert job.is_final


@pytest.mark.parametrize(
    "job", [new_job, running, failed], ids=["queued", "running", "failed"]
)
def test_an_unfinished_job_can_be_cancelled(job) -> None:
    cancelled = job().cancel(clock=at(T4))
    assert cancelled.status is AIJobStatus.CANCELLED
    assert cancelled.is_final
    assert cancelled.last_error is None


def test_every_change_returns_a_new_job() -> None:
    job = new_job()
    job.start(clock=at(T1))
    assert job.status is AIJobStatus.QUEUED


# Checkpoints


def test_checkpoints_are_appended_in_order() -> None:
    job = (
        running()
        .checkpoint("script.outline_saved", {"sections": 3}, clock=at(T2))
        .checkpoint("script.draft_saved", clock=at(T3))
    )

    assert [c.sequence for c in job.checkpoints] == [1, 2]
    assert [c.step for c in job.checkpoints] == [
        "script.outline_saved",
        "script.draft_saved",
    ]
    assert job.last_checkpoint.created_at == T3
    assert dict(job.last_checkpoint.data) == {}
    assert job.updated_at == T3


def test_checkpoint_data_is_read_only_json_scalars() -> None:
    data = {"sections": 3, "model": "mock", "ok": True, "score": 0.5, "note": None}
    job = running().checkpoint("script.outline_saved", data, clock=at(T2))
    data["sections"] = 99

    assert job.last_checkpoint.data["sections"] == 3
    with pytest.raises(TypeError):
        job.last_checkpoint.data["sections"] = 1  # type: ignore[index]


@pytest.mark.parametrize("data", [{"nested": {"a": 1}}, {"items": [1]}, {1: "x"}])
def test_checkpoint_data_rejects_non_scalars(data) -> None:
    with pytest.raises(ValueError):
        running().checkpoint("script.outline_saved", data, clock=at(T2))


@pytest.mark.parametrize("step", ["", "Script.saved", "script saved", ".saved"])
def test_checkpoint_step_must_be_a_dotted_name(step: str) -> None:
    with pytest.raises(ValueError):
        running().checkpoint(step, clock=at(T2))


# Refused actions


@pytest.mark.parametrize(
    ("job", "action"),
    [
        (new_job, lambda j: j.checkpoint("x.y")),
        (new_job, lambda j: j.fail("x")),
        (new_job, lambda j: j.succeed()),
        (running, lambda j: j.start()),
        (failed, lambda j: j.checkpoint("x.y")),
        (failed, lambda j: j.fail("x")),
        (failed, lambda j: j.succeed()),
    ],
)
def test_actions_outside_a_running_attempt_are_refused(job, action) -> None:
    with pytest.raises(AIJobStateError) as info:
        action(job())
    assert info.value.code == "domain.ai_job_state"


@pytest.mark.parametrize(
    "final",
    [
        lambda: running().succeed(clock=at(T2)),
        lambda: new_job().cancel(clock=at(T1)),
    ],
    ids=["succeeded", "cancelled"],
)
@pytest.mark.parametrize(
    "action",
    [
        lambda j: j.start(clock=at(T4)),
        lambda j: j.checkpoint("x.y", clock=at(T4)),
        lambda j: j.fail("x", clock=at(T4)),
        lambda j: j.succeed(clock=at(T4)),
        lambda j: j.cancel(clock=at(T4)),
    ],
)
def test_succeeded_and_cancelled_are_final(final, action) -> None:
    with pytest.raises(AIJobStateError):
        action(final())


def test_fail_needs_a_reason() -> None:
    with pytest.raises(ValueError):
        running().fail("  ", clock=at(T2))


# Stored state


@pytest.mark.parametrize(
    "changes",
    [
        {"id": ""},
        {"status": "queued"},
        {"attempts": -1},
        {"attempts": True},
        {"attempts": 1},
        {"last_error": "boom"},
        {"checkpoints": [JobCheckpoint(1, "a.b", T0)]},
        {"checkpoints": ("not a checkpoint",)},
        {"created_at": datetime(2026, 9, 29, 10, 0)},
        {"updated_at": T0.astimezone(timezone(timedelta(hours=7)))},
        {"updated_at": T0 - timedelta(seconds=1)},
    ],
)
def test_queued_job_rejects_invalid_state(changes) -> None:
    with pytest.raises((ValueError, TypeError)):
        rebuild(new_job(), **changes)


@pytest.mark.parametrize(
    "status", [AIJobStatus.RUNNING, AIJobStatus.WAITING, AIJobStatus.SUCCEEDED]
)
def test_started_statuses_need_an_attempt(status: AIJobStatus) -> None:
    with pytest.raises(ValueError):
        rebuild(new_job(), status=status)


def test_a_stored_waiting_job_is_valid() -> None:
    assert rebuild(running(), status=AIJobStatus.WAITING).status is AIJobStatus.WAITING


@pytest.mark.parametrize(
    "checkpoints",
    [
        (JobCheckpoint(2, "a.b", T2),),
        (JobCheckpoint(1, "a.b", T2), JobCheckpoint(1, "a.c", T2)),
        (JobCheckpoint(1, "a.b", T2), JobCheckpoint(2, "a.c", T1 + timedelta(0, 30))),
        (JobCheckpoint(1, "a.b", T0 - timedelta(seconds=1)),),
        (JobCheckpoint(1, "a.b", T4),),
    ],
)
def test_checkpoint_history_must_be_consistent(checkpoints) -> None:
    job = rebuild(running(), updated_at=T3)
    with pytest.raises(ValueError):
        rebuild(job, checkpoints=checkpoints)


def test_failed_job_needs_a_last_error() -> None:
    with pytest.raises(ValueError):
        rebuild(failed(), last_error=None)


def test_job_is_frozen() -> None:
    with pytest.raises(dataclasses.FrozenInstanceError):
        new_job().status = AIJobStatus.RUNNING  # type: ignore[misc]


# Serialisation


def test_as_dict_is_json_friendly() -> None:
    job = failed()
    session = Session.start(actor=USER, clock=at(T0)).stop(clock=at(T1))

    assert job.as_dict() == {
        "id": job.id,
        "kind": "script.generate",
        "idempotency_key": KEY,
        "content_item_id": None,
        "session_id": None,
        "status": "failed",
        "attempts": 1,
        "last_error": "provider timeout",
        "checkpoints": [
            {
                "sequence": 1,
                "step": "script.outline_saved",
                "created_at": "2026-09-29T10:02:00+00:00",
                "data": {"sections": 3},
            }
        ],
        "created_at": "2026-09-29T10:00:00+00:00",
        "updated_at": "2026-09-29T10:03:00+00:00",
    }
    assert session.as_dict() == {
        "id": session.id,
        "started_by": {"kind": "user", "id": "owner-1"},
        "status": "stopped",
        "started_at": "2026-09-29T10:00:00+00:00",
        "stopped_at": "2026-09-29T10:01:00+00:00",
    }
