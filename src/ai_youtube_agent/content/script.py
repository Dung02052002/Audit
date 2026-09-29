"""Script entity (Prompt Pack v8, prompt #017), context C5 Script & Fact Check.

Three frozen entities, related by id:

- ``Script``: one immutable version of the script of a content item. It has its
  own id, ``content_item_id``, a version that starts at 1, the text and a UTC
  ``created_at``. ``next_version`` returns a new record with version + 1 and
  refuses unchanged text.
- ``Claim``: a factual claim made by one exact script version (``script_id``).
  A new script version starts with no claims; they are extracted again (#068).
- ``Evidence``: links a claim to a research source. ``source_ref`` is the
  opaque id of that source, defined later by #055 and matched by #069. The
  optional ``excerpt`` is the supporting passage.

Adding claims or evidence never creates a new script version. Sections and the
duration target come with #064, fact-check results with #070 and diff metadata
with #073. Persistence stores the relationships (#029, #030).
"""

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

Clock = Callable[[], datetime]


@dataclass(frozen=True)
class Script:
    id: str
    content_item_id: str
    version: int
    text: str
    created_at: datetime

    def __post_init__(self) -> None:
        _require_ids(self, "id", "content_item_id")
        if (
            isinstance(self.version, bool)
            or not isinstance(self.version, int)
            or self.version < 1
        ):
            raise ValueError("version must be a whole number of 1 or more")
        _require_text("script text", self.text)
        _require_utc(self.created_at)

    @classmethod
    def create(
        cls, content_item_id: str, text: str, *, clock: Clock | None = None
    ) -> "Script":
        return cls(
            id=_new_id(),
            content_item_id=content_item_id,
            version=1,
            text=text.strip(),
            created_at=_now(clock),
        )

    def next_version(self, text: str, *, clock: Clock | None = None) -> "Script":
        text = text.strip()
        if text == self.text:
            raise ValueError("a new version needs different script text")
        return Script(
            id=_new_id(),
            content_item_id=self.content_item_id,
            version=self.version + 1,
            text=text,
            created_at=_now(clock),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "content_item_id": self.content_item_id,
            "version": self.version,
            "text": self.text,
            "created_at": self.created_at.isoformat(),
        }


@dataclass(frozen=True)
class Claim:
    id: str
    script_id: str
    text: str
    created_at: datetime

    def __post_init__(self) -> None:
        _require_ids(self, "id", "script_id")
        _require_text("claim text", self.text)
        _require_utc(self.created_at)

    @classmethod
    def create(
        cls, script_id: str, text: str, *, clock: Clock | None = None
    ) -> "Claim":
        return cls(
            id=_new_id(), script_id=script_id, text=text.strip(), created_at=_now(clock)
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "script_id": self.script_id,
            "text": self.text,
            "created_at": self.created_at.isoformat(),
        }


@dataclass(frozen=True)
class Evidence:
    id: str
    claim_id: str
    source_ref: str
    excerpt: str | None
    created_at: datetime

    def __post_init__(self) -> None:
        _require_ids(self, "id", "claim_id", "source_ref")
        if self.excerpt is not None:
            _require_text("evidence excerpt", self.excerpt)
        _require_utc(self.created_at)

    @classmethod
    def create(
        cls,
        claim_id: str,
        source_ref: str,
        excerpt: str | None = None,
        *,
        clock: Clock | None = None,
    ) -> "Evidence":
        return cls(
            id=_new_id(),
            claim_id=claim_id,
            source_ref=source_ref,
            excerpt=excerpt.strip() if excerpt is not None else None,
            created_at=_now(clock),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "claim_id": self.claim_id,
            "source_ref": self.source_ref,
            "excerpt": self.excerpt,
            "created_at": self.created_at.isoformat(),
        }


def _require_ids(entity: object, *names: str) -> None:
    for name in names:
        value = getattr(entity, name)
        if not value or not value.strip():
            raise ValueError(f"{name} must not be empty")


def _require_text(name: str, value: str) -> None:
    if not value.strip():
        raise ValueError(f"{name} must not be empty")


def _require_utc(moment: datetime) -> None:
    if moment.utcoffset() != timedelta(0):
        raise ValueError("created_at must be timezone-aware UTC")


def _new_id() -> str:
    return uuid.uuid4().hex


def _now(clock: Clock | None) -> datetime:
    return clock() if clock else datetime.now(UTC)
