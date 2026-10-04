"""Asset entity (Prompt Pack v8, prompt #075), context C6 Rights & Policy.

An ``Asset`` is one image, clip, sound, font or other file a channel may use in
its videos, with the category of its provenance:

- ``id``: a stable internal id.
- ``channel_id``: the ``Channel.id`` that owns the asset.
- ``kind``: ``AssetKind`` image, video_clip, audio, music, voice, font,
  subtitle, template or other.
- ``category``: ``AssetCategory`` generated, licensed, public_domain,
  user_owned or unknown. The category is declared by the caller, never derived.
  ``unknown`` means the provenance is not declared yet.
- ``title``: 1-200 characters, with runs of whitespace collapsed to one space.
  The text is otherwise kept as given: no Unicode normalisation (NFC) is applied,
  like ``Source`` titles.
- ``source``: 1-500 characters: a provider name, a URL, or ``user``.
- ``artifact_id``: optional ``Artifact.id`` when the system generated the file.
- ``license_ref``: optional licence text or reference, 1-500 when set.
- ``attribution``: optional credit line, 1-500 when set.
- ``owner``: optional owner name, 1-200 when set.
- ``created_at``: timezone-aware UTC.

Category rules: ``licensed`` needs ``license_ref``; ``user_owned`` needs
``owner``; ``generated`` needs a ``source`` that is not ``user`` (a provider)
and is the only category that may carry ``artifact_id``; ``attribution`` is
optional for every category (typical for ``licensed`` and ``public_domain``);
``unknown`` needs nothing beyond the base fields. An asset is frozen and has no
mutators.

``RightsRecord.asset_ref`` (B-019) will point at ``Asset.id`` once the Asset
Registry (#076) exists. #076 stores and registers assets, #077 records the
provenance and licence metadata, and #078 scores the rights risk (it will treat
``unknown`` as high risk). This module defines no risk level.
"""

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any

MAX_TITLE = 200
MAX_SOURCE = 500
MAX_LICENSE_REF = 500
MAX_ATTRIBUTION = 500
MAX_OWNER = 200
USER_SOURCE = "user"
Clock = Callable[[], datetime]


class AssetCategory(StrEnum):
    GENERATED = "generated"
    LICENSED = "licensed"
    PUBLIC_DOMAIN = "public_domain"
    USER_OWNED = "user_owned"
    UNKNOWN = "unknown"


class AssetKind(StrEnum):
    IMAGE = "image"
    VIDEO_CLIP = "video_clip"
    AUDIO = "audio"
    MUSIC = "music"
    VOICE = "voice"
    FONT = "font"
    SUBTITLE = "subtitle"
    TEMPLATE = "template"
    OTHER = "other"


@dataclass(frozen=True)
class Asset:
    id: str
    channel_id: str
    kind: AssetKind
    category: AssetCategory
    title: str
    source: str
    artifact_id: str | None
    license_ref: str | None
    attribution: str | None
    owner: str | None
    created_at: datetime

    def __post_init__(self) -> None:
        for name in ("id", "channel_id"):
            if not getattr(self, name).strip():
                raise ValueError(f"{name} must not be empty")
        if not isinstance(self.kind, AssetKind):
            raise TypeError("kind must be an AssetKind")
        if not isinstance(self.category, AssetCategory):
            raise TypeError("category must be an AssetCategory")
        if self.title != _collapse(self.title):
            raise ValueError("title must have single spaces and no outer whitespace")
        _require_length(self.title, "title", MAX_TITLE)
        _require_length(self.source, "source", MAX_SOURCE)
        if self.artifact_id is not None and not self.artifact_id.strip():
            raise ValueError("artifact_id must not be empty")
        _require_optional(self.license_ref, "license_ref", MAX_LICENSE_REF)
        _require_optional(self.attribution, "attribution", MAX_ATTRIBUTION)
        _require_optional(self.owner, "owner", MAX_OWNER)
        if self.created_at.utcoffset() != timedelta(0):
            raise ValueError("created_at must be timezone-aware UTC")
        self._check_category()

    @classmethod
    def create(
        cls,
        channel_id: str,
        kind: AssetKind,
        category: AssetCategory,
        *,
        title: str,
        source: str,
        artifact_id: str | None = None,
        license_ref: str | None = None,
        attribution: str | None = None,
        owner: str | None = None,
        clock: Clock | None = None,
    ) -> "Asset":
        return cls(
            id=uuid.uuid4().hex,
            channel_id=channel_id,
            kind=kind,
            category=category,
            title=_collapse(title),
            source=source.strip(),
            artifact_id=_strip(artifact_id),
            license_ref=_strip(license_ref),
            attribution=_strip(attribution),
            owner=_strip(owner),
            created_at=_now(clock),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "channel_id": self.channel_id,
            "kind": self.kind.value,
            "category": self.category.value,
            "title": self.title,
            "source": self.source,
            "artifact_id": self.artifact_id,
            "license_ref": self.license_ref,
            "attribution": self.attribution,
            "owner": self.owner,
            "created_at": self.created_at.isoformat(),
        }

    def _check_category(self) -> None:
        category = self.category
        if category is AssetCategory.LICENSED and self.license_ref is None:
            raise ValueError("a licensed asset needs a license_ref")
        if category is AssetCategory.USER_OWNED and self.owner is None:
            raise ValueError("a user_owned asset needs an owner")
        if category is AssetCategory.GENERATED:
            if self.source.strip().casefold() == USER_SOURCE:
                raise ValueError("a generated asset needs a provider as its source")
        elif self.artifact_id is not None:
            raise ValueError("only a generated asset may have an artifact_id")


def _collapse(value: str) -> str:
    return " ".join(value.split())


def _require_length(value: str, name: str, limit: int) -> None:
    if not value.strip():
        raise ValueError(f"{name} must not be empty")
    if len(value) > limit:
        raise ValueError(f"{name} must be at most {limit} characters")


def _require_optional(value: str | None, name: str, limit: int) -> None:
    if value is not None:
        _require_length(value, name, limit)


def _strip(value: str | None) -> str | None:
    return value.strip() if value is not None else None


def _now(clock: Clock | None) -> datetime:
    return clock() if clock else datetime.now(UTC)
