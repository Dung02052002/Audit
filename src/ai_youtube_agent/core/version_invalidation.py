"""Version Invalidation (Prompt Pack v8, prompt #035), context C3 Control Gates.

When an approved artifact changes, the approval that covered it stops being
valid. The rules were approved by the user on 2026-09-30:

- Invalidation runs when a new artifact version is stored, in the same
  transaction as the store, so there is no moment where the artifact changed
  but its approval still stands. ``VersionInvalidation.invalidate`` also runs
  it on demand for one item.
- A request is stale when ``stale_kinds`` is not empty. Only ``PENDING`` and
  ``APPROVED`` requests are invalidated; the other statuses are history.
- When at least one request is invalidated, an item in ``PREVIEW_READY``,
  ``AWAITING_APPROVAL`` or ``APPROVED`` moves back to ``GENERATING`` (#032), so
  it passes testing, preview and approval again. Other statuses stay.
- Each invalidated request is audited as ``approval.invalidated`` by the
  system, after the transaction commits (SQLite allows one writer at a time).

``invalidate_stale_approvals`` does the work inside a caller's transaction and
leaves auditing to the caller.
"""

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from ai_youtube_agent.content.approval import (
    INVALIDATABLE,
    ApprovalRequest,
    ApprovalStatus,
    stale_kinds,
)
from ai_youtube_agent.core.artifact import Artifact, ArtifactKind
from ai_youtube_agent.core.audit import (
    Actor,
    ActorKind,
    AuditLog,
    AuditResult,
    EntityRef,
)
from ai_youtube_agent.core.content_item import ContentItem, ContentStatus
from ai_youtube_agent.core.db.database import Database, RecordNotFoundError
from ai_youtube_agent.core.db.repositories.content import (
    ArtifactRepository,
    ContentItemRepository,
)
from ai_youtube_agent.core.db.repositories.review import ApprovalRequestRepository

Clock = Callable[[], datetime]

ACTOR = Actor(ActorKind.SYSTEM, "version_invalidation")
RESET_STATUSES = frozenset(
    {
        ContentStatus.PREVIEW_READY,
        ContentStatus.AWAITING_APPROVAL,
        ContentStatus.APPROVED,
    }
)


@dataclass(frozen=True)
class InvalidatedApproval:
    approval: ApprovalRequest
    previous_status: ApprovalStatus
    stale_kinds: tuple[ArtifactKind, ...]


@dataclass(frozen=True)
class InvalidationResult:
    item: ContentItem
    invalidated: tuple[InvalidatedApproval, ...]
    item_reset: bool


def invalidate_stale_approvals(
    connection: sqlite3.Connection, item_id: str, *, clock: Clock | None = None
) -> InvalidationResult:
    """Invalidate the item's stale requests inside the caller's transaction."""
    items = ContentItemRepository(connection)
    approvals = ApprovalRequestRepository(connection)
    item = items.get(item_id)
    if item is None:
        raise RecordNotFoundError(f"content item {item_id} does not exist")
    artifacts = ArtifactRepository(connection).list_by_content_item(item_id)

    invalidated = []
    for request in approvals.list_by_content_item(item_id):
        if request.status not in INVALIDATABLE:
            continue
        kinds = stale_kinds(request, artifacts)
        if not kinds:
            continue
        changed = request.invalidate()
        approvals.update(changed, expected_status=request.status)
        invalidated.append(InvalidatedApproval(changed, request.status, tuple(kinds)))

    reset = bool(invalidated) and item.status in RESET_STATUSES
    if reset:
        moved = item.with_status(ContentStatus.GENERATING, clock=clock)
        items.update(moved, expected_updated_at=item.updated_at)
        item = moved
    return InvalidationResult(item, tuple(invalidated), reset)


class VersionInvalidation:
    """Stores artifact versions and invalidates stale approvals, with audit."""

    def __init__(
        self, database: Database, audit: AuditLog, *, clock: Clock | None = None
    ) -> None:
        self._database = database
        self._audit = audit
        self._clock = clock

    def store_artifact_version(self, artifact: Artifact) -> InvalidationResult:
        with self._database.transaction() as connection:
            ArtifactRepository(connection).add(artifact)
            result = invalidate_stale_approvals(
                connection, artifact.content_item_id, clock=self._clock
            )
        self._record(result)
        return result

    def invalidate(self, item_id: str) -> InvalidationResult:
        with self._database.transaction() as connection:
            result = invalidate_stale_approvals(connection, item_id, clock=self._clock)
        self._record(result)
        return result

    def _record(self, result: InvalidationResult) -> None:
        for entry in result.invalidated:
            self._audit.record(
                "approval.invalidated",
                ACTOR,
                EntityRef("approval_request", entry.approval.id),
                AuditResult.SUCCESS,
                {
                    "content_item_id": result.item.id,
                    "previous_status": entry.previous_status.value,
                    "stale_kinds": ",".join(kind.value for kind in entry.stale_kinds),
                    "item_reset": result.item_reset,
                },
            )
