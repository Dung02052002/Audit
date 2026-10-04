"""Cost-aware Task/Model Router for the development workflow of this repository.

This is development infrastructure, not part of the ``ai_youtube_agent``
package: it is not imported by ``src/`` and not shipped by ``uv build``.
The rules are explained in ``docs/TASK_ROUTER.md``; the policy (a profile per
role and level, the Opus trigger and escalation profiles and limits) lives
only in ``.claude/task-router.json`` so it can be changed in one place.

Complexity and execution are separate:

- ``classify`` turns the routing criteria of a task into a level with a
  fixed, transparent score plus hard floors (see ``WEIGHTS`` and
  ``_floor``). Unknown criteria count as their lowest value and mark the
  decision uncertain.
- ``route`` picks a ``model/effort`` profile for each role (planner, coder,
  reviewer) from the level's policy, then adjusts it by evidence from the
  risk profile: an Opus trigger lifts planner and coder to Opus at NORMAL
  and COMPLEX; a high regression or migration risk without architecture
  impact raises the coder's effort on the same model. A user override is
  applied last and recorded. The router never chooses ``xhigh`` or ``max``.
- ``escalate`` moves the coder one step up the policy's ladder, only for an
  escalating reason with evidence, at most ``max_steps`` times and never
  above the ladder's top (``opus/high``).
- ``render_agents`` / ``sync_agents`` / ``check_agents`` keep the subagent
  files in ``.claude/agents/`` (one per role and profile) generated from the
  policy, because Claude Code fixes a subagent's effort in its file.
- ``record_event`` / ``report`` / ``compare`` keep append-only telemetry in
  ``.claude/router-telemetry.jsonl`` (counts only, never prompts or answers).

Command line (run from the repository root)::

    uv run python tools/task_router.py route --task "F-070 Fact Check Result" \
        --criteria criteria.json [--profile risk.json] \
        [--prior-review-issue "..."] [--model opus] [--effort high] \
        [--role coder] [--skip-planner] [--skip-reviewer] [--json] [--record]
    uv run python tools/task_router.py escalate --decision decision.json \
        --reason logic_failure --evidence "..." [--record]
    uv run python tools/task_router.py record --event '{"kind": "gate", ...}'
    uv run python tools/task_router.py report | compare
    uv run python tools/task_router.py sync-agents | check-agents
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
POLICY_PATH = ROOT / ".claude" / "task-router.json"
AGENTS_DIR = ROOT / ".claude" / "agents"
TELEMETRY_PATH = ROOT / ".claude" / "router-telemetry.jsonl"
LEVELS = ("TRIVIAL", "SIMPLE", "NORMAL", "COMPLEX", "ARCHITECTURAL")
ROLES = ("planner", "coder", "reviewer")
# Efforts the router never chooses by itself; only a user override may.
OVERRIDE_ONLY_EFFORTS = {"xhigh", "max"}
# The only profiles a policy may route to, weakest first.
ALLOWED_PROFILES = (
    "haiku/low",
    "sonnet/low",
    "sonnet/medium",
    "sonnet/high",
    "opus/medium",
    "opus/high",
)
# Reasons that never justify escalation, whatever the policy lists: fix them
# at the current profile.
NEVER_ESCALATE = frozenset(
    {
        "typo",
        "lint",
        "format",
        "simple_import",
        "type",
        "test_fixture",
        "environment",
        "flaky_unrelated",
    }
)
# The most escalation steps a policy may allow.
MAX_ESCALATION_STEPS = 2
LEVEL_KEYS = {
    "planner",
    "coder",
    "reviewer",
    "risk_reviewer",
    "opus_trigger_profile",
    "risk_effort_bump",
    "architecture_review",
}
V1_LEVEL_KEYS = {"model", "effort", "planner", "reviewer", "architecture_review"}

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

RISK_VALUES = ("low", "medium", "high")
# The risk profile: optional input; a missing field is derived from the
# criteria (see ``_derive_risk``) and reported as derived.
RISK_FIELDS: dict[str, tuple[str, ...] | type] = {
    "reasoning_risk": RISK_VALUES,
    "architecture_risk": RISK_VALUES,
    "regression_risk": RISK_VALUES,
    "migration_risk": RISK_VALUES,
    "provider_risk": RISK_VALUES,
    "uncertainty": RISK_VALUES,
    "estimated_files": int,
    "estimated_modules": int,
    "estimated_test_complexity": RISK_VALUES,
}
# The criteria each risk field is derived from (for the evidence text).
DERIVED_FROM = {
    "reasoning_risk": ("concurrency_risk", "failure_recovery_complexity"),
    "architecture_risk": ("architecture_impact",),
    "regression_risk": ("backward_compatibility_risk", "behavior_change"),
    "migration_risk": ("migration_required", "rollback_risk"),
    "provider_risk": ("provider_change",),
    "uncertainty": ("uncertainty",),
    "estimated_files": ("files_affected",),
    "estimated_modules": ("modules_affected",),
    "estimated_test_complexity": ("test_complexity",),
}

# The v1 policy that routed tasks until TOOL-002; ``compare`` shows it next to
# the current policy. It is history, not configuration.
LEGACY_V1_POLICY: dict[str, Any] = {
    "schema_version": 1,
    "supported_models": ["haiku", "sonnet", "opus", "fable", "inherit"],
    "supported_efforts": ["low", "medium", "high", "xhigh", "max"],
    "levels": {
        level: {
            "model": model,
            "effort": effort,
            "planner": planner,
            "reviewer": reviewer,
            "architecture_review": level == "ARCHITECTURAL",
        }
        for level, model, effort, planner, reviewer in (
            ("TRIVIAL", "haiku", "low", False, False),
            ("SIMPLE", "sonnet", "low", False, False),
            ("NORMAL", "sonnet", "medium", True, False),
            ("COMPLEX", "opus", "high", True, True),
            ("ARCHITECTURAL", "opus", "high", True, True),
        )
    },
    "escalation": {
        "max_steps": 2,
        "escalating_reasons": [
            "logic_failure",
            "design_gap",
            "repeated_test_failure",
            "insufficient_reasoning",
        ],
        "non_escalating_reasons": [
            "typo",
            "lint",
            "format",
            "simple_import",
            "environment",
            "flaky_unrelated",
        ],
    },
}


class RouterError(ValueError):
    """Invalid policy, criteria, risk profile, override, escalation or event."""


def split_profile(profile: str) -> tuple[str, str]:
    """``"sonnet/high"`` -> ``("sonnet", "high")``."""
    model, sep, effort = str(profile).partition("/")
    if not sep or not model or not effort or "/" in effort:
        raise RouterError(f"a profile is 'model/effort', not {profile!r}")
    return model, effort


def _allowed_rank(profile: str) -> int:
    return ALLOWED_PROFILES.index(profile)


# Policy


@dataclass(frozen=True)
class LevelPolicy:
    """The profile per role of one level (``None`` = the role is off)."""

    planner: str | None
    coder: str
    reviewer: str | None
    risk_reviewer: str | None
    opus_trigger_profile: str | None
    risk_effort_bump: bool
    architecture_review: bool


@dataclass(frozen=True)
class Policy:
    schema_version: int
    levels: dict[str, LevelPolicy]
    supported_models: tuple[str, ...]
    supported_efforts: tuple[str, ...]
    opus_reviewer: str
    ladder: tuple[str, ...]
    max_steps: int
    escalating_reasons: tuple[str, ...]
    non_escalating_reasons: tuple[str, ...]

    def effort_step(self, profile: str) -> str:
        """The next ladder profile on the same model, or ``profile`` itself."""
        model = split_profile(profile)[0]
        for step in self.ladder:
            if split_profile(step)[0] == model and _ladder_rank(
                step, self.ladder
            ) > _ladder_rank(profile, self.ladder):
                return step
        return profile


def _routed_profile(where: str, value: Any, optional: bool = True) -> str | None:
    """A profile the router may choose; ``None`` only when ``optional``."""
    if value is None and optional:
        return None
    if not isinstance(value, str):
        raise RouterError(
            f"{where} must be a profile 'model/effort'"
            + (" or null" if optional else "")
        )
    effort = split_profile(value)[1]
    if effort in OVERRIDE_ONLY_EFFORTS:
        raise RouterError(
            f"{where}: effort {effort!r} is only allowed as a user override"
        )
    if value not in ALLOWED_PROFILES:
        raise RouterError(
            f"{where}: {value!r} is not an allowed profile "
            f"({', '.join(ALLOWED_PROFILES)})"
        )
    return value


def _upgrade_v1(data: dict[str, Any]) -> dict[str, Any]:
    """A schema_version 1 policy as version 2: each level's model/effort
    becomes the profile of every enabled role, with no trigger or bump."""
    raw_levels = data.get("levels", {})
    if set(raw_levels) != set(LEVELS):
        raise RouterError(f"the policy must define exactly the levels {LEVELS}")
    levels: dict[str, Any] = {}
    ladder: list[str] = []
    for name in LEVELS:
        raw = raw_levels[name]
        if set(raw) != V1_LEVEL_KEYS:
            raise RouterError(f"{name} must have exactly {sorted(V1_LEVEL_KEYS)}")
        if not all(
            isinstance(raw[key], bool)
            for key in ("planner", "reviewer", "architecture_review")
        ):
            raise RouterError(f"{name}: planner/reviewer flags must be true or false")
        profile = f"{raw['model']}/{raw['effort']}"
        levels[name] = {
            "planner": profile if raw["planner"] else None,
            "coder": profile,
            "reviewer": profile if raw["reviewer"] else None,
            "risk_reviewer": None,
            "opus_trigger_profile": None,
            "risk_effort_bump": False,
            "architecture_review": raw["architecture_review"],
        }
        if profile not in ladder:
            ladder.append(profile)
    escalation = dict(data.get("escalation", {}))
    escalation["ladder"] = ladder
    return {
        **data,
        "levels": levels,
        "opus_reviewer": levels["COMPLEX"]["coder"],
        "escalation": escalation,
    }


def parse_policy(data: dict[str, Any]) -> Policy:
    version = data.get("schema_version")
    if version == 1:
        data = _upgrade_v1(data)
    elif version != 2:
        raise RouterError("the policy schema_version must be 1 or 2")
    models = tuple(data.get("supported_models", ()))
    efforts = tuple(data.get("supported_efforts", ()))
    raw_levels = data.get("levels", {})
    if set(raw_levels) != set(LEVELS):
        raise RouterError(f"the policy must define exactly the levels {LEVELS}")
    levels = {}
    for name in LEVELS:
        raw = raw_levels[name]
        if set(raw) != LEVEL_KEYS:
            raise RouterError(f"{name} must have exactly {sorted(LEVEL_KEYS)}")
        if not all(
            isinstance(raw[key], bool)
            for key in ("risk_effort_bump", "architecture_review")
        ):
            raise RouterError(
                f"{name}: risk_effort_bump/architecture_review must be true or false"
            )
        rule = LevelPolicy(
            planner=_routed_profile(f"{name}.planner", raw["planner"]),
            coder=_routed_profile(f"{name}.coder", raw["coder"], optional=False),
            reviewer=_routed_profile(f"{name}.reviewer", raw["reviewer"]),
            risk_reviewer=_routed_profile(
                f"{name}.risk_reviewer", raw["risk_reviewer"]
            ),
            opus_trigger_profile=_routed_profile(
                f"{name}.opus_trigger_profile", raw["opus_trigger_profile"]
            ),
            risk_effort_bump=raw["risk_effort_bump"],
            architecture_review=raw["architecture_review"],
        )
        if rule.architecture_review and rule.reviewer is None:
            raise RouterError(f"{name}: an architecture review needs a reviewer")
        levels[name] = rule
    opus_reviewer = _routed_profile(
        "opus_reviewer", data.get("opus_reviewer"), optional=False
    )
    escalation = data.get("escalation", {})
    ladder = tuple(
        _routed_profile(f"escalation.ladder[{i}]", step, optional=False)
        for i, step in enumerate(escalation.get("ladder", ()))
    )
    if not ladder or any(
        _allowed_rank(low) >= _allowed_rank(high)
        for low, high in zip(ladder, ladder[1:], strict=False)
    ):
        raise RouterError(
            "escalation.ladder must list allowed profiles, weakest first, "
            "without repeats"
        )
    for name, rule in levels.items():
        for profile in (rule.coder, rule.opus_trigger_profile):
            if profile is not None and profile not in ladder:
                raise RouterError(
                    f"{name}: coder profile {profile!r} is not on the escalation ladder"
                )
    max_steps = escalation.get("max_steps")
    if (
        not isinstance(max_steps, int)
        or isinstance(max_steps, bool)
        or not (0 <= max_steps <= MAX_ESCALATION_STEPS)
    ):
        raise RouterError(
            "escalation.max_steps must be a whole number from 0 to "
            f"{MAX_ESCALATION_STEPS}"
        )
    escalating = tuple(escalation.get("escalating_reasons", ()))
    non_escalating = tuple(escalation.get("non_escalating_reasons", ()))
    if set(escalating) & set(non_escalating):
        raise RouterError("a reason cannot both escalate and not escalate")
    never = sorted(set(escalating) & NEVER_ESCALATE)
    if never:
        raise RouterError(f"{', '.join(never)} must never escalate")
    return Policy(
        version,
        levels,
        models,
        efforts,
        opus_reviewer,
        ladder,
        max_steps,
        escalating,
        non_escalating,
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


# Risk profile


def _derive_risk(values: dict[str, Any]) -> dict[str, Any]:
    """The risk profile implied by the criteria (table in docs/TASK_ROUTER.md)."""
    worst = max(
        values["concurrency_risk"],
        values["failure_recovery_complexity"],
        key=("none", "low", "high").index,
    )
    if values["backward_compatibility_risk"] == "high":
        regression = "high"
    elif values["backward_compatibility_risk"] == "low" or values["behavior_change"]:
        regression = "medium"
    else:
        regression = "low"
    if not values["migration_required"]:
        migration = "low"
    elif values["rollback_risk"] == "high":
        migration = "high"
    else:
        migration = "medium"
    return {
        "reasoning_risk": {"none": "low", "low": "medium", "high": "high"}[worst],
        "architecture_risk": {
            "none": "low",
            "local": "low",
            "cross_module": "medium",
            "architectural": "high",
        }[values["architecture_impact"]],
        "regression_risk": regression,
        "migration_risk": migration,
        "provider_risk": {"none": "low", "logic": "medium", "contract": "high"}[
            values["provider_change"]
        ],
        "uncertainty": values["uncertainty"],
        "estimated_files": values["files_affected"],
        "estimated_modules": values["modules_affected"],
        "estimated_test_complexity": values["test_complexity"],
    }


def risk_profile(
    criteria: dict[str, Any], given: dict[str, Any] | None = None
) -> tuple[dict[str, Any], tuple[str, ...]]:
    """The full risk profile and the names of the fields derived from criteria."""
    given = {name: value for name, value in (given or {}).items() if value is not None}
    extra = set(given) - set(RISK_FIELDS)
    if extra:
        raise RouterError(f"unknown risk profile fields {sorted(extra)}")
    for name, value in given.items():
        allowed = RISK_FIELDS[name]
        if allowed is int:
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise RouterError(f"{name} must be a whole number >= 0")
        elif value not in allowed:
            raise RouterError(f"{name} must be one of {list(allowed)}")
    values, _ = _check_criteria(criteria)
    derived_values = _derive_risk(values)
    profile = {name: given.get(name, derived_values[name]) for name in RISK_FIELDS}
    derived = tuple(name for name in RISK_FIELDS if name not in given)
    return profile, derived


def _origin(name: str, values: dict[str, Any], derived: tuple[str, ...]) -> str:
    if name not in derived:
        return "given in the risk profile"
    sources = ", ".join(f"{source}={values[source]}" for source in DERIVED_FROM[name])
    return f"derived from {sources}"


def opus_triggers(
    values: dict[str, Any],
    risk: dict[str, Any],
    derived: tuple[str, ...],
    prior_review_issue: str = "",
) -> tuple[dict[str, str], ...]:
    """The evidence that the task needs Opus; empty when Sonnet is enough.

    ``uncertainty = high`` alone is not a trigger: the task starts on Sonnet,
    is marked uncertain and relies on escalation.
    """
    found: list[dict[str, str]] = []

    def add(name: str, evidence: str) -> None:
        found.append({"name": name, "evidence": evidence})

    if risk["architecture_risk"] == "high":
        add(
            "architecture_risk",
            "architecture_risk=high, an unclear architecture decision "
            f"({_origin('architecture_risk', values, derived)})",
        )
    if values["architecture_impact"] == "cross_module":
        add(
            "interdependent_modules",
            "architecture_impact=cross_module: many interdependent modules",
        )
    elif risk["estimated_modules"] >= 4 and values["dependency_depth"] >= 3:
        add(
            "interdependent_modules",
            f"estimated_modules={risk['estimated_modules']} with "
            f"dependency_depth={values['dependency_depth']}: many interdependent "
            "modules",
        )
    for name, meaning in (
        ("reasoning_risk", "concurrency or failure/recovery"),
        ("migration_risk", "a risky migration"),
        ("provider_risk", "a provider contract change"),
    ):
        if risk[name] == "high":
            add(
                name,
                f"{name}=high, {meaning} ({_origin(name, values, derived)})",
            )
    if prior_review_issue:
        add(
            "prior_review_issue",
            f"a prior review found a design/reasoning issue: {prior_review_issue}",
        )
    if risk["uncertainty"] == "high" and risk["architecture_risk"] != "low":
        add(
            "uncertainty_with_architecture",
            f"uncertainty=high with architecture_risk={risk['architecture_risk']}",
        )
    return tuple(found)


# Routing


NO_AGENT_FILE_HINT = "use the fail-safe in docs/TASK_ROUTER.md"


def agent_name(role: str, profile: str) -> str:
    model, effort = split_profile(profile)
    return f"task-{role}-{model}-{effort}"


@dataclass(frozen=True)
class Decision:
    """The routing of one task: a ``model/effort`` profile per role.

    ``model`` and ``effort`` are the coder's, kept at the top level of the
    JSON so scripts written for schema_version 1 decisions keep working.
    """

    task: str
    level: str
    coder: str
    planner: str | None = None
    reviewer: str | None = None
    architecture_review: bool = False
    score: int | None = None
    reasons: tuple[str, ...] = ()
    uncertain: bool = False
    unknown: tuple[str, ...] = ()
    risk_profile: dict[str, Any] = field(default_factory=dict)
    derived: tuple[str, ...] = ()
    triggers: tuple[dict[str, str], ...] = ()
    adjustments: tuple[str, ...] = ()
    escalation_allowed: bool = True
    steps: int = 0
    overrides: tuple[str, ...] = ()
    # The user skipped the reviewer: escalation never turns it back on.
    reviewer_skipped: bool = False
    # Roles whose profile has no generated agent file (fail-safe applies).
    no_agent_file: tuple[str, ...] = ()
    history: tuple[dict[str, Any], ...] = ()

    @property
    def model(self) -> str:
        return split_profile(self.coder)[0]

    @property
    def effort(self) -> str:
        return split_profile(self.coder)[1]

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        return {
            "schema_version": 2,
            "task": data.pop("task"),
            "level": data.pop("level"),
            "model": self.model,
            "effort": self.effort,
            **data,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Decision:
        """A decision from its JSON, schema_version 2 or 1 (top-level
        model/effort and planner/reviewer flags)."""
        data = dict(data)
        data.pop("schema_version", None)
        try:
            if "coder" not in data:
                profile = f"{data.pop('model')}/{data.pop('effort')}"
                data["coder"] = profile
                data["planner"] = profile if data.get("planner") else None
                data["reviewer"] = profile if data.get("reviewer") else None
            else:
                data.pop("model", None)
                data.pop("effort", None)
            for key in (
                "reasons",
                "unknown",
                "derived",
                "adjustments",
                "overrides",
                "no_agent_file",
            ):
                data[key] = tuple(data.get(key, ()))
            if "reviewer_skipped" not in data:  # older decisions: from the notes
                data["reviewer_skipped"] = any(
                    note in ("reviewer=False", "reviewer=off")
                    for note in data["overrides"]
                )
            for key in ("triggers", "history"):
                data[key] = tuple(dict(item) for item in data.get(key, ()))
            decision = cls(**data)
            split_profile(decision.coder)
        except (KeyError, TypeError) as error:
            raise RouterError(f"not a routing decision: {error}") from error
        return decision

    def render(self) -> str:
        score = f", score {self.score}" if self.score is not None else ""
        lines = [
            f"Task: {self.task}",
            f"Complexity: {self.level}{score}"
            + (" (uncertain)" if self.uncertain else ""),
            "Reasons:",
            *(f"- {reason}" for reason in self.reasons or ("small, local change",)),
        ]
        if self.unknown:
            lines.append(
                f"Unknown criteria (counted lowest): {', '.join(self.unknown)}"
            )
        for role in ROLES:
            profile = getattr(self, role)
            if profile is None:
                text = "off"
            elif role in self.no_agent_file or profile not in ALLOWED_PROFILES:
                text = f"{profile} (no agent file: {NO_AGENT_FILE_HINT})"
            else:
                text = f"{profile} ({agent_name(role, profile)})"
            if role == "reviewer" and self.architecture_review:
                text += " + architecture review"
            lines.append(f"{role.capitalize()}: {text}")
        lines += [f"Model: {self.model} (coder)", f"Effort: {self.effort} (coder)"]
        if self.triggers:
            lines.append("Opus triggers:")
            lines += [f"- {t['name']}: {t['evidence']}" for t in self.triggers]
        elif self.model == "opus":
            lines.append("Opus triggers: none (Opus by level policy or override)")
        else:
            lines.append(f"Opus triggers: none: {self.model.capitalize()} is enough")
        if self.adjustments:
            lines.append("Adjustments:")
            lines += [f"- {adjustment}" for adjustment in self.adjustments]
        if self.risk_profile:
            marks = ", ".join(
                f"{name}={value}" + ("*" if name in self.derived else "")
                for name, value in self.risk_profile.items()
            )
            lines.append(f"Risk profile (* derived from the criteria): {marks}")
        if self.escalation_allowed:
            lines.append(f"Escalation: Allowed ({self.steps} used)")
        else:
            lines.append(
                f"Escalation: Not allowed ({self.steps} used; the coder is at the "
                "top of the ladder or the steps are used up): stop and ask the user"
            )
        for record in self.history:
            if "current_model" in record:
                lines.append(
                    f"- {record.get('role', 'coder')} "
                    f"{record['current_model']}/{record['current_effort']} -> "
                    f"{record['next_model']}/{record['next_effort']}: "
                    f"{record['reason']} ({record['evidence']})"
                )
            else:  # a schema_version 1 record
                lines.append(
                    f"- {record.get('current')} -> {record.get('next')}: "
                    f"{record.get('reason')} ({record.get('evidence')})"
                )
        if self.overrides:
            lines.append(f"User override: {', '.join(self.overrides)}")
        return "\n".join(lines)


@dataclass(frozen=True)
class Override:
    """A user's explicit choice, e.g. "use opus", "high effort", "skip planner".

    ``model``/``effort`` apply to ``role`` (the coder by default); ``planner``
    or ``reviewer`` = False skips that role, True turns it on at the coder's
    profile.
    """

    model: str | None = None
    effort: str | None = None
    role: str = "coder"
    planner: bool | None = None
    reviewer: bool | None = None

    def is_empty(self) -> bool:
        return self == Override()


def route(
    task: str,
    criteria: dict[str, Any],
    policy: Policy,
    override: Override | None = None,
    profile: dict[str, Any] | None = None,
    prior_review_issue: str | None = None,
) -> Decision:
    if not task.strip():
        raise RouterError("the task needs a name")
    if prior_review_issue is not None and not prior_review_issue.strip():
        raise RouterError("a prior review issue needs evidence")
    issue = (prior_review_issue or "").strip()
    found = classify(criteria)
    values, _ = _check_criteria(criteria)
    risk, derived = risk_profile(criteria, profile)
    rule = policy.levels[found.level]
    planner, coder, reviewer = rule.planner, rule.coder, rule.reviewer
    adjustments: list[str] = []
    triggers: tuple[dict[str, str], ...] = ()
    if rule.opus_trigger_profile:
        triggers = opus_triggers(values, risk, derived, issue)
    elif issue:
        adjustments.append(
            f"prior review issue noted; it is not an Opus trigger at {found.level}"
        )
    if (
        not rule.opus_trigger_profile
        and not rule.architecture_review
        and risk["architecture_risk"] == "high"
    ):
        adjustments.append(
            "architecture_risk=high "
            f"({_origin('architecture_risk', values, derived)}) is not an Opus "
            f"trigger at {found.level}: re-measure the criteria or re-route, an "
            f"open architecture question does not fit {found.level}"
        )
    if triggers:
        # The trigger evidence explains the Opus reviewer.
        planner = rule.opus_trigger_profile if planner else None
        coder = rule.opus_trigger_profile
        reviewer = policy.opus_reviewer
    else:
        high = [
            name
            for name in ("regression_risk", "migration_risk", "reasoning_risk")
            if risk[name] == "high"
        ]
        if (
            reviewer is None
            and rule.risk_reviewer
            and values["behavior_change"]
            and high
        ):
            reviewer = rule.risk_reviewer
            adjustments.append(
                f"reviewer on at {reviewer}: behaviour changes and "
                + ", ".join(f"{name}=high" for name in high)
            )
        risky = [
            name
            for name in ("regression_risk", "migration_risk")
            if risk[name] == "high"
        ]
        stronger = policy.effort_step(coder)
        if rule.risk_effort_bump and risky and stronger != coder:
            adjustments.append(
                f"coder {coder} -> {stronger} (same model, more effort): "
                + ", ".join(f"{name}=high" for name in risky)
                + f" with architecture_risk={risk['architecture_risk']}"
            )
            coder = stronger
    decision = Decision(
        task=task.strip(),
        level=found.level,
        coder=coder,
        planner=planner,
        reviewer=reviewer,
        architecture_review=rule.architecture_review,
        score=found.score,
        reasons=found.reasons,
        uncertain=found.uncertain or risk["uncertainty"] == "high",
        unknown=found.unknown,
        risk_profile=risk,
        derived=derived,
        triggers=triggers,
        adjustments=tuple(adjustments),
    )
    if override:
        decision = apply_override(decision, override, policy)
    return _checked(decision, policy)


def _checked(decision: Decision, policy: Policy) -> Decision:
    """The decision with ``escalation_allowed`` and ``no_agent_file`` worked
    out from the policy (after overrides and escalations)."""
    profiles = agent_profiles(policy)
    missing = tuple(
        role
        for role in ROLES
        if getattr(decision, role) is not None
        and getattr(decision, role) not in profiles[role]
    )
    top = _ladder_rank(decision.coder, policy.ladder) >= len(policy.ladder) - 1
    return replace(
        decision,
        escalation_allowed=decision.steps < policy.max_steps and not top,
        no_agent_file=missing,
    )


def apply_override(decision: Decision, override: Override, policy: Policy) -> Decision:
    if override.is_empty():
        return decision
    if override.role not in ROLES:
        raise RouterError(f"unknown role {override.role!r}; use one of {ROLES}")
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
    if override.model is not None or override.effort is not None:
        model, effort = split_profile(
            getattr(decision, override.role) or decision.coder
        )
        profile = f"{override.model or model}/{override.effort or effort}"
        changes[override.role] = profile
        notes.append(f"{override.role}={profile}")
    for role in ("planner", "reviewer"):
        switch = getattr(override, role)
        if switch is None:
            continue
        if role in changes:
            if not switch:
                raise RouterError(f"cannot both set and skip the {role}")
            continue
        changes[role] = (getattr(decision, role) or decision.coder) if switch else None
        notes.append(f"{role}={'on' if switch else 'off'}")
    if "reviewer" in changes:
        changes["reviewer_skipped"] = changes["reviewer"] is None
    if (
        decision.architecture_review
        and "reviewer" in changes
        and not changes["reviewer"]
    ):
        changes["architecture_review"] = False
        notes.append(f"architecture review skipped at {decision.level}")
    return replace(decision, **changes, overrides=decision.overrides + tuple(notes))


# Escalation


def _ladder_rank(profile: str, ladder: tuple[str, ...]) -> int:
    """The position of a profile; an off-ladder profile ranks by its model."""
    if profile in ladder:
        return ladder.index(profile)
    model = split_profile(profile)[0]
    same_model = [i for i, step in enumerate(ladder) if split_profile(step)[0] == model]
    return max(same_model) if same_model else len(ladder)


def _weaker_than(profile: str | None, target: str) -> bool:
    """Whether a role at ``profile`` (``None`` = off) is below ``target``."""
    if profile is None:
        return True
    if profile in ALLOWED_PROFILES:
        return _allowed_rank(profile) < _allowed_rank(target)
    # An override profile such as opus/max or sonnet/xhigh: compare models.
    return split_profile(profile)[0] != split_profile(target)[0]


def escalate(
    decision: Decision, reason: str, evidence: str, policy: Policy
) -> Decision:
    """The decision with the coder one step up the ladder; RouterError when
    not allowed. Reaching Opus turns the reviewer on at the Opus reviewer
    profile, unless the user skipped the reviewer."""
    if reason in NEVER_ESCALATE or reason in policy.non_escalating_reasons:
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
    current = _ladder_rank(decision.coder, policy.ladder)
    above = [step for step in policy.ladder if policy.ladder.index(step) > current]
    if not above:
        raise RouterError(
            f"{decision.coder} is already the strongest routed profile: "
            "stop and ask the user"
        )
    next_model, next_effort = split_profile(above[0])
    current_model, current_effort = split_profile(decision.coder)
    record = {
        "role": "coder",
        "current_model": current_model,
        "current_effort": current_effort,
        "reason": reason,
        "evidence": evidence.strip(),
        "next_model": next_model,
        "next_effort": next_effort,
    }
    reviewer = decision.reviewer
    if (
        next_model == "opus"
        and not decision.reviewer_skipped
        and _weaker_than(reviewer, policy.opus_reviewer)
    ):
        reviewer = policy.opus_reviewer
    return _checked(
        replace(
            decision,
            coder=above[0],
            reviewer=reviewer,
            steps=decision.steps + 1,
            history=decision.history + (record,),
        ),
        policy,
    )


# Subagent files

ROLE_TOOLS = {
    "planner": "Read, Grep, Glob, Bash",
    "coder": None,
    "reviewer": "Read, Grep, Glob, Bash",
}
STATE_SECTIONS = """\
Read only the "Current state", "Next task" and "Invariants" sections of
PROJECT_STATE.md (the task history lives in CHANGELOG.md)."""
GIT_SAFETY = (
    "Never run git commands that change the working tree, index, branches or\n"
    "stashes (stash, checkout, reset, restore, clean, commit, push); read-only git\n"
    "(status, diff, log, show) only."
)
ROLE_TEXT = {
    "planner": """\
You are the planner ({profile}) for a task the task router sent to you.

Read CLAUDE.md, the task's row in TASK_STATUS.md and the code the task
touches. {state} Do not edit files. {git}

Return:
1. The routing criteria you measured, as JSON for tools/task_router.py, the
   risk profile fields you can judge better than the criteria (reasoning,
   architecture, regression, migration and provider risk, uncertainty), each
   with one line of evidence, and whether the routing still fits.
2. Open decisions the Prompt Pack does not settle, each with 3-4 real
   options and a recommendation. The main session asks the user; you never
   decide them.
3. A step-by-step implementation plan: files to create or change, tests to
   add, migrations (forward-only, never edit an applied one), state files to
   update.
4. Risks and what must not change (behaviour of finished tasks, provider
   contracts, strategy, migration history).
5. The affected tests to run: the new test files, the existing test files the
   change can touch and the related regression, each with the reason, and
   whether a full suite run or the migration chain (tests/test_migrations.py,
   tests/test_bootstrap.py and the upgrade tests) is justified, and why.
""",
    "coder": """\
You are the coder ({profile}) for a task the task router sent to you.

{state}

Follow the plan and the user's decisions you are given exactly; do not widen
the scope or take architecture decisions on your own. Write code that reads
like the surrounding code. Stack: Python 3.11, uv, pytest, ruff (no type
checker). Never edit an applied migration, delete tests or lower coverage.

Tests (smart test execution): while you iterate, run the single failing test
id first, then the new tests, then the affected tests, then the related
regression. Do not run the full suite (`uv run python -m pytest`) unless the
main session's brief explicitly asks for it: the main session owns the one
justified full suite per task (core or shared code, a database or migration
change, an API contract, architecture, test infrastructure, a dependency, many
modules, before a checkpoint, or the end of a high-risk task). If the brief
asks for it, run it at most once and never after every fix. Do not re-run
passing tests a change cannot affect. Normal tests must not run the migration
chain: use the `database` template fixture from tests/conftest.py; only
migration and upgrade tests migrate a fresh file. Never skip or delete tests,
weaken an assertion or mock an integration a test needs.

Before you report, run `uv run python -m ruff check .` and
`uv run python -m ruff format --check .`, then report the files changed, every
failure with its output, and a Tests block: new tests, affected tests,
regression, full suite, migration chain, each marked RUN (with the count) or
NOT RUN (with the reason); by default the full suite is "NOT RUN (main session
owns it)".

{git}

Fix round: when you are given review findings, you get only the findings,
the user's decisions and the file list. Fix exactly those findings under the
same test rules: run the single failing test id and the tests of the finding,
the full suite only if the brief asks for it (the main session owns it), ruff
once, and report the same Tests block.

If a failure needs deeper reasoning than you can give (a logic or design
problem, not a typo, lint, format, simple import, type, test fixture or
environment error), stop and report it with the evidence so the main session
can escalate. Do not commit or push.
""",
    "reviewer": """\
You are the reviewer ({profile}) for a task the task router sent to you.

Review the uncommitted changes (`git diff`, `git status`) against the task,
the user's decisions and the plan. {state} Do not edit files. {git}

Check: correctness and edge cases, that finished tasks keep their behaviour,
provider contracts and migration history are unchanged, and tests cover the
new rules. Do not re-run the full gate (pytest, ruff check, ruff format
--check, uv build): the main session runs it. Run targeted tests only, by
test id or file, where they settle a finding; never the full suite or the
migration chain.

Architecture review, only when the dispatch prompt asks for it (ARCHITECTURAL
tasks): also review module boundaries and dependencies
(docs/ARCHITECTURE.md), contracts other tasks rely on, and what the decision
means for later Prompt Pack tasks.

Answer PASS, or FAIL with each finding (file:line, problem, evidence), its
severity (blocking, should-fix or minor) and whether it needs escalation
(logic or design problem) or only a fix at the current profile.
""",
}


def agent_profiles(policy: Policy) -> dict[str, list[str]]:
    """Every profile the policy can route each role to, weakest first: level
    defaults, Opus trigger profiles, effort bumps and every coder ladder step."""
    found: dict[str, set[str]] = {role: set() for role in ROLES}
    for rule in policy.levels.values():
        found["coder"].add(rule.coder)
        if rule.planner:
            found["planner"].add(rule.planner)
        if rule.reviewer:
            found["reviewer"].add(rule.reviewer)
        if rule.risk_reviewer:
            found["reviewer"].add(rule.risk_reviewer)
        if rule.opus_trigger_profile:
            found["coder"].add(rule.opus_trigger_profile)
            found["reviewer"].add(policy.opus_reviewer)
            if rule.planner:
                found["planner"].add(rule.opus_trigger_profile)
        if rule.risk_effort_bump:
            found["coder"].add(policy.effort_step(rule.coder))
    found["coder"].update(policy.ladder)
    if any(split_profile(step)[0] == "opus" for step in policy.ladder):
        found["reviewer"].add(policy.opus_reviewer)
    return {role: sorted(found[role], key=_allowed_rank) for role in ROLES}


def agent_specs(policy: Policy) -> dict[str, str]:
    """File name -> content of every routed subagent file."""
    files = {}
    for role, profiles in agent_profiles(policy).items():
        for profile in profiles:
            name = agent_name(role, profile)
            model, effort = split_profile(profile)
            body = ROLE_TEXT[role].format(
                profile=profile, state=STATE_SECTIONS, git=GIT_SAFETY
            )
            front = [
                "---",
                f"name: {name}",
                f"description: Routed {role} at {profile} (task router). Use only "
                f"when the routing decision names {role} {profile}; see "
                "docs/TASK_ROUTER.md.",
                f"model: {model}",
                f"effort: {effort}",
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


# Telemetry (append-only; counts and decisions only, never prompts or answers)

AGENT_ROLES = ("planner", "coder", "coder-fix", "reviewer")
OUTCOMES = ("completed", "failed", "stopped")
PASS_FAIL = ("pass", "fail")
SEVERITIES = ("blocking", "should_fix", "minor")
COMMON_FIELDS = {"kind", "at", "backfilled", "estimated"}


def _is_count(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _is_text(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _is_criteria(value: Any) -> bool:
    try:
        _check_criteria(value)
    except (RouterError, TypeError, AttributeError):
        return False
    return isinstance(value, dict)


def _is_decision(value: Any) -> bool:
    try:
        Decision.from_dict(value)
    except (RouterError, TypeError, ValueError):
        return False
    return isinstance(value, dict)


def _is_findings(value: Any) -> bool:
    return (
        isinstance(value, dict)
        and set(value) <= set(SEVERITIES)
        and all(_is_count(count) for count in value.values())
    )


def _event_schema(
    policy: Policy,
) -> dict[str, tuple[dict[str, Callable[[Any], bool]], dict[str, Callable]]]:
    """Kind -> (required fields, optional fields), each with its check."""

    def is_profile(value: Any) -> bool:
        try:
            model, effort = split_profile(value)
        except RouterError:
            return False
        return model in policy.supported_models and effort in policy.supported_efforts

    def is_risk_profile(value: Any) -> bool:
        if not isinstance(value, dict):
            return False
        try:
            risk_profile({}, value)
        except RouterError:
            return False
        return True

    task = {"task": _is_text}
    return {
        "route": (
            {
                **task,
                "policy_version": lambda v: v in (1, 2),
                "criteria": _is_criteria,
                "decision": _is_decision,
            },
            {"risk_profile": is_risk_profile, "prior_review_issue": _is_text},
        ),
        "agent_run": (
            {
                **task,
                "role": lambda v: v in AGENT_ROLES,
                "profile": is_profile,
                "tokens": _is_count,
                "tool_uses": _is_count,
                "duration_ms": _is_count,
                "outcome": lambda v: v in OUTCOMES,
            },
            {},
        ),
        "escalation": (
            {
                **task,
                "role": lambda v: v in ROLES,
                "current_model": lambda v: v in policy.supported_models,
                "current_effort": lambda v: v in policy.supported_efforts,
                "reason": lambda v: v in policy.escalating_reasons,
                "evidence": _is_text,
                "next_model": lambda v: v in policy.supported_models,
                "next_effort": lambda v: v in policy.supported_efforts,
            },
            {},
        ),
        "gate": (
            {
                **task,
                "tests_passed": _is_count,
                "tests_failed": _is_count,
                "ruff_check": lambda v: v in PASS_FAIL,
                "ruff_format": lambda v: v in PASS_FAIL,
                "build": lambda v: v in PASS_FAIL,
            },
            {},
        ),
        "result": (
            {
                **task,
                "result": lambda v: v in PASS_FAIL,
                "tests": _is_count,
                "findings": _is_findings,
            },
            {},
        ),
    }


def validate_event(event: Any, policy: Policy) -> dict[str, Any]:
    """The event with ``at`` filled in; RouterError for anything invalid."""
    if not isinstance(event, dict):
        raise RouterError("an event is a JSON object")
    schema = _event_schema(policy)
    kind = event.get("kind")
    if kind not in schema:
        raise RouterError(
            f"unknown event kind {kind!r}; use one of {', '.join(schema)}"
        )
    required, optional = schema[kind]
    extra = set(event) - COMMON_FIELDS - set(required) - set(optional)
    if extra:
        raise RouterError(f"{kind}: unknown fields {sorted(extra)}")
    missing = set(required) - set(event)
    if missing:
        raise RouterError(f"{kind}: missing fields {sorted(missing)}")
    for name, check in {**required, **optional}.items():
        if name in event and not check(event[name]):
            raise RouterError(f"{kind}.{name}: invalid value {event[name]!r}")
    for flag in ("backfilled", "estimated"):
        if flag in event and not isinstance(event[flag], bool):
            raise RouterError(f"{kind}.{flag} must be true or false")
    event = dict(event)
    at = event.setdefault("at", datetime.now(UTC).isoformat(timespec="seconds"))
    try:
        datetime.fromisoformat(at)
    except (TypeError, ValueError) as error:
        raise RouterError(f"{kind}.at must be an ISO date-time") from error
    ordered = {"kind": kind, "task": event["task"]}
    ordered.update(event)
    return ordered


def route_event(
    decision: Decision,
    criteria: dict[str, Any],
    policy: Policy,
    profile: dict[str, Any] | None = None,
    prior_review_issue: str | None = None,
) -> dict[str, Any]:
    event: dict[str, Any] = {
        "kind": "route",
        "task": decision.task,
        "policy_version": policy.schema_version,
        "criteria": criteria,
        "decision": decision.to_dict(),
    }
    if profile:
        event["risk_profile"] = profile
    if prior_review_issue:
        event["prior_review_issue"] = prior_review_issue
    return event


def record_event(
    event: dict[str, Any], policy: Policy, path: Path = TELEMETRY_PATH
) -> dict[str, Any]:
    """Validate ``event`` and append it as one JSON line; never rewrites."""
    event = validate_event(event, policy)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as file:
        file.write(json.dumps(event, ensure_ascii=False) + "\n")
    return event


def _is_whole(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


# The fields ``report`` and ``compare`` read from a stored event, per kind.
# Anything else may come from another policy version and is not checked.
READ_FIELDS: dict[str, dict[str, Callable[[Any], bool]]] = {
    "route": {
        "policy_version": _is_whole,
        "criteria": lambda v: isinstance(v, dict),
        "decision": lambda v: isinstance(v, dict),
    },
    "agent_run": {
        "role": _is_text,
        "profile": _is_text,
        "tokens": _is_count,
        "duration_ms": _is_count,
    },
    "escalation": {},
    "gate": {"tests_failed": _is_count},
    "result": {
        "result": _is_text,
        "findings": lambda v: (
            isinstance(v, dict) and all(_is_count(c) for c in v.values())
        ),
    },
}


def _stored_event(event: Any) -> dict[str, Any]:
    """A stored event whose stable envelope (kind, task, at and the fields the
    report reads) is usable; RouterError otherwise. Values that only another
    policy version knows (a reason, a model, a field) are kept."""
    if not isinstance(event, dict):
        raise RouterError("an event is a JSON object")
    kind = event.get("kind")
    if not _is_text(kind) or not _is_text(event.get("task")):
        raise RouterError("an event needs a kind and a task")
    try:
        datetime.fromisoformat(event.get("at"))
    except (TypeError, ValueError) as error:
        raise RouterError(f"{kind}.at must be an ISO date-time") from error
    for name, check in READ_FIELDS.get(kind, {}).items():
        if name not in event or not check(event[name]):
            raise RouterError(f"{kind}.{name}: missing or invalid")
    return event


def read_events(path: Path = TELEMETRY_PATH) -> list[dict[str, Any]]:
    """The stored events. Events are validated strictly when written; a read
    checks only the stable envelope, so a later policy change never breaks
    ``report`` or ``compare``. A malformed line is skipped with a warning."""
    if not path.exists():
        return []
    events = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            events.append(_stored_event(json.loads(line)))
        except (RouterError, json.JSONDecodeError) as error:
            print(
                f"task_router: {path.name} line {number} skipped: {error}",
                file=sys.stderr,
            )
    return events


def _table(header: list[str], rows: list[list[str]]) -> list[str]:
    widths = [
        max(len(str(cell)) for cell in column)
        for column in zip(header, *rows, strict=False)
    ]

    def line(cells: Any) -> str:
        return "  ".join(cells).rstrip()

    return [
        line(cell.ljust(width) for cell, width in zip(header, widths, strict=True)),
        line("-" * width for width in widths),
        *(
            line(
                str(cell).ljust(width) for cell, width in zip(row, widths, strict=True)
            )
            for row in rows
        ),
    ]


def _findings_text(findings: dict[str, int]) -> str:
    return "/".join(str(findings.get(severity, 0)) for severity in SEVERITIES)


def report(events: list[dict[str, Any]]) -> str:
    """Per task and per profile: tokens, duration, agent runs, escalations,
    test failures, review findings and results."""
    tasks: dict[str, dict[str, Any]] = {}
    profiles: dict[str, dict[str, Any]] = {}
    for event in events:
        if event["kind"] not in READ_FIELDS:  # a kind of a later policy
            continue
        row = tasks.setdefault(
            event["task"],
            {
                "level": "-",
                "routed": "-",
                "runs": 0,
                "tokens": 0,
                "duration": 0,
                "escalations": 0,
                "failures": 0,
                "findings": None,
                "result": "-",
                "coders": set(),
                "backfilled": False,
            },
        )
        row["backfilled"] |= bool(event.get("backfilled"))
        kind = event["kind"]
        if kind == "route":
            try:
                decision = Decision.from_dict(event["decision"])
            except RouterError:  # a decision of another policy version
                row["level"] = str(event["decision"].get("level", "-"))
                row["routed"] = "?"
            else:
                row["level"] = decision.level
                row["routed"] = (
                    f"{decision.planner or '-'} / {decision.coder} / "
                    f"{decision.reviewer or '-'}"
                )
        elif kind == "agent_run":
            row["runs"] += 1
            row["tokens"] += event["tokens"]
            row["duration"] += event["duration_ms"]
            if event["role"] in ("coder", "coder-fix"):
                row["coders"].add(event["profile"])
            usage = profiles.setdefault(
                event["profile"], {"runs": 0, "tokens": 0, "duration": 0}
            )
            usage["runs"] += 1
            usage["tokens"] += event["tokens"]
            usage["duration"] += event["duration_ms"]
        elif kind == "escalation":
            row["escalations"] += 1
        elif kind == "gate":
            row["failures"] += event["tests_failed"]
        elif kind == "result":
            row["result"] = event["result"]
            row["findings"] = event["findings"]
    quality: dict[str, dict[str, int]] = {}
    for row in tasks.values():
        for profile in row["coders"]:
            totals = quality.setdefault(
                profile,
                {"tasks": 0, "escalations": 0, "failures": 0, "pass": 0, "fail": 0}
                | dict.fromkeys(SEVERITIES, 0),
            )
            totals["tasks"] += 1
            totals["escalations"] += row["escalations"]
            totals["failures"] += row["failures"]
            if row["result"] in PASS_FAIL:
                totals[row["result"]] += 1
            for severity in SEVERITIES:
                totals[severity] += (row["findings"] or {}).get(severity, 0)
    lines = ["Per task (findings = blocking/should-fix/minor; * = backfilled)"]
    lines += _table(
        [
            "Task",
            "Level",
            "Routed planner / coder / reviewer",
            "Runs",
            "Tokens",
            "Duration s",
            "Escal.",
            "Test fails",
            "Findings",
            "Result",
        ],
        [
            [
                name + ("*" if row["backfilled"] else ""),
                row["level"],
                row["routed"],
                str(row["runs"]),
                f"{row['tokens']:,}",
                f"{row['duration'] / 1000:,.0f}",
                str(row["escalations"]),
                str(row["failures"]),
                "-" if row["findings"] is None else _findings_text(row["findings"]),
                row["result"],
            ]
            for name, row in tasks.items()
        ],
    )
    lines += ["", "Per profile (quality counts the tasks whose coder ran at it)"]
    names = sorted(
        set(profiles) | set(quality),
        key=lambda p: (_allowed_rank(p) if p in ALLOWED_PROFILES else 99, p),
    )
    rows = []
    for name in names:
        usage = profiles.get(name, {"runs": 0, "tokens": 0, "duration": 0})
        totals = quality.get(name, {})
        rows.append(
            [
                name,
                str(usage["runs"]),
                f"{usage['tokens']:,}",
                f"{usage['duration'] / 1000:,.0f}",
                str(totals.get("tasks", 0)),
                str(totals.get("escalations", 0)),
                str(totals.get("failures", 0)),
                _findings_text(totals) if totals else "-",
                f"{totals.get('pass', 0)}/{totals.get('fail', 0)}",
            ]
        )
    lines += _table(
        [
            "Profile",
            "Runs",
            "Tokens",
            "Duration s",
            "Coder tasks",
            "Escal.",
            "Test fails",
            "Findings",
            "Pass/fail",
        ],
        rows,
    )
    return "\n".join(lines)


def compare(events: list[dict[str, Any]], policy: Policy) -> str:
    """The routing of every recorded task under the v1 policy and the current
    one, recomputed from the recorded criteria (and risk profile)."""
    latest: dict[str, dict[str, Any]] = {}
    for event in events:
        if event["kind"] == "route":
            latest[event["task"]] = event
    legacy = parse_policy(LEGACY_V1_POLICY)

    def roles(decision: Decision) -> str:
        return (
            f"{decision.planner or '-'} / {decision.coder} / {decision.reviewer or '-'}"
        )

    rows = []
    for name, event in latest.items():
        try:
            old = route(name, event["criteria"], legacy)
            new = route(
                name,
                event["criteria"],
                policy,
                profile=event.get("risk_profile"),
                prior_review_issue=event.get("prior_review_issue"),
            )
        except (RouterError, TypeError, AttributeError) as error:
            # Criteria or a risk profile of another policy version.
            rows.append([name, "-", "-", "-", "-", f"not comparable: {error}"])
            continue
        rows.append(
            [
                name,
                str(new.score),
                new.level,
                roles(old),
                roles(new),
                ", ".join(t["name"] for t in new.triggers) or "none",
            ]
        )
    return "\n".join(
        [
            "Routing per task: v1 policy vs current policy "
            "(planner / coder / reviewer)",
            *_table(["Task", "Score", "Level", "v1", "current", "Opus triggers"], rows),
        ]
    )


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
    parser.add_argument("--telemetry", type=Path, default=TELEMETRY_PATH)
    commands = parser.add_subparsers(dest="command", required=True)
    route_cmd = commands.add_parser("route")
    route_cmd.add_argument("--task", required=True)
    route_cmd.add_argument("--criteria", required=True, help="JSON or a JSON file")
    route_cmd.add_argument("--profile", help="risk profile: JSON or a JSON file")
    route_cmd.add_argument("--prior-review-issue")
    route_cmd.add_argument("--model")
    route_cmd.add_argument("--effort")
    route_cmd.add_argument("--role", default="coder", choices=ROLES)
    route_cmd.add_argument("--skip-planner", action="store_true")
    route_cmd.add_argument("--skip-reviewer", action="store_true")
    route_cmd.add_argument("--json", action="store_true")
    route_cmd.add_argument("--record", action="store_true")
    escalate_cmd = commands.add_parser("escalate")
    escalate_cmd.add_argument("--decision", required=True, help="JSON or a file")
    escalate_cmd.add_argument("--reason", required=True)
    escalate_cmd.add_argument("--evidence", required=True)
    escalate_cmd.add_argument("--record", action="store_true")
    record_cmd = commands.add_parser("record")
    record_cmd.add_argument("--event", required=True, help="JSON or a JSON file")
    commands.add_parser("report")
    commands.add_parser("compare")
    commands.add_parser("sync-agents")
    commands.add_parser("check-agents")
    args = parser.parse_args(argv)
    try:
        policy = load_policy(args.policy)
        if args.command == "route":
            override = Override(
                model=args.model,
                effort=args.effort,
                role=args.role,
                planner=False if args.skip_planner else None,
                reviewer=False if args.skip_reviewer else None,
            )
            criteria = _json_arg(args.criteria)
            profile = _json_arg(args.profile) if args.profile else None
            decision = route(
                args.task,
                criteria,
                policy,
                override,
                profile=profile,
                prior_review_issue=args.prior_review_issue,
            )
            if args.record:
                record_event(
                    route_event(
                        decision, criteria, policy, profile, args.prior_review_issue
                    ),
                    policy,
                    args.telemetry,
                )
            print(
                json.dumps(decision.to_dict(), indent=2)
                if args.json
                else decision.render()
            )
        elif args.command == "escalate":
            decision = Decision.from_dict(_json_arg(args.decision))
            decision = escalate(decision, args.reason, args.evidence, policy)
            if args.record:
                record_event(
                    {
                        "kind": "escalation",
                        "task": decision.task,
                        **decision.history[-1],
                    },
                    policy,
                    args.telemetry,
                )
            print(json.dumps(decision.to_dict(), indent=2))
        elif args.command == "record":
            event = record_event(_json_arg(args.event), policy, args.telemetry)
            print(f"recorded {event['kind']} for {event['task']}")
        elif args.command == "report":
            print(report(read_events(args.telemetry)))
        elif args.command == "compare":
            print(compare(read_events(args.telemetry), policy))
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
