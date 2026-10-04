# CLAUDE.md

Read `PROJECT_STATE.md` (rules, next task) and `TASK_STATUS.md` before doing
anything. The Prompt Pack v8 PDF (outside the repo) defines the tasks; one
task at a time, stop after each task, ask the user before any architecture
decision the pack does not settle, push only when asked.

## Route every task first

Before implementing any task (for example "làm F-070"), route it with the
cost-aware Task/Model Router. Do not default to the session's model and
effort; use the cheapest capable profile per role, Opus only on evidence.

1. Measure the routing criteria (not the task name), optionally a risk
   profile, and run
   `uv run python tools/task_router.py route --task "<id name>" --criteria <json> [--profile <json>] --record`.
2. Show the decision (level, score, reasons, per-role profile, Opus triggers,
   risk profile, escalation) to the user, then follow it: dispatch
   `task-planner-<model>-<effort>`, `task-coder-<model>-<effort>` and
   `task-reviewer-<model>-<effort>` from `.claude/agents/` (only the roles
   that are on; ask the reviewer for an architecture review on ARCHITECTURAL).
3. Open decisions are asked of the user in the main session; the main session
   runs the green gate itself: ruff check and ruff format --check always,
   uv build when packaging-relevant code changed or before a checkpoint, and
   the full pytest suite at most once per task, run by the main session only
   (a coder runs it only if the brief asks), only when justified (core or
   shared code, DB or migration, API contract, architecture, test
   infrastructure, dependency, many modules, before a checkpoint, high-risk
   end of task); when it is skipped, say why. The end-of-task report includes
   a Tests block (new tests, affected tests, regression, full suite,
   migration chain: each RUN or NOT RUN with the reason). The reviewer does
   not repeat the gate. Fix rounds go to a fresh coder given only the
   findings, the decisions and the file list.
4. Escalate only through `tools/task_router.py escalate --record` with an
   escalating reason and evidence; never for typo, lint, format, simple
   import, type, test fixture or environment problems; at most 2 steps, then
   ask the user.
5. Record every agent run, the gate and the result with
   `tools/task_router.py record --event <json>` (counts only, no prompts);
   `report` and `compare` summarise them.
6. User overrides ("use opus", "use sonnet", "high effort", "skip planner",
   `--role reviewer`) win unless they break a project rule.

Full rules, policy, telemetry, fail-safe and examples: `docs/TASK_ROUTER.md`.
The policy lives only in `.claude/task-router.json`; after changing it run
`uv run python tools/task_router.py sync-agents` and start a new session.

## Commands

```sh
uv run python -m pytest tests/test_x.py            # targeted files first
uv run python -m pytest tests/test_x.py::test_name # one failing test id
uv run python -m pytest                            # full suite: once, when justified
uv run python -m ruff check .
uv run python -m ruff format --check .
uv build                                           # packaging code or checkpoint
```

Smart test execution: run the single failing test id, then the new tests,
then the affected tests, then the related regression; the full suite at most
once per task, owned by the main session (coders and fix rounds do not run it
unless the brief asks), and only when justified, never after each fix. Subagents
never run git commands that change the working tree, index, branches or stashes
(read-only git only). Normal tests get
a migrated database from the `database` fixture in `tests/conftest.py` (a
template migrated once per session, copied per test); only migration and
upgrade tests run the migration chain on a fresh file.

`pytest.exe` is blocked by Windows Application Control on this machine; use
`python -m pytest`. Print Vietnamese with `PYTHONIOENCODING=utf-8`.
