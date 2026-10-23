"""Paid AI API budget policy and per-request quotes (BUDGET-01), context C13.

As the user decided for BUDGET-01:

- ``PaidApiPolicy`` is one row (``paid_api_policy``, migration 0029). Without a
  row the policy is ``DEFAULT_POLICY``: ``free_only`` at version 0. Only a
  USER actor may change it; the AI never changes the budget.
- ``free_only`` never calls a paid API, also not after a free quota runs out
  or a free provider fails. ``paid_with_per_request_approval`` stops before
  every request that may cost money and stores a ``PaidRequestQuote``; the
  request runs only after a user approved exactly that quote. Turning the mode
  on is not an approval to pay. A request whose price is unknown is blocked
  (a quote with status ``blocked`` and the reason), never offered for approval.
- ``PaidRequestSpec`` describes one provider call; ``request_hash`` is the
  SHA-256 of its canonical JSON (provider, model, task type, unit, unit count,
  fingerprint), so the same call on a re-run has the same hash. An approval is
  bound to that hash, the quoted price and the provider/model, expires
  (``Settings.paid_quote_ttl_minutes``) and is consumed by exactly one call.
- ``PaidRequestDecisionError`` (409) stops a run: its subclasses are
  ``PaidRequestAwaitingApprovalError`` (quotes to approve) and
  ``PaidRequestBlockedError`` (no paid call possible). ``PaidQuoteError``
  refuses an approval, a rejection or a policy change.

Money is ``Decimal``, never a float. The budget layer is separate from the
channel ``Budget``, ``BudgetGate`` (#037) and ``BudgetGuard`` (#180).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from http import HTTPStatus
from typing import Any, Final

from ai_youtube_agent.core.audit import Actor
from ai_youtube_agent.core.errors import DomainError

MAX_PROVIDER: Final = 80
MAX_MODEL: Final = 120
MAX_UNIT: Final = 40


class PaidApiMode(StrEnum):
    FREE_ONLY = "free_only"
    PAID_WITH_PER_REQUEST_APPROVAL = "paid_with_per_request_approval"


class PaidRequestStatus(StrEnum):
    FREE = "free"
    AWAITING_APPROVAL = "awaiting_approval"
    PAID_APPROVED = "paid_approved"
    BLOCKED = "blocked"


class PaidQuoteStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    CONSUMED = "consumed"
    REJECTED = "rejected"
    EXPIRED = "expired"
    BLOCKED = "blocked"


OPEN_QUOTE_STATUSES: Final = frozenset(
    {PaidQuoteStatus.PENDING, PaidQuoteStatus.APPROVED}
)


class PaidTaskType(StrEnum):
    VIDEO_CLIP_GENERATION = "video_clip_generation"
    SPEECH_SYNTHESIS = "speech_synthesis"
    IMAGE_GENERATION = "image_generation"
    TEXT_GENERATION = "text_generation"
    RESEARCH = "research"


@dataclass(frozen=True)
class PaidApiPolicy:
    mode: PaidApiMode
    version: int
    updated_by: Actor | None = None
    updated_at: datetime | None = None

    @property
    def is_default(self) -> bool:
        """True while no user has saved a policy (no row, version 0)."""
        return self.version == 0

    @property
    def allows_paid_requests(self) -> bool:
        return self.mode is PaidApiMode.PAID_WITH_PER_REQUEST_APPROVAL


DEFAULT_POLICY: Final = PaidApiPolicy(PaidApiMode.FREE_ONLY, 0)


def _text(name: str, value: object, limit: int) -> None:
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > limit:
        raise ValueError(f"{name} must be 1 to {limit} characters")


@dataclass(frozen=True)
class PaidRequestSpec:
    """One provider call that may cost money, before it is made."""

    provider: str
    model: str
    task_type: PaidTaskType
    unit: str
    unit_count: Decimal
    fingerprint: str
    duration_ms: int | None = None
    scene_id: str | None = None

    def __post_init__(self) -> None:
        _text("provider", self.provider, MAX_PROVIDER)
        _text("model", self.model, MAX_MODEL)
        _text("unit", self.unit, MAX_UNIT)
        if not isinstance(self.task_type, PaidTaskType):
            raise ValueError("task_type must be a PaidTaskType")
        if (
            not isinstance(self.unit_count, Decimal)
            or not self.unit_count.is_finite()
            or self.unit_count <= 0
        ):
            raise ValueError("unit_count must be a finite Decimal above 0")
        if not isinstance(self.fingerprint, str) or not self.fingerprint:
            raise ValueError("fingerprint must not be empty")
        if self.duration_ms is not None and (
            isinstance(self.duration_ms, bool)
            or not isinstance(self.duration_ms, int)
            or self.duration_ms < 1
        ):
            raise ValueError("duration_ms must be a whole number of 1 or more")

    @property
    def request_hash(self) -> str:
        canonical = json.dumps(
            {
                "provider": self.provider,
                "model": self.model,
                "task_type": self.task_type.value,
                "unit": self.unit,
                "unit_count": format(self.unit_count.normalize(), "f"),
                "fingerprint": self.fingerprint,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class PaidRequestContext:
    """The content item and job a provider call is made for, when known."""

    content_item_id: str | None = None
    job_id: str | None = None


@dataclass(frozen=True)
class PaidRequestQuote:
    """A stored quote: one paid request, its estimated price and its decision."""

    id: str
    status: PaidQuoteStatus
    task_type: PaidTaskType
    provider: str
    model: str
    request_hash: str
    unit: str
    unit_count: Decimal
    duration_ms: int | None
    scene_id: str | None
    unit_price: Decimal | None
    estimated_amount: Decimal | None
    currency: str | None
    price_source: str | None
    conditions: tuple[str, ...]
    unknowns: tuple[str, ...]
    blocked_reason: str | None
    content_item_id: str | None
    job_id: str | None
    created_at: datetime
    expires_at: datetime
    decided_by: Actor | None = None
    decided_at: datetime | None = None
    consumed_at: datetime | None = None

    @property
    def price_known(self) -> bool:
        return self.estimated_amount is not None and self.currency is not None

    def is_expired(self, now: datetime) -> bool:
        return now >= self.expires_at

    def as_dict(self) -> dict[str, Any]:
        def money(value: Decimal | None) -> str | None:
            return format(value, "f") if value is not None else None

        return {
            "id": self.id,
            "status": self.status.value,
            "task_type": self.task_type.value,
            "provider": self.provider,
            "model": self.model,
            "request_hash": self.request_hash,
            "unit": self.unit,
            "unit_count": format(self.unit_count, "f"),
            "duration_ms": self.duration_ms,
            "scene_id": self.scene_id,
            "unit_price": money(self.unit_price),
            "estimated_amount": money(self.estimated_amount),
            "currency": self.currency,
            "price_known": self.price_known,
            "price_source": self.price_source,
            "conditions": list(self.conditions),
            "unknowns": list(self.unknowns),
            "blocked_reason": self.blocked_reason,
            "content_item_id": self.content_item_id,
            "job_id": self.job_id,
            "created_at": self.created_at.isoformat(),
            "expires_at": self.expires_at.isoformat(),
            "decided_by_id": self.decided_by.id if self.decided_by else None,
            "decided_at": _iso(self.decided_at),
            "consumed_at": _iso(self.consumed_at),
        }


class PaidBudgetErrorCode(StrEnum):
    AWAITING_APPROVAL = "budget.paid_request_awaiting_approval"
    BLOCKED_FREE_ONLY = "budget.paid_provider_blocked_free_only"
    PRICE_UNKNOWN = "budget.price_unknown"
    PROVIDER_COST_UNKNOWN = "budget.provider_cost_unknown"
    QUOTE_NOT_FOUND = "budget.quote_not_found"
    QUOTE_NOT_PENDING = "budget.quote_not_pending"
    QUOTE_EXPIRED = "budget.quote_expired"
    QUOTE_MISMATCH = "budget.quote_mismatch"
    PAID_MODE_DISABLED = "budget.paid_mode_disabled"
    POLICY_ACTOR_NOT_USER = "budget.policy_actor_not_user"
    POLICY_VERSION_CONFLICT = "budget.policy_version_conflict"


BLOCKING_CODES: Final = frozenset(
    {
        PaidBudgetErrorCode.BLOCKED_FREE_ONLY,
        PaidBudgetErrorCode.PRICE_UNKNOWN,
        PaidBudgetErrorCode.PROVIDER_COST_UNKNOWN,
    }
)

LOCAL_FREE_SUGGESTION: Final = (
    "Hãy dùng công cụ cục bộ/miễn phí: ComfyUI (ảnh, SD1.5 trên máy), "
    "Edge TTS (giọng), chế độ Story video."
)

_DECISION_MESSAGES: Final[dict[PaidBudgetErrorCode, str]] = {
    PaidBudgetErrorCode.AWAITING_APPROVAL: (
        "Yêu cầu tới {provider} có thể tính phí. Hệ thống đã dừng và tạo báo "
        "giá; hãy xem và duyệt chi phí rồi chạy lại. Chưa có khoản phí nào "
        "phát sinh."
    ),
    PaidBudgetErrorCode.BLOCKED_FREE_ONLY: (
        "Chế độ 'Chỉ miễn phí' đang bật nên không gọi {provider} (API trả phí). "
        + LOCAL_FREE_SUGGESTION
    ),
    PaidBudgetErrorCode.PRICE_UNKNOWN: (
        "Chưa rõ giá của {provider} nên không thể tạo báo giá để duyệt. Yêu cầu "
        "bị chặn và không gọi API trả phí. " + LOCAL_FREE_SUGGESTION
    ),
    PaidBudgetErrorCode.PROVIDER_COST_UNKNOWN: (
        "Không xác định được {provider} có tính phí hay không, nên hệ thống coi "
        "là API trả phí chưa rõ giá và chặn yêu cầu. " + LOCAL_FREE_SUGGESTION
    ),
}

QUOTE_MESSAGES: Final[dict[PaidBudgetErrorCode, str]] = {
    PaidBudgetErrorCode.QUOTE_NOT_FOUND: "Không tìm thấy báo giá này.",
    PaidBudgetErrorCode.QUOTE_NOT_PENDING: (
        "Báo giá này không còn chờ duyệt (đã duyệt, đã dùng, bị từ chối, hết hạn "
        "hoặc bị chặn)."
    ),
    PaidBudgetErrorCode.QUOTE_EXPIRED: (
        "Báo giá đã hết hạn. Hãy chạy lại để nhận báo giá mới."
    ),
    PaidBudgetErrorCode.QUOTE_MISMATCH: (
        "Thông tin duyệt không khớp với báo giá (mã yêu cầu, chi phí hoặc tiền "
        "tệ). Hãy tải lại báo giá rồi duyệt lại."
    ),
    PaidBudgetErrorCode.PAID_MODE_DISABLED: (
        "Chế độ 'Chỉ miễn phí' đang bật nên không thể duyệt chi phí. Hãy bật chế "
        "độ cho phép trả phí sau khi duyệt trong Cài đặt."
    ),
    PaidBudgetErrorCode.POLICY_ACTOR_NOT_USER: (
        "Chỉ người dùng mới được thay đổi ngân sách hoặc duyệt chi phí."
    ),
    PaidBudgetErrorCode.POLICY_VERSION_CONFLICT: (
        "Chính sách ngân sách vừa được thay đổi ở nơi khác. Hãy tải lại rồi thử lại."
    ),
}

_QUOTE_HTTP: Final[dict[PaidBudgetErrorCode, HTTPStatus]] = {
    PaidBudgetErrorCode.QUOTE_NOT_FOUND: HTTPStatus.NOT_FOUND,
    PaidBudgetErrorCode.POLICY_ACTOR_NOT_USER: HTTPStatus.FORBIDDEN,
}


class PaidRequestDecisionError(DomainError):
    """A provider call was stopped by the budget policy before it was made."""

    default_code = PaidBudgetErrorCode.AWAITING_APPROVAL.value
    default_user_message = "Yêu cầu bị dừng bởi chính sách ngân sách."
    default_http_status = HTTPStatus.CONFLICT

    def __init__(
        self,
        detail: str = "",
        *,
        code: PaidBudgetErrorCode,
        status: PaidRequestStatus,
        provider: str,
        quote_ids: tuple[str, ...] = (),
    ) -> None:
        super().__init__(
            detail or code.value,
            code=code.value,
            user_message=_DECISION_MESSAGES[code].format(provider=provider),
            http_status=HTTPStatus.CONFLICT,
        )
        self.status = status
        self.reason_code = code
        self.provider = provider
        self.quote_ids = tuple(quote_ids)


class PaidRequestAwaitingApprovalError(PaidRequestDecisionError):
    def __init__(
        self, detail: str = "", *, provider: str, quote_ids: tuple[str, ...]
    ) -> None:
        super().__init__(
            detail,
            code=PaidBudgetErrorCode.AWAITING_APPROVAL,
            status=PaidRequestStatus.AWAITING_APPROVAL,
            provider=provider,
            quote_ids=quote_ids,
        )


class PaidRequestBlockedError(PaidRequestDecisionError):
    def __init__(
        self,
        detail: str = "",
        *,
        code: PaidBudgetErrorCode,
        provider: str,
        quote_ids: tuple[str, ...] = (),
    ) -> None:
        if code not in BLOCKING_CODES:
            raise ValueError(f"{code.value} is not a blocking code")
        super().__init__(
            detail,
            code=code,
            status=PaidRequestStatus.BLOCKED,
            provider=provider,
            quote_ids=quote_ids,
        )


class PaidQuoteError(DomainError):
    """An approval, rejection or policy change was refused."""

    default_code = PaidBudgetErrorCode.QUOTE_NOT_FOUND.value
    default_user_message = QUOTE_MESSAGES[PaidBudgetErrorCode.QUOTE_NOT_FOUND]
    default_http_status = HTTPStatus.CONFLICT

    def __init__(self, detail: str = "", *, code: PaidBudgetErrorCode) -> None:
        super().__init__(
            detail or code.value,
            code=code.value,
            user_message=QUOTE_MESSAGES[code],
            http_status=_QUOTE_HTTP.get(code, HTTPStatus.CONFLICT),
        )
        self.reason_code = code


def is_budget_stop_code(code: str | None) -> bool:
    """Whether ``code`` (an AIJob ``last_error``) is a budget stop."""
    return code is not None and (
        code.startswith(PaidBudgetErrorCode.AWAITING_APPROVAL.value)
        or any(code.startswith(blocking.value) for blocking in BLOCKING_CODES)
    )


def _iso(moment: datetime | None) -> str | None:
    return moment.isoformat() if moment is not None else None


__all__ = [
    "BLOCKING_CODES",
    "DEFAULT_POLICY",
    "LOCAL_FREE_SUGGESTION",
    "OPEN_QUOTE_STATUSES",
    "PaidApiMode",
    "PaidApiPolicy",
    "PaidBudgetErrorCode",
    "PaidQuoteError",
    "PaidQuoteStatus",
    "PaidRequestAwaitingApprovalError",
    "PaidRequestBlockedError",
    "PaidRequestContext",
    "PaidRequestDecisionError",
    "PaidRequestQuote",
    "PaidRequestSpec",
    "PaidRequestStatus",
    "PaidTaskType",
    "is_budget_stop_code",
]
