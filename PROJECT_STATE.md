# Project State

Last updated: 2026-10-20 (BUILD-FIRST: up to Q-200 implemented, untested)

This file is the handoff document. A new session should read it, together with `TASK_STATUS.md`, before doing anything else. There is no need to audit the repository again from the start.

## Current state

```
PROJECT_STATE      = READY_FOR_PHASE_A
BASELINE_COMMIT    = 3b41416
CURRENT_CHECKPOINT = 7c64291
BASELINE_STATUS    = CLEAN
TEST_STATUS        = PASS at 8b41c0b; DEFERRED for the BUILD-FIRST work
LINT_STATUS        = PASS
BUILD_STATUS       = PASS
KNOWN_FAILURES     = NONE
UNKNOWN_BLOCKERS   = NONE
BUILD_MODE         = BUILD-FIRST (user order 2026-10-16: no tests, lint, format or build until the user asks)
UNTESTED_TASKS     = H-089, H-090, H-091, H-092 (STATIC_REVIEWED_UNTESTED), H-093, I-094, I-095, I-096, I-097, I-098, I-099, I-100 (IMPLEMENTED_UNTESTED), I-101, I-102, I-103, I-104, J-105, J-106, J-107, J-108, J-109, J-110, J-111, J-112, J-113, J-114, J-115, K-116, K-117, K-118, K-119, K-120, K-121, K-122, K-123, K-124, K-125, K-126, K-127, K-128, K-129, K-130, L-131, L-132, L-133, L-134, L-135, L-136, L-137, L-138, L-139, L-140, L-141, L-142, L-143, L-144 (STATIC_REVIEWED_UNTESTED), M-145, M-146, M-147, M-148, M-149, M-150 (IMPLEMENTED_UNTESTED, downgraded 2026-10-20 after API errors were found and fixed), M-151 (IMPLEMENTED_UNTESTED), M-152 (IMPLEMENTED_UNTESTED), M-153 (IMPLEMENTED_UNTESTED), M-154 (IMPLEMENTED_UNTESTED), M-155 (IMPLEMENTED_UNTESTED), M-156 (IMPLEMENTED_UNTESTED), M-157 (IMPLEMENTED_UNTESTED), N-158 (IMPLEMENTED_UNTESTED), N-159 (IMPLEMENTED_UNTESTED), N-160 (IMPLEMENTED_UNTESTED), N-161 (IMPLEMENTED_UNTESTED), N-162 (IMPLEMENTED_UNTESTED), N-163 (IMPLEMENTED_UNTESTED), N-164 (IMPLEMENTED_UNTESTED), N-165 (IMPLEMENTED_UNTESTED), N-166 (IMPLEMENTED_UNTESTED), N-167 (IMPLEMENTED_UNTESTED), N-168 (IMPLEMENTED_UNTESTED), N-169 (IMPLEMENTED_UNTESTED), O-170 (IMPLEMENTED_UNTESTED), O-171 (IMPLEMENTED_UNTESTED), O-172 (IMPLEMENTED_UNTESTED), O-173 (IMPLEMENTED_UNTESTED), O-174 (IMPLEMENTED_UNTESTED), O-175 (IMPLEMENTED_UNTESTED), O-176 (IMPLEMENTED_UNTESTED), O-177 (IMPLEMENTED_UNTESTED), O-178 (IMPLEMENTED_UNTESTED), O-179 (IMPLEMENTED_UNTESTED), O-180 (IMPLEMENTED_UNTESTED), O-181 (IMPLEMENTED_UNTESTED), P-182 (IMPLEMENTED_UNTESTED), P-183 (IMPLEMENTED_UNTESTED), P-184 (IMPLEMENTED_UNTESTED), P-185 (IMPLEMENTED_UNTESTED), P-186 (IMPLEMENTED_UNTESTED), P-187 (IMPLEMENTED_UNTESTED), P-188 (IMPLEMENTED_UNTESTED), P-189 (IMPLEMENTED_UNTESTED), P-190 (IMPLEMENTED_UNTESTED), Q-191 (IMPLEMENTED_UNTESTED), Q-192 (IMPLEMENTED_UNTESTED), Q-193 (IMPLEMENTED_UNTESTED), Q-194 (IMPLEMENTED_UNTESTED), Q-195 (IMPLEMENTED_UNTESTED), Q-196 (IMPLEMENTED_UNTESTED), Q-197 (IMPLEMENTED_UNTESTED), Q-198 (IMPLEMENTED_UNTESTED), Q-199 (IMPLEMENTED_UNTESTED), Q-200 (IMPLEMENTED_UNTESTED), Q-201 (IMPLEMENTED_UNTESTED), Q-202 (IMPLEMENTED_UNTESTED), Q-203 (IMPLEMENTED_UNTESTED), R-204 (IMPLEMENTED_UNTESTED), R-205 (IMPLEMENTED_UNTESTED), R-206 (IMPLEMENTED_UNTESTED), R-207 (IMPLEMENTED_UNTESTED), R-208 (IMPLEMENTED_UNTESTED), R-209 (IMPLEMENTED_UNTESTED)
NEXT_TASK          = R-210 Daily Stop
```

## Next task

**BUILD-FIRST batch (user order, 2026-10-16/18/19).** Up to 10 tasks per batch, no tests, lint, format or build, status `IMPLEMENTED_UNTESTED` or `STATIC_REVIEWED_UNTESTED`, never PASS.

L-141 Request Changes is STATIC_REVIEWED_UNTESTED (`ApprovalRequest.request_changes` in `content/approval.py`, `ApprovalService.request_changes` in `content/approval_service.py` returning item to `GENERATING`, `approval.changes_requested` audit, `POST /approvals/{id}/request-changes`).

L-142 Approval Expiry is STATIC_REVIEWED_UNTESTED (`Settings.approval_ttl_hours`, `ApprovalRequest.expires_at` / `is_expired` / `expire`, `ApprovalGate` optional `ttl` (`approval.expired`), `ApprovalService.expire_stale` and `revalidate`, `/approvals/by-item/{content_item_id}/expire` and `/approvals/{id}/revalidate`).

L-143 Approval Audit is STATIC_REVIEWED_UNTESTED (`content/approval_audit.py` `ApprovalAuditService` binding actor, timestamp, action, version, and reason, `/approvals/{id}/audit` and `/approvals/by-item/{content_item_id}/audit`).

L-144 Approval Tests is STATIC_REVIEWED_UNTESTED (`tests/test_approval.py`, `tests/test_approval_scenarios.py`).

M-145 OAuth Contract is IMPLEMENTED_UNTESTED (downgraded 2026-10-20, API errors fixed) (`providers/youtube_oauth.py` `OAuthScope`, `OAuthToken` with `SecretStr` tokens, `YouTubeOAuthProvider`, `providers/mock_youtube_oauth.py` `MockYouTubeOAuthProvider`).

M-146 Channel Provider is IMPLEMENTED_UNTESTED (downgraded 2026-10-20, API errors fixed) (`providers/youtube_channel.py` `RemoteChannelInfo`, `YouTubeChannelProvider`, `providers/mock_youtube_channel.py` `MockYouTubeChannelProvider`).

M-147 Upload Provider is IMPLEMENTED_UNTESTED (downgraded 2026-10-20, API errors fixed) (`providers/youtube_upload.py` `YouTubeUploadPayload`, `YouTubeUploadReceipt`, `YouTubeUploadProvider`, `providers/mock_youtube_upload.py` `MockYouTubeUploadProvider`).

M-148 Publish Metadata Mapper is IMPLEMENTED_UNTESTED (downgraded 2026-10-20, API errors fixed) (`pipeline/publish_metadata_mapper.py` `PublishMetadataMapper.map_to_payload`).

M-149 Shorts Publisher is IMPLEMENTED_UNTESTED (downgraded 2026-10-20, API errors fixed) (`pipeline/shorts_publisher.py` `ShortsPublisher.publish`).

M-150 LongForm Publisher is IMPLEMENTED_UNTESTED (downgraded 2026-10-20, API errors fixed) (`pipeline/longform_publisher.py` `LongFormPublisher.publish`).

M-151 Idempotency Key is IMPLEMENTED_UNTESTED. `pipeline/idempotency.py` adds `upload_key` (job key + YouTube channel id + video sha256, prefix `upl:`), `PublishKeys` / `derive_publish_keys`, and `is_publish_key` / `is_upload_key` shape checks; `publish_key` and the idempotency gate are unchanged.

M-152 Duplicate Guard is IMPLEMENTED_UNTESTED. `PublishDuplicateGuard` (replay / in-flight / already published / same video bytes / lost race), shared `PublishContextLoader` + `PublishExecutor` extracted from the two publishers, and corrections of the API errors in the M-149/M-150 publishers and the `ApprovalAuditService` wiring found by reading the real signatures.

M-153 Upload Retry is IMPLEMENTED_UNTESTED. `UploadRetryPolicy` / `call_with_retry` (exponential, bounded, only retryable errors), resumable failure handling in `PublishExecutor` (item stays `PUBLISHING`, same job restarted on the next call), `Settings.upload_max_attempts` and `upload_retry_base_delay_seconds`, and a multi-failure mock upload provider.

M-154 Token Recovery is IMPLEMENTED_UNTESTED. `TokenRecovery` and `PublishAuthorizationError`: safe renewal of expired tokens, a clear re-authorisation error for everything a refresh cannot fix, and executor integration at token acquisition, channel verification and upload.

M-155 Publish Result is IMPLEMENTED_UNTESTED. `confirm_receipt`, `PublishResultService` and read-only `/publish-results` endpoints; the executor refuses to record a receipt that does not match the approved payload (including the AI-disclosure flag).

M-156 Publish Audit is IMPLEMENTED_UNTESTED. `PublishAuditRecorder` / `PublishAuditService` and the audit events for the request, duplicate block, replay, gate block, token recovery, retries, result and failure of a publish, plus `GET /publish-audit/by-item/{id}`.

M-157 Publishing Tests is IMPLEMENTED_UNTESTED. End-to-end publishing scenarios with mock providers (success, failure, retry, resume, token recovery, duplicates, gates, preconditions), a rewritten mapper/loader test module and shared test helpers. Phase M is complete.

N-158 Analytics Provider is IMPLEMENTED_UNTESTED. `AnalyticsProvider` contract (`AnalyticsQuery`, `AnalyticsRow`, `AnalyticsPage`, `KNOWN_METRICS`, typed errors) and a seeded `MockAnalyticsProvider` that never invents data; also fixes the YouTube provider error classes, which could not be constructed (`cause=` passed to `ProviderError`).

N-159 Channel Metrics is IMPLEMENTED_UNTESTED. `ChannelMetricsSync`: idempotent, retrying sync of channel metrics into `MetricSnapshot`, keeping absent metrics absent and reporting data completeness.

N-160 Video Metrics is IMPLEMENTED_UNTESTED. `VideoMetricsSync`: chunked, paged, idempotent per-video metrics snapshots for the published videos of a channel, with videos of other channels refused and absent metrics kept absent.

N-161 Shorts Metrics is IMPLEMENTED_UNTESTED. `ShortsMetricsView`, `normalize_shorts_metrics` and `ShortsMetricsService` in `content/shorts_metrics.py`: normalises Shorts-specific (`engaged_views`, `swiped_away`, `shorts_feed_impressions`) and standard video metrics where available, keeping absent metrics `None` and refusing LongForm items.

N-162 LongForm Metrics is IMPLEMENTED_UNTESTED. `LongFormMetricsView`, `normalize_longform_metrics` and `LongFormMetricsService` in `content/longform_metrics.py`: normalises long-form video metrics and derived watch-time/interaction rates where available, keeping absent metrics `None` and refusing Shorts items.

N-163 Watch Time is IMPLEMENTED_UNTESTED. `WatchTimeUnit`, `WatchTimeMeasurement`, `normalize_watch_time_period`, `normalize_snapshot_watch_time`, `aggregate_watch_time` and `WatchTimeService` in `content/watch_time.py`: normalises watch-time units and reporting periods, refuses overlapping periods, and keeps absent watch time `None`.

N-164 Retention is IMPLEMENTED_UNTESTED. `RetentionView`, `extract_retention` and `RetentionSync` in `content/retention.py`: stores and extracts audience retention (`average_view_percentage`, `average_view_duration`) only when actually returned by the provider, never defaulting missing retention to `0`.

N-165 CTR is IMPLEMENTED_UNTESTED. `CTRView`, `extract_ctr` and `CTRSync` in `content/ctr.py`: stores and extracts `impressions_click_through_rate` and `impressions` only when actually returned by the provider, never defaulting missing CTR to `0` or synthesizing it from `views / impressions`.

N-166 Subscriber Change is IMPLEMENTED_UNTESTED. `SubscriberChangeView`, `extract_subscriber_change`, `aggregate_subscriber_changes` and `SubscriberChangeService` in `content/subscriber_change.py`: tracks gained, lost and net subscribers by period, keeping missing counts `None` and refusing overlapping periods.

N-167 Freshness Model is IMPLEMENTED_UNTESTED. `FreshnessStatus`, `AnalyticsFreshnessPolicy`, `SnapshotFreshness`, `evaluate_freshness` and `AnalyticsFreshnessService` in `content/analytics_freshness.py`: classifies snapshots as `fresh`, `lagging` or `stale` from source provenance, retrieval time, period and reporting lag.

N-168 Analytics Cache is IMPLEMENTED_UNTESTED. `AnalyticsCache`, `AnalyticsCacheOutcome`, `AnalyticsCacheInfo`, `AnalyticsCacheStats` and `analytics_cache_key` in `providers/analytics_cache.py`: caches and invalidates analytics pages safely with token verification on every hit, freshness-policy TTLs, and stale fallback on retryable errors.

N-169 Analytics Tests is IMPLEMENTED_UNTESTED. End-to-end Phase N analytics test scenarios in `tests/test_analytics_scenarios.py` covering pagination, missing fields, stale/lagging data, cache fallback and invalidation, and provider failures. Phase N is complete.

O-170 Revenue Provider is IMPLEMENTED_UNTESTED. `RevenueProvider` contract (`RevenueQuery`, `RevenueRow`, `RevenuePage`, `KNOWN_REVENUE_TYPES`, typed errors) in `providers/revenue.py` and seeded `MockRevenueProvider` in `providers/mock_revenue.py` keeping `ESTIMATED` and `FINAL` strictly separate and enforcing monetary OAuth scope; registered Phase N and O-170 services in `bootstrap.py`.

O-171 Estimated Revenue is IMPLEMENTED_UNTESTED. `EstimatedRevenueSync`, `ChannelEstimatedRevenueResult`, `VideoEstimatedRevenueResult`, and `EstimatedRecordOutcome` in `content/estimated_revenue.py`: syncs and queries `RevenueStage.ESTIMATED` revenue for channels and published videos strictly separated from `RevenueStage.FINAL`, keeping estimate revision history and never fabricating missing rows.

O-172 Final Revenue is IMPLEMENTED_UNTESTED. `FinalRevenueSync`, `FinalRevenueFreshness`, `ChannelFinalRevenueResult`, `VideoFinalRevenueResult`, and `RevenueStageComparison` in `content/final_revenue.py`: syncs and queries finalized revenue (`RevenueStage.FINAL`) with source and freshness tracking and side-by-side comparison against estimated revenue.

O-173 Revenue Sources is IMPLEMENTED_UNTESTED. `RevenueStreamRegistry`, `RevenueStreamSpec`, `RevenueSourceBreakdown`, `RevenueGoalProgress`, and `RevenueSourceService` in `content/revenue_sources.py`: formalises the 8 built-in revenue sources (`ads`, `shorts_ads`, `memberships`, `super_thanks`, `super_chat`, `sponsorships`, `affiliate`, `merchandise`) and extensible custom streams, with single-stage breakdowns and non-guaranteed goal progress.

O-174 Cost Ledger is IMPLEMENTED_UNTESTED. `CostLedger`, `CostLedgerEntry`, `CostLedgerSummary`, and `CostLedgerError` in `content/cost_ledger.py`: unified append-only ledger for all tracked contribution costs across categories, with idempotency by `(category, ref)`, budget currency guard, append-only corrections, and channel/item/period summaries.

O-175 LLM Cost is IMPLEMENTED_UNTESTED. `LLMModelRate`, `LLMPricingCatalog`, `LLMCostRecorder`, and `LLMCostError` in `content/llm_cost.py`: computes and records `CostCategory.LLM` usage costs into `CostLedger` from `GeneratedText` or explicit token counts.

O-176 TTS Cost is IMPLEMENTED_UNTESTED. `TTSCostService`, `TTSChannelCostSummary`, `compute_tts_amount`, and `TTSCostError` in `content/tts_cost.py`: connects `AudioCostRecorder` (`H-092`) and standalone `SynthesizedSpeech` voice generation costs to `CostLedger` (`O-174`), including append-only rate corrections.

O-177 Render Cost is IMPLEMENTED_UNTESTED. `RenderPricingPolicy`, `RenderCostRecorder`, and `RenderCostError` in `content/render_cost.py`: computes and records `CostCategory.RENDER` costs into `CostLedger` for rendered video artifacts and render telemetry.

O-178 Storage/API Cost is IMPLEMENTED_UNTESTED. `StoragePricingPolicy`, `ApiPricingPolicy`, `StorageApiCostRecorder`, and `StorageApiCostError` in `content/storage_api_cost.py`: computes and records `CostCategory.STORAGE` and `CostCategory.API` contribution costs into `CostLedger`.

O-179 Contribution Margin is IMPLEMENTED_UNTESTED. `ContributionMarginCalculator`, `ContributionMarginReport`, and `ContributionMarginError` in `content/contribution_margin.py`: calculates channel and per-item contribution margin (`revenue - tracked contribution costs`), keeping `ESTIMATED` and `FINAL` separate and explicitly labeling the metric as `contribution_margin` (`is_net_profit=False`, never net profit).

O-180 Budget Guard is IMPLEMENTED_UNTESTED. `BudgetGuard`, `BudgetGuardStatus`, `BudgetAlert`, `BudgetGuardError`, and `BudgetGuardBlockedError` in `content/budget_guard.py`: enforces daily and monthly budget limits and threshold alerts with pre-job projected cost support; registered all `O-171`..`O-180` services in `bootstrap.py`.

O-181 Economics Tests is IMPLEMENTED_UNTESTED. End-to-end Phase O economics test scenarios in `tests/test_economics_scenarios.py` covering currencies, periods, estimated/final revenue separation, multi-source revenue, contribution costs, contribution margin (`is_net_profit=False`), and budget guard blocking. Phase O is complete.

P-182 Comment Provider is IMPLEMENTED_UNTESTED. `CommentProvider` contract (`CommentQuery`, `RemoteComment`, `CommentPage`, `ReplyPostRequest`, `ReplyPostReceipt`, typed errors) in `providers/comments.py` and seeded `MockCommentProvider` in `providers/mock_comments.py` supporting pagination, reply threads, OAuth scope checks, and idempotent reply receipts.

P-183 Comment Sync is IMPLEMENTED_UNTESTED. `CommentSync`, `CommentSyncResult`, `SyncedCommentOutcome`, and `CommentSyncError` in `content/comment_sync.py`: syncs comments and replies for published videos across bounded pages with retry and disabled-comment handling, preserving idempotency by `youtube_comment_id`.

P-184 Comment Classification is IMPLEMENTED_UNTESTED. `CommentClassifier`, `evaluate_comment_rules`, `ClassificationDecision`, `CommentClassificationOutcome`, and `ChannelClassificationSummary` in `content/comment_classifier.py`: classifies comments for moderation, creator attention, and reply drafting while keeping classification history.

P-185 Reply Draft is IMPLEMENTED_UNTESTED. `ReplyDraftService`, `ReplyDraftOutcome`, and `ReplyDraftError` in `content/reply_draft.py`: generates AI and manual reply drafts in `ReplyStatus.DRAFT` without posting by default, enforcing moderation and banned-phrase checks.

P-186 Reply Approval is IMPLEMENTED_UNTESTED. `ReplyApprovalService`, `ReplyApprovalOutcome`, `ReplyPostOutcome`, `reply_post_idempotency_key`, and `ReplyApprovalError` in `content/reply_approval.py`: human approval, optional text edit, discard, and posting workflow for comment reply drafts.

P-187 Duplicate Reply Guard is IMPLEMENTED_UNTESTED. `DuplicateReplyGuard`, `ReplyDuplicateCheck`, `ReplyDuplicateDecision`, `DuplicateReplyError`, and `concurrent_reply_post` in `content/duplicate_reply_guard.py`: prevents duplicate reply posts on the same comment, in the same thread, or with identical normalized text on a video.

P-188 AUTO_REPLY Flag is IMPLEMENTED_UNTESTED. Added `AutoReplyGuard` and `AutoReplyService` in `src/ai_youtube_agent/content/auto_reply.py` plus unit tests in `tests/test_auto_reply.py`.

P-189 Comment Audit is IMPLEMENTED_UNTESTED. Added `CommentAuditService` in `src/ai_youtube_agent/content/comment_audit.py`, wired Phase P community services in `src/ai_youtube_agent/bootstrap.py`, and added unit tests in `tests/test_comment_audit.py` and `tests/test_bootstrap.py`.

P-190 Comment Tests is IMPLEMENTED_UNTESTED. Added Phase P end-to-end scenario tests in `tests/test_comment_scenarios.py`.

Q-191 Dashboard Shell is IMPLEMENTED_UNTESTED. `DashboardShellService`, `DashboardShellState`, `DesignSystemTokens`, and `render_dashboard_shell_html` in `content/dashboard_shell.py` plus `dashboard/design_system.css`: creates the Command Center navigation, channel switcher, live badge counts, and shared iOS-style design system.

Q-192 Home Screen is IMPLEMENTED_UNTESTED. Added `src/ai_youtube_agent/content/dashboard_home.py` (`ChannelHealthCard`, `AIStatusSummary`, `ProductionSummary`, `PendingApprovalSummaryItem`, `HomeAlertPreview`, `HomeScreenView`, `HomeScreenService`) and `tests/test_dashboard_home.py` to aggregate channel health, active/stopped AI session & job counts, Shorts/LongForm production counts across lifecycle stages, pending human approvals, and open warning/error alerts onto the Command Center Home screen.

Q-193 Channel Overview is IMPLEMENTED_UNTESTED. Added `src/ai_youtube_agent/content/dashboard_channel.py` (`ChannelIdentityCard`, `ChannelSubscribersSummary`, `ChannelViewsWatchTimeSummary`, `ChannelContentCounts`, `ChannelOverviewView`, `ChannelOverviewService`) and `tests/test_dashboard_channel.py` to present channel identity, strategy completeness, gained/lost/net subscribers, views, watch time in minutes/hours/seconds, and Shorts/LongForm content counts by lifecycle status.

Q-194 Content Screen is IMPLEMENTED_UNTESTED. Added `src/ai_youtube_agent/content/dashboard_content.py` (`ContentCardItem`, `LifecycleStageGroup`, `ContentTypeColumn`, `ContentScreenView`, `ContentScreenService`) and `tests/test_dashboard_content.py` to separate `ContentType.SHORTS` and `ContentType.LONGFORM` columns and group items across the ten shared `ContentStatus` lifecycle states.

Q-195 Video Detail is IMPLEMENTED_UNTESTED. Added `src/ai_youtube_agent/content/dashboard_video_detail.py` (`VideoDetailView`, `VideoDetailService`) and `tests/test_dashboard_video_detail.py` to present the real HTML5 preview player (`PreviewPlayerService`), exact `PreviewSnapshot` and all artifact versions/checksums, `PreviewMetadataView`, `PreviewReportsBundle` (QC, Rights, AI disclosure, Policy), `VersionComparisonResult`, approval history, and publish status.

Q-196 Analytics Screen is IMPLEMENTED_UNTESTED. Added `src/ai_youtube_agent/content/dashboard_analytics.py` (`TimeRangePeriodRow`, `ShortsAnalyticsSummary`, `LongFormAnalyticsSummary`, `AnalyticsScreenView`, `AnalyticsScreenService`) and `tests/test_dashboard_analytics.py` to present time-range channel metrics, freshness status, and side-by-side Shorts vs LongForm performance keeping absent metrics `None`.

Q-197 Revenue Screen is IMPLEMENTED_UNTESTED. Added `src/ai_youtube_agent/content/dashboard_revenue.py` (`RevenueScreenView`, `RevenueScreenService`) and `tests/test_dashboard_revenue.py` to present strictly separated `RevenueStage.ESTIMATED` and `RevenueStage.FINAL` revenue breakdowns, tracked contribution costs (`CostLedgerSummary`), and `ContributionMarginReport` with explicit `metric_label="contribution_margin"`, `is_net_profit=False`, and `NOT_NET_PROFIT_DISCLAIMER`.

Q-198 Comments Screen is IMPLEMENTED_UNTESTED. Added `src/ai_youtube_agent/content/dashboard_comments.py` (`ReplyDraftViewItem`, `CommentThreadViewItem`, `CommentsScreenView`, `CommentsScreenService`) and `tests/test_dashboard_comments.py` to present new/unclassified and attention-needed comments, reply drafts by status, `auto_reply_enabled` safety indicator (`False` by default), and human draft approval/discard delegation.

Q-199 AI Center is IMPLEMENTED_UNTESTED. Added `src/ai_youtube_agent/content/dashboard_ai_center.py` (`SessionStateView`, `AIJobViewItem`, `ProviderCostSummary`, `AICenterScreenView`, `AICenterScreenService`) and `tests/test_dashboard_ai_center.py` to present active/recent user session state, AI job status, checkpoint progress, retry counts, errors, configured providers, and tracked AI costs.

Q-200 Approval Center UI is IMPLEMENTED_UNTESTED. Added `src/ai_youtube_agent/content/dashboard_approvals.py` (`ApprovalQueueItem`, `ApprovalCenterView`, `ApprovalCenterService`) and `tests/test_dashboard_approvals.py`, and registered all Phase Q Command Center screen services (`Q-191`–`Q-200`) in `src/ai_youtube_agent/bootstrap.py` (`tests/test_bootstrap.py`), presenting pending and historical approval requests bound to exact artifact versions/SHA-256 checksums, staleness/expiry indicators, and human decision actions.

Q-201 Settings Screen is IMPLEMENTED_UNTESTED. Added `src/ai_youtube_agent/content/dashboard_settings.py` (`ChannelSettingsView`, `OAuthSettingsView`, `EmailSettingsView`, `ProvidersSettingsView`, `StrategyProfileView`, `LimitsSettingsView`, `FeatureFlagsView`, `SettingsScreenView`, `SettingsScreenService`) and `tests/test_dashboard_settings.py`, and registered `SettingsScreenService` in `src/ai_youtube_agent/bootstrap.py` (`tests/test_bootstrap.py`), exposing channel identity, OAuth authorization and scopes without leaking credentials, email approval TTL, engine providers, strategy blueprint, cadence/budget limits, and feature flags.

Q-202 Alerts is IMPLEMENTED_UNTESTED. Added `src/ai_youtube_agent/content/dashboard_alerts.py` (`ActionableAlertCard`, `AlertCategory`, `AlertSeverity`, `AlertsScreenView`, `AlertsScreenService`) and `tests/test_dashboard_alerts.py`, and registered `AlertsScreenService` in `src/ai_youtube_agent/bootstrap.py` (`tests/test_bootstrap.py`), implementing actionable alert cards with direct triage and remediation links across lifecycle failures, pending/stale human approvals, budget overruns, and system/provider health degradations.

Q-203 Dashboard Tests is IMPLEMENTED_UNTESTED. Added `tests/test_dashboard_scenarios.py` verifying mocked data rendering across all 11 Command Center screens, empty states when no channels exist, dynamic badge calculations across pending approvals, attention comments, and active jobs, error states, and XSS escaping. Phase Q is complete.

Next: **R-204 Job Queue**.

H-088 TTS Job has PASSED (pack #088; scope confirmed by the user on 2026-10-16 after a read-only survey). User decisions: one chunk per `ScriptSection`, a section over 5000 characters split at sentence ends, then whitespace, then a hard cut; chunk audio in a `ChunkStore` Protocol with `InMemoryChunkStore`; one merged PCM WAV (formats must match); reuse `AIJob` (kind `audio.generate`, key `audio_key(script id, voice profile id, version)` added to `pipeline/idempotency.py`), no migration; a stuck RUNNING job is recovered only by `run(..., take_over=True)` (waiting is always refused); no audit events; a succeeded rerun rebuilds from checkpoints and the store with no provider call or write (`domain.tts_result_unavailable` if chunks are gone); total duration is the sum of chunk durations. New `content/tts_job.py`: the provider name, item and channel are checked before anything is stored; scalar checkpoints `audio.planned`, `audio.chunk_done` (index, hashes, duration, format) and `audio.completed` (saved with `succeed`); `store.put` before the checkpoint; a chunk is reused only when its newest checkpoint, text hash, bytes hash and format match; a `SpeechSynthesisError` or `TtsJobError` fails the attempt with the code only and is re-raised (no retry, #091); other errors and `ConcurrencyError` leave the job running; no script text in checkpoints, `last_error` or errors. Routing: NORMAL (score 7) re-routed to COMPLEX (score 12, Opus trigger reasoning risk) on the planner's evidence; planner sonnet/medium, coder and reviewer opus/high. Review PASS: 0 blocking, 0 should-fix, 5 minor (permanent failure after a provider format change, no store fence on take-over, reused + synthesized counts can exceed chunks, the pinned clock can repeat `updated_at`, a raw `IntegrityError` on two concurrent first runs; no test for "no audit" or an exact 5000-character chunk). Tests: `tests/test_tts_job.py` 53 new; affected 900 passed (job, repositories, speech_synthesis, voice, script, idempotency_gate, gate_matrix, publishing_blocker, folder_structure); full suite RUN once at the checkpoint, 6069 passed (justified: checkpoint and a new shared pipeline key); migration chain NOT RUN (no schema change); ruff and `uv build` pass. Agent cost: about 323k tokens over 3 runs.

H-087 Voice Profile has PASSED (pack #087 with a spec the user pasted on 2026-10-05; scope confirmed after a read-only survey). User decisions: pronunciation is deferred as a scope deviation (no evidence of its shape, no column in `voice_profiles`, persistence and migration out of scope); Unicode category Cc characters are rejected in `voice_id` and `speaking_style`; a `speech_request(text)` bridge is added; `provider` uses `fullmatch` too. `content/voice.py` only: `voice_id` non-empty, no whitespace, at most `MAX_VOICE_ID` (200, imported from `providers/speech_synthesis.py`, no duplicated constant) and no Cc; `language` `LANGUAGE_TAG_PATTERN.fullmatch` (same set, a trailing newline rejected); `speaking_style` None or non-blank, at most `MAX_STYLE` (200) after the existing strip, no Cc; `_require_provider` (shared with `AudioMetadata`) uses `fullmatch`; error messages name the field and no longer echo the value; `VoiceProfile.speech_request(text) -> SpeechRequest` passes voice id, language and style unchanged, calls no provider; stored rows breaking the tighter rules now fail to load (no real data exists). H-086 contracts, providers, bootstrap, config, repositories and migrations unchanged. Routing: NORMAL (score 6), planner and coder sonnet/medium, reviewer off by policy; the main session self-reviewed with the spec's checklist: no blocking or should-fix; minor: the length and whitespace checks exist in both `VoiceProfile` and `SpeechRequest` (constants shared, the profile is stricter, so a valid profile always gives a valid request); `AudioMetadata.provider` is tightened as well (intended); old rows that break the rules fail on load. Tests: `tests/test_voice.py` 97 (42 new); affected `tests/test_speech_synthesis.py`, `tests/test_repositories.py` and `tests/test_brand_settings.py` 191 passed (every test module that touches the voice entities, directly or through `tests/factories.py`); full suite NOT RUN (a change confined to `content/voice.py` with no bootstrap, config, contract or schema change; 6016 tests collected); migration chain NOT RUN; ruff and `uv build` pass. Agent cost: about 102k tokens over 2 runs.

H-086 Voice Provider has PASSED (pack #086 with a spec the user pasted on 2026-10-04; scope confirmed after a read-only survey). User decisions: a synchronous `@runtime_checkable` Protocol like F-065's `TextGenerator`; one provider chosen by `Settings.voice_provider`; the result carries audio bytes plus metadata (storage is #089); a deterministic WAV sine mock; no extra duration cap (5000 characters give 300 s); sine with `round()` (no cross-platform golden digest); `characters = len(text)` as sent (billing normalisation is #092). New `providers/speech_synthesis.py`: frozen `SpeechRequest(text <= 5000 non-blank, voice_id <= 200 without whitespace, language BCP-47 via fullmatch, speaking_style None or <= 200)`, frozen `SynthesizedSpeech(audio bytes, media_type audio/* via MEDIA_TYPE_PATTERN.fullmatch, duration_ms 1..86,400,000, provider, voice_id, characters >= 0, generated_at UTC)`, `VoiceErrorCode` (`voice.unavailable`, `voice.rate_limited`, `voice.timeout` retryable; `voice.invalid_request`, `voice.voice_not_found`, `voice.refused` not), `SpeechSynthesisError(ProviderError)` with safe user messages, `SpeechSynthesizer` (name, synthesize, check); errors never hold the request text. New `providers/mock_speech_synthesis.py`: `MockSpeechSynthesizer(*, name="mock", clock=None)` returns a WAV PCM 16-bit mono 16 kHz sine at 200 + (first 4 bytes of sha256 of a canonical UTF-8 encoding of text, voice_id, language and style joined with NUL) % 601 Hz, 60 ms per character with a 500 ms minimum, amplitude 8000 (not silent); identical requests give identical bytes, different ones usually differ; `queue(audio, *, duration_ms, media_type)` (eager validation, corrupt bytes allowed), `fail_next`, `set_healthy`, `calls`, a lock. `core/config.py`: `VoiceProviderKind` (mock) and `Settings.voice_provider` (env `AI_YOUTUBE_AGENT_VOICE_PROVIDER`); `bootstrap.py`: `SpeechSynthesizer` registered (unknown kind raises), health check `voice_provider` after `text_provider`; docs in `.env.example`, `content/voice.py`, `docs/ARCHITECTURE.md`. No consumer, HTTP, storage, retry, new dependency or Phase G change. Routing: ARCHITECTURAL (score 13, provider-contract floor), planner, coder and reviewer opus/high with an architecture review: PASS, 0 blocking, 0 should-fix, 4 minor; one opus/high fix round fixed the overclaiming docstring and replaced `repr(request)` hashing with the canonical encoding (+3 tests); open: the `VoiceProfile` validation gap (for H-087) and an unvalidated empty mock `name` (as in the text mock). Tests: 93 in `tests/test_speech_synthesis.py`, 10 cases in `tests/test_config.py`, `tests/test_health_check.py` updated; affected 169 (config, health_check, bootstrap, health, voice) and regression 96 (di, hook_generator, research_provider) passed; one full suite at the checkpoint, 5974 passed, justified by `Settings`, the bootstrap and the health registry; migration chain NOT RUN; ruff and `uv build` pass. Agent cost: about 284k tokens over 4 runs.

Earlier task summaries (G-085 back to A-001, and the Phase A start note) were moved verbatim to `CHANGELOG.md`, section "Task summaries (moved from PROJECT_STATE.md)".

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
