"""Rights entity (Prompt Pack v8, prompt #019), context C6 Rights & Policy.

A ``RightsRecord`` holds the provenance and rights status of one asset used by
a content item:

- ``id``: a stable internal id.
- ``content_item_id``: the ``ContentItem.id`` that uses the asset.
- ``asset_ref``: an opaque asset id. It can be an ``Artifact`` id or an
  external asset such as stock music; the registry (#076) defines it later.
- ``source``: where the asset came from, such as a provider or a URL.
- ``license``: optional licence text or reference, such as ``CC-BY-4.0``.
- ``risk_level``: ``RiskLevel`` unknown, low, medium or high. A new record
  starts unknown.
- ``resolution``: ``RiskResolution`` unresolved or resolved, with
  ``resolved_by`` and ``resolved_at`` set only while resolved.
- ``created_at`` and ``updated_at``: timezone-aware UTC.

Any actor, such as the risk engine (#078), may set the risk level. Only a user
may resolve a risk (``RightsResolutionNotAllowedError``). Changing the level or
the licence makes the record unresolved again. A record is frozen: every change
returns a new record. Asset categories come with #075, and blocking a publish
on unresolved high risk is the rights gate (#038).
"""

import uuid
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any

from ai_youtube_agent.core.audit import Actor, ActorKind
from ai_youtube_agent.core.errors import DomainError

Clock = Callable[[], datetime]


class RiskLevel(StrEnum):
    UNKNOWN = "unknown"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class RiskResolution(StrEnum):
    UNRESOLVED = "unresolved"
    RESOLVED = "resolved"


class RightsResolutionNotAllowedError(DomainError):
    default_code = "domain.rights_resolution_not_allowed"
    default_user_message = "Only a user can resolve a rights risk."


@dataclass(frozen=True)
class RightsRecord:
    id: str
    content_item_id: str
    asset_ref: str
    source: str
    license: str | None
    risk_level: RiskLevel
    resolution: RiskResolution
    resolved_by: Actor | None
    resolved_at: datetime | None
    created_at: datetime
    updated_at: datetime

    def __post_init__(self) -> None:
        for name in ("id", "content_item_id", "asset_ref", "source"):
            if not getattr(self, name).strip():
                raise ValueError(f"{name} must not be empty")
        if self.license is not None and not self.license.strip():
            raise ValueError("license must not be empty")
        if not isinstance(self.risk_level, RiskLevel):
            raise TypeError("risk_level must be a RiskLevel")
        if not isinstance(self.resolution, RiskResolution):
            raise TypeError("resolution must be a RiskResolution")
        for name in ("created_at", "updated_at"):
            _require_utc(getattr(self, name), name)
        if self.updated_at < self.created_at:
            raise ValueError("updated_at must not be earlier than created_at")
        self._check_resolution()

    @classmethod
    def create(
        cls,
        content_item_id: str,
        asset_ref: str,
        *,
        source: str,
        license: str | None = None,
        clock: Clock | None = None,
    ) -> "RightsRecord":
        now = _now(clock)
        return cls(
            id=uuid.uuid4().hex,
            content_item_id=content_item_id,
            asset_ref=asset_ref,
            source=source.strip(),
            license=_strip(license),
            risk_level=RiskLevel.UNKNOWN,
            resolution=RiskResolution.UNRESOLVED,
            resolved_by=None,
            resolved_at=None,
            created_at=now,
            updated_at=now,
        )

    @property
    def is_resolved(self) -> bool:
        return self.resolution is RiskResolution.RESOLVED

    def with_risk_level(
        self, level: RiskLevel, *, clock: Clock | None = None
    ) -> "RightsRecord":
        if level is self.risk_level:
            return self
        return self._unresolved(clock, risk_level=level)

    def with_license(
        self, license: str | None, *, clock: Clock | None = None
    ) -> "RightsRecord":
        license = _strip(license)
        if license == self.license:
            return self
        return self._unresolved(clock, license=license)

    def resolve(self, *, actor: Actor, clock: Clock | None = None) -> "RightsRecord":
        _ensure_user(actor)
        if self.is_resolved:
            return self
        now = _now(clock)
        return replace(
            self,
            resolution=RiskResolution.RESOLVED,
            resolved_by=actor,
            resolved_at=now,
            updated_at=now,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "content_item_id": self.content_item_id,
            "asset_ref": self.asset_ref,
            "source": self.source,
            "license": self.license,
            "risk_level": self.risk_level.value,
            "resolution": self.resolution.value,
            "resolved_by": (
                {"kind": self.resolved_by.kind.value, "id": self.resolved_by.id}
                if self.resolved_by
                else None
            ),
            "resolved_at": self.resolved_at.isoformat() if self.resolved_at else None,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }

    def _unresolved(self, clock: Clock | None, **changes: Any) -> "RightsRecord":
        return replace(
            self,
            **changes,
            resolution=RiskResolution.UNRESOLVED,
            resolved_by=None,
            resolved_at=None,
            updated_at=_now(clock),
        )

    def _check_resolution(self) -> None:
        if not self.is_resolved:
            if self.resolved_by is not None or self.resolved_at is not None:
                raise ValueError("an unresolved record has no resolver")
            return
        if self.resolved_by is None or self.resolved_at is None:
            raise ValueError("a resolved record needs resolved_by and resolved_at")
        _ensure_user(self.resolved_by)
        _require_utc(self.resolved_at, "resolved_at")
        if not self.created_at <= self.resolved_at <= self.updated_at:
            raise ValueError("resolved_at must be between created_at and updated_at")


def _ensure_user(actor: Actor) -> None:
    if actor.kind is not ActorKind.USER:
        raise RightsResolutionNotAllowedError(
            f"actor {actor.kind.value}:{actor.id} may not resolve a rights risk"
        )


def _require_utc(moment: datetime, name: str) -> None:
    if moment.utcoffset() != timedelta(0):
        raise ValueError(f"{name} must be timezone-aware UTC")


def _strip(value: str | None) -> str | None:
    return value.strip() if value is not None else None


def _now(clock: Clock | None) -> datetime:
    return clock() if clock else datetime.now(UTC)
