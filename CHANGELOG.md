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
- A-006 Feature Flags is complete. `core/flags.py` adds SHORTS_ENABLED (default on), LONGFORM_ENABLED (off), PUBLISH_ENABLED (off), TEST_REQUIRED (on), APPROVAL_REQUIRED (on) and AUTO_REPLY_ENABLED (off). They are exposed as `Settings.flags` and set with `AI_YOUTUBE_AGENT_FLAGS__<NAME>`. Publishing requires test and approval, and production always requires both. Added 14 tests, with no new dependencies. The next task is A-007.
- A-007 Logging Contract is complete. `core/log.py` writes one JSON object per record, with timestamp, severity, logger, message, correlation, session and job IDs, fields and exception. It defines five severity levels (`Severity`). `log_context` binds IDs through context variables, so they stay isolated across asyncio tasks, and `configure_logging` is idempotent. Added `Settings.log_level` (`AI_YOUTUBE_AGENT_LOG_LEVEL`, default INFO) and 16 tests, with no new dependencies. Redacting logs stays in #219. The next task is A-008.
- A-008 Error Model is complete. `core/errors.py` defines `AppError` with the categories `ApplicationError` (500), `DomainError` (422) and `ProviderError` (502, with a provider name and a retryable flag). Each error has a stable code, an internal `detail` that is never public, a safe `user_message`, a frozen `PublicError` view and `log_fields()` for structured logs. `to_public()` hides any unexpected exception behind a generic message. Added 16 tests, with no new dependencies. The next task is A-009.
- A-009 Dependency Injection is complete, with the design choices the user approved: a hand-written container, a composition root, and `create_app()`. `core/di.py` provides `Container` with singleton and transient lifetimes, `register_instance`, `override` for tests, type checks, cycle detection and thread-safe singletons, and it raises `RegistrationError` (an `ApplicationError`). `bootstrap.build_container()` is the only place that picks implementations, and so far it registers `Settings` and `FeatureFlags`. `main.create_app()` builds the container, configures logging and sets `app.state.container`. `/health` is unchanged, and `provide()` resolves dependencies in routes. Added 19 tests, with no new dependencies. The next task is A-010.
- A-010 Health Check is complete, with the design choices the user approved. `core/health.py` defines `HealthStatus` (ok, degraded, down), `CheckKind` (application, provider), `HealthCheck`, `CheckResult`, `HealthReport` and `HealthRegistry`. A failing application check makes the service `down`, and `GET /health` answers 503. A failing provider check makes it only `degraded`, and the answer stays 200. Checks run in parallel with a per-check timeout (default 2 s). A failure shows only the safe `to_public()` message, and the internal detail goes to the log. The registry is a singleton in the container, and `bootstrap` registers the application checks `settings` and `feature_flags`. Later providers add their own checks there. `GET /health` keeps `status` and `version` and adds `checks`. Two existing tests changed from comparing the exact body to checking each key. Added 19 tests, with no new dependencies. The next task is A-011.
- A-011 Audit Event Model is complete, with the design choices the user approved. `core/audit.py` defines a frozen `AuditEvent`. Each event has an id, a UTC timestamp, a dotted action name, a typed `Actor` (user, system or ai), an `EntityRef`, an `AuditResult` (success, failure or denied), the correlation, session and job IDs from `log_context`, and read-only metadata limited to JSON scalars. `AuditSink` is an append-only protocol with no update or delete. `InMemoryAuditSink` is the sink for now and rejects duplicate event ids, and a database sink will come with #029. `AuditLog.record()` builds the event, appends it and writes a structured log record. `bootstrap` registers `AuditSink` and `AuditLog` as singletons. There is no tamper-evidence hash chain, which is left to the security phase (#216–#223). Added 26 tests, with no new dependencies. The next task is A-012.
- A-012 Build Baseline is complete, and so is Phase A. The full foundation baseline on `59fcd2d` is recorded in `docs/A-012_BUILD_BASELINE.md`. `uv sync --locked`, `uv lock --check`, 132 tests (also with `-W error` and on 5 repeated runs), `ruff check`, `ruff format --check`, `uv build`, an isolated install of the wheel and a uvicorn run of `GET /health` all pass. There are no baseline failures and no infrastructure blockers, so no code changed. `PROJECT_STATE.md` now records the foundation baseline, the correct pushed checkpoint (`3ebc29c`) and the expected test count. The next task is B-013 Channel Entity.
- B-013 Channel Entity is complete, with the design choices the user approved. `content/channel.py` (context C1) defines a frozen `Channel` with a stable internal id (uuid4), a trimmed non-empty title, `YouTubeIdentifiers` and a `ChannelStatus` (pending, active, paused, disconnected, archived; new channels start pending). It has UTC `created_at` and `updated_at`, and `updated_at` can never be earlier than `created_at`. The `channel_id` is required and must be `UC` plus 22 characters, and the `@handle` is optional. `with_status()` and `rename()` return a new channel with the same id and `created_at`. A change that makes no difference returns the same object. No transition rules are enforced yet, except that an archived channel is read-only and raises `ChannelArchivedError` (a `DomainError`, 422). Persistence is left to #029. Added 34 tests, with no new dependencies. The next task is B-014.

## [0.1.0] - 2026-09-25

Baseline commit: `3b41416`.

### Added

- Minimal Python 3.11 project managed by uv, with the `uv_build` backend and `uv.lock`.
- Package `ai_youtube_agent` in `src/`, with a FastAPI app that has a `GET /health` endpoint.
- Smoke test `tests/test_health.py`.
- pytest and ruff configuration in `pyproject.toml`.
- `README.md` and `.gitignore`.
