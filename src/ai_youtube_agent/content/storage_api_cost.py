"""Storage and external API cost recorder (Prompt Pack v8, prompt #178), context C14 Economics.

``StorageApiCostRecorder`` computes and records configured ``CostCategory.STORAGE``
and ``CostCategory.API`` contribution costs into ``CostLedger`` (#174):

- ``StoragePricingPolicy`` defines ``price_per_gb`` (per GiB = ``1_073_741_824``
  bytes) in an ISO 4217 ``currency``, rounded half-up to 6 decimal places.
- ``ApiPricingPolicy`` defines ``price_per_1k_requests`` in an ISO 4217 ``currency``
  plus optional per-operation overrides (``operation_rates``).
- ``record_artifact_storage`` records ``CostCategory.STORAGE`` for a stored
  ``Artifact`` (#016) with ``ref = f"storage:{artifact.id}"``.
- ``record_storage_bytes`` records ``CostCategory.STORAGE`` for channel or item
  storage usage.
- ``record_api_calls`` records ``CostCategory.API`` for external API calls.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum
from http import HTTPStatus
from typing import Any, Final

from ai_youtube_agent.content.cost import CostCategory
from ai_youtube_agent.content.cost_ledger import (
    CostLedger,
    CostLedgerEntry,
    quantize_cost_amount,
)
from ai_youtube_agent.content.strategy import CURRENCY_PATTERN
from ai_youtube_agent.core.artifact import Artifact
from ai_youtube_agent.core.audit import Actor, ActorKind, MetadataValue
from ai_youtube_agent.core.errors import DomainError

BYTES_PER_GIB: Final[Decimal] = Decimal(1_073_741_824)
REQUESTS_PER_UNIT: Final[Decimal] = Decimal(1000)
STORAGE_API_COST_ACTOR: Final[Actor] = Actor(
    ActorKind.SYSTEM, "storage_api_cost"
)


class StorageApiCostErrorCode(StrEnum):
    INVALID_STORAGE_BYTES = "domain.storage_cost_invalid_bytes"
    INVALID_API_REQUESTS = "domain.api_cost_invalid_requests"
    INVALID_OPERATION = "domain.api_cost_invalid_operation"


USER_MESSAGES: Final[dict[StorageApiCostErrorCode, str]] = {
    StorageApiCostErrorCode.INVALID_STORAGE_BYTES: (
        "Storage size must be a positive number of bytes."
    ),
    StorageApiCostErrorCode.INVALID_API_REQUESTS: (
        "API request count must be a positive whole number."
    ),
    StorageApiCostErrorCode.INVALID_OPERATION: (
        "API operation name must not be blank when provided."
    ),
}

_STATUS: Final[dict[StorageApiCostErrorCode, HTTPStatus]] = {
    StorageApiCostErrorCode.INVALID_STORAGE_BYTES: HTTPStatus.UNPROCESSABLE_ENTITY,
    StorageApiCostErrorCode.INVALID_API_REQUESTS: HTTPStatus.UNPROCESSABLE_ENTITY,
    StorageApiCostErrorCode.INVALID_OPERATION: HTTPStatus.UNPROCESSABLE_ENTITY,
}


class StorageApiCostError(DomainError):
    default_code = StorageApiCostErrorCode.INVALID_STORAGE_BYTES.value
    default_user_message = USER_MESSAGES[
        StorageApiCostErrorCode.INVALID_STORAGE_BYTES
    ]
    default_http_status = HTTPStatus.UNPROCESSABLE_ENTITY

    def __init__(self, code: StorageApiCostErrorCode, detail: str) -> None:
        if not isinstance(code, StorageApiCostErrorCode):
            raise TypeError("code must be a StorageApiCostErrorCode")
        self.cost_code = code
        super().__init__(detail, code=code.value, user_message=USER_MESSAGES[code])
        self.default_http_status = _STATUS[code]


def _check_non_negative_decimal(name: str, value: object) -> None:
    if (
        isinstance(value, bool)
        or not isinstance(value, Decimal)
        or not value.is_finite()
        or value < 0
    ):
        raise ValueError(f"{name} must be a finite Decimal of 0 or more")


@dataclass(frozen=True)
class StoragePricingPolicy:
    price_per_gb: Decimal = Decimal("0")
    currency: str = "USD"

    def __post_init__(self) -> None:
        _check_non_negative_decimal("price_per_gb", self.price_per_gb)
        if not isinstance(self.currency, str) or not CURRENCY_PATTERN.match(
            self.currency
        ):
            raise ValueError(
                f"currency {self.currency!r} must be an ISO 4217 code such as 'USD'"
            )

    def compute_amount(self, size_bytes: int) -> Decimal:
        if isinstance(size_bytes, bool) or not isinstance(size_bytes, int) or size_bytes < 1:
            raise StorageApiCostError(
                StorageApiCostErrorCode.INVALID_STORAGE_BYTES,
                "size_bytes must be a positive integer",
            )
        raw = (Decimal(size_bytes) / BYTES_PER_GIB) * self.price_per_gb
        return quantize_cost_amount(raw)

    def as_dict(self) -> dict[str, Any]:
        return {
            "price_per_gb": str(self.price_per_gb),
            "currency": self.currency,
        }


@dataclass(frozen=True)
class ApiPricingPolicy:
    price_per_1k_requests: Decimal = Decimal("0")
    currency: str = "USD"
    operation_rates: Mapping[str, Decimal] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _check_non_negative_decimal(
            "price_per_1k_requests", self.price_per_1k_requests
        )
        if not isinstance(self.currency, str) or not CURRENCY_PATTERN.match(
            self.currency
        ):
            raise ValueError(
                f"currency {self.currency!r} must be an ISO 4217 code such as 'USD'"
            )
        cleaned: dict[str, Decimal] = {}
        for op, rate in dict(self.operation_rates).items():
            if not isinstance(op, str) or not op.strip():
                raise ValueError("operation_rates keys must be non-empty strings")
            _check_non_negative_decimal(f"operation_rates[{op!r}]", rate)
            cleaned[op.strip()] = rate
        object.__setattr__(self, "operation_rates", cleaned)

    def rate_for(self, operation: str | None = None) -> Decimal:
        if operation is None:
            return self.price_per_1k_requests
        if not isinstance(operation, str) or not operation.strip():
            raise StorageApiCostError(
                StorageApiCostErrorCode.INVALID_OPERATION,
                "operation must be a non-empty string when given",
            )
        return self.operation_rates.get(
            operation.strip(), self.price_per_1k_requests
        )

    def compute_amount(
        self, requests: int, *, operation: str | None = None
    ) -> Decimal:
        if isinstance(requests, bool) or not isinstance(requests, int) or requests < 1:
            raise StorageApiCostError(
                StorageApiCostErrorCode.INVALID_API_REQUESTS,
                "requests must be a positive integer",
            )
        rate = self.rate_for(operation)
        raw = (Decimal(requests) / REQUESTS_PER_UNIT) * rate
        return quantize_cost_amount(raw)

    def as_dict(self) -> dict[str, Any]:
        return {
            "price_per_1k_requests": str(self.price_per_1k_requests),
            "currency": self.currency,
            "operation_rates": {
                k: str(v) for k, v in self.operation_rates.items()
            },
        }


class StorageApiCostRecorder:
    """Records configured ``CostCategory.STORAGE`` and ``CostCategory.API`` costs into ``CostLedger``."""

    def __init__(
        self,
        ledger: CostLedger,
        *,
        storage_policy: StoragePricingPolicy | None = None,
        api_policy: ApiPricingPolicy | None = None,
    ) -> None:
        if not isinstance(ledger, CostLedger):
            raise TypeError("ledger must be a CostLedger")
        if storage_policy is not None and not isinstance(
            storage_policy, StoragePricingPolicy
        ):
            raise TypeError("storage_policy must be a StoragePricingPolicy")
        if api_policy is not None and not isinstance(api_policy, ApiPricingPolicy):
            raise TypeError("api_policy must be an ApiPricingPolicy")
        self._ledger = ledger
        self._storage_policy = storage_policy or StoragePricingPolicy()
        self._api_policy = api_policy or ApiPricingPolicy()

    @property
    def storage_policy(self) -> StoragePricingPolicy:
        return self._storage_policy

    @property
    def api_policy(self) -> ApiPricingPolicy:
        return self._api_policy

    def record_artifact_storage(
        self,
        channel_id: str,
        artifact: Artifact,
        *,
        provider: str = "local_storage",
        actor: Actor = STORAGE_API_COST_ACTOR,
    ) -> CostLedgerEntry:
        """Record the storage cost of an ``Artifact`` idempotently by ``storage:{artifact.id}``."""
        if not isinstance(artifact, Artifact):
            raise TypeError("artifact must be an Artifact")
        return self.record_storage_bytes(
            channel_id,
            provider=provider,
            size_bytes=artifact.size_bytes,
            content_item_id=artifact.content_item_id,
            ref=f"storage:{artifact.id}",
            actor=actor,
        )

    def record_storage_bytes(
        self,
        channel_id: str,
        *,
        provider: str,
        size_bytes: int,
        content_item_id: str | None = None,
        ref: str | None = None,
        actor: Actor = STORAGE_API_COST_ACTOR,
    ) -> CostLedgerEntry:
        """Compute and record a ``CostCategory.STORAGE`` entry for ``size_bytes``."""
        amount = self._storage_policy.compute_amount(size_bytes)
        return self._ledger.record(
            channel_id,
            CostCategory.STORAGE,
            provider=provider,
            amount=amount,
            currency=self._storage_policy.currency,
            content_item_id=content_item_id,
            ref=ref,
            actor=actor,
            extra_metadata={"size_bytes": size_bytes},
        )

    def record_api_calls(
        self,
        channel_id: str,
        *,
        provider: str,
        requests: int = 1,
        operation: str | None = None,
        content_item_id: str | None = None,
        ref: str | None = None,
        actor: Actor = STORAGE_API_COST_ACTOR,
    ) -> CostLedgerEntry:
        """Compute and record a ``CostCategory.API`` entry for ``requests`` external API calls."""
        amount = self._api_policy.compute_amount(requests, operation=operation)
        meta: dict[str, MetadataValue] = {"requests": requests}
        if operation is not None:
            meta["operation"] = operation.strip()
        return self._ledger.record(
            channel_id,
            CostCategory.API,
            provider=provider,
            amount=amount,
            currency=self._api_policy.currency,
            content_item_id=content_item_id,
            ref=ref,
            actor=actor,
            extra_metadata=meta,
        )
