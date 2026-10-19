"""Approval Audit service (Prompt Pack v8, prompt #143), context C11 Review.

``ApprovalAuditService`` ensures every approval, rejection, change-request,
expiry, revalidation, invalidation, request creation, and email notification is
recorded as an immutable ``AuditEvent`` with structured scalar metadata, and
provides read-only audit trail queries per ``ApprovalRequest`` or per
``ContentItem``.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final

from ai_youtube_agent.content.approval import ApprovalNotFoundError
from ai_youtube_agent.core.audit import (
    Actor,
    AuditEvent,
    AuditLog,
    AuditResult,
    AuditSink,
    EntityRef,
    MetadataValue,
)
from ai_youtube_agent.core.db.database import Database
from ai_youtube_agent.core.db.repositories.content import ContentItemRepository
from ai_youtube_agent.core.db.repositories.review import ApprovalRequestRepository

APPROVAL_ENTITY_TYPE: Final[str] = "approval_request"

APPROVAL_REQUESTED: Final[str] = "approval.requested"
APPROVAL_EMAIL_SENT: Final[str] = "approval.email_sent"
APPROVAL_APPROVED: Final[str] = "approval.approved"
APPROVAL_REJECTED: Final[str] = "approval.rejected"
APPROVAL_CHANGES_REQUESTED: Final[str] = "approval.changes_requested"
APPROVAL_EXPIRED: Final[str] = "approval.expired"
APPROVAL_REVALIDATED: Final[str] = "approval.revalidated"
APPROVAL_INVALIDATED: Final[str] = "approval.invalidated"

APPROVAL_AUDIT_ACTIONS: Final[frozenset[str]] = frozenset(
    {
        APPROVAL_REQUESTED,
        APPROVAL_EMAIL_SENT,
        APPROVAL_APPROVED,
        APPROVAL_REJECTED,
        APPROVAL_CHANGES_REQUESTED,
        APPROVAL_EXPIRED,
        APPROVAL_REVALIDATED,
        APPROVAL_INVALIDATED,
    }
)

DECISION_AUDIT_ACTIONS: Final[frozenset[str]] = frozenset(
    {
        APPROVAL_APPROVED,
        APPROVAL_REJECTED,
        APPROVAL_CHANGES_REQUESTED,
        APPROVAL_EXPIRED,
        APPROVAL_REVALIDATED,
        APPROVAL_INVALIDATED,
    }
)


@dataclass(frozen=True)
class ApprovalAuditEntry:
    event_id: str
    action: str
    actor: Actor
    result: AuditResult
    approval_request_id: str
    content_item_id: str | None
    video_artifact_id: str | None
    video_artifact_version: int | None
    video_artifact_sha256: str | None
    reason: str | None
    occurred_at: datetime
    metadata: Mapping[str, MetadataValue]

    @classmethod
    def from_event(
        cls,
        event: AuditEvent,
        *,
        fallback_content_item_id: str | None = None,
    ) -> "ApprovalAuditEntry":
        meta = event.metadata
        content_item_id = meta.get("content_item_id")
        if not isinstance(content_item_id, str):
            content_item_id = fallback_content_item_id
        video_id = meta.get("video_artifact_id")
        video_ver = meta.get("video_artifact_version")
        video_sha = meta.get("video_artifact_sha256")
        reason = meta.get("reason") or meta.get("note")
        return cls(
            event_id=event.event_id,
            action=event.action,
            actor=event.actor,
            result=event.result,
            approval_request_id=event.entity.id,
            content_item_id=content_item_id,
            video_artifact_id=video_id if isinstance(video_id, str) else None,
            video_artifact_version=(
                video_ver
                if isinstance(video_ver, int) and not isinstance(video_ver, bool)
                else None
            ),
            video_artifact_sha256=video_sha if isinstance(video_sha, str) else None,
            reason=reason if isinstance(reason, str) else None,
            occurred_at=event.timestamp,
            metadata=meta,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "action": self.action,
            "actor": {
                "kind": self.actor.kind.value,
                "id": self.actor.id,
            },
            "result": self.result.value,
            "approval_request_id": self.approval_request_id,
            "content_item_id": self.content_item_id,
            "video_artifact_id": self.video_artifact_id,
            "video_artifact_version": self.video_artifact_version,
            "video_artifact_sha256": self.video_artifact_sha256,
            "reason": self.reason,
            "occurred_at": self.occurred_at.isoformat(),
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True)
class ApprovalAuditTrail:
    content_item_id: str
    approval_request_id: str | None
    entries: tuple[ApprovalAuditEntry, ...]
    action_counts: Mapping[str, int]

    def as_dict(self) -> dict[str, Any]:
        return {
            "content_item_id": self.content_item_id,
            "approval_request_id": self.approval_request_id,
            "total_events": len(self.entries),
            "action_counts": dict(self.action_counts),
            "entries": [entry.as_dict() for entry in self.entries],
        }


class ApprovalAuditService:
    """Records and queries immutable approval decision audit events (#143)."""

    def __init__(
        self,
        database: Database,
        audit_sink: AuditSink,
        audit_log: AuditLog,
    ) -> None:
        self._database = database
        self._sink = audit_sink
        self._audit = audit_log

    def record_decision(
        self,
        action: str,
        *,
        actor: Actor,
        approval_request_id: str,
        result: AuditResult,
        metadata: Mapping[str, MetadataValue] | None = None,
    ) -> AuditEvent:
        if action not in APPROVAL_AUDIT_ACTIONS:
            raise ValueError(f"unsupported approval audit action {action!r}")
        return self._audit.record(
            action,
            actor,
            EntityRef(APPROVAL_ENTITY_TYPE, approval_request_id),
            result,
            metadata=metadata,
        )

    def trail_for_request(self, approval_request_id: str) -> ApprovalAuditTrail:
        with self._database.transaction() as conn:
            request = ApprovalRequestRepository(conn).get(approval_request_id)
            if request is None:
                raise ApprovalNotFoundError(
                    f"approval request {approval_request_id} was not found"
                )
            content_item_id = request.content_item_id

        matching = [
            ApprovalAuditEntry.from_event(
                event, fallback_content_item_id=content_item_id
            )
            for event in self._sink.events()
            if event.entity.type == APPROVAL_ENTITY_TYPE
            and event.entity.id == approval_request_id
            and event.action in APPROVAL_AUDIT_ACTIONS
        ]
        return _build_trail(
            content_item_id=content_item_id,
            approval_request_id=approval_request_id,
            entries=matching,
        )

    def trail_for_content_item(self, content_item_id: str) -> ApprovalAuditTrail:
        with self._database.transaction() as conn:
            item = ContentItemRepository(conn).get(content_item_id)
            if item is None:
                raise ApprovalNotFoundError(
                    f"content item {content_item_id} was not found"
                )
            requests = ApprovalRequestRepository(conn).list_by_content_item(
                content_item_id
            )
            request_ids = {r.id for r in requests}

        matching = [
            ApprovalAuditEntry.from_event(
                event, fallback_content_item_id=content_item_id
            )
            for event in self._sink.events()
            if event.entity.type == APPROVAL_ENTITY_TYPE
            and event.action in APPROVAL_AUDIT_ACTIONS
            and (
                event.entity.id in request_ids
                or event.metadata.get("content_item_id") == content_item_id
            )
        ]
        return _build_trail(
            content_item_id=content_item_id,
            approval_request_id=None,
            entries=matching,
        )


def _build_trail(
    *,
    content_item_id: str,
    approval_request_id: str | None,
    entries: list[ApprovalAuditEntry],
) -> ApprovalAuditTrail:
    counts: dict[str, int] = {}
    for entry in entries:
        counts[entry.action] = counts.get(entry.action, 0) + 1
    return ApprovalAuditTrail(
        content_item_id=content_item_id,
        approval_request_id=approval_request_id,
        entries=tuple(entries),
        action_counts=counts,
    )
