# Task / Model Router

Development-workflow infrastructure for this repository (added 2026-10-03,
after F-067). It is **not** part of the YouTube Agent runtime: nothing in
`src/` imports it and `uv build` does not ship it.

> **Rule: route every task before you implement it.**
> Use the cheapest model that is reasonably capable of completing the task
> safely.

| Piece | Path | Role |
|---|---|---|
| Policy (single source of truth) | `.claude/task-router.json` | model, effort, planner, reviewer per level; escalation limits and reasons |
| Router | `tools/task_router.py` | classify criteria → level → decision; escalate; generate and check subagent files |
| Subagents (generated) | `.claude/agents/task-*.md` | planner / coder / reviewer per level, with `model` and `effort` in frontmatter |
| Tests | `tests/test_task_router.py` | classification, policy, overrides, escalation, agent files in sync |

## What Claude Code can and cannot do here

Checked on Claude Code 2.1.288:

- The **main session's** model and effort are set only by the user
  (`/model`, `/effort`, `--model`, `--effort`, `"model"` in settings; the user
  setting is `opus`). Claude cannot change them mid-session, so the router
  never pretends to.
- A **subagent** gets `model` and `effort` from its file's frontmatter
  (`model: haiku|sonnet|opus|inherit`, `effort: low|medium|high|xhigh|max`).
  The Agent tool can override the model per call, but **not the effort**,
  so there is one generated file per role and level.
- So the main session (the user's Opus) routes, asks the user, verifies and
  commits; the routed subagents do the planning, coding and reviewing at the
  routed model and effort.
- New or changed agent files may only be picked up by a new session. If a
  `task-*` agent is not listed, restart the session or use the fallback below.

## Classification

Never classify by the task name. Measure these criteria (from the Prompt
Pack line, the code the task touches, and the planner's findings):

| Criterion | Values (lowest first) |
|---|---|
| `files_affected`, `modules_affected`, `dependency_depth` | whole numbers |
| `behavior_change` | false / true |
| `architecture_impact` | none, local, cross_module, architectural |
| `database_change`, `migration_required` | false / true |
| `provider_change` | none, logic, contract |
| `concurrency_risk`, `failure_recovery_complexity` | none, low, high |
| `test_complexity` | low, medium, high |
| `backward_compatibility_risk` | none, low, high |
| `uncertainty`, `rollback_risk` | low, medium, high |

**Score** (fixed weights in `WEIGHTS` / `_size_points`): files 3-5 +1, 6-10 +2,
>10 +3; modules 2 +1, 3-4 +2, ≥5 +3; depth 2 +1, ≥3 +2; architecture local +1,
cross_module +3, architectural +6; database +1; migration +1; provider logic
+1, contract +4; concurrency low +1, high +3; failure/recovery low +1, high +3;
tests medium +1, high +2; backward compatibility low +1, high +2; rollback
medium +1, high +2. `uncertainty` adds nothing (see fail-safe).

**Level by score:** 0 → TRIVIAL (SIMPLE if behaviour changes), 1-2 → SIMPLE,
3-8 → NORMAL, 9-14 → COMPLEX, ≥15 → ARCHITECTURAL.

**Floors** (raise the level, never lower it):

| Condition | At least |
|---|---|
| `architecture_impact = architectural` or `provider_change = contract` | ARCHITECTURAL |
| high concurrency risk, high failure/recovery, cross-module impact, or database + provider + ≥2 modules | COMPLEX |
| a migration, or provider logic | NORMAL |
| any behaviour change | SIMPLE |

Many files alone never mean Opus: a formatting pass over 60 files is still
cheap. Typical meaning of the levels:

- **TRIVIAL**: typo, small rename, docs, format, trivial test, no behaviour change.
- **SIMPLE**: small bug, extra tests, small CRUD, one module, local refactor.
- **NORMAL**: ordinary feature over several files, small migration, ordinary provider logic.
- **COMPLEX**: several related modules, database + domain + provider, failure/recovery, concurrency, cache, hard to test fully.
- **ARCHITECTURAL**: architecture, large contract/interface, persistence model or agent/provider architecture changes; decisions that shape many later tasks.

## Model, effort, planner and reviewer policy

Default `.claude/task-router.json`:

| Level | Model | Effort | Flow |
|---|---|---|---|
| TRIVIAL | haiku | low | coder → main session verifies |
| SIMPLE | sonnet | low | coder → tests |
| NORMAL | sonnet | medium | planner → coder → tests |
| COMPLEX | opus | high | planner → coder → reviewer → tests |
| ARCHITECTURAL | opus | high | planner → coder → reviewer (+ architecture review) → tests |

- The router never chooses `xhigh` or `max`; the policy file is refused if a
  level uses them. Only a user override can.
- **Changing the policy:** edit `.claude/task-router.json` only (for example
  set TRIVIAL to `sonnet`), then run
  `uv run python tools/task_router.py sync-agents` and the tests.
  `test_the_subagent_files_match_the_policy` fails if the files drift.

## Workflow for a Prompt Pack task ("làm F-068")

1. Read `PROJECT_STATE.md` / `TASK_STATUS.md` and look at the code the task
   touches, only as far as routing needs.
2. Write the criteria as JSON and run
   `uv run python tools/task_router.py route --task "F-068 Claim Extractor" --criteria <json or file>`.
   Show the routing decision to the user before implementing.
3. **Planner** (if routed): dispatch `task-planner-<level>`. It returns the
   measured criteria, the open decisions (3-4 options each) and a plan. If its
   criteria give another level, re-route.
4. Ask the user every open decision in the main session (subagents cannot
   ask the user). Do not decide architecture on your own.
5. **Coder**: dispatch `task-coder-<level>` with the plan and the user's
   decisions. TRIVIAL work that is a one-line edit may be done directly in
   the main session when spinning up an agent would cost more; say so in the
   report.
6. Verify in the main session: full `pytest`, `ruff check .`,
   `ruff format --check .`, `uv build`. Never trust a report without running
   them.
7. **Reviewer** (if routed): dispatch `task-reviewer-<level>`.
8. On failure: classify it (below); fix at the same level or escalate.
9. Update the state files, commit (no push), and report, including the
   routing decision and every escalation.

## Escalation

Escalate only with a reason and evidence:

- **Escalating reasons:** `logic_failure`, `design_gap`,
  `repeated_test_failure` (the same failure after a fix attempt),
  `insufficient_reasoning`.
- **Never escalate for:** `typo`, `lint`, `format`, `simple_import`,
  `environment`, `flaky_unrelated`. Fix those at the current level.

`uv run python tools/task_router.py escalate --decision decision.json --reason logic_failure --evidence "test_x: expected 3, got 2"`
returns the decision one step up the ladder built from the policy
(haiku/low → sonnet/low → sonnet/medium → opus/high) and appends
`{current, reason, evidence, next}` to its history. Escalating to Opus turns
the reviewer on. At most `max_steps` (2) escalations per task, and never above
the strongest routed profile (opus/high). After that, **stop and ask the
user**. Continue with the coder agent of the new profile (for example
opus/high → `task-coder-complex`).

## Fail-safe

- Unknown criteria count as their lowest value and are listed; together with
  `uncertainty = high` they mark the decision **uncertain**. The router keeps
  the lower level and relies on escalation, and never picks `xhigh`.
- If a routed agent is not available in the session, use the Agent tool with
  the routed `model` on a general-purpose agent and say that its effort
  follows the session. Or do the task in the main session and tell the user
  the recommended `/model` and `/effort`.
- If the router itself fails (bad policy or criteria), fix the input. Do not
  guess a level.

## User override

User words win unless they break a technical or safety rule of the project:

| User says | Router option |
|---|---|
| "use opus" / "use sonnet" / "use haiku" | `--model opus` / `sonnet` / `haiku` |
| "do this with high effort" (or low/medium/xhigh/max) | `--effort high` (any supported effort, including xhigh/max) |
| "skip planner" / "skip reviewer" | `--skip-planner` / `--skip-reviewer` |
| "no subagents" / "làm trực tiếp" | do it in the main session; recommend `/model` and `/effort` |

Overrides are recorded in the decision. An unsupported model or effort is
refused. No override skips the green gate, approval questions, the
migration rules or the push rule.

## Examples

| Task | Level | Model / effort |
|---|---|---|
| Fix one failing assertion | SIMPLE | sonnet / low |
| Rename a property across 3 files | SIMPLE | sonnet / low |
| Ordinary feature, 4 files, small migration | NORMAL | sonnet / medium |
| Debug race condition in Research Failure Recovery | COMPLEX (floor: concurrency) | opus / high |
| Design a new persistence architecture | ARCHITECTURAL | opus / high |

```text
Task: F-068 Claim Extractor
Complexity: COMPLEX
Reasons:
- floor: database + domain + provider together
- ...
Model: opus
Effort: high
Planner: YES
Reviewer: YES
Escalation: Allowed (0 used)
```

(The F-068 lines above illustrate the output format only; the real decision
comes from the measured criteria.)
