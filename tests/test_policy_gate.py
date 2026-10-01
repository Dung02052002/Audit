"""C-039 Policy Gate (Prompt Pack v8, prompt #039).

Rules the user approved on 2026-10-01:

- the gate reads ``PolicyCheck`` values through a protocol; there is no table
  until #080/#083;
- a finding blocks only when its rule is configured as blocking; non-blocking
  findings are warnings;
- an item without any policy check blocks (fail closed);
- only the newest check counts;
- the gate only blocks the move to ``publishing``.
"""

import dataclasses
from datetime import UTC, datetime, timedelta

import pytest

from ai_youtube_agent.content.policy import PolicyCheck, PolicyFinding
from ai_youtube_agent.core.audit import Actor, ActorKind
from ai_youtube_agent.core.content_item import ContentItem, ContentStatus, ContentType
from ai_youtube_agent.core.gates import (
    GateContext,
    GateName,
    GateOutcome,
    PipelineGate,
    evaluate_gates,
)
from ai_youtube_agent.core.policy_gate import PolicyGate

T0 = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
SYSTEM = Actor(ActorKind.SYSTEM, "pipeline")


class Checks:
    """In-memory stand-in for the future policy check repository."""

    def __init__(self) -> None:
        self.checks: list[PolicyCheck] = []

    def list_by_content_item(self, content_item_id: str) -> list[PolicyCheck]:
        return [c for c in self.checks if c.content_item_id == content_item_id]


def at(moment: datetime):
    return lambda: moment


def new_item(status: ContentStatus = ContentStatus.APPROVED) -> ContentItem:
    item = ContentItem.create(
        "channel-1", "strategy-1", 1, ContentType.SHORTS, "Video", clock=at(T0)
    )
    return dataclasses.replace(item, status=status)


def finding(rule: str = "no-medical-claims", *, blocking: bool = True, version=1):
    return PolicyFinding(rule, version, blocking, f"{rule} was broken.")


def check(item: ContentItem, *findings: PolicyFinding, minutes: int = 0):
    return PolicyCheck.create(
        item.id, findings, clock=at(T0 + timedelta(minutes=minutes))
    )


@pytest.fixture
def checks() -> Checks:
    return Checks()


@pytest.fixture
def gate(checks: Checks) -> PolicyGate:
    return PolicyGate(checks)


def context(item: ContentItem, target=ContentStatus.PUBLISHING) -> GateContext:
    return GateContext(item=item, target_status=target, actor=SYSTEM, at=T0)


def codes(result) -> list[str]:
    return [r.code for r in result.reasons]


# Values


def test_a_finding_needs_a_rule_a_version_and_a_message() -> None:
    with pytest.raises(ValueError):
        PolicyFinding(" ", 1, True, "m")
    with pytest.raises(ValueError):
        PolicyFinding("r", 0, True, "m")
    with pytest.raises(ValueError):
        PolicyFinding("r", True, True, "m")
    with pytest.raises(ValueError):
        PolicyFinding("r", 1, True, " ")
    with pytest.raises(TypeError):
        PolicyFinding("r", 1, "yes", "m")


def test_a_check_is_utc_and_holds_findings_only() -> None:
    with pytest.raises(ValueError):
        PolicyCheck("id", "item", datetime(2026, 10, 1), ())
    with pytest.raises(TypeError):
        PolicyCheck("id", "item", T0, [finding()])
    with pytest.raises(ValueError):
        PolicyCheck("id", " ", T0, ())


def test_a_check_lists_its_blocking_findings_and_serialises() -> None:
    hard, soft = finding("a"), finding("b", blocking=False)
    made = PolicyCheck.create("item-1", [hard, soft], clock=at(T0))

    assert made.blocking_findings == (hard,)
    assert made.as_dict() == {
        "id": made.id,
        "content_item_id": "item-1",
        "checked_at": T0.isoformat(),
        "findings": [hard.as_dict(), soft.as_dict()],
    }
    with pytest.raises(dataclasses.FrozenInstanceError):
        made.findings = ()  # type: ignore[misc]


# Contract


def test_the_gate_follows_the_contract(gate: PolicyGate) -> None:
    assert isinstance(gate, PipelineGate)
    assert gate.name is GateName.POLICY


# Passing


def test_passes_when_the_newest_check_has_no_findings(
    checks: Checks, gate: PolicyGate
) -> None:
    item = new_item()
    checks.checks.append(check(item))

    result = gate.evaluate(context(item))

    assert result.outcome is GateOutcome.PASS
    assert result.gate is GateName.POLICY
    assert result.evaluated_at == T0


def test_non_blocking_findings_are_warnings(checks: Checks, gate: PolicyGate) -> None:
    item = new_item()
    checks.checks.append(check(item, finding(blocking=False)))

    assert gate.evaluate(context(item)).is_passed


def test_a_later_clean_check_clears_an_older_failure(
    checks: Checks, gate: PolicyGate
) -> None:
    item = new_item()
    checks.checks += [check(item, minutes=5), check(item, finding(), minutes=0)]

    assert gate.evaluate(context(item)).is_passed


@pytest.mark.parametrize(
    "target",
    [ContentStatus.GENERATING, ContentStatus.AWAITING_APPROVAL, ContentStatus.FAILED],
)
def test_other_moves_are_not_this_gates_concern(
    gate: PolicyGate, target: ContentStatus
) -> None:
    # No policy check at all, yet the gate passes.
    item = new_item(ContentStatus.PREVIEW_READY)

    assert gate.evaluate(context(item, target)).is_passed


# Blocking


def test_blocks_without_any_check(gate: PolicyGate) -> None:
    result = gate.evaluate(context(new_item()))

    assert result.outcome is GateOutcome.BLOCK
    assert codes(result) == ["policy.not_checked"]


def test_another_items_check_does_not_count(checks: Checks, gate: PolicyGate) -> None:
    item, other = new_item(), new_item()
    checks.checks.append(check(other))

    assert codes(gate.evaluate(context(item))) == ["policy.not_checked"]


def test_blocks_on_a_blocking_finding(checks: Checks, gate: PolicyGate) -> None:
    item = new_item()
    checks.checks.append(check(item, finding("no-medical-claims", version=3)))

    result = gate.evaluate(context(item))

    assert codes(result) == ["policy.failed"]
    assert "no-medical-claims" in result.reasons[0].message
    assert "version 3" in result.reasons[0].message


def test_reports_every_blocking_finding_and_skips_warnings(
    checks: Checks, gate: PolicyGate
) -> None:
    item = new_item()
    checks.checks.append(
        check(item, finding("a"), finding("warn", blocking=False), finding("b"))
    )

    result = gate.evaluate(context(item))

    assert codes(result) == ["policy.failed", "policy.failed"]
    assert "a (" in result.reasons[0].message
    assert "b (" in result.reasons[1].message


def test_an_older_clean_check_does_not_hide_a_newer_failure(
    checks: Checks, gate: PolicyGate
) -> None:
    item = new_item()
    checks.checks += [check(item, finding(), minutes=5), check(item, minutes=0)]

    assert codes(gate.evaluate(context(item))) == ["policy.failed"]


def test_a_failing_source_blocks_through_evaluate_gates() -> None:
    class Broken:
        def list_by_content_item(self, content_item_id: str):
            raise RuntimeError("database is locked")

    report = evaluate_gates([PolicyGate(Broken())], context(new_item()))

    assert report.outcome is GateOutcome.BLOCK
    assert [r.code for r in report.reasons] == ["gate.error"]
