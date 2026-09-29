"""Artifact entity (Prompt Pack v8, prompt #016), context C2 Content Lifecycle.

An ``Artifact`` is one immutable version of a file produced for a content item:

- ``id``: a stable id for this exact version. An approval (#021) binds to it.
- ``content_item_id``: the ``ContentItem.id`` the file belongs to.
- ``kind``: one of ``ArtifactKind``: video, audio, subtitles, thumbnail or
  metadata. Every kind is a stored file; the metadata artifact is a document
  such as JSON with the title, description and tags.
- ``version``: starts at 1 and grows by one per new version of the same item
  and kind.
- ``uri``: an opaque storage reference. No storage backend is chosen yet.
- ``sha256``, ``size_bytes`` and ``media_type``: what the file contains. The
  checksum is what tells a changed artifact apart (#035).
- ``created_at``: timezone-aware UTC.

A version never changes. ``next_version`` returns a new record with a new id
and ``version + 1`` and refuses content that has the same checksum, so each
version is real new content. A unique (content_item_id, kind, version) is
enforced by persistence (#029, #030).
"""

import re
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any

SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
MEDIA_TYPE_PATTERN = re.compile(r"^[a-z]+/[a-z0-9][a-z0-9.+-]*$")
Clock = Callable[[], datetime]


class ArtifactKind(StrEnum):
    VIDEO = "video"
    AUDIO = "audio"
    SUBTITLES = "subtitles"
    THUMBNAIL = "thumbnail"
    METADATA = "metadata"


@dataclass(frozen=True)
class Artifact:
    id: str
    content_item_id: str
    kind: ArtifactKind
    version: int
    uri: str
    sha256: str
    size_bytes: int
    media_type: str
    created_at: datetime

    def __post_init__(self) -> None:
        for name in ("id", "content_item_id"):
            if not getattr(self, name):
                raise ValueError(f"{name} must not be empty")
        if not isinstance(self.kind, ArtifactKind):
            raise TypeError("kind must be an ArtifactKind")
        _require_whole(self.version, "version")
        if not self.uri or any(char.isspace() for char in self.uri):
            raise ValueError("uri must not be empty or contain whitespace")
        if not SHA256_PATTERN.match(self.sha256):
            raise ValueError("sha256 must be 64 lowercase hexadecimal characters")
        _require_whole(self.size_bytes, "size_bytes")
        if not MEDIA_TYPE_PATTERN.match(self.media_type):
            raise ValueError(
                f"media_type {self.media_type!r} must look like 'video/mp4'"
            )
        if self.created_at.utcoffset() != timedelta(0):
            raise ValueError("created_at must be timezone-aware UTC")

    @classmethod
    def create(
        cls,
        content_item_id: str,
        kind: ArtifactKind,
        *,
        uri: str,
        sha256: str,
        size_bytes: int,
        media_type: str,
        clock: Clock | None = None,
    ) -> "Artifact":
        return cls(
            id=uuid.uuid4().hex,
            content_item_id=content_item_id,
            kind=kind,
            version=1,
            uri=uri,
            sha256=sha256,
            size_bytes=size_bytes,
            media_type=media_type,
            created_at=_now(clock),
        )

    def next_version(
        self,
        *,
        uri: str,
        sha256: str,
        size_bytes: int,
        media_type: str,
        clock: Clock | None = None,
    ) -> "Artifact":
        if sha256 == self.sha256:
            raise ValueError("a new version needs content with a different sha256")
        return Artifact(
            id=uuid.uuid4().hex,
            content_item_id=self.content_item_id,
            kind=self.kind,
            version=self.version + 1,
            uri=uri,
            sha256=sha256,
            size_bytes=size_bytes,
            media_type=media_type,
            created_at=_now(clock),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "content_item_id": self.content_item_id,
            "kind": self.kind.value,
            "version": self.version,
            "uri": self.uri,
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
            "media_type": self.media_type,
            "created_at": self.created_at.isoformat(),
        }


def _require_whole(value: object, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{name} must be a whole number of 1 or more")


def _now(clock: Clock | None) -> datetime:
    return clock() if clock else datetime.now(UTC)
