"""Cost entity (Prompt Pack v8, prompt #025), context C14 Economics.

A ``CostRecord`` is one tracked cost:

- ``id``: a stable id for the record.
- ``channel_id``: the ``Channel.id`` that pays for it, so the budget gate
  (#037) and budget guard (#180) can sum costs per channel.
- ``category``: ``CostCategory`` production, api, tts, render, storage or llm.
- ``provider``: the lowercase name of whoever charged it, such as ``mock_tts``.
- ``amount`` and ``currency``: a finite ``Decimal`` of 0 or more in an ISO 4217
  currency.
- ``incurred_at``: when the cost was incurred, UTC. The daily and monthly
  budget buckets use it.
- ``content_item_id``: the optional ``ContentItem.id`` it belongs to. Some
  costs, such as storage, belong to no item.
- ``ref``: the optional opaque id of the job or artifact that caused it (#092).

A record is frozen, and a correction is a new record. The ledger (#174), the
per-category recorders (#175–#178) and the contribution margin (#179) come
later.
"""

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Any

from ai_youtube_agent.content.analytics import SOURCE_PATTERN as PROVIDER_PATTERN
from ai_youtube_agent.content.strategy import CURRENCY_PATTERN

Clock = Callable[[], datetime]


class CostCategory(StrEnum):
    PRODUCTION = "production"
    API = "api"
    TTS = "tts"
    RENDER = "render"
    STORAGE = "storage"
    LLM = "llm"


@dataclass(frozen=True)
class CostRecord:
    id: str
    channel_id: str
    category: CostCategory
    provider: str
    amount: Decimal
    currency: str
    incurred_at: datetime
    content_item_id: str | None = None
    ref: str | None = None

    def __post_init__(self) -> None:
        for name in ("id", "channel_id"):
            if not getattr(self, name).strip():
                raise ValueError(f"{name} must not be empty")
        for name in ("content_item_id", "ref"):
            value = getattr(self, name)
            if value is not None and not value.strip():
                raise ValueError(f"{name} must not be empty when given")
        if not isinstance(self.category, CostCategory):
            raise TypeError("category must be a CostCategory")
        if not PROVIDER_PATTERN.match(self.provider):
            raise ValueError(
                f"provider {self.provider!r} must be a lowercase name such as "
                "'mock_tts'"
            )
        if (
            not isinstance(self.amount, Decimal)
            or not self.amount.is_finite()
            or self.amount < 0
        ):
            raise ValueError("amount must be a finite Decimal of 0 or more")
        if not CURRENCY_PATTERN.match(self.currency):
            raise ValueError(
                f"currency {self.currency!r} must be an ISO 4217 code such as 'USD'"
            )
        if self.incurred_at.utcoffset() != timedelta(0):
            raise ValueError("incurred_at must be timezone-aware UTC")

    @classmethod
    def create(
        cls,
        channel_id: str,
        category: CostCategory,
        *,
        provider: str,
        amount: Decimal,
        currency: str,
        content_item_id: str | None = None,
        ref: str | None = None,
        clock: Clock | None = None,
    ) -> "CostRecord":
        return cls(
            id=uuid.uuid4().hex,
            channel_id=channel_id,
            category=category,
            provider=provider,
            amount=amount,
            currency=currency,
            incurred_at=clock() if clock else datetime.now(UTC),
            content_item_id=content_item_id,
            ref=ref,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "channel_id": self.channel_id,
            "category": self.category.value,
            "provider": self.provider,
            "amount": str(self.amount),
            "currency": self.currency,
            "incurred_at": self.incurred_at.isoformat(),
            "content_item_id": self.content_item_id,
            "ref": self.ref,
        }
