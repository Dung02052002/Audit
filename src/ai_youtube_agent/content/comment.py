"""Comment entity (Prompt Pack v8, prompt #026), context C15 Community.

Three frozen entities, related by id:

- ``Comment``: a synced YouTube comment. It keeps only what the community
  features need: the channel, the YouTube comment and video ids, an optional
  parent comment id for replies in a thread, the author's display name, the
  text and a UTC ``published_at``. No author channel id, email or avatar.
- ``CommentClassification``: one ``CommentLabel`` given to a comment by an
  ``Actor`` (usually the classifier, #184), with an optional rationale and a
  UTC ``classified_at``. Reclassifying adds a new record, so history is kept.
- ``ReplyDraft``: a drafted reply with its ``ReplyStatus``. A new draft is
  always ``DRAFT``, and there is no method to approve or post it:
  ``AUTO_REPLY_ENABLED`` stays false, and approval, posting and the duplicate
  guard come with #186–#188. A stored posted reply carries its YouTube reply id.
"""

import re
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any

from ai_youtube_agent.core.audit import Actor

VIDEO_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{11}$")
Clock = Callable[[], datetime]


class CommentLabel(StrEnum):
    NEEDS_ATTENTION = "needs_attention"
    MODERATION = "moderation"
    REPLY_CANDIDATE = "reply_candidate"
    NO_ACTION = "no_action"


class ReplyStatus(StrEnum):
    DRAFT = "draft"
    APPROVED = "approved"
    POSTED = "posted"
    FAILED = "failed"
    DISCARDED = "discarded"


@dataclass(frozen=True)
class Comment:
    id: str
    channel_id: str
    youtube_comment_id: str
    youtube_video_id: str
    parent_comment_id: str | None
    author_display_name: str
    text: str
    published_at: datetime

    def __post_init__(self) -> None:
        _require_text(
            self, "id", "channel_id", "youtube_comment_id", "author_display_name"
        )
        _require_text(self, "text")
        if not VIDEO_ID_PATTERN.match(self.youtube_video_id):
            raise ValueError(
                f"youtube_video_id {self.youtube_video_id!r} must be 11 letters, "
                "digits, '_' or '-'"
            )
        if self.parent_comment_id is not None:
            _require_text(self, "parent_comment_id")
            if self.parent_comment_id == self.youtube_comment_id:
                raise ValueError("a comment cannot be its own parent")
        _require_utc(self.published_at, "published_at")

    @classmethod
    def create(
        cls,
        channel_id: str,
        youtube_comment_id: str,
        youtube_video_id: str,
        *,
        author_display_name: str,
        text: str,
        published_at: datetime,
        parent_comment_id: str | None = None,
    ) -> "Comment":
        return cls(
            id=_new_id(),
            channel_id=channel_id,
            youtube_comment_id=youtube_comment_id,
            youtube_video_id=youtube_video_id,
            parent_comment_id=parent_comment_id,
            author_display_name=author_display_name,
            text=text,
            published_at=published_at,
        )

    @property
    def is_reply(self) -> bool:
        return self.parent_comment_id is not None

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "channel_id": self.channel_id,
            "youtube_comment_id": self.youtube_comment_id,
            "youtube_video_id": self.youtube_video_id,
            "parent_comment_id": self.parent_comment_id,
            "author_display_name": self.author_display_name,
            "text": self.text,
            "published_at": self.published_at.isoformat(),
        }


@dataclass(frozen=True)
class CommentClassification:
    id: str
    comment_id: str
    label: CommentLabel
    classified_by: Actor
    rationale: str | None
    classified_at: datetime

    def __post_init__(self) -> None:
        _require_text(self, "id", "comment_id")
        if not isinstance(self.label, CommentLabel):
            raise TypeError("label must be a CommentLabel")
        if not isinstance(self.classified_by, Actor):
            raise TypeError("classified_by must be an Actor")
        if self.rationale is not None:
            _require_text(self, "rationale")
        _require_utc(self.classified_at, "classified_at")

    @classmethod
    def create(
        cls,
        comment_id: str,
        label: CommentLabel,
        *,
        classified_by: Actor,
        rationale: str | None = None,
        clock: Clock | None = None,
    ) -> "CommentClassification":
        return cls(
            id=_new_id(),
            comment_id=comment_id,
            label=label,
            classified_by=classified_by,
            rationale=rationale.strip() if rationale is not None else None,
            classified_at=_now(clock),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "comment_id": self.comment_id,
            "label": self.label.value,
            "classified_by": _actor_dict(self.classified_by),
            "rationale": self.rationale,
            "classified_at": self.classified_at.isoformat(),
        }


@dataclass(frozen=True)
class ReplyDraft:
    id: str
    comment_id: str
    text: str
    status: ReplyStatus
    created_by: Actor
    created_at: datetime
    youtube_reply_id: str | None = None

    def __post_init__(self) -> None:
        _require_text(self, "id", "comment_id", "text")
        if not isinstance(self.status, ReplyStatus):
            raise TypeError("status must be a ReplyStatus")
        if not isinstance(self.created_by, Actor):
            raise TypeError("created_by must be an Actor")
        _require_utc(self.created_at, "created_at")
        if self.status is ReplyStatus.POSTED:
            if self.youtube_reply_id is None:
                raise ValueError("a posted reply needs its youtube_reply_id")
            _require_text(self, "youtube_reply_id")
        elif self.youtube_reply_id is not None:
            raise ValueError("only a posted reply has a youtube_reply_id")

    @classmethod
    def create(
        cls,
        comment_id: str,
        text: str,
        *,
        created_by: Actor,
        clock: Clock | None = None,
    ) -> "ReplyDraft":
        return cls(
            id=_new_id(),
            comment_id=comment_id,
            text=text.strip(),
            status=ReplyStatus.DRAFT,
            created_by=created_by,
            created_at=_now(clock),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "comment_id": self.comment_id,
            "text": self.text,
            "status": self.status.value,
            "created_by": _actor_dict(self.created_by),
            "created_at": self.created_at.isoformat(),
            "youtube_reply_id": self.youtube_reply_id,
        }


def _require_text(entity: object, *names: str) -> None:
    for name in names:
        if not getattr(entity, name).strip():
            raise ValueError(f"{name} must not be empty")


def _require_utc(moment: datetime, name: str) -> None:
    if moment.utcoffset() != timedelta(0):
        raise ValueError(f"{name} must be timezone-aware UTC")


def _actor_dict(actor: Actor) -> dict[str, str]:
    return {"kind": actor.kind.value, "id": actor.id}


def _new_id() -> str:
    return uuid.uuid4().hex


def _now(clock: Clock | None) -> datetime:
    return clock() if clock else datetime.now(UTC)
