"""C-041 Idempotency Gate (Prompt Pack v8, prompt #041).

Rules the user approved on 2026-10-01:

- a move into ``generating`` uses ``generation_key(item)`` (kind, item id,
  status, updated_at); the move to ``publishing`` uses ``publish_key`` (item id
  and the approved newest request id);
- a stored job with that key blocks unless it failed (cancelled blocks too);
- without an approved newest request there is no publish key and the gate
  passes, because the approval gate blocks that move;
- other moves pass.
"""

import dataclasses
from datetime import UTC, datetime, timedelta

import pytest

from ai_youtube_agent.content.approval import ApprovalRequest, ApprovalStatus
from ai_youtube_agent.core.artifact import Artifact, ArtifactKind
from ai_youtube_agent.core.audit import Actor, ActorKind
from ai_youtube_agent.core.content_item import (
    ContentItem,
    ContentStatus,
    ContentType,
)
from ai_youtube_agent.core.db.database import Database
from ai_youtube_agent.core.db.repositories.channel import (
    ChannelRepository,
    StrategyProfileRepository,
)
from ai_youtube_agent.core.db.repositories.content import (
    ArtifactRepository,
    ContentItemRepository,
)
from ai_youtube_agent.core.db.repositories.jobs import AIJobRepository
from ai_youtube_agent.core.db.repositories.publish import PublishJobRepository
from ai_youtube_agent.core.db.repositories.review import ApprovalRequestRepository
from ai_youtube_agent.core.gates import (
    GateContext,
    GateName,
    GateOutcome,
    PipelineGate,
    evaluate_gates,
)
from ai_youtube_agent.core.idempotency_gate import IdempotencyGate
from ai_youtube_agent.pipeline.idempotency import (
    GENERATION_KIND,
    generation_key,
    publish_key,
)
from ai_youtube_agent.pipeline.job import AIJob, AIJobStatus
from ai_youtube_agent.pipeline.publish import PublishJob, PublishStatus
from factories import make_channel, make_strategy_profile

T0 = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
SYSTEM = Actor(ActorKind.SYSTEM, "pipeline")


class Store:
    """In-memory stand-in for the three repositories the gate reads."""

    def __init__(self) -> None:
        self.jobs: dict[str, AIJob] = {}
        self.publishes: dict[str, PublishJob] = {}
        self.approvals: list[ApprovalRequest] = []


class Jobs:
    def __init__(self, store: Store) -> None:
        self.store = store

    def get_by_idempotency_key(self, key: str) -> AIJob | None:
        return self.store.jobs.get(key)


class Publishes:
    def __init__(self, store: Store) -> None:
        self.store = store

    def get_by_idempotency_key(self, key: str) -> PublishJob | None:
        return self.store.publishes.get(key)


class Approvals:
    def __init__(self, store: Store) -> None:
        self.store = store

    def list_by_content_item(self, content_item_id: str) -> list[ApprovalRequest]:
        return [a for a in self.store.approvals if a.content_item_id == content_item_id]


def at(moment: datetime):
    return lambda: moment


def item_in(status: ContentStatus, item_id: str | None = None) -> ContentItem:
    item = ContentItem.create(
        "channel-1", "strategy-1", 1, ContentType.SHORTS, "Video", clock=at(T0)
    )
    return dataclasses.replace(item, status=status, id=item_id or item.id)


def video(item: ContentItem) -> Artifact:
    return Artifact.create(
        item.id,
        ArtifactKind.VIDEO,
        uri="store://video/1",
        sha256="1" * 64,
        size_bytes=10,
        media_type="video/mp4",
        clock=at(T0),
    )


def approval(
    item: ContentItem,
    status: ApprovalStatus = ApprovalStatus.APPROVED,
    minutes: int = 0,
) -> ApprovalRequest:
    made = ApprovalRequest.create(
        item.id,
        [video(item)],
        requested_by=SYSTEM,
        clock=at(T0 + timedelta(minutes=minutes)),
    )
    return dataclasses.replace(made, status=status)


def ai_job(key: str, status: AIJobStatus, item: ContentItem) -> AIJob:
    job = AIJob.create(GENERATION_KIND, key, content_item_id=item.id, clock=at(T0))
    if status is AIJobStatus.QUEUED:
        return job
    if status is AIJobStatus.CANCELLED:
        return job.cancel(clock=at(T0))
    running = job.start(clock=at(T0))
    return {
        AIJobStatus.RUNNING: running,
        AIJobStatus.WAITING: dataclasses.replace(running, status=AIJobStatus.WAITING),
        AIJobStatus.SUCCEEDED: running.succeed(clock=at(T0)),
        AIJobStatus.FAILED: running.fail("provider down", clock=at(T0)),
    }[status]


def publish_job(
    key: str, status: PublishStatus, request: ApprovalRequest
) -> PublishJob:
    job = PublishJob.create(request, ContentType.SHORTS, key, clock=at(T0))
    if status is PublishStatus.QUEUED:
        return job
    running = job.start(clock=at(T0))
    return {
        PublishStatus.IN_PROGRESS: running,
        PublishStatus.SUCCEEDED: running.succeed("abcdefghijk", clock=at(T0)),
        PublishStatus.FAILED: running.fail("upload error", clock=at(T0)),
    }[status]


@pytest.fixture
def store() -> Store:
    return Store()


@pytest.fixture
def gate(store: Store) -> IdempotencyGate:
    return IdempotencyGate(Jobs(store), Publishes(store), Approvals(store))


def context(item: ContentItem, target: ContentStatus) -> GateContext:
    return GateContext(item=item, target_status=target, actor=SYSTEM, at=T0)


def codes(result) -> list[str]:
    return [r.code for r in result.reasons]


# Keys


def test_the_generation_key_is_deterministic_and_prefixed() -> None:
    item = item_in(ContentStatus.DRAFT, "item-1")

    key = generation_key(item)

    assert key == generation_key(dataclasses.replace(item))
    assert key.startswith("gen:")
    assert len(key) == len("gen:") + 64
    assert not any(c.isspace() for c in key)


@pytest.mark.parametrize(
    "change",
    [
        {"id": "item-2"},
        {"status": ContentStatus.REJECTED},
        {"updated_at": T0 + timedelta(seconds=1)},
    ],
)
def test_the_generation_key_changes_with_the_item_state(change) -> None:
    item = item_in(ContentStatus.DRAFT, "item-1")

    assert generation_key(item) != generation_key(dataclasses.replace(item, **change))


def test_the_generation_key_depends_on_the_kind() -> None:
    item = item_in(ContentStatus.DRAFT, "item-1")

    assert generation_key(item) == generation_key(item, GENERATION_KIND)
    assert generation_key(item) != generation_key(item, "script.generate")


def test_the_publish_key_is_deterministic_per_item_and_approval() -> None:
    key = publish_key("item-1", "approval-1")

    assert key == publish_key("item-1", "approval-1")
    assert key.startswith("pub:")
    assert key != publish_key("item-1", "approval-2")
    assert key != publish_key("item-2", "approval-1")


def test_key_parts_cannot_collide_through_separators() -> None:
    assert publish_key("a,b", "c") != publish_key("a", "b,c")
    with pytest.raises(ValueError):
        publish_key(" ", "approval-1")


def test_the_keys_are_valid_job_keys() -> None:
    item = item_in(ContentStatus.DRAFT)
    request = approval(item)

    ai_job(generation_key(item), AIJobStatus.QUEUED, item)
    publish_job(publish_key(item.id, request.id), PublishStatus.QUEUED, request)


# Contract


def test_the_gate_follows_the_contract(gate: IdempotencyGate) -> None:
    assert isinstance(gate, PipelineGate)
    assert gate.name is GateName.IDEMPOTENCY


# Generation


def test_generation_passes_without_a_job(gate: IdempotencyGate) -> None:
    result = gate.evaluate(
        context(item_in(ContentStatus.DRAFT), ContentStatus.GENERATING)
    )

    assert result.outcome is GateOutcome.PASS
    assert result.gate is GateName.IDEMPOTENCY
    assert result.evaluated_at == T0


@pytest.mark.parametrize(
    "status", [s for s in AIJobStatus if s is not AIJobStatus.FAILED]
)
def test_generation_blocks_on_an_existing_job(
    store: Store, gate: IdempotencyGate, status: AIJobStatus
) -> None:
    item = item_in(ContentStatus.DRAFT)
    key = generation_key(item)
    store.jobs[key] = ai_job(key, status, item)

    result = gate.evaluate(context(item, ContentStatus.GENERATING))

    assert codes(result) == ["idempotency.duplicate_generation"]
    assert status.value in result.reasons[0].message


def test_generation_passes_on_a_failed_job_so_it_can_retry(
    store: Store, gate: IdempotencyGate
) -> None:
    item = item_in(ContentStatus.DRAFT)
    key = generation_key(item)
    store.jobs[key] = ai_job(key, AIJobStatus.FAILED, item)

    assert gate.evaluate(context(item, ContentStatus.GENERATING)).is_passed


def test_a_regeneration_from_a_new_state_gets_a_new_key(
    store: Store, gate: IdempotencyGate
) -> None:
    first = item_in(ContentStatus.DRAFT)
    store.jobs[generation_key(first)] = ai_job(
        generation_key(first), AIJobStatus.SUCCEEDED, first
    )
    later = dataclasses.replace(
        first, status=ContentStatus.PREVIEW_READY, updated_at=T0 + timedelta(hours=1)
    )

    assert gate.evaluate(context(later, ContentStatus.GENERATING)).is_passed


# Publishing


def test_publish_passes_without_a_job(store: Store, gate: IdempotencyGate) -> None:
    item = item_in(ContentStatus.APPROVED)
    store.approvals.append(approval(item))

    assert gate.evaluate(context(item, ContentStatus.PUBLISHING)).is_passed


@pytest.mark.parametrize(
    "status", [s for s in PublishStatus if s is not PublishStatus.FAILED]
)
def test_publish_blocks_on_an_existing_job(
    store: Store, gate: IdempotencyGate, status: PublishStatus
) -> None:
    item = item_in(ContentStatus.APPROVED)
    request = approval(item)
    store.approvals.append(request)
    key = publish_key(item.id, request.id)
    store.publishes[key] = publish_job(key, status, request)

    result = gate.evaluate(context(item, ContentStatus.PUBLISHING))

    assert codes(result) == ["idempotency.duplicate_publish"]
    assert status.value in result.reasons[0].message


def test_publish_passes_on_a_failed_job_so_it_can_retry(
    store: Store, gate: IdempotencyGate
) -> None:
    item = item_in(ContentStatus.APPROVED)
    request = approval(item)
    store.approvals.append(request)
    key = publish_key(item.id, request.id)
    store.publishes[key] = publish_job(key, PublishStatus.FAILED, request)

    assert gate.evaluate(context(item, ContentStatus.PUBLISHING)).is_passed


def test_a_new_approval_gets_a_new_publish_key(
    store: Store, gate: IdempotencyGate
) -> None:
    item = item_in(ContentStatus.APPROVED)
    old = approval(item, minutes=0)
    key = publish_key(item.id, old.id)
    store.publishes[key] = publish_job(key, PublishStatus.SUCCEEDED, old)
    store.approvals += [old, approval(item, minutes=5)]

    assert gate.evaluate(context(item, ContentStatus.PUBLISHING)).is_passed


@pytest.mark.parametrize("approved", [False, True])
def test_without_an_approved_newest_request_the_gate_passes(
    store: Store, gate: IdempotencyGate, approved: bool
) -> None:
    item = item_in(ContentStatus.APPROVED)
    if approved:
        # An older approval with a publish job, then a newer pending request.
        old = approval(item, minutes=0)
        key = publish_key(item.id, old.id)
        store.publishes[key] = publish_job(key, PublishStatus.SUCCEEDED, old)
        store.approvals += [old, approval(item, ApprovalStatus.PENDING, minutes=5)]

    assert gate.evaluate(context(item, ContentStatus.PUBLISHING)).is_passed


# Other moves


@pytest.mark.parametrize(
    "source, target",
    [
        (ContentStatus.GENERATING, ContentStatus.TESTING),
        (ContentStatus.AWAITING_APPROVAL, ContentStatus.APPROVED),
        (ContentStatus.GENERATING, ContentStatus.FAILED),
    ],
)
def test_other_moves_are_not_this_gates_concern(
    store: Store, gate: IdempotencyGate, source, target
) -> None:
    item = item_in(source)
    key = generation_key(item)
    store.jobs[key] = ai_job(key, AIJobStatus.RUNNING, item)

    assert gate.evaluate(context(item, target)).is_passed


# Through evaluate_gates and SQLite


def test_a_failing_source_blocks_through_evaluate_gates(store: Store) -> None:
    class Broken:
        def get_by_idempotency_key(self, key: str):
            raise RuntimeError("database is locked")

    gate = IdempotencyGate(Broken(), Publishes(store), Approvals(store))
    report = evaluate_gates(
        [gate], context(item_in(ContentStatus.DRAFT), ContentStatus.GENERATING)
    )

    assert report.outcome is GateOutcome.BLOCK
    assert [r.code for r in report.reasons] == ["gate.error"]


def test_the_real_repositories_satisfy_the_gate(database: Database) -> None:
    channel = make_channel()
    strategy = make_strategy_profile(channel)
    item = dataclasses.replace(
        ContentItem.create(
            channel.id, strategy.id, strategy.version, ContentType.SHORTS, "Video"
        ),
        status=ContentStatus.APPROVED,
    )
    clip = video(item)
    request = dataclasses.replace(
        ApprovalRequest.create(item.id, [clip], requested_by=SYSTEM),
        status=ApprovalStatus.APPROVED,
    )
    draft = dataclasses.replace(item, status=ContentStatus.DRAFT)
    with database.transaction() as connection:
        ChannelRepository(connection).add(channel)
        StrategyProfileRepository(connection).add(strategy)
        ContentItemRepository(connection).add(item)
        ArtifactRepository(connection).add(clip)
        ApprovalRequestRepository(connection).add(request)

    with database.transaction() as connection:
        gate = IdempotencyGate(
            AIJobRepository(connection),
            PublishJobRepository(connection),
            ApprovalRequestRepository(connection),
        )
        before = [
            gate.evaluate(context(draft, ContentStatus.GENERATING)),
            gate.evaluate(context(item, ContentStatus.PUBLISHING)),
        ]
        AIJobRepository(connection).add(
            AIJob.create(
                GENERATION_KIND, generation_key(draft), content_item_id=item.id
            )
        )
        PublishJobRepository(connection).add(
            PublishJob.create(
                request, ContentType.SHORTS, publish_key(item.id, request.id)
            )
        )
        after = [
            gate.evaluate(context(draft, ContentStatus.GENERATING)),
            gate.evaluate(context(item, ContentStatus.PUBLISHING)),
        ]

    assert all(r.is_passed for r in before)
    assert [codes(r) for r in after] == [
        ["idempotency.duplicate_generation"],
        ["idempotency.duplicate_publish"],
    ]
