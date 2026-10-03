# Project State

Last updated: 2026-10-04 (F-073 Script Versioning, after F-072 Script Validator)

This file is the handoff document. A new session should read it, together with `TASK_STATUS.md`, before doing anything else. There is no need to audit the repository again from the start.

## Current state

```
PROJECT_STATE      = READY_FOR_PHASE_A
BASELINE_COMMIT    = 3b41416
CURRENT_CHECKPOINT = 1c3e126
BASELINE_STATUS    = CLEAN
TEST_STATUS        = PASS
LINT_STATUS        = PASS
BUILD_STATUS       = PASS
KNOWN_FAILURES     = NONE
UNKNOWN_BLOCKERS   = NONE
NEXT_TASK          = F-074 Script Tests
```

## Next task

**F-074 Script Tests** (Prompt Pack v8, Phase F Script & Fact Check, prompt 74). It depends on F-073, which has PASSED.

> Test generators, claims, evidence gaps and revision behavior.

Note: the pack does not say how wide these tests are beyond that line, so what is in scope (test-only end-to-end scenarios like `tests/test_research_flow.py` of E-063, or also filling gaps in the existing unit tests of F-064..F-073), which chain they run through (the generators #065-#067, claim extraction #068, evidence matching #069 and fact check #070 with the originality #071, validation #072 and revision #073 runs, from one mocked research report), which "evidence gaps" count (claims with no evidence, claims with differing numbers, a script version whose research report is missing, a stale or conflicting source) and what the expected result is for each, which "revision behavior" is tested (the diff and its alignment, idempotency, one revision per version, the history, no relation to approvals), whether any source file may change if a test finds a defect (default: report it, do not fix it inside this task), and whether the mock `TextGenerator` answers are fixed per scenario or generated. Confirm these with the user before implementing, offering 3-4 alternatives per question.

F-073 Script Versioning has PASSED. `ScriptVersioner` (`content/script_versioner.py`) stores a revision of one stored script version: the diff metadata of the version against its parent only (method `script-diff-v1`, deterministic, no AI model, record only; `content/script_revision.py`). Version 1 has no parent and no revision. No script text is stored anywhere (rows, entries, audit, history). A revision holds: counts added, removed, changed and unchanged (their sum is `entries_count`, at most 200 = `MAX_ENTRIES`), `words_before`/`words_after`, `seconds_before`/`seconds_after` (the `estimated_seconds` of the parent and of the version), `content_sha256` and one entry per aligned section: `change` (added, removed, changed, unchanged), `old_index` and `new_index` (0..99, none for an added or a removed section), `kind`, the flags `text_changed`/`title_changed`/`seconds_changed` (0 for added, removed and unchanged; at least one for changed), `words_delta` (new words minus old words: added +words, removed -words, unchanged 0, so the deltas add up to `words_after - words_before`) and `sha256` (the section key of the new section, of the old one when removed). Hashes (defined once in `script_revision.py`): a section key is the SHA-256 of the canonical JSON of `{kind, title, text, seconds}` (`seconds` = `estimated_seconds`; keys sorted, no spaces, non-ASCII kept, UTF-8; text compared exactly, not normalised); `content_sha256` is the SHA-256 of the canonical JSON array of the sections of the new version, in order. Unchanged = kind, title, text and estimated seconds all equal. The hashes cover the estimated seconds, so versions that differ only in declared seconds against none with the same estimate get the same `content_sha256` (it is not a fingerprint of the stored row). Alignment (pure `diff_scripts(old, new)`, nothing stored): longest common subsequence over the exact section keys (a tie keeps the earlier old index); inside each gap between two matches the old and new sections of the same kind are paired in order (a second LCS over the kinds, same tie rule) as changed, the rest are removed and added; a moved section is one removed and one added entry; entries are ordered so that old and new indexes both only grow; `ScriptRevision` (frozen: validation, `create`, `counts`, `summary`) checks that the diff belongs to the two scripts (`create`) and the v1 rules (indexes once in order from 0, word deltas add up, counts match entries) only for method `script-diff-v1`. A revision with no change at all is allowed. `ScriptVersioner(database, audit, *, clock=None).record(script_id, *, actor)`: unknown script `ScriptNotFoundError` (404); version 1, a missing parent or a parent of another content item `ScriptInputError` (422); any actor may record (derived data); a stored revision is returned before the parent is read, without computing, writing or auditing, and never recomputed; the diff runs outside the write transaction, the revision and its entries are stored in one; an `IntegrityError` (unique `script_id`) re-reads and returns the stored revision, else re-raises; `script.revision_recorded` (entity `script`, ids and counts only) is audited after commit. `history(content_item_id)` (read only, no HTTP route) gives per version in order the script id, version, parent id, `created_by`, reason, strategy version, words, seconds and the revision summary or None; an unknown content item is `ContentItemNotFoundError` (404). Migration `0022_script_revisions.sql`: STRICT `script_revisions` (script_id UNIQUE FK, parent FK, content item, method open text with a length check, version >= 2, counts with a CHECK that their sum = entries_count (0..200), words/seconds >= 0, content_sha256 64 lowercase hex, requested_by, created_at GLOB, index by item) and `script_revision_sections` (revision FK, `change` open text with a length check, `kind` CHECK of `SectionKind`, indexes 0..99 nullable and consistent with the change, flags 0/1, words_delta sign by change, optional sha256 64 lowercase hex, UNIQUE per revision on old and on new index); no ALTER of existing tables; a test ties the SQL bounds to the Python constants. `ScriptRevisionRepository` (add, get, get_by_script, list_by_content_item by version; entries ordered by rowid). Bootstrap registers the versioner. Correction (docs only): a new script version does NOT already invalidate approvals. In code, approvals bind artifacts only (`ArtifactKind` video, audio, subtitles, thumbnail, metadata; C-035 `VersionInvalidation` reacts to a new artifact version) and `store_script` never touches approvals, so a new script version invalidates nothing until it is rendered into new artifacts. A revision is not related to approvals or to the F-068..F-072 runs of a version. No restore of an earlier version exists (open question for a later task). F-064..F-072 behaviour and rows, `content/script.py` entities (docstring line only), `store_script` and the generators, `core/version_invalidation.py`, `content/approval.py`, `core/gates.py`, providers, strategy and migrations 0001-0021 are unchanged. Tests: 4141 (3903 before, 236 new: `tests/test_script_revision.py` 131, `tests/test_script_versioner.py` 104, 1 in the B-029 migration tests).

F-072 Script Validator has PASSED. `ScriptValidator` (`content/script_validator.py`) validates one stored script version against the length, section, language and content-type rules and the current strategy of its channel, on demand, by deterministic rules (`content/script_validation.py`, method `script-rules-v1`, no AI model, record only). `validate_script(script, content_type, strategy)` is pure and gives the findings in this order. Sections (FAIL, strict set and order, the shapes the #066/#067 generators write): Shorts = one HOOK, 1-3 BODY, one CTA; LongForm = one HOOK, one INTRO, 3-12 chapter sections, one OUTRO, one CTA, where a chapter section is a CHAPTER (always titled, since `ScriptSection` refuses an untitled chapter, so there is no title code) when `format.longform.chapters` is on and a BODY when it is off; codes `missing_section` (one per missing kind; for Shorts also no BODY), `unexpected_section` (a kind the type lacks, a second HOOK/INTRO/OUTRO/CTA, a 4th Shorts BODY; `section_index`), `chapter_count` (LongForm only; actual, minimum 3, maximum 12), `chapter_kind` (LongForm only, FAIL: every chapter-position section has the other kind, CHAPTER while chapters are off or BODY while they are on, and there are 3-12 of them; one finding with `section_index` of the first and `actual` = how many, instead of an `unexpected_section` each and a `chapter_count`; mixed kinds or a wrong count keep the old codes), `wrong_order` (the first section out of place in the surviving sequence, e.g. [HOOK, CTA, BODY] gives index 2; hook-first and CTA-last come from it); the kind of a missing or unexpected section is not stored (the script is immutable). Length (FAIL `too_short` / `too_long`): `Script.estimated_seconds` (150 words per minute) against the script's own `duration_target`, else the strategy format range of the content type, limits inclusive; actual, minimum and maximum seconds on the finding. Language (`language_mismatch`, FAIL or WARN): a deterministic EN/VI heuristic with its own word lists in `script_validation.py` (NFC then casefold; English and Vietnamese function words, words in both lists dropped (do, no, me, an, ai); Vietnamese signal also from letters distinctive of Vietnamese (`VI_LETTERS`), not from letters shared with French or Spanish; English terms inside Vietnamese text carry no signal, so code-switching passes); all sections read, at most 20,000 words (`MAX_WORDS`) and 800,000 characters; allowed languages = base subtags of the strategy primary and secondary tags; fewer than 10 hits (`MIN_HITS`) or no allowed `en`/`vi` tag = the language is not checked (no finding; the run stores `language_checked` false and `language_hits`, so there is no false pass); else a known language that is not allowed holding >= 80% of the hits is a FAIL, 60% to 80% a WARN (integer percent compare; `score` rounded down to 3 decimals, `matched` = its hits, `actual` = all hits). Banned phrases (FAIL `banned_phrase`): `text_prompts.banned_phrases(strategy)` with the public `find_banned_phrases` over the text and over the title of each section, scanned apart (a phrase never runs from the title into the text); one finding per section with `section_index` and `matched` = how many distinct phrases, never the phrase. Hook length (WARN `hook_too_long`): `check_hook` and `HOOK_LIMITS` on the first HOOK section, one finding per exceeded limit (words or sentences; `actual` and `maximum`). The `LONGFORM_ENABLED` flag is not read: LongForm scripts are validated with it off. `Finding` (frozen: closed `ValidationCode`, status, optional `section_index`, `actual`, `minimum`, `maximum`, `score` 0..1, `matched`; never script text); own `ValidationStatus` (pass, warn, fail); `ScriptValidation` run (findings, counts, `words`, `seconds`, `language_checked`, `language_hits`, `strategy_version` = the current strategy's version the rules read, `script_strategy_version` = the script's own; derived `status` = worst finding, no finding = PASS; the v1 value checks apply only to runs of method `script-rules-v1`). It does not read the F-070/F-071 results. Record only: no gate (no change to `core/gates.py`), immutable, no override (a later task may add a separate override table). `ScriptValidator(database, audit, *, clock=None).validate(script_id, *, actor)`: an unknown script is `ScriptNotFoundError` (404); a channel without a strategy `StrategyNotFoundError` (404); a strategy without a language or a format `ScriptInputError` (422); a stored run is returned before the strategy is loaded, without writing or auditing, and is never recomputed when the strategy changes later; the rules run outside the write transaction, the run and its findings are stored in one; an `IntegrityError` (unique `script_id`) re-reads and returns the stored run, else re-raises; `script.validated` (entity `script`, ids and counts only) is audited after commit. Migration `0021_script_validations.sql`: STRICT `script_validations` (script_id UNIQUE FK, content item, method open text with a length check, words, seconds, language_checked 0/1, language_hits 0-20000, strategy_version, script_strategy_version nullable, findings_count 0-210 (`MAX_FINDINGS`) with warn + fail = findings_count, requested_by, created_at GLOB, index by item) and `script_validation_findings` (FK to the run; status CHECK of the enum list but never `pass`; `code` open text with a length check (closed in Python only); section_index, actual, minimum, maximum, score 0-1, matched nullable with CHECKs; minimum <= maximum; index by run); no ALTER of existing tables; a test ties the SQL bounds to the Python constants. `ScriptValidationRepository` (add, get, get_by_script; findings ordered by rowid). Bootstrap registers the validator. F-064..F-071 behaviour and rows, the generators, `script_generation.py`, `hook.py`, `similarity.py`, migrations 0001-0020, providers, `core/gates.py` and the strategy are unchanged. Tests: 3903 (3602 before, 301 new: `tests/test_script_validation.py` 201, `tests/test_script_validator.py` 99, 1 in the B-029 migration tests).

Earlier task summaries (F-071 back to A-001, and the Phase A start note) were moved verbatim to `CHANGELOG.md`, section "Task summaries (moved from PROJECT_STATE.md)".

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
6. **Green gate before finishing a task:** `uv run pytest`, `uv run ruff check .` and `uv run ruff format --check .` must pass.
7. **No secrets in source control.** Credentials, API keys and OAuth secrets stay out of Git. `.env` is ignored.
8. **No auto-publish and no strategy drift.** A production publish always needs a valid approval. Market, language, niche, format, budget and channel are never changed on the AI's own initiative.
9. **Do not re-run Phase 0 or re-bootstrap.** The project exists. Do not reset or delete it.
10. **Keep state files current.** Update `PROJECT_STATE.md`, `TASK_STATUS.md` and `CHANGELOG.md` at the end of each task.
11. **Push only when the user asks.**
12. **Route every task first.** Before implementing a task, classify it with the Task/Model Router (`tools/task_router.py`, policy in `.claude/task-router.json`, rules in `docs/TASK_ROUTER.md`, summary in `CLAUDE.md`) and follow the routed profile (model/effort) of each role (planner, coder, reviewer); Opus only on evidence (Opus triggers); escalate only with a reason and evidence; record runs, gate and result in the router telemetry. User overrides win unless they break a rule above.

## Workflow infrastructure

Task/Model Router (2026-10-03, after F-067; not a Prompt Pack task, so it has no TASK_STATUS row and does not change the state machine). User-approved: routed subagents per level generated from the policy (`.claude/agents/task-{planner,coder,reviewer}-<level>.md`), a deterministic scoring script with floors and tests (`tests/test_task_router.py`, 45 tests), the main session keeps the user's Opus setting (no project model setting), commit without push. Default policy: TRIVIAL haiku/low, SIMPLE sonnet/low, NORMAL sonnet/medium + planner, COMPLEX opus/high + planner + reviewer, ARCHITECTURAL opus/high + planner + reviewer + architecture review; escalation at most 2 steps up the ladder, never to xhigh/max without a user override. Nothing in `src/`, the migrations or earlier tests changed.

TOOL-002 Cost-Aware Task Router (2026-10-03, after F-069; workflow infrastructure, not a Prompt Pack task: no TASK_STATUS row, no change to `src/`, the migrations or the state machine; NEXT_TASK stays F-070). User-approved design B: complexity (score, weights, thresholds, floors, levels) is unchanged; execution is a `model/effort` profile per role from policy schema_version 2 (allowed: haiku/low, sonnet/low, sonnet/medium, sonnet/high, opus/medium, opus/high; xhigh/max only by user override). TRIVIAL coder haiku/low; SIMPLE coder sonnet/low; NORMAL planner + coder sonnet/medium (reviewer sonnet/high only for a behaviour change with a high risk); COMPLEX planner + coder sonnet/high, reviewer opus/high; ARCHITECTURAL opus/high everywhere + architecture review. Evidence-based Opus triggers from an optional risk profile (derived from the criteria when missing) lift NORMAL to opus/medium and COMPLEX to opus/high; high regression/migration risk without architecture impact raises the coder's effort on Sonnet. Escalation ladder haiku/low → … → opus/high, at most 2 steps. Subagents are named by role and profile (`.claude/agents/task-<role>-<model>-<effort>.md`, 12 files; the level-named files were removed). Append-only telemetry `.claude/router-telemetry.jsonl` (`record`, `report`, `compare`), backfilled for F-065..F-069. v1 policies and decisions still load. The per-task history paragraphs moved verbatim to `CHANGELOG.md`. `tests/test_task_router.py` now has 203 tests. Details: `docs/TASK_ROUTER.md`.

## How to resume

```sh
git status --short --branch   # expect a clean tree on main
uv sync                       # recreate .venv if needed
uv run python -m pytest       # expect 4141 passed (132 at the A-012 baseline)
uv run ruff check . && uv run ruff format --check .
```

Then start the task marked NOT_STARTED first in `TASK_STATUS.md`, which is F-074 right now.

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

The change from EMPTY to EXISTING is a valid state transition caused by Project Initialization. It is not a pre-existing failure. The files in the baseline were created by that task, so they are not PRE_EXISTING relative to the original empty state.

## Scope not started

- Phases C–T. Phases A and B are complete
- AI YouTube business logic (Research, Script, Voice, YouTube API, etc.)
- Docker and CI
