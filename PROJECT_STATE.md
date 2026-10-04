# Project State

Last updated: 2026-10-04 (G-078b Rights Gate Integration, after G-078 Rights Risk Engine)

This file is the handoff document. A new session should read it, together with `TASK_STATUS.md`, before doing anything else. There is no need to audit the repository again from the start.

## Current state

```
PROJECT_STATE      = READY_FOR_PHASE_A
BASELINE_COMMIT    = 3b41416
CURRENT_CHECKPOINT = 541c8af
BASELINE_STATUS    = CLEAN
TEST_STATUS        = PASS
LINT_STATUS        = PASS
BUILD_STATUS       = PASS
KNOWN_FAILURES     = NONE
UNKNOWN_BLOCKERS   = NONE
NEXT_TASK          = G-079 Policy Rule Interface
```

## Next task

**G-079 Policy Rule Interface** (Prompt Pack v8, Phase G Rights, Policy & AI Disclosure, prompt 79). It depends on G-078 (Rights Risk Engine), which has PASSED, as has the follow-up G-078b.

> Create versioned policy rule interface and mock rules.

Note: the pack line is one sentence, so the scope must be confirmed with the user before implementing, offering 3-4 alternatives per question: the shape of a rule (a Protocol with id, version and evaluate, and its input: content, metadata, script or artifacts), how versions are kept (a code constant, a stored registry table, or both), which mock rules ship, and how it relates to the C-039 read shape `PolicyCheck`/`PolicyFinding` in `content/policy.py` (evaluation and storage are #080, the report #083).

G-078b Rights Gate Integration has PASSED (a follow-up of G-078 from a spec the user pasted on 2026-10-04; not a pack prompt, so pack G-079 stays next; it does the rights half of what #084 Publishing Blocker names). Decisions the user approved: new `core/publish_gate.py` with `PublishGate(database, settings)` registered in the bootstrap and not called by anything yet (the runner is #206): `evaluate(item, *, actor, at=None)` builds the `GateContext` for the move to PUBLISHING (a non-APPROVED item raises before a connection opens) and runs, in the C-042 order, `ApprovalGate`, `DailyLimitGate`, `RightsGate` (levels from `Settings.rights_block_levels` via `blocking_levels_for`, validated at construction, plus `RepositoryFreshness`) and `IdempotencyGate`, all on real repositories over one connection with a deferred `BEGIN` read snapshot that is rolled back (only when still in a transaction) and closed in a `finally`; it writes nothing and audits nothing; `ensure_can_publish` raises `GateBlockedError`; `DEFERRED_GATES = (POLICY, KILL_SWITCH)` is pinned by a test (no policy store before #080, no kill switch store before #213; budget and strategy guard GENERATING only). `RightsGate(rights, *, blocking_levels=..., freshness=None)`: with no freshness source the behaviour, codes and messages are those of C-038/G-078; with one, an unresolved record that is not blocked by its level blocks with `rights.assessment_stale` when it has no assessment or its latest assessment's `AssessmentBasis` (asset_id, provenance_id) differs from the current one (one reason per record, the level reason first; resolved records are never checked; the gate imports no classification rule). The engine and `RepositoryFreshness` share `current_facts(assets, provenances, asset_ref, channel_id)` in `content/rights_assessment.py` (the asset only when it is in the item's channel, and its newest provenance), so an unregistered asset is (None, None) on both sides and blocks as high, never as stale forever. `RightsRiskEngine.assess` now checks every unresolved record before any write and raises a plain `ValueError` naming only the record id when the clock is earlier than the record's `updated_at` or its latest assessment's `created_at` (an equal time is allowed and idempotent). No migration. Review (opus/high): PASS, 0 blocking; one fresh sonnet/high fix round fixed the should-fix (the copied lookup, now `current_facts` with a contract test) and two nits (guarded ROLLBACK; tests that prove a deferred snapshot, one connection and its close); open nits: `at` is read just before the snapshot, up to three reads per unresolved record, and the rules version is not part of the basis (ask the user when the rules first change). Tests: 5049 collected and passed in one full suite (4976 before, 73 new: 49 in `tests/test_publish_gate.py`, 18 in `tests/test_rights_gate.py`, 6 in `tests/test_rights_risk_engine.py`).

G-078 Rights Risk Engine has PASSED. Scope the user approved (2026-10-04): a service registered in the bootstrap with an append-only assessment history (migration 0025) and a repository, no HTTP route, no wiring into the shared publish gate (#084). `content/rights_assessment.py`: `RULES_VERSION = "rights-rules-v1"`, frozen `RightsAssessment` (id, rights_record_id, content_item_id, asset_id or None, level low/medium/high never unknown, rule_codes (at least one stable code, never text), rules_version, provenance_id or None (implies asset_id), assessed_by `Actor`, created_at UTC; `outcome_key()` = level, codes, provenance_id, rules_version) and the pure `classify(asset, item_channel_id, provenance)`, deterministic, no AI, one code per outcome: asset missing or of another channel high `asset.not_registered` (asset and provenance id None); `unknown` high; `licensed` no provenance high, provenance without licence name/ref high (defensive), licence only medium, licence + (licence URL or proof) low; `public_domain` no provenance, no source URL or source URL only medium, source URL + (proof or licence URL) low; `user_owned` no provenance medium, owner + (proof or SHA-256) low, otherwise medium; `generated` low. provenance_id is the current provenance id whenever the asset is registered, so a new provenance record gives a new row. `content/rights_risk_engine.py`: `RightsRiskEngine(database, audit, *, clock=None)` with `assess(content_item_id, *, actor)` (any actor stored; a missing item is `ContentItemNotFoundError` 404; one `BEGIN IMMEDIATE` transaction, the clock read once inside it, a naive or non-UTC clock a plain `ValueError`; a resolved record is skipped before any read, never changed and gets no row; an unresolved record is unchanged only when its latest assessment has the same outcome key and the record already holds that level, otherwise a new row is added and a different level is applied with `with_risk_level` and the optimistic update, so the history explains every level and a drifted level is rewritten; any error rolls the whole run back) returning `RightsAssessmentRun` (assessments, changed = new rows, skipped_resolved, assessed, level_counts), and `latest(rights_record_id)` / `history(rights_record_id)` (a missing record is `RightsRecordNotFoundError`, 404, `domain.rights_record_not_found`; they write nothing). Audit after commit, only when changed > 0: `rights.assessed` on `EntityRef("content_item", id)` with assessed, changed, skipped_resolved, low, medium and high counts, never a URL, licence or text. `core/db/repositories/rights_assessment.py` (`RightsAssessmentRepository`: `add`, `latest`, `list_by_rights_record`, `list_by_content_item`; no update or delete). Migration 0025 (`0025_rights_assessments.sql`, forward-only, STRICT): `rights_assessments` with FKs, the enum CHECK on level plus `level <> 'unknown'`, a non-empty JSON array of codes, provenance implies asset, no UNIQUE, indexes on `(rights_record_id, created_at)` and `(content_item_id, created_at)`; no other table changed. Blocking: `Settings.rights_block_levels` (env `AI_YOUTUBE_AGENT_RIGHTS_BLOCK_LEVELS`, a comma list or a JSON list, items stripped, values `medium`/`high`, case-sensitive, default `{high}`, must contain high, `RightsBlockLevel` in `core/config.py`); `RightsGate(rights, *, blocking_levels=BLOCKING_LEVELS)` (must hold HIGH and UNKNOWN, only MEDIUM/HIGH/UNKNOWN, else `ValueError`), new reason `rights.unresolved_medium`, helper `blocking_levels_for` (always adds unknown); the default gate, its codes and messages are those of C-038. The gate is not built in `src` yet, so the setting has no consumer until #084. `content/rights.py`, `RightsRecordRepository`, the asset and provenance services and migrations 0001-0024 are unchanged. Review (opus/high): PASS, 0 blocking; the main session fixed the two should-fix items (the 0025 header comment, the `.env.example` wording) and two nits (JSON items are stripped like comma items; the resolved-record test now spies on the asset and provenance reads); open nits: a clock that goes backwards breaks idempotency (as in G-077), the inside-transaction clock test only counts calls. Tests: 4976 collected and passed in one full suite (4726 before, 250 new: 84 in `tests/test_rights_assessment.py`, 49 in `tests/test_rights_risk_engine.py`, and 117 in `tests/test_migrations.py` (now 25 migrations), `tests/test_config.py` and `tests/test_rights_gate.py`).

Earlier task summaries (G-077 back to A-001, and the Phase A start note) were moved verbatim to `CHANGELOG.md`, section "Task summaries (moved from PROJECT_STATE.md)".

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
| Tracked code | `src/ai_youtube_agent/` (`__init__.py`, `main.py`, `bootstrap.py`, `core/config.py`, `core/flags.py`, `core/log.py`, `core/errors.py`, `core/di.py`, `core/health.py`, `core/audit.py`, `core/content_item.py`, `core/artifact.py`, `content/channel.py`, `content/channel_settings.py`, `content/channel_api.py`, `content/strategy_settings.py`, `content/strategy_api.py`, `content/strategy.py`, `content/script.py`, `content/voice.py`, `content/rights.py`, `content/policy.py`, `content/qc.py`, `content/approval.py`, `content/analytics.py`, `content/revenue.py`, `content/cost.py`, `content/comment.py`, `content/experiment.py`, `pipeline/publish.py`, `core/gates.py`, `core/approval_gate.py`, `core/version_invalidation.py`, `core/production_start.py`, `core/production.py`, `core/daily_limit_gate.py`, `core/budget_gate.py`, `core/rights_gate.py`, `core/policy_gate.py`, `core/kill_switch_gate.py`, `core/idempotency_gate.py`, `core/http.py`, `core/db/` (`codec.py`, `migrate.py`, `migrations/0001_initial_schema.sql`, `migrations/0002_production_starts.sql`, `migrations/0003_partial_strategy.sql`, `database.py`, `repositories/`), `pipeline/job.py`, `pipeline/kill_switch.py`, `pipeline/idempotency.py`, and the packages `core/`, `content/`, `providers/`, `pipeline/`), `dashboard/README.md`, `.env.example`, `tests/` (`test_health.py`, `test_folder_structure.py`, `test_config.py`, `test_flags.py`, `test_log.py`, `test_errors.py`, `test_di.py`, `test_bootstrap.py`, `test_health_check.py`, `test_audit.py`, `test_channel.py`, `test_strategy.py`, `test_content_item.py`, `test_artifact.py`, `test_script.py`, `test_voice.py`, `test_rights.py`, `test_qc.py`, `test_approval.py`, `test_publish.py`, `test_analytics.py`, `test_revenue.py`, `test_cost.py`, `test_comment.py`, `test_job.py`, `test_experiment.py`, `test_db_codec.py`, `test_migrations.py`, `test_repositories.py`, `test_audit_sink.py`, `test_status_enum.py`, `test_transitions.py`, `test_gates.py`, `test_approval_gate.py`, `test_version_invalidation.py`, `test_daily_limit.py`, `test_budget_gate.py`, plus `conftest.py` and `factories.py`) |
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
6. **Green gate before finishing a task:** `uv run ruff check .` and `uv run ruff format --check .` always pass; `uv build` when packaging-relevant code changed or before a checkpoint; the full `uv run python -m pytest` suite runs at most once per task, run by the main session (a coder runs it only when the brief explicitly asks), and only when justified (core or shared code, DB or migration, API contract, architecture, test infrastructure, dependency, many modules, before a checkpoint, high-risk end of task), with the reason stated when it is skipped; while iterating run the failing test id, the new tests, the affected tests and the related regression. The end-of-task report has a Tests block (new tests, affected tests, regression, full suite, migration chain: each RUN or NOT RUN with the reason).
7. **No secrets in source control.** Credentials, API keys and OAuth secrets stay out of Git. `.env` is ignored.
8. **No auto-publish and no strategy drift.** A production publish always needs a valid approval. Market, language, niche, format, budget and channel are never changed on the AI's own initiative.
9. **Do not re-run Phase 0 or re-bootstrap.** The project exists. Do not reset or delete it.
10. **Keep state files current.** Update `PROJECT_STATE.md`, `TASK_STATUS.md` and `CHANGELOG.md` at the end of each task.
11. **Push only when the user asks.**
12. **Route every task first.** Before implementing a task, classify it with the Task/Model Router (`tools/task_router.py`, policy in `.claude/task-router.json`, rules in `docs/TASK_ROUTER.md`, summary in `CLAUDE.md`) and follow the routed profile (model/effort) of each role (planner, coder, reviewer); Opus only on evidence (Opus triggers); escalate only with a reason and evidence; record runs, gate and result in the router telemetry. User overrides win unless they break a rule above.

## Workflow infrastructure

Task/Model Router (2026-10-03, after F-067; not a Prompt Pack task, so it has no TASK_STATUS row and does not change the state machine). User-approved: routed subagents per level generated from the policy (`.claude/agents/task-{planner,coder,reviewer}-<level>.md`), a deterministic scoring script with floors and tests (`tests/test_task_router.py`, 45 tests), the main session keeps the user's Opus setting (no project model setting), commit without push. Default policy: TRIVIAL haiku/low, SIMPLE sonnet/low, NORMAL sonnet/medium + planner, COMPLEX opus/high + planner + reviewer, ARCHITECTURAL opus/high + planner + reviewer + architecture review; escalation at most 2 steps up the ladder, never to xhigh/max without a user override. Nothing in `src/`, the migrations or earlier tests changed.

TOOL-002 Cost-Aware Task Router (2026-10-03, after F-069; workflow infrastructure, not a Prompt Pack task: no TASK_STATUS row, no change to `src/`, the migrations or the state machine; NEXT_TASK stays F-070). User-approved design B: complexity (score, weights, thresholds, floors, levels) is unchanged; execution is a `model/effort` profile per role from policy schema_version 2 (allowed: haiku/low, sonnet/low, sonnet/medium, sonnet/high, opus/medium, opus/high; xhigh/max only by user override). TRIVIAL coder haiku/low; SIMPLE coder sonnet/low; NORMAL planner + coder sonnet/medium (reviewer sonnet/high only for a behaviour change with a high risk); COMPLEX planner + coder sonnet/high, reviewer opus/high; ARCHITECTURAL opus/high everywhere + architecture review. Evidence-based Opus triggers from an optional risk profile (derived from the criteria when missing) lift NORMAL to opus/medium and COMPLEX to opus/high; high regression/migration risk without architecture impact raises the coder's effort on Sonnet. Escalation ladder haiku/low → … → opus/high, at most 2 steps. Subagents are named by role and profile (`.claude/agents/task-<role>-<model>-<effort>.md`, 12 files; the level-named files were removed). Append-only telemetry `.claude/router-telemetry.jsonl` (`record`, `report`, `compare`), backfilled for F-065..F-069. v1 policies and decisions still load. The per-task history paragraphs moved verbatim to `CHANGELOG.md`. `tests/test_task_router.py` now has 203 tests. Details: `docs/TASK_ROUTER.md`.

TOOL-003 Smart Test Execution (2026-10-04, after F-073; workflow infrastructure and test infrastructure, not a Prompt Pack task: no TASK_STATUS row, no change to `src/`, the migrations or any existing assertion; NEXT_TASK stays F-074). `tests/conftest.py` migrates a read-only template database once per session; the `database` fixture and `database_copy(path)` copy it per test, so normal tests no longer run the migration chain. Migration, bootstrap and upgrade tests still migrate a fresh file. `tests/test_db_template.py` guards the template. The agent texts, `CLAUDE.md`, `docs/TASK_ROUTER.md` and invariant 6 encode the smart test rules (targeted first, the full suite at most once per task, owned by the main session, and only when justified, a Tests block in every report, no state-changing git for subagents). The suite has 4154 tests and runs in about 48 s. Details: `docs/TASK_ROUTER.md`, CHANGELOG.

## How to resume

```sh
git status --short --branch   # expect a clean tree on main
uv sync                       # recreate .venv if needed
uv run python -m pytest       # expect 4726 passed (4726 collected after G-077, 132 at the A-012 baseline)
uv run ruff check . && uv run ruff format --check .
```

Then start the task marked NOT_STARTED first in `TASK_STATUS.md`, which is G-078 right now.

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
| C-040 Kill Switch Gate | READY_FOR_PHASE_A | `KillSwitchGate` (active `EmergencyStop` blocks moves into generating and publishing, one `killswitch.active` reason). Tests now 1537. |
| C-041 Idempotency Gate | READY_FOR_PHASE_A | `IdempotencyGate` + deterministic `generation_key` / `publish_key`; an existing job with the key blocks unless it failed. Tests now 1567. |
| C-042 Gate Tests | READY_FOR_PHASE_A | `tests/test_gate_matrix.py`: 7 gates x 21 allowed moves in clean and worst worlds, all gates together, broken gate, 79 refused moves. No production code change. Phase C complete. Tests now 1990. |
| D-043 Channel Settings | READY_FOR_PHASE_A | `/channels` HTTP API (no UI) with entity-rule validation, one error envelope, local-user actor, optimistic `expected_updated_at`, audit after commit. Tests now 2023. |
| D-044 Market Settings | READY_FOR_PHASE_A | Partial strategy (migration 0003, gates block on missing cadence/budget), `PUT /channels/{id}/strategy/market` (country only, nothing else changes, `expected_version`, audit). Runner checks foreign keys per migration. Tests now 2050. |
| D-045 Language Settings | READY_FOR_PHASE_A | `PUT /channels/{id}/strategy/languages`: canonical BCP-47 case, ordered secondary (max 5), no repeats; generic `StrategySettings._save`. Tests now 2081. |
| D-046 Audience Settings | READY_FOR_PHASE_A | `Audience` gains optional `AgeRange` (13-100), interests (max 10 x 50 chars) and `AudienceLevel`; description max 500; `PUT /channels/{id}/strategy/audience`. Tests now 2115. |
| D-047 Niche Settings | READY_FOR_PHASE_A | `Niche` pillars are `Pillar(name, description?)`, 1-10, unique; `PUT /channels/{id}/strategy/niche` replaces the niche. Tests now 2143. |
| D-048 Brand Settings | READY_FOR_PHASE_A | `Brand` gains tone keywords, voice dos/donts, banned phrases and `BrandVisual`; `PUT /channels/{id}/strategy/brand`. Tests now 2182. |
| D-049 Format Settings | READY_FOR_PHASE_A | New required strategy setting `format` (Shorts and LongForm defaults) in `format_json` via migration 0004; `PUT /channels/{id}/strategy/format`. Tests now 2223. |
| D-050 Cadence Settings | READY_FOR_PHASE_A | Capped daily limits, channel time zone (daily limit day now local; `tzdata` added) and publish schedules in `cadence_schedule_json` via migration 0005; `PUT /channels/{id}/strategy/cadence`. Tests now 2277. |
| D-051 Budget Settings | READY_FOR_PHASE_A | Capped 2-decimal limits, alert thresholds in `budget_alert_thresholds_json` via migration 0006, budget day and month in the cadence time zone; `PUT /channels/{id}/strategy/budget`. Tests now 2326. |
| D-052 Monetization Settings | READY_FOR_PHASE_A | Closed `RevenueSource` list with optional monthly targets and notes, not-guaranteed label in the API; `PUT /channels/{id}/strategy/monetization`. Tests now 2369. |
| D-053 Strategy Validation | READY_FOR_PHASE_A | `validate_strategy` with blocking findings and warnings, `StrategyGate` (`GateName.STRATEGY`) on every move into generating, `GET /channels/{id}/strategy/validation`. Phase D complete. Tests now 2444. |
| E-054 Research Provider | READY_FOR_PHASE_A | Sync `ResearchProvider` Protocol with typed search/fetch values and retryable error codes; in-memory `MockResearchProvider` selected by `Settings.research_provider`, with a provider health check. Tests now 2496. |
| E-055 Source Model | READY_FOR_PHASE_A | `Source` with URL normalisation and evidence notes; `sources` table via migration 0007 and add-only `SourceRepository`. Tests now 2545. |
| E-056 Source Collector | READY_FOR_PHASE_A | Stored `ResearchRequest` with limits and status (migration 0008); `SourceCollector` with in-call retries, host and source limits. Tests now 2576. |
| E-057 Source Deduplication | READY_FOR_PHASE_A | Simhash content fingerprints and title similarity; `SourceDeduplicator` marks near-duplicates per request (migration 0009); query warnings. Tests now 2616. |
| E-058 Topic Extractor | READY_FOR_PHASE_A | Keyword-statistics `TopicExtractor` with per-source evidence; topics stored once per request (migration 0010). Tests now 2644. |
| E-059 Topic Scoring | READY_FOR_PHASE_A | Transparent relevance, novelty and support signals with a fixed weighted score and reasons; stored once per request (migration 0011). Tests now 2666. |
| E-060 Research Report | READY_FOR_PHASE_A | JSON `ResearchReport` with claims, evidence and rule-based uncertainty, stored once per request (migration 0012), Markdown on demand. Tests now 2689. |
| E-061 Research Cache | READY_FOR_PHASE_A | SQLite `ResearchCache` around the research provider (search 6 h, fetch 24 h, refresh, stale fallback marked by `CacheInfo`). Tests now 2709. |
| E-062 Research Failure Recovery | READY_FOR_PHASE_A | Research progress saved after each item with a 10-min lease (migration 0014); `resume` after an expired lease and `retry` of failures only, refused once results are stored. Tests now 2727. |
| E-063 Research Tests | READY_FOR_PHASE_A | End-to-end research scenarios (mocked sources, duplicates, empty results, provider failures) in `tests/test_research_flow.py`; test-only. Phase E complete; Phase F table added. Tests now 2741. |
| F-064 Script Model | READY_FOR_PHASE_A | B-017 `Script` extended with ordered sections, a duration target from the format (reported only), version history and section-linked claims (migration 0015). Tests now 2772. |
| F-065 Hook Generator | READY_FOR_PHASE_A | `TextGenerator` provider + mock (Q1 resolved); `HookGenerator` stores up to 3 checked hook candidates per run (migration 0016). Tests now 2803. |
| F-066 Shorts Script Generator | READY_FOR_PHASE_A | `ShortsScriptGenerator`: HOOK (chosen candidate) + 1-3 BODY + CTA from a checked JSON answer, stored as a Script version by the AI actor. Tests now 2824. |
| F-067 LongForm Script Generator | READY_FOR_PHASE_A | `LongFormScriptGenerator`: refused while LONGFORM_ENABLED is off; outline + one call per chapter, HOOK + INTRO + 3-12 CHAPTERs + OUTRO + CTA, stored as a Script version by the AI actor. Tests now 2857. |
| F-068 Claim Extractor | READY_FOR_PHASE_A | `ClaimExtractor`: deterministic rules (`rules-v1`), one `ClaimKind` per claim, CTA/questions/opinions skipped, max 200 claims, one stored run per script version (migration 0017). Tests now 2990. |
| F-069 Evidence Matcher | READY_FOR_PHASE_A | `EvidenceMatcher`: deterministic rules (`rules-v1`), own research report only, containment >= 0.5 with 2 shared words, numbers/dates agree/differ/none, max 3 links from distinct sources, one stored run per claim extraction (migration 0018); ranges, abbreviations, ambiguous numeric dates and entity capitals refined after review. Tests now 3161. |
| F-070 Fact Check Result | READY_FOR_PHASE_A | `FactChecker`: deterministic rules (`rules-v1`), first matching of 9 rows per claim (PASS, WARN or FAIL with a code; only differing numbers with no agreeing link are FAIL), a derived worst-of run status, a record only (no gate, no override), one stored run per evidence match (migration 0019). Tests now 3431. |
| F-071 Originality Check | READY_FOR_PHASE_A | `OriginalityChecker`: deterministic rules (`originality-rules-v1`), reuse by word 5-gram shingle containment against the latest version of the other items of the same channel created before the script (newest 50; >= 60% FAIL `near_duplicate`, >= 25% with 8 shared WARN `high_overlap`, >= 3 copied sentences WARN), WARN-only structure findings (repeated hook, sentences, openers), a derived worst-of run status, a record only (no gate, no override), ids and counts only, one stored run per script (migration 0020). Tests now 3602. |
| F-072 Script Validator | READY_FOR_PHASE_A | `ScriptValidator`: deterministic rules (`script-rules-v1`) in the order sections (Shorts HOOK + 1-3 BODY + CTA, LongForm HOOK + INTRO + 3-12 chapters + OUTRO + CTA, strict set and order, FAIL), length (estimated seconds against the script's duration target or the strategy format range, FAIL), language (EN/VI heuristic, 10 hits needed, >= 80% FAIL and 60-80% WARN of a known language that is not allowed, else not checked and flagged), banned phrases (FAIL, never the phrase) and hook length (WARN); own `ValidationStatus`, a derived worst-of run status, a record only (no gate, no override), one stored run per script, never recomputed (migration 0021). Tests now 3903. |
| F-073 Script Versioning | READY_FOR_PHASE_A | `ScriptVersioner`: on demand, the diff metadata (`script-diff-v1`) of a script version (2 or later) against its parent only: counts added, removed, changed and unchanged, words and estimated seconds before and after, the SHA-256 of the whole script and one entry per aligned section (LCS over the exact section keys, then same-kind pairs in each gap; flags, words delta, optional section hash); no script text stored; one stored revision per version, never recomputed; any actor; a read-only `history`; no restore yet; not related to approvals (they bind artifacts only: a new script version invalidates nothing until rendered) or to the F-068..F-072 runs (migration 0022). Tests now 4141. |
| F-074 Script Tests | READY_FOR_PHASE_A | End-to-end script chain tests (`tests/test_script_flow.py`, 39 tests) and four regression tests for the `store_script` race: `store_script` re-reads the latest script inside the write transaction and raises `ScriptConflictError` (409, `domain.script_conflict`) instead of an uncaught `IntegrityError`. Phase F is complete. Next: G-075 Asset Model. |
| G-075 Asset Model | READY_FOR_PHASE_A | Entity only (`content/asset.py`, no migration, repository or API): frozen `Asset` with closed `AssetCategory` (generated, licensed, public_domain, user_owned, unknown; declared, never derived) and `AssetKind`; rules licensed needs license_ref, user_owned needs owner, generated needs a provider source, only generated may have artifact_id; no NFC; 34 tests in `tests/test_asset.py`. |
| G-076 Asset Registry | READY_FOR_PHASE_A | `AssetRegistry` service over `Asset` (`content/asset_registry.py`, new `content/asset_usage.py`, repositories in `core/db/repositories/asset.py`, migration 0023 `assets` and `asset_usages`, bootstrap; no HTTP route): `register` (duplicates in a channel by source key and title key or by artifact: equal returns the stored asset, different is 409), `attach` (asset to content item of the same channel, many to many, idempotent, creates an unresolved unknown `RightsRecord` with `asset_ref` = `Asset.id` unless one exists), reads; audit `asset.registered` and `asset.attached` with ids and counts only; 129 new tests. |
| G-077 Provenance Record | READY_FOR_PHASE_A | `ProvenanceRecorder` service over `Asset` (`content/provenance.py`, `content/provenance_recorder.py`, `core/db/repositories/provenance.py`, migration 0024 `asset_provenance`, bootstrap; no HTTP route, no risk computation): an append-only history of source and licence records per asset (the newest is current), any actor stored, category rules on the record (licensed needs a licence name or reference, user_owned needs an owner), URLs without credentials, secret-looking query or fragment parameters, a bad port or whitespace (error messages never hold a value), a SHA-256 checksum, an identical repeat of the current record writes and audits nothing (A, B, A writes three rows), neither `Asset` nor `RightsRecord` is changed, audit `asset.provenance_recorded` with ids and flags only; 366 new tests (228 + 91 + 47). |

The change from EMPTY to EXISTING is a valid state transition caused by Project Initialization. It is not a pre-existing failure. The files in the baseline were created by that task, so they are not PRE_EXISTING relative to the original empty state.

## Scope not started

- Phases C–T. Phases A and B are complete
- AI YouTube business logic (Research, Script, Voice, YouTube API, etc.)
- Docker and CI
