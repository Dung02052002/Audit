"""Asset usage (Prompt Pack v8, prompt #076), context C6 Rights & Policy.

An ``AssetUsage`` records that one ``Asset`` is used by one content item of the
same channel (the registry, ``content/asset_registry.py``, checks the channel):

- ``id``: a stable internal id.
- ``asset_id``: the ``Asset.id``.
- ``content_item_id``: the ``ContentItem.id`` that uses the asset.
- ``purpose``: optional free text, runs of whitespace collapsed, 1-200.
- ``attached_by``: the ``Actor`` who attached the asset.
- ``created_at``: timezone-aware UTC.

A pair of asset and content item has at most one usage, and an asset can be used
by several items. A usage is frozen and has no mutators.
"""

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from ai_youtube_agent.core.audit import Actor

MAX_PURPOSE = 200
Clock = Callable[[], datetime]


@dataclass(frozen=True)
class AssetUsage:
    id: str
    asset_id: str
    content_item_id: str
    purpose: str | None
    attached_by: Actor
    created_at: datetime

    def __post_init__(self) -> None:
        for name in ("id", "asset_id", "content_item_id"):
            if not getattr(self, name).strip():
                raise ValueError(f"{name} must not be empty")
        if self.purpose is not None:
            if self.purpose != collapse_purpose(self.purpose):
                raise ValueError(
                    "purpose must have single spaces and no outer whitespace"
                )
            if not self.purpose:
                raise ValueError("purpose must not be empty")
            if len(self.purpose) > MAX_PURPOSE:
                raise ValueError(f"purpose must be at most {MAX_PURPOSE} characters")
        if self.created_at.utcoffset() != timedelta(0):
            raise ValueError("created_at must be timezone-aware UTC")

    @classmethod
    def create(
        cls,
        asset_id: str,
        content_item_id: str,
        *,
        purpose: str | None = None,
        attached_by: Actor,
        clock: Clock | None = None,
    ) -> "AssetUsage":
        return cls(
            id=uuid.uuid4().hex,
            asset_id=asset_id,
            content_item_id=content_item_id,
            purpose=collapse_purpose(purpose) if purpose is not None else None,
            attached_by=attached_by,
            created_at=clock() if clock else datetime.now(UTC),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "asset_id": self.asset_id,
            "content_item_id": self.content_item_id,
            "purpose": self.purpose,
            "attached_by": {
                "kind": self.attached_by.kind.value,
                "id": self.attached_by.id,
            },
            "created_at": self.created_at.isoformat(),
        }


def collapse_purpose(value: str) -> str:
    return " ".join(value.split())
