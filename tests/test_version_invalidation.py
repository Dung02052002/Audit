"""C-035 Version Invalidation (Prompt Pack v8, prompt #035).

Rules the user approved on 2026-09-30:

- invalidation runs when a new artifact version is stored, in the same
  transaction, and can also be run on demand for one item;
- pending and approved requests can be invalidated; rejected,
  changes_requested, expired and invalidated ones are history and stay;
- when an approval is invalidated, an item in preview_ready,
  awaiting_approval or approved moves back to generating;
- each invalidation is audited as ``approval.invalidated`` by the system,
  after the transaction commits.
"""

import dataclasses
import hashlib
import sqlite3
from datetime import timedelta

import pytest

from ai_youtube_agent.content.approval import (
    INVALIDATABLE,
    ApprovalRequest,
    ApprovalStateError,
    ApprovalStatus,
    stale_kinds,
)
from ai_youtube_agent.core.artifact import Artifact, ArtifactKind
from ai_youtube_agent.core.audit import (
    ActorKind,
    AuditLog,
    AuditResult,
    InMemoryAuditSink,
)
from ai_youtube_agent.core.content_item import ContentItem, ContentStatus
from ai_youtube_agent.core.db.database import Database, RecordNotFoundError
from ai_youtube_agent.core.db.repositories.channel import (
    ChannelRepository,
    StrategyProfileRepository,
)
from ai_youtube_agent.core.db.repositories.content import (
    ArtifactRepository,
    ContentItemRepository,
)
from ai_youtube_agent.core.db.repositories.review import ApprovalRequestRepository
from ai_youtube_agent.core.errors import DomainError
from ai_youtube_agent.core.version_invalidation import (
    RESET_STATUSES,
    VersionInvalidation,
    invalidate_stale_approvals,
)
from factories import (
    T0,
    at,
    make_approval_request,
    make_artifact,
    make_channel,
    make_content_item,
    make_strategy_profile,
)

T1 = T0 + timedelta(minutes=5)
FINISHED = [s for s in ApprovalStatus if s not in INVALIDATABLE]


def newer(old: Artifact) -> Artifact:
    # A checksum derived from the old one never clashes with factory checksums.
    return old.next_version(
        uri=f"{old.uri}/next",
        sha256=hashlib.sha256(old.sha256.encode()).hexdigest(),
        size_bytes=5,
        media_type="application/octet-stream",
        clock=at(T1),
    )


def save_graph(database: Database, status: ContentStatus = ContentStatus.APPROVED):
    channel = make_channel()
    strategy = make_strategy_profile(channel)
    item = dataclasses.replace(make_content_item(channel, strategy), status=status)
    with database.transaction() as connection:
        ChannelRepository(connection).add(channel)
        StrategyProfileRepository(connection).add(strategy)
        ContentItemRepository(connection).add(item)
    return item


def save_artifacts(database: Database, *artifacts: Artifact) -> None:
    with database.transaction() as connection:
        for artifact in artifacts:
            ArtifactRepository(connection).add(artifact)


def save_approvals(database: Database, *approvals: ApprovalRequest) -> None:
    with database.transaction() as connection:
        for approval in approvals:
            ApprovalRequestRepository(connection).add(approval)


def load(database: Database, item: ContentItem):
    with database.transaction() as connection:
        return (
            ContentItemRepository(connection).get(item.id),
            ApprovalRequestRepository(connection).list_by_content_item(item.id),
            ArtifactRepository(connection).list_by_content_item(item.id),
        )


@pytest.fixture
def sink() -> InMemoryAuditSink:
    return InMemoryAuditSink()


@pytest.fixture
def service(database: Database, sink: InMemoryAuditSink) -> VersionInvalidation:
    return VersionInvalidation(database, AuditLog(sink, clock=at(T1)), clock=at(T1))


# The entity


@pytest.mark.parametrize("status", sorted(INVALIDATABLE))
def test_pending_and_approved_requests_can_be_invalidated(status) -> None:
    item = make_content_item(make_channel(), make_strategy_profile(make_channel()))
    request = make_approval_request(item, [make_artifact(item)], status=status)

    invalidated = request.invalidate()

    assert invalidated.status is ApprovalStatus.INVALIDATED
    assert dataclasses.replace(invalidated, status=status) == request
    assert request.status is status


@pytest.mark.parametrize("status", FINISHED)
def test_finished_requests_cannot_be_invalidated(status) -> None:
    item = make_content_item(make_channel(), make_strategy_profile(make_channel()))
    request = make_approval_request(item, [make_artifact(item)], status=status)

    with pytest.raises(ApprovalStateError) as caught:
        request.invalidate()

    assert isinstance(caught.value, DomainError)
    assert caught.value.code == "domain.approval_state"


def test_invalidatable_statuses_are_pending_and_approved() -> None:
    assert {ApprovalStatus.PENDING, ApprovalStatus.APPROVED} == INVALIDATABLE


# stale_kinds


def test_stale_kinds_is_empty_for_the_current_versions() -> None:
    item = make_content_item(make_channel(), make_strategy_profile(make_channel()))
    video = make_artifact(item)
    thumb = make_artifact(item, ArtifactKind.THUMBNAIL)
    request = make_approval_request(item, [video, thumb])

    assert stale_kinds(request, [thumb, video]) == []


def test_stale_kinds_names_every_changed_kind_in_enum_order() -> None:
    item = make_content_item(make_channel(), make_strategy_profile(make_channel()))
    video = make_artifact(item)
    thumb = make_artifact(item, ArtifactKind.THUMBNAIL)
    audio = make_artifact(item, ArtifactKind.AUDIO)
    request = make_approval_request(item, [video, thumb])

    # A newer thumbnail, a new audio kind, and the video is gone.
    assert stale_kinds(request, [newer(thumb), thumb, audio]) == [
        ArtifactKind.VIDEO,
        ArtifactKind.AUDIO,
        ArtifactKind.THUMBNAIL,
    ]


# Storing a new version


def test_a_new_version_invalidates_the_approval_and_resets_the_item(
    database: Database, service: VersionInvalidation, sink: InMemoryAuditSink
) -> None:
    item = save_graph(database)
    video = make_artifact(item)
    approval = make_approval_request(item, [video], status=ApprovalStatus.APPROVED)
    save_artifacts(database, video)
    save_approvals(database, approval)
    changed = newer(video)

    result = service.store_artifact_version(changed)

    stored_item, approvals, artifacts = load(database, item)
    assert changed in artifacts
    assert [a.status for a in approvals] == [ApprovalStatus.INVALIDATED]
    assert stored_item.status is ContentStatus.GENERATING
    assert stored_item.updated_at == T1

    assert result.item == stored_item
    assert result.item_reset is True
    (entry,) = result.invalidated
    assert entry.approval == approvals[0]
    assert entry.previous_status is ApprovalStatus.APPROVED
    assert entry.stale_kinds == (ArtifactKind.VIDEO,)

    (event,) = sink.events()
    assert event.action == "approval.invalidated"
    assert event.actor.kind is ActorKind.SYSTEM
    assert (event.entity.type, event.entity.id) == ("approval_request", approval.id)
    assert event.result is AuditResult.SUCCESS
    assert dict(event.metadata) == {
        "content_item_id": item.id,
        "previous_status": "approved",
        "stale_kinds": "video",
        "item_reset": True,
    }


def test_a_pending_request_is_invalidated_too(
    database: Database, service: VersionInvalidation, sink: InMemoryAuditSink
) -> None:
    item = save_graph(database, ContentStatus.AWAITING_APPROVAL)
    video = make_artifact(item)
    save_artifacts(database, video)
    save_approvals(database, make_approval_request(item, [video]))

    service.store_artifact_version(newer(video))

    stored_item, approvals, _ = load(database, item)
    assert [a.status for a in approvals] == [ApprovalStatus.INVALIDATED]
    assert stored_item.status is ContentStatus.GENERATING
    assert dict(sink.events()[0].metadata)["previous_status"] == "pending"


def test_a_new_kind_invalidates_as_well(
    database: Database, service: VersionInvalidation
) -> None:
    item = save_graph(database)
    video = make_artifact(item)
    save_artifacts(database, video)
    save_approvals(
        database,
        make_approval_request(item, [video], status=ApprovalStatus.APPROVED),
    )

    result = service.store_artifact_version(make_artifact(item, ArtifactKind.AUDIO))

    assert result.invalidated[0].stale_kinds == (ArtifactKind.AUDIO,)


@pytest.mark.parametrize("status", FINISHED)
def test_finished_requests_are_left_alone(
    database: Database,
    service: VersionInvalidation,
    sink: InMemoryAuditSink,
    status: ApprovalStatus,
) -> None:
    item = save_graph(database, ContentStatus.REJECTED)
    video = make_artifact(item)
    save_artifacts(database, video)
    save_approvals(database, make_approval_request(item, [video], status=status))

    result = service.store_artifact_version(newer(video))

    stored_item, approvals, _ = load(database, item)
    assert [a.status for a in approvals] == [status]
    assert stored_item.status is ContentStatus.REJECTED
    assert result.invalidated == ()
    assert result.item_reset is False
    assert sink.events() == ()


def test_every_stale_request_is_invalidated(
    database: Database, service: VersionInvalidation, sink: InMemoryAuditSink
) -> None:
    item = save_graph(database)
    video = make_artifact(item)
    save_artifacts(database, video)
    old = make_approval_request(item, [video], status=ApprovalStatus.APPROVED)
    new = make_approval_request(item, [video])
    rejected = make_approval_request(item, [video], status=ApprovalStatus.REJECTED)
    save_approvals(database, old, new, rejected)

    result = service.store_artifact_version(newer(video))

    statuses = {a.id: a.status for a in load(database, item)[1]}
    assert statuses == {
        old.id: ApprovalStatus.INVALIDATED,
        new.id: ApprovalStatus.INVALIDATED,
        rejected.id: ApprovalStatus.REJECTED,
    }
    assert {e.approval.id for e in result.invalidated} == {old.id, new.id}
    assert {e.entity.id for e in sink.events()} == {old.id, new.id}


@pytest.mark.parametrize("status", sorted(RESET_STATUSES))
def test_items_waiting_on_review_move_back_to_generating(
    database: Database, service: VersionInvalidation, status: ContentStatus
) -> None:
    item = save_graph(database, status)
    video = make_artifact(item)
    save_artifacts(database, video)
    save_approvals(database, make_approval_request(item, [video]))

    result = service.store_artifact_version(newer(video))

    assert result.item_reset is True
    assert load(database, item)[0].status is ContentStatus.GENERATING


@pytest.mark.parametrize(
    "status", [s for s in ContentStatus if s not in RESET_STATUSES]
)
def test_other_item_statuses_are_left_alone(
    database: Database, service: VersionInvalidation, status: ContentStatus
) -> None:
    item = save_graph(database, status)
    video = make_artifact(item)
    save_artifacts(database, video)
    save_approvals(
        database,
        make_approval_request(item, [video], status=ApprovalStatus.APPROVED),
    )

    result = service.store_artifact_version(newer(video))

    assert result.item_reset is False
    assert len(result.invalidated) == 1
    stored_item = load(database, item)[0]
    assert stored_item.status is status
    assert stored_item.updated_at == item.updated_at


def test_reset_statuses_are_the_three_review_statuses() -> None:
    assert {
        ContentStatus.PREVIEW_READY,
        ContentStatus.AWAITING_APPROVAL,
        ContentStatus.APPROVED,
    } == RESET_STATUSES


def test_the_item_stays_when_nothing_is_invalidated(
    database: Database, service: VersionInvalidation, sink: InMemoryAuditSink
) -> None:
    item = save_graph(database, ContentStatus.PREVIEW_READY)
    video = make_artifact(item)
    save_artifacts(database, video)

    result = service.store_artifact_version(newer(video))

    assert result.invalidated == ()
    assert result.item_reset is False
    assert load(database, item)[0].status is ContentStatus.PREVIEW_READY
    assert sink.events() == ()


def test_another_items_approval_is_not_touched(
    database: Database, service: VersionInvalidation
) -> None:
    item, other = save_graph(database), save_graph(database)
    other_video = make_artifact(other)
    save_artifacts(database, other_video)
    save_approvals(
        database,
        make_approval_request(other, [other_video], status=ApprovalStatus.APPROVED),
    )

    service.store_artifact_version(make_artifact(item))

    other_item, approvals, _ = load(database, other)
    assert [a.status for a in approvals] == [ApprovalStatus.APPROVED]
    assert other_item.status is ContentStatus.APPROVED


# Atomicity and audit timing


def test_a_failure_rolls_back_the_artifact_and_audits_nothing(
    database: Database,
    service: VersionInvalidation,
    sink: InMemoryAuditSink,
    monkeypatch,
) -> None:
    item = save_graph(database)
    video = make_artifact(item)
    save_artifacts(database, video)
    save_approvals(
        database,
        make_approval_request(item, [video], status=ApprovalStatus.APPROVED),
    )

    def broken(self, item, *, expected_updated_at):
        raise RuntimeError("disk full")

    monkeypatch.setattr(ContentItemRepository, "update", broken)
    changed = newer(video)

    with pytest.raises(RuntimeError):
        service.store_artifact_version(changed)

    stored_item, approvals, artifacts = load(database, item)
    assert changed not in artifacts
    assert [a.status for a in approvals] == [ApprovalStatus.APPROVED]
    assert stored_item.status is ContentStatus.APPROVED
    assert sink.events() == ()


def test_a_duplicate_version_changes_nothing(
    database: Database, service: VersionInvalidation, sink: InMemoryAuditSink
) -> None:
    item = save_graph(database)
    video = make_artifact(item)
    save_artifacts(database, video)
    save_approvals(
        database,
        make_approval_request(item, [video], status=ApprovalStatus.APPROVED),
    )
    clash = dataclasses.replace(newer(video), version=video.version)

    with pytest.raises(sqlite3.IntegrityError):
        service.store_artifact_version(clash)

    assert [a.status for a in load(database, item)[1]] == [ApprovalStatus.APPROVED]
    assert sink.events() == ()


def test_audit_events_are_recorded_after_the_commit(database: Database) -> None:
    item = save_graph(database)
    video = make_artifact(item)
    save_artifacts(database, video)
    save_approvals(
        database,
        make_approval_request(item, [video], status=ApprovalStatus.APPROVED),
    )
    seen: list[ApprovalStatus] = []

    class CheckingSink(InMemoryAuditSink):
        def append(self, event) -> None:
            # A fresh connection only sees committed data.
            seen.extend(a.status for a in load(database, item)[1])
            super().append(event)

    VersionInvalidation(database, AuditLog(CheckingSink())).store_artifact_version(
        newer(video)
    )

    assert seen == [ApprovalStatus.INVALIDATED]


# On demand


def test_on_demand_invalidation_finds_stale_approvals(
    database: Database, service: VersionInvalidation, sink: InMemoryAuditSink
) -> None:
    item = save_graph(database)
    video = make_artifact(item)
    save_artifacts(database, video, newer(video))
    save_approvals(
        database,
        make_approval_request(item, [video], status=ApprovalStatus.APPROVED),
    )

    result = service.invalidate(item.id)

    assert result.item_reset is True
    assert [a.status for a in load(database, item)[1]] == [ApprovalStatus.INVALIDATED]
    assert len(sink.events()) == 1


def test_on_demand_with_current_approvals_changes_nothing(
    database: Database, service: VersionInvalidation, sink: InMemoryAuditSink
) -> None:
    item = save_graph(database)
    video = make_artifact(item)
    save_artifacts(database, video)
    save_approvals(
        database,
        make_approval_request(item, [video], status=ApprovalStatus.APPROVED),
    )

    result = service.invalidate(item.id)

    assert result.invalidated == ()
    assert result.item == item
    assert load(database, item)[0].status is ContentStatus.APPROVED
    assert sink.events() == ()


def test_an_unknown_item_is_reported(
    database: Database, service: VersionInvalidation
) -> None:
    with pytest.raises(RecordNotFoundError):
        service.invalidate("missing")


def test_the_in_transaction_function_leaves_audit_to_the_caller(
    database: Database,
) -> None:
    item = save_graph(database)
    video = make_artifact(item)
    save_artifacts(database, video, newer(video))
    save_approvals(
        database,
        make_approval_request(item, [video], status=ApprovalStatus.APPROVED),
    )

    with database.transaction() as connection:
        result = invalidate_stale_approvals(connection, item.id, clock=at(T1))

    assert len(result.invalidated) == 1
    assert load(database, item)[0].status is ContentStatus.GENERATING
