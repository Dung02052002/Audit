"""Paid AI API budget service (BUDGET-01), context C13.

``PaidApiBudgetService`` keeps the paid-API policy and the per-request quotes
over the database (``paid_api_policy`` and ``paid_api_quotes``, migration
0029):

- ``get_policy`` reads the policy (``DEFAULT_POLICY``, free only, without a
  row). ``set_policy`` saves a mode for a USER actor only, with an optional
  ``expected_version``; an unchanged mode records nothing, a change records
  ``budget.paid_api_policy_changed``.
- ``open_quote`` stores a pending quote for a priced request (or returns the
  pending quote of the same request hash and price), ``record_blocked`` a
  blocked one with its reason. Stale open quotes of the hash are expired
  first.
- ``approve`` and ``reject`` are user decisions. An approval needs the paid
  mode, a pending and unexpired quote, and the caller's echo of its request
  hash, estimated amount and currency (``budget.quote_mismatch`` otherwise).
- ``consume`` uses the approved quote of a request exactly once, in paid mode
  only and only while provider, model, unit price, estimate and currency are
  still those of the current pricing; otherwise the quote expires and
  ``None`` is returned.

Audit metadata holds ids, provider, model, hash and amounts only: never a key,
a prompt or a script text. Nothing here calls a provider.
"""

from __future__ import annotations

import sqlite3
import uuid
from collections.abc import Callable, Sequence
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Final

from ai_youtube_agent.content.cost_ledger import quantize_cost_amount
from ai_youtube_agent.content.paid_api_budget import (
    DEFAULT_POLICY,
    PaidApiMode,
    PaidApiPolicy,
    PaidBudgetErrorCode,
    PaidQuoteError,
    PaidQuoteStatus,
    PaidRequestContext,
    PaidRequestQuote,
    PaidRequestSpec,
)
from ai_youtube_agent.core.audit import (
    Actor,
    ActorKind,
    AuditLog,
    AuditResult,
    EntityRef,
)
from ai_youtube_agent.core.db.database import ConcurrencyError, Database
from ai_youtube_agent.core.db.repositories.base import dt
from ai_youtube_agent.core.db.repositories.paid_api_budget import (
    PaidApiPolicyRepository,
    PaidApiQuoteRepository,
)
from ai_youtube_agent.providers.provider_cost import ProviderPricing

Clock = Callable[[], datetime]

POLICY_CHANGED: Final = "budget.paid_api_policy_changed"
QUOTE_CREATED: Final = "budget.paid_quote_created"
REQUEST_BLOCKED: Final = "budget.paid_request_blocked"
QUOTE_APPROVED: Final = "budget.paid_quote_approved"
QUOTE_REJECTED: Final = "budget.paid_quote_rejected"
QUOTE_CONSUMED: Final = "budget.paid_quote_consumed"
QUOTE_EXPIRED: Final = "budget.paid_quote_expired"
POLICY_ENTITY: Final = EntityRef("paid_api_policy", "1")
GUARD_ACTOR: Final = Actor(ActorKind.SYSTEM, "paid_request_guard")
MAX_LIST: Final = 200


def estimate(pricing: ProviderPricing, unit_count: Decimal) -> Decimal:
    """The estimated amount of ``unit_count`` units, half-up to 6 places."""
    return quantize_cost_amount(pricing.unit_price * unit_count)


class PaidApiBudgetService:
    def __init__(
        self,
        database: Database,
        audit: AuditLog,
        *,
        quote_ttl: timedelta,
        clock: Clock | None = None,
    ) -> None:
        if quote_ttl <= timedelta(0):
            raise ValueError("quote_ttl must be positive")
        self._database = database
        self._audit = audit
        self._ttl = quote_ttl
        self._clock = clock

    def _now(self) -> datetime:
        return self._clock() if self._clock else datetime.now(UTC)

    # Policy

    def get_policy(self) -> PaidApiPolicy:
        with self._database.transaction() as connection:
            return PaidApiPolicyRepository(connection).get() or DEFAULT_POLICY

    def set_policy(
        self,
        mode: PaidApiMode,
        *,
        actor: Actor,
        expected_version: int | None = None,
    ) -> PaidApiPolicy:
        """Save ``mode``; only a user may change the budget."""
        _require_user(actor)
        mode = PaidApiMode(mode)
        with self._database.transaction() as connection:
            policies = PaidApiPolicyRepository(connection)
            current = policies.get() or DEFAULT_POLICY
            if expected_version is not None and expected_version != current.version:
                raise PaidQuoteError(
                    f"policy is at version {current.version}, not {expected_version}",
                    code=PaidBudgetErrorCode.POLICY_VERSION_CONFLICT,
                )
            if current.mode is mode:
                return current
            saved = PaidApiPolicy(
                mode=mode,
                version=current.version + 1,
                updated_by=actor,
                updated_at=self._now(),
            )
            try:
                policies.upsert(saved, expected_version=current.version)
            except ConcurrencyError as error:
                raise PaidQuoteError(
                    str(error), code=PaidBudgetErrorCode.POLICY_VERSION_CONFLICT
                ) from error
        self._audit.record(
            POLICY_CHANGED,
            actor,
            POLICY_ENTITY,
            AuditResult.SUCCESS,
            {"from": current.mode.value, "to": mode.value, "version": saved.version},
        )
        return saved

    # Quotes

    def open_quote(
        self,
        spec: PaidRequestSpec,
        pricing: ProviderPricing,
        context: PaidRequestContext,
        *,
        unknowns: Sequence[str] = (),
    ) -> PaidRequestQuote:
        """The pending quote of ``spec`` at ``pricing``, stored when new.

        When another call stores the open quote of the same hash first (the
        one-open-quote-per-hash index refuses this one), that quote is used.
        """
        now = self._now()
        try:
            return self._open_quote(spec, pricing, context, unknowns, now)
        except sqlite3.IntegrityError:
            with self._database.transaction() as connection:
                existing = PaidApiQuoteRepository(connection).find_open(
                    spec.request_hash
                )
            if (
                existing is None
                or existing.is_expired(now)
                or not _matches(existing, spec, pricing)
            ):
                raise
            return existing

    def _open_quote(
        self,
        spec: PaidRequestSpec,
        pricing: ProviderPricing,
        context: PaidRequestContext,
        unknowns: Sequence[str],
        now: datetime,
    ) -> PaidRequestQuote:
        amount = estimate(pricing, spec.unit_count)
        stale: PaidRequestQuote | None = None
        with self._database.transaction() as connection:
            quotes = PaidApiQuoteRepository(connection)
            existing = quotes.find_open(spec.request_hash)
            if existing is not None:
                if not existing.is_expired(now) and _matches(existing, spec, pricing):
                    return existing
                quotes.transition(existing.id, existing.status, PaidQuoteStatus.EXPIRED)
                stale = existing
            quote = PaidRequestQuote(
                id="pq_" + uuid.uuid4().hex,
                status=PaidQuoteStatus.PENDING,
                task_type=spec.task_type,
                provider=spec.provider,
                model=spec.model,
                request_hash=spec.request_hash,
                unit=spec.unit,
                unit_count=spec.unit_count,
                duration_ms=spec.duration_ms,
                scene_id=spec.scene_id,
                unit_price=pricing.unit_price,
                estimated_amount=amount,
                currency=pricing.currency,
                price_source=f"{pricing.source} ({pricing.verified_at.isoformat()})",
                conditions=tuple(pricing.conditions),
                unknowns=tuple(unknowns),
                blocked_reason=None,
                content_item_id=context.content_item_id,
                job_id=context.job_id,
                created_at=now,
                expires_at=now + self._ttl,
            )
            quotes.add(quote)
        if stale is not None:
            self._record_expired(stale)
        self._audit.record(
            QUOTE_CREATED,
            GUARD_ACTOR,
            EntityRef("paid_api_quote", quote.id),
            AuditResult.SUCCESS,
            _quote_metadata(quote),
        )
        return quote

    def record_blocked(
        self,
        spec: PaidRequestSpec,
        reason_code: PaidBudgetErrorCode,
        context: PaidRequestContext,
        *,
        unknowns: Sequence[str] = (),
    ) -> PaidRequestQuote:
        """Store a request that may not run, with its reason; never approvable."""
        now = self._now()
        quote = PaidRequestQuote(
            id="pq_" + uuid.uuid4().hex,
            status=PaidQuoteStatus.BLOCKED,
            task_type=spec.task_type,
            provider=spec.provider,
            model=spec.model,
            request_hash=spec.request_hash,
            unit=spec.unit,
            unit_count=spec.unit_count,
            duration_ms=spec.duration_ms,
            scene_id=spec.scene_id,
            unit_price=None,
            estimated_amount=None,
            currency=None,
            price_source=None,
            conditions=(),
            unknowns=tuple(unknowns),
            blocked_reason=reason_code.value,
            content_item_id=context.content_item_id,
            job_id=context.job_id,
            created_at=now,
            expires_at=now + self._ttl,
        )
        with self._database.transaction() as connection:
            PaidApiQuoteRepository(connection).add(quote)
        self._audit.record(
            REQUEST_BLOCKED,
            GUARD_ACTOR,
            EntityRef("paid_api_quote", quote.id),
            AuditResult.DENIED,
            {**_quote_metadata(quote), "reason": reason_code.value},
        )
        return quote

    def list_quotes(
        self,
        *,
        status: PaidQuoteStatus | None = None,
        content_item_id: str | None = None,
        job_id: str | None = None,
        limit: int = 50,
    ) -> list[PaidRequestQuote]:
        if isinstance(limit, bool) or not 1 <= limit <= MAX_LIST:
            raise ValueError(f"limit must be from 1 to {MAX_LIST}")
        with self._database.transaction() as connection:
            return PaidApiQuoteRepository(connection).list(
                status=status,
                content_item_id=content_item_id,
                job_id=job_id,
                limit=limit,
            )

    def get_quote(self, quote_id: str) -> PaidRequestQuote:
        with self._database.transaction() as connection:
            quote = PaidApiQuoteRepository(connection).get(quote_id)
        if quote is None:
            raise PaidQuoteError(
                f"quote {quote_id} not found", code=PaidBudgetErrorCode.QUOTE_NOT_FOUND
            )
        return quote

    def approve(
        self,
        quote_id: str,
        *,
        actor: Actor,
        request_hash: str,
        estimated_amount: Decimal,
        currency: str,
    ) -> PaidRequestQuote:
        """Approve a pending quote, bound to the hash, price and currency echoed."""
        _require_user(actor)
        now = self._now()
        with self._database.transaction() as connection:
            policy = PaidApiPolicyRepository(connection).get() or DEFAULT_POLICY
            if not policy.allows_paid_requests:
                raise PaidQuoteError(
                    "free_only refuses approvals",
                    code=PaidBudgetErrorCode.PAID_MODE_DISABLED,
                )
            quotes = PaidApiQuoteRepository(connection)
            quote = quotes.get(quote_id)
            if quote is None:
                raise PaidQuoteError(
                    f"quote {quote_id} not found",
                    code=PaidBudgetErrorCode.QUOTE_NOT_FOUND,
                )
            if quote.status is not PaidQuoteStatus.PENDING:
                raise PaidQuoteError(
                    f"quote {quote_id} is {quote.status.value}",
                    code=PaidBudgetErrorCode.QUOTE_NOT_PENDING,
                )
            expired = quote.is_expired(now)
            if expired:
                # Committed before the refusal below, so the quote stays expired.
                quotes.transition(
                    quote.id, PaidQuoteStatus.PENDING, PaidQuoteStatus.EXPIRED
                )
            elif not _echo_matches(quote, request_hash, estimated_amount, currency):
                raise PaidQuoteError(
                    f"approval of {quote_id} does not match the quote",
                    code=PaidBudgetErrorCode.QUOTE_MISMATCH,
                )
            elif not quotes.transition(
                quote.id,
                PaidQuoteStatus.PENDING,
                PaidQuoteStatus.APPROVED,
                decided_by_kind=actor.kind.value,
                decided_by_id=actor.id,
                decided_at=dt(now),
            ):
                raise PaidQuoteError(
                    f"quote {quote_id} changed",
                    code=PaidBudgetErrorCode.QUOTE_NOT_PENDING,
                )
        if expired:
            self._record_expired(quote)
            raise PaidQuoteError(
                f"quote {quote_id} expired", code=PaidBudgetErrorCode.QUOTE_EXPIRED
            )
        approved = replace(
            quote, status=PaidQuoteStatus.APPROVED, decided_by=actor, decided_at=now
        )
        self._audit.record(
            QUOTE_APPROVED,
            actor,
            EntityRef("paid_api_quote", approved.id),
            AuditResult.SUCCESS,
            _quote_metadata(approved),
        )
        return approved

    def reject(self, quote_id: str, *, actor: Actor) -> PaidRequestQuote:
        """Reject a pending quote, or withdraw an approval not used yet."""
        _require_user(actor)
        now = self._now()
        with self._database.transaction() as connection:
            quotes = PaidApiQuoteRepository(connection)
            quote = quotes.get(quote_id)
            if quote is None:
                raise PaidQuoteError(
                    f"quote {quote_id} not found",
                    code=PaidBudgetErrorCode.QUOTE_NOT_FOUND,
                )
            if quote.status not in (PaidQuoteStatus.PENDING, PaidQuoteStatus.APPROVED):
                raise PaidQuoteError(
                    f"quote {quote_id} is {quote.status.value}",
                    code=PaidBudgetErrorCode.QUOTE_NOT_PENDING,
                )
            if not quotes.transition(
                quote.id,
                quote.status,
                PaidQuoteStatus.REJECTED,
                decided_by_kind=actor.kind.value,
                decided_by_id=actor.id,
                decided_at=dt(now),
            ):
                raise PaidQuoteError(
                    f"quote {quote_id} changed",
                    code=PaidBudgetErrorCode.QUOTE_NOT_PENDING,
                )
        rejected = replace(
            quote, status=PaidQuoteStatus.REJECTED, decided_by=actor, decided_at=now
        )
        self._audit.record(
            QUOTE_REJECTED,
            actor,
            EntityRef("paid_api_quote", rejected.id),
            AuditResult.SUCCESS,
            _quote_metadata(rejected),
        )
        return rejected

    def approved_quote(
        self, spec: PaidRequestSpec, pricing: ProviderPricing
    ) -> PaidRequestQuote | None:
        """The approved quote ``consume`` would use for ``spec``, without using it.

        An approved quote that expired or no longer matches ``pricing`` is
        expired here.
        """
        if not self.get_policy().allows_paid_requests:
            return None
        now = self._now()
        with self._database.transaction() as connection:
            quotes = PaidApiQuoteRepository(connection)
            quote = quotes.find_open(spec.request_hash)
            if quote is None or quote.status is not PaidQuoteStatus.APPROVED:
                return None
            if quote.is_expired(now) or not _matches(quote, spec, pricing):
                quotes.transition(
                    quote.id, PaidQuoteStatus.APPROVED, PaidQuoteStatus.EXPIRED
                )
                stale = quote
            else:
                return quote
        self._record_expired(stale)
        return None

    def consume(
        self, spec: PaidRequestSpec, pricing: ProviderPricing
    ) -> PaidRequestQuote | None:
        """Use the approved quote of ``spec`` once; ``None`` when there is none.

        Exactly the quote checked against ``spec`` and ``pricing`` is consumed
        (by its id), never another quote of the same hash.
        """
        approved = self.approved_quote(spec, pricing)
        if approved is None:
            return None
        now = self._now()
        with self._database.transaction() as connection:
            policy = PaidApiPolicyRepository(connection).get() or DEFAULT_POLICY
            if not policy.allows_paid_requests:
                return None
            consumed = PaidApiQuoteRepository(connection).consume(approved.id, now)
        if consumed is None:
            return None
        self._audit.record(
            QUOTE_CONSUMED,
            GUARD_ACTOR,
            EntityRef("paid_api_quote", consumed.id),
            AuditResult.SUCCESS,
            _quote_metadata(consumed),
        )
        return consumed

    def _record_expired(self, quote: PaidRequestQuote) -> None:
        self._audit.record(
            QUOTE_EXPIRED,
            GUARD_ACTOR,
            EntityRef("paid_api_quote", quote.id),
            AuditResult.SUCCESS,
            _quote_metadata(quote),
        )


def _require_user(actor: Actor) -> None:
    if actor.kind is not ActorKind.USER:
        raise PaidQuoteError(
            f"{actor.kind.value} actor {actor.id} may not change the budget",
            code=PaidBudgetErrorCode.POLICY_ACTOR_NOT_USER,
        )


def _matches(
    quote: PaidRequestQuote, spec: PaidRequestSpec, pricing: ProviderPricing
) -> bool:
    """Whether ``quote`` still prices ``spec`` exactly as ``pricing`` does."""
    return (
        quote.request_hash == spec.request_hash
        and quote.provider == spec.provider
        and quote.model == spec.model
        and quote.unit_price == pricing.unit_price
        and quote.currency == pricing.currency
        and quote.estimated_amount == estimate(pricing, spec.unit_count)
    )


def _echo_matches(
    quote: PaidRequestQuote,
    request_hash: str,
    estimated_amount: Decimal,
    currency: str,
) -> bool:
    return (
        quote.price_known
        and request_hash == quote.request_hash
        and isinstance(estimated_amount, Decimal)
        and estimated_amount.is_finite()
        and estimated_amount == quote.estimated_amount
        and currency == quote.currency
    )


def _quote_metadata(quote: PaidRequestQuote) -> dict[str, str | int | None]:
    return {
        "quote_id": quote.id,
        "status": quote.status.value,
        "task_type": quote.task_type.value,
        "provider": quote.provider,
        "model": quote.model,
        "request_hash": quote.request_hash,
        "unit": quote.unit,
        "unit_count": format(quote.unit_count, "f"),
        "estimated_amount": (
            format(quote.estimated_amount, "f")
            if quote.estimated_amount is not None
            else None
        ),
        "currency": quote.currency,
        "content_item_id": quote.content_item_id,
        "job_id": quote.job_id,
    }


__all__ = ["PaidApiBudgetService", "estimate"]
