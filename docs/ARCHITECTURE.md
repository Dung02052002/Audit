# Architecture Map

| Item | Value |
|---|---|
| Produced by | A-003 Architecture Map |
| Date | 2026-09-25 |
| Source | `docs/REQUIREMENTS.md` (frozen in A-002) |
| Status | Documentation only. Nothing in this document is implemented yet. |

This map describes the bounded contexts, interfaces and data flow of the AI YouTube Autonomous Agent. Every element names the prompts it comes from, written as `#NNN` for a prompt in the catalog in `docs/REQUIREMENTS.md`. Where the requirements leave a decision open, it is listed in section 8 instead of being decided here.

## 1. Current implementation

Only the Phase 0 bootstrap exists: a FastAPI app (`ai_youtube_agent.main:app`) with `GET /health`. No context below has code yet. The backend stack is fixed: Python 3.11, FastAPI, uv, pytest and ruff.

## 2. Architectural principles

These come directly from the frozen requirements.

| Principle | Source |
|---|---|
| Exactly two content types, `SHORTS` and `LONGFORM`, sharing one lifecycle and one set of gates | REQUIREMENTS §3–4, #015 |
| `TEST`, `QC`, `PREVIEW` and `APPROVAL` are shared control stages, not content types | REQUIREMENTS §4 |
| External services sit behind provider interfaces. Most of them require a mock (see 4.1) | #009, #054, #086, #147, #158, #170, #182 |
| Implementations are registered through dependency injection, never hard-coded | #009 |
| Publishing is blocked unless every gate passes. There is no auto-publish | #033, #034, #084, R-08 |
| An approval is bound to one exact artifact version and becomes invalid when that version changes | #021, #035, #137 |
| The AI never changes strategy (market, language, niche, format, budget, channel) | #014, #028, #044, #059, R-09 |
| Important actions produce immutable audit events | #011, #143, #156, #189, #214 |
| Jobs are resumable and idempotent | #027, #041, #088, #151, #211 |
| Secrets stay outside source control | #005, #216, R-07 |

## 3. Bounded contexts

### 3.1 Domain contexts

| # | Context | Responsibility | Owns (entities) | Prompts |
|---|---|---|---|---|
| C1 | Channel & Strategy | User-controlled channel and strategy configuration, and its validation | Channel, StrategyProfile | #013, #014, #043–#053 |
| C2 | Content Lifecycle | The common content item, versioned artifacts, and lifecycle status and transitions | ContentItem, Artifact | #015, #016, #031, #032 |
| C3 | Control Gates | The shared gate contract and every blocking gate | (gate results) | #033–#042 |
| C4 | Research | Collecting, deduplicating and scoring sources and topics, and producing research reports | Source (`content/source.py`, table `sources`, E-055: one per normalised URL), ResearchRequest (`content/research_request.py`, E-056, collected by `SourceCollector`; near-duplicate sources marked by `SourceDeduplicator`, E-057), Topic (`content/topic.py`, extracted by keyword statistics in `TopicExtractor`, E-058; scored by `TopicScorer` with transparent relevance, novelty and support signals, E-059), ResearchReport (`content/research_report.py`, JSON per request with claims, evidence and uncertainty, built by `ResearchReportGenerator`, E-060) | #054–#063 |
| C5 | Script & Fact Check | Script generation, claim extraction, evidence matching, fact checking, originality and versioning | Script, Claim, FactCheckResult | #017, #064–#074 |
| C6 | Rights & Policy | Asset registry, provenance, rights risk, policy rules, AI disclosure and blocking reports | Asset, Provenance, RightsReport, PolicyReport | #019, #075–#085 |
| C7 | Voice & Audio | Voice profiles, TTS jobs, audio artifacts, validation and cost | VoiceProfile, AudioArtifact | #018, #086–#093 |
| C8 | Shorts Production | Shorts project, timeline, captions, subtitles, audio mix, render, thumbnail, metadata and bundle | ShortsProject | #094–#104 |
| C9 | LongForm Production | LongForm project, timeline, chapters, B-roll, subtitles, audio mix, render, thumbnail, metadata and bundle | LongFormProject | #105–#115 |
| C10 | Quality (Test & QC) | Technical tests of rendered artifacts and the aggregated QC report | QCResult | #020, #116–#130 |
| C11 | Review (Preview & Approval) | Preview of the exact artifact version and the approval workflow | ApprovalRequest | #021, #131–#144 |
| C12 | Publishing | YouTube OAuth, upload, publish paths, idempotency and publish results | PublishJob, PublishResult | #022, #145–#157 |
| C13 | Analytics | Channel and video metrics with freshness, and the experiment registry | MetricSnapshot, Experiment | #023, #028, #158–#169 |
| C14 | Economics | Estimated and final revenue, the cost ledger, contribution margin and budget guard | RevenueRecord, CostRecord | #024, #025, #170–#181 |
| C15 | Community | Comment sync, classification, reply drafts and reply approval | Comment, ReplyDraft | #026, #182–#190 |
| C16 | Orchestration | Job queue, scheduler, pipeline runner, sessions, retries, daily stop, crash resume and kill switch | AIJob, Session | #027, #204–#215 |

### 3.2 Cross-cutting areas

| # | Area | Responsibility | Prompts |
|---|---|---|---|
| X1 | Foundation & Governance | Audit, requirements, architecture, folders, configuration, feature flags, logging, error model, DI, health check, audit events and build baseline | #001–#012 |
| X2 | Persistence | Migrations, schema creation, rollback strategy and repository tests for all entities | #029, #030 |
| X3 | Command Center (presentation) | The dashboard. It reads from the domain contexts and triggers user actions such as approval and settings. It owns no domain rules | #191–#203 |
| X4 | Security & Recovery | Secret boundary, token storage, least privilege, log redaction, backup, restore and failure injection | #216–#223 |
| X5 | Integration & Readiness | End-to-end tests, the pilot, LongForm unlock and the production audit | #224–#238 |

### 3.3 Context relationships

```
                      C1 Channel & Strategy  (read-only for every other context)
                                 │
   C4 Research → C5 Script → C6 Rights & Policy → C7 Voice → C8 Shorts / C9 LongForm
                                                                   │
                                                             C2 Artifact (versioned)
                                                                   │
                                        C10 Quality → C11 Review → C12 Publishing
                                                                   │
                                        C13 Analytics → C14 Economics      C15 Community

   C3 Control Gates      guard the transitions of C2 and the publish step of C12
   C16 Orchestration     runs the pipeline one gate at a time (#206)
   X3 Command Center     reads every context and sends user actions only
```

- **Upstream / downstream:** each production step consumes the previous step's output. Strategy (C1) is upstream of everything and is never written by the AI.
- **Shared kernel:** C2 (ContentItem, Artifact, status) is shared by every context in the pipeline.
- **Conformist to external systems:** C12, C13, C14 and C15 adapt YouTube data through provider interfaces (section 4).

## 4. Interfaces

### 4.1 Provider interfaces (external systems)

The requirements define each of these as an abstraction with a mock.

| Interface | Defined by | Purpose | Mock required | Used by |
|---|---|---|---|---|
| Research Provider | #054 | Search and fetch sources. Implemented in `providers/research.py` (sync Protocol, typed values, retryable error codes) with `MockResearchProvider` in `providers/mock_research.py`, chosen by `Settings.research_provider`; since #061 always used through `ResearchCache` (`providers/research_cache.py`, SQLite, search 6 h, fetch 24 h, stale fallback on retryable errors) | Yes (#054) | C4 |
| Text Generation Provider | #065 (user decision 2026-10-03, answers Q1) | Generate text candidates (hooks, later scripts). Implemented in `providers/text_generation.py` (sync `TextGenerator` Protocol, `TextRequest`/`GeneratedText`, retryable `TextErrorCode`s) with `MockTextGenerator` in `providers/mock_text_generation.py`, chosen by `Settings.text_provider`, `text_provider` health check | Yes (#065) | C5, C14 (#175 cost) |
| Voice (TTS) Provider | #086 | Generate speech audio | Yes (#086) | C7 |
| Render provider abstraction | #100, #111 | Render vertical and long-form MP4 | Not stated | C8, C9 |
| Policy Rule Interface | #079 | Versioned policy rules | Yes, mock rules (#079) | C6 |
| OAuth / token interface | #145 | Secure YouTube authorization | Not stated | C12, X4 (#217) |
| Channel Provider | #146 | Read channel data | Not stated | C12, C13 |
| Upload Provider | #147 | Upload videos | Yes (#147) | C12 |
| Analytics Provider | #158 | Read metrics | Yes, mock data source (#158) | C13 |
| Revenue Provider | #170 | Read revenue | Yes (#170) | C14 |
| Comment Provider | #182 | Read comments and post replies | Yes (#182) | C15 |

### 4.2 Internal contracts

| Contract | Defined by | Purpose | Implemented by |
|---|---|---|---|
| Configuration contract | #005 | Typed loading, validation and environment separation. Implemented in `core/config.py` | X1 |
| Feature flags | #006 | `SHORTS_ENABLED`, `LONGFORM_ENABLED`, `PUBLISH_ENABLED`, `TEST_REQUIRED`, `APPROVAL_REQUIRED`, `AUTO_REPLY_ENABLED=false`. Implemented in `core/flags.py` | X1 |
| Logging contract | #007 | Structured logs with correlation, session and job IDs. Implemented in `core/log.py` | X1 |
| Error model | #008 | Typed application, domain and provider errors with safe user-facing messages. Implemented in `core/errors.py` | X1 |
| DI registry | #009 | Registers core interfaces and providers. Implemented in `core/di.py`. The composition root is `bootstrap.py`, and `main.create_app()` attaches the container to `app.state` | X1 |
| Health / status model | #010 | Application and provider health checks | X1 |
| Audit event | #011 | Immutable record of actor, timestamp, entity and result | X1, used by C11, C12, C15, C16 |
| Pipeline Gate Contract | #033 | One shared gate interface for Test, QC, Rights, Policy and Approval. Implemented in `core/gates.py` | C3 |
| Job queue | #204 | Durable queue abstraction | C16 |
| Repositories | #029, #030 | Persistence for every entity | X2 |

### 4.3 External-facing interfaces

| Interface | Source | Notes |
|---|---|---|
| HTTP API (FastAPI) | Current stack; #043 "configuration UI/API" | Serves the Command Center and user actions. Today: `/health` (#010) and `/channels` (#043). See the API notes below |
| Approval email | #138, #228 | Sends a safe preview reference and the exact artifact ID |
| Command Center UI | #191–#203 | Dashboard client. See open question Q4 |

API notes (D-043, approved by the user on 2026-10-01):

- Routers live in the context that owns them (`content/channel_api.py` for C1) and are included by `main.create_app`. Shared HTTP helpers are in `core/http.py`.
- Every request acts as `current_actor()`, which is `Actor(user, "local-user")` until there is a login; a real login only replaces that dependency.
- Every error uses one envelope: `{"error": {"code", "category", "message", "retryable", "fields"?}}`, built from `PublicError` (#008). `AppError`s use their own HTTP status; a request the schema refuses is 422 `validation.invalid_request` with `fields`; unknown routes and methods use `request.*` codes; anything unexpected is a 500 `application.internal` whose detail only goes to the log.
- `/channels`: `GET` lists, `POST` creates (title, YouTube channel id, optional handle), `GET /{id}` reads and `PATCH /{id}` changes title, handle and a user status (active, paused, archived) with `expected_updated_at` for optimistic concurrency. Changes are audited after commit (`channel.created`, `channel.updated`, `channel.status_changed`).
- Strategy (D-044): `GET /channels/{id}/strategy` returns the profile with `missing_settings`; one `PUT /channels/{id}/strategy/<setting>` per setting (#044 `market`, #045-#052 the rest). A strategy is configured one setting at a time: the first save creates the profile, every setting may be unset until #053 validates the profile before a run, and a save changes only its own setting. Later saves carry `expected_version` (409 when stale). Changes are audited after commit (`strategy.created`, `strategy.<setting>_changed`). Gates that need a setting block when it is missing (`daily_limit.no_cadence`, `budget.no_budget`).
- Languages (D-045): `PUT /channels/{id}/strategy/languages` takes a primary and up to 5 ordered secondary BCP-47 tags. Tags are stored in canonical case (`en-us` becomes `en-US`) and none may repeat, the primary included.
- Audience (D-046): `PUT /channels/{id}/strategy/audience` replaces the whole audience: a required description (at most 500 characters) and optional age range (13 to 100), up to 10 interests and a level. There are no fields for sensitive traits, and no audience under 13 can be targeted.
- Niche (D-047): `PUT /channels/{id}/strategy/niche` replaces the whole niche: a name (at most 100 characters) and 1 to 10 ordered content pillars, each a unique name (at most 60) with an optional description (at most 300).
- Brand (D-048): `PUT /channels/{id}/strategy/brand` replaces the whole brand: name, tone, tone keywords, voice dos and donts, banned phrases (for the script stage, #065-#074) and visual rules (colours, a font name and notes; no files). The spoken TTS voice is `VoiceProfile` (#087), not the brand.
- Format (D-049): `PUT /channels/{id}/strategy/format` replaces the Shorts and LongForm production defaults together: a target duration range (Shorts 1-180 s, LongForm 181-14400 s), resolution (720p, 1080p, 2160p) and captions, plus chapters for LongForm. Aspect ratios are fixed (Shorts 9:16, LongForm 16:9). The format is a required strategy setting stored in `format_json` (migration 0004). LongForm defaults may be saved while `LONGFORM_ENABLED` is off; the flag controls production. The project models (#094, #105) read these defaults.
- Cadence (D-050): `PUT /channels/{id}/strategy/cadence` replaces the daily limits (Shorts 0-20, LongForm 0-5), the channel time zone (IANA name, default UTC, data from `tzdata`) and a publish schedule per type (weekdays, up to 5 `HH:MM` times in that zone, a minimum gap in minutes). `DailyLimitGate` counts the day in the cadence time zone; the schedules are preferences for the publishers (#149, #150) and the job scheduler (#205). The limits keep their columns; the rest is `cadence_schedule_json` (migration 0005).
- Budget (D-051): `PUT /channels/{id}/strategy/budget` replaces the currency, the daily and monthly limits (0 to 1,000,000, at most 2 decimal places) and 1 to 5 alert thresholds in percent of each limit (default 50, 80, 100). Alerts are only configured here: the budget guard (#180) raises them and the alert cards (#202) show them. `BudgetGate` counts the day and month in the cadence time zone. The thresholds are `budget_alert_thresholds_json` (migration 0006).
- Monetization (D-052): `PUT /channels/{id}/strategy/monetization` replaces 0 to 8 revenue goals from the closed `RevenueSource` list, each with an optional monthly target and note, and a currency that is required once a target is set. Targets are tracking goals, not guaranteed outcomes: the strategy API labels them with the read-only `goals_are_not_guaranteed` and `notice`, and actual revenue stays in `RevenueRecord` (C14).
- Strategy validation (D-053): `validate_strategy` (`content/strategy_validation.py`) checks a strategy before a run, which is any move into generating. Missing configuration and conflicts that make a run impossible block; other conflicts are warnings. `StrategyGate` (`GateName.STRATEGY`, `core/strategy_gate.py`) blocks the run on the blocking findings, and `GET /channels/{id}/strategy/validation` shows every finding beforehand.
- Migrations (D-044): the runner applies each migration with foreign keys off and runs `PRAGMA foreign_key_check` before commit, so a migration may rebuild a referenced table (0003 rebuilds `strategy_profiles`).

## 5. Data flow

### 5.1 Production pipeline (one content item)

```
StrategyProfile (C1, user-owned)
  │  research request
  ▼
Research (C4): collect #056 → deduplicate #057 → extract topics #058 → score #059
  │  (collection saves progress per item; resume / retry failures #062)
  │  ResearchReport (claims, evidence, uncertainty) #060
  ▼
Script (C5): hook #065 → Shorts #066 or LongForm #067 script
  │        (Script = ordered sections + duration target + version history #064)
  │        (hooks: 3 checked candidates from TextGenerator, caller picks #065)
  │        (Shorts: chosen hook + 1-3 body + cta, JSON answer, checked, Script version #066)
  │        (LongForm: refused while LONGFORM_ENABLED is off; outline, then one call per
  │         chapter; hook + intro + 3-12 chapters + outro + cta, Script version #067)
  │        (claims: on demand per Script version, deterministic rules `rules-v1`,
  │         CTA/questions/opinions skipped, one ClaimKind each, max 200, run stored #068)
  │        (evidence: on demand per claim run, rules `rules-v1`, own research report only,
  │         word containment >= 0.5 and 2 words, numbers/dates agree|differ|none,
  │         max 3 links from distinct sources, run stored #069)
  │        (fact check: on demand per evidence match run, rules `rules-v1`, first matching
  │         of 9 rows per claim gives PASS/WARN/FAIL + code, derived worst-of run status,
  │         a record only (no gate, no override), run stored #070)
  │        → extract claims #068 → match evidence #069 → fact-check PASS/WARN/FAIL #070
  │        (originality: on demand per Script version, rules `originality-rules-v1`, word
  │         5-gram shingle containment against the latest version of the other items of
  │         the channel made before it (newest 50) + repeated hook/sentences/openers,
  │         PASS/WARN/FAIL, a record only (no gate, no override), run stored #071)
  │        (validation: on demand per Script version against the channel's current strategy,
  │         rules `script-rules-v1`: strict sections per type, length, EN/VI language,
  │         banned phrases, hook length; PASS/WARN/FAIL, a record only (no gate, no
  │         override), run stored #072)
  │        → originality #071 → validate #072 → version #073
  ▼
Rights & Policy (C6): register assets #076 → provenance #077 → rights risk #078
  │        → policy check #080 → AI disclosure #081 → reports #082, #083
  ▼
Voice (C7): TTS job #088 → audio artifact #089 → validate #090
  ▼
Production (C8 Shorts or C9 LongForm): timeline → subtitles → audio mix → render → thumbnail → metadata
  │  Artifact bundle #103 / #114 (MP4 + subtitles + thumbnail + metadata + reports), versioned in C2
  ▼
Quality (C10): container, resolution, FPS, duration, audio, subtitles, black frames, integrity, metadata
  │        + rights QC #126 + policy QC #127 + disclosure QC #128 → QC report #129
  ▼
Review (C11): preview the exact version #131 → approval request #137 → email #138
  │        → approve #139 / reject #140 / request changes #141
  │        (a change loops back: change → rerender → retest → new preview, #136)
  ▼
Publishing (C12): gates pass → Shorts #149 or LongForm #150 publisher → upload → result with YouTube video ID #155
  ▼
Analytics (C13) → Economics (C14)          Community (C15) reads comments on published videos
```

### 5.2 Lifecycle status (C2)

The statuses are defined in #031: `Draft`, `Generating`, `Testing`, `PreviewReady`, `AwaitingApproval`, `Approved`, `Publishing`, `Published`, `Rejected` and `Failed`. In code they are `ContentStatus` in `core/content_item.py` (C-031). Each stored value is the snake_case form of the name, for example `preview_ready`, and the database CHECK constraint on `content_items.status` allows exactly these ten values.

The transitions are defined in #032 (C-032, approved by the user on 2026-09-30) as `ALLOWED_TRANSITIONS` in `core/content_item.py`:

| From | Allowed to |
|---|---|
| Draft | Generating, Failed |
| Generating | Testing, Failed |
| Testing | PreviewReady, Generating, Failed |
| PreviewReady | AwaitingApproval, Generating, Failed |
| AwaitingApproval | Approved, Rejected, Generating, Failed |
| Approved | Publishing, Generating, Failed |
| Publishing | Published, Failed |
| Published | none (final) |
| Rejected | Draft |
| Failed | Draft |

`ContentItem.with_status` refuses every other move with `ContentTransitionError` (a `DomainError`, code `domain.content_transition_blocked`). Asking for the current status changes nothing. A regenerated artifact always passes Testing and PreviewReady again, and the only way into Approved is from AwaitingApproval, and into Publishing from Approved. An artifact change after approval also invalidates the approval (#035). The transition rules do not check who asks for a move or whether a gate passed; those checks are the gates of #033–#041.

### 5.3 Gates (C3)

| Gate | Blocks | Prompt |
|---|---|---|
| Approval gate | Publish without an explicit approval of the current artifact version. Implemented in `core/approval_gate.py` | #034 |
| Version invalidation | An approval whose artifact has changed. Implemented in `core/version_invalidation.py` | #035 |
| Daily limit gate | Production or publishing beyond the configured daily limit. Implemented in `core/daily_limit_gate.py` | #036 |
| Budget gate | Cost-incurring jobs beyond budget thresholds. Implemented in `core/budget_gate.py` | #037 |
| Rights gate | Publish with unresolved high-risk rights. Implemented in `core/rights_gate.py` | #038 |
| Policy gate | Publish with configured policy failures. Implemented in `core/policy_gate.py` | #039 |
| Kill switch gate | All new production actions while the emergency stop is active. Implemented in `core/kill_switch_gate.py` | #040 |
| Idempotency gate | Duplicate publish or generation jobs. Implemented in `core/idempotency_gate.py` | #041 |

Every gate implements the Pipeline Gate Contract (#033). The Pipeline Runner (#206) runs the lifecycle one gate at a time.

The contract (C-033, `core/gates.py`, design approved by the user on 2026-09-30):

- A gate is a `PipelineGate`: a `name` from the closed `GateName` enum (`test`, `qc`, `rights`, `policy`, `approval`; #036–#041 add theirs and D-053 adds `strategy`) and a synchronous `evaluate(context) -> GateResult`.
- `GateContext` holds the `ContentItem`, the `target_status` it is asked to move to (publishing is the move to `Publishing`), the `Actor` and the UTC time. The move must be allowed by #032. A gate reads anything else through repositories it is given when it is built.
- `GateResult` either passes with no reasons or blocks with one or more `GateReason` values (a dotted code and a safe message).
- `evaluate_gates` runs every gate in order and returns a `GateReport` with every result. The report blocks if any gate blocks, and `raise_if_blocked` raises `GateBlockedError` (`domain.gate_blocked`).
- Gates fail closed: a gate that raises or returns something other than its own result is counted as a block (`gate.error` or `gate.invalid_result`), and the detail goes only to the log.

Approval gate (C-034, rules approved by the user on 2026-09-30): `ApprovalGate` judges only the move to `Publishing`. The item's newest `ApprovalRequest` (latest `created_at`, then `id`) must be `approved`, and it must bind every artifact kind the item has at its latest version with the same id, version and sha256. Otherwise it blocks with `approval.missing`, `approval.not_approved` or `approval.not_current`. It always checks and does not read `APPROVAL_REQUIRED`. It reads through `ApprovalSource` and `ArtifactSource`, which the SQLite repositories satisfy.

Version invalidation (C-035, rules approved by the user on 2026-09-30): `VersionInvalidation.store_artifact_version` stores a new artifact version and, in the same transaction, invalidates every `pending` or `approved` request for the item that `stale_kinds` (in `content/approval.py`, shared with the approval gate) reports as stale. Rejected, changes_requested, expired and invalidated requests stay. If any request was invalidated, an item in `PreviewReady`, `AwaitingApproval` or `Approved` moves back to `Generating`. Each invalidation is audited as `approval.invalidated` by the system after the commit. `VersionInvalidation.invalidate(item_id)` runs the same check on demand, and `invalidate_stale_approvals` runs it inside a caller's transaction.

Daily limit gate (C-036, rules approved by the user on 2026-09-30): `DailyLimitGate` (`GateName.DAILY_LIMIT`) uses the channel's `StrategyProfile.cadence` for the item's content type as the limit, and counts production and publishing separately against it, per calendar day (00:00–24:00) in the cadence time zone (UTC until D-050 added `Cadence.time_zone`, user decision 2026-10-02). A production is the move `Draft → Generating`, counted from the append-only `production_starts` table (migration 0002) that `start_production` in `core/production.py` writes in the same transaction as the move. A publish is the move to `Publishing`, counted from publish jobs created that day that have not failed, leaving out the item's own jobs. Block reasons: `daily_limit.production_reached`, `daily_limit.publish_reached`, `daily_limit.no_strategy`. A limit of 0 blocks that type. Other moves pass.

Budget gate (C-037, rules approved by the user on 2026-09-30): `BudgetGate` (`GateName.BUDGET`) checks every move into `Generating` (a new production or a regeneration). The limits are the channel's `StrategyProfile.budget` (daily and monthly, one currency), and spend is the `Decimal` sum of its `CostRecord`s in the calendar day and month of the cadence time zone (UTC until D-051, user decision 2026-10-02; UTC when no cadence is configured). A limit is exceeded when spend ≥ limit, and each exceeded limit gives its own reason (`budget.daily_exceeded`, `budget.monthly_exceeded`). A cost in another currency in the month blocks with `budget.currency_mismatch`, and a channel without a strategy blocks with `budget.no_strategy`. Only actual spend counts; estimating a job's cost is the budget guard (#180).

Rights gate (C-038, rules approved by the user on 2026-10-01): `RightsGate` (`GateName.RIGHTS`) judges only the move to `Publishing`. It reads every `RightsRecord` of the item, and each one that is unresolved at level `high` or `unknown` blocks with its own reason (`rights.unresolved_high` or `rights.unresolved_unknown`) naming the asset ref, in record order. `unknown` counts as high so the gate fails closed. Unresolved `low` and `medium` pass, a record a user resolved passes at any level, and an item with no rights records passes; checking that every asset has a record is the asset registry (#076) and rights QC (#126).

Policy gate (C-039, rules approved by the user on 2026-10-01): `PolicyGate` (`GateName.POLICY`) judges only the move to `Publishing`. It reads `PolicyCheck` values (`content/policy.py`) through the `PolicySource` protocol; each check is one run over an item with its `PolicyFinding`s (rule id, rule version, a `blocking` flag copied from the rule's configuration, and a safe message). Only the newest check counts (latest `checked_at`, then `id`). Each blocking finding blocks with its own `policy.failed` reason, non-blocking findings are warnings, and an item without any check blocks with `policy.not_checked` so the gate fails closed. The rule interface (#079), the check that creates results (#080) and their storage and report (#083) come later.

Kill switch gate (C-040, rules approved by the user on 2026-10-01): `KillSwitchGate` (`GateName.KILL_SWITCH`) reads the current `EmergencyStop` (`pipeline/kill_switch.py`) through the `KillSwitchSource` protocol on every evaluation, so a stop applies to the next move. While it is active, every move into `Generating` (new production or regeneration) and the move to `Publishing` block with one `killswitch.active` reason: a fixed message plus the reason the activator gave, without naming the activator. Moves to draft, failed, rejected and the review states pass, so work can be wound down. The store, the user toggle and stopping running jobs are the kill switch (#213).

Idempotency gate (C-041, rules approved by the user on 2026-10-01): job keys are deterministic (`pipeline/idempotency.py`). `generation_key(item, kind)` is `gen:` plus the sha256 of the job kind (`content.generate` for the move into `Generating`), the item id, its status and its `updated_at`; `publish_key(item_id, approval_request_id)` is `pub:` plus the sha256 of `publish`, the item id and the approved request id. Whoever creates the job must use the same key. `IdempotencyGate` (`GateName.IDEMPOTENCY`) derives the key for a move into `Generating`, or for the move to `Publishing` from the item's newest approval request when it is approved, and blocks when a job is already stored under it, unless that job failed (a retry restarts the same job). A cancelled job blocks because its UNIQUE key stays taken. Without an approved newest request the gate passes; the approval gate blocks that move. #151 and #152 add upload-level publish guarantees.

Gate tests (C-042, scope approved by the user on 2026-10-01): `tests/test_gate_matrix.py` checks every concrete gate against every move #032 allows. Which moves each gate guards: approval, rights and policy guard `→ Publishing`; budget guards `→ Generating`; daily limit guards `Draft → Generating` and `→ Publishing`; kill switch and idempotency guard `→ Generating` and `→ Publishing`. This table is kept in the test only; the pipeline runner (#206) and the publishing blocker (#084) decide which gates run in the flow.

### 5.4 Control and observability flows

- **Orchestration (C16):** the job queue (#204) and scheduler (#205) feed the pipeline runner (#206). Jobs wait and resume across human approval (#207), retry with bounded backoff (#208), and resume after a crash from the last safe checkpoint (#211).
- **Audit:** approval decisions (#143), publishes (#156), comment drafts and posts (#189), and job events (#214) all produce audit events (#011).
- **Cost:** TTS (#092, #176), LLM (#175), render (#177) and storage/API (#178) costs flow into the cost ledger (#174). The budget guard (#180) and budget gate (#037) read from it.
- **Command Center (X3):** reads jobs, content, analytics, revenue, comments and approvals. It writes only user actions, such as approve, reject, request changes and settings.

## 6. Feature flags in the flow

| Flag | Default (A-006) | Effect in the flow | Source |
|---|---|---|---|
| `SHORTS_ENABLED` | `true` | Enables the C8 path | #006, REQUIREMENTS §5 |
| `LONGFORM_ENABLED` | `false` | Enables the C9 path. It stays locked until the LongForm unlock | #006, #237 |
| `PUBLISH_ENABLED` | `false` | Enables C12 publishing. It requires `TEST_REQUIRED` and `APPROVAL_REQUIRED` | #006, R-08 |
| `TEST_REQUIRED` | `true` | Requires the C10 stage. Always true in production | #006, REQUIREMENTS §4 |
| `APPROVAL_REQUIRED` | `true` | Requires the C11 approval. Always true in production | #006, R-08 |
| `AUTO_REPLY_ENABLED` | `false` | Replies are drafts only. Enforcement comes in #188 | #006, #188 |

## 7. Folder structure (set by A-004)

A-004 created these folders and kept the existing `src/` layout.

| A-004 folder | Path | Contexts |
|---|---|---|
| Core | `src/ai_youtube_agent/core/` | X1 Foundation, C2 Content Lifecycle, C3 Control Gates, X2 Persistence |
| Content | `src/ai_youtube_agent/content/` | C1, C4, C5, C6, C7, C8, C9, C10, C11, C13, C14, C15 |
| Providers | `src/ai_youtube_agent/providers/` | Every provider interface in 4.1 and its mocks |
| Pipeline | `src/ai_youtube_agent/pipeline/` | C16 Orchestration, C12 Publishing flow |
| Dashboard | `dashboard/` (repository root) | X3 Command Center. It is outside the Python package because its platform is still open (Q4) |
| Tests | `tests/` (existing) | All tests |
| Docs | `docs/` (existing) | Requirements, architecture and reports |

The four backend folders are empty packages. Each one holds only an `__init__.py` with a docstring. Later prompts add modules inside them. The FastAPI app stays at `src/ai_youtube_agent/main.py`.

## 8. Open questions

The requirements do not decide these. Each one must be answered by the user or by the prompt named, and none is decided here.

| # | Question | Where it matters |
|---|---|---|
| Q1 | There is no dedicated **LLM provider interface**, although scripts are generated (#065–#067) and LLM cost is tracked (#175). **Resolved in F-065 (user decision, 2026-10-03):** a synchronous `TextGenerator` provider interface with a deterministic mock (`providers/text_generation.py`, `providers/mock_text_generation.py`), selected by `Settings.text_provider`; no real provider yet. | C5, C14 |
| Q2 | The **render provider** is required "through provider abstraction" (#100, #111), but no prompt defines the interface itself. | C8, C9 |
| Q3 | There is no **email provider interface** for approval emails (#138, #228). | C11 |
| Q4 | The **Command Center platform** is "iOS-style" (#191). Is it a native iOS app or a web dashboard with an iOS-style design system? The backend is FastAPI. | X3 |
| Q5 | The **database technology** is not specified. Only migrations and rollback are required (#029). **Resolved in B-029 (user decision, 2026-09-30):** SQLite via the standard `sqlite3`, numbered forward-only SQL migrations in `core/db/migrations/`, backup before migrating. | X2 |
| Q6 | The **durable queue technology** is not specified (#204). | C16 |
| Q7 | The **thumbnail** generation or selection source is not specified (#101, #112). | C8, C9 |
| Q8 | The source of **music and SFX** assets is not specified (#099). Any use must pass through the asset registry (#076). | C8, C6 |

## 9. Traceability check

Every prompt from #001 to #238 is assigned to exactly one context or cross-cutting area in section 3. This was verified by a script against `docs/REQUIREMENTS.md` when A-003 was completed.
