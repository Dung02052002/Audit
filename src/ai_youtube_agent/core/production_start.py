"""Production start log (Prompt Pack v8, prompt #036), context C3 Control Gates.

A ``ProductionStart`` records that a content item started production: the move
from draft to generating. The daily limit gate (#036) counts these per channel,
content type and UTC day. The log is append-only (migration 0002), so a
restart after failed or rejected, which goes back through draft, is a new
start and counts again.

The writer that moves an item and logs its start is ``start_production`` in
``core/production.py``.
"""

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

from ai_youtube_agent.core.content_item import ContentItem, ContentType
from ai_youtube_agent.core.errors import DomainError


class ProductionStartError(DomainError):
    default_code = "domain.production_start"
    default_user_message = "Only a draft can start production."


@dataclass(frozen=True)
class ProductionStart:
    id: str
    content_item_id: str
    channel_id: str
    content_type: ContentType
    started_at: datetime

    def __post_init__(self) -> None:
        for name in ("id", "content_item_id", "channel_id"):
            if not getattr(self, name).strip():
                raise ValueError(f"{name} must not be empty")
        if not isinstance(self.content_type, ContentType):
            raise TypeError("content_type must be a ContentType")
        if not isinstance(
            self.started_at, datetime
        ) or self.started_at.utcoffset() != timedelta(0):
            raise ValueError("started_at must be timezone-aware UTC")

    @classmethod
    def of(cls, item: ContentItem, started_at: datetime) -> "ProductionStart":
        return cls(
            id=uuid.uuid4().hex,
            content_item_id=item.id,
            channel_id=item.channel_id,
            content_type=item.content_type,
            started_at=started_at,
        )
