"""Cost-aware Task/Model Router for the development workflow (tools/task_router.py).

Development infrastructure, not part of the package: the policy lives in
``.claude/task-router.json``, the rules in ``docs/TASK_ROUTER.md``. Checks
classification (unchanged since v1), the per-role execution policy, the risk
profile and Opus triggers, overrides, escalation, backward compatibility with
schema_version 1, the generated subagent files and the telemetry.
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
LEGACY = router.parse_policy(router.LEGACY_V1_POLICY)

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
# A COMPLEX task with no Opus trigger: database + domain + provider floor.
COMPLEX_PLAIN = {
    "behavior_change": True,
    "files_affected": 8,
    "modules_affected": 3,
    "database_change": True,
    "migration_required": True,
    "provider_change": "logic",
}
# A NORMAL task with no Opus trigger.
NORMAL_PLAIN = {
    "behavior_change": True,
    "files_affected": 4,
    "modules_affected": 2,
    "test_complexity": "medium",
}


def task(**changes):
    return {**BASE, **changes}


def route(name="task", override=None, profile=None, issue=None, **changes):
    return router.route(name, task(**changes), POLICY, override, profile, issue)


def roles(decision):
    return (decision.planner, decision.coder, decision.reviewer)


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
    decision = route("add a feature over a few files", **NORMAL_PLAIN)

    assert (decision.level, decision.model, decision.effort) == (
        "NORMAL",
        "sonnet",
        "medium",
    )
    assert decision.planner == "sonnet/medium" and decision.reviewer is None


def test_a_complex_task_without_a_trigger_codes_on_sonnet_with_an_opus_reviewer() -> (
    None
):
    decision = route("database + domain + provider feature", **COMPLEX_PLAIN)

    assert decision.level == "COMPLEX"
    assert roles(decision) == ("sonnet/high", "sonnet/high", "opus/high")
    assert (decision.model, decision.effort) == ("sonnet", "high")
    assert decision.triggers == ()
    assert not decision.architecture_review
    assert "Opus triggers: none: Sonnet is enough" in decision.render()


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
    assert roles(decision) == ("opus/high", "opus/high", "opus/high")
    assert decision.architecture_review
    assert decision.reasons[0] == "floor: changes the architecture"


def test_many_files_alone_do_not_mean_opus() -> None:
    decision = route("format the whole repository", files_affected=60)

    assert decision.model != "opus"


@pytest.mark.parametrize(
    ("changes", "level", "expected"),
    [
        ({}, "TRIVIAL", (None, "haiku/low", None)),
        ({"behavior_change": True}, "SIMPLE", (None, "sonnet/low", None)),
        (NORMAL_PLAIN, "NORMAL", ("sonnet/medium", "sonnet/medium", None)),
        (COMPLEX_PLAIN, "COMPLEX", ("sonnet/high", "sonnet/high", "opus/high")),
        (
            {"architecture_impact": "architectural"},
            "ARCHITECTURAL",
            ("opus/high", "opus/high", "opus/high"),
        ),
    ],
)
def test_each_level_routes_each_role_per_the_policy(changes, level, expected) -> None:
    decision = route(**changes)

    assert decision.level == level
    assert roles(decision) == expected


# Floors for specific kinds of task


def test_a_migration_task_is_at_least_normal() -> None:
    decision = route("add a column", behavior_change=True, migration_required=True)

    assert decision.level == "NORMAL"
    assert "floor: needs a migration" in decision.reasons


def test_a_provider_logic_task_is_normal_and_a_contract_change_architectural() -> None:
    assert route("provider retry", provider_change="logic").level == "NORMAL"
    contract = route("change TextGenerator", provider_change="contract")
    assert contract.level == "ARCHITECTURAL" and contract.model == "opus"


def test_a_concurrency_task_is_complex_and_triggers_opus() -> None:
    decision = route(
        "debug race condition in Research Failure Recovery",
        behavior_change=True,
        concurrency_risk="high",
    )

    assert (decision.level, decision.model) == ("COMPLEX", "opus")
    assert "floor: high concurrency risk" in decision.reasons
    assert [t["name"] for t in decision.triggers] == ["reasoning_risk"]


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


# Risk profile


@pytest.mark.parametrize(
    ("changes", "field", "value"),
    [
        ({"architecture_impact": "none"}, "architecture_risk", "low"),
        ({"architecture_impact": "local"}, "architecture_risk", "low"),
        ({"architecture_impact": "cross_module"}, "architecture_risk", "medium"),
        ({"architecture_impact": "architectural"}, "architecture_risk", "high"),
        ({"provider_change": "none"}, "provider_risk", "low"),
        ({"provider_change": "logic"}, "provider_risk", "medium"),
        ({"provider_change": "contract"}, "provider_risk", "high"),
        ({}, "migration_risk", "low"),
        ({"migration_required": True}, "migration_risk", "medium"),
        (
            {"migration_required": True, "rollback_risk": "high"},
            "migration_risk",
            "high",
        ),
        ({"rollback_risk": "high"}, "migration_risk", "low"),
        ({}, "regression_risk", "low"),
        ({"behavior_change": True}, "regression_risk", "medium"),
        ({"backward_compatibility_risk": "low"}, "regression_risk", "medium"),
        ({"backward_compatibility_risk": "high"}, "regression_risk", "high"),
        ({}, "reasoning_risk", "low"),
        ({"concurrency_risk": "low"}, "reasoning_risk", "medium"),
        ({"failure_recovery_complexity": "low"}, "reasoning_risk", "medium"),
        ({"concurrency_risk": "high"}, "reasoning_risk", "high"),
        ({"failure_recovery_complexity": "high"}, "reasoning_risk", "high"),
        ({"uncertainty": "high"}, "uncertainty", "high"),
        ({"files_affected": 7}, "estimated_files", 7),
        ({"modules_affected": 3}, "estimated_modules", 3),
        ({"test_complexity": "medium"}, "estimated_test_complexity", "medium"),
    ],
)
def test_a_missing_risk_field_is_derived_from_the_criteria(
    changes, field, value
) -> None:
    risk, derived = router.risk_profile(task(**changes))

    assert risk[field] == value
    assert field in derived


def test_given_risk_fields_win_and_are_not_marked_derived() -> None:
    decision = route(profile={"regression_risk": "low", "estimated_files": 30})

    assert decision.risk_profile["regression_risk"] == "low"
    assert decision.risk_profile["estimated_files"] == 30
    assert "regression_risk" not in decision.derived
    assert "reasoning_risk" in decision.derived
    rendered = decision.render()
    assert "regression_risk=low," in rendered and "reasoning_risk=low*" in rendered


@pytest.mark.parametrize(
    "bad",
    [
        {"regression": "high"},
        {"reasoning_risk": "extreme"},
        {"estimated_files": -1},
        {"estimated_modules": True},
        {"uncertainty": "none"},
    ],
)
def test_an_invalid_risk_profile_is_refused(bad) -> None:
    with pytest.raises(router.RouterError):
        route(profile=bad)


# Opus triggers


@pytest.mark.parametrize(
    ("changes", "profile", "issue", "name", "evidence"),
    [
        (
            {},
            {"architecture_risk": "high"},
            None,
            "architecture_risk",
            "architecture_risk=high, an unclear architecture decision "
            "(given in the risk profile)",
        ),
        (
            {"architecture_impact": "cross_module"},
            None,
            None,
            "interdependent_modules",
            "architecture_impact=cross_module",
        ),
        (
            {"dependency_depth": 3},
            {"estimated_modules": 4},
            None,
            "interdependent_modules",
            "estimated_modules=4 with dependency_depth=3",
        ),
        (
            {"failure_recovery_complexity": "high"},
            None,
            None,
            "reasoning_risk",
            "derived from concurrency_risk=none, failure_recovery_complexity=high",
        ),
        (
            {"rollback_risk": "high"},
            None,
            None,
            "migration_risk",
            "derived from migration_required=True, rollback_risk=high",
        ),
        (
            {},
            {"provider_risk": "high"},
            None,
            "provider_risk",
            "provider_risk=high, a provider contract change",
        ),
        (
            {},
            None,
            "reviewer: retry loop can double-charge the budget",
            "prior_review_issue",
            "retry loop can double-charge the budget",
        ),
        (
            {},
            {"uncertainty": "high", "architecture_risk": "medium"},
            None,
            "uncertainty_with_architecture",
            "uncertainty=high with architecture_risk=medium",
        ),
    ],
)
def test_each_opus_trigger_lifts_planner_and_coder_with_evidence(
    changes, profile, issue, name, evidence
) -> None:
    decision = route(profile=profile, issue=issue, **{**COMPLEX_PLAIN, **changes})

    assert decision.level == "COMPLEX"
    assert roles(decision) == ("opus/high", "opus/high", "opus/high")
    assert name in [t["name"] for t in decision.triggers]
    found = next(t for t in decision.triggers if t["name"] == name)
    assert evidence in found["evidence"]
    assert f"- {name}: " in decision.render()


def test_a_trigger_at_normal_lifts_to_opus_medium_and_adds_an_opus_reviewer() -> None:
    decision = route(issue="design gap in the plan", **NORMAL_PLAIN)

    assert decision.level == "NORMAL"
    assert roles(decision) == ("opus/medium", "opus/medium", "opus/high")


@pytest.mark.parametrize(("depth", "modules"), [(2, 4), (3, 3)])
def test_few_modules_or_shallow_dependencies_are_no_trigger(depth, modules) -> None:
    decision = route(
        profile={"estimated_modules": modules},
        **{**COMPLEX_PLAIN, "dependency_depth": depth},
    )

    assert decision.triggers == () and decision.coder == "sonnet/high"


@pytest.mark.parametrize("base", [NORMAL_PLAIN, COMPLEX_PLAIN])
def test_high_uncertainty_alone_stays_on_sonnet_and_is_marked(base) -> None:
    plain = route(**base)
    decision = route(**{**base, "uncertainty": "high"})

    assert decision.triggers == ()
    assert roles(decision) == roles(plain)
    assert decision.model == "sonnet" and decision.uncertain


def test_a_trigger_is_not_used_below_normal_or_at_architectural() -> None:
    simple = route(issue="reviewer found a gap", behavior_change=True)
    architectural = route(issue="gap", architecture_impact="architectural")

    assert simple.coder == "sonnet/low" and simple.triggers == ()
    assert "not an Opus trigger at SIMPLE" in simple.adjustments[0]
    assert architectural.triggers == () and architectural.model == "opus"


def test_an_empty_prior_review_issue_is_refused() -> None:
    with pytest.raises(router.RouterError, match="needs evidence"):
        route(issue="  ")


# Risk without architecture impact


def test_high_regression_risk_at_normal_raises_effort_and_adds_a_sonnet_reviewer() -> (
    None
):
    decision = route(profile={"regression_risk": "high"}, **NORMAL_PLAIN)

    assert decision.level == "NORMAL" and decision.triggers == ()
    assert roles(decision) == ("sonnet/medium", "sonnet/high", "sonnet/high")
    assert any("sonnet/medium -> sonnet/high" in a for a in decision.adjustments)


def test_high_risk_at_simple_raises_effort_on_the_same_model() -> None:
    regression = route(profile={"regression_risk": "high"}, behavior_change=True)
    migration = route(profile={"migration_risk": "high"}, behavior_change=True)

    assert roles(regression) == (None, "sonnet/medium", None)
    assert roles(migration) == (None, "sonnet/medium", None)


def test_the_normal_reviewer_needs_a_behaviour_change_and_a_high_risk() -> None:
    no_change = route(
        profile={"regression_risk": "high"},
        **{**NORMAL_PLAIN, "behavior_change": False},
    )
    medium = route(**NORMAL_PLAIN)

    assert no_change.level == "NORMAL" and no_change.reviewer is None
    assert no_change.coder == "sonnet/high"
    assert medium.risk_profile["regression_risk"] == "medium"
    assert medium.reviewer is None and medium.coder == "sonnet/medium"


def test_a_trigger_replaces_the_normal_risk_reviewer_without_its_note() -> None:
    decision = route(
        **{**NORMAL_PLAIN, "migration_required": True, "rollback_risk": "high"}
    )

    assert decision.level == "NORMAL"
    assert [t["name"] for t in decision.triggers] == ["migration_risk"]
    assert roles(decision) == ("opus/medium", "opus/medium", "opus/high")
    assert not any("reviewer on at" in a for a in decision.adjustments)
    assert "reviewer on at" not in decision.render()


def test_the_effort_bump_note_names_the_actual_risks() -> None:
    decision = route(profile={"regression_risk": "high"}, **NORMAL_PLAIN)

    assert (
        "coder sonnet/medium -> sonnet/high (same model, more effort): "
        "regression_risk=high with architecture_risk=low" in decision.adjustments
    )
    assert not any("without architecture impact" in a for a in decision.adjustments)


@pytest.mark.parametrize(
    ("profile", "coder"),
    [
        ({"architecture_risk": "high"}, "sonnet/low"),
        ({"architecture_risk": "high", "regression_risk": "high"}, "sonnet/medium"),
    ],
)
def test_high_architecture_risk_at_simple_asks_to_re_measure_or_re_route(
    profile, coder
) -> None:
    decision = route(profile=profile, behavior_change=True)

    assert decision.level == "SIMPLE" and decision.triggers == ()
    assert decision.coder == coder
    note = next(a for a in decision.adjustments if a.startswith("architecture_risk"))
    assert "not an Opus trigger at SIMPLE" in note
    assert "re-measure the criteria or re-route" in note
    assert "(given in the risk profile)" in note
    bumps = [a for a in decision.adjustments if a.startswith("coder ")]
    assert all("with architecture_risk=high" in a for a in bumps)


def test_architecture_risk_at_architectural_adds_no_re_route_note() -> None:
    decision = route(architecture_impact="architectural")

    assert decision.risk_profile["architecture_risk"] == "high"
    assert decision.adjustments == ()


def test_effort_is_chosen_independently_of_the_model() -> None:
    normal = route(**NORMAL_PLAIN)
    complex_ = route(**COMPLEX_PLAIN)
    normal_opus = route(issue="gap", **NORMAL_PLAIN)

    assert (normal.model, complex_.model) == ("sonnet", "sonnet")
    assert (normal.effort, complex_.effort) == ("medium", "high")
    assert (normal_opus.model, normal_opus.effort) == ("opus", "medium")


# Policy


def test_the_policy_never_routes_to_xhigh_or_max() -> None:
    every = [
        profile
        for profiles in router.agent_profiles(POLICY).values()
        for profile in profiles
    ]
    assert all(
        router.split_profile(profile)[1] not in router.OVERRIDE_ONLY_EFFORTS
        for profile in every
    )
    assert set(every) <= set(router.ALLOWED_PROFILES)
    data = copy.deepcopy(RAW)
    data["levels"]["ARCHITECTURAL"]["coder"] = "opus/xhigh"
    with pytest.raises(router.RouterError, match="user override"):
        router.parse_policy(data)


def test_the_policy_can_be_changed_in_one_place() -> None:
    data = copy.deepcopy(RAW)
    data["levels"]["TRIVIAL"]["coder"] = "sonnet/low"
    data["escalation"]["ladder"].remove("haiku/low")
    policy = router.parse_policy(data)

    decision = router.route("typo", task(), policy)

    assert decision.model == "sonnet"
    specs = router.agent_specs(policy)
    assert "task-coder-haiku-low.md" not in specs
    assert "model: sonnet" in specs["task-coder-sonnet-low.md"]


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("levels", "COMPLEX", "coder"), "gpt4/high"),
        (("levels", "SIMPLE", "coder"), "sonnet/extreme"),
        (("levels", "SIMPLE", "coder"), None),
        (("levels", "NORMAL", "planner"), "yes"),
        (("levels", "NORMAL", "risk_effort_bump"), "yes"),
        (("levels", "COMPLEX", "reviewer"), "opus/low"),
        (("levels", "TRIVIAL", "coder"), "haiku/medium"),
        (("levels", "NORMAL", "opus_trigger_profile"), "fable/high"),
        (("levels", "ARCHITECTURAL", "reviewer"), None),
        (("opus_reviewer",), "opus/max"),
        (("escalation", "max_steps"), 9),
        (("escalation", "ladder"), []),
        (("escalation", "ladder"), ["sonnet/low", "haiku/low", "opus/high"]),
        (("escalation", "ladder"), ["haiku/low", "sonnet/low", "sonnet/low"]),
        (("escalation", "ladder"), ["haiku/low", "sonnet/low", "opus/high"]),
        (("escalation", "non_escalating_reasons"), ["logic_failure"]),
        (("schema_version",), 3),
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
        None,
    )
    assert decision.level == "TRIVIAL"
    assert decision.overrides == ("coder=opus/xhigh", "planner=off")
    assert "User override" in decision.render()
    assert decision.no_agent_file == ("coder",)
    assert (
        "Coder: opus/xhigh (no agent file: use the fail-safe in docs/TASK_ROUTER.md)"
        in decision.render()
    )


@pytest.mark.parametrize(
    ("override", "changes", "role", "profile"),
    [
        (
            router.Override(effort="medium", role="reviewer"),
            COMPLEX_PLAIN,
            "reviewer",
            "opus/medium",
        ),
        (
            router.Override(model="opus", role="reviewer"),
            NORMAL_PLAIN,
            "reviewer",
            "opus/medium",
        ),
        (router.Override(model="sonnet", role="planner"), {}, "planner", "sonnet/low"),
    ],
)
def test_an_override_profile_without_an_agent_file_names_the_fail_safe(
    override, changes, role, profile
) -> None:
    decision = route(override=override, **changes)

    assert getattr(decision, role) == profile
    assert decision.no_agent_file == (role,)
    rendered = decision.render()
    assert (
        f"{role.capitalize()}: {profile} (no agent file: use the fail-safe in "
        "docs/TASK_ROUTER.md)" in rendered
    )
    assert router.agent_name(role, profile) not in rendered
    assert f"{router.agent_name(role, profile)}.md" not in router.agent_specs(POLICY)


def test_routed_profiles_with_agent_files_name_their_agent() -> None:
    decision = route(
        override=router.Override(model="sonnet", role="reviewer"), **COMPLEX_PLAIN
    )

    assert decision.no_agent_file == ()
    assert "Reviewer: sonnet/high (task-reviewer-sonnet-high)" in decision.render()


def test_an_override_to_an_unsupported_model_or_effort_is_refused() -> None:
    with pytest.raises(router.RouterError, match="unsupported model"):
        route(override=router.Override(model="gpt-4"))
    with pytest.raises(router.RouterError, match="unsupported effort"):
        route(override=router.Override(effort="ultra"))


def test_an_override_can_target_one_role() -> None:
    override = router.Override(model="sonnet", role="reviewer")

    decision = route(override=override, **COMPLEX_PLAIN)

    assert roles(decision) == ("sonnet/high", "sonnet/high", "sonnet/high")
    assert decision.overrides == ("reviewer=sonnet/high",)


def test_an_override_can_use_max_effort_for_the_planner() -> None:
    override = router.Override(effort="max", role="planner")

    decision = route(override=override, **NORMAL_PLAIN)

    assert decision.planner == "sonnet/max" and decision.coder == "sonnet/medium"


def test_skipping_the_reviewer_at_architectural_is_allowed_and_recorded() -> None:
    decision = route(
        override=router.Override(reviewer=False),
        architecture_impact="architectural",
    )

    assert decision.reviewer is None and not decision.architecture_review
    assert decision.overrides == (
        "reviewer=off",
        "architecture review skipped at ARCHITECTURAL",
    )


def test_an_override_with_an_unknown_role_or_a_conflict_is_refused() -> None:
    with pytest.raises(router.RouterError, match="unknown role"):
        route(override=router.Override(model="opus", role="tester"))
    with pytest.raises(router.RouterError, match="both set and skip"):
        route(override=router.Override(model="opus", role="reviewer", reviewer=False))


# Escalation


def test_escalation_moves_one_step_and_records_why() -> None:
    decision = route("normal feature", behavior_change=True, migration_required=True)

    up = router.escalate(decision, "logic_failure", "test_x fails: wrong total", POLICY)

    assert (up.model, up.effort, up.steps) == ("sonnet", "high", 1)
    assert up.planner == "sonnet/medium"
    assert up.history == (
        {
            "role": "coder",
            "current_model": "sonnet",
            "current_effort": "medium",
            "reason": "logic_failure",
            "evidence": "test_x fails: wrong total",
            "next_model": "sonnet",
            "next_effort": "high",
        },
    )


def test_escalation_goes_low_medium_high_and_then_stops() -> None:
    decision = route("small bug", behavior_change=True)
    assert (decision.model, decision.effort) == ("sonnet", "low")

    first = router.escalate(decision, "repeated_test_failure", "same failure", POLICY)
    second = router.escalate(first, "insufficient_reasoning", "still wrong", POLICY)

    assert (first.model, first.effort) == ("sonnet", "medium")
    assert (second.model, second.effort) == ("sonnet", "high")
    assert not second.escalation_allowed
    with pytest.raises(router.RouterError, match="limit reached"):
        router.escalate(second, "logic_failure", "again", POLICY)


def test_the_policy_ladder_is_the_six_allowed_profiles() -> None:
    assert POLICY.ladder == router.ALLOWED_PROFILES
    assert POLICY.ladder[-1] == "opus/high"


def test_reaching_opus_turns_the_reviewer_on_at_opus_high() -> None:
    decision = route(**NORMAL_PLAIN)
    first = router.escalate(decision, "logic_failure", "wrong totals", POLICY)

    second = router.escalate(first, "design_gap", "plan misses retries", POLICY)

    assert first.reviewer is None
    assert (second.coder, second.reviewer) == ("opus/medium", "opus/high")
    assert second.planner == "sonnet/medium"


def test_reaching_opus_keeps_a_reviewer_the_user_skipped_off() -> None:
    decision = route(override=router.Override(reviewer=False), **COMPLEX_PLAIN)

    up = router.escalate(decision, "logic_failure", "x", POLICY)

    assert up.coder == "opus/medium" and up.reviewer is None


def test_escalation_never_goes_above_opus_high() -> None:
    decision = route(issue="gap", **NORMAL_PLAIN)
    up = router.escalate(decision, "logic_failure", "x", POLICY)

    assert up.coder == "opus/high"
    with pytest.raises(router.RouterError, match="strongest routed profile"):
        router.escalate(up, "logic_failure", "y", POLICY)


def test_an_off_ladder_override_profile_escalates_by_its_model() -> None:
    decision = route(override=router.Override(effort="xhigh"), **COMPLEX_PLAIN)

    up = router.escalate(decision, "logic_failure", "x", POLICY)

    assert decision.coder == "sonnet/xhigh"
    assert up.coder == "opus/medium"


@pytest.mark.parametrize(
    "reason",
    [
        "typo",
        "lint",
        "format",
        "simple_import",
        "type",
        "test_fixture",
        "environment",
        "flaky_unrelated",
    ],
)
def test_no_escalation_for_small_or_environment_problems(reason) -> None:
    decision = route("small bug", behavior_change=True)

    with pytest.raises(router.RouterError, match="does not justify escalation"):
        router.escalate(decision, reason, "ruff E501", POLICY)


@pytest.mark.parametrize("reason", sorted(router.NEVER_ESCALATE))
def test_a_policy_that_escalates_a_never_escalate_reason_is_refused(reason) -> None:
    data = copy.deepcopy(RAW)
    data["escalation"]["escalating_reasons"].append(reason)
    data["escalation"]["non_escalating_reasons"].remove(reason)

    with pytest.raises(router.RouterError, match="must never escalate"):
        router.parse_policy(data)


@pytest.mark.parametrize("reason", sorted(router.NEVER_ESCALATE))
def test_never_escalate_reasons_are_refused_even_if_the_policy_omits_them(
    reason,
) -> None:
    data = copy.deepcopy(RAW)
    data["escalation"]["non_escalating_reasons"] = []
    policy = router.parse_policy(data)
    decision = router.route("bug", task(behavior_change=True), policy)

    with pytest.raises(router.RouterError, match="does not justify escalation"):
        router.escalate(decision, reason, "ruff E501", policy)


def test_the_never_escalate_set_is_the_documented_one() -> None:
    documented = {
        "typo",
        "lint",
        "format",
        "simple_import",
        "type",
        "test_fixture",
        "environment",
        "flaky_unrelated",
    }

    assert documented == router.NEVER_ESCALATE


@pytest.mark.parametrize(("steps", "ok"), [(0, True), (2, True), (3, False)])
def test_max_steps_is_capped_at_two_in_code(steps, ok) -> None:
    data = copy.deepcopy(RAW)
    data["escalation"]["max_steps"] = steps

    if ok:
        assert router.parse_policy(data).max_steps == steps
    else:
        with pytest.raises(router.RouterError, match="from 0 to 2"):
            router.parse_policy(data)


def test_escalation_needs_a_known_reason_and_evidence() -> None:
    decision = route("small bug", behavior_change=True)

    with pytest.raises(router.RouterError, match="unknown escalation reason"):
        router.escalate(decision, "feels hard", "x", POLICY)
    with pytest.raises(router.RouterError, match="needs evidence"):
        router.escalate(decision, "logic_failure", " ", POLICY)


@pytest.mark.parametrize(
    "decision",
    [
        route(architecture_impact="architectural"),
        route(issue="gap", **COMPLEX_PLAIN),
        route(override=router.Override(model="opus", effort="max"), **COMPLEX_PLAIN),
    ],
)
def test_escalation_is_not_allowed_at_the_top_of_the_ladder(decision) -> None:
    assert decision.steps == 0 and not decision.escalation_allowed
    assert "Escalation: Not allowed (0 used; the coder is at the top" in (
        decision.render()
    )
    assert "stop and ask the user" in decision.render()


def test_escalation_is_not_allowed_when_the_policy_allows_no_steps() -> None:
    data = copy.deepcopy(RAW)
    data["escalation"]["max_steps"] = 0
    policy = router.parse_policy(data)

    decision = router.route("bug", task(behavior_change=True), policy)

    assert decision.coder == "sonnet/low" and not decision.escalation_allowed


def test_escalation_stays_allowed_below_the_top_with_steps_left() -> None:
    decision = route(issue="gap", **NORMAL_PLAIN)

    assert decision.coder == "opus/medium" and decision.escalation_allowed
    assert "Escalation: Allowed (0 used)" in decision.render()


def test_the_strongest_profile_cannot_escalate_further() -> None:
    decision = route("architecture", architecture_impact="architectural")

    with pytest.raises(router.RouterError, match="strongest routed profile"):
        router.escalate(decision, "design_gap", "contract unclear", POLICY)


def test_a_decision_survives_a_json_round_trip() -> None:
    decision = router.escalate(
        route("small bug", issue=None, behavior_change=True),
        "logic_failure",
        "x",
        POLICY,
    )
    triggered = route(issue="gap", **COMPLEX_PLAIN)

    for item in (decision, triggered):
        again = router.Decision.from_dict(json.loads(json.dumps(item.to_dict())))
        assert again == item


# Backward compatibility with schema_version 1


V1_DECISION = {
    "task": "bug",
    "level": "SIMPLE",
    "model": "sonnet",
    "effort": "low",
    "planner": False,
    "reviewer": False,
    "architecture_review": False,
    "reasons": ["floor: changes behaviour"],
    "uncertain": False,
    "unknown": [],
    "escalation_allowed": True,
    "steps": 0,
    "overrides": [],
    "history": [],
}


def test_a_v1_policy_file_still_loads(tmp_path) -> None:
    path = tmp_path / "policy.json"
    path.write_text(json.dumps(router.LEGACY_V1_POLICY), encoding="utf-8")

    policy = router.load_policy(path)
    decision = router.route("x", task(**COMPLEX_PLAIN), policy)

    assert policy.schema_version == 1
    assert policy.ladder == ("haiku/low", "sonnet/low", "sonnet/medium", "opus/high")
    assert roles(decision) == ("opus/high", "opus/high", "opus/high")
    assert roles(router.route("x", task(**NORMAL_PLAIN), policy)) == (
        "sonnet/medium",
        "sonnet/medium",
        None,
    )


def test_a_v1_decision_json_loads_and_escalates() -> None:
    decision = router.Decision.from_dict(V1_DECISION)

    up = router.escalate(decision, "logic_failure", "test fails", POLICY)

    assert roles(decision) == (None, "sonnet/low", None)
    assert (up.model, up.effort, up.steps) == ("sonnet", "medium", 1)
    assert router.Decision.from_dict({**V1_DECISION, "planner": True}).planner == (
        "sonnet/low"
    )


@pytest.mark.parametrize("note", ["reviewer=False", "reviewer=off"])
def test_a_v1_decision_with_a_skipped_reviewer_keeps_it_off_at_opus(note) -> None:
    data = {
        **V1_DECISION,
        "level": "COMPLEX",
        "effort": "high",
        "overrides": [note],
    }

    decision = router.Decision.from_dict(data)
    up = router.escalate(decision, "logic_failure", "wrong totals", POLICY)

    assert decision.reviewer_skipped
    assert (up.coder, up.reviewer) == ("opus/medium", None)


def test_a_v1_decision_without_a_skip_gets_the_opus_reviewer() -> None:
    data = {**V1_DECISION, "level": "COMPLEX", "effort": "high"}

    decision = router.Decision.from_dict(data)
    up = router.escalate(decision, "logic_failure", "wrong totals", POLICY)

    assert not decision.reviewer_skipped
    assert (up.coder, up.reviewer) == ("opus/medium", "opus/high")


def test_skipping_the_reviewer_is_a_structured_field() -> None:
    skipped = route(override=router.Override(reviewer=False), **COMPLEX_PLAIN)
    chosen = route(override=router.Override(model="sonnet", role="reviewer"))

    assert skipped.reviewer_skipped and skipped.to_dict()["reviewer_skipped"]
    assert not chosen.reviewer_skipped
    again = router.Decision.from_dict(json.loads(json.dumps(skipped.to_dict())))
    assert again.reviewer_skipped
    # The structured field decides, not the text of the override notes.
    unskipped = router.Decision.from_dict(
        {**skipped.to_dict(), "reviewer_skipped": False}
    )
    assert router.escalate(unskipped, "logic_failure", "x", POLICY).reviewer == (
        "opus/high"
    )


def test_a_v2_decision_keeps_top_level_model_and_effort() -> None:
    data = route(**COMPLEX_PLAIN).to_dict()

    assert (data["schema_version"], data["model"], data["effort"]) == (
        2,
        "sonnet",
        "high",
    )
    assert data["coder"] == "sonnet/high" and data["reviewer"] == "opus/high"


def test_a_broken_decision_json_is_refused() -> None:
    with pytest.raises(router.RouterError, match="not a routing decision"):
        router.Decision.from_dict({"task": "x"})


def test_old_command_line_flags_still_work(capsys) -> None:
    criteria = json.dumps(task(behavior_change=True))
    args = ["route", "--task", "x", "--criteria", criteria, "--model", "opus"]

    assert (
        router.main([*args, "--effort", "high", "--skip-planner", "--skip-reviewer"])
        == 0
    )
    assert "Model: opus" in capsys.readouterr().out
    assert router.main([*args, "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert (data["model"], data["effort"]) == ("opus", "low")


# Subagent files


def test_the_subagent_files_match_the_policy() -> None:
    assert router.check_agents(POLICY) == []


def test_agent_files_cover_exactly_the_profiles_the_policy_can_produce() -> None:
    names = set(router.agent_specs(POLICY))

    assert names == {
        "task-planner-sonnet-medium.md",
        "task-planner-sonnet-high.md",
        "task-planner-opus-medium.md",
        "task-planner-opus-high.md",
        *(
            f"task-coder-{profile.replace('/', '-')}.md"
            for profile in router.ALLOWED_PROFILES
        ),
        "task-reviewer-sonnet-high.md",
        "task-reviewer-opus-high.md",
    }
    coder = router.agent_specs(POLICY)["task-coder-sonnet-high.md"]
    assert "model: sonnet\neffort: high\n" in coder
    reviewer = router.agent_specs(POLICY)["task-reviewer-opus-high.md"]
    assert "Do not re-run the full gate" in reviewer
    assert "Architecture review, only when the dispatch prompt asks" in reviewer


def test_every_reviewer_still_checks_finished_tasks_and_test_coverage() -> None:
    specs = router.agent_specs(POLICY)
    reviewers = [text for name, text in specs.items() if "-reviewer-" in name]

    assert reviewers
    for text in reviewers:
        assert "that finished tasks keep their behaviour" in text
        assert "tests cover the\nnew rules" in text


def test_every_coder_still_forbids_deleting_tests_or_lowering_coverage() -> None:
    specs = router.agent_specs(POLICY)
    coders = [text for name, text in specs.items() if "-coder-" in name]

    assert len(coders) == len(router.ALLOWED_PROFILES)
    for text in coders:
        assert "Never edit an applied migration, delete tests or lower coverage." in (
            text
        )


def flat(text: str) -> str:
    return " ".join(text.split())


def test_every_coder_states_the_smart_test_rules_and_the_tests_block() -> None:
    specs = router.agent_specs(POLICY)
    coders = [flat(text) for name, text in specs.items() if "-coder-" in name]

    assert len(coders) == len(router.ALLOWED_PROFILES)
    for text in coders:
        assert "run the single failing test id first" in text
        assert "Do not run the full suite" in text
        assert "unless the main session's brief explicitly asks for it" in text
        assert "the main session owns the one justified full suite" in text
        assert "run it at most once and never after every fix" in text
        assert 'the full suite is "NOT RUN (main session owns it)"' in text
        assert "the full suite only if the brief asks for it" in text
        assert "Do not re-run passing tests" in text
        assert "must not run the migration chain" in text
        assert "`database` template fixture" in text
        assert "weaken an assertion" in text
        assert (
            "Tests block: new tests, affected tests, regression, full suite, "
            "migration chain" in text
        )
        assert "RUN (with the count) or NOT RUN (with the reason)" in text
        assert "same test rules" in text


def test_every_planner_lists_the_affected_tests_to_run() -> None:
    specs = router.agent_specs(POLICY)
    planners = [flat(text) for name, text in specs.items() if "-planner-" in name]

    assert planners
    for text in planners:
        assert "5. The affected tests to run" in text
        assert "whether a full suite run or the migration chain" in text


def test_every_reviewer_runs_targeted_tests_only() -> None:
    specs = router.agent_specs(POLICY)
    reviewers = [flat(text) for name, text in specs.items() if "-reviewer-" in name]

    assert reviewers
    for text in reviewers:
        assert "Run targeted tests only" in text
        assert "never the full suite or the migration chain" in text


def test_every_agent_never_runs_git_commands_that_change_state() -> None:
    specs = router.agent_specs(POLICY)

    assert len(specs) == sum(len(p) for p in router.agent_profiles(POLICY).values())
    for text in specs.values():
        assert (
            "Never run git commands that change the working tree, index, branches "
            "or stashes (stash, checkout, reset, restore, clean, commit, push); "
            "read-only git (status, diff, log, show) only."
        ) in flat(text)


def test_every_agent_reads_only_the_short_state_sections() -> None:
    for content in router.agent_specs(POLICY).values():
        assert '"Current state", "Next task" and "Invariants"' in content


def test_the_old_level_named_agent_files_are_gone() -> None:
    names = {path.name for path in router.AGENTS_DIR.glob("task-*.md")}

    for level in router.LEVELS:
        for role in router.ROLES:
            assert f"task-{role}-{level.lower()}.md" not in names


def test_sync_writes_and_check_finds_drift(tmp_path) -> None:
    assert router.check_agents(POLICY, tmp_path)
    router.sync_agents(POLICY, tmp_path)
    assert router.check_agents(POLICY, tmp_path) == []

    (tmp_path / "task-coder-haiku-low.md").write_text("edited", encoding="utf-8")
    (tmp_path / "task-coder-trivial.md").write_text("old", encoding="utf-8")
    assert router.check_agents(POLICY, tmp_path) == [
        "outdated task-coder-haiku-low.md",
        "unexpected task-coder-trivial.md",
    ]
    assert sorted(router.sync_agents(POLICY, tmp_path)) == [
        "task-coder-haiku-low.md",
        "task-coder-trivial.md",
    ]
    assert router.check_agents(POLICY, tmp_path) == []


# Command line


def test_the_command_line_routes_and_reports_errors(capsys) -> None:
    criteria = json.dumps(task(behavior_change=True, concurrency_risk="high"))

    assert router.main(["route", "--task", "race", "--criteria", criteria]) == 0
    out = capsys.readouterr().out
    assert "Task: race" in out and "Model: opus" in out
    assert "Reviewer: opus/high (task-reviewer-opus-high)" in out
    assert "- reasoning_risk: " in out

    assert (
        router.main(["route", "--task", "x", "--criteria", criteria, "--model", "gpt4"])
        == 2
    )
    assert "unsupported model" in capsys.readouterr().err


def test_the_command_line_takes_a_profile_a_role_and_a_prior_review_issue(
    tmp_path, capsys
) -> None:
    profile = tmp_path / "risk.json"
    profile.write_text(json.dumps({"regression_risk": "high"}), encoding="utf-8")
    criteria = json.dumps(task(**NORMAL_PLAIN))
    args = ["route", "--task", "x", "--criteria", criteria, "--json"]

    assert router.main([*args, "--profile", str(profile)]) == 0
    assert json.loads(capsys.readouterr().out)["coder"] == "sonnet/high"
    assert router.main([*args, "--prior-review-issue", "race in retry"]) == 0
    assert json.loads(capsys.readouterr().out)["coder"] == "opus/medium"
    assert router.main([*args, "--role", "reviewer", "--model", "opus"]) == 0
    assert json.loads(capsys.readouterr().out)["reviewer"] == "opus/medium"


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


# Telemetry


def agent_run(**changes):
    return {
        "kind": "agent_run",
        "task": "F-070",
        "role": "coder",
        "profile": "sonnet/high",
        "tokens": 1000,
        "tool_uses": 3,
        "duration_ms": 5000,
        "outcome": "completed",
        **changes,
    }


VALID_EVENTS = [
    agent_run(),
    agent_run(role="coder-fix", profile="opus/max"),
    {
        "kind": "escalation",
        "task": "F-070",
        "role": "coder",
        "current_model": "sonnet",
        "current_effort": "high",
        "reason": "logic_failure",
        "evidence": "test_x",
        "next_model": "opus",
        "next_effort": "medium",
    },
    {
        "kind": "gate",
        "task": "F-070",
        "tests_passed": 10,
        "tests_failed": 1,
        "ruff_check": "pass",
        "ruff_format": "fail",
        "build": "pass",
    },
    {
        "kind": "result",
        "task": "F-070",
        "result": "pass",
        "tests": 10,
        "findings": {"blocking": 0, "should_fix": 2, "minor": 1},
    },
]


@pytest.mark.parametrize("event", VALID_EVENTS)
def test_a_valid_event_is_recorded_with_a_time(tmp_path, event) -> None:
    path = tmp_path / "telemetry.jsonl"

    stored = router.record_event(event, POLICY, path)

    assert stored["at"] and stored["kind"] == event["kind"]
    assert router.read_events(path) == [stored]


@pytest.mark.parametrize(
    ("event", "message"),
    [
        ({"kind": "chat", "task": "x"}, "unknown event kind"),
        ({**agent_run(), "prompt": "raw text"}, "unknown fields"),
        ({k: v for k, v in agent_run().items() if k != "tokens"}, "missing fields"),
        (agent_run(tokens=-1), "invalid value"),
        (agent_run(role="tester"), "invalid value"),
        (agent_run(profile="gpt4/high"), "invalid value"),
        (agent_run(outcome="great"), "invalid value"),
        (agent_run(task=" "), "invalid value"),
        (agent_run(backfilled="yes"), "true or false"),
        (agent_run(at="yesterday"), "ISO date-time"),
        ({**VALID_EVENTS[2], "reason": "lint"}, "invalid value"),
        ({**VALID_EVENTS[3], "build": "ok"}, "invalid value"),
        ({**VALID_EVENTS[4], "findings": {"critical": 1}}, "invalid value"),
        (
            {
                "kind": "route",
                "task": "x",
                "policy_version": 2,
                "criteria": {"files": 1},
                "decision": route().to_dict(),
            },
            "invalid value",
        ),
        (
            {
                "kind": "route",
                "task": "x",
                "policy_version": 2,
                "criteria": task(),
                "decision": {"task": "x"},
            },
            "invalid value",
        ),
    ],
)
def test_an_invalid_event_is_refused(tmp_path, event, message) -> None:
    path = tmp_path / "telemetry.jsonl"

    with pytest.raises(router.RouterError, match=message):
        router.record_event(event, POLICY, path)
    assert not path.exists()


def test_telemetry_is_append_only(tmp_path) -> None:
    path = tmp_path / "telemetry.jsonl"
    router.record_event(VALID_EVENTS[0], POLICY, path)
    before = path.read_bytes()

    router.record_event(VALID_EVENTS[3], POLICY, path)

    after = path.read_bytes()
    assert after.startswith(before) and len(after.splitlines()) == 2


def test_route_and_escalate_record_events_from_the_command_line(
    tmp_path, capsys
) -> None:
    telemetry = tmp_path / "telemetry.jsonl"
    criteria = json.dumps(task(**COMPLEX_PLAIN))
    common = ["--telemetry", str(telemetry)]

    assert (
        router.main(
            [*common, "route", "--task", "F-070", "--criteria", criteria, "--json"]
            + ["--record"]
        )
        == 0
    )
    decision = tmp_path / "decision.json"
    decision.write_text(capsys.readouterr().out, encoding="utf-8")
    assert (
        router.main(
            [*common, "escalate", "--decision", str(decision), "--record"]
            + ["--reason", "logic_failure", "--evidence", "test_x fails"]
        )
        == 0
    )
    capsys.readouterr()
    assert router.main([*common, "record", "--event", json.dumps(VALID_EVENTS[0])]) == 0
    assert router.main([*common, "record", "--event", '{"kind": "chat"}']) == 2

    events = router.read_events(telemetry)
    assert [e["kind"] for e in events] == ["route", "escalation", "agent_run"]
    assert events[0]["policy_version"] == 2
    assert events[0]["decision"]["coder"] == "sonnet/high"
    assert events[1]["next_model"] == "opus" and events[1]["role"] == "coder"


def test_the_report_sums_per_task_and_per_profile(tmp_path, capsys) -> None:
    telemetry = tmp_path / "telemetry.jsonl"
    decision = route("F-070", **COMPLEX_PLAIN)
    router.record_event(
        router.route_event(decision, task(**COMPLEX_PLAIN), POLICY), POLICY, telemetry
    )
    for event in VALID_EVENTS:
        router.record_event(event, POLICY, telemetry)
    router.record_event(agent_run(task="F-071", tokens=500), POLICY, telemetry)

    text = router.report(router.read_events(telemetry))

    f070 = next(line for line in text.splitlines() if line.startswith("F-070 "))
    assert "COMPLEX" in f070 and "sonnet/high / sonnet/high / opus/high" in f070
    assert "2,000" in f070 and "0/2/1" in f070 and f070.endswith("pass")
    sonnet = next(line for line in text.splitlines() if line.startswith("sonnet/high"))
    assert sonnet.split()[1:4] == ["2", "1,500", "10"]
    assert router.main(["--telemetry", str(telemetry), "report"]) == 0
    assert "Per profile" in capsys.readouterr().out


def test_old_events_of_another_policy_still_report_and_garbage_is_skipped(
    tmp_path, capsys
) -> None:
    telemetry = tmp_path / "telemetry.jsonl"
    router.record_event(
        router.route_event(
            route("F-070", **COMPLEX_PLAIN), task(**COMPLEX_PLAIN), POLICY
        ),
        POLICY,
        telemetry,
    )
    at = "2026-10-03T10:00:00+00:00"
    later_route = {
        "kind": "route",
        "task": "F-090",
        "at": at,
        "policy_version": 3,
        "criteria": {**task(), "gpu_hours": 4},
        "decision": {**route("F-090").to_dict(), "budget": "low"},
        "cost_class": "cheap",
    }
    lines = [
        json.dumps({**VALID_EVENTS[2], "at": at, "reason": "gut_feeling"}),
        json.dumps(agent_run(profile="gpt5/turbo", at=at)),
        json.dumps(later_route),
        json.dumps({"kind": "budget", "task": "F-090", "at": at, "spent": 3}),
        "this is not json",
        json.dumps({"kind": "gate", "task": "F-070", "at": at}),
        json.dumps(["not", "an", "object"]),
    ]
    with telemetry.open("a", encoding="utf-8") as file:
        file.write("\n".join(lines) + "\n")
    capsys.readouterr()

    events = router.read_events(telemetry)

    err = capsys.readouterr().err
    assert [e["kind"] for e in events] == [
        "route",
        "escalation",
        "agent_run",
        "route",
        "budget",
    ]
    assert "telemetry.jsonl line 6 skipped" in err
    assert "telemetry.jsonl line 7 skipped: gate.tests_failed" in err
    assert "telemetry.jsonl line 8 skipped" in err
    assert "line 2 " not in err and "line 4 " not in err
    text = router.report(events)
    f090 = next(line for line in text.splitlines() if line.startswith("F-090 "))
    assert "TRIVIAL" in f090 and "?" in f090
    assert any(line.startswith("gpt5/turbo") for line in text.splitlines())
    compared = router.compare(events, POLICY)
    row = next(line for line in compared.splitlines() if line.startswith("F-090"))
    assert "not comparable: unknown criteria ['gpu_hours']" in row
    assert "sonnet/high / sonnet/high / opus/high" in compared
    assert router.main(["--telemetry", str(telemetry), "report"]) == 0
    assert router.main(["--telemetry", str(telemetry), "compare"]) == 0
    assert "skipped" in capsys.readouterr().err


def test_an_event_of_another_policy_is_still_refused_on_write(tmp_path) -> None:
    path = tmp_path / "telemetry.jsonl"

    with pytest.raises(router.RouterError, match="invalid value"):
        router.record_event({**VALID_EVENTS[2], "reason": "gut_feeling"}, POLICY, path)
    with pytest.raises(router.RouterError, match="invalid value"):
        router.record_event(agent_run(profile="gpt5/turbo"), POLICY, path)
    assert not path.exists()


# Backfilled history (F-065..F-069)

EXPECTED_HISTORY = {
    "F-065 Hook Generator": (19, "ARCHITECTURAL", ("opus/high",) * 3),
    "F-066 Shorts Script Generator": (
        8,
        "NORMAL",
        ("sonnet/medium", "sonnet/medium", None),
    ),
    "F-067 LongForm Script Generator": (
        9,
        "COMPLEX",
        ("sonnet/high", "sonnet/high", "opus/high"),
    ),
    "F-068 Claim Extractor": (
        12,
        "COMPLEX",
        ("sonnet/high", "sonnet/high", "opus/high"),
    ),
    "F-069 Evidence Matcher": (
        14,
        "COMPLEX",
        ("sonnet/high", "sonnet/high", "opus/high"),
    ),
}


def backfilled_routes():
    events = router.read_events()
    return {
        e["task"]: e for e in events if e["kind"] == "route" and e.get("backfilled")
    }


def test_the_committed_telemetry_is_valid_and_backfilled() -> None:
    events = router.read_events()
    # The log grows with every routed task; the backfill is its fixed start.
    backfill = [event for event in events if event.get("backfilled")]

    assert backfill and events[: len(backfill)] == backfill
    for event in events:  # every event was written under today's rules
        assert router.validate_event(event, POLICY) == event
    runs = [e for e in backfill if e["kind"] == "agent_run"]
    assert len(runs) == 8 and {e["profile"] for e in runs} == {"opus/high"}
    results = {e["task"]: e for e in events if e["kind"] == "result"}
    assert results["F-068 Claim Extractor"]["findings"] == {
        "blocking": 0,
        "should_fix": 4,
    }
    assert results["F-069 Evidence Matcher"]["tests"] == 3161


@pytest.mark.parametrize("name", list(EXPECTED_HISTORY))
def test_backfilled_routes_keep_their_score_and_get_cheaper_execution(name) -> None:
    score, level, expected = EXPECTED_HISTORY[name]
    event = backfilled_routes()[name]

    old = router.route(name, event["criteria"], LEGACY)
    new = router.route(name, event["criteria"], POLICY)

    assert router.classify(event["criteria"]).score == score
    assert event["decision"]["score"] == score and event["policy_version"] == 1
    assert (old.score, old.level) == (new.score, new.level) == (score, level)
    assert roles(new) == expected
    assert roles(router.Decision.from_dict(event["decision"])) == roles(old)
    assert new.triggers == ()


def test_compare_shows_the_v1_and_the_current_routing(capsys) -> None:
    assert router.main(["compare"]) == 0
    out = capsys.readouterr().out

    row = next(line for line in out.splitlines() if line.startswith("F-068"))
    assert "opus/high / opus/high / opus/high" in row
    assert "sonnet/high / sonnet/high / opus/high" in row
    assert len([line for line in out.splitlines() if line.startswith("F-06")]) == 5
