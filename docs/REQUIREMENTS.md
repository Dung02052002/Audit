# Requirements (v7 source of truth)

| Item | Value |
|---|---|
| Status | **FROZEN** |
| Frozen by | A-002 Requirements Freeze |
| Frozen on | 2026-09-25 |
| Source | `AI_YouTube_Autonomous_Agent_PROMPT_PACK_v8_BASELINE_SAFE.pdf`: Prompt Pack v8, which embeds the 238 micro-prompts of Prompt Pack v7 |
| Scope | Product and engineering requirements for the AI YouTube Autonomous Agent (`ai_youtube_agent`) |

This document restates Prompt Pack v8. It adds no requirements of its own. If this document and the pack disagree, the pack wins, and this document must be corrected.

## 1. Change control

- These requirements are frozen. A change needs the user's explicit approval and a new entry in `CHANGELOG.md`.
- The AI must not add, remove or reinterpret a requirement on its own (rules R-02, R-03 and R-09).
- Each later prompt implements only its own row in the catalog (section 8).

## 2. Product goal

Build the AI YouTube Autonomous Agent as 238 small micro-tasks. They are implemented in sequence, and each prompt delivers one function with a clear dependency, scope, tests and acceptance gate. The aim is to stop the AI from inferring scope on its own.

## 3. Content types (frozen)

The system has **exactly two content types**:

| Content type | Meaning in the pack | Production phase |
|---|---|---|
| `SHORTS` | Short-form video | Phase I (prompts 94–104) |
| `LONGFORM` | Long-form video | Phase J (prompts 105–115) |

No other content type exists. Adding one requires a requirements change (section 1).

## 4. Control stages (shared gates, frozen)

`TEST`, `QC`, `PREVIEW` and `APPROVAL` are **not content types**. They are required control stages, shared by both `SHORTS` and `LONGFORM`.

| Control stage | Required | Applies to | Implemented in |
|---|---|---|---|
| `TEST` | Yes | SHORTS and LONGFORM | Phase K: Test & QC (116–130) |
| `QC` | Yes | SHORTS and LONGFORM | Phase K: Test & QC (116–130) |
| `PREVIEW` | Yes | SHORTS and LONGFORM | Phase L: Preview & Approval (131–144) |
| `APPROVAL` | Yes | SHORTS and LONGFORM | Phase L: Preview & Approval (131–144) |

A production publish always requires a valid approval (rule R-08).

## 5. Pilot and rollout

- **Shorts first.** Shorts runs end to end first. LongForm production opens only after the required technical gates have PASSED.
- **Rollout order:** finish the foundation → finish the shared pipeline → finish Shorts → QC/Preview/Approval → run the Shorts pilot → confirm the technical gate → open LongForm → finish the integration audit.
- **Implementation order:** Foundation → Domain → Gates → Strategy → Research → Script → Rights/Policy → Voice → Shorts → LongForm → QC → Preview/Approval → YouTube → Analytics → Economics → Comments → Dashboard → Orchestrator → Security → Integration/Pilot.
- **Technical PASS is not a business guarantee.** It proves only that the system meets the technical criteria that were tested. It does not guarantee revenue, monetization, YPP or business results.

## 6. Rules for every prompt (Master Ruleset)

| ID | Rule | Requirement |
|---|---|---|
| R-01 | INSPECT FIRST | Read the repository, architecture, dependencies and current tests before changing anything. |
| R-02 | IMPLEMENT ONLY THIS TASK | Do only the function of the current prompt. |
| R-03 | DO NOT ASSUME | If a dependency does not exist or breaks its contract, stop and report it. |
| R-04 | DO NOT MODIFY UNRELATED MODULES | Do not refactor or touch modules outside the scope unless it is required. |
| R-05 | TEST BEFORE FINISHING | Build and test the new work, and run the relevant regression checks. |
| R-06 | STOP ON BROKEN DEPENDENCY | Never jump ahead to the next prompt. |
| R-07 | NO SECRETS IN SOURCE | Credentials, API keys and OAuth secrets stay outside source control. |
| R-08 | NO AUTO-PUBLISH | A production publish always requires a valid approval. |
| R-09 | NO STRATEGY DRIFT | The AI never changes market, language, niche, format, budget or channel on its own. |
| R-10 | EVERY PROMPT ENDS WITH PASS/FAIL | Report files changed, tests, errors, risks and the gate result. |

## 7. Engineering protocol

**Global Baseline Rule:** never fail the current task because of an unrelated pre-existing error.

1. Run the baseline before changing anything.
2. Record the failures that already exist.
3. After the change, run the related tests.
4. An old failure the task did not cause is PRE_EXISTING and does not fail the task.
5. A regression caused by the task is a FAIL.
6. A failure of unknown origin is UNKNOWN: stop and investigate.

**Task test protocol:** Inspect → Baseline → Implement only scope → Targeted tests → Relevant regression tests → Reclassify failures → Report → PASS/FAIL.

**Standard prompt contract:** Goal → Dependency → Scope → Implementation → Allowed Changes → Tests → Acceptance Criteria → PASS/FAIL Report.

**Gate for every prompt:** a prompt PASSES only when its function can be verified, the relevant tests run successfully, and no earlier contract or gate is broken. On FAIL, stop at that prompt.

**Execution order:** run the 238 prompts in sequence from 001 to 238. Do not skip a prompt because the code "seems to exist already"; inspect and test it. Within a phase, do not start a prompt until the previous one has PASSED.

**Project state machine:**

```
EMPTY    → BOOTSTRAP_REQUIRED → BOOTSTRAPPED      → BASELINE_RECORDED → READY_FOR_PHASE_A
EXISTING → AUDIT_REQUIRED     → BASELINE_RECORDED → READY_FOR_PHASE_A
PARTIAL  → MINIMAL_BOOTSTRAP  → BASELINE_RECORDED → READY_FOR_PHASE_A
```

Phase A starts only after Phase 0 prompt 011 PASSES. Do not invent state values outside this machine.

## 8. Capability catalog (238 prompts)

Each row is one required capability. The goal text is copied verbatim from the pack. "Dependency" uses the pack's notation: `A-1` means the first prompt of Phase A.

### Phase A: Foundation & Governance (1–12)

| # | Prompt | Dependency | Goal (verbatim from the pack) |
|---|---|---|---|
| 001 | Project Audit | None | Inspect repository, runtime, existing modules and tests. Do not change code. Produce architecture/state report and dependency risks. |
| 002 | Requirements Freeze | A-1 | Create the v7 source-of-truth requirements document. Confirm Shorts + LongForm as the only content types and Test/QC/Preview/Approval as shared gates. |
| 003 | Architecture Map | A-2 | Document bounded contexts, interfaces and data flow. Do not implement features. |
| 004 | Folder Structure | A-3 | Create/normalize folders for Core, Content, Providers, Pipeline, Dashboard, Tests and Docs. Preserve existing conventions where valid. |
| 005 | Configuration Contract | A-4 | Define typed configuration loading, validation and environment separation. No secrets in source. |
| 006 | Feature Flags | A-5 | Implement flags for SHORTS_ENABLED, LONGFORM_ENABLED, PUBLISH_ENABLED, TEST_REQUIRED, APPROVAL_REQUIRED and AUTO_REPLY_ENABLED=false. |
| 007 | Logging Contract | A-5 | Create structured logging with correlation/session/job IDs and severity levels. |
| 008 | Error Model | A-7 | Create typed application/domain/provider error categories and safe user-facing messages. |
| 009 | Dependency Injection | A-8 | Register core interfaces and providers without hard-coding implementations. |
| 010 | Health Check | A-9 | Create application/provider health checks and a minimal health endpoint/status model. |
| 011 | Audit Event Model | A-10 | Create immutable audit events for important actions, actor, timestamp, entity and result. |
| 012 | Build Baseline | A-11 | Run full build/test and record baseline failures. Fix only infrastructure blockers introduced by the foundation. |

### Phase B: Domain & Persistence (13–30)

| # | Prompt | Dependency | Goal (verbatim from the pack) |
|---|---|---|---|
| 013 | Channel Entity | A-12 | Create Channel entity with stable ID, YouTube identifiers, status and timestamps. |
| 014 | StrategyProfile Entity | B-1 | Create user-controlled market, language, audience, niche, brand, cadence, budget and monetization configuration. |
| 015 | ContentItem Entity | B-2 | Create common content entity with ContentType=Shorts\|LongForm and lifecycle status. |
| 016 | Artifact Entity | B-3 | Create versioned artifact records for video, audio, subtitles, thumbnail and metadata. |
| 017 | Script Entity | B-4 | Create versioned script and claim/evidence relationships. |
| 018 | Voice Entity | B-5 | Create voice configuration and generated audio metadata. |
| 019 | Rights Entity | B-6 | Create asset provenance, source, license and risk status. |
| 020 | QC Entity | B-7 | Create structured QC result with PASS/WARN/FAIL checks. |
| 021 | Approval Entity | B-8 | Create approval request tied to exact artifact version. |
| 022 | Publish Entity | B-9 | Create idempotent publish job/result model. |
| 023 | Analytics Entity | B-10 | Create metric snapshot with source, period, retrieval time and freshness. |
| 024 | Revenue Entity | B-11 | Create Estimated/Final revenue records with currency, period, source and freshness. |
| 025 | Cost Entity | B-12 | Create production/API/TTS/render/storage cost records. |
| 026 | Comment Entity | B-13 | Create comment, classification, draft reply and posting state. |
| 027 | AI Job Entity | B-14 | Create resumable AI job/session entity with attempts, status and checkpoints. |
| 028 | Experiment Entity | B-15 | Create title/thumbnail/content experiment registry without auto-changing strategy. |
| 029 | Migrations | B-16 | Implement migrations/schema creation and rollback strategy. |
| 030 | Repository Tests | B-17 | Add persistence repository tests and fixture factories. |

### Phase C: State Machine & Control Gates (31–42)

| # | Prompt | Dependency | Goal (verbatim from the pack) |
|---|---|---|---|
| 031 | Status Enum | B-18 | Define Draft, Generating, Testing, PreviewReady, AwaitingApproval, Approved, Publishing, Published, Rejected, Failed. |
| 032 | Transition Rules | C-1 | Implement allowed/blocked transitions and typed transition errors. |
| 033 | Pipeline Gate Contract | C-2 | Define shared gate interface for Test, QC, Rights, Policy and Approval. |
| 034 | Approval Gate | C-3 | Block publish unless explicit approval exists for current artifact version. |
| 035 | Version Invalidation | C-4 | Invalidate approval when approved artifact changes. |
| 036 | Daily Limit Gate | C-5 | Enforce configured daily production/publish limit. |
| 037 | Budget Gate | C-6 | Block cost-incurring jobs when budget thresholds are exceeded. |
| 038 | Rights Gate | C-7 | Block publish on unresolved high-risk rights. |
| 039 | Policy Gate | C-8 | Block publish on configured policy failures. |
| 040 | Kill Switch Gate | C-9 | Block all new production actions when emergency stop is active. |
| 041 | Idempotency Gate | C-10 | Prevent duplicate publish/generation jobs using deterministic keys. |
| 042 | Gate Tests | C-11 | Create unit tests for every gate and transition combination. |

### Phase D: Strategy & Channel Configuration (43–53)

| # | Prompt | Dependency | Goal (verbatim from the pack) |
|---|---|---|---|
| 043 | Channel Settings | C-12 | Create channel configuration UI/API and validation. |
| 044 | Market Settings | D-1 | Create market configuration without automatic strategy changes. |
| 045 | Language Settings | D-2 | Create primary/secondary language configuration. |
| 046 | Audience Settings | D-3 | Create audience profile configuration. |
| 047 | Niche Settings | D-4 | Create niche/content-pillar configuration. |
| 048 | Brand Settings | D-5 | Create voice, visual and tone rules. |
| 049 | Format Settings | D-6 | Configure Shorts and LongForm production defaults. |
| 050 | Cadence Settings | D-7 | Configure daily limits and scheduling preferences. |
| 051 | Budget Settings | D-8 | Configure daily/monthly spend limits and alerts. |
| 052 | Monetization Settings | D-9 | Configure revenue-source tracking goals, not guaranteed outcomes. |
| 053 | Strategy Validation | D-10 | Validate incompatible or missing configuration before a run starts. |

### Phase E: Research System (54–63)

| # | Prompt | Dependency | Goal (verbatim from the pack) |
|---|---|---|---|
| 054 | Research Provider | D-11 | Define provider interface with search/fetch abstraction and mock provider. |
| 055 | Source Model | E-1 | Normalize source URL, title, timestamp, provider and evidence notes. |
| 056 | Source Collector | E-2 | Collect sources for a research request with retries and limits. |
| 057 | Source Deduplication | E-3 | Detect duplicate/near-duplicate sources and topics. |
| 058 | Topic Extractor | E-4 | Extract candidate topics and supporting evidence. |
| 059 | Topic Scoring | E-5 | Compute transparent internal relevance/novelty signals; do not alter strategy. |
| 060 | Research Report | E-6 | Generate structured research report with claims, evidence and uncertainty. |
| 061 | Research Cache | E-7 | Cache safe research results with freshness policy. |
| 062 | Research Failure Recovery | E-8 | Retry transient failures and persist partial progress. |
| 063 | Research Tests | E-9 | Test mocked sources, duplicates, empty results and provider failures. |

### Phase F: Script & Fact Check (64–74)

| # | Prompt | Dependency | Goal (verbatim from the pack) |
|---|---|---|---|
| 064 | Script Model | E-10 | Define script sections, duration target, claims and version history. |
| 065 | Hook Generator | F-1 | Implement hook generation with format-specific constraints. |
| 066 | Shorts Script Generator | F-2 | Generate concise Shorts scripts using StrategyProfile and ResearchReport. |
| 067 | LongForm Script Generator | F-3 | Generate structured long-form scripts with chapters. |
| 068 | Claim Extractor | F-4 | Extract factual claims from a script. |
| 069 | Evidence Matcher | F-5 | Match claims to stored research evidence. |
| 070 | Fact Check Result | F-6 | Create PASS/WARN/FAIL claim-level results. |
| 071 | Originality Check | F-7 | Detect excessive reuse and repetitive structure against prior content. |
| 072 | Script Validator | F-8 | Validate length, required sections, language and content-type rules. |
| 073 | Script Versioning | F-9 | Store revisions and diff metadata. |
| 074 | Script Tests | F-10 | Test generators, claims, evidence gaps and revision behavior. |

### Phase G: Rights, Policy & AI Disclosure (75–85)

| # | Prompt | Dependency | Goal (verbatim from the pack) |
|---|---|---|---|
| 075 | Asset Model | F-11 | Define generated, licensed, public-domain and user-owned asset categories. |
| 076 | Asset Registry | G-1 | Register every external/generated asset used in a video. |
| 077 | Provenance Record | G-2 | Store source and license/provenance metadata. |
| 078 | Rights Risk Engine | G-3 | Classify unresolved rights risks and block configured high-risk cases. |
| 079 | Policy Rule Interface | G-4 | Create versioned policy rule interface and mock rules. |
| 080 | Policy Check | G-5 | Evaluate content/metadata against configured rules. |
| 081 | AI Disclosure Rule | G-6 | Create versioned disclosure decision and store rationale/source. |
| 082 | Rights Report | G-7 | Generate machine-readable and dashboard-friendly rights report. |
| 083 | Policy Report | G-8 | Generate policy report with blocking findings. |
| 084 | Publishing Blocker | G-9 | Wire rights/policy failures into the shared publish gate. |
| 085 | Rights Tests | G-10 | Test safe, uncertain and blocked asset scenarios. |

### Phase H: Voice & Audio (86–93)

| # | Prompt | Dependency | Goal (verbatim from the pack) |
|---|---|---|---|
| 086 | Voice Provider | G-11 | Define TTS provider interface and mock provider. |
| 087 | Voice Profile | H-1 | Configure voice, language, pronunciation and speaking style. |
| 088 | TTS Job | H-2 | Create resumable audio generation job. |
| 089 | Audio Artifact | H-3 | Persist generated audio metadata, duration, provider and cost. |
| 090 | Audio Validation | H-4 | Check duration, corruption, silence and basic level constraints. |
| 091 | Audio Retry | H-5 | Retry transient provider failures without uncontrolled duplication. |
| 092 | Audio Cost | H-6 | Record TTS cost per artifact/job. |
| 093 | Voice Tests | H-7 | Test provider mock, invalid audio and retry behavior. |

### Phase I: Shorts Production (94–104)

| # | Prompt | Dependency | Goal (verbatim from the pack) |
|---|---|---|---|
| 094 | Shorts Project | H-8 | Create Shorts-specific project model and configuration. |
| 095 | Shorts Timeline | I-1 | Implement scene/timeline representation. |
| 096 | Shorts Scene | I-2 | Implement scene asset and timing model. |
| 097 | Shorts Captions | I-3 | Implement caption overlays and safe-area rules. |
| 098 | Shorts Subtitles | I-4 | Generate timed subtitle artifact. |
| 099 | Shorts Audio Mix | I-5 | Combine voice/music/SFX under configured constraints. |
| 100 | Shorts Render | I-6 | Render vertical MP4 through provider abstraction. |
| 101 | Shorts Thumbnail | I-7 | Generate/select thumbnail artifact and metadata. |
| 102 | Shorts Metadata | I-8 | Create title, description, tags/metadata package as applicable. |
| 103 | Shorts Artifact Bundle | I-9 | Package MP4 + subtitles + thumbnail + metadata + reports. |
| 104 | Shorts Render Tests | I-10 | Validate output fixtures and failure cases. |

### Phase J: LongForm Production (105–115)

| # | Prompt | Dependency | Goal (verbatim from the pack) |
|---|---|---|---|
| 105 | LongForm Project | I-11 | Create LongForm-specific project model and configuration. |
| 106 | LongForm Timeline | J-1 | Implement chapter/scene/timeline representation. |
| 107 | Chapters | J-2 | Generate chapter metadata from approved script. |
| 108 | B-roll Manager | J-3 | Map approved assets to timeline scenes with provenance. |
| 109 | LongForm Subtitles | J-4 | Generate long-form subtitle artifact. |
| 110 | LongForm Audio Mix | J-5 | Build long-form audio mix pipeline. |
| 111 | LongForm Render | J-6 | Render long-form MP4 through provider abstraction. |
| 112 | LongForm Thumbnail | J-7 | Generate/select thumbnail artifact. |
| 113 | LongForm Metadata | J-8 | Create title, description, chapters and metadata package. |
| 114 | LongForm Artifact Bundle | J-9 | Package all long-form artifacts and reports. |
| 115 | LongForm Render Tests | J-10 | Validate output fixtures and failure cases. |

### Phase K: Test & QC (116–130)

| # | Prompt | Dependency | Goal (verbatim from the pack) |
|---|---|---|---|
| 116 | Test Harness | J-11 | Create reusable test harness for rendered video artifacts. |
| 117 | Container Test | K-1 | Validate MP4/container/codec readability. |
| 118 | Resolution Test | K-2 | Validate configured resolution and aspect ratio. |
| 119 | FPS Test | K-3 | Validate configured FPS. |
| 120 | Duration Test | K-4 | Validate duration constraints for content type. |
| 121 | Audio Test | K-5 | Validate presence, duration, clipping/silence thresholds. |
| 122 | Subtitle Test | K-6 | Validate timing, encoding and missing captions. |
| 123 | Black Frame Test | K-7 | Detect excessive black/frozen frames. |
| 124 | Render Integrity Test | K-8 | Detect incomplete/corrupt renders. |
| 125 | Metadata Test | K-9 | Validate consistency between artifact and publish metadata. |
| 126 | Rights QC | K-10 | Run rights report as a blocking QC dependency. |
| 127 | Policy QC | K-11 | Run policy report as a blocking QC dependency. |
| 128 | AI Disclosure QC | K-12 | Verify configured disclosure requirement is satisfied. |
| 129 | QC Report | K-13 | Aggregate checks into structured PASS/WARN/FAIL report. |
| 130 | QC Regression Suite | K-14 | Create regression fixtures for both Shorts and LongForm. |

### Phase L: Preview & Approval (131–144)

| # | Prompt | Dependency | Goal (verbatim from the pack) |
|---|---|---|---|
| 131 | Preview Artifact API | K-15 | Expose exact rendered artifact version for preview. |
| 132 | Preview Player | L-1 | Build actual video playback component. |
| 133 | Preview Metadata | L-2 | Show title, description, thumbnail, subtitles and chapters. |
| 134 | Preview Reports | L-3 | Show QC, rights and policy reports. |
| 135 | Version Comparison | L-4 | Compare current and previous artifact versions. |
| 136 | Preview Edit Loop | L-5 | Allow change → rerender → retest → new preview. |
| 137 | Approval Request | L-6 | Create approval request bound to artifact version. |
| 138 | Email Approval | L-7 | Send approval email containing safe preview/reference and exact artifact ID. |
| 139 | Approve Action | L-8 | Implement explicit approval action. |
| 140 | Reject Action | L-9 | Implement reject action with reason. |
| 141 | Request Changes | L-10 | Implement change-request workflow. |
| 142 | Approval Expiry | L-11 | Define and implement configurable expiry/revalidation behavior. |
| 143 | Approval Audit | L-12 | Audit every approval/rejection/change decision. |
| 144 | Approval Tests | L-13 | Test stale approval, rejection and version invalidation. |

### Phase M: YouTube Integration & Publishing (145–157)

| # | Prompt | Dependency | Goal (verbatim from the pack) |
|---|---|---|---|
| 145 | OAuth Contract | L-14 | Define secure OAuth/token interface. |
| 146 | Channel Provider | M-1 | Implement channel read provider interface. |
| 147 | Upload Provider | M-2 | Implement upload provider interface with mock. |
| 148 | Publish Metadata Mapper | M-3 | Map internal metadata to YouTube payloads. |
| 149 | Shorts Publisher | M-4 | Implement Shorts publishing path. |
| 150 | LongForm Publisher | M-5 | Implement LongForm publishing path. |
| 151 | Idempotency Key | M-6 | Generate deterministic publish keys. |
| 152 | Duplicate Guard | M-7 | Block duplicate publish attempts. |
| 153 | Upload Retry | M-8 | Implement resumable/retry behavior. |
| 154 | Token Recovery | M-9 | Handle expired/invalid authorization safely. |
| 155 | Publish Result | M-10 | Persist confirmed YouTube video ID/result. |
| 156 | Publish Audit | M-11 | Audit publish request, approval and result. |
| 157 | Publishing Tests | M-12 | Mock upload success, failure, retry and duplicate cases. |

### Phase N: Analytics (158–169)

| # | Prompt | Dependency | Goal (verbatim from the pack) |
|---|---|---|---|
| 158 | Analytics Provider | M-13 | Define provider interface and mock data source. |
| 159 | Channel Metrics | N-1 | Sync subscribers, views and other available channel metrics. |
| 160 | Video Metrics | N-2 | Sync per-video metrics. |
| 161 | Shorts Metrics | N-3 | Normalize Shorts-specific metrics where available. |
| 162 | LongForm Metrics | N-4 | Normalize long-form metrics. |
| 163 | Watch Time | N-5 | Normalize watch-time periods and units. |
| 164 | Retention | N-6 | Store retention metrics only when actually available. |
| 165 | CTR | N-7 | Store CTR only when actually available. |
| 166 | Subscriber Change | N-8 | Track gained/lost subscribers by period. |
| 167 | Freshness Model | N-9 | Store source, retrieval time, period and freshness. |
| 168 | Analytics Cache | N-10 | Cache and invalidate analytics safely. |
| 169 | Analytics Tests | N-11 | Test pagination, missing fields, stale data and provider failure. |

### Phase O: Revenue, Costs & Economics (170–181)

| # | Prompt | Dependency | Goal (verbatim from the pack) |
|---|---|---|---|
| 170 | Revenue Provider | N-12 | Define revenue source interface and mock. |
| 171 | Estimated Revenue | O-1 | Store estimated revenue separately from final. |
| 172 | Final Revenue | O-2 | Store finalized revenue separately with source/freshness. |
| 173 | Revenue Sources | O-3 | Support ads and extensible memberships/affiliate/sponsorship records. |
| 174 | Cost Ledger | O-4 | Record all tracked contribution costs. |
| 175 | LLM Cost | O-5 | Record model/provider usage cost. |
| 176 | TTS Cost | O-6 | Connect voice generation costs to ledger. |
| 177 | Render Cost | O-7 | Record rendering cost. |
| 178 | Storage/API Cost | O-8 | Record configured storage/API costs. |
| 179 | Contribution Margin | O-9 | Calculate revenue minus tracked contribution costs; never label as net profit. |
| 180 | Budget Guard | O-10 | Enforce daily/monthly budget limits and alerts. |
| 181 | Economics Tests | O-11 | Test currencies, periods, estimated/final and budget blocking. |

### Phase P: Comments & Community (182–190)

| # | Prompt | Dependency | Goal (verbatim from the pack) |
|---|---|---|---|
| 182 | Comment Provider | O-12 | Define provider interface and mock. |
| 183 | Comment Sync | P-1 | Sync comments and pagination safely. |
| 184 | Comment Classification | P-2 | Classify comments for attention/moderation/reply drafting. |
| 185 | Reply Draft | P-3 | Generate draft reply without posting by default. |
| 186 | Reply Approval | P-4 | Add optional human approval for reply posting. |
| 187 | Duplicate Reply Guard | P-5 | Prevent duplicate replies. |
| 188 | AUTO_REPLY Flag | P-6 | Keep AUTO_REPLY_ENABLED=false by default and enforce it. |
| 189 | Comment Audit | P-7 | Audit drafts and posts. |
| 190 | Comment Tests | P-8 | Test sync, duplicate, moderation and reply state. |

### Phase Q: iOS YouTube Command Center (191–203)

| # | Prompt | Dependency | Goal (verbatim from the pack) |
|---|---|---|---|
| 191 | Dashboard Shell | P-9 | Create app navigation and shared iOS-style design system. |
| 192 | Home Screen | Q-1 | Show channel health, AI status, production count, approvals and alerts. |
| 193 | Channel Overview | Q-2 | Show channel identity, subscribers, views, watch time and content counts. |
| 194 | Content Screen | Q-3 | Separate Shorts and LongForm by lifecycle state. |
| 195 | Video Detail | Q-4 | Show real preview and all video artifacts/reports. |
| 196 | Analytics Screen | Q-5 | Show time-range metrics and Shorts vs LongForm. |
| 197 | Revenue Screen | Q-6 | Show estimated/final revenue, costs and contribution margin. |
| 198 | Comments Screen | Q-7 | Show new/attention comments and reply drafts. |
| 199 | AI Center | Q-8 | Show active jobs, progress, errors, retries, provider/cost and session state. |
| 200 | Approval Center UI | Q-9 | Show pending approvals and exact artifact version. |
| 201 | Settings Screen | Q-10 | Expose channel, OAuth, email, providers, strategy, limits and flags. |
| 202 | Alerts | Q-11 | Implement actionable alert cards for failures, approvals, budget and health. |
| 203 | Dashboard Tests | Q-12 | Test mocked data rendering and empty/error/loading states. |

### Phase R: Orchestrator & Session Runtime (204–215)

| # | Prompt | Dependency | Goal (verbatim from the pack) |
|---|---|---|---|
| 204 | Job Queue | Q-13 | Implement durable queue abstraction. |
| 205 | Job Scheduler | R-1 | Schedule eligible pipeline jobs without exceeding limits. |
| 206 | Pipeline Runner | R-2 | Execute shared lifecycle one gate at a time. |
| 207 | Approval Wait | R-3 | Persist and resume jobs while waiting for human approval. |
| 208 | Retry Manager | R-4 | Implement bounded retries/backoff. |
| 209 | Session Manager | R-5 | Start/stop a user-initiated session and track state. |
| 210 | Daily Stop | R-6 | Stop new production after configured daily limit. |
| 211 | Crash Resume | R-7 | Resume from last safe checkpoint after restart. |
| 212 | Graceful Shutdown | R-8 | Finish safe operations and persist state before exit. |
| 213 | Kill Switch | R-9 | Stop new production/publish actions immediately. |
| 214 | Orchestrator Audit | R-10 | Audit every job start, completion, retry and block. |
| 215 | Orchestrator Tests | R-11 | Test concurrency, retry, approval wait, crash and limit cases. |

### Phase S: Security & Recovery (216–223)

| # | Prompt | Dependency | Goal (verbatim from the pack) |
|---|---|---|---|
| 216 | Secret Boundary | R-12 | Move all secrets/credentials outside source control. |
| 217 | Token Storage | S-1 | Securely store/refresh OAuth tokens using platform-appropriate storage. |
| 218 | Least Privilege | S-2 | Document required scopes and minimize permissions. |
| 219 | Sensitive Logging | S-3 | Redact secrets/tokens/PII from logs. |
| 220 | Backup Plan | S-4 | Implement/document backup for critical state and artifacts. |
| 221 | Restore Test | S-5 | Perform a restore test using non-production data. |
| 222 | Failure Injection | S-6 | Test network, provider, render and DB failures. |
| 223 | Security Audit | S-7 | Review dependencies, secrets, permissions and unsafe defaults. |

### Phase T: Integration & Production Readiness (224–238)

| # | Prompt | Dependency | Goal (verbatim from the pack) |
|---|---|---|---|
| 224 | Core Integration | S-8 | Run integration tests across persistence, state machine and gates. |
| 225 | Shorts E2E | T-1 | Run full Shorts pipeline through preview and approval using controlled/mock publishing. |
| 226 | LongForm E2E | T-2 | Run full LongForm pipeline through preview and approval using controlled/mock publishing. |
| 227 | Dashboard E2E | T-3 | Verify dashboard reflects pipeline, analytics, economics and approvals. |
| 228 | Email E2E | T-4 | Verify approval email/request/reply flow in controlled environment. |
| 229 | YouTube E2E | T-5 | Verify OAuth and publishing in controlled test conditions before production. |
| 230 | Duplicate Test | T-6 | Attempt duplicate publish and verify hard block/idempotency. |
| 231 | Stale Approval Test | T-7 | Modify artifact after approval and verify old approval is rejected. |
| 232 | Daily Limit Test | T-8 | Exceed configured limit and verify orchestrator stops production. |
| 233 | Budget Test | T-9 | Exceed budget threshold and verify cost-incurring jobs are blocked. |
| 234 | Kill Switch Test | T-10 | Activate kill switch during a session and verify new production actions stop. |
| 235 | Recovery Test | T-11 | Crash during pipeline and verify safe resume without duplicate artifacts/uploads. |
| 236 | Pilot Report | T-12 | Run one real Shorts pilot with explicit user approval and produce a technical report. |
| 237 | LongForm Unlock | T-13 | Unlock LongForm production only if the required technical gates pass. |
| 238 | Production Audit | T-14 | Generate final readiness report: READY/NOT READY, blockers, risks, rollback. |
