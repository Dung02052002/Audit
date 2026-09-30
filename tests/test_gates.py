"""C-033 Pipeline Gate Contract (Prompt Pack v8, prompt #033).

The design is the one the user approved on 2026-09-30: outcomes PASS and
BLOCK, a context of item + requested status + actor + time, every gate run and
reported, and a closed ``GateName`` enum. The real gates come with #034-#041,
so these tests use small fake gates.
"""

import dataclasses
import logging
from datetime import UTC, datetime, timedelta, timezone

import pytest

from ai_youtube_agent.core.audit import Actor, ActorKind
from ai_youtube_agent.core.content_item import (
    ContentItem,
    ContentStatus,
    ContentTransitionError,
    ContentType,
)
from ai_youtube_agent.core.errors import DomainError
from ai_youtube_agent.core.gates import (
    GateBlockedError,
    GateContext,
    GateName,
    GateOutcome,
    GateReason,
    GateReport,
    GateResult,
    PipelineGate,
    evaluate_gates,
)

T0 = datetime(2026, 9, 30, 12, 0, tzinfo=UTC)
USER = Actor(ActorKind.USER, "owner-1")
SYSTEM = Actor(ActorKind.SYSTEM, "pipeline")
REASON = GateReason("rights.high_risk_unresolved", "A high-risk asset is not resolved.")


def item_in(status: ContentStatus) -> ContentItem:
    item = ContentItem.create(
        "channel-1", "strategy-1", 1, ContentType.SHORTS, "Video", clock=lambda: T0
    )
    return dataclasses.replace(item, status=status)


def publish_context() -> GateContext:
    return GateContext(
        item=item_in(ContentStatus.APPROVED),
        target_status=ContentStatus.PUBLISHING,
        actor=SYSTEM,
        at=T0,
    )


class PassingGate:
    def __init__(self, name: GateName) -> None:
        self.name = name
        self.seen: list[GateContext] = []

    def evaluate(self, context: GateContext) -> GateResult:
        self.seen.append(context)
        return GateResult.passed(self.name, context.at)


class BlockingGate:
    def __init__(self, name: GateName, *reasons: GateReason) -> None:
        self.name = name
        self.reasons = reasons or (REASON,)

    def evaluate(self, context: GateContext) -> GateResult:
        return GateResult.blocked(self.name, context.at, *self.reasons)


class RaisingGate:
    def __init__(self, name: GateName, exc: Exception) -> None:
        self.name = name
        self.exc = exc

    def evaluate(self, context: GateContext) -> GateResult:
        raise self.exc


class WrongResultGate:
    def __init__(self, name: GateName, result: object) -> None:
        self.name = name
        self.result = result

    def evaluate(self, context: GateContext) -> GateResult:
        return self.result  # type: ignore[return-value]


# Names and outcomes


def test_gate_names_are_the_five_of_prompt_033() -> None:
    assert [g.value for g in GateName][:5] == [
        "test",
        "qc",
        "rights",
        "policy",
        "approval",
    ]


def test_later_gates_add_their_names_after_the_five() -> None:
    assert [g.value for g in GateName][5:] == ["daily_limit"]


def test_there_are_only_two_outcomes() -> None:
    assert [o.value for o in GateOutcome] == ["pass", "block"]


# Reasons


def test_reason_keeps_a_code_and_a_safe_message() -> None:
    assert (REASON.code, REASON.message) == (
        "rights.high_risk_unresolved",
        "A high-risk asset is not resolved.",
    )
    assert REASON.as_dict() == {
        "code": "rights.high_risk_unresolved",
        "message": "A high-risk asset is not resolved.",
    }


@pytest.mark.parametrize("code", ["", "rights", "Rights.High", "rights.", "a b.c"])
def test_reason_code_must_be_a_dotted_name(code: str) -> None:
    with pytest.raises(ValueError):
        GateReason(code, "message")


def test_reason_message_must_not_be_empty() -> None:
    with pytest.raises(ValueError):
        GateReason("rights.unresolved", "  ")


# Results


def test_a_passed_result_has_no_reasons() -> None:
    result = GateResult.passed(GateName.QC, T0)
    assert result.outcome is GateOutcome.PASS
    assert result.is_passed
    assert result.reasons == ()


def test_a_blocked_result_keeps_its_reasons_in_order() -> None:
    other = GateReason("rights.licence_missing", "A licence is missing.")
    result = GateResult.blocked(GateName.RIGHTS, T0, REASON, other)
    assert result.outcome is GateOutcome.BLOCK
    assert not result.is_passed
    assert result.reasons == (REASON, other)


def test_a_block_needs_at_least_one_reason() -> None:
    with pytest.raises(ValueError):
        GateResult.blocked(GateName.RIGHTS, T0)


def test_a_pass_cannot_carry_reasons() -> None:
    with pytest.raises(ValueError):
        GateResult(GateName.QC, GateOutcome.PASS, (REASON,), T0)


def test_result_fields_are_type_checked() -> None:
    with pytest.raises(TypeError):
        GateResult("qc", GateOutcome.PASS, (), T0)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        GateResult(GateName.QC, "pass", (), T0)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        GateResult(GateName.QC, GateOutcome.BLOCK, ["x"], T0)  # type: ignore[arg-type]


def test_result_time_must_be_utc() -> None:
    with pytest.raises(ValueError):
        GateResult.passed(GateName.QC, T0.replace(tzinfo=None))
    with pytest.raises(ValueError):
        GateResult.passed(GateName.QC, T0.astimezone(timezone(timedelta(hours=7))))


def test_result_is_frozen_and_json_friendly() -> None:
    result = GateResult.blocked(GateName.RIGHTS, T0, REASON)
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.outcome = GateOutcome.PASS  # type: ignore[misc]
    assert result.as_dict() == {
        "gate": "rights",
        "outcome": "block",
        "reasons": [REASON.as_dict()],
        "evaluated_at": T0.isoformat(),
    }


# Context


def test_context_holds_item_target_actor_and_time() -> None:
    context = publish_context()
    assert context.item.status is ContentStatus.APPROVED
    assert context.target_status is ContentStatus.PUBLISHING
    assert context.actor == SYSTEM
    assert context.at == T0


def test_context_refuses_a_move_the_transition_rules_block() -> None:
    with pytest.raises(ContentTransitionError):
        GateContext(
            item=item_in(ContentStatus.DRAFT),
            target_status=ContentStatus.PUBLISHING,
            actor=USER,
            at=T0,
        )


def test_context_refuses_staying_in_the_same_status() -> None:
    with pytest.raises(ValueError):
        GateContext(
            item=item_in(ContentStatus.APPROVED),
            target_status=ContentStatus.APPROVED,
            actor=USER,
            at=T0,
        )


def test_context_fields_are_checked() -> None:
    item = item_in(ContentStatus.APPROVED)
    with pytest.raises(TypeError):
        GateContext(item, "publishing", USER, T0)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        GateContext(item, ContentStatus.PUBLISHING, "owner-1", T0)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        GateContext("item", ContentStatus.PUBLISHING, USER, T0)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        GateContext(item, ContentStatus.PUBLISHING, USER, T0.replace(tzinfo=None))


# The protocol


def test_fake_gates_satisfy_the_protocol() -> None:
    assert isinstance(PassingGate(GateName.QC), PipelineGate)
    assert not isinstance(object(), PipelineGate)


# Running gates


def test_every_gate_runs_in_order_and_all_pass() -> None:
    gates = [PassingGate(GateName.TEST), PassingGate(GateName.QC)]
    context = publish_context()

    report = evaluate_gates(gates, context)

    assert report.outcome is GateOutcome.PASS
    assert report.is_passed
    assert [r.gate for r in report.results] == [GateName.TEST, GateName.QC]
    assert all(gate.seen == [context] for gate in gates)
    assert report.blocked == ()
    assert report.reasons == ()
    report.raise_if_blocked()


def test_one_block_blocks_the_report_but_every_gate_still_runs() -> None:
    last = PassingGate(GateName.APPROVAL)
    gates = [
        BlockingGate(GateName.RIGHTS),
        PassingGate(GateName.QC),
        BlockingGate(
            GateName.POLICY, GateReason("policy.disclosure_missing", "Add disclosure.")
        ),
        last,
    ]

    report = evaluate_gates(gates, publish_context())

    assert report.outcome is GateOutcome.BLOCK
    assert not report.is_passed
    assert len(report.results) == 4
    assert last.seen  # the gate after a block still ran
    assert [r.gate for r in report.blocked] == [GateName.RIGHTS, GateName.POLICY]
    assert [r.code for r in report.reasons] == [
        "rights.high_risk_unresolved",
        "policy.disclosure_missing",
    ]


def test_a_gate_that_raises_blocks_and_hides_the_detail(caplog) -> None:
    gates = [
        RaisingGate(GateName.RIGHTS, RuntimeError("db password is hunter2")),
        PassingGate(GateName.QC),
    ]

    with caplog.at_level(logging.WARNING, logger="ai_youtube_agent.core.gates"):
        report = evaluate_gates(gates, publish_context())

    (blocked,) = report.blocked
    assert blocked.gate is GateName.RIGHTS
    assert blocked.evaluated_at == T0
    assert [r.code for r in blocked.reasons] == ["gate.error"]
    assert "hunter2" not in blocked.reasons[0].message
    assert "gate failed" in caplog.text


def test_a_domain_error_from_a_gate_also_blocks() -> None:
    gates = [RaisingGate(GateName.APPROVAL, DomainError("no approval"))]
    report = evaluate_gates(gates, publish_context())
    assert [r.code for r in report.reasons] == ["gate.error"]


@pytest.mark.parametrize(
    "result",
    [
        None,
        "pass",
        GateResult.passed(GateName.QC, T0),  # a result for another gate
    ],
)
def test_a_gate_that_returns_a_wrong_result_blocks(result) -> None:
    report = evaluate_gates(
        [WrongResultGate(GateName.RIGHTS, result)], publish_context()
    )
    (blocked,) = report.blocked
    assert blocked.gate is GateName.RIGHTS
    assert [r.code for r in blocked.reasons] == ["gate.invalid_result"]


def test_no_gates_is_refused() -> None:
    with pytest.raises(ValueError):
        evaluate_gates([], publish_context())


def test_the_same_gate_twice_is_refused() -> None:
    with pytest.raises(ValueError):
        evaluate_gates(
            [PassingGate(GateName.QC), PassingGate(GateName.QC)], publish_context()
        )


def test_a_gate_without_a_gate_name_is_refused() -> None:
    gate = PassingGate(GateName.QC)
    gate.name = "qc"  # type: ignore[assignment]
    with pytest.raises(TypeError):
        evaluate_gates([gate], publish_context())


# Report and the blocked error


def test_report_is_json_friendly() -> None:
    report = evaluate_gates(
        [PassingGate(GateName.QC), BlockingGate(GateName.RIGHTS)], publish_context()
    )
    assert report.as_dict() == {
        "outcome": "block",
        "results": [
            {
                "gate": "qc",
                "outcome": "pass",
                "reasons": [],
                "evaluated_at": T0.isoformat(),
            },
            {
                "gate": "rights",
                "outcome": "block",
                "reasons": [REASON.as_dict()],
                "evaluated_at": T0.isoformat(),
            },
        ],
    }


def test_a_report_needs_at_least_one_result() -> None:
    with pytest.raises(ValueError):
        GateReport(())


def test_raise_if_blocked_raises_a_typed_domain_error() -> None:
    report = evaluate_gates([BlockingGate(GateName.RIGHTS)], publish_context())

    with pytest.raises(GateBlockedError) as caught:
        report.raise_if_blocked()

    error = caught.value
    assert isinstance(error, DomainError)
    assert error.code == "domain.gate_blocked"
    assert error.report is report
    assert error.to_public().http_status == 422
    fields = error.log_fields()
    assert fields["blocked_gates"] == ["rights"]
    assert fields["reason_codes"] == ["rights.high_risk_unresolved"]
