"""Publish audit (Prompt Pack v8, prompt #156), context C12 Publishing.

Every step of a publish leaves an immutable audit event, from the request to the
result, so "who published what, under which approval, and what happened" can be
answered from the audit log alone.

``PublishAuditRecorder`` writes the events (through ``AuditLog``; metadata holds ids,
keys, codes, counts and flags, never a token, a file or the text of a video):

| action | when | result |
|---|---|---|
| ``publish.requested`` | a publish was asked for: actor, approval, bound video artifact | success |
| ``publish.duplicate_blocked`` | the duplicate guard refused it (#152) | denied |
| ``publish.replayed`` | a succeeded job was answered from storage, nothing uploaded | success |
| ``publish.blocked`` | the publish gate refused it, with the blocking gates and reason codes | denied |
| ``publish.authorization_failed`` / ``publish.token_renewed`` | token recovery (#154) | denied / success |
| ``publish.upload_retry`` | one retry of the upload (#153) | failure |
| ``<prefix>.published`` | the video is on YouTube: video id, privacy, disclosure flag | success |
| ``<prefix>.publish_failed`` | the job failed: error code, resumable, attempts | failure |

``<prefix>`` is ``shorts`` or ``longform``. Item-level events use the entity
``content_item``; job-level events use the entity ``publish_job``. Every event carries
``content_item_id`` and ``approval_request_id``.

``PublishAuditService.trail_for_content_item`` joins the approval decisions of an item
(``ApprovalAuditService``, #143) with its publish events, oldest first, so the
approval and the result appear in one trail.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from http import HTTPStatus
from typing import Any, Final

from ai_youtube_agent.content.approval_audit import ApprovalAuditService
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
from ai_youtube_agent.core.errors import DomainError
from ai_youtube_agent.pipeline.publish import PublishJob
from ai_youtube_agent.pipeline.publish_context import PublishContext

PUBLISH_REQUESTED: Final[str] = "publish.requested"
PUBLISH_DUPLICATE_BLOCKED: Final[str] = "publish.duplicate_blocked"
PUBLISH_REPLAYED: Final[str] = "publish.replayed"
PUBLISH_BLOCKED: Final[str] = "publish.blocked"
PUBLISH_AUTHORIZATION_FAILED: Final[str] = "publish.authorization_failed"
PUBLISH_TOKEN_RENEWED: Final[str] = "publish.token_renewed"
PUBLISH_UPLOAD_RETRY: Final[str] = "publish.upload_retry"
PUBLISHED_SUFFIX: Final[str] = "published"
PUBLISH_FAILED_SUFFIX: Final[str] = "publish_failed"

PUBLISH_JOB_ENTITY: Final[str] = "publish_job"
CONTENT_ITEM_ENTITY: Final[str] = "content_item"

GENERIC_PUBLISH_ACTIONS: Final[frozenset[str]] = frozenset(
    {
        PUBLISH_REQUESTED,
        PUBLISH_DUPLICATE_BLOCKED,
        PUBLISH_REPLAYED,
        PUBLISH_BLOCKED,
        PUBLISH_AUTHORIZATION_FAILED,
        PUBLISH_TOKEN_RENEWED,
        PUBLISH_UPLOAD_RETRY,
    }
)
TYPED_PUBLISH_ACTIONS: Final[frozenset[str]] = frozenset(
    f"{prefix}.{suffix}"
    for prefix in ("shorts", "longform")
    for suffix in (PUBLISHED_SUFFIX, PUBLISH_FAILED_SUFFIX)
)
PUBLISH_AUDIT_ACTIONS: Final[frozenset[str]] = (
    GENERIC_PUBLISH_ACTIONS | TYPED_PUBLISH_ACTIONS
)


def published_action(prefix: str) -> str:
    return f"{prefix}.{PUBLISHED_SUFFIX}"


def publish_failed_action(prefix: str) -> str:
    return f"{prefix}.{PUBLISH_FAILED_SUFFIX}"


class PublishAuditRecorder:
    def __init__(self, audit_log: AuditLog) -> None:
        self._audit = audit_log

    def item_event(
        self,
        action: str,
        result: AuditResult,
        context: PublishContext,
        actor: Actor,
        extra: Mapping[str, MetadataValue] | None = None,
    ) -> AuditEvent:
        """An event about the item, for the steps before a publish job exists."""
        metadata = self._base(context)
        metadata.update(extra or {})
        return self._audit.record(
            action,
            actor,
            EntityRef(CONTENT_ITEM_ENTITY, context.item.id),
            result,
            metadata=metadata,
        )

    def job_event(
        self,
        action: str,
        result: AuditResult,
        context: PublishContext,
        actor: Actor,
        job: PublishJob,
        item_status: str,
        extra: Mapping[str, MetadataValue] | None = None,
    ) -> AuditEvent:
        """An event about a publish job (its attempts and status)."""
        metadata = self._base(context)
        metadata.update(
            {
                "attempts": job.attempts,
                "job_status": job.status.value,
                "item_status": item_status,
            }
        )
        metadata.update(extra or {})
        return self._audit.record(
            action,
            actor,
            EntityRef(PUBLISH_JOB_ENTITY, job.id),
            result,
            metadata=metadata,
        )

    @staticmethod
    def _base(context: PublishContext) -> dict[str, MetadataValue]:
        video = context.video_artifact
        return {
            "content_item_id": context.item.id,
            "channel_id": context.item.channel_id,
            "content_type": context.item.content_type.value,
            "approval_request_id": context.approval.id,
            "video_artifact_id": video.id,
            "video_artifact_version": video.version,
            "video_artifact_sha256": video.sha256,
            "job_key": context.keys.job_key,
            "upload_key": context.keys.upload_key,
            "item_status": context.item.status.value,
        }


class PublishAuditNotFoundError(DomainError):
    default_code = "domain.publish_audit_not_found"
    default_user_message = "The requested content item does not exist."
    default_http_status = HTTPStatus.NOT_FOUND


@dataclass(frozen=True)
class PublishAuditEntry:
    event_id: str
    source: str
    action: str
    actor_kind: str
    actor_id: str
    result: str
    entity_type: str
    entity_id: str
    occurred_at: datetime
    metadata: Mapping[str, MetadataValue]

    @classmethod
    def from_event(cls, event: AuditEvent, source: str) -> "PublishAuditEntry":
        return cls(
            event_id=event.event_id,
            source=source,
            action=event.action,
            actor_kind=event.actor.kind.value,
            actor_id=event.actor.id,
            result=event.result.value,
            entity_type=event.entity.type,
            entity_id=event.entity.id,
            occurred_at=event.timestamp,
            metadata=event.metadata,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "source": self.source,
            "action": self.action,
            "actor": {"kind": self.actor_kind, "id": self.actor_id},
            "result": self.result,
            "entity": {"type": self.entity_type, "id": self.entity_id},
            "occurred_at": self.occurred_at.isoformat(),
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True)
class PublishAuditTrail:
    content_item_id: str
    entries: tuple[PublishAuditEntry, ...]
    action_counts: Mapping[str, int]
    youtube_video_id: str | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "content_item_id": self.content_item_id,
            "total_events": len(self.entries),
            "action_counts": dict(self.action_counts),
            "youtube_video_id": self.youtube_video_id,
            "entries": [entry.as_dict() for entry in self.entries],
        }


class PublishAuditService:
    def __init__(
        self,
        database: Database,
        audit_sink: AuditSink,
        approval_audit: ApprovalAuditService,
    ) -> None:
        self._database = database
        self._sink = audit_sink
        self._approvals = approval_audit

    def trail_for_content_item(self, content_item_id: str) -> PublishAuditTrail:
        with self._database.transaction() as connection:
            if ContentItemRepository(connection).get(content_item_id) is None:
                raise PublishAuditNotFoundError(
                    f"content item {content_item_id} was not found"
                )
        approval_ids = {
            entry.event_id
            for entry in self._approvals.trail_for_content_item(content_item_id).entries
        }
        entries: list[PublishAuditEntry] = []
        for event in self._sink.events():
            if event.event_id in approval_ids:
                entries.append(PublishAuditEntry.from_event(event, "approval"))
            elif (
                event.action in PUBLISH_AUDIT_ACTIONS
                and event.metadata.get("content_item_id") == content_item_id
            ):
                entries.append(PublishAuditEntry.from_event(event, "publish"))
        counts: dict[str, int] = {}
        video_id: str | None = None
        for entry in entries:
            counts[entry.action] = counts.get(entry.action, 0) + 1
            if entry.action in TYPED_PUBLISH_ACTIONS and entry.action.endswith(
                f".{PUBLISHED_SUFFIX}"
            ):
                value = entry.metadata.get("youtube_video_id")
                video_id = value if isinstance(value, str) else video_id
        return PublishAuditTrail(
            content_item_id=content_item_id,
            entries=tuple(entries),
            action_counts=counts,
            youtube_video_id=video_id,
        )
