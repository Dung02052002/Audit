"""Comment and Reply Audit service (Prompt Pack v8, prompt #189), context C15 Community.

``CommentAuditService`` provides read-only, structured audit trails over all
comment sync, classification, reply drafting, human reply approval/discard,
duplicate reply blocking, ``AUTO_REPLY_ENABLED`` enforcement, and YouTube reply
post events.

Safety invariants:
- Audit entries contain only scalar IDs, counts, status codes, flags, and hashes.
- Comment body text, reply text, author display names, and OAuth tokens are never
  stored in or exposed by audit metadata.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from http import HTTPStatus
from typing import Any, Final

from ai_youtube_agent.content.auto_reply import (
    AUTO_REPLY_BLOCKED,
    AUTO_REPLY_POSTED,
)
from ai_youtube_agent.content.comment_classifier import COMMENT_CLASSIFIED
from ai_youtube_agent.content.comment_sync import COMMENTS_SYNCED
from ai_youtube_agent.content.duplicate_reply_guard import REPLY_DUPLICATE_BLOCKED
from ai_youtube_agent.content.reply_approval import (
    REPLY_APPROVED,
    REPLY_DISCARDED,
    REPLY_POST_FAILED,
    REPLY_POSTED,
)
from ai_youtube_agent.content.reply_draft import REPLY_DRAFTED
from ai_youtube_agent.core.audit import (
    Actor,
    AuditEvent,
    AuditResult,
    AuditSink,
    MetadataValue,
)
from ai_youtube_agent.core.db.database import Database
from ai_youtube_agent.core.db.repositories.channel import ChannelRepository
from ai_youtube_agent.core.db.repositories.community import CommentRepository
from ai_youtube_agent.core.errors import DomainError

COMMENT_ENTITY_TYPE: Final[str] = "comment"
REPLY_DRAFT_ENTITY_TYPE: Final[str] = "reply_draft"
CHANNEL_ENTITY_TYPE: Final[str] = "channel"

COMMENT_AUDIT_ACTIONS: Final[frozenset[str]] = frozenset(
    {
        COMMENTS_SYNCED,
        COMMENT_CLASSIFIED,
        REPLY_DRAFTED,
        REPLY_APPROVED,
        REPLY_DISCARDED,
        REPLY_POSTED,
        REPLY_POST_FAILED,
        REPLY_DUPLICATE_BLOCKED,
        AUTO_REPLY_BLOCKED,
        AUTO_REPLY_POSTED,
    }
)

REPLY_LIFECYCLE_ACTIONS: Final[frozenset[str]] = frozenset(
    {
        REPLY_DRAFTED,
        REPLY_APPROVED,
        REPLY_DISCARDED,
        REPLY_POSTED,
        REPLY_POST_FAILED,
        REPLY_DUPLICATE_BLOCKED,
        AUTO_REPLY_BLOCKED,
        AUTO_REPLY_POSTED,
    }
)


class CommentAuditErrorCode(StrEnum):
    CHANNEL_NOT_FOUND = "channel_not_found"
    COMMENT_NOT_FOUND = "comment_not_found"
    DRAFT_NOT_FOUND = "draft_not_found"


class CommentAuditError(DomainError):
    """Domain error raised when querying comment/reply audit trails."""

    default_code = "domain.comment_audit_error"
    default_user_message = "Comment audit trail could not be retrieved."
    default_http_status = HTTPStatus.NOT_FOUND

    def __init__(
        self,
        reason: CommentAuditErrorCode,
        message: str,
        *,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        merged = dict(details or {})
        merged["reason"] = reason.value
        super().__init__(
            message,
            code=f"domain.comment_audit.{reason.value}",
        )
        self.reason = reason
        self.details = merged


@dataclass(frozen=True)
class CommentAuditEntry:
    """One immutable event in a comment or reply draft audit trail."""

    event_id: str
    action: str
    actor: Actor
    result: AuditResult
    entity_type: str
    entity_id: str
    channel_id: str | None
    comment_id: str | None
    reply_draft_id: str | None
    youtube_video_id: str | None
    youtube_reply_id: str | None
    occurred_at: datetime
    metadata: Mapping[str, MetadataValue]

    @classmethod
    def from_event(
        cls,
        event: AuditEvent,
        *,
        fallback_channel_id: str | None = None,
        fallback_comment_id: str | None = None,
        fallback_reply_draft_id: str | None = None,
        fallback_youtube_video_id: str | None = None,
    ) -> "CommentAuditEntry":
        meta = event.metadata
        channel_id = _str_or_none(meta.get("channel_id")) or (
            event.entity.id
            if event.entity.type == CHANNEL_ENTITY_TYPE
            else fallback_channel_id
        )
        comment_id = _str_or_none(meta.get("comment_id")) or (
            event.entity.id
            if event.entity.type == COMMENT_ENTITY_TYPE
            else fallback_comment_id
        )
        reply_draft_id = _str_or_none(meta.get("reply_draft_id")) or (
            event.entity.id
            if event.entity.type == REPLY_DRAFT_ENTITY_TYPE
            else fallback_reply_draft_id
        )
        youtube_video_id = (
            _str_or_none(meta.get("youtube_video_id")) or fallback_youtube_video_id
        )
        youtube_reply_id = _str_or_none(meta.get("youtube_reply_id"))
        return cls(
            event_id=event.event_id,
            action=event.action,
            actor=event.actor,
            result=event.result,
            entity_type=event.entity.type,
            entity_id=event.entity.id,
            channel_id=channel_id,
            comment_id=comment_id,
            reply_draft_id=reply_draft_id,
            youtube_video_id=youtube_video_id,
            youtube_reply_id=youtube_reply_id,
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
            "entity": {
                "type": self.entity_type,
                "id": self.entity_id,
            },
            "channel_id": self.channel_id,
            "comment_id": self.comment_id,
            "reply_draft_id": self.reply_draft_id,
            "youtube_video_id": self.youtube_video_id,
            "youtube_reply_id": self.youtube_reply_id,
            "occurred_at": self.occurred_at.isoformat(),
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True)
class CommentAuditTrail:
    """Aggregated audit trail for a channel, comment, or reply draft."""

    channel_id: str
    comment_id: str | None
    reply_draft_id: str | None
    entries: tuple[CommentAuditEntry, ...]
    action_counts: Mapping[str, int]
    posted_reply_ids: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "channel_id": self.channel_id,
            "comment_id": self.comment_id,
            "reply_draft_id": self.reply_draft_id,
            "total_events": len(self.entries),
            "action_counts": dict(self.action_counts),
            "posted_reply_ids": list(self.posted_reply_ids),
            "entries": [entry.as_dict() for entry in self.entries],
        }


class CommentAuditService:
    """Queries immutable audit events for comment sync, classification, and replies
    (#189).
    """

    def __init__(self, database: Database, audit_sink: AuditSink) -> None:
        self._database = database
        self._sink = audit_sink

    def trail_for_reply_draft(self, draft_id: str) -> CommentAuditTrail:
        """Return the chronological audit trail for a single ``ReplyDraft``."""
        with self._database.transaction() as conn:
            repo = CommentRepository(conn)
            draft = repo.get_reply_draft(draft_id)
            if draft is None:
                raise CommentAuditError(
                    CommentAuditErrorCode.DRAFT_NOT_FOUND,
                    f"reply draft {draft_id!r} was not found",
                    details={"reply_draft_id": draft_id},
                )
            comment = repo.get(draft.comment_id)
            if comment is None:
                raise CommentAuditError(
                    CommentAuditErrorCode.COMMENT_NOT_FOUND,
                    f"comment {draft.comment_id!r} was not found",
                    details={"comment_id": draft.comment_id},
                )

        entries: list[CommentAuditEntry] = []
        for event in self._sink.events():
            if event.action not in REPLY_LIFECYCLE_ACTIONS:
                continue
            if self._matches_draft(event, draft_id=draft.id, comment_id=comment.id):
                entries.append(
                    CommentAuditEntry.from_event(
                        event,
                        fallback_channel_id=comment.channel_id,
                        fallback_comment_id=comment.id,
                        fallback_reply_draft_id=draft.id,
                        fallback_youtube_video_id=comment.youtube_video_id,
                    )
                )

        return _build_trail(
            channel_id=comment.channel_id,
            comment_id=comment.id,
            reply_draft_id=draft.id,
            entries=entries,
        )

    def trail_for_comment(self, comment_id: str) -> CommentAuditTrail:
        """Return classification, draft, approval, guard, and post events for a
        comment.
        """
        with self._database.transaction() as conn:
            repo = CommentRepository(conn)
            comment = repo.get(comment_id)
            if comment is None:
                raise CommentAuditError(
                    CommentAuditErrorCode.COMMENT_NOT_FOUND,
                    f"comment {comment_id!r} was not found",
                    details={"comment_id": comment_id},
                )
            draft_ids = {d.id for d in repo.list_reply_drafts(comment.id)}

        entries: list[CommentAuditEntry] = []
        for event in self._sink.events():
            if event.action not in COMMENT_AUDIT_ACTIONS:
                continue
            if event.action == COMMENTS_SYNCED:
                continue
            if self._matches_comment(event, comment_id=comment.id, draft_ids=draft_ids):
                entries.append(
                    CommentAuditEntry.from_event(
                        event,
                        fallback_channel_id=comment.channel_id,
                        fallback_comment_id=comment.id,
                        fallback_youtube_video_id=comment.youtube_video_id,
                    )
                )

        return _build_trail(
            channel_id=comment.channel_id,
            comment_id=comment.id,
            reply_draft_id=None,
            entries=entries,
        )

    def trail_for_channel(self, channel_id: str) -> CommentAuditTrail:
        """Return all comment sync, classification, draft, and reply events for a
        channel.
        """
        with self._database.transaction() as conn:
            if ChannelRepository(conn).get(channel_id) is None:
                raise CommentAuditError(
                    CommentAuditErrorCode.CHANNEL_NOT_FOUND,
                    f"channel {channel_id!r} was not found",
                    details={"channel_id": channel_id},
                )
            repo = CommentRepository(conn)
            comments = repo.list_by_channel(channel_id)
            comment_by_id = {c.id: c for c in comments}
            draft_to_comment: dict[str, str] = {}
            for comment in comments:
                for draft in repo.list_reply_drafts(comment.id):
                    draft_to_comment[draft.id] = comment.id

        entries: list[CommentAuditEntry] = []
        for event in self._sink.events():
            if event.action not in COMMENT_AUDIT_ACTIONS:
                continue
            matched_comment_id = self._resolve_channel_event_comment_id(
                event,
                channel_id=channel_id,
                comment_ids=set(comment_by_id),
                draft_to_comment=draft_to_comment,
            )
            if matched_comment_id is None and not self._is_channel_level_event(
                event, channel_id=channel_id
            ):
                continue
            comment_obj = (
                comment_by_id.get(matched_comment_id)
                if matched_comment_id is not None
                else None
            )
            entries.append(
                CommentAuditEntry.from_event(
                    event,
                    fallback_channel_id=channel_id,
                    fallback_comment_id=matched_comment_id,
                    fallback_youtube_video_id=(
                        comment_obj.youtube_video_id if comment_obj else None
                    ),
                )
            )

        return _build_trail(
            channel_id=channel_id,
            comment_id=None,
            reply_draft_id=None,
            entries=entries,
        )

    @staticmethod
    def _matches_draft(event: AuditEvent, *, draft_id: str, comment_id: str) -> bool:
        if event.entity.type == REPLY_DRAFT_ENTITY_TYPE and event.entity.id == draft_id:
            return True
        if event.metadata.get("reply_draft_id") == draft_id:
            return True
        return bool(
            event.action in {REPLY_DUPLICATE_BLOCKED, AUTO_REPLY_BLOCKED}
            and event.entity.type == COMMENT_ENTITY_TYPE
            and event.entity.id == comment_id
            and event.metadata.get("reply_draft_id") in {None, draft_id}
        )

    @staticmethod
    def _matches_comment(
        event: AuditEvent, *, comment_id: str, draft_ids: set[str]
    ) -> bool:
        if event.entity.type == COMMENT_ENTITY_TYPE and event.entity.id == comment_id:
            return True
        if event.metadata.get("comment_id") == comment_id:
            return True
        if (
            event.entity.type == REPLY_DRAFT_ENTITY_TYPE
            and event.entity.id in draft_ids
        ):
            return True
        meta_draft_id = event.metadata.get("reply_draft_id")
        return bool(isinstance(meta_draft_id, str) and meta_draft_id in draft_ids)

    @staticmethod
    def _is_channel_level_event(event: AuditEvent, *, channel_id: str) -> bool:
        if event.entity.type == CHANNEL_ENTITY_TYPE and event.entity.id == channel_id:
            return True
        return event.metadata.get("channel_id") == channel_id

    @staticmethod
    def _resolve_channel_event_comment_id(
        event: AuditEvent,
        *,
        channel_id: str,
        comment_ids: set[str],
        draft_to_comment: Mapping[str, str],
    ) -> str | None:
        meta_comment_id = event.metadata.get("comment_id")
        if isinstance(meta_comment_id, str) and meta_comment_id in comment_ids:
            return meta_comment_id
        if event.entity.type == COMMENT_ENTITY_TYPE and event.entity.id in comment_ids:
            return event.entity.id
        if (
            event.entity.type == REPLY_DRAFT_ENTITY_TYPE
            and event.entity.id in draft_to_comment
        ):
            return draft_to_comment[event.entity.id]
        meta_draft_id = event.metadata.get("reply_draft_id")
        if isinstance(meta_draft_id, str) and meta_draft_id in draft_to_comment:
            return draft_to_comment[meta_draft_id]
        if event.metadata.get("channel_id") == channel_id and isinstance(
            meta_comment_id, str
        ):
            return meta_comment_id
        return None


def _build_trail(
    *,
    channel_id: str,
    comment_id: str | None,
    reply_draft_id: str | None,
    entries: list[CommentAuditEntry],
) -> CommentAuditTrail:
    counts: dict[str, int] = {}
    posted_ids: list[str] = []
    for entry in entries:
        counts[entry.action] = counts.get(entry.action, 0) + 1
        if (
            entry.action in {REPLY_POSTED, AUTO_REPLY_POSTED}
            and entry.youtube_reply_id is not None
            and entry.youtube_reply_id not in posted_ids
        ):
            posted_ids.append(entry.youtube_reply_id)
    return CommentAuditTrail(
        channel_id=channel_id,
        comment_id=comment_id,
        reply_draft_id=reply_draft_id,
        entries=tuple(entries),
        action_counts=counts,
        posted_reply_ids=tuple(posted_ids),
    )


def _str_or_none(value: MetadataValue | None) -> str | None:
    return value if isinstance(value, str) and value else None
