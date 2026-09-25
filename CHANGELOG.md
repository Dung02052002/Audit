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
- A-001 Project Audit is complete. The report `docs/A-001_PROJECT_AUDIT.md` covers architecture, state and dependency risks, with no code changes. The project state stays `READY_FOR_PHASE_A`, and the next task is A-002.
- A-002 Requirements Freeze is complete. `docs/REQUIREMENTS.md` freezes the v7 requirements from Prompt Pack v8. It confirms SHORTS and LONGFORM as the only content types and TEST, QC, PREVIEW and APPROVAL as shared control stages, and it includes the 238-prompt catalog verbatim. No code changes. The next task is A-003.
- A-003 Architecture Map is complete. `docs/ARCHITECTURE.md` maps 16 bounded contexts and 5 cross-cutting areas, provider and internal interfaces, the production data flow, lifecycle statuses and gates. Every one of the 238 prompts is traced to one context. It lists 8 open questions and makes no code changes. The next task is A-004.
- A-004 Folder Structure is complete. It added the empty packages `core/`, `content/`, `providers/` and `pipeline/` under `src/ai_youtube_agent/`, and a `dashboard/` folder at the root (its platform is still open question Q4). It also added `tests/test_folder_structure.py` (7 tests) and updated the README layout and ARCHITECTURE section 7. There are no feature changes. The next task is A-005.
- A-005 Configuration Contract is complete. `core/config.py` loads typed, frozen settings with the `AI_YOUTUBE_AGENT_` prefix, per-environment `.env` files (development, test, production) and validation. Debug is forbidden in production, and secrets must be `SecretStr` with no default. Added the runtime dependency `pydantic-settings` (with `python-dotenv`), `.env.example`, the `.env.*` ignore rules and 14 tests. The next task is A-006.

## [0.1.0] - 2026-09-25

Baseline commit: `3b41416`.

### Added

- Minimal Python 3.11 project managed by uv, with the `uv_build` backend and `uv.lock`.
- Package `ai_youtube_agent` in `src/`, with a FastAPI app that has a `GET /health` endpoint.
- Smoke test `tests/test_health.py`.
- pytest and ruff configuration in `pyproject.toml`.
- `README.md` and `.gitignore`.
