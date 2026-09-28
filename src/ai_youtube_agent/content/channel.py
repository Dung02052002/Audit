"""Channel entity (Prompt Pack v8, prompt #013), context C1 Channel & Strategy.

A ``Channel`` is the YouTube channel the agent produces for:

- ``id``: a stable internal id. It never changes, even if the YouTube
  identifiers do.
- ``youtube``: ``YouTubeIdentifiers``, with the required ``channel_id``
  (``UC`` followed by 22 characters) and an optional ``@handle``.
- ``status``: one of ``ChannelStatus``. A new channel starts ``PENDING``.
- ``created_at`` and ``updated_at``: timezone-aware UTC.

A channel is frozen. ``with_status`` and ``rename`` return a new channel with
the same id and ``created_at`` and a new ``updated_at``. Transition rules
between statuses are not defined yet, except that ``ARCHIVED`` is final: an
archived channel is read-only. The channel is user-owned configuration, and
the AI never changes it on its own (R-09). Persistence comes with #029.
"""

import re
import uuid
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any

from ai_youtube_agent.core.errors import DomainError

CHANNEL_ID_PATTERN = re.compile(r"^UC[A-Za-z0-9_-]{22}$")
HANDLE_PATTERN = re.compile(r"^@[A-Za-z0-9._-]{3,30}$")
Clock = Callable[[], datetime]


class ChannelStatus(StrEnum):
    PENDING = "pending"
    ACTIVE = "active"
    PAUSED = "paused"
    DISCONNECTED = "disconnected"
    ARCHIVED = "archived"


class ChannelArchivedError(DomainError):
    default_code = "domain.channel_archived"
    default_user_message = "This channel is archived and cannot be changed."


@dataclass(frozen=True)
class YouTubeIdentifiers:
    channel_id: str
    handle: str | None = None

    def __post_init__(self) -> None:
        if not CHANNEL_ID_PATTERN.match(self.channel_id):
            raise ValueError(
                f"channel_id {self.channel_id!r} must be 'UC' followed by "
                "22 letters, digits, '_' or '-'"
            )
        if self.handle is not None and not HANDLE_PATTERN.match(self.handle):
            raise ValueError(
                f"handle {self.handle!r} must be '@' followed by 3 to 30 "
                "letters, digits, '.', '_' or '-'"
            )


@dataclass(frozen=True)
class Channel:
    id: str
    title: str
    youtube: YouTubeIdentifiers
    status: ChannelStatus
    created_at: datetime
    updated_at: datetime

    def __post_init__(self) -> None:
        if not self.id:
            raise ValueError("channel id must not be empty")
        if not self.title.strip():
            raise ValueError("channel title must not be empty")
        for name in ("created_at", "updated_at"):
            if getattr(self, name).utcoffset() != timedelta(0):
                raise ValueError(f"{name} must be timezone-aware UTC")
        if self.updated_at < self.created_at:
            raise ValueError("updated_at must not be earlier than created_at")

    @classmethod
    def create(
        cls,
        title: str,
        youtube: YouTubeIdentifiers,
        *,
        clock: Clock | None = None,
    ) -> "Channel":
        now = _now(clock)
        return cls(
            id=uuid.uuid4().hex,
            title=title.strip(),
            youtube=youtube,
            status=ChannelStatus.PENDING,
            created_at=now,
            updated_at=now,
        )

    @property
    def is_archived(self) -> bool:
        return self.status is ChannelStatus.ARCHIVED

    def with_status(
        self, status: ChannelStatus, *, clock: Clock | None = None
    ) -> "Channel":
        if status is self.status:
            return self
        self._ensure_not_archived()
        return replace(self, status=status, updated_at=_now(clock))

    def rename(self, title: str, *, clock: Clock | None = None) -> "Channel":
        title = title.strip()
        if title == self.title:
            return self
        self._ensure_not_archived()
        return replace(self, title=title, updated_at=_now(clock))

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "youtube": {
                "channel_id": self.youtube.channel_id,
                "handle": self.youtube.handle,
            },
            "status": self.status.value,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }

    def _ensure_not_archived(self) -> None:
        if self.is_archived:
            raise ChannelArchivedError(f"channel {self.id} is archived")


def _now(clock: Clock | None) -> datetime:
    return clock() if clock else datetime.now(UTC)
