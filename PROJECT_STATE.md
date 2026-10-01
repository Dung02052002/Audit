# Project State

Last updated: 2026-10-01 (C-039 Policy Gate)

This file is the handoff document. A new session should read it, together with `TASK_STATUS.md`, before doing anything else. There is no need to audit the repository again from the start.

## Current state

```
PROJECT_STATE      = READY_FOR_PHASE_A
BASELINE_COMMIT    = 3b41416
CURRENT_CHECKPOINT = 2e82cfd
BASELINE_STATUS    = CLEAN
TEST_STATUS        = PASS
LINT_STATUS        = PASS
BUILD_STATUS       = PASS
KNOWN_FAILURES     = NONE
UNKNOWN_BLOCKERS   = NONE
NEXT_TASK          = C-040 Kill Switch Gate
```

## Next task

**C-040 Kill Switch Gate** (Prompt Pack v8, Phase C State Machine & Control Gates, prompt 40). It depends on C-039, which has PASSED.

> Block all new production actions when emergency stop is active.

Note: no kill switch or emergency-stop flag exists in the code yet (the feature flags are A-006). The pack does not say where the emergency stop is stored or who may turn it on and off, which moves count as "new production actions" (only Draft → Generating, every move into Generating, or also publishing), or whether jobs already running are affected. A `GateName` value for it does not exist yet. Confirm these with the user before implementing.

C-039 Policy Gate has PASSED. `PolicyGate` in `core/policy_gate.py` blocks the move to publishing on every blocking finding of the item's newest `PolicyCheck` (`content/policy.py`, read shape only until #080/#083), and when the item has never been checked. See ARCHITECTURE §5.3. C-038 Rights Gate has PASSED. `RightsGate` in `core/rights_gate.py` blocks the move to publishing while any rights record of the item is unresolved at level high or unknown (unknown counts as high); low, medium, resolved records and items without records pass. See ARCHITECTURE §5.3. C-037 Budget Gate has PASSED. `BudgetGate` in `core/budget_gate.py` blocks every move into generating once the channel's daily or monthly spend reaches its budget (UTC day and month, `Decimal`), and on another currency or a missing strategy. See ARCHITECTURE §5.3. C-036 Daily Limit Gate has PASSED. `DailyLimitGate` in `core/daily_limit_gate.py` limits production and publishing per channel, content type and UTC day by the strategy cadence, using the new `production_starts` log (migration 0002, `start_production` in `core/production.py`). See ARCHITECTURE §5.3. C-035 Version Invalidation has PASSED. `VersionInvalidation` in `core/version_invalidation.py` stores new artifact versions and invalidates stale pending/approved requests in the same transaction, moves reviewed items back to generating, and audits `approval.invalidated` after the commit (see ARCHITECTURE §5.3). C-034 Approval Gate has PASSED. `ApprovalGate` in `core/approval_gate.py` blocks the move to publishing unless the newest approval request is approved and binds every artifact kind at its latest version (see ARCHITECTURE §5.3). C-033 Pipeline Gate Contract has PASSED. `core/gates.py` defines the shared gate interface (see ARCHITECTURE §5.3): closed `GateName`, pass/block `GateResult` with `GateReason`s, `GateContext`, the sync `PipelineGate` protocol, `evaluate_gates` (runs all, fails closed) and `GateReport`/`GateBlockedError`. C-032 Transition Rules has PASSED. `ALLOWED_TRANSITIONS` in `core/content_item.py` holds the user-approved table (see ARCHITECTURE §5.2), and `ContentItem.with_status` raises `ContentTransitionError` (`domain.content_transition_blocked`) for any other move. Transitions do not check actors or gates; that is the job of #033–#041. C-031 Status Enum has PASSED. `ContentStatus` in `core/content_item.py` is the single lifecycle status enum: the ten #031 statuses in the prompt's order, stored as snake_case values (`PreviewReady` → `preview_ready`). `tests/test_status_enum.py` checks the mapping, the refusal of other values, a SQLite round-trip for every status and the CHECK constraint. B-030 Repository Tests has PASSED, and Phase B is complete. `core/db/database.py` provides `Database` and `transaction()`; `core/db/repositories/` holds one SQLite repository per aggregate (add-only for immutable records, optimistic `update` raising `ConcurrencyError`, no deletes) and `SqliteAuditSink`, which bootstrap uses outside the TEST environment. Record audit events after a business transaction ends, because SQLite allows one writer at a time. Test data comes from `tests/factories.py` and the `database` fixture in `tests/conftest.py`. B-029 Migrations has PASSED. The database is SQLite (Q5 resolved by the user). `core/db/migrate.py` runs numbered forward-only SQL migrations with checksums, one transaction per migration, a backup in `backups/` before migrating an existing database, and `restore_backup`. `core/db/migrations/0001_initial_schema.sql` holds STRICT tables for every entity and an append-only `audit_events` table. The path is `Settings.database_path` (`AI_YOUTUBE_AGENT_DATABASE_PATH`, default `data/ai_youtube_agent.db`, ignored by Git), and migrations run only at startup through the FastAPI lifespan. B-028 Experiment Entity has PASSED. The entity is in `src/ai_youtube_agent/content/experiment.py`: a frozen `Experiment` that any actor may propose but only a user may start, conclude or cancel. A conclusion is only a record and is never applied to strategy. B-027 AI Job Entity has PASSED. The entities are in `src/ai_youtube_agent/pipeline/job.py` (C16 lives in `pipeline/`): a user-only `Session`, and a frozen `AIJob` with a supplied idempotency key, attempts, guarded transitions and an append-only checkpoint history. Succeeded and cancelled are final. B-026 Comment Entity has PASSED. The entities are in `src/ai_youtube_agent/content/comment.py`: `Comment` (minimal, display name only), `CommentClassification` (label, actor, rationale, history kept) and `ReplyDraft` (always starts as draft, with no approve or post method until #186–#188). B-025 Cost Entity has PASSED. The entity is in `src/ai_youtube_agent/content/cost.py`: a frozen `CostRecord` per channel with a `CostCategory` (production, api, tts, render, storage, llm), provider, non-negative `Decimal` amount, ISO 4217 currency, UTC `incurred_at`, and optional item and job/artifact ref. B-024 Revenue Entity has PASSED. The entity is in `src/ai_youtube_agent/content/revenue.py`: a frozen `RevenueRecord` with a `RevenueStage` (estimated or final as separate records), scope, revenue type, data source, non-negative `Decimal` amount, ISO 4217 currency, UTC period and retrieval time, and derived freshness. B-023 Analytics Entity has PASSED. The entity is in `src/ai_youtube_agent/content/analytics.py`: a frozen `MetricSnapshot` (channel or video scope, source, UTC period, retrieval time, read-only `Decimal` metrics) with derived freshness through `age_at` and `is_stale`. B-022 Publish Entity has PASSED. The entity is in `src/ai_youtube_agent/pipeline/publish.py` (C12 lives in `pipeline/`): a frozen `PublishJob` with a supplied idempotency key that can only be created from an approved `ApprovalRequest`, counts attempts, and becomes final once it succeeds with a `PublishResult`. B-021 Approval Entity has PASSED. The entity is in `src/ai_youtube_agent/content/approval.py`: a frozen `ApprovalRequest` that starts pending and is bound to exact artifact versions through `ArtifactBinding` snapshots (id, kind, version, sha256). Decision methods come with #139–#142. B-020 QC Entity has PASSED. The entity is in `src/ai_youtube_agent/content/qc.py`: a frozen `QCResult` bound to a content item and the exact artifact versions checked, holding `QCCheck` values (pass, warn, fail) and a derived worst-of `status`. B-019 Rights Entity has PASSED. The entity is in `src/ai_youtube_agent/content/rights.py`: a frozen `RightsRecord` per asset with source, optional licence, `RiskLevel` and `RiskResolution`. Only a user may resolve a risk, and changing the level or licence makes it unresolved again. B-018 Voice Entity has PASSED. The entities are in `src/ai_youtube_agent/content/voice.py`: a user-controlled `VoiceProfile` with a version and a user-only actor guard, and `AudioMetadata` that links an audio `Artifact`, the exact `Script` version and the voice profile version. B-017 Script Entity has PASSED. The entities are in `src/ai_youtube_agent/content/script.py`: immutable `Script` versions, `Claim` bound to one exact script version, and `Evidence` with an opaque `source_ref`, all related by id. B-016 Artifact Entity has PASSED. The entity is in `src/ai_youtube_agent/core/artifact.py`. Each `Artifact` is one immutable version (own id, `content_item_id`, `ArtifactKind`, version from 1, `uri`, `sha256`, `size_bytes`, `media_type`). `next_version` makes a new record and refuses an unchanged sha256. B-015 ContentItem Entity has PASSED. The entity is in `src/ai_youtube_agent/core/content_item.py` (C2 lives in `core/`). It is frozen, links to `channel_id`, `strategy_profile_id` and `strategy_version`, has `ContentType` SHORTS or LONGFORM, and a `ContentStatus` with the 10 values of #031, starting at draft. `with_status` has no transition rules until #032. B-014 StrategyProfile Entity has PASSED. The entity is in `src/ai_youtube_agent/content/strategy.py`. It is frozen, belongs to one channel (`channel_id`), and holds one typed value object per setting. Only a user `Actor` can create or update it (R-09), and every change bumps `version`. Detailed setting rules are left to #044–#053. B-013 Channel Entity has PASSED. The entity is in `src/ai_youtube_agent/content/channel.py`. It is frozen, its id is stable, and its statuses are pending, active, paused, disconnected and archived. Archived is final. No other transition rules exist yet. Phase A (A-001 to A-012) is complete. A-012 Build Baseline has PASSED. The foundation build baseline is `59fcd2d`, and the report is `docs/A-012_BUILD_BASELINE.md`: 132 tests passed, with no failures and no infrastructure blockers. A-011 Audit Event Model has PASSED. The model is in `src/ai_youtube_agent/core/audit.py`. Code records events with `AuditLog.record()`, which it resolves from the container. The sink is `InMemoryAuditSink` until persistence (#029) adds a database sink in `bootstrap.py`. A-010 Health Check has PASSED. The status model and `HealthRegistry` are in `src/ai_youtube_agent/core/health.py`. The registry is a singleton in the container, and providers added later register a `CheckKind.PROVIDER` check in `bootstrap.py`. `GET /health` returns `status`, `version` and `checks`, and answers 503 only when an application check fails. A-009 Dependency Injection has PASSED. The container is in `src/ai_youtube_agent/core/di.py`, the composition root is `src/ai_youtube_agent/bootstrap.py`, and `main.create_app()` puts the container on `app.state.container`. A-008 Error Model has PASSED. Errors are in `src/ai_youtube_agent/core/errors.py`. A-007 Logging Contract has PASSED. Logging is in `src/ai_youtube_agent/core/log.py`. A-006 Feature Flags has PASSED. The flags are in `src/ai_youtube_agent/core/flags.py`, exposed as `Settings.flags`.

A-005 Configuration Contract has PASSED. Settings are in `src/ai_youtube_agent/core/config.py`. A-004 Folder Structure has PASSED. The layout is in `docs/ARCHITECTURE.md` section 7 and the README. A-003 Architecture Map has PASSED. The map is in `docs/ARCHITECTURE.md`, and section 8 lists 8 open questions. A-002 Requirements Freeze has PASSED. The frozen requirements are in `docs/REQUIREMENTS.md`, which includes the full 238-prompt catalog copied verbatim from the pack. A-001 Project Audit has PASSED, and its report is `docs/A-001_PROJECT_AUDIT.md`. Phase 0 (000–011) is complete, which meets the pack rule that Phase A may start only after Prompt 011 PASSES. Run the Phase A prompts in order, and do not start a prompt until the previous one has PASSED. The full list is in `TASK_STATUS.md`.

## Known failures

| Category | Count |
|---|---|
| PRE_EXISTING | 0 |
| ENVIRONMENT | 0 |
| TOOLING | 0 |
| UNKNOWN | 0 |

Two notes do not block anything:
- The Docker daemon is not reachable. Docker is deferred.
- On 2026-09-30 Windows Application Control blocked the `pytest.exe` launcher (`uv run pytest` fails with os error 4551). `uv run python -m pytest` and `uv run python -m ruff` work and give the same results. This is an ENVIRONMENT note, not a code failure.
- uv reports "Failed to hardlink files" because the uv cache is on drive C: and the project is on drive D:. Installs still succeed.

## Foundation build baseline (A-012)

Measured on `59fcd2d` on 2026-09-28. The full report is `docs/A-012_BUILD_BASELINE.md`.

| Check | Command | Result |
|---|---|---|
| Environment | `uv sync --locked` | PASS (26 packages) |
| Lockfile | `uv lock --check` | PASS |
| Tests | `uv run pytest` | 132 passed, 0 failed. Also passes with `-W error` and on 5 repeated runs |
| Lint | `uv run ruff check .` | PASS |
| Format | `uv run ruff format --check .` | PASS |
| Build | `uv build` | PASS (wheel + sdist). The installed wheel imports and runs |
| Run | `uv run uvicorn ai_youtube_agent.main:app` | `GET /health` returns 200 with `status: ok` and the checks `settings` and `feature_flags` |

Any failure that appears after `59fcd2d` comes from a later change. It is not pre-existing.

## Bootstrap baseline

Commit `3b41416` (`chore: bootstrap ai_youtube_agent project`) is the code baseline after Project Initialization. Later commits up to `6ce7d3b` changed documentation only.

| Check | Command | Result |
|---|---|---|
| Tests | `uv run pytest` | 1 passed, 0 failed |
| Lint | `uv run ruff check .` | PASS |
| Format | `uv run ruff format --check .` | PASS |
| Lockfile | `uv lock --check` | PASS (24 packages) |
| Build | `uv build` | PASS (wheel + sdist) |
| Run | `uv run uvicorn ai_youtube_agent.main:app` | `GET /health` returns 200 `{"status":"ok","version":"0.1.0"}` |

These checks were last run in Task 010. Any failure that appears after `3b41416` comes from a later change. It is not pre-existing.

## Repository status

| Item | Value |
|---|---|
| Local path | `D:\Project_Audit` |
| Remote | `origin` = https://github.com/Dung02052002/Audit.git |
| Branch | `main`, tracking `origin/main` |
| Pushed checkpoint | `3ebc29c` (`docs: record A-001 project audit`). Later commits are local only |
| Tracked code | `src/ai_youtube_agent/` (`__init__.py`, `main.py`, `bootstrap.py`, `core/config.py`, `core/flags.py`, `core/log.py`, `core/errors.py`, `core/di.py`, `core/health.py`, `core/audit.py`, `core/content_item.py`, `core/artifact.py`, `content/channel.py`, `content/strategy.py`, `content/script.py`, `content/voice.py`, `content/rights.py`, `content/policy.py`, `content/qc.py`, `content/approval.py`, `content/analytics.py`, `content/revenue.py`, `content/cost.py`, `content/comment.py`, `content/experiment.py`, `pipeline/publish.py`, `core/gates.py`, `core/approval_gate.py`, `core/version_invalidation.py`, `core/production_start.py`, `core/production.py`, `core/daily_limit_gate.py`, `core/budget_gate.py`, `core/rights_gate.py`, `core/policy_gate.py`, `core/db/` (`codec.py`, `migrate.py`, `migrations/0001_initial_schema.sql`, `migrations/0002_production_starts.sql`, `database.py`, `repositories/`), `pipeline/job.py`, and the packages `core/`, `content/`, `providers/`, `pipeline/`), `dashboard/README.md`, `.env.example`, `tests/` (`test_health.py`, `test_folder_structure.py`, `test_config.py`, `test_flags.py`, `test_log.py`, `test_errors.py`, `test_di.py`, `test_bootstrap.py`, `test_health_check.py`, `test_audit.py`, `test_channel.py`, `test_strategy.py`, `test_content_item.py`, `test_artifact.py`, `test_script.py`, `test_voice.py`, `test_rights.py`, `test_qc.py`, `test_approval.py`, `test_publish.py`, `test_analytics.py`, `test_revenue.py`, `test_cost.py`, `test_comment.py`, `test_job.py`, `test_experiment.py`, `test_db_codec.py`, `test_migrations.py`, `test_repositories.py`, `test_audit_sink.py`, `test_status_enum.py`, `test_transitions.py`, `test_gates.py`, `test_approval_gate.py`, `test_version_invalidation.py`, `test_daily_limit.py`, `test_budget_gate.py`, plus `conftest.py` and `factories.py`) |
| Ignored | `.venv/`, `dist/`, `.pytest_cache/`, `.ruff_cache/`, `__pycache__/`, `data/` (local SQLite database and backups), `.env`, `.env.*` (except `.env.example`) |

## Toolchain

| Item | Value |
|---|---|
| OS | Windows 11 Pro. Shells: PowerShell 5.1 and Git Bash |
| Language / runtime | Python 3.11.9 (`requires-python = ">=3.11,<3.12"`) |
| Project type | Backend / AI agent |
| Framework | FastAPI 0.141, served by uvicorn 0.54. Settings use pydantic-settings 2.15 |
| Package manager | uv 0.12.1 (build backend `uv_build`) |
| Test runner | pytest 9.1.1, with `httpx2` for `TestClient` |
| Formatter / linter | ruff 0.16.9 (rules E, F, I, UP, B, SIM; line length 88) |
| Package | `ai_youtube_agent` |
| Layout | `src/ai_youtube_agent/`, `tests/` |
| Docker | Deferred. The CLI is installed but the daemon is not running |
| CI | Deferred |

## Invariants

Every future task must keep these true:

1. **Prompt Pack v8 is the source of truth** for task definitions. The file is `AI_YouTube_Autonomous_Agent_PROMPT_PACK_v8_BASELINE_SAFE.pdf`, stored outside the repository in `D:\Downloads`. Do not invent or guess tasks.
2. **One task at a time.** Follow Inspect → Baseline → Implement only scope → Targeted tests → Relevant regression tests → Reclassify failures → Report → PASS/FAIL. Stop after each task.
3. **Global Baseline Rule.** A task never fails because of an unrelated pre-existing error. A regression caused by the task is a FAIL. A failure of unknown origin is UNKNOWN: stop and investigate.
4. **The stack is fixed:** Python 3.11, FastAPI, uv, pytest, ruff, with the `src/` and `tests/` layout. Change it only if the user asks.
5. **Dependencies only through uv** (`uv add` / `uv add --dev`), and `uv.lock` is committed.
6. **Green gate before finishing a task:** `uv run pytest`, `uv run ruff check .` and `uv run ruff format --check .` must pass.
7. **No secrets in source control.** Credentials, API keys and OAuth secrets stay out of Git. `.env` is ignored.
8. **No auto-publish and no strategy drift.** A production publish always needs a valid approval. Market, language, niche, format, budget and channel are never changed on the AI's own initiative.
9. **Do not re-run Phase 0 or re-bootstrap.** The project exists. Do not reset or delete it.
10. **Keep state files current.** Update `PROJECT_STATE.md`, `TASK_STATUS.md` and `CHANGELOG.md` at the end of each task.
11. **Push only when the user asks.**

## How to resume

```sh
git status --short --branch   # expect a clean tree on main
uv sync                       # recreate .venv if needed
uv run python -m pytest       # expect 1502 passed (132 at the A-012 baseline)
uv run ruff check . && uv run ruff format --check .
```

Then start the task marked NOT_STARTED first in `TASK_STATUS.md`, which is C-040 right now.

## State history

| Step | Classification | Notes |
|---|---|---|
| Tasks 001–005 (initial audit) | EMPTY / CLEAN EMPTY STATE | Correct at that time: the repository had 0 files and no Git. |
| Project Initialization | EMPTY → EXISTING | The minimal project was created and committed as `3b41416`. |
| State drift audit | EXISTING | Confirmed from the filesystem. |
| State tracking (`5821e98`) | EXISTING_INITIALIZED | State files added. Code is unchanged from `3b41416`. |
| Task 007 Clean Baseline Gate | EXISTING_INITIALIZED → READY_FOR_PHASE_A | Gate PASS. Baseline Gate is OPEN. |
| Tasks 008–010 | READY_FOR_PHASE_A | Bootstrap app, tests and clean gate verified. No code changes. |
| Task 011 Project Handoff | READY_FOR_PHASE_A | Handoff completed. Phase 0 is complete. |
| A-001 Project Audit | READY_FOR_PHASE_A | Audit report written to `docs/A-001_PROJECT_AUDIT.md`. No code changes. |
| A-002 Requirements Freeze | READY_FOR_PHASE_A | Requirements frozen in `docs/REQUIREMENTS.md`. No code changes. |
| A-003 Architecture Map | READY_FOR_PHASE_A | Architecture documented in `docs/ARCHITECTURE.md`. No code changes. |
| A-004 Folder Structure | READY_FOR_PHASE_A | Created `core/`, `content/`, `providers/`, `pipeline/` (empty packages) and `dashboard/`. Tests now 8. |
| A-005 Configuration Contract | READY_FOR_PHASE_A | Typed settings in `core/config.py` (pydantic-settings). Tests now 22. |
| A-006 Feature Flags | READY_FOR_PHASE_A | Six flags with safe defaults in `core/flags.py`. Tests now 36. |
| A-007 Logging Contract | READY_FOR_PHASE_A | JSON logging with correlation, session and job IDs in `core/log.py`. Tests now 52. |
| A-008 Error Model | READY_FOR_PHASE_A | Typed errors and safe public messages in `core/errors.py`. Tests now 68. |
| A-009 Dependency Injection | READY_FOR_PHASE_A | Container in `core/di.py`, composition root `bootstrap.py`, and `create_app()` in `main.py`. Tests now 87. |
| A-010 Health Check | READY_FOR_PHASE_A | Status model and `HealthRegistry` in `core/health.py`. `/health` adds `checks` and answers 503 when down. Tests now 106. |
| A-011 Audit Event Model | READY_FOR_PHASE_A | Frozen `AuditEvent`, append-only `AuditSink`, `InMemoryAuditSink` and `AuditLog` in `core/audit.py`. Tests now 132. |
| A-012 Build Baseline | READY_FOR_PHASE_A | Full build and test baseline recorded on `59fcd2d` in `docs/A-012_BUILD_BASELINE.md`. No failures, no blockers, no code changes. Phase A is complete. |
| B-013 Channel Entity | READY_FOR_PHASE_A | Frozen `Channel` with a stable id, YouTube identifiers, status and UTC timestamps in `content/channel.py`. Tests now 166. |
| B-014 StrategyProfile Entity | READY_FOR_PHASE_A | Frozen `StrategyProfile` for one channel with typed market, language, audience, niche, brand, cadence, budget and monetization settings in `content/strategy.py`. Only a user actor can change it. Tests now 256. |
| B-015 ContentItem Entity | READY_FOR_PHASE_A | Frozen `ContentItem` with `ContentType` (shorts, longform), the 10 `ContentStatus` values of #031 and links to channel and strategy version in `core/content_item.py`. Tests now 295. |
| B-016 Artifact Entity | READY_FOR_PHASE_A | Immutable `Artifact` version records (video, audio, subtitles, thumbnail, metadata) with uri, sha256, size and media type in `core/artifact.py`. Tests now 332. |
| B-017 Script Entity | READY_FOR_PHASE_A | Immutable `Script` versions, `Claim` per script version and `Evidence` links in `content/script.py`. Tests now 369. |
| B-018 Voice Entity | READY_FOR_PHASE_A | User-controlled `VoiceProfile` and `AudioMetadata` linked to an audio artifact, script version and voice version in `content/voice.py`. Tests now 424. |
| B-019 Rights Entity | READY_FOR_PHASE_A | Frozen `RightsRecord` per asset with provenance, licence, risk level and user-only resolution in `content/rights.py`. Tests now 465. |
| B-020 QC Entity | READY_FOR_PHASE_A | Frozen `QCResult` with `QCCheck` values and a derived worst-of status, bound to exact artifact versions, in `content/qc.py`. Tests now 508. |
| B-021 Approval Entity | READY_FOR_PHASE_A | Frozen pending `ApprovalRequest` bound to artifact version snapshots (id, kind, version, sha256) in `content/approval.py`. Tests now 549. |
| B-022 Publish Entity | READY_FOR_PHASE_A | Frozen `PublishJob` (approved-only creation, attempts, final success) and `PublishResult` in `pipeline/publish.py`. Tests now 604. |
| B-023 Analytics Entity | READY_FOR_PHASE_A | Frozen `MetricSnapshot` with source, period, retrieval time, `Decimal` metrics and derived freshness in `content/analytics.py`. Tests now 645. |
| B-024 Revenue Entity | READY_FOR_PHASE_A | Frozen `RevenueRecord` (estimated and final as separate records) with currency, period, source and derived freshness in `content/revenue.py`. Tests now 682. |
| B-025 Cost Entity | READY_FOR_PHASE_A | Frozen `CostRecord` per channel with category, provider, amount, currency and `incurred_at` in `content/cost.py`. Tests now 715. |
| B-026 Comment Entity | READY_FOR_PHASE_A | `Comment`, `CommentClassification` and draft-only `ReplyDraft` in `content/comment.py`. Tests now 753. |
| B-027 AI Job Entity | READY_FOR_PHASE_A | User-only `Session` and resumable `AIJob` with attempts, guarded status and checkpoint history in `pipeline/job.py`. Tests now 834. |
| B-028 Experiment Entity | READY_FOR_PHASE_A | Frozen `Experiment` registry entry (propose by anyone; start, conclude and cancel by a user only; conclusion never applied) in `content/experiment.py`. Tests now 900. |
| B-029 Migrations | READY_FOR_PHASE_A | SQLite database foundation in `core/db/`: codec, forward-only migration runner with checksums, transactions, backup and restore, and the initial STRICT schema. Migrations run at startup only. Tests now 1003. |
| B-030 Repository Tests | READY_FOR_PHASE_A | `Database`, SQLite repositories for every aggregate with optimistic updates, `SqliteAuditSink`, fixture factories. Phase B complete. Tests now 1072. |
| C-031 Status Enum | READY_FOR_PHASE_A | `ContentStatus` verified as the single #031 status enum across Python and SQLite; docstring, docs note and 30 tests. No behaviour change. Tests now 1102. |
| C-032 Transition Rules | READY_FOR_PHASE_A | User-approved `ALLOWED_TRANSITIONS` table and typed `ContentTransitionError`; `ContentItem.with_status` is guarded. Tests now 1307. |
| C-033 Pipeline Gate Contract | READY_FOR_PHASE_A | Shared gate interface in `core/gates.py` (pass/block results, run-all report, fail closed). No concrete gates. Tests now 1341. |
| C-034 Approval Gate | READY_FOR_PHASE_A | `ApprovalGate` (newest request approved, every kind at its latest version) in `core/approval_gate.py`. Tests now 1363. |
| C-035 Version Invalidation | READY_FOR_PHASE_A | `VersionInvalidation` (store version + invalidate stale approvals atomically, reset item, audit after commit) in `core/version_invalidation.py`. Tests now 1400. |
| C-036 Daily Limit Gate | READY_FOR_PHASE_A | `DailyLimitGate` (cadence per type, production and publish counted separately per UTC day), migration 0002 `production_starts`. Tests now 1438. |
| C-037 Budget Gate | READY_FOR_PHASE_A | `BudgetGate` (daily and monthly budget, spend >= limit, UTC day and month, currency mismatch blocks). Tests now 1468. |
| C-038 Rights Gate | READY_FOR_PHASE_A | `RightsGate` (publish blocked by unresolved high or unknown rights records, one reason per record, no records pass). Tests now 1486. |
| C-039 Policy Gate | READY_FOR_PHASE_A | `PolicyGate` (newest `PolicyCheck` only, blocking findings block publish, warnings pass, no check blocks). Tests now 1502. |

The change from EMPTY to EXISTING is a valid state transition caused by Project Initialization. It is not a pre-existing failure. The files in the baseline were created by that task, so they are not PRE_EXISTING relative to the original empty state.

## Scope not started

- Phases C–T. Phases A and B are complete
- AI YouTube business logic (Research, Script, Voice, YouTube API, etc.)
- Docker and CI
