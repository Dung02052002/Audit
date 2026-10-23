"""Tests for the BUDGET-01 paid-API policy and quotes (PaidApiBudgetService)."""

from __future__ import annotations

import threading
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from ai_youtube_agent.content.paid_api_budget import (
    DEFAULT_POLICY,
    PaidApiMode,
    PaidBudgetErrorCode,
    PaidQuoteError,
    PaidQuoteStatus,
    PaidRequestContext,
    PaidRequestSpec,
    PaidTaskType,
    is_budget_stop_code,
)
from ai_youtube_agent.content.paid_api_budget_service import PaidApiBudgetService
from ai_youtube_agent.core.audit import Actor, ActorKind, AuditLog, InMemoryAuditSink
from ai_youtube_agent.core.db.database import Database
from ai_youtube_agent.core.db.repositories.paid_api_budget import (
    PaidApiQuoteRepository,
)
from ai_youtube_agent.providers.provider_cost import ProviderPricing

T0 = datetime(2026, 10, 23, 8, 0, tzinfo=UTC)
USER = Actor(ActorKind.USER, "local-user")
PRICING = ProviderPricing(
    unit="second",
    unit_price=Decimal("0.05"),
    currency="USD",
    source="test price sheet",
    verified_at=date(2026, 10, 1),
    conditions=("tối đa 10 giây mỗi clip",),
)
CONTEXT = PaidRequestContext()


class Clock:
    def __init__(self, moment: datetime = T0) -> None:
        self.now = moment

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **delta: float) -> None:
        self.now += timedelta(**delta)


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def sink() -> InMemoryAuditSink:
    return InMemoryAuditSink()


@pytest.fixture
def budget(database: Database, sink: InMemoryAuditSink, clock: Clock):
    return PaidApiBudgetService(
        database, AuditLog(sink), quote_ttl=timedelta(minutes=30), clock=clock
    )


def spec(**overrides) -> PaidRequestSpec:
    values = {
        "provider": "priced_video",
        "model": "clip-v1",
        "task_type": PaidTaskType.VIDEO_CLIP_GENERATION,
        "unit": "second",
        "unit_count": Decimal("6.5"),
        "fingerprint": "a" * 64,
        "duration_ms": 6500,
        "scene_id": "scene_1",
    }
    values.update(overrides)
    return PaidRequestSpec(**values)


def paid_mode(budget: PaidApiBudgetService) -> None:
    budget.set_policy(PaidApiMode.PAID_WITH_PER_REQUEST_APPROVAL, actor=USER)


def approve(budget: PaidApiBudgetService, quote) -> object:
    return budget.approve(
        quote.id,
        actor=USER,
        request_hash=quote.request_hash,
        estimated_amount=quote.estimated_amount,
        currency=quote.currency,
    )


def actions(sink: InMemoryAuditSink) -> list[str]:
    return [event.action for event in sink.events()]


# Policy


def test_the_default_policy_is_free_only(budget: PaidApiBudgetService) -> None:
    policy = budget.get_policy()

    assert policy == DEFAULT_POLICY
    assert policy.mode is PaidApiMode.FREE_ONLY
    assert policy.is_default is True
    assert policy.version == 0


def test_a_saved_policy_persists_across_service_instances(
    database: Database, sink: InMemoryAuditSink, clock: Clock
) -> None:
    first = PaidApiBudgetService(
        database, AuditLog(sink), quote_ttl=timedelta(minutes=30), clock=clock
    )
    saved = first.set_policy(PaidApiMode.PAID_WITH_PER_REQUEST_APPROVAL, actor=USER)
    second = PaidApiBudgetService(
        database, AuditLog(sink), quote_ttl=timedelta(minutes=30), clock=clock
    )

    policy = second.get_policy()

    assert policy == saved
    assert policy.mode is PaidApiMode.PAID_WITH_PER_REQUEST_APPROVAL
    assert policy.version == 1
    assert policy.updated_by == USER
    assert policy.updated_at == T0
    assert policy.is_default is False


@pytest.mark.parametrize("kind", [ActorKind.SYSTEM, ActorKind.AI])
def test_only_a_user_may_change_the_policy(
    budget: PaidApiBudgetService, sink: InMemoryAuditSink, kind: ActorKind
) -> None:
    with pytest.raises(PaidQuoteError) as refused:
        budget.set_policy(
            PaidApiMode.PAID_WITH_PER_REQUEST_APPROVAL, actor=Actor(kind, "agent")
        )

    assert refused.value.code == PaidBudgetErrorCode.POLICY_ACTOR_NOT_USER.value
    assert refused.value.to_public().http_status == 403
    assert budget.get_policy() == DEFAULT_POLICY
    assert actions(sink) == []


def test_a_policy_change_is_audited_and_an_unchanged_mode_is_not(
    budget: PaidApiBudgetService, sink: InMemoryAuditSink
) -> None:
    unchanged = budget.set_policy(PaidApiMode.FREE_ONLY, actor=USER)
    paid_mode(budget)
    budget.set_policy(PaidApiMode.FREE_ONLY, actor=USER, expected_version=1)

    assert unchanged == DEFAULT_POLICY
    events = sink.events()
    assert [event.action for event in events] == [
        "budget.paid_api_policy_changed",
        "budget.paid_api_policy_changed",
    ]
    assert dict(events[0].metadata) == {
        "from": "free_only",
        "to": "paid_with_per_request_approval",
        "version": 1,
    }
    assert events[1].metadata["version"] == 2
    assert events[0].entity.type == "paid_api_policy"
    assert events[0].actor == USER


def test_a_stale_expected_version_is_refused(budget: PaidApiBudgetService) -> None:
    paid_mode(budget)

    with pytest.raises(PaidQuoteError) as refused:
        budget.set_policy(PaidApiMode.FREE_ONLY, actor=USER, expected_version=0)

    assert refused.value.code == PaidBudgetErrorCode.POLICY_VERSION_CONFLICT.value
    assert refused.value.to_public().http_status == 409
    assert budget.get_policy().mode is PaidApiMode.PAID_WITH_PER_REQUEST_APPROVAL


# Quotes


def test_the_request_hash_is_stable_and_bound_to_the_request() -> None:
    assert spec().request_hash == spec().request_hash
    assert spec().request_hash == spec(unit_count=Decimal("6.50")).request_hash
    assert spec().request_hash != spec(fingerprint="b" * 64).request_hash
    assert spec().request_hash != spec(model="clip-v2").request_hash
    assert spec().request_hash != spec(unit_count=Decimal("7")).request_hash
    # Scene and duration describe the request; the fingerprint binds it.
    assert spec().request_hash == spec(scene_id="other").request_hash


def test_a_quote_estimates_with_decimals(budget: PaidApiBudgetService) -> None:
    quote = budget.open_quote(spec(), PRICING, CONTEXT, unknowns=("hạn mức",))

    assert quote.status is PaidQuoteStatus.PENDING
    assert quote.unit_price == Decimal("0.05")
    assert quote.estimated_amount == Decimal("0.325000")
    assert isinstance(quote.estimated_amount, Decimal)
    assert quote.currency == "USD"
    assert quote.conditions == ("tối đa 10 giây mỗi clip",)
    assert quote.unknowns == ("hạn mức",)
    assert quote.expires_at == T0 + timedelta(minutes=30)
    assert budget.get_quote(quote.id) == quote
    assert quote.as_dict()["estimated_amount"] == "0.325000"


def test_a_pending_quote_of_the_same_request_is_reused(
    budget: PaidApiBudgetService, sink: InMemoryAuditSink
) -> None:
    first = budget.open_quote(spec(), PRICING, CONTEXT)
    second = budget.open_quote(spec(), PRICING, CONTEXT)

    assert second.id == first.id
    assert actions(sink) == ["budget.paid_quote_created"]


def test_a_changed_price_replaces_the_pending_quote(
    budget: PaidApiBudgetService,
) -> None:
    first = budget.open_quote(spec(), PRICING, CONTEXT)
    dearer = ProviderPricing(
        unit="second",
        unit_price=Decimal("0.07"),
        currency="USD",
        source="test price sheet",
        verified_at=date(2026, 10, 2),
    )

    second = budget.open_quote(spec(), dearer, CONTEXT)

    assert second.id != first.id
    assert budget.get_quote(first.id).status is PaidQuoteStatus.EXPIRED
    assert second.estimated_amount == Decimal("0.455000")


def test_approve_binds_the_quote(
    budget: PaidApiBudgetService, sink: InMemoryAuditSink
) -> None:
    paid_mode(budget)
    quote = budget.open_quote(spec(), PRICING, CONTEXT)

    approved = approve(budget, quote)

    assert approved.status is PaidQuoteStatus.APPROVED
    assert approved.decided_by == USER
    assert approved.decided_at == T0
    assert budget.get_quote(quote.id) == approved
    assert actions(sink)[-1] == "budget.paid_quote_approved"


@pytest.mark.parametrize(
    "echo",
    [
        {"request_hash": "b" * 64},
        {"estimated_amount": Decimal("0.32")},
        {"currency": "EUR"},
    ],
)
def test_approve_refuses_a_mismatched_echo(
    budget: PaidApiBudgetService, echo: dict
) -> None:
    paid_mode(budget)
    quote = budget.open_quote(spec(), PRICING, CONTEXT)
    values = {
        "request_hash": quote.request_hash,
        "estimated_amount": quote.estimated_amount,
        "currency": quote.currency,
        **echo,
    }

    with pytest.raises(PaidQuoteError) as refused:
        budget.approve(quote.id, actor=USER, **values)

    assert refused.value.code == PaidBudgetErrorCode.QUOTE_MISMATCH.value
    assert budget.get_quote(quote.id).status is PaidQuoteStatus.PENDING


def test_approve_refuses_an_expired_quote_and_expires_it(
    budget: PaidApiBudgetService, clock: Clock
) -> None:
    paid_mode(budget)
    quote = budget.open_quote(spec(), PRICING, CONTEXT)
    clock.advance(minutes=30)

    with pytest.raises(PaidQuoteError) as refused:
        approve(budget, quote)

    assert refused.value.code == PaidBudgetErrorCode.QUOTE_EXPIRED.value
    assert budget.get_quote(quote.id).status is PaidQuoteStatus.EXPIRED


def test_approve_refuses_a_quote_that_is_not_pending(
    budget: PaidApiBudgetService,
) -> None:
    paid_mode(budget)
    quote = budget.open_quote(spec(), PRICING, CONTEXT)
    approve(budget, quote)

    with pytest.raises(PaidQuoteError) as refused:
        approve(budget, quote)

    assert refused.value.code == PaidBudgetErrorCode.QUOTE_NOT_PENDING.value


def test_approve_is_refused_in_free_only(budget: PaidApiBudgetService) -> None:
    quote = budget.open_quote(spec(), PRICING, CONTEXT)

    with pytest.raises(PaidQuoteError) as refused:
        approve(budget, quote)

    assert refused.value.code == PaidBudgetErrorCode.PAID_MODE_DISABLED.value
    assert budget.get_quote(quote.id).status is PaidQuoteStatus.PENDING


def test_approve_by_a_non_user_and_of_an_unknown_quote_is_refused(
    budget: PaidApiBudgetService,
) -> None:
    paid_mode(budget)
    quote = budget.open_quote(spec(), PRICING, CONTEXT)

    with pytest.raises(PaidQuoteError) as by_ai:
        budget.approve(
            quote.id,
            actor=Actor(ActorKind.AI, "agent"),
            request_hash=quote.request_hash,
            estimated_amount=quote.estimated_amount,
            currency="USD",
        )
    with pytest.raises(PaidQuoteError) as unknown:
        budget.approve(
            "pq_missing",
            actor=USER,
            request_hash=quote.request_hash,
            estimated_amount=quote.estimated_amount,
            currency="USD",
        )

    assert by_ai.value.code == PaidBudgetErrorCode.POLICY_ACTOR_NOT_USER.value
    assert unknown.value.code == PaidBudgetErrorCode.QUOTE_NOT_FOUND.value
    assert unknown.value.to_public().http_status == 404


def test_reject_a_pending_quote_and_withdraw_an_approval(
    budget: PaidApiBudgetService, sink: InMemoryAuditSink
) -> None:
    paid_mode(budget)
    pending = budget.open_quote(spec(), PRICING, CONTEXT)
    other = budget.open_quote(spec(fingerprint="c" * 64), PRICING, CONTEXT)
    approve(budget, other)

    rejected = budget.reject(pending.id, actor=USER)
    withdrawn = budget.reject(other.id, actor=USER)

    assert rejected.status is PaidQuoteStatus.REJECTED
    assert withdrawn.status is PaidQuoteStatus.REJECTED
    assert budget.consume(spec(fingerprint="c" * 64), PRICING) is None
    with pytest.raises(PaidQuoteError) as again:
        budget.reject(pending.id, actor=USER)
    assert again.value.code == PaidBudgetErrorCode.QUOTE_NOT_PENDING.value
    assert actions(sink).count("budget.paid_quote_rejected") == 2


def test_an_approval_is_consumed_once(
    budget: PaidApiBudgetService, sink: InMemoryAuditSink
) -> None:
    paid_mode(budget)
    quote = budget.open_quote(spec(), PRICING, CONTEXT)
    approve(budget, quote)

    first = budget.consume(spec(), PRICING)
    second = budget.consume(spec(), PRICING)

    assert first is not None
    assert first.id == quote.id
    assert first.status is PaidQuoteStatus.CONSUMED
    assert first.consumed_at == T0
    assert second is None
    assert actions(sink).count("budget.paid_quote_consumed") == 1


def test_an_expired_approval_is_not_consumed(
    budget: PaidApiBudgetService, clock: Clock
) -> None:
    paid_mode(budget)
    quote = budget.open_quote(spec(), PRICING, CONTEXT)
    approve(budget, quote)
    clock.advance(minutes=31)

    assert budget.consume(spec(), PRICING) is None
    assert budget.get_quote(quote.id).status is PaidQuoteStatus.EXPIRED


def test_a_price_change_after_the_approval_expires_it(
    budget: PaidApiBudgetService,
) -> None:
    paid_mode(budget)
    quote = budget.open_quote(spec(), PRICING, CONTEXT)
    approve(budget, quote)
    dearer = ProviderPricing(
        unit="second",
        unit_price=Decimal("0.06"),
        currency="USD",
        source="test price sheet",
        verified_at=date(2026, 10, 2),
    )

    assert budget.consume(spec(), dearer) is None
    assert budget.get_quote(quote.id).status is PaidQuoteStatus.EXPIRED


def test_switching_back_to_free_only_makes_approvals_unusable(
    budget: PaidApiBudgetService,
) -> None:
    paid_mode(budget)
    quote = budget.open_quote(spec(), PRICING, CONTEXT)
    approve(budget, quote)
    budget.set_policy(PaidApiMode.FREE_ONLY, actor=USER)

    assert budget.consume(spec(), PRICING) is None
    assert budget.get_quote(quote.id).status is PaidQuoteStatus.APPROVED


def test_two_threads_consuming_the_same_approval_have_exactly_one_winner(
    budget: PaidApiBudgetService,
) -> None:
    paid_mode(budget)
    approve(budget, budget.open_quote(spec(), PRICING, CONTEXT))
    barrier = threading.Barrier(2)
    results: list[object] = []
    lock = threading.Lock()

    def consume() -> None:
        barrier.wait(timeout=10)
        outcome = budget.consume(spec(), PRICING)
        with lock:
            results.append(outcome)

    threads = [threading.Thread(target=consume) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert len(results) == 2
    assert sum(1 for outcome in results if outcome is not None) == 1


def test_consume_uses_exactly_the_quote_it_checked(
    budget: PaidApiBudgetService, monkeypatch: pytest.MonkeyPatch
) -> None:
    paid_mode(budget)
    checked = approve(budget, budget.open_quote(spec(), PRICING, CONTEXT))
    replacement: list[object] = []
    real_check = budget.approved_quote

    def check_then_swap(request_spec, pricing):
        # Between the check and the consume the checked approval is withdrawn
        # and another quote of the same hash is approved.
        found = real_check(request_spec, pricing)
        budget.reject(checked.id, actor=USER)
        replacement.append(
            approve(budget, budget.open_quote(request_spec, pricing, CONTEXT))
        )
        return found

    monkeypatch.setattr(budget, "approved_quote", check_then_swap)

    assert budget.consume(spec(), PRICING) is None
    assert budget.get_quote(checked.id).status is PaidQuoteStatus.REJECTED
    assert budget.get_quote(replacement[0].id).status is PaidQuoteStatus.APPROVED


def test_the_repository_consumes_a_quote_by_id_once_while_unexpired(
    budget: PaidApiBudgetService, database: Database, clock: Clock
) -> None:
    paid_mode(budget)
    quote = approve(budget, budget.open_quote(spec(), PRICING, CONTEXT))
    pending = budget.open_quote(spec(fingerprint="c" * 64), PRICING, CONTEXT)

    with database.transaction() as connection:
        quotes = PaidApiQuoteRepository(connection)
        assert quotes.consume(pending.id, clock()) is None
        assert quotes.consume("pq_missing", clock()) is None
        assert quotes.consume(quote.id, quote.expires_at) is None
        consumed = quotes.consume(quote.id, clock())
        again = quotes.consume(quote.id, clock())

    assert consumed is not None
    assert consumed.id == quote.id
    assert consumed.status is PaidQuoteStatus.CONSUMED
    assert again is None


def test_a_concurrent_open_quote_of_the_same_hash_reuses_the_pending_one(
    budget: PaidApiBudgetService, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = budget.open_quote(spec(), PRICING, CONTEXT)
    real_find_open = PaidApiQuoteRepository.find_open
    reads: list[str] = []

    def stale_once(self, request_hash):
        # The first read misses the quote another call stored meanwhile.
        reads.append(request_hash)
        if len(reads) == 1:
            return None
        return real_find_open(self, request_hash)

    monkeypatch.setattr(PaidApiQuoteRepository, "find_open", stale_once)

    second = budget.open_quote(spec(), PRICING, CONTEXT)

    assert second.id == first.id
    assert second.status is PaidQuoteStatus.PENDING
    assert len(reads) == 2
    assert [q.id for q in budget.list_quotes()] == [first.id]


def test_a_blocked_request_is_stored_and_never_approvable(
    budget: PaidApiBudgetService, sink: InMemoryAuditSink
) -> None:
    paid_mode(budget)

    blocked = budget.record_blocked(
        spec(), PaidBudgetErrorCode.PRICE_UNKNOWN, CONTEXT, unknowns=("giá",)
    )

    assert blocked.status is PaidQuoteStatus.BLOCKED
    assert blocked.blocked_reason == "budget.price_unknown"
    assert blocked.price_known is False
    assert blocked.estimated_amount is None
    with pytest.raises(PaidQuoteError) as refused:
        budget.approve(
            blocked.id,
            actor=USER,
            request_hash=blocked.request_hash,
            estimated_amount=Decimal("0"),
            currency="USD",
        )
    assert refused.value.code == PaidBudgetErrorCode.QUOTE_NOT_PENDING.value
    assert sink.events()[-1].action == "budget.paid_request_blocked"
    assert sink.events()[-1].metadata["reason"] == "budget.price_unknown"


def test_quotes_are_listed_newest_first_and_filtered(
    budget: PaidApiBudgetService, clock: Clock
) -> None:
    first = budget.open_quote(spec(), PRICING, CONTEXT)
    clock.advance(seconds=1)
    blocked = budget.record_blocked(
        spec(fingerprint="d" * 64), PaidBudgetErrorCode.BLOCKED_FREE_ONLY, CONTEXT
    )

    assert [q.id for q in budget.list_quotes()] == [blocked.id, first.id]
    assert [q.id for q in budget.list_quotes(status=PaidQuoteStatus.PENDING)] == [
        first.id
    ]
    assert budget.list_quotes(job_id="nope") == []
    with pytest.raises(ValueError):
        budget.list_quotes(limit=0)


def test_audit_metadata_holds_no_prompt_or_key(
    budget: PaidApiBudgetService, sink: InMemoryAuditSink
) -> None:
    paid_mode(budget)
    approve(budget, budget.open_quote(spec(), PRICING, CONTEXT))
    budget.consume(spec(), PRICING)

    for event in sink.events():
        assert "prompt" not in event.metadata
        assert "api_key" not in event.metadata
        assert all(
            not isinstance(value, str) or "secret" not in value
            for value in event.metadata.values()
        )


def test_budget_stop_codes_are_recognised() -> None:
    assert is_budget_stop_code("budget.paid_request_awaiting_approval")
    assert is_budget_stop_code("budget.paid_provider_blocked_free_only")
    assert is_budget_stop_code("budget.price_unknown")
    assert is_budget_stop_code("budget.provider_cost_unknown")
    assert not is_budget_stop_code("pipeline.script_to_video_stage_failed")
    assert not is_budget_stop_code(None)
