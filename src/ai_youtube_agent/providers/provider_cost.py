"""Cost classification of AI providers (BUDGET-01).

Every provider the pipeline may call declares what a call costs through
``cost_declaration()`` (the ``DeclaresProviderCost`` protocol):

- ``mock``: deterministic, in memory, never touches the network.
- ``local_free``: runs on this machine with free or open-source software
  (ComfyUI, FFmpeg, the local PNG generator).
- ``free_service``: a free online service without an API key (Edge TTS).
- ``paid``: an API that may charge per request (ElevenLabs, video APIs).
- ``unknown``: the provider does not say. It is treated as paid with an
  unknown price.

``ProviderPricing`` is a verified unit price with its source and the date it
was checked. No price source exists yet, so every paid adapter declares
``pricing=None``: the budget guard then blocks it ("chưa rõ giá") instead of
asking for an approval. ``classify_provider`` fails closed: a provider without
a declaration, or whose declaration fails, is ``unknown``. A free provider
whose explicit fallback is not free takes the fallback's class without a
price, since the guard cannot see which of the two answers a call.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from datetime import date
from decimal import Decimal
from enum import StrEnum
from typing import Any, Final, Protocol, runtime_checkable

from ai_youtube_agent.content.paid_api_budget import PaidTaskType

CURRENCY_PATTERN: Final = re.compile(r"^[A-Z]{3}$")
UNVERIFIED_PRICE: Final = "giá chưa xác minh"
UNKNOWN_QUOTA: Final = "hạn mức tài khoản chưa rõ"
PAID_UNKNOWNS: Final = (UNVERIFIED_PRICE, UNKNOWN_QUOTA)
_CATEGORY_TASKS: Final[dict[str, PaidTaskType]] = {
    "image_generation": PaidTaskType.IMAGE_GENERATION,
    "speech_synthesis": PaidTaskType.SPEECH_SYNTHESIS,
    "video_generation": PaidTaskType.VIDEO_CLIP_GENERATION,
}


class ProviderCostClass(StrEnum):
    MOCK = "mock"
    LOCAL_FREE = "local_free"
    FREE_SERVICE = "free_service"
    PAID = "paid"
    UNKNOWN = "unknown"

    @property
    def is_free(self) -> bool:
        return self in FREE_COST_CLASSES


FREE_COST_CLASSES: Final = frozenset(
    {
        ProviderCostClass.MOCK,
        ProviderCostClass.LOCAL_FREE,
        ProviderCostClass.FREE_SERVICE,
    }
)


@dataclass(frozen=True)
class ProviderPricing:
    """A verified price of one ``unit`` (for example a second or a character)."""

    unit: str
    unit_price: Decimal
    currency: str
    source: str
    verified_at: date
    conditions: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.unit, str) or not self.unit.strip():
            raise ValueError("unit must not be empty")
        if (
            not isinstance(self.unit_price, Decimal)
            or not self.unit_price.is_finite()
            or self.unit_price < 0
        ):
            raise ValueError("unit_price must be a finite Decimal of 0 or more")
        if not isinstance(self.currency, str) or not CURRENCY_PATTERN.match(
            self.currency
        ):
            raise ValueError("currency must be an ISO 4217 code")
        if not isinstance(self.source, str) or not self.source.strip():
            raise ValueError("source must not be empty")
        if not isinstance(self.verified_at, date):
            raise ValueError("verified_at must be a date")


@dataclass(frozen=True)
class ProviderCostDeclaration:
    provider: str
    model: str
    cost_class: ProviderCostClass
    pricing: ProviderPricing | None = None
    basis: str = ""
    unknowns: tuple[str, ...] = ()

    @property
    def is_free(self) -> bool:
        return self.cost_class.is_free

    @property
    def price_known(self) -> bool:
        return self.pricing is not None

    def as_dict(self) -> dict[str, Any]:
        """Names and classes only: never a key or a credentialed endpoint."""
        return {
            "provider": self.provider,
            "model": self.model,
            "cost_class": self.cost_class.value,
            "price_known": self.price_known,
            "basis": self.basis,
            "unknowns": list(self.unknowns),
        }


@runtime_checkable
class DeclaresProviderCost(Protocol):
    def cost_declaration(self) -> ProviderCostDeclaration: ...


def provider_name(provider: object) -> str:
    name = getattr(provider, "name", None)
    return name if isinstance(name, str) and name.strip() else type(provider).__name__


def unknown_declaration(provider: object, basis: str) -> ProviderCostDeclaration:
    return ProviderCostDeclaration(
        provider=provider_name(provider)[:80],
        model="unknown",
        cost_class=ProviderCostClass.UNKNOWN,
        basis=basis,
        unknowns=PAID_UNKNOWNS,
    )


def classify_provider(provider: object) -> ProviderCostDeclaration:
    """The cost declaration of ``provider``; ``unknown`` when it has none."""
    if isinstance(provider, DeclaresProviderCost):
        try:
            declaration = provider.cost_declaration()
        except Exception:
            return unknown_declaration(
                provider, "Khai báo chi phí của nhà cung cấp bị lỗi; coi là trả phí."
            )
        if isinstance(declaration, ProviderCostDeclaration):
            return declaration
    return unknown_declaration(
        provider, "Nhà cung cấp không khai báo chi phí; coi là trả phí, chưa rõ giá."
    )


def with_fallback(
    declaration: ProviderCostDeclaration, fallback: object | None
) -> ProviderCostDeclaration:
    """``declaration``, unless an explicit ``fallback`` is not free: then the
    fallback's class without a price (the guard cannot tell who answers)."""
    if fallback is None or not declaration.is_free:
        return declaration
    backup = classify_provider(fallback)
    if backup.is_free:
        return declaration
    return replace(
        declaration,
        cost_class=backup.cost_class,
        pricing=None,
        basis=(
            f"Có nhà cung cấp dự phòng '{backup.provider}' không miễn phí; "
            "coi là trả phí, chưa rõ giá."
        ),
        unknowns=PAID_UNKNOWNS,
    )


def free_declaration(
    provider: str, model: str, cost_class: ProviderCostClass, basis: str
) -> ProviderCostDeclaration:
    if not cost_class.is_free:
        raise ValueError(f"{cost_class.value} is not a free cost class")
    return ProviderCostDeclaration(
        provider=provider, model=model, cost_class=cost_class, basis=basis
    )


def paid_declaration(provider: str, model: str, basis: str) -> ProviderCostDeclaration:
    """A paid provider without a verified price (no price source exists yet)."""
    return ProviderCostDeclaration(
        provider=provider,
        model=model,
        cost_class=ProviderCostClass.PAID,
        pricing=None,
        basis=basis,
        unknowns=PAID_UNKNOWNS,
    )


def task_type_of(provider: object) -> PaidTaskType:
    """The kind of AI call ``provider`` makes (for the stored quote)."""
    declared = getattr(provider, "task_type", None)
    if isinstance(declared, PaidTaskType):
        return declared
    category = getattr(provider, "category", None)
    if category is not None and str(category) in _CATEGORY_TASKS:
        return _CATEGORY_TASKS[str(category)]
    if hasattr(provider, "generate_clip"):
        return PaidTaskType.VIDEO_CLIP_GENERATION
    if hasattr(provider, "synthesize"):
        return PaidTaskType.SPEECH_SYNTHESIS
    if hasattr(provider, "search") or hasattr(provider, "fetch"):
        return PaidTaskType.RESEARCH
    if hasattr(provider, "capabilities_image_gen") or (
        hasattr(provider, "generate") and hasattr(provider, "capabilities")
    ):
        return PaidTaskType.IMAGE_GENERATION
    return PaidTaskType.TEXT_GENERATION


__all__ = [
    "DeclaresProviderCost",
    "FREE_COST_CLASSES",
    "PAID_UNKNOWNS",
    "ProviderCostClass",
    "ProviderCostDeclaration",
    "ProviderPricing",
    "classify_provider",
    "free_declaration",
    "paid_declaration",
    "provider_name",
    "task_type_of",
    "unknown_declaration",
    "with_fallback",
]
