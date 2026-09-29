"""ContentItem entity (Prompt Pack v8, prompt #015), context C2 Content Lifecycle.

A ``ContentItem`` is the common content entity shared by both content types:

- ``id``: a stable internal id.
- ``channel_id``: the ``Channel.id`` the item is produced for.
- ``strategy_profile_id`` and ``strategy_version``: the ``StrategyProfile`` and
  its version the item was produced under, so a later strategy change never
  rewrites what an item was made for (R-09).
- ``content_type``: ``SHORTS`` or ``LONGFORM``, the only two content types.
- ``title``: a working title.
- ``status``: one of the ten ``ContentStatus`` values listed in #031. A new
  item starts ``DRAFT``.
- ``created_at`` and ``updated_at``: timezone-aware UTC.

An item is frozen. ``with_status`` and ``rename`` return a new item with the
same id and ``created_at`` and a new ``updated_at``. No transition rules exist
yet: #032 adds the allowed and blocked transitions. The entity does not read
feature flags, so ``LONGFORM_ENABLED`` is enforced by gates and the pipeline.
"""

import uuid
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any

Clock = Callable[[], datetime]


class ContentType(StrEnum):
    SHORTS = "shorts"
    LONGFORM = "longform"


class ContentStatus(StrEnum):
    DRAFT = "draft"
    GENERATING = "generating"
    TESTING = "testing"
    PREVIEW_READY = "preview_ready"
    AWAITING_APPROVAL = "awaiting_approval"
    APPROVED = "approved"
    PUBLISHING = "publishing"
    PUBLISHED = "published"
    REJECTED = "rejected"
    FAILED = "failed"


@dataclass(frozen=True)
class ContentItem:
    id: str
    channel_id: str
    strategy_profile_id: str
    strategy_version: int
    content_type: ContentType
    title: str
    status: ContentStatus
    created_at: datetime
    updated_at: datetime

    def __post_init__(self) -> None:
        for name in ("id", "channel_id", "strategy_profile_id"):
            if not getattr(self, name):
                raise ValueError(f"{name} must not be empty")
        if (
            isinstance(self.strategy_version, bool)
            or not isinstance(self.strategy_version, int)
            or self.strategy_version < 1
        ):
            raise ValueError("strategy_version must be a whole number of 1 or more")
        if not isinstance(self.content_type, ContentType):
            raise TypeError("content_type must be a ContentType")
        if not isinstance(self.status, ContentStatus):
            raise TypeError("status must be a ContentStatus")
        if not self.title.strip():
            raise ValueError("content title must not be empty")
        for name in ("created_at", "updated_at"):
            if getattr(self, name).utcoffset() != timedelta(0):
                raise ValueError(f"{name} must be timezone-aware UTC")
        if self.updated_at < self.created_at:
            raise ValueError("updated_at must not be earlier than created_at")

    @classmethod
    def create(
        cls,
        channel_id: str,
        strategy_profile_id: str,
        strategy_version: int,
        content_type: ContentType,
        title: str,
        *,
        clock: Clock | None = None,
    ) -> "ContentItem":
        now = _now(clock)
        return cls(
            id=uuid.uuid4().hex,
            channel_id=channel_id,
            strategy_profile_id=strategy_profile_id,
            strategy_version=strategy_version,
            content_type=content_type,
            title=title.strip(),
            status=ContentStatus.DRAFT,
            created_at=now,
            updated_at=now,
        )

    def with_status(
        self, status: ContentStatus, *, clock: Clock | None = None
    ) -> "ContentItem":
        if status is self.status:
            return self
        return replace(self, status=status, updated_at=_now(clock))

    def rename(self, title: str, *, clock: Clock | None = None) -> "ContentItem":
        title = title.strip()
        if title == self.title:
            return self
        return replace(self, title=title, updated_at=_now(clock))

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "channel_id": self.channel_id,
            "strategy_profile_id": self.strategy_profile_id,
            "strategy_version": self.strategy_version,
            "content_type": self.content_type.value,
            "title": self.title,
            "status": self.status.value,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }


def _now(clock: Clock | None) -> datetime:
    return clock() if clock else datetime.now(UTC)
