# Changelog

All notable changes to this project are documented in this file.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added

- `PROJECT_STATE.md`, `TASK_STATUS.md` and `CHANGELOG.md` to track the project state and task results.

### Changed

- Phase 0 is complete, and the Clean Baseline Gate (Task 007) passed on baseline `3b41416`.
- The project state moved from `EXISTING_INITIALIZED` to `READY_FOR_PHASE_A`.
- Phase 0 tasks 008–011 of Prompt Pack v8 were verified and recorded. No code changed.
- `PROJECT_STATE.md` became the handoff document. It now includes known failures (none), invariants, repository status, how to resume, and the next task (A-001).
- The Phase A task list (A-001 to A-012) was added to `TASK_STATUS.md`.

## [0.1.0] - 2026-09-25

Baseline commit: `3b41416`.

### Added

- Minimal Python 3.11 project managed by uv, with the `uv_build` backend and `uv.lock`.
- Package `ai_youtube_agent` in `src/`, with a FastAPI app that has a `GET /health` endpoint.
- Smoke test `tests/test_health.py`.
- pytest and ruff configuration in `pyproject.toml`.
- `README.md` and `.gitignore`.
