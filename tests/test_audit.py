import dataclasses
import logging
from datetime import UTC, datetime, timedelta, timezone

import pytest

from ai_youtube_agent.bootstrap import build_container
from ai_youtube_agent.core.audit import (
    Actor,
    ActorKind,
    AuditError,
    AuditEvent,
    AuditLog,
    AuditResult,
    AuditSink,
    EntityRef,
    InMemoryAuditSink,
)
from ai_youtube_agent.core.config import Environment, Settings
from ai_youtube_agent.core.log import log_context

NOW = datetime(2026, 9, 28, 10, 0, tzinfo=UTC)
OWNER = Actor(ActorKind.USER, "owner")
VIDEO = EntityRef("video", "v-42")


def clock() -> datetime:
    return NOW


def event(**changes: object) -> AuditEvent:
    values: dict[str, object] = {
        "event_id": "e-1",
        "timestamp": NOW,
        "action": "approval.decided",
        "actor": OWNER,
        "entity": VIDEO,
        "result": AuditResult.SUCCESS,
    }
    values.update(changes)
    return AuditEvent(**values)  # type: ignore[arg-type]


# Event model


def test_create_fills_id_time_and_fields() -> None:
    created = AuditEvent.create(
        "approval.decided",
        OWNER,
        VIDEO,
        AuditResult.SUCCESS,
        {"reason": "looks good"},
        clock=clock,
    )

    assert created.event_id
    assert created.timestamp == NOW
    assert created.actor == OWNER
    assert created.entity == VIDEO
    assert created.result is AuditResult.SUCCESS
    assert created.metadata == {"reason": "looks good"}


def test_create_uses_utc_now_by_default() -> None:
    created = AuditEvent.create("job.started", OWNER, VIDEO, AuditResult.SUCCESS)

    assert created.timestamp.tzinfo is UTC
    assert abs(datetime.now(UTC) - created.timestamp) < timedelta(seconds=5)


def test_create_gives_every_event_a_new_id() -> None:
    first = AuditEvent.create("job.started", OWNER, VIDEO, AuditResult.SUCCESS)
    second = AuditEvent.create("job.started", OWNER, VIDEO, AuditResult.SUCCESS)

    assert first.event_id != second.event_id


def test_create_takes_ids_from_the_log_context() -> None:
    with log_context(correlation_id="c-1", session_id="s-1", job_id="j-1"):
        created = AuditEvent.create("job.started", OWNER, VIDEO, AuditResult.SUCCESS)

    assert (created.correlation_id, created.session_id, created.job_id) == (
        "c-1",
        "s-1",
        "j-1",
    )


def test_event_is_frozen() -> None:
    recorded = event()

    with pytest.raises(dataclasses.FrozenInstanceError):
        recorded.result = AuditResult.FAILURE  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        recorded.actor.id = "someone-else"  # type: ignore[misc]


def test_metadata_is_read_only_and_copied() -> None:
    source = {"reason": "ok"}
    recorded = event(metadata=source)

    source["reason"] = "changed"
    with pytest.raises(TypeError):
        recorded.metadata["reason"] = "changed"  # type: ignore[index]
    assert recorded.metadata == {"reason": "ok"}


@pytest.mark.parametrize("value", [[1, 2], {"a": 1}, {1, 2}, object()])
def test_metadata_rejects_mutable_or_unknown_values(value: object) -> None:
    with pytest.raises(ValueError):
        event(metadata={"x": value})


def test_metadata_rejects_non_string_keys() -> None:
    with pytest.raises(ValueError):
        event(metadata={1: "x"})


@pytest.mark.parametrize(
    "timestamp",
    [
        datetime(2026, 9, 28, 10, 0),
        datetime(2026, 9, 28, 17, 0, tzinfo=timezone(timedelta(hours=7))),
    ],
)
def test_timestamp_must_be_utc(timestamp: datetime) -> None:
    with pytest.raises(ValueError):
        event(timestamp=timestamp)


@pytest.mark.parametrize(
    "action", ["", "approval", "Approval.decided", "approval..decided", "a.b-c"]
)
def test_action_must_be_a_dotted_name(action: str) -> None:
    with pytest.raises(ValueError):
        event(action=action)


def test_empty_ids_are_rejected() -> None:
    with pytest.raises(ValueError):
        Actor(ActorKind.AI, "")
    with pytest.raises(ValueError):
        EntityRef("", "v-1")
    with pytest.raises(ValueError):
        EntityRef("video", "")
    with pytest.raises(ValueError):
        event(event_id="")


def test_as_dict() -> None:
    recorded = event(
        actor=Actor(ActorKind.AI, "script-writer"),
        result=AuditResult.DENIED,
        correlation_id="c-1",
        metadata={"gate": "approval"},
    )

    assert recorded.as_dict() == {
        "event_id": "e-1",
        "timestamp": "2026-09-28T10:00:00+00:00",
        "action": "approval.decided",
        "actor": {"kind": "ai", "id": "script-writer"},
        "entity": {"type": "video", "id": "v-42"},
        "result": "denied",
        "correlation_id": "c-1",
        "session_id": None,
        "job_id": None,
        "metadata": {"gate": "approval"},
    }


# Sink


def test_in_memory_sink_keeps_order_and_is_an_audit_sink() -> None:
    sink = InMemoryAuditSink()
    first, second = event(event_id="e-1"), event(event_id="e-2")

    sink.append(first)
    sink.append(second)

    assert isinstance(sink, AuditSink)
    assert sink.events() == (first, second)


def test_in_memory_sink_rejects_a_recorded_event_id() -> None:
    sink = InMemoryAuditSink()
    sink.append(event(event_id="e-1"))

    with pytest.raises(AuditError):
        sink.append(event(event_id="e-1", result=AuditResult.FAILURE))
    assert len(sink.events()) == 1


def test_events_returns_a_snapshot() -> None:
    sink = InMemoryAuditSink()
    snapshot = sink.events()

    sink.append(event())

    assert snapshot == ()
    assert len(sink.events()) == 1


# Audit log


def test_record_appends_to_the_sink_and_returns_the_event() -> None:
    sink = InMemoryAuditSink()
    audit = AuditLog(sink, clock=clock)

    recorded = audit.record("publish.requested", OWNER, VIDEO, AuditResult.DENIED)

    assert sink.events() == (recorded,)
    assert recorded.timestamp == NOW
    assert recorded.result is AuditResult.DENIED


def test_record_writes_a_structured_log(caplog: pytest.LogCaptureFixture) -> None:
    audit = AuditLog(InMemoryAuditSink(), clock=clock)

    with caplog.at_level(logging.INFO, logger="ai_youtube_agent.core.audit"):
        recorded = audit.record("job.started", OWNER, VIDEO, AuditResult.SUCCESS)

    [record] = caplog.records
    assert record.getMessage() == "audit event"
    assert record.fields == {"audit": recorded.as_dict()}


def test_container_provides_one_audit_log_and_sink() -> None:
    container = build_container(Settings(environment=Environment.TEST))

    audit = container.resolve(AuditLog)
    recorded = audit.record("job.started", OWNER, VIDEO, AuditResult.SUCCESS)

    assert container.resolve(AuditLog) is audit
    assert container.resolve(AuditSink).events() == (recorded,)
