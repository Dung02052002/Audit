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
   runs the green gate itself (pytest, ruff check, ruff format --check,
   uv build); the reviewer does not repeat it. Fix rounds go to a fresh coder
   given only the findings, the decisions and the file list.
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
uv run python -m pytest
uv run python -m ruff check .
uv run python -m ruff format --check .
uv build
```

`pytest.exe` is blocked by Windows Application Control on this machine; use
`python -m pytest`. Print Vietnamese with `PYTHONIOENCODING=utf-8`.
