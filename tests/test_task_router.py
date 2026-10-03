"""Task/Model Router for the development workflow (tools/task_router.py).

Development infrastructure, not part of the package: the policy lives in
``.claude/task-router.json``, the rules in ``docs/TASK_ROUTER.md``. Checks
classification, the model/effort policy, overrides, escalation and that the
generated subagent files match the policy.
"""

import copy
import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "task_router", ROOT / "tools" / "task_router.py"
)
router = importlib.util.module_from_spec(_spec)
sys.modules["task_router"] = router
_spec.loader.exec_module(router)

RAW = json.loads((ROOT / ".claude" / "task-router.json").read_text(encoding="utf-8"))
POLICY = router.parse_policy(RAW)

BASE = {
    "files_affected": 1,
    "modules_affected": 1,
    "dependency_depth": 0,
    "behavior_change": False,
    "architecture_impact": "none",
    "database_change": False,
    "migration_required": False,
    "provider_change": "none",
    "concurrency_risk": "none",
    "failure_recovery_complexity": "none",
    "test_complexity": "low",
    "backward_compatibility_risk": "none",
    "uncertainty": "low",
    "rollback_risk": "low",
}


def task(**changes):
    return {**BASE, **changes}


def route(name="task", override=None, **changes):
    return router.route(name, task(**changes), POLICY, override)


# Levels and the default policy


def test_a_trivial_task_goes_to_the_lightest_model() -> None:
    decision = route("fix a typo in README")

    assert (decision.level, decision.model, decision.effort) == (
        "TRIVIAL",
        "haiku",
        "low",
    )
    assert not decision.planner and not decision.reviewer
    assert not decision.uncertain


def test_a_rename_across_three_files_does_not_need_opus() -> None:
    decision = route("rename a property", files_affected=3, test_complexity="low")

    assert decision.level in ("TRIVIAL", "SIMPLE") and decision.model != "opus"


def test_a_simple_task_goes_to_sonnet_low() -> None:
    decision = route("fix one failing assertion", behavior_change=True)

    assert (decision.level, decision.model, decision.effort) == (
        "SIMPLE",
        "sonnet",
        "low",
    )
    assert not decision.planner


def test_a_normal_task_goes_to_sonnet_medium_with_a_planner() -> None:
    decision = route(
        "add a feature over a few files",
        behavior_change=True,
        files_affected=4,
        modules_affected=2,
        test_complexity="medium",
    )

    assert (decision.level, decision.model, decision.effort) == (
        "NORMAL",
        "sonnet",
        "medium",
    )
    assert decision.planner and not decision.reviewer


def test_a_complex_task_goes_to_opus_high_with_planner_and_reviewer() -> None:
    decision = route(
        "database + domain + provider feature",
        behavior_change=True,
        files_affected=8,
        modules_affected=3,
        database_change=True,
        migration_required=True,
        provider_change="logic",
    )

    assert (decision.level, decision.model, decision.effort) == (
        "COMPLEX",
        "opus",
        "high",
    )
    assert decision.planner and decision.reviewer
    assert not decision.architecture_review


def test_an_architectural_task_goes_to_opus_with_architecture_review() -> None:
    decision = route(
        "design a new persistence architecture",
        behavior_change=True,
        architecture_impact="architectural",
    )

    assert (decision.level, decision.model, decision.effort) == (
        "ARCHITECTURAL",
        "opus",
        "high",
    )
    assert decision.architecture_review
    assert decision.reasons[0] == "floor: changes the architecture"


def test_many_files_alone_do_not_mean_opus() -> None:
    decision = route("format the whole repository", files_affected=60)

    assert decision.model != "opus"


# Floors for specific kinds of task


def test_a_migration_task_is_at_least_normal() -> None:
    decision = route("add a column", behavior_change=True, migration_required=True)

    assert decision.level == "NORMAL"
    assert "floor: needs a migration" in decision.reasons


def test_a_provider_logic_task_is_normal_and_a_contract_change_architectural() -> None:
    assert route("provider retry", provider_change="logic").level == "NORMAL"
    contract = route("change TextGenerator", provider_change="contract")
    assert contract.level == "ARCHITECTURAL" and contract.model == "opus"


def test_a_concurrency_task_is_complex() -> None:
    decision = route(
        "debug race condition in Research Failure Recovery",
        behavior_change=True,
        concurrency_risk="high",
    )

    assert (decision.level, decision.model) == ("COMPLEX", "opus")
    assert "floor: high concurrency risk" in decision.reasons


def test_failure_recovery_and_cross_module_tasks_are_complex() -> None:
    assert route(failure_recovery_complexity="high").level == "COMPLEX"
    assert route(architecture_impact="cross_module").level == "COMPLEX"


def test_a_high_score_without_floors_becomes_architectural() -> None:
    decision = route(
        files_affected=20,
        modules_affected=6,
        dependency_depth=4,
        behavior_change=True,
        database_change=True,
        migration_required=True,
        test_complexity="high",
        backward_compatibility_risk="high",
        rollback_risk="high",
        concurrency_risk="low",
    )

    assert decision.level == "ARCHITECTURAL"


# Uncertainty and invalid input


def test_an_ambiguous_task_takes_the_lower_level_and_is_marked_uncertain() -> None:
    decision = router.route("vague request", {"files_affected": 2}, POLICY)

    assert decision.level == "TRIVIAL" and decision.uncertain
    assert "modules_affected" in decision.unknown
    assert decision.effort not in ("xhigh", "max")
    assert decision.escalation_allowed
    assert "(uncertain)" in decision.render()


def test_high_uncertainty_is_marked_but_does_not_raise_the_level() -> None:
    decision = route(behavior_change=True, uncertainty="high")

    assert decision.level == "SIMPLE" and decision.uncertain


@pytest.mark.parametrize(
    "bad",
    [
        {"files": 1},
        {"files_affected": -1},
        {"files_affected": True},
        {"behavior_change": "yes"},
        {"provider_change": "big"},
    ],
)
def test_invalid_criteria_are_refused(bad) -> None:
    with pytest.raises(router.RouterError):
        router.route("task", bad, POLICY)


def test_the_policy_never_routes_to_xhigh_or_max() -> None:
    assert all(
        rule.effort not in router.OVERRIDE_ONLY_EFFORTS
        for rule in POLICY.levels.values()
    )
    data = copy.deepcopy(RAW)
    data["levels"]["ARCHITECTURAL"]["effort"] = "xhigh"
    with pytest.raises(router.RouterError, match="user override"):
        router.parse_policy(data)


# Configurable policy


def test_the_policy_can_be_changed_in_one_place() -> None:
    data = copy.deepcopy(RAW)
    data["levels"]["TRIVIAL"]["model"] = "sonnet"
    policy = router.parse_policy(data)

    decision = router.route("typo", task(), policy)

    assert decision.model == "sonnet"
    assert "model: sonnet" in router.agent_specs(policy)["task-coder-trivial.md"]


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("levels", "COMPLEX", "model"), "gpt4"),
        (("levels", "SIMPLE", "effort"), "extreme"),
        (("levels", "NORMAL", "planner"), "yes"),
        (("escalation", "max_steps"), 9),
        (("schema_version",), 2),
    ],
)
def test_an_unsupported_policy_configuration_is_refused(path, value) -> None:
    data = copy.deepcopy(RAW)
    target = data
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value

    with pytest.raises(router.RouterError):
        router.parse_policy(data)


def test_a_policy_missing_a_level_is_refused() -> None:
    data = copy.deepcopy(RAW)
    del data["levels"]["NORMAL"]

    with pytest.raises(router.RouterError):
        router.parse_policy(data)


# User override


def test_a_user_override_is_respected_and_recorded() -> None:
    override = router.Override(model="opus", effort="xhigh", planner=False)

    decision = route("typo", override=override)

    assert (decision.model, decision.effort, decision.planner) == (
        "opus",
        "xhigh",
        False,
    )
    assert decision.level == "TRIVIAL"
    assert decision.overrides == ("model=opus", "effort=xhigh", "planner=False")
    assert "User override" in decision.render()


def test_an_override_to_an_unsupported_model_or_effort_is_refused() -> None:
    with pytest.raises(router.RouterError, match="unsupported model"):
        route(override=router.Override(model="gpt-4"))
    with pytest.raises(router.RouterError, match="unsupported effort"):
        route(override=router.Override(effort="ultra"))


# Escalation


def test_escalation_moves_one_step_and_records_why() -> None:
    decision = route("normal feature", behavior_change=True, migration_required=True)

    up = router.escalate(decision, "logic_failure", "test_x fails: wrong total", POLICY)

    assert (up.model, up.effort, up.steps) == ("opus", "high", 1)
    assert up.reviewer
    assert up.history == (
        {
            "current": "sonnet/medium",
            "reason": "logic_failure",
            "evidence": "test_x fails: wrong total",
            "next": "opus/high",
        },
    )


def test_escalation_goes_low_medium_high_and_then_stops() -> None:
    decision = route("small bug", behavior_change=True)
    assert (decision.model, decision.effort) == ("sonnet", "low")

    first = router.escalate(decision, "repeated_test_failure", "same failure", POLICY)
    second = router.escalate(first, "insufficient_reasoning", "still wrong", POLICY)

    assert (first.model, first.effort) == ("sonnet", "medium")
    assert (second.model, second.effort) == ("opus", "high")
    assert not second.escalation_allowed
    with pytest.raises(router.RouterError, match="limit reached"):
        router.escalate(second, "logic_failure", "again", POLICY)


@pytest.mark.parametrize(
    "reason", ["typo", "lint", "format", "simple_import", "environment"]
)
def test_no_escalation_for_small_or_environment_problems(reason) -> None:
    decision = route("small bug", behavior_change=True)

    with pytest.raises(router.RouterError, match="does not justify escalation"):
        router.escalate(decision, reason, "ruff E501", POLICY)


def test_escalation_needs_a_known_reason_and_evidence() -> None:
    decision = route("small bug", behavior_change=True)

    with pytest.raises(router.RouterError, match="unknown escalation reason"):
        router.escalate(decision, "feels hard", "x", POLICY)
    with pytest.raises(router.RouterError, match="needs evidence"):
        router.escalate(decision, "logic_failure", " ", POLICY)


def test_the_strongest_profile_cannot_escalate_further() -> None:
    decision = route("architecture", architecture_impact="architectural")

    with pytest.raises(router.RouterError, match="strongest routed profile"):
        router.escalate(decision, "design_gap", "contract unclear", POLICY)


def test_a_decision_survives_a_json_round_trip() -> None:
    decision = router.escalate(
        route("small bug", behavior_change=True), "logic_failure", "x", POLICY
    )

    again = router.Decision.from_dict(json.loads(json.dumps(decision.to_dict())))

    assert again == decision


# Subagent files and the command line


def test_the_subagent_files_match_the_policy() -> None:
    assert router.check_agents(POLICY) == []


def test_agent_files_follow_the_planner_and_reviewer_policy() -> None:
    names = set(router.agent_specs(POLICY))

    assert {f"task-coder-{level.lower()}.md" for level in router.LEVELS} <= names
    assert "task-planner-trivial.md" not in names
    assert "task-planner-normal.md" in names
    assert "task-reviewer-normal.md" not in names
    assert "task-reviewer-complex.md" in names
    complex_coder = router.agent_specs(POLICY)["task-coder-complex.md"]
    assert "model: opus\neffort: high\n" in complex_coder


def test_sync_writes_and_check_finds_drift(tmp_path) -> None:
    assert router.check_agents(POLICY, tmp_path)
    router.sync_agents(POLICY, tmp_path)
    assert router.check_agents(POLICY, tmp_path) == []

    (tmp_path / "task-coder-trivial.md").write_text("edited", encoding="utf-8")
    (tmp_path / "task-old.md").write_text("old", encoding="utf-8")
    assert router.check_agents(POLICY, tmp_path) == [
        "outdated task-coder-trivial.md",
        "unexpected task-old.md",
    ]
    assert sorted(router.sync_agents(POLICY, tmp_path)) == [
        "task-coder-trivial.md",
        "task-old.md",
    ]
    assert router.check_agents(POLICY, tmp_path) == []


def test_the_command_line_routes_and_reports_errors(capsys) -> None:
    criteria = json.dumps(task(behavior_change=True, concurrency_risk="high"))

    assert router.main(["route", "--task", "race", "--criteria", criteria]) == 0
    out = capsys.readouterr().out
    assert "Task: race" in out and "Model: opus" in out and "Reviewer: YES" in out

    assert (
        router.main(["route", "--task", "x", "--criteria", criteria, "--model", "gpt4"])
        == 2
    )
    assert "unsupported model" in capsys.readouterr().err


def test_the_command_line_escalates(tmp_path, capsys) -> None:
    decision = route("bug", behavior_change=True)
    path = tmp_path / "decision.json"
    path.write_text(json.dumps(decision.to_dict()), encoding="utf-8")

    args = ["escalate", "--decision", str(path), "--reason", "logic_failure"]
    assert router.main([*args, "--evidence", "test fails"]) == 0
    assert json.loads(capsys.readouterr().out)["effort"] == "medium"
    assert (
        router.main(
            [
                "escalate",
                "--decision",
                str(path),
                "--reason",
                "lint",
                "--evidence",
                "E501",
            ]
        )
        == 2
    )


def test_an_ordinary_feature_with_a_small_migration_stays_on_sonnet() -> None:
    decision = route(
        "ordinary feature",
        behavior_change=True,
        files_affected=4,
        modules_affected=2,
        dependency_depth=2,
        architecture_impact="local",
        database_change=True,
        migration_required=True,
        test_complexity="medium",
    )

    assert (decision.level, decision.model) == ("NORMAL", "sonnet")
