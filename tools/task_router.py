"""Task/Model Router for the development workflow of this repository.

This is development infrastructure, not part of the ``ai_youtube_agent``
package: it is not imported by ``src/`` and not shipped by ``uv build``.
The rules are explained in ``docs/TASK_ROUTER.md``; the policy (model,
effort, planner, reviewer per level and the escalation limits) lives only in
``.claude/task-router.json`` so it can be changed in one place.

- ``classify`` turns the routing criteria of a task into a level with a
  fixed, transparent score plus hard floors (see ``WEIGHTS`` and
  ``FLOORS``). Unknown criteria count as their lowest value and mark the
  decision uncertain: the router picks the lower level and relies on
  escalation, it never picks ``xhigh`` or ``max`` on its own.
- ``route`` looks the level up in the policy and applies a user override.
- ``escalate`` moves a decision one step up the model/effort ladder built from
  the policy, only for an escalating reason with evidence, at most
  ``max_steps`` times and never above the strongest level's profile.
- ``render_agents`` / ``sync_agents`` / ``check_agents`` keep the subagent
  files in ``.claude/agents/`` generated from the policy, because Claude Code
  fixes a subagent's effort in its file (the model can also be overridden
  per call).

Command line (run from the repository root)::

    uv run python tools/task_router.py route --task "F-068 Claim Extractor" \
        --criteria criteria.json [--model opus] [--effort high] [--skip-planner]
    uv run python tools/task_router.py escalate --decision decision.json \
        --reason logic_failure --evidence "..."
    uv run python tools/task_router.py sync-agents | check-agents
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
POLICY_PATH = ROOT / ".claude" / "task-router.json"
AGENTS_DIR = ROOT / ".claude" / "agents"
LEVELS = ("TRIVIAL", "SIMPLE", "NORMAL", "COMPLEX", "ARCHITECTURAL")
POLICY_KEYS = {"model", "effort", "planner", "reviewer", "architecture_review"}
# Efforts the router never chooses by itself; only a user override may.
OVERRIDE_ONLY_EFFORTS = {"xhigh", "max"}

# Allowed values of each criterion, lowest first. A missing criterion counts
# as its lowest value and is reported as unknown.
CRITERIA: dict[str, tuple[Any, ...] | type] = {
    "files_affected": int,
    "modules_affected": int,
    "dependency_depth": int,
    "behavior_change": bool,
    "architecture_impact": ("none", "local", "cross_module", "architectural"),
    "database_change": bool,
    "migration_required": bool,
    "provider_change": ("none", "logic", "contract"),
    "concurrency_risk": ("none", "low", "high"),
    "failure_recovery_complexity": ("none", "low", "high"),
    "test_complexity": ("low", "medium", "high"),
    "backward_compatibility_risk": ("none", "low", "high"),
    "uncertainty": ("low", "medium", "high"),
    "rollback_risk": ("low", "medium", "high"),
}

WEIGHTS: dict[str, dict[Any, int]] = {
    "architecture_impact": {
        "none": 0,
        "local": 1,
        "cross_module": 3,
        "architectural": 6,
    },
    "database_change": {False: 0, True: 1},
    "migration_required": {False: 0, True: 1},
    "provider_change": {"none": 0, "logic": 1, "contract": 4},
    "concurrency_risk": {"none": 0, "low": 1, "high": 3},
    "failure_recovery_complexity": {"none": 0, "low": 1, "high": 3},
    "test_complexity": {"low": 0, "medium": 1, "high": 2},
    "backward_compatibility_risk": {"none": 0, "low": 1, "high": 2},
    "rollback_risk": {"low": 0, "medium": 1, "high": 2},
}
# Upper score bound (inclusive) of each level below ARCHITECTURAL.
THRESHOLDS = {"TRIVIAL": 0, "SIMPLE": 2, "NORMAL": 8, "COMPLEX": 14}


class RouterError(ValueError):
    """Invalid policy, criteria, override or escalation."""


# Policy


@dataclass(frozen=True)
class LevelPolicy:
    model: str
    effort: str
    planner: bool
    reviewer: bool
    architecture_review: bool


@dataclass(frozen=True)
class Policy:
    levels: dict[str, LevelPolicy]
    supported_models: tuple[str, ...]
    supported_efforts: tuple[str, ...]
    max_steps: int
    escalating_reasons: tuple[str, ...]
    non_escalating_reasons: tuple[str, ...]

    def ladder(self) -> list[tuple[str, str]]:
        """The distinct (model, effort) profiles in level order."""
        profiles: list[tuple[str, str]] = []
        for level in LEVELS:
            profile = (self.levels[level].model, self.levels[level].effort)
            if profile not in profiles:
                profiles.append(profile)
        return profiles


def parse_policy(data: dict[str, Any]) -> Policy:
    if data.get("schema_version") != 1:
        raise RouterError("the policy schema_version must be 1")
    models = tuple(data.get("supported_models", ()))
    efforts = tuple(data.get("supported_efforts", ()))
    raw_levels = data.get("levels", {})
    if set(raw_levels) != set(LEVELS):
        raise RouterError(f"the policy must define exactly the levels {LEVELS}")
    levels = {}
    for name in LEVELS:
        raw = raw_levels[name]
        if set(raw) != POLICY_KEYS:
            raise RouterError(f"{name} must have exactly {sorted(POLICY_KEYS)}")
        if raw["model"] not in models:
            raise RouterError(f"{name}: unsupported model {raw['model']!r}")
        if raw["effort"] not in efforts:
            raise RouterError(f"{name}: unsupported effort {raw['effort']!r}")
        if raw["effort"] in OVERRIDE_ONLY_EFFORTS:
            raise RouterError(
                f"{name}: effort {raw['effort']!r} is only allowed as a user override"
            )
        if not all(isinstance(raw[k], bool) for k in POLICY_KEYS - {"model", "effort"}):
            raise RouterError(f"{name}: planner/reviewer flags must be true or false")
        levels[name] = LevelPolicy(**raw)
    escalation = data.get("escalation", {})
    max_steps = escalation.get("max_steps")
    if (
        not isinstance(max_steps, int)
        or isinstance(max_steps, bool)
        or not (0 <= max_steps <= 3)
    ):
        raise RouterError("escalation.max_steps must be a whole number from 0 to 3")
    return Policy(
        levels,
        models,
        efforts,
        max_steps,
        tuple(escalation.get("escalating_reasons", ())),
        tuple(escalation.get("non_escalating_reasons", ())),
    )


def load_policy(path: Path = POLICY_PATH) -> Policy:
    return parse_policy(json.loads(path.read_text(encoding="utf-8")))


# Classification


@dataclass(frozen=True)
class Classification:
    level: str
    score: int
    reasons: tuple[str, ...]
    unknown: tuple[str, ...]
    uncertain: bool


def _check_criteria(criteria: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    extra = set(criteria) - set(CRITERIA)
    if extra:
        raise RouterError(f"unknown criteria {sorted(extra)}")
    values: dict[str, Any] = {}
    unknown = []
    for name, allowed in CRITERIA.items():
        value = criteria.get(name)
        if value is None:
            unknown.append(name)
            value = 0 if allowed is int else False if allowed is bool else allowed[0]
        elif allowed is int:
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise RouterError(f"{name} must be a whole number >= 0")
        elif allowed is bool:
            if not isinstance(value, bool):
                raise RouterError(f"{name} must be true or false")
        elif value not in allowed:
            raise RouterError(f"{name} must be one of {list(allowed)}")
        values[name] = value
    return values, unknown


def _size_points(files: int, modules: int, depth: int) -> int:
    points = 0 if files <= 2 else 1 if files <= 5 else 2 if files <= 10 else 3
    points += 0 if modules <= 1 else 1 if modules == 2 else 2 if modules <= 4 else 3
    points += 0 if depth <= 1 else 1 if depth == 2 else 2
    return points


def _floor(values: dict[str, Any]) -> tuple[str, str] | None:
    """The lowest level some criterion forces, with the reason."""
    if values["architecture_impact"] == "architectural":
        return "ARCHITECTURAL", "changes the architecture"
    if values["provider_change"] == "contract":
        return "ARCHITECTURAL", "changes a provider contract"
    if values["concurrency_risk"] == "high":
        return "COMPLEX", "high concurrency risk"
    if values["failure_recovery_complexity"] == "high":
        return "COMPLEX", "complex failure/recovery"
    if values["architecture_impact"] == "cross_module":
        return "COMPLEX", "cross-module impact"
    if (
        values["database_change"]
        and values["provider_change"] != "none"
        and values["modules_affected"] >= 2
    ):
        return "COMPLEX", "database + domain + provider together"
    if values["migration_required"]:
        return "NORMAL", "needs a migration"
    if values["provider_change"] == "logic":
        return "NORMAL", "changes provider logic"
    if values["behavior_change"]:
        return "SIMPLE", "changes behaviour"
    return None


def classify(criteria: dict[str, Any]) -> Classification:
    values, unknown = _check_criteria(criteria)
    score = _size_points(
        values["files_affected"],
        values["modules_affected"],
        values["dependency_depth"],
    )
    reasons = []
    for name, weights in WEIGHTS.items():
        points = weights[values[name]]
        if points:
            score += points
            reasons.append(f"{name}={values[name]} (+{points})")
    level = next(
        (name for name, limit in THRESHOLDS.items() if score <= limit),
        "ARCHITECTURAL",
    )
    if level == "TRIVIAL" and values["behavior_change"]:
        level = "SIMPLE"
    floor = _floor(values)
    if floor and LEVELS.index(floor[0]) > LEVELS.index(level):
        level = floor[0]
        reasons.insert(0, f"floor: {floor[1]}")
    uncertain = bool(unknown) or values["uncertainty"] == "high"
    return Classification(level, score, tuple(reasons), tuple(unknown), uncertain)


# Routing


@dataclass(frozen=True)
class Decision:
    task: str
    level: str
    model: str
    effort: str
    planner: bool
    reviewer: bool
    architecture_review: bool
    reasons: tuple[str, ...]
    uncertain: bool
    unknown: tuple[str, ...] = ()
    escalation_allowed: bool = True
    steps: int = 0
    overrides: tuple[str, ...] = ()
    history: tuple[dict[str, Any], ...] = field(default=())

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Decision:
        data = dict(data)
        for key in ("reasons", "unknown", "overrides", "history"):
            data[key] = tuple(data.get(key, ()))
        return cls(**data)

    def render(self) -> str:
        yes = {True: "YES", False: "NO"}
        lines = [
            f"Task: {self.task}",
            f"Complexity: {self.level}" + (" (uncertain)" if self.uncertain else ""),
            "Reasons:",
            *(f"- {reason}" for reason in self.reasons or ("small, local change",)),
        ]
        if self.unknown:
            lines.append(
                f"Unknown criteria (counted lowest): {', '.join(self.unknown)}"
            )
        lines += [
            f"Model: {self.model}",
            f"Effort: {self.effort}",
            f"Planner: {yes[self.planner]}",
            f"Reviewer: {yes[self.reviewer]}"
            + (" + architecture review" if self.architecture_review else ""),
            f"Escalation: {'Allowed' if self.escalation_allowed else 'Not allowed'}"
            f" ({self.steps} used)",
        ]
        if self.overrides:
            lines.append(f"User override: {', '.join(self.overrides)}")
        return "\n".join(lines)


@dataclass(frozen=True)
class Override:
    """A user's explicit choice, e.g. "use opus", "high effort", "skip planner"."""

    model: str | None = None
    effort: str | None = None
    planner: bool | None = None
    reviewer: bool | None = None

    def is_empty(self) -> bool:
        return self == Override()


def route(
    task: str,
    criteria: dict[str, Any],
    policy: Policy,
    override: Override | None = None,
) -> Decision:
    if not task.strip():
        raise RouterError("the task needs a name")
    found = classify(criteria)
    level = policy.levels[found.level]
    decision = Decision(
        task=task.strip(),
        level=found.level,
        model=level.model,
        effort=level.effort,
        planner=level.planner,
        reviewer=level.reviewer,
        architecture_review=level.architecture_review,
        reasons=found.reasons,
        uncertain=found.uncertain,
        unknown=found.unknown,
        escalation_allowed=policy.max_steps > 0,
    )
    return apply_override(decision, override, policy) if override else decision


def apply_override(decision: Decision, override: Override, policy: Policy) -> Decision:
    if override.is_empty():
        return decision
    if override.model is not None and override.model not in policy.supported_models:
        raise RouterError(
            f"unsupported model {override.model!r}; "
            f"supported: {', '.join(policy.supported_models)}"
        )
    if override.effort is not None and override.effort not in policy.supported_efforts:
        raise RouterError(
            f"unsupported effort {override.effort!r}; "
            f"supported: {', '.join(policy.supported_efforts)}"
        )
    changes: dict[str, Any] = {}
    notes = []
    for name in ("model", "effort", "planner", "reviewer"):
        value = getattr(override, name)
        if value is not None:
            changes[name] = value
            notes.append(f"{name}={value}")
    return replace(decision, **changes, overrides=decision.overrides + tuple(notes))


# Escalation


def escalate(
    decision: Decision, reason: str, evidence: str, policy: Policy
) -> Decision:
    """The decision one step up the ladder; RouterError when not allowed."""
    if reason in policy.non_escalating_reasons:
        raise RouterError(
            f"{reason!r} does not justify escalation: fix it at the current level"
        )
    if reason not in policy.escalating_reasons:
        raise RouterError(
            f"unknown escalation reason {reason!r}; use one of "
            f"{', '.join(policy.escalating_reasons)}"
        )
    if not evidence.strip():
        raise RouterError("an escalation needs evidence (failing test, error, gap)")
    if decision.steps >= policy.max_steps:
        raise RouterError(
            f"escalation limit reached ({policy.max_steps} steps): "
            "stop and ask the user"
        )
    ladder = policy.ladder()
    current = (decision.model, decision.effort)
    above = [
        profile for profile in ladder if _rank(profile, ladder) > _rank(current, ladder)
    ]
    if not above:
        raise RouterError(
            f"{decision.model}/{decision.effort} is already the strongest routed "
            "profile: stop and ask the user"
        )
    model, effort = above[0]
    record = {
        "current": f"{decision.model}/{decision.effort}",
        "reason": reason,
        "evidence": evidence.strip(),
        "next": f"{model}/{effort}",
    }
    reviewer = decision.reviewer or model == policy.levels["COMPLEX"].model
    return replace(
        decision,
        model=model,
        effort=effort,
        reviewer=reviewer,
        steps=decision.steps + 1,
        escalation_allowed=decision.steps + 1 < policy.max_steps,
        history=decision.history + (record,),
    )


def _rank(profile: tuple[str, str], ladder: list[tuple[str, str]]) -> int:
    """The position of a profile; an off-ladder profile ranks by its model."""
    if profile in ladder:
        return ladder.index(profile)
    same_model = [i for i, (model, _) in enumerate(ladder) if model == profile[0]]
    return max(same_model) if same_model else len(ladder)


# Subagent files

ROLE_TOOLS = {
    "planner": "Read, Grep, Glob, Bash",
    "coder": None,
    "reviewer": "Read, Grep, Glob, Bash",
}
ROLE_TEXT = {
    "planner": """You are the planner for this {level} task in this repository.

Read CLAUDE.md, PROJECT_STATE.md (rules and next task), TASK_STATUS.md and the
code the task touches. Do not edit files.

Return:
1. The routing criteria you measured, as JSON for tools/task_router.py, and
   whether the level {level} still fits (say so if it does not).
2. Open decisions the Prompt Pack does not settle, each with 3-4 real
   options and a recommendation. The main session asks the user; you never
   decide them.
3. A step-by-step implementation plan: files to create or change, tests to
   add, migrations (forward-only, never edit an applied one), state files to
   update.
4. Risks and what must not change (behaviour of finished tasks, provider
   contracts, strategy, migration history).
""",
    "coder": """You are the coder for this {level} task in this repository.

Follow the plan and the user's decisions you are given exactly; do not widen
the scope or take architecture decisions on your own. Write code that reads
like the surrounding code. Stack: Python 3.11, uv, pytest, ruff (no type
checker). Never edit an applied migration, delete tests or lower coverage.

Before you report:
- run `uv run python -m pytest` (the full suite), `uv run python -m ruff check .`
  and `uv run python -m ruff format --check .`;
- report the files changed, the test count, and every failure with its output.

If a failure needs deeper reasoning than you can give (a logic or design
problem, not a typo, lint, format, simple import or environment error), stop
and report it with the evidence so the main session can escalate. Do not
commit or push.
""",
    "reviewer": """You are the reviewer for this {level} task in this repository.

Review the uncommitted changes (`git diff`, `git status`) against the task,
the user's decisions and the plan. Do not edit files.

Check: correctness and edge cases, that finished tasks keep their behaviour,
provider contracts and migration history are unchanged, tests cover the new
rules, and the full suite, ruff check and ruff format pass (run them).
{architecture}
Answer PASS, or FAIL with each finding (file:line, problem, evidence) and
whether it needs escalation (logic or design problem) or only a fix at the
current level.
""",
}
ARCHITECTURE_TEXT = """Also review the architecture: module boundaries and
dependencies (docs/ARCHITECTURE.md), contracts other tasks rely on, and what
the decision means for later Prompt Pack tasks.
"""


def agent_specs(policy: Policy) -> dict[str, str]:
    """File name -> content of every routed subagent file."""
    files = {}
    for level in LEVELS:
        rule = policy.levels[level]
        roles = ["coder"]
        if rule.planner:
            roles.insert(0, "planner")
        if rule.reviewer:
            roles.append("reviewer")
        for role in roles:
            name = f"task-{role}-{level.lower()}"
            body = ROLE_TEXT[role].format(
                level=level,
                architecture=ARCHITECTURE_TEXT if rule.architecture_review else "",
            )
            front = [
                "---",
                f"name: {name}",
                f"description: Routed {role} for {level} tasks (task router, "
                f"{rule.model}/{rule.effort}). Use only after routing a task to "
                f"{level}; see docs/TASK_ROUTER.md.",
                f"model: {rule.model}",
                f"effort: {rule.effort}",
            ]
            if ROLE_TOOLS[role]:
                front.append(f"tools: {ROLE_TOOLS[role]}")
            front += [
                "---",
                "",
                "<!-- Generated from .claude/task-router.json by "
                "`uv run python tools/task_router.py sync-agents`. Do not edit. -->",
                "",
                "",
            ]
            files[f"{name}.md"] = "\n".join(front) + body
    return files


def check_agents(policy: Policy, directory: Path = AGENTS_DIR) -> list[str]:
    """The differences between the policy and the files in ``directory``."""
    expected = agent_specs(policy)
    problems = []
    for name, content in expected.items():
        path = directory / name
        if not path.exists():
            problems.append(f"missing {name}")
        elif path.read_text(encoding="utf-8").replace("\r\n", "\n") != content:
            problems.append(f"outdated {name}")
    if directory.exists():
        for path in sorted(directory.glob("task-*.md")):
            if path.name not in expected:
                problems.append(f"unexpected {path.name}")
    return problems


def sync_agents(policy: Policy, directory: Path = AGENTS_DIR) -> list[str]:
    """Write the routed subagent files and remove stale ones; the changed names."""
    directory.mkdir(parents=True, exist_ok=True)
    expected = agent_specs(policy)
    changed = []
    for name, content in expected.items():
        path = directory / name
        current = path.read_text(encoding="utf-8") if path.exists() else None
        if current is None or current.replace("\r\n", "\n") != content:
            path.write_text(content, encoding="utf-8", newline="\n")
            changed.append(name)
    for path in sorted(directory.glob("task-*.md")):
        if path.name not in expected:
            path.unlink()
            changed.append(path.name)
    return changed


# Command line


def _json_arg(value: str) -> dict[str, Any]:
    path = Path(value)
    text = path.read_text(encoding="utf-8") if path.exists() else value
    data = json.loads(text)
    if not isinstance(data, dict):
        raise RouterError("expected a JSON object")
    return data


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="task_router")
    parser.add_argument("--policy", type=Path, default=POLICY_PATH)
    commands = parser.add_subparsers(dest="command", required=True)
    route_cmd = commands.add_parser("route")
    route_cmd.add_argument("--task", required=True)
    route_cmd.add_argument("--criteria", required=True, help="JSON or a JSON file")
    route_cmd.add_argument("--model")
    route_cmd.add_argument("--effort")
    route_cmd.add_argument("--skip-planner", action="store_true")
    route_cmd.add_argument("--skip-reviewer", action="store_true")
    route_cmd.add_argument("--json", action="store_true")
    escalate_cmd = commands.add_parser("escalate")
    escalate_cmd.add_argument("--decision", required=True, help="JSON or a file")
    escalate_cmd.add_argument("--reason", required=True)
    escalate_cmd.add_argument("--evidence", required=True)
    commands.add_parser("sync-agents")
    commands.add_parser("check-agents")
    args = parser.parse_args(argv)
    try:
        policy = load_policy(args.policy)
        if args.command == "route":
            override = Override(
                model=args.model,
                effort=args.effort,
                planner=False if args.skip_planner else None,
                reviewer=False if args.skip_reviewer else None,
            )
            decision = route(args.task, _json_arg(args.criteria), policy, override)
            print(
                json.dumps(decision.to_dict(), indent=2)
                if args.json
                else decision.render()
            )
        elif args.command == "escalate":
            decision = Decision.from_dict(_json_arg(args.decision))
            print(
                json.dumps(
                    escalate(decision, args.reason, args.evidence, policy).to_dict(),
                    indent=2,
                )
            )
        elif args.command == "sync-agents":
            changed = sync_agents(policy)
            print("\n".join(changed) if changed else "agents up to date")
        else:
            problems = check_agents(policy)
            print("\n".join(problems) if problems else "agents up to date")
            return 1 if problems else 0
    except (RouterError, json.JSONDecodeError, OSError) as error:
        print(f"task_router: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
