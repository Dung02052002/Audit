"""C-034 Approval Gate (Prompt Pack v8, prompt #034).

Rules the user approved on 2026-09-30:

- only the item's newest approval request counts, and it must be approved;
- it must cover every artifact kind the item has, each bound to its latest
  version (same id, version and sha256);
- the gate always checks, whatever ``APPROVAL_REQUIRED`` says;
- it only blocks the move to ``publishing``.
"""

import dataclasses
from datetime import UTC, datetime, timedelta

import pytest

from ai_youtube_agent.content.approval import ApprovalRequest, ApprovalStatus
from ai_youtube_agent.core.approval_gate import ApprovalGate
from ai_youtube_agent.core.artifact import Artifact, ArtifactKind
from ai_youtube_agent.core.audit import Actor, ActorKind
from ai_youtube_agent.core.content_item import ContentItem, ContentStatus, ContentType
from ai_youtube_agent.core.db.database import Database
from ai_youtube_agent.core.db.repositories.channel import (
    ChannelRepository,
    StrategyProfileRepository,
)
from ai_youtube_agent.core.db.repositories.content import (
    ArtifactRepository,
    ContentItemRepository,
)
from ai_youtube_agent.core.db.repositories.review import ApprovalRequestRepository
from ai_youtube_agent.core.gates import (
    GateContext,
    GateName,
    GateOutcome,
    PipelineGate,
    evaluate_gates,
)
from factories import make_channel, make_strategy_profile

T0 = datetime(2026, 9, 30, 12, 0, tzinfo=UTC)
SYSTEM = Actor(ActorKind.SYSTEM, "pipeline")


class Store:
    """In-memory stand-in for the two repositories the gate reads."""

    def __init__(self) -> None:
        self.approvals: list[ApprovalRequest] = []
        self.artifacts: list[Artifact] = []


class Approvals:
    def __init__(self, store: Store) -> None:
        self.store = store

    def list_by_content_item(self, content_item_id: str) -> list[ApprovalRequest]:
        return [a for a in self.store.approvals if a.content_item_id == content_item_id]


class Artifacts:
    def __init__(self, store: Store) -> None:
        self.store = store

    def list_by_content_item(self, content_item_id: str) -> list[Artifact]:
        return [a for a in self.store.artifacts if a.content_item_id == content_item_id]


def at(moment: datetime):
    return lambda: moment


def new_item(status: ContentStatus = ContentStatus.APPROVED) -> ContentItem:
    item = ContentItem.create(
        "channel-1", "strategy-1", 1, ContentType.SHORTS, "Video", clock=at(T0)
    )
    return dataclasses.replace(item, status=status)


def artifact(item: ContentItem, kind: ArtifactKind, n: int = 1) -> Artifact:
    return Artifact.create(
        item.id,
        kind,
        uri=f"store://{kind.value}/{n}",
        sha256=f"{n:064x}",
        size_bytes=10,
        media_type="application/octet-stream",
        clock=at(T0),
    )


def newer(old: Artifact, n: int) -> Artifact:
    return old.next_version(
        uri=f"store://{old.kind.value}/{n}",
        sha256=f"{n:064x}",
        size_bytes=11,
        media_type="application/octet-stream",
        clock=at(T0 + timedelta(minutes=n)),
    )


def request(
    item: ContentItem,
    artifacts: list[Artifact],
    status: ApprovalStatus = ApprovalStatus.APPROVED,
    minutes: int = 0,
) -> ApprovalRequest:
    made = ApprovalRequest.create(
        item.id,
        artifacts,
        requested_by=SYSTEM,
        clock=at(T0 + timedelta(minutes=minutes)),
    )
    return dataclasses.replace(made, status=status)


@pytest.fixture
def store() -> Store:
    return Store()


@pytest.fixture
def gate(store: Store) -> ApprovalGate:
    return ApprovalGate(Approvals(store), Artifacts(store))


def context(item: ContentItem, target=ContentStatus.PUBLISHING) -> GateContext:
    return GateContext(item=item, target_status=target, actor=SYSTEM, at=T0)


def codes(result) -> list[str]:
    return [r.code for r in result.reasons]


def approved_setup(store: Store, item: ContentItem):
    video = artifact(item, ArtifactKind.VIDEO, 1)
    thumb = artifact(item, ArtifactKind.THUMBNAIL, 2)
    store.artifacts += [video, thumb]
    store.approvals.append(request(item, [video, thumb]))
    return video, thumb


# Contract


def test_the_gate_follows_the_contract(gate: ApprovalGate) -> None:
    assert isinstance(gate, PipelineGate)
    assert gate.name is GateName.APPROVAL


# Passing


def test_passes_when_the_newest_request_approves_every_latest_version(
    store: Store, gate: ApprovalGate
) -> None:
    item = new_item()
    approved_setup(store, item)

    result = gate.evaluate(context(item))

    assert result.outcome is GateOutcome.PASS
    assert result.gate is GateName.APPROVAL
    assert result.evaluated_at == T0


def test_passes_with_the_latest_of_several_versions(
    store: Store, gate: ApprovalGate
) -> None:
    item = new_item()
    v1 = artifact(item, ArtifactKind.VIDEO, 1)
    v2 = newer(v1, 2)
    store.artifacts += [v2, v1]  # order must not matter
    store.approvals.append(request(item, [v2]))

    assert gate.evaluate(context(item)).is_passed


@pytest.mark.parametrize(
    "target",
    [ContentStatus.GENERATING, ContentStatus.FAILED],
)
def test_other_moves_are_not_this_gates_concern(
    gate: ApprovalGate, target: ContentStatus
) -> None:
    # No approval and no artifacts at all, yet the gate passes.
    assert gate.evaluate(context(new_item(), target)).is_passed


def test_other_items_data_is_ignored(store: Store, gate: ApprovalGate) -> None:
    item, other = new_item(), new_item()
    approved_setup(store, other)
    store.artifacts.append(artifact(item, ArtifactKind.VIDEO, 9))

    assert codes(gate.evaluate(context(item))) == ["approval.missing"]


# Blocking


def test_blocks_without_any_request(store: Store, gate: ApprovalGate) -> None:
    item = new_item()
    store.artifacts.append(artifact(item, ArtifactKind.VIDEO))

    result = gate.evaluate(context(item))

    assert result.outcome is GateOutcome.BLOCK
    assert codes(result) == ["approval.missing"]


@pytest.mark.parametrize(
    "status", [s for s in ApprovalStatus if s is not ApprovalStatus.APPROVED]
)
def test_blocks_when_the_newest_request_is_not_approved(
    store: Store, gate: ApprovalGate, status: ApprovalStatus
) -> None:
    item = new_item()
    video = artifact(item, ArtifactKind.VIDEO)
    store.artifacts.append(video)
    store.approvals.append(request(item, [video], status))

    assert codes(gate.evaluate(context(item))) == ["approval.not_approved"]


def test_an_older_approval_does_not_count_after_a_newer_request(
    store: Store, gate: ApprovalGate
) -> None:
    item = new_item()
    video = artifact(item, ArtifactKind.VIDEO)
    store.artifacts.append(video)
    store.approvals += [
        request(item, [video], ApprovalStatus.PENDING, minutes=5),
        request(item, [video], ApprovalStatus.APPROVED, minutes=0),
    ]

    assert codes(gate.evaluate(context(item))) == ["approval.not_approved"]


def test_the_newest_approved_request_wins_over_older_rejections(
    store: Store, gate: ApprovalGate
) -> None:
    item = new_item()
    video = artifact(item, ArtifactKind.VIDEO)
    store.artifacts.append(video)
    store.approvals += [
        request(item, [video], ApprovalStatus.REJECTED, minutes=0),
        request(item, [video], ApprovalStatus.APPROVED, minutes=5),
    ]

    assert gate.evaluate(context(item)).is_passed


def test_blocks_when_a_newer_version_exists(store: Store, gate: ApprovalGate) -> None:
    item = new_item()
    video, _ = approved_setup(store, item)
    store.artifacts.append(newer(video, 3))

    result = gate.evaluate(context(item))

    assert codes(result) == ["approval.not_current"]
    assert "video" in result.reasons[0].message


def test_blocks_when_a_kind_was_added_after_approval(
    store: Store, gate: ApprovalGate
) -> None:
    item = new_item()
    approved_setup(store, item)
    store.artifacts.append(artifact(item, ArtifactKind.SUBTITLES, 7))

    result = gate.evaluate(context(item))

    assert codes(result) == ["approval.not_current"]
    assert "subtitles" in result.reasons[0].message


def test_blocks_when_the_bound_checksum_differs(
    store: Store, gate: ApprovalGate
) -> None:
    item = new_item()
    video = artifact(item, ArtifactKind.VIDEO, 1)
    store.artifacts.append(video)
    tampered = dataclasses.replace(video, sha256="f" * 64)
    store.approvals.append(request(item, [tampered]))

    assert codes(gate.evaluate(context(item))) == ["approval.not_current"]


def test_blocks_when_a_bound_artifact_no_longer_exists(
    store: Store, gate: ApprovalGate
) -> None:
    item = new_item()
    video = artifact(item, ArtifactKind.VIDEO, 1)
    thumb = artifact(item, ArtifactKind.THUMBNAIL, 2)
    store.artifacts.append(video)  # the thumbnail is not stored
    store.approvals.append(request(item, [video, thumb]))

    result = gate.evaluate(context(item))

    assert codes(result) == ["approval.not_current"]
    assert "thumbnail" in result.reasons[0].message


def test_blocks_when_the_item_has_no_artifacts(
    store: Store, gate: ApprovalGate
) -> None:
    item = new_item()
    fake = artifact(item, ArtifactKind.VIDEO)
    store.approvals.append(request(item, [fake]))

    assert codes(gate.evaluate(context(item))) == ["approval.not_current"]


def test_the_approval_required_flag_is_not_read() -> None:
    import inspect

    from ai_youtube_agent.core import approval_gate

    source = inspect.getsource(approval_gate)
    assert "approval_required" not in source
    assert "FeatureFlags" not in source


# Through evaluate_gates and SQLite


def test_a_failing_source_blocks_through_evaluate_gates(store: Store) -> None:
    class Broken:
        def list_by_content_item(self, content_item_id: str):
            raise RuntimeError("database is locked")

    gate = ApprovalGate(Broken(), Artifacts(store))
    report = evaluate_gates([gate], context(new_item()))

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
    video = artifact(item, ArtifactKind.VIDEO, 1)
    approval = request(item, [video])
    with database.transaction() as connection:
        ChannelRepository(connection).add(channel)
        StrategyProfileRepository(connection).add(strategy)
        ContentItemRepository(connection).add(item)
        ArtifactRepository(connection).add(video)
        ApprovalRequestRepository(connection).add(approval)

    with database.transaction() as connection:
        gate = ApprovalGate(
            ApprovalRequestRepository(connection), ArtifactRepository(connection)
        )
        passed = gate.evaluate(context(item))
        ArtifactRepository(connection).add(newer(video, 2))
        blocked = gate.evaluate(context(item))

    assert passed.is_passed
    assert codes(blocked) == ["approval.not_current"]
