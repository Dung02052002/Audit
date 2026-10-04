# Task / Model Router (v2, cost-aware)

Development-workflow infrastructure for this repository (added 2026-10-03
after F-067; made cost-aware by TOOL-002 after F-069). It is **not** part of
the YouTube Agent runtime: nothing in `src/` imports it and `uv build` does
not ship it. It has no TASK_STATUS row.

> **Rule: route every task before you implement it.**
> Use the cheapest model that is capable for each role. Pay for Opus only
> when evidence demands it. Never drop tests, the green gate or safety to
> save money.

| Piece | Path | Role |
|---|---|---|
| Policy (single source of truth) | `.claude/task-router.json` | profile per role and level, Opus trigger profiles, escalation ladder, limits and reasons |
| Router | `tools/task_router.py` | classify → level; risk profile → triggers → per-role profiles; overrides; escalate; agent files; telemetry |
| Subagents (generated) | `.claude/agents/task-<role>-<model>-<effort>.md` | one file per role and profile the policy can produce |
| Telemetry (committed) | `.claude/router-telemetry.jsonl` | append-only events: route, agent_run, escalation, gate, result |
| Tests | `tests/test_task_router.py` | classification, per-role policy, triggers, risk bumps, overrides, escalation, v1 compatibility, agent files, telemetry |

## What Claude Code can and cannot do here

Checked on Claude Code 2.1.288:

- The **main session's** model and effort are set only by the user
  (`/model`, `/effort`, `--model`, `--effort`, `"model"` in settings; the user
  setting is `opus`). Claude cannot change them mid-session.
- A **subagent** gets `model` and `effort` from its file's frontmatter. The
  Agent tool can override the model per call, but **not the effort**, so
  there is one generated file per role and profile.
- The main session (the user's Opus) routes, asks the user, runs the green
  gate and commits; the routed subagents plan, code and review.
- New or changed agent files are picked up only by a **new session**. After
  `sync-agents` changes the files, restart the session (or use the fail-safe
  below).

## Complexity vs execution

The router answers two separate questions.

1. **Complexity** (unchanged since v1): how big and risky is the task? The
   criteria give a score and floors, and the score gives the level.
2. **Execution**: which model and effort does each role (planner, coder,
   reviewer) get? The level gives a default per role; evidence from the
   risk profile adjusts it; a user override comes last.

A higher level never means Opus by itself. A COMPLEX task with no Opus
trigger is coded on Sonnet/high and reviewed by Opus/high.

### Classification (unchanged)

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
medium +1, high +2. `uncertainty` adds nothing.

**Level by score:** 0 → TRIVIAL (SIMPLE if behaviour changes), 1-2 → SIMPLE,
3-8 → NORMAL, 9-14 → COMPLEX, ≥15 → ARCHITECTURAL.

**Floors** (raise the level, never lower it):

| Condition | At least |
|---|---|
| `architecture_impact = architectural` or `provider_change = contract` | ARCHITECTURAL |
| high concurrency risk, high failure/recovery, cross-module impact, or database + provider + ≥2 modules | COMPLEX |
| a migration, or provider logic | NORMAL |
| any behaviour change | SIMPLE |

## Execution policy (schema_version 2)

Profiles are `model/effort` strings. The policy may only use these six
(weakest first): `haiku/low`, `sonnet/low`, `sonnet/medium`, `sonnet/high`,
`opus/medium`, `opus/high`. A policy with any other profile is refused, and
`xhigh`/`max` are refused with "only allowed as a user override".

| Level | Planner | Coder | Reviewer |
|---|---|---|---|
| TRIVIAL | off | haiku/low | off |
| SIMPLE | off | sonnet/low | off |
| NORMAL | sonnet/medium | sonnet/medium | off; sonnet/high when behaviour changes **and** regression, migration or reasoning risk is high |
| COMPLEX | sonnet/high | sonnet/high | opus/high (always) |
| ARCHITECTURAL | opus/high | opus/high | opus/high + architecture review (mandatory unless the user explicitly skips the reviewer, which is recorded) |

Per level the policy holds `planner`, `coder`, `reviewer` (null = off),
`risk_reviewer` (the conditional NORMAL reviewer), `opus_trigger_profile`
(NORMAL opus/medium, COMPLEX opus/high), `risk_effort_bump` (SIMPLE,
NORMAL) and `architecture_review`; at the top level `opus_reviewer`
(opus/high) and `escalation` (ladder, limits, reasons).

**Effort is chosen independently of the model.** The same Sonnet runs at
medium for NORMAL and high for COMPLEX; an Opus trigger at NORMAL gives
opus/medium, not opus/high.

### Risk profile

Optional input (`--profile` JSON or file). Any missing field is derived from
the criteria and marked `*` (derived) in the output:

| Field | Values | Derived from the criteria |
|---|---|---|
| `reasoning_risk` | low/medium/high | worst of `concurrency_risk` and `failure_recovery_complexity`: none → low, low → medium, high → high |
| `architecture_risk` | low/medium/high | `architecture_impact`: none/local → low, cross_module → medium, architectural → high |
| `regression_risk` | low/medium/high | `backward_compatibility_risk` high → high; low, or any `behavior_change` → medium; else low |
| `migration_risk` | low/medium/high | no migration → low; `migration_required` → medium; with `rollback_risk` high → high |
| `provider_risk` | low/medium/high | `provider_change`: none → low, logic → medium, contract → high |
| `uncertainty` | low/medium/high | `uncertainty` |
| `estimated_files`, `estimated_modules` | whole numbers | `files_affected`, `modules_affected` |
| `estimated_test_complexity` | low/medium/high | `test_complexity` |

The criteria alone are still enough (old calls keep working). Give a risk
field when the planner has better evidence than the criteria, for example
`{"architecture_risk": "high"}` for an open architecture question.

### Opus triggers (evidence, never "because COMPLEX")

At NORMAL and COMPLEX, any trigger lifts the planner and coder to the
level's `opus_trigger_profile` (NORMAL → opus/medium, COMPLEX → opus/high)
and turns the reviewer on at opus/high. The decision lists every trigger
with its evidence:

| Trigger | Fires when |
|---|---|
| `architecture_risk` | `architecture_risk = high` (an unclear architecture decision) |
| `interdependent_modules` | `architecture_impact = cross_module`, or `estimated_modules >= 4` with `dependency_depth >= 3` |
| `reasoning_risk` | `reasoning_risk = high` (high concurrency or failure/recovery) |
| `migration_risk` | `migration_risk = high` |
| `provider_risk` | `provider_risk = high` (provider contract change) |
| `prior_review_issue` | a prior review found a design/reasoning issue (`--prior-review-issue "<evidence>"`) |
| `uncertainty_with_architecture` | `uncertainty = high` together with `architecture_risk` medium or high |

**Uncertainty high alone is not a trigger:** the task starts on Sonnet, the
decision is marked uncertain, and escalation covers a wrong guess.
TRIVIAL/SIMPLE have no trigger profile (a prior review issue there is noted
in the adjustments; escalate if needed), and ARCHITECTURAL already uses Opus.
No trigger prints `Opus triggers: none: Sonnet is enough`.

### Risk without architecture impact

At SIMPLE and NORMAL, when no trigger fired, a high `regression_risk` or
`migration_risk` raises the **coder's effort one step on the same model**
(sonnet/low → sonnet/medium → sonnet/high): high risk with low architecture
impact means Sonnet/high, not Opus. (At NORMAL a high migration risk is
already an Opus trigger.) The adjustment names the actual values, for
example `coder sonnet/low -> sonnet/medium (same model, more effort):
regression_risk=high with architecture_risk=low`.

A level without a trigger profile and without an architecture review
(TRIVIAL, SIMPLE) cannot act on `architecture_risk = high` (given in the
risk profile, alone or with a high regression risk). The decision then
notes that it is not an Opus trigger at that level and asks to **re-measure
the criteria or re-route**, like the prior-review-issue note: an open
architecture question does not fit a SIMPLE task.

### Reviewer budget

- TRIVIAL/SIMPLE: no reviewer; the main session's green gate is the check.
- NORMAL: a sonnet/high reviewer only for behaviour changes with a high
  regression, migration or reasoning risk, and only when no Opus trigger
  fired (a trigger already brings the opus/high reviewer and its evidence
  explains why, so no `reviewer on at sonnet/high` note is added).
- COMPLEX: always an opus/high reviewer, also when the coder is on Sonnet:
  the cheap coder is paid back by a strong review.
- ARCHITECTURAL and any Opus trigger: opus/high reviewer. At ARCHITECTURAL
  the review, with its architecture section, is mandatory unless the user
  explicitly skips the reviewer (recorded, see User override).
- The reviewer does **not** re-run the full gate (the main session runs
  pytest, ruff check, ruff format --check and uv build); it runs targeted
  tests only (by id or file) to settle a finding, never the full suite or the
  migration chain.
- **Architecture review** is a section of the reviewer's prompt file that the
  main session activates by asking for it in the dispatch prompt (ARCHITECTURAL
  tasks). One `task-reviewer-opus-high` file serves COMPLEX and ARCHITECTURAL;
  a separate architecture-reviewer file was not needed.

## Workflow for a Prompt Pack task ("làm F-070")

1. Read `PROJECT_STATE.md` (Current state, Next task, Invariants) and
   `TASK_STATUS.md`, and look at the code the task touches only as far as
   routing needs.
2. Route and log the decision:
   `uv run python tools/task_router.py route --task "F-070 Fact Check Result" --criteria <json|file> [--profile <json|file>] --record`.
   Show the decision (level, score, reasons, per-role profile, triggers,
   risk profile, escalation) to the user before implementing.
3. **Planner** (if on): dispatch `task-planner-<model>-<effort>`. It returns
   measured criteria, risk evidence, open decisions (3-4 options each) and a
   plan. If its criteria or risk profile change the routing, re-route.
4. Ask the user every open decision in the main session.
5. **Coder**: dispatch `task-coder-<model>-<effort>` with the plan and the
   decisions. While iterating it runs the single failing test id, then the
   new tests, the affected tests and the related regression (the planner
   lists them), not the full suite unless the brief explicitly asks for it
   (the main session owns it, see Smart test execution below); ruff before
   reporting. It reports a Tests block, with the full suite "NOT RUN (main
   session owns it)" by default. A one-line TRIVIAL edit may be done in the main session when an
   agent would cost more; say so.
6. Green gate in the main session: `ruff check .` and `ruff format --check .`
   always; `uv build` when packaging-relevant code changed or before a
   checkpoint; the full `pytest` suite at most once per task, when justified
   (see Smart test execution below), stating the reason when it is skipped.
   Never trust a report without checking it. Record it
   (`record --event '{"kind": "gate", ...}'`).
7. **Reviewer** (if on): dispatch `task-reviewer-<model>-<effort>` (add the
   architecture-review request for ARCHITECTURAL).
8. **Fix round**: give the review findings to a **fresh** coder of the
   current profile with only the findings, the user's decisions and the file
   list (not a resumed coder: a resumed agent re-reads its whole history).
   Record it as an `agent_run` with role `coder-fix`.
9. On failure: classify it (below); fix at the same profile or escalate.
10. Record each `agent_run` and the `result`, update the state files, commit
    (no push), and report, including the routing and every escalation.

Agents read only the Current state, Next task and Invariants sections of
`PROJECT_STATE.md`; the per-task history lives in `CHANGELOG.md`.

### Smart test execution (TOOL-003)

The test order is: the single failing test id, the new tests, the affected
tests, the related regression, then (only when justified) the full suite. A
passing test that a change cannot affect is not re-run.

The full suite runs **at most once per task, and the main session owns it**:
a coder or fix-round coder does not run it unless the main session's brief
explicitly asks for it. It runs only for one of these triggers: core or shared code,
a database or migration change, an API contract, architecture, test
infrastructure, a dependency change, many modules, before a checkpoint, or the
end of a high-risk task. It is never run after each fix. When it is skipped
the report says why.

The migration chain (`tests/test_migrations.py`, `tests/test_bootstrap.py`
and the upgrade tests that migrate a fresh file) runs when migrations or the
migration runner changed, and is part of the full suite. Normal tests must
not run it: `tests/conftest.py` migrates a template database once per test
session (read-only) and the `database` fixture, or `database_copy(path)` for
an app started by a test, copies it. A test that needs a fresh file (an
upgrade test, a startup test) migrates its own.

The planner lists the affected tests and whether a full suite or the
migration chain is justified; there is no automatic affected-tests helper.
Every coder and main-session report includes a Tests block: new tests,
affected tests, regression, full suite, migration chain, each RUN (with the
count) or NOT RUN (with the reason); a coder reports the full suite as "NOT RUN
(main session owns it)" by default. The reviewer runs targeted tests only.

### Git safety for subagents

Every planner, coder and reviewer file says: never run git commands that
change the working tree, index, branches or stashes (stash, checkout, reset,
restore, clean, commit, push); read-only git (status, diff, log, show) only.
(A reviewer once ran `git stash` and `git stash pop` on the uncommitted work.)
Commits and pushes stay with the main session, on the user's request.

## Escalation

Ladder (in the policy, validated weakest first):
`haiku/low → sonnet/low → sonnet/medium → sonnet/high → opus/medium → opus/high`.

- **Escalating reasons:** `logic_failure`, `design_gap`,
  `repeated_test_failure` (the same failure after a fix attempt),
  `insufficient_reasoning`. Evidence is required.
- **Never escalate for:** `typo`, `lint`, `format`, `simple_import`, `type`,
  `test_fixture`, `environment`, `flaky_unrelated`. Fix those at the current
  profile. This set is hard-coded (`NEVER_ESCALATE` in
  `tools/task_router.py`): a policy that lists any of them as escalating is
  refused, and `escalate` refuses them even if the policy leaves them out of
  `non_escalating_reasons`.
- **Step limit:** the policy's `max_steps` may be 0, 1 or 2; the code caps it
  at 2 (`MAX_ESCALATION_STEPS`) and refuses a policy with more.

`uv run python tools/task_router.py escalate --decision decision.json --reason logic_failure --evidence "test_x: expected 3, got 2" [--record]`
moves the **coder** one ladder step up and appends
`{role, current_model, current_effort, reason, evidence, next_model, next_effort}`
to the history. Only the coder moves (keep it simple); if the plan itself is
wrong, re-route with `--prior-review-issue` instead, which lifts the planner
too. Reaching Opus turns the reviewer on at opus/high unless the user
skipped the reviewer: the decision's `reviewer_skipped` field (set by
`--skip-reviewer`) decides, not the text of the override notes. A v1 decision
JSON has no such field; it counts as skipped when its overrides contain
`reviewer=False` or `reviewer=off`. At most `max_steps` (2) escalations, never
above opus/high; then **stop and ask the user**. An override profile off the
ladder (for example `sonnet/xhigh`) escalates by its model.

`escalation_allowed` in the decision is false when the coder is already at
the top of the ladder (for example ARCHITECTURAL or a COMPLEX trigger at
opus/high, or an override such as `opus/max`) or the steps are used up; the
output then reads `Escalation: Not allowed (... ): stop and ask the user`.

## User override

User words win unless they break a technical or safety rule of the project:

| User says | Router option |
|---|---|
| "use opus" / "use sonnet" / "use haiku" | `--model opus` (coder; `--role planner\|reviewer` targets another role) |
| "do this with high effort" (or low/medium/xhigh/max) | `--effort high`; `xhigh`/`max` are allowed only here |
| "skip planner" / "skip reviewer" | `--skip-planner` / `--skip-reviewer` |
| "no subagents" / "làm trực tiếp" | do it in the main session; recommend `/model` and `/effort` |

Overrides are recorded in the decision (`coder=opus/xhigh`, `planner=off`,
...). A role that is off is turned on by a model/effort override, starting
from the coder's profile.

An override can produce a role and profile with no generated agent file,
for example `--role reviewer --effort medium` at COMPLEX (opus/medium),
`--role reviewer --model opus` at NORMAL (opus/medium) or `--role planner
--model sonnet` at TRIVIAL (sonnet/low). The router checks every role
against the files the policy generates; such a role is listed in the
decision's `no_agent_file` field and printed as
`opus/medium (no agent file: use the fail-safe in docs/TASK_ROUTER.md)`
instead of a non-existent agent name. Skipping the reviewer at ARCHITECTURAL is allowed
(user words win) but recorded as `reviewer=off` plus
`architecture review skipped at ARCHITECTURAL`. An unsupported model or effort
is refused. No override skips the green gate, approval questions, the
migration rules or the push rule.

## Telemetry

`.claude/router-telemetry.jsonl` is committed and append-only (one JSON
object per line, never rewritten). It holds counts and decisions only,
**never prompts or answers**. Every event has `kind`, `task`, `at` (filled
in when missing) and optional `backfilled` / `estimated` flags.

Events are validated **strictly on write** (`record`, `route --record`,
`escalate --record`): unknown kinds, unknown fields and values the current
policy does not know (a reason, a model) are refused. **Reading** (`report`,
`compare`) checks only the stable envelope: `kind` and `task` as text, `at` as
an ISO date-time, a whole-number `policy_version` on route events and the
fields the report reads (counts, the decision and criteria objects). Values
and fields from another policy version are kept, kinds the report does not
know are ignored, and a malformed line (not JSON, or a broken envelope) is
skipped with a warning on stderr (`... line N skipped: ...`) instead of
aborting. A route whose criteria the current router cannot read is listed by
`compare` as `not comparable`.

| Kind | Fields |
|---|---|
| `route` | `policy_version`, `criteria`, `decision` (+ optional `risk_profile`, `prior_review_issue`); written by `route --record` |
| `agent_run` | `role` (planner, coder, coder-fix, reviewer), `profile`, `tokens`, `tool_uses`, `duration_ms`, `outcome` (completed, failed, stopped) |
| `escalation` | `role`, `current_model`, `current_effort`, `reason`, `evidence`, `next_model`, `next_effort`; written by `escalate --record` |
| `gate` | `tests_passed`, `tests_failed`, `ruff_check`, `ruff_format`, `build` (pass/fail) |
| `result` | `result` (pass/fail), `tests`, `findings` (`blocking`, `should_fix`, `minor` counts) |

```sh
uv run python tools/task_router.py record --event '{"kind": "agent_run", "task": "F-070 Fact Check Result", "role": "coder", "profile": "sonnet/high", "tokens": 210000, "tool_uses": 60, "duration_ms": 800000, "outcome": "completed"}'
uv run python tools/task_router.py report    # per task and per profile
uv run python tools/task_router.py compare   # v1 vs current routing per recorded task
```

`report` sums tokens, duration, agent runs, escalations, test failures,
review findings and results per task and per profile (quality columns count
the tasks whose coder ran at that profile), so we can later ask whether
Sonnet/high instead of Opus/high hurt quality. F-065..F-069 are backfilled:
F-068 and F-069 with their measured agent runs (all opus/high under the v1
policy), F-065..F-067 with route events only, from estimated criteria. The
backfilled F-065..F-067 route events are **estimated** v2-shaped decisions
(per-role profiles, risk profile) labelled `policy_version: 1`, the policy in
force when those tasks ran; their roles are the v1 routing, not a v2
decision that was ever used.

| Task | Score | Level | v1 (planner / coder / reviewer) | v2 |
|---|---|---|---|---|
| F-065 Hook Generator | 19 | ARCHITECTURAL | opus/high ×3 | opus/high ×3 |
| F-066 Shorts Script Generator | 8 | NORMAL | sonnet/medium, sonnet/medium, off | same |
| F-067 LongForm Script Generator | 9 | COMPLEX | opus/high ×3 | sonnet/high, sonnet/high, opus/high |
| F-068 Claim Extractor | 12 | COMPLEX | opus/high ×3 | sonnet/high, sonnet/high, opus/high |
| F-069 Evidence Matcher | 14 | COMPLEX | opus/high ×3 | sonnet/high, sonnet/high, opus/high |

## Changing the policy

Edit `.claude/task-router.json` only, then run
`uv run python tools/task_router.py sync-agents` (writes the profile files
the policy can produce: level defaults, trigger profiles, effort bumps and
every coder ladder step; removes stale `task-*.md`, including the old
level-named files) and the tests. `check-agents` and
`test_the_subagent_files_match_the_policy` fail on drift. Start a new
session so Claude Code sees the new files.

**Backward compatibility:** a schema_version 1 policy still loads (each
level's model/effort becomes the profile of every enabled role, no triggers
or bumps, the ladder is the distinct level profiles). A v1 decision JSON
(top-level `model`/`effort`, `planner`/`reviewer` booleans) still loads and
escalates. v2 decisions keep top-level `model`/`effort` equal to the coder's
profile, so scripts that read them keep working.

## Fail-safe

- Unknown criteria count as their lowest value and are listed; together with
  `uncertainty = high` they mark the decision **uncertain**. The router keeps
  the lower level and relies on escalation, and never picks `xhigh`/`max`.
- If the routed `task-<role>-<model>-<effort>` agent is missing in the
  session (new files need a new session; override profiles such as
  `opus/xhigh`, or a role/profile pair the policy never routes to, have no
  file and the decision says `no agent file`), use the Agent tool on a general-purpose agent
  with the profile's `model` and say that its effort follows the session. Or
  do the task in the main session and tell the user the recommended
  `/model` and `/effort`.
- If the router itself fails (bad policy, criteria, risk profile or event),
  fix the input. Do not guess a level or a profile.

## Example

```text
Task: F-069 Evidence Matcher
Complexity: COMPLEX, score 14
Reasons:
- architecture_impact=local (+1)
- ...
Planner: sonnet/high (task-planner-sonnet-high)
Coder: sonnet/high (task-coder-sonnet-high)
Reviewer: opus/high (task-reviewer-opus-high)
Model: sonnet (coder)
Effort: high (coder)
Opus triggers: none: Sonnet is enough
Risk profile (* derived from the criteria): reasoning_risk=medium*, architecture_risk=low*, ...
Escalation: Allowed (0 used)
```
