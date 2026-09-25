# Changelog

All notable changes to this project are documented in this file.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added

- `PROJECT_STATE.md`, `TASK_STATUS.md` and `CHANGELOG.md` to track the project state and task results.

## [0.1.0] - 2026-09-25

Baseline commit: `3b41416`.

### Added

- Minimal Python 3.11 project managed by uv, with the `uv_build` backend and `uv.lock`.
- Package `ai_youtube_agent` in `src/`, with a FastAPI app that has a `GET /health` endpoint.
- Smoke test `tests/test_health.py`.
- pytest and ruff configuration in `pyproject.toml`.
- `README.md` and `.gitignore`.
