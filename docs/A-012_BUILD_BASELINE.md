# A-012 Build Baseline

- Date: 2026-09-28
- Prompt: Prompt Pack v8, Phase A, prompt 12 (Foundation & Governance)
- Scope: run the full build and test suite, record baseline failures, and fix only infrastructure blockers introduced by the foundation (A-001 to A-011).
- Checkpoint measured: `59fcd2d` (last code commit `60d6b2b`, A-011)
- Result: **PASS**. No failures and no infrastructure blockers, so no code changed.

## 1. Environment

| Item | Value |
|---|---|
| OS | Windows 11 Pro 10.0.26200, x86_64 |
| Python | 3.11.9 (project `.venv`, managed by uv) |
| uv | 0.12.1 |
| Working tree | Clean before and after every check |

## 2. Checks

| Check | Command | Result |
|---|---|---|
| Environment matches the lockfile | `uv sync --locked` | PASS (26 packages) |
| Lockfile is current | `uv lock --check` | PASS |
| Tests | `uv run pytest` | PASS: 132 passed, 0 failed, 0 skipped |
| Tests with warnings as errors | `uv run pytest -W error` | PASS: 132 passed, so there are no deprecation or runtime warnings |
| Flakiness | `uv run pytest` five times in a row | PASS: 132 passed on every run (about 0.5 s each) |
| Lint | `uv run ruff check .` | PASS |
| Format | `uv run ruff format --check .` | PASS (32 files) |
| Build | `uv build` from an empty `dist/` | PASS: `ai_youtube_agent-0.1.0-py3-none-any.whl` and `ai_youtube_agent-0.1.0.tar.gz` |
| Wheel contents | list `.py` files in the wheel | All 14 modules are present: `__init__`, `main`, `bootstrap`, `core/{audit,config,di,errors,flags,health,log}` and the 4 package `__init__` files |
| Installed wheel | `uv run --isolated --no-project --with <wheel>` then import `main.app` and run the health registry | PASS: `ok ['settings', 'feature_flags']` |
| Run | `uv run uvicorn ai_youtube_agent.main:app` | PASS: `GET /health` returns 200 with `status: ok` and both checks ok. `GET /openapi.json` returns 200 |

## 3. Tests by file

| File | Tests | Foundation prompt |
|---|---|---|
| `tests/test_health.py` | 1 | Phase 0 smoke test (updated in A-010) |
| `tests/test_folder_structure.py` | 7 | A-004 |
| `tests/test_config.py` | 14 | A-005 |
| `tests/test_flags.py` | 14 | A-006 |
| `tests/test_log.py` | 16 | A-007 |
| `tests/test_errors.py` | 16 | A-008 |
| `tests/test_di.py` | 13 | A-009 |
| `tests/test_bootstrap.py` | 6 | A-009 (updated in A-010) |
| `tests/test_health_check.py` | 19 | A-010 |
| `tests/test_audit.py` | 26 | A-011 |
| **Total** | **132** | |

## 4. Baseline failures

| Category | Count | Items |
|---|---|---|
| PRE_EXISTING | 0 | None |
| Introduced by the foundation | 0 | None |
| ENVIRONMENT | 0 | None |
| TOOLING | 0 | None |
| UNKNOWN | 0 | None |

Infrastructure blockers fixed: none, because there were none.

## 5. Notes that do not block anything

- The Docker daemon is not reachable. Docker and CI remain deferred, as recorded in `PROJECT_STATE.md`.
- uv warns "Failed to hardlink files" because its cache is on drive C: and the project is on D:. Installs still succeed.
- In Windows PowerShell 5.1, piping `uv` output with `2>&1` shows a `NativeCommandError` wrapper around normal stderr lines. The exit code is 0, so this is not a failure.

## 6. Foundation delivered by Phase A

| Prompt | Module |
|---|---|
| A-005 Configuration Contract | `core/config.py` |
| A-006 Feature Flags | `core/flags.py` |
| A-007 Logging Contract | `core/log.py` |
| A-008 Error Model | `core/errors.py` |
| A-009 Dependency Injection | `core/di.py`, `bootstrap.py`, `main.create_app()` |
| A-010 Health Check | `core/health.py`, `GET /health` |
| A-011 Audit Event Model | `core/audit.py` |

Any failure that appears after `59fcd2d` comes from a later change and is not pre-existing.
