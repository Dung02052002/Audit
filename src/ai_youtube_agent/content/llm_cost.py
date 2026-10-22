"""LLM usage cost recorder (Prompt Pack v8, prompt #175), context C14 Economics.

``LLMCostRecorder`` computes and records model/provider usage costs of category
``CostCategory.LLM`` into ``CostLedger`` (#174) from ``GeneratedText`` (#065) or
explicit token usage counts:

- ``LLMModelRate`` defines ``input_price_per_1k`` and ``output_price_per_1k`` as
  non-negative finite ``Decimal`` amounts in an ISO 4217 ``currency``.
- ``LLMPricingCatalog`` resolves the rate for ``(provider, model)`` or falls back
  to a default rate when configured, raising ``LLMCostError(UNKNOWN_MODEL_RATE)``
  otherwise.
- Token counts are never guessed: if ``GeneratedText.input_tokens`` or
  ``GeneratedText.output_tokens`` is ``None``, ``LLMCostError(MISSING_TOKEN_USAGE)``
  is raised.
- Amount is
  ``(input_tokens * input_price_per_1k + output_tokens * output_price_per_1k) / 1000``
  rounded half-up to 6 decimal places via ``quantize_cost_amount``.
- Delegates persistence, channel/item validation, budget currency guard, and
  ``(CostCategory.LLM, ref)`` idempotency to ``CostLedger``.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from http import HTTPStatus
from typing import Any, Final

from ai_youtube_agent.content.analytics import SOURCE_PATTERN as PROVIDER_PATTERN
from ai_youtube_agent.content.cost import CostCategory
from ai_youtube_agent.content.cost_ledger import (
    CostLedger,
    CostLedgerEntry,
    quantize_cost_amount,
)
from ai_youtube_agent.content.strategy import CURRENCY_PATTERN
from ai_youtube_agent.core.audit import Actor, ActorKind
from ai_youtube_agent.core.errors import DomainError
from ai_youtube_agent.providers.text_generation import GeneratedText

TOKENS_PER_UNIT: Final[Decimal] = Decimal(1000)
LLM_COST_ACTOR: Final[Actor] = Actor(ActorKind.SYSTEM, "llm_cost")


class LLMCostErrorCode(StrEnum):
    MISSING_TOKEN_USAGE = "domain.llm_cost_missing_token_usage"
    UNKNOWN_MODEL_RATE = "domain.llm_cost_unknown_model_rate"
    INVALID_TOKEN_COUNT = "domain.llm_cost_invalid_token_count"


USER_MESSAGES: Final[dict[LLMCostErrorCode, str]] = {
    LLMCostErrorCode.MISSING_TOKEN_USAGE: (
        "The text generation result did not report input and output token usage."
    ),
    LLMCostErrorCode.UNKNOWN_MODEL_RATE: (
        "No pricing rate is configured for the requested LLM provider and model."
    ),
    LLMCostErrorCode.INVALID_TOKEN_COUNT: (
        "LLM token counts must be non-negative whole numbers."
    ),
}

_STATUS: Final[dict[LLMCostErrorCode, HTTPStatus]] = {
    LLMCostErrorCode.MISSING_TOKEN_USAGE: HTTPStatus.UNPROCESSABLE_ENTITY,
    LLMCostErrorCode.UNKNOWN_MODEL_RATE: HTTPStatus.UNPROCESSABLE_ENTITY,
    LLMCostErrorCode.INVALID_TOKEN_COUNT: HTTPStatus.UNPROCESSABLE_ENTITY,
}


class LLMCostError(DomainError):
    default_code = LLMCostErrorCode.MISSING_TOKEN_USAGE.value
    default_user_message = USER_MESSAGES[LLMCostErrorCode.MISSING_TOKEN_USAGE]
    default_http_status = HTTPStatus.UNPROCESSABLE_ENTITY

    def __init__(self, code: LLMCostErrorCode, detail: str) -> None:
        if not isinstance(code, LLMCostErrorCode):
            raise TypeError("code must be an LLMCostErrorCode")
        self.llm_cost_code = code
        super().__init__(detail, code=code.value, user_message=USER_MESSAGES[code])
        self.default_http_status = _STATUS[code]


@dataclass(frozen=True)
class LLMModelRate:
    input_price_per_1k: Decimal
    output_price_per_1k: Decimal
    currency: str = "USD"

    def __post_init__(self) -> None:
        for name in ("input_price_per_1k", "output_price_per_1k"):
            val = getattr(self, name)
            if (
                isinstance(val, bool)
                or not isinstance(val, Decimal)
                or not val.is_finite()
                or val < 0
            ):
                raise ValueError(f"{name} must be a finite Decimal of 0 or more")
        if not isinstance(self.currency, str) or not CURRENCY_PATTERN.match(
            self.currency
        ):
            raise ValueError(
                f"currency {self.currency!r} must be an ISO 4217 code such as 'USD'"
            )

    def compute_amount(self, input_tokens: int, output_tokens: int) -> Decimal:
        _check_tokens("input_tokens", input_tokens)
        _check_tokens("output_tokens", output_tokens)
        raw = (
            Decimal(input_tokens) * self.input_price_per_1k
            + Decimal(output_tokens) * self.output_price_per_1k
        ) / TOKENS_PER_UNIT
        return quantize_cost_amount(raw)

    def as_dict(self) -> dict[str, Any]:
        return {
            "input_price_per_1k": str(self.input_price_per_1k),
            "output_price_per_1k": str(self.output_price_per_1k),
            "currency": self.currency,
        }


# The catalog's default when no rate is given: free, in USD.
FREE_USD_RATE = LLMModelRate(
    input_price_per_1k=Decimal("0"),
    output_price_per_1k=Decimal("0"),
    currency="USD",
)


class LLMPricingCatalog:
    """Lookup table of ``LLMModelRate`` by ``(provider, model)``.

    With an optional default.
    """

    def __init__(
        self,
        rates: Mapping[tuple[str, str], LLMModelRate] | None = None,
        *,
        default_rate: LLMModelRate | None = FREE_USD_RATE,
    ) -> None:
        if default_rate is not None and not isinstance(default_rate, LLMModelRate):
            raise TypeError("default_rate must be an LLMModelRate or None")
        self._rates: dict[tuple[str, str], LLMModelRate] = {}
        self._default = default_rate
        if rates:
            for (provider, model), rate in rates.items():
                self.set_rate(provider, model, rate)

    def set_rate(self, provider: str, model: str, rate: LLMModelRate) -> None:
        if not isinstance(provider, str) or not PROVIDER_PATTERN.match(provider):
            raise ValueError("provider must be a lowercase provider name")
        if not isinstance(model, str) or not model.strip():
            raise ValueError("model must not be empty")
        if not isinstance(rate, LLMModelRate):
            raise TypeError("rate must be an LLMModelRate")
        self._rates[(provider, model.strip())] = rate

    def resolve(self, provider: str, model: str) -> LLMModelRate:
        rate = self._rates.get((provider, model.strip()))
        if rate is not None:
            return rate
        if self._default is not None:
            return self._default
        raise LLMCostError(
            LLMCostErrorCode.UNKNOWN_MODEL_RATE,
            f"no LLM rate configured for provider={provider!r}, model={model!r}",
        )


class LLMCostRecorder:
    """Records ``CostCategory.LLM`` usage costs into ``CostLedger``."""

    def __init__(
        self,
        ledger: CostLedger,
        *,
        catalog: LLMPricingCatalog | None = None,
    ) -> None:
        if not isinstance(ledger, CostLedger):
            raise TypeError("ledger must be a CostLedger")
        if catalog is not None and not isinstance(catalog, LLMPricingCatalog):
            raise TypeError("catalog must be an LLMPricingCatalog")
        self._ledger = ledger
        self._catalog = catalog or LLMPricingCatalog()

    @property
    def catalog(self) -> LLMPricingCatalog:
        return self._catalog

    def record_generated(
        self,
        channel_id: str,
        generated: GeneratedText,
        *,
        content_item_id: str | None = None,
        ref: str | None = None,
        actor: Actor = LLM_COST_ACTOR,
    ) -> CostLedgerEntry:
        """Record the LLM cost of a ``GeneratedText`` response."""
        if not isinstance(generated, GeneratedText):
            raise TypeError("generated must be a GeneratedText")
        if generated.input_tokens is None or generated.output_tokens is None:
            raise LLMCostError(
                LLMCostErrorCode.MISSING_TOKEN_USAGE,
                f"GeneratedText from {generated.provider}/{generated.model} "
                "is missing token counts",
            )
        return self.record_usage(
            channel_id,
            provider=generated.provider,
            model=generated.model,
            input_tokens=generated.input_tokens,
            output_tokens=generated.output_tokens,
            content_item_id=content_item_id,
            ref=ref,
            actor=actor,
        )

    def record_usage(
        self,
        channel_id: str,
        *,
        provider: str,
        model: str,
        input_tokens: int,
        output_tokens: int,
        content_item_id: str | None = None,
        ref: str | None = None,
        actor: Actor = LLM_COST_ACTOR,
    ) -> CostLedgerEntry:
        """Compute and record LLM usage cost from explicit token counts."""
        _check_tokens("input_tokens", input_tokens)
        _check_tokens("output_tokens", output_tokens)
        rate = self._catalog.resolve(provider, model)
        amount = rate.compute_amount(input_tokens, output_tokens)
        return self._ledger.record(
            channel_id,
            CostCategory.LLM,
            provider=provider,
            amount=amount,
            currency=rate.currency,
            content_item_id=content_item_id,
            ref=ref,
            actor=actor,
            extra_metadata={
                "model": model.strip(),
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
            },
        )


def _check_tokens(name: str, value: object) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise LLMCostError(
            LLMCostErrorCode.INVALID_TOKEN_COUNT,
            f"{name} must be a non-negative integer",
        )
