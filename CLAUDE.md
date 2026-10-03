# CLAUDE.md

Read `PROJECT_STATE.md` (rules, next task) and `TASK_STATUS.md` before doing
anything. The Prompt Pack v8 PDF (outside the repo) defines the tasks; one
task at a time, stop after each task, ask the user before any architecture
decision the pack does not settle, push only when asked.

## Route every task first

Before implementing any task (for example "làm F-068"), route it with the
Task/Model Router. Do not default to the session's model and effort.

1. Measure the routing criteria (not the task name) and run
   `uv run python tools/task_router.py route --task "<id name>" --criteria <json>`.
2. Show the routing decision (level, reasons, model, effort, planner,
   reviewer, escalation) to the user, then follow it: planner
   `task-planner-<level>`, coder `task-coder-<level>`, reviewer
   `task-reviewer-<level>` in `.claude/agents/` (only those the level uses).
3. Open decisions are asked of the user in the main session; the main session
   runs the green gate itself (pytest, ruff check, ruff format --check,
   uv build) before reporting.
4. Escalate only through `tools/task_router.py escalate` with an escalating
   reason and evidence; never for typo, lint, format, simple import or
   environment problems; at most 2 steps, then ask the user.
5. User overrides ("use opus", "use sonnet", "high effort", "skip planner")
   win unless they break a project rule.

Full rules, policy, fallback and examples: `docs/TASK_ROUTER.md`. The policy
lives only in `.claude/task-router.json`; after changing it run
`uv run python tools/task_router.py sync-agents`.

## Commands

```sh
uv run python -m pytest
uv run python -m ruff check .
uv run python -m ruff format --check .
uv build
```

`pytest.exe` is blocked by Windows Application Control on this machine; use
`python -m pytest`. Print Vietnamese with `PYTHONIOENCODING=utf-8`.
