# Task Status

Last updated: 2026-09-30

| Task | Name | Result | Notes |
|---|---|---|---|
| 000 | Environment Detection | PASS | Windows 11, Python 3.11.9, uv 0.12.1, git 2.55. The Docker daemon is not reachable, which is not needed yet. |
| 001 | Repository State Detection | PASS | Initial state was EMPTY: no files, no Git. |
| 002 | Empty Project Detection | PASS | Initial classification was EMPTY. |
| 003 | Existing Test Inventory | PASS | Initially there was no test suite, because the repository was EMPTY. |
| 004 | Pre-existing Baseline | PASS | Initial baseline was CLEAN EMPTY STATE, with no pre-existing failures. |
| 005 | Baseline Classification | PASS | EMPTY + CLEAN EMPTY STATE + INITIALIZATION_REQUIRED. |
| — | Project Initialization | PASS | Created the minimal FastAPI/uv/pytest/ruff project. Baseline commit is `3b41416`. |
| 000–005 | Re-run after initialization | PASS | EXISTING + CLEAN BASELINE, with no failures. |
| 006 | Bootstrap Decision | PASS | The repository is EXISTING, so the decision was an Audit Plan and no re-bootstrap. |
| — | State Drift Audit | PASS | Actual state is EXISTING. The earlier EMPTY classification was correct at the time it was made. |
| 007 | Clean Baseline Gate | PASS | Clean Baseline Gate = PASS, and the gate is OPEN. Baseline commit is `3b41416`. Test = PASS, lint = PASS, build = PASS. No pre-existing failures and no UNKNOWN blockers. |

## Phase 0 tasks 008–011 (Prompt Pack v8)

The pack requires Prompt 011 to PASS before Phase A starts. Project Initialization did most of this work. Each task was then run and verified on its own number.

| Task | Name | Status |
|---|---|---|
| 008 | Bootstrap Minimal App | PASS. No code created: the app already existed from Project Initialization (`3b41416`). Verified that `uv build` works, `GET /health` returns 200 on uvicorn, and pytest reports 1 passed. |
| 009 | Bootstrap Tests | PASS. No new test was needed: the existing smoke test `tests/test_health.py::test_health_returns_ok` already meets the criteria. pytest collects it (1 test) and it passes. A mutation check on a scratchpad copy (`/health` status changed to "broken") made it fail, which proves it catches regressions. |
| 010 | Bootstrap Clean Gate | PASS. Re-ran every check: pytest 1 passed, `ruff check` passed, `ruff format --check` passed, `uv lock --check` passed, `uv build` produced a wheel and sdist. There were no failures from the bootstrap and no PRE_EXISTING failures. Code is unchanged since `3b41416`. |
| 011 | Project Handoff | PASS. `PROJECT_STATE.md` now includes state, baseline, current checkpoint, known failures (none), toolchain, repository status, invariants, how to resume, and the next task (A-001). Phase 0 is complete. |

## Phase A: Foundation & Governance (Prompt Pack v8, prompts 1–12)

Source: `AI_YouTube_Autonomous_Agent_PROMPT_PACK_v8_BASELINE_SAFE.pdf`, pages 8–9. Do not start a prompt until the previous one has PASSED.

| Task | Name | Dependency | Status |
|---|---|---|---|
| A-001 | Project Audit | None | PASS. See `docs/A-001_PROJECT_AUDIT.md`. |
| A-002 | Requirements Freeze | A-1 | PASS. See `docs/REQUIREMENTS.md` (FROZEN). |
| A-003 | Architecture Map | A-2 | PASS. See `docs/ARCHITECTURE.md`. It has 8 open questions (Q1–Q8). |
| A-004 | Folder Structure | A-3 | PASS. Added the backend packages `core/`, `content/`, `providers/` and `pipeline/` under `src/ai_youtube_agent/`, plus `dashboard/` at the root. Tests: 8 passed. |
| A-005 | Configuration Contract | A-4 | PASS. Added `core/config.py`, `.env.example` and 14 config tests. New dependency: `pydantic-settings`. Tests: 22 passed. |
| A-006 | Feature Flags | A-5 | PASS. Added `core/flags.py` with the 6 required flags and safe defaults, wired into `Settings`, with 14 flag tests. Tests: 36 passed. |
| A-007 | Logging Contract | A-5 | PASS. Added `core/log.py` (JSON records, `Severity`, `log_context` for correlation, session and job IDs) and `Settings.log_level`, with 16 logging tests. Tests: 52 passed. |
| A-008 | Error Model | A-7 | PASS. Added `core/errors.py` (the categories Application, Domain and Provider, `PublicError` and `to_public`) with 16 tests. Tests: 68 passed. |
| A-009 | Dependency Injection | A-8 | PASS. Added the container in `core/di.py`, the composition root `bootstrap.py`, and `create_app()` plus `provide()` in `main.py`, with 19 new tests. Tests: 87 passed. |
| A-010 | Health Check | A-9 | PASS. Added `core/health.py` (status model ok/degraded/down, application and provider checks, `HealthRegistry` with per-check timeouts). The registry is registered in `bootstrap.py`, and `/health` now returns `checks` and answers 503 when down. 19 new tests. Tests: 106 passed. |
| A-011 | Audit Event Model | A-10 | PASS. Added `core/audit.py`: a frozen `AuditEvent` (actor, UTC timestamp, action, entity, result, context IDs, read-only metadata), an append-only `AuditSink` protocol, `InMemoryAuditSink` and `AuditLog`, all registered in `bootstrap.py`. 26 new tests. Tests: 132 passed. |
| A-012 | Build Baseline | A-11 | PASS. See `docs/A-012_BUILD_BASELINE.md`. Full baseline on `59fcd2d`: sync, lock, 132 tests (also with `-W error` and 5 repeated runs), lint, format, build, installed wheel and a uvicorn run all pass. No baseline failures and no infrastructure blockers, so no code changed. |

## Phase B: Domain & Persistence (Prompt Pack v8, prompts 13–30)

Source: `AI_YouTube_Autonomous_Agent_PROMPT_PACK_v8_BASELINE_SAFE.pdf`, pages 10–12, and `docs/REQUIREMENTS.md`. Do not start a prompt until the previous one has PASSED.

| Task | Name | Dependency | Status |
|---|---|---|---|
| B-013 | Channel Entity | A-12 | PASS. Added `content/channel.py`: a frozen `Channel` with a stable id, `YouTubeIdentifiers` (validated `channel_id` and an optional `@handle`), `ChannelStatus` (pending, active, paused, disconnected, archived) and UTC `created_at` and `updated_at`. `with_status` and `rename` return a new channel, and an archived channel is read-only (`ChannelArchivedError`). 34 new tests. Tests: 166 passed. |
| B-014 | StrategyProfile Entity | B-1 | PASS. Added `content/strategy.py`: a frozen `StrategyProfile` linked to one channel, with typed value objects for market (ISO 3166-1), languages (BCP-47), audience, niche, brand, cadence, budget (ISO 4217, `Decimal`) and monetization. Only a user actor can create or update it (`StrategyChangeNotAllowedError`, R-09), and each change bumps the version. 90 new tests. Tests: 256 passed. |
| B-015 | ContentItem Entity | B-2 | PASS. Added `core/content_item.py`: `ContentType` (shorts, longform), `ContentStatus` (the 10 statuses of #031) and a frozen `ContentItem` linked to a channel and a strategy profile version, starting at draft. `with_status` has no transition rules yet (#032). 39 new tests. Tests: 295 passed. |
| B-016 | Artifact Entity | B-3 | PASS. Added `core/artifact.py`: `ArtifactKind` (video, audio, subtitles, thumbnail, metadata) and a frozen `Artifact` version record with uri, sha256, size and media type. `next_version` creates a new record with version + 1 and refuses unchanged content. 37 new tests. Tests: 332 passed. |
| B-017 | Script Entity | B-4 | PASS. Added `content/script.py`: immutable `Script` version records (`next_version` refuses unchanged text), `Claim` bound to one exact script version, and `Evidence` linking a claim to an opaque research `source_ref`. 37 new tests. Tests: 369 passed. |
| B-018 | Voice Entity | B-5 | PASS. Added `content/voice.py`: a user-controlled `VoiceProfile` (provider, voice id, BCP-47 language, optional style, version, user-only actor guard) and `AudioMetadata` linking an audio `Artifact`, the exact `Script` version and the voice profile version, with provider and duration in milliseconds. No cost stored. 55 new tests. Tests: 424 passed. |
| B-019 | Rights Entity | B-6 | PASS. Added `content/rights.py`: a frozen `RightsRecord` per asset (opaque `asset_ref`, source, optional licence, `RiskLevel` and `RiskResolution`). Only a user may resolve a risk, and a change of level or licence makes it unresolved again. 41 new tests. Tests: 465 passed. |
| B-020 | QC Entity | B-7 | PASS. Added `content/qc.py`: `QCStatus` (pass, warn, fail), `QCCheck` and a frozen `QCResult` bound to a content item and the exact artifact versions checked, with a derived worst-of `status`. 43 new tests. Tests: 508 passed. |
| B-021 | Approval Entity | B-8 | PASS. Added `content/approval.py`: `ApprovalStatus`, `ArtifactBinding` (artifact id, kind, version and sha256 snapshot) and a frozen pending `ApprovalRequest` bound to exact artifact versions, with `requested_by` and an optional `qc_result_id`. No decision methods yet (#139–#142). 41 new tests. Tests: 549 passed. |
| B-022 | Publish Entity | B-9 | PASS. Added `pipeline/publish.py`: `PublishStatus`, `PublishResult` and a frozen `PublishJob` with a supplied idempotency key. Creation requires an approved `ApprovalRequest` (R-08), attempts are counted, and succeeded is final. 55 new tests. Tests: 604 passed. |
| B-023 | Analytics Entity | B-10 | PASS. Added `content/analytics.py`: `MetricScope` and a frozen `MetricSnapshot` with source, UTC period, retrieval time and a read-only name → `Decimal` metric map (missing metrics stay absent). Freshness is derived with `age_at` and `is_stale`. 41 new tests. Tests: 645 passed. |
| B-024 | Revenue Entity | B-11 | PASS. Added `content/revenue.py`: `RevenueStage` and a frozen `RevenueRecord` (estimated or final, never converted) with scope, revenue type, data source, a non-negative `Decimal` amount, ISO 4217 currency, UTC period and retrieval time, and derived freshness. 37 new tests. Tests: 682 passed. |
| B-025 | Cost Entity | B-12 | PASS. Added `content/cost.py`: `CostCategory` (production, api, tts, render, storage, llm) and a frozen `CostRecord` attributed to a channel, with provider, a non-negative `Decimal` amount, ISO 4217 currency, UTC `incurred_at`, and optional content item and job/artifact ref. 33 new tests. Tests: 715 passed. |
| B-026 | Comment Entity | B-13 | PASS. Added `content/comment.py`: a minimal synced `Comment` (display name only), `CommentClassification` records with history, and `ReplyDraft` with a `ReplyStatus` that always starts as draft and has no approve or post method yet. 38 new tests. Tests: 753 passed. |
| B-027 | AI Job Entity | B-14 | PASS. Added `pipeline/job.py`: a user-only `Session` and a frozen `AIJob` (kind, supplied idempotency key, status, attempts, append-only `JobCheckpoint` history) with guarded `start`, `checkpoint`, `fail`, `succeed` and `cancel`. Succeeded and cancelled are final. 81 new tests. Tests: 834 passed. |
| B-028 | Experiment Entity | B-15 | PASS. Added `content/experiment.py`: a frozen `Experiment` registry entry (title, thumbnail or content, hypothesis, 2+ variants). Any actor may propose; only a user may start, conclude or cancel. The conclusion records a winner, note, evidence snapshot ids, the concluding user and UTC time, and is never applied (R-09). 66 new tests. Tests: 900 passed. |
| B-029 | Migrations | B-16 | PASS. SQLite (Q5, user decision). Added `core/db/`: canonical codec (fixed-width UTC datetime, plain decimal TEXT), an in-house forward-only migration runner (checksums, one transaction per migration, backup before migrating, `restore_backup`), and `0001_initial_schema.sql` with STRICT tables for all entities plus `audit_events`. `Settings.database_path` added; migrations run only at startup (FastAPI lifespan). Checksums ignore line endings. No repositories yet (B-030). 103 new tests. Tests: 1003 passed. |
| B-030 | Repository Tests | B-17 | PASS. Added `core/db/database.py` (`Database`, `transaction()`, `ConcurrencyError`) and `core/db/repositories/`: one SQLite repository per aggregate, add-only for immutable records, optimistic `update` for changing entities, no deletes, plus `SqliteAuditSink` (used by bootstrap outside the TEST environment). Added `tests/factories.py`, a `database` fixture and 69 tests. Tests: 1072 passed. Phase B is complete. |

## Phase C: State Machine & Control Gates (Prompt Pack v8, prompts 31–42)

Source: `AI_YouTube_Autonomous_Agent_PROMPT_PACK_v8_BASELINE_SAFE.pdf` and `docs/REQUIREMENTS.md`. Do not start a prompt until the previous one has PASSED.

| Task | Name | Dependency | Status |
|---|---|---|---|
| C-031 | Status Enum | B-18 | PASS. `ContentStatus` in `core/content_item.py` (from B-015) verified as the single status enum: exactly the ten #031 statuses in order, snake_case stored values, no other value accepted, every status round-trips through SQLite, and the CHECK constraint refuses unknown values. Added an enum docstring, an ARCHITECTURE §5.2 note and `tests/test_status_enum.py` (30 tests). No behaviour change. Tests: 1102 passed. |
| C-032 | Transition Rules | C-1 | PASS. User-approved table `ALLOWED_TRANSITIONS` in `core/content_item.py` (21 allowed moves of 90; published final; rejected and failed restart from draft; testing to approved may return to generating), `can_transition`, `allowed_transitions`, `is_final`, and typed `ContentTransitionError` (`domain.content_transition_blocked`). `with_status` is guarded. Two earlier tests (B-015, C-031) build items directly in a status, as approved. `tests/test_transitions.py` (205 tests). Tests: 1307 passed. |
| C-033 | Pipeline Gate Contract | C-2 | PASS. User-approved design in `core/gates.py`: closed `GateName` (5 gates), pass/block `GateResult` with `GateReason`s, `GateContext` (item, allowed target status, actor, time), sync `PipelineGate` protocol, `evaluate_gates` running every gate and failing closed, `GateReport` and `GateBlockedError` (`domain.gate_blocked`). No concrete gates. `tests/test_gates.py` (34 tests). Tests: 1341 passed. |
| C-034 | Approval Gate | C-3 | PASS. `ApprovalGate` in `core/approval_gate.py` (user-approved rules): judges only the move to publishing; the newest approval request must be approved and bind every artifact kind at its latest version (id, version, sha256); codes `approval.missing`, `approval.not_approved`, `approval.not_current`; ignores `APPROVAL_REQUIRED`. Reads through protocols the SQLite repositories satisfy. `tests/test_approval_gate.py` (22 tests). Tests: 1363 passed. |
| C-035 | Version Invalidation | C-4 | PASS. User-approved rules: `VersionInvalidation` in `core/version_invalidation.py` stores a new artifact version and invalidates stale pending/approved requests in the same transaction (or on demand); items in preview_ready/awaiting_approval/approved move back to generating; `approval.invalidated` audited after commit. `ApprovalRequest.invalidate()` and shared `stale_kinds` in `content/approval.py` (the C-034 gate now uses it, no behaviour change). The B-021 placeholder test no longer forbids `invalidate` (user approved). `tests/test_version_invalidation.py` (37 tests). Tests: 1400 passed. |
| C-036 | Daily Limit Gate | C-5 | PASS. User-approved rules: `DailyLimitGate` in `core/daily_limit_gate.py` (`GateName.DAILY_LIMIT`) uses the channel's cadence per content type, counted separately for production (draft to generating, from the new append-only `production_starts` table, migration 0002, written by `start_production` in `core/production.py`) and publishing (non-failed publish jobs, excluding the item's own), per UTC day. New `core/production_start.py` and `core/db/repositories/usage.py`. Updated the C-033 gate-name test and seven B-029 migration tests that pinned version 1 and the table list. `tests/test_daily_limit.py` (37 tests). Tests: 1438 passed. |
| C-037 | Budget Gate | C-6 | PASS. User-approved rules: `BudgetGate` in `core/budget_gate.py` (`GateName.BUDGET`) checks every move into generating against the channel's daily and monthly budget (spend >= limit blocks), summing `CostRecord`s as `Decimal` per UTC day and UTC calendar month; another currency this month and a missing strategy block. The C-033 later-names test now includes `budget`. `tests/test_budget_gate.py` (30 tests). Tests: 1468 passed. |
| C-038 | Rights Gate | C-7 | PASS. User-approved rules: `RightsGate` in `core/rights_gate.py` (`GateName.RIGHTS`) judges only the move to publishing and blocks on every rights record of the item that is unresolved at level high or unknown (unknown counts as high), one reason per record (`rights.unresolved_high`, `rights.unresolved_unknown`) naming its asset ref. Unresolved low and medium, resolved records at any level, and items without records pass. `tests/test_rights_gate.py` (18 tests). Tests: 1486 passed. |
| C-039 | Policy Gate | C-8 | PASS. User-approved rules: `content/policy.py` adds the frozen read shape `PolicyCheck` / `PolicyFinding` (rule id, rule version, blocking flag from the rule's configuration, safe message), with no table until #080/#083. `PolicyGate` in `core/policy_gate.py` (`GateName.POLICY`) judges only the move to publishing: only the newest check counts, each blocking finding gives a `policy.failed` reason, non-blocking findings are warnings, and an item never checked blocks with `policy.not_checked`. `tests/test_policy_gate.py` (16 tests). Tests: 1502 passed. |
| C-040 | Kill Switch Gate | C-9 | PASS. User-approved rules: `pipeline/kill_switch.py` adds the frozen read shape `EmergencyStop` (active, activated_by, activated_at, optional reason), with no store or toggle until #213. `KillSwitchGate` in `core/kill_switch_gate.py` (new `GateName.KILL_SWITCH`) reads the current stop on every evaluation and, while it is active, blocks every move into generating and the move to publishing with one `killswitch.active` reason (fixed message plus the activator's reason, not the activator). Other moves pass. The C-033 later-names test now includes `kill_switch`. `tests/test_kill_switch_gate.py` (35 tests). Tests: 1537 passed. |
| C-041 | Idempotency Gate | C-10 | PASS. User-approved rules: `pipeline/idempotency.py` adds deterministic keys, `generation_key(item, kind="content.generate")` = `gen:` + sha256 of kind, item id, status and updated_at, and `publish_key(item_id, approval_request_id)` = `pub:` + sha256, hashed as a JSON array. `IdempotencyGate` in `core/idempotency_gate.py` (new `GateName.IDEMPOTENCY`) checks the move into generating and the move to publishing (newest request, when approved): a job stored under the key blocks unless it failed (`idempotency.duplicate_generation`, `idempotency.duplicate_publish`); cancelled blocks; no approved newest request passes (the approval gate blocks). The C-033 later-names test now includes `idempotency`. `tests/test_idempotency_gate.py` (30 tests, including SQLite). Tests: 1567 passed. |
| C-042 | Gate Tests | C-11 | PASS. User-approved scope: `tests/test_gate_matrix.py` (423 tests, no production code change). Each of the 7 concrete gates is built in a clean and a worst world; for each of the 21 moves #032 allows, the worst world blocks exactly the moves the gate guards (table `GUARDS`, kept in the test only; #206/#084 wire gates into the flow) with the expected reason codes and passes the rest, and the clean world passes every move. All 7 gates run together through `evaluate_gates` for every move in both worlds, a broken gate gives `gate.error` while the others still run, and all 79 refused pairs (69 forbidden moves and 10 same-status) are refused before any gate is asked. Tests: 1990 passed. Phase C is complete. |

## Phase D: Strategy & Channel Configuration (Prompt Pack v8, prompts 43–53)

Source: `AI_YouTube_Autonomous_Agent_PROMPT_PACK_v8_BASELINE_SAFE.pdf` and `docs/REQUIREMENTS.md`. Do not start a prompt until the previous one has PASSED.

| Task | Name | Dependency | Status |
|---|---|---|---|
| D-043 | Channel Settings | C-12 | PASS. User-approved scope: HTTP API only (UI is #191-#203, Q4 open). `content/channel_api.py` adds `GET/POST /channels` and `GET/PATCH /channels/{id}`; `content/channel_settings.py` adds the `ChannelSettings` service (registered in bootstrap). Create takes title, YouTube channel id (unique, never changes) and optional handle; update may change title, set or clear the handle (new `Channel.with_handle`), and set active, paused or archived (pending and disconnected are refused unless unchanged; archived is final). Updates need `expected_updated_at` (409 `domain.channel_conflict` when stale); duplicates are 409 `domain.channel_exists`, unknown ids 404. `core/http.py` holds `provide` (moved from `main.py`, still re-exported), `current_actor` (always `user:local-user` until login) and one error envelope for the whole app (`validation.invalid_request` with fields, `request.*`, safe 500). Audit `channel.created`, `channel.updated`, `channel.status_changed` after commit. `tests/test_channel_settings.py` (33 tests). Tests: 2023 passed. |
| D-044 | Market Settings | D-1 | PASS. User-approved rules: a strategy is configured one setting at a time. `StrategyProfile` settings may be `None` (`missing_settings`, `is_complete`; a set setting cannot be removed); migration `0003_partial_strategy.sql` rebuilds `strategy_profiles` with nullable settings (multi-column settings all-or-nothing) and keeps existing rows and `content_items` references; `DailyLimitGate` blocks with `daily_limit.no_cadence` and `BudgetGate` with `budget.no_budget` when the setting is missing. The migration runner now runs each migration with foreign keys off and `PRAGMA foreign_key_check` before commit. Market is the ISO 3166-1 alpha-2 country only. `StrategySettings` (`content/strategy_settings.py`) and `GET /channels/{id}/strategy`, `PUT /channels/{id}/strategy/market` (`content/strategy_api.py`): the first save creates the profile (201), later saves need `expected_version` (409 `domain.strategy_conflict`), archived channels are refused, only the market changes, and `strategy.created` / `strategy.market_changed` are audited after commit. Updated the B-014 test that required every setting and the B-029 tests that pinned schema version 2. `tests/test_market_settings.py` (27 tests). Tests: 2050 passed. |
| D-045 | Language Settings | D-2 | PASS. User-approved rules: `canonical_language_tag` and `LanguageSettings.canonical` store tags in canonical BCP-47 case (`en-us` -> `en-US`, `zh-hant-tw` -> `zh-Hant-TW`); secondary languages keep the user's order, at most `MAX_SECONDARY_LANGUAGES` = 5 (entity rule), and no tag repeats including the primary. `PUT /channels/{id}/strategy/languages` (`primary`, `secondary`, `expected_version`) follows the market rules: first save creates the profile (201), later saves need the version (409), only languages change, `strategy.languages_changed` audited with primary-first comma lists. `StrategySettings` now saves every setting through one `_save`. No market/language cross-check (#053). `tests/test_language_settings.py` (31 tests). Tests: 2081 passed. |
| D-046 | Audience Settings | D-3 | PASS. User-approved rules: `Audience` keeps the required `description` (now at most 500 characters) and gains optional `age_range` (`AgeRange` min/max, 13 to 100, min <= max), ordered `interests` (at most 10, each at most 50 characters, no repeats, case-insensitive) and `level` (`AudienceLevel`: beginner, intermediate, advanced, mixed). There are no fields for sensitive traits and no under-13 targeting; made-for-kids is left to publish metadata (#102, #113). Stored in `audience_json` (no migration; rows from before #046 still read). `PUT /channels/{id}/strategy/audience` follows the market rules; the body replaces the whole audience and text is trimmed. Audit summaries for settings other than market and languages are compact JSON. Updated the B-014 `as_dict` test for the new audience keys. `tests/test_audience_settings.py` (34 tests). Tests: 2115 passed. |
| D-047 | Niche Settings | D-4 | PASS. User-approved rules: `Niche` has a name (at most 100 characters) and 1 to 10 ordered `Pillar` values (`name` at most 60 characters, no two equal ignoring case; optional `description` at most 300). Stored in `niche_json` (no migration; rows from before #047 with string pillars still read). `PUT /channels/{id}/strategy/niche` replaces the whole niche (add, remove or reorder by sending the new list) and follows the market rules; text is trimmed. Updated B-014 tests and the factory to use `Pillar` and to require at least one pillar, and the B-030 repository assertion on pillar names. `tests/test_niche_settings.py` (27 tests). Tests: 2143 passed. |
| D-048 | Brand Settings | D-5 | PASS. User-approved rules: `Brand` keeps `name` (now at most 100) and `tone` (at most 200) and gains ordered, non-repeating `tone_keywords` (at most 5 x 30), `voice_dos` and `voice_donts` (each at most 10 x 200) and `banned_phrases` (at most 30 x 50), plus optional `visual` (`BrandVisual`: `primary_color` and up to 5 `accent_colors` as upper-case `#RRGGBB` with no repeats, `font_family` at most 100, `notes` at most 500; no files). The spoken TTS voice stays in `VoiceProfile` (#087). Stored in `brand_json` via `setting_dict` (no migration; rows from before #048 still read). `PUT /channels/{id}/strategy/brand` replaces the whole brand and follows the market rules. Updated the B-014 `as_dict` test. `tests/test_brand_settings.py` (39 tests). Tests: 2182 passed. |
| D-049 | Format Settings | D-6 | PASS. User-approved rules (2026-10-02): a new required strategy setting `format` (`FormatSettings`, after `brand` in `SETTING_TYPES`, so it shows in `missing_settings` until saved) holds `ShortsFormat` and `LongFormFormat` together. Each has `min_seconds`/`max_seconds` (Shorts 1 to 180, LongForm 181 to 14400, min <= max), `Resolution` (720p, 1080p, 2160p; default 1080p) and `captions` (default true); LongForm adds `chapters` (default true). `AspectRatio` is fixed per type (Shorts 9:16, LongForm 16:9). LongForm defaults can be saved while `LONGFORM_ENABLED` is off (the flag controls production, #237). Stored in the new `format_json` column (migration `0004_format_settings.sql`, `ALTER TABLE ADD COLUMN`; existing profiles get NULL). `PUT /channels/{id}/strategy/format` replaces the whole setting and follows the market rules; `strategy.format_changed` is audited as compact JSON. Updated the factory and the B-014 tests (`SETTINGS`, `as_dict`), the B-029 tests that pinned schema version 3 and the 19-column row, and the D-044 migration 0003 test (it now stores a profile without a format and expects 0003 and 0004 to apply). `tests/test_format_settings.py` (41 tests). Tests: 2223 passed. |
| D-050 | Cadence Settings | D-7 | PASS. User-approved rules (2026-10-02): `Cadence` keeps the daily limits, now capped (`shorts_per_day` 0 to 20, `longform_per_day` 0 to 5; a LongForm limit above 0 may be saved while `LONGFORM_ENABLED` is off), and gains `time_zone` (IANA name, default `UTC`; the new runtime dependency `tzdata` supplies the zone data on Windows) and a `PublishSchedule` per type (`weekdays` 1 to 7 as `Weekday`, Monday first; up to 5 `HH:MM` `times`, ascending; `min_gap_minutes` 0 to 1440). `DailyLimitGate` now counts the calendar day in the cadence time zone (`day_window(moment, time_zone)`, DST days may be 23 or 25 hours); `BudgetGate` still uses the UTC day. The schedules are preferences for #149/#150/#205; no gate reads them yet. The limit columns are unchanged; the time zone and schedules are stored in `cadence_schedule_json` (migration `0005_cadence_schedule.sql`, NULL only without limits), and a cadence from before #050 reads as UTC with the default schedule. `PUT /channels/{id}/strategy/cadence` replaces the whole cadence (time zone in any case, stored canonical; weekdays and times sorted; repeats refused) and follows the market rules. Updated the B-014 `as_dict` test, the B-029 tests that pinned schema version 4 and the row shape, and the D-044/D-049 migration tests (now expect 0005 too). `tests/test_cadence_settings.py` (48 tests) and 6 time-zone tests in `tests/test_daily_limit.py`. Tests: 2277 passed. |
| D-051 | Budget Settings | D-8 | PASS. User-approved rules (2026-10-02): `Budget` limits are `Decimal` amounts from 0 to 1,000,000 with at most 2 decimal places (daily <= monthly, one ISO 4217 currency), and the budget gains `alert_thresholds`: 1 to 5 ascending whole percentages (1 to 100) shared by the daily and monthly limits, default 50, 80, 100. D-051 only configures alerts; the budget guard (#180) raises them and the alert cards (#202) show them. `BudgetGate` now counts the day and month in the cadence time zone (`month_window(moment, time_zone)`, `strategy_time_zone`; UTC without a cadence). Changing the currency is allowed: costs in the old currency this month keep blocking with `budget.currency_mismatch` until the month ends. The thresholds are stored in `budget_alert_thresholds_json` (migration `0006_budget_alerts.sql`, NULL only without a budget); a budget from before #051 reads with the defaults. `PUT /channels/{id}/strategy/budget` takes the limits as JSON numbers or decimal strings, stores and shows them with 2 decimal places, sorts the thresholds, refuses repeats and follows the market rules. Updated the C-037 gate test helper (cadence time zone), the B-014 `as_dict` test, the B-029 tests that pinned schema version 5 and the row shape, and the D-044/D-049/D-050 migration tests (now expect 0006 too). `tests/test_budget_settings.py` (43 tests) and 6 time-zone tests in `tests/test_budget_gate.py`. Tests: 2326 passed. |
| D-052 | Monetization Settings | D-9 | PASS. User-approved rules (2026-10-02): `Monetization` now holds ordered `goals` (0 to 8, no source twice; an empty list is valid) and an optional `currency`. Each `RevenueGoal` has a `RevenueSource` from a closed list (ads, shorts_ads, memberships, super_thanks, super_chat, sponsorships, affiliate, merchandise; the old lowercase names, `RevenueRecord` unchanged until #173), an optional `monthly_target` (0 to 1,000,000, at most 2 decimal places, the budget amount rule shared through `_check_money`) and an optional `note` (at most 200). A currency (ISO 4217) is required once any goal has a target. `tracked_sources` is now a derived property. Targets are goals, not guaranteed outcomes: the strategy API adds the read-only `goals_are_not_guaranteed: true` and `notice` (`MONETIZATION_NOTICE`) to the monetization object whenever it is configured; a `PUT` that sends them is refused. No YouTube Partner Program field. Stored in `monetization_json` with targets in canonical decimal form (no migration); rows from before #052 with plain source names still read. `PUT /channels/{id}/strategy/monetization` replaces the whole setting and follows the market rules. Updated the factory and the B-014 tests (closed sources, `as_dict`). `tests/test_monetization_settings.py` (42 tests). Tests: 2369 passed. |
| D-053 | Strategy Validation | D-10 | PASS. User-approved rules (2026-10-02): a run is a move into generating (a new production or a regeneration). `validate_strategy` (`content/strategy_validation.py`) returns ordered `StrategyFinding`s (code, `FindingSeverity` blocking or warning, safe message, setting). Blocking: `strategy.missing` (no strategy), `strategy.missing_setting` (one per unset setting), `strategy.content_type_disabled` (the item's type is switched off by its flag), `strategy.no_daily_limit` (the item's type has a daily limit of 0), `strategy.zero_budget` (daily or monthly budget 0). Warnings, which never block: `strategy.language_region_mismatch` (primary language region differs from the market), `strategy.currency_mismatch` (monetization vs budget currency), `strategy.limit_for_disabled_type`, `strategy.nothing_can_run` (every limit 0) and `strategy.version_changed` (item created from an older strategy version). `StrategyGate` (`core/strategy_gate.py`, new `GateName.STRATEGY`) blocks every move into generating with one reason per blocking finding; `GET /channels/{id}/strategy/validation?content_type=` shows every finding (200 also without a strategy, 404 for an unknown channel); `StrategySettings.find` added. Updated the C-033 gate-name test and the C-042 matrix (eight gates; the strategy gate's worst world has no strategy). `tests/test_strategy_validation.py` (32 tests). Tests: 2444 passed. Phase D is complete. |

## Phase E: Research System (Prompt Pack v8, prompts 54–63)

Source: `AI_YouTube_Autonomous_Agent_PROMPT_PACK_v8_BASELINE_SAFE.pdf` and `docs/REQUIREMENTS.md`. Do not start a prompt until the previous one has PASSED.

| Task | Name | Dependency | Status |
|---|---|---|---|
| E-054 | Research Provider | D-11 | NOT_STARTED |
| E-055 | Source Model | E-1 | NOT_STARTED |
| E-056 | Source Collector | E-2 | NOT_STARTED |
| E-057 | Source Deduplication | E-3 | NOT_STARTED |
| E-058 | Topic Extractor | E-4 | NOT_STARTED |
| E-059 | Topic Scoring | E-5 | NOT_STARTED |
| E-060 | Research Report | E-6 | NOT_STARTED |
| E-061 | Research Cache | E-7 | NOT_STARTED |
| E-062 | Research Failure Recovery | E-8 | NOT_STARTED |
| E-063 | Research Tests | E-9 | NOT_STARTED |

## Current

- `PROJECT_STATE = READY_FOR_PHASE_A`
- `BASELINE_COMMIT = 3b41416`
- Phase 0 (000–011) is complete.
- A-001 Project Audit has PASSED.
- A-002 Requirements Freeze has PASSED.
- A-003 Architecture Map has PASSED.
- A-004 Folder Structure has PASSED.
- A-005 Configuration Contract has PASSED.
- A-006 Feature Flags has PASSED.
- A-007 Logging Contract has PASSED.
- A-008 Error Model has PASSED.
- A-009 Dependency Injection has PASSED.
- A-010 Health Check has PASSED.
- A-011 Audit Event Model has PASSED.
- A-012 Build Baseline has PASSED. Phase A is complete.
- B-013 Channel Entity has PASSED.
- B-014 StrategyProfile Entity has PASSED.
- B-015 ContentItem Entity has PASSED.
- B-016 Artifact Entity has PASSED.
- B-017 Script Entity has PASSED.
- B-018 Voice Entity has PASSED.
- B-019 Rights Entity has PASSED.
- B-020 QC Entity has PASSED.
- B-021 Approval Entity has PASSED.
- B-022 Publish Entity has PASSED.
- B-023 Analytics Entity has PASSED.
- B-024 Revenue Entity has PASSED.
- B-025 Cost Entity has PASSED.
- B-026 Comment Entity has PASSED.
- B-027 AI Job Entity has PASSED.
- B-028 Experiment Entity has PASSED.
- B-029 Migrations has PASSED.
- B-030 Repository Tests has PASSED. Phase B is complete.
- C-031 Status Enum has PASSED.
- C-032 Transition Rules has PASSED.
- C-033 Pipeline Gate Contract has PASSED.
- C-034 Approval Gate has PASSED.
- C-035 Version Invalidation has PASSED.
- C-036 Daily Limit Gate has PASSED.
- C-037 Budget Gate has PASSED.
- C-038 Rights Gate has PASSED.
- C-039 Policy Gate has PASSED.
- C-040 Kill Switch Gate has PASSED.
- C-041 Idempotency Gate has PASSED.
- C-042 Gate Tests has PASSED. Phase C is complete.
- D-043 Channel Settings has PASSED.
- D-044 Market Settings has PASSED.
- D-045 Language Settings has PASSED.
- D-046 Audience Settings has PASSED.
- D-047 Niche Settings has PASSED.
- D-048 Brand Settings has PASSED.
- D-049 Format Settings has PASSED.
- D-050 Cadence Settings has PASSED.
- D-051 Budget Settings has PASSED.
- D-052 Monetization Settings has PASSED.
- D-053 Strategy Validation has PASSED. Phase D is complete.
- Next task: **E-054 Research Provider**. It is NOT_STARTED.
