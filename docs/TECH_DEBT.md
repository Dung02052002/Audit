# Technical Debt

Open technical debt and unverified risks, newest task last. Each entry names
the task that found it. Remove an entry when a task fixes it, and say which.

Since 2026-10-16 tasks are built in BUILD-FIRST mode (user order): no tests,
lint, format or build run during a batch. Every task marked
`IMPLEMENTED_UNTESTED` or `STATIC_REVIEWED_UNTESTED` in `TASK_STATUS.md` still
needs its tests run in the later test phase; those tests are listed per task in
`TASK_STATUS.md` and below.

## Pending verification (BUILD-FIRST batch from 2026-10-16)

| Task | Tests to run later |
|---|---|
| H-089 Audio Artifact | `tests/test_artifact_storage.py`, `tests/test_audio_artifact.py` (new); `tests/test_config.py`, `tests/test_bootstrap.py`, `tests/test_repositories.py`, `tests/test_version_invalidation.py` (additions); affected `tests/test_approval_gate.py`, `tests/test_approval.py`, `tests/test_tts_job.py`, `tests/test_voice.py`, `tests/test_health_check.py`, `tests/test_di.py`; full suite once (Settings, bootstrap and shared `core/version_invalidation.py` changed); ruff, format, `uv build` |
| H-090 Audio Validation | `tests/test_audio_validation.py` (new) |
| H-091 Audio Retry | `tests/test_audio_retry.py` (new); `tests/test_tts_job.py` (helper changes: `Flaky` default code, no-op sleep) |
| H-092 Audio Cost | `tests/test_audio_cost.py` (new); `tests/test_config.py`, `tests/test_bootstrap.py`, `tests/test_repositories.py` (additions); affected `tests/test_budget_gate.py` |
| H-093 Voice Tests | `tests/test_voice_scenarios.py` (new) |
| I-094 Shorts Project | `tests/test_shorts_config.py`, `tests/test_shorts_timeline.py`, `tests/test_shorts_project.py` (new); `tests/test_migrations.py` (LATEST 27, new table and enums), `tests/test_db_template.py`, `tests/test_bootstrap.py`; regression `tests/test_repositories.py`, `tests/test_market_settings.py`; migration chain once (0027) |
| I-095 Shorts Timeline | `tests/test_shorts_planner.py` (new), `tests/test_shorts_timeline.py`, `tests/test_shorts_project.py`; regression `tests/test_tts_job.py`, `tests/test_shorts_config.py` |
| I-096 Shorts Scene | `tests/test_shorts_scene.py` (new), `tests/test_shorts_timeline.py`, `tests/test_shorts_project.py`, `tests/test_shorts_planner.py`; regression `tests/test_repositories.py -k "asset or provenance"` |
| I-097 Shorts Captions | `tests/test_shorts_captions.py` (new; fragile first: `::test_caption_plans_and_saves_the_captions_of_every_scene`, `::test_decomposed_script_text_gives_composed_captions`), then all `tests/test_shorts_*.py` |
| I-098 Shorts Subtitles | `tests/test_shorts_subtitles.py` (new), `tests/test_artifact_storage.py`, `tests/test_bootstrap.py`; regression `tests/test_audio_artifact.py`, `tests/test_version_invalidation.py`, `tests/test_shorts_*.py` (shared `core/artifact_storage.py` changed) |
| I-099 Shorts Audio Mix | `tests/test_audio_mix.py`, `tests/test_shorts_audio_mix.py` (new), `tests/test_bootstrap.py`; regression all `tests/test_shorts_*.py`, `tests/test_audio_validation.py` (`shorts_service.py` refactored) |
| I-100 Shorts Render | `tests/test_video_render.py`, `tests/test_ffmpeg_video_render.py`, `tests/test_shorts_render.py` (new; fragile first: `::test_a_worker_that_was_taken_over_cannot_store_its_video`, `::test_captions_become_one_text_line_per_wrapped_line`, `::test_the_default_source_reads_generated_pictures`); `tests/test_config.py`, `tests/test_bootstrap.py`, `tests/test_health_check.py` (names and endpoint body gain `render_provider`), `tests/test_artifact_storage.py` (`MEDIA_EXTENSIONS` equality, `v1.mp4`), `tests/test_idempotency_gate.py` (`render_key`) (additions); regression all `tests/test_shorts_*.py`, `tests/test_health.py`, `tests/test_di.py`, `tests/test_speech_synthesis.py` (shared `shorts_service.py`, Settings, bootstrap and health registry changed); the real-ffmpeg tests skip without ffmpeg (and the caption one without a font file); full suite once (Settings, bootstrap, health registry, shared core) |
| I-101 Shorts Thumbnail | `tests/test_shorts_thumbnail.py` (new), `tests/test_artifact_storage.py` (`image/png`, `image/jpeg`, `image/webp` extensions), `tests/test_bootstrap.py`; regression `tests/test_version_invalidation.py`, `tests/test_shorts_render.py` |
| I-102 Shorts Metadata | `tests/test_shorts_metadata.py` (new), `tests/test_artifact_storage.py` (`application/json` extension), `tests/test_bootstrap.py`; regression `tests/test_disclosure_decider.py`, `tests/test_policy_check.py`, `tests/test_version_invalidation.py` |
| I-103 Shorts Artifact Bundle | `tests/test_shorts_bundle.py` (new), `tests/test_bootstrap.py`; regression `tests/test_rights_report.py`, `tests/test_policy_report.py`, `tests/test_shorts_render.py`, `tests/test_shorts_thumbnail.py`, `tests/test_shorts_metadata.py`, `tests/test_shorts_subtitles.py` |
| I-104 Shorts Render Tests | `tests/test_shorts_render_scenarios.py` (new); full `tests/test_shorts_*.py` suite |
| J-105 LongForm Project | `tests/test_longform_config.py`, `tests/test_longform_project.py` (new), `tests/test_migrations.py` (`LATEST = 28`, `longform_projects`), `tests/test_bootstrap.py`; migration chain once (0028) |
| J-106 LongForm Timeline | `tests/test_longform_timeline.py` (new), `tests/test_longform_project.py` |
| J-107 Chapters | `tests/test_longform_chapters.py` (new), `tests/test_longform_timeline.py`, `tests/test_longform_project.py` |
| J-108 B-roll Manager | `tests/test_broll_manager.py` (new), `tests/test_bootstrap.py`; regression `tests/test_longform_project.py`, `tests/test_rights_risk_engine.py` |
| J-109 LongForm Subtitles | `tests/test_longform_subtitles.py` (new), `tests/test_bootstrap.py`; regression `tests/test_shorts_subtitles.py`, `tests/test_version_invalidation.py` |
| J-110 LongForm Audio Mix | `tests/test_longform_audio_mix.py` (new), `tests/test_bootstrap.py`; regression `tests/test_audio_mix.py`, `tests/test_audio_validation.py`, `tests/test_shorts_audio_mix.py` |
| J-111 LongForm Render | `tests/test_longform_render_scenarios.py` (new), `tests/test_bootstrap.py`; regression `tests/test_longform_audio_mix.py`, `tests/test_video_render.py` |
| J-112 LongForm Thumbnail | `tests/test_longform_render_scenarios.py` (new), `tests/test_bootstrap.py`; regression `tests/test_shorts_thumbnail.py` |
| J-113 LongForm Metadata | `tests/test_longform_render_scenarios.py` (new), `tests/test_bootstrap.py`; regression `tests/test_shorts_metadata.py`, `tests/test_longform_chapters.py` |
| J-114 LongForm Artifact Bundle | `tests/test_longform_render_scenarios.py` (new), `tests/test_bootstrap.py`; regression `tests/test_shorts_bundle.py`, `tests/test_rights_report.py`, `tests/test_policy_report.py` |
| J-115 LongForm Render Tests | `tests/test_longform_render_scenarios.py` (new); full LongForm production suite |
| K-116 Test Harness | `tests/test_qc_video_checks.py` (new), `tests/test_bootstrap.py` |
| K-117 Container Test | `tests/test_qc_video_checks.py` (new) |
| K-118 Resolution Test | `tests/test_qc_video_checks.py` (new) |
| K-119 FPS Test | `tests/test_qc_video_checks.py` (new) |
| K-120 Duration Test | `tests/test_qc_video_checks.py` (new) |
| K-121 Audio Test | `tests/test_qc_video_checks.py` (additions); regression `tests/test_audio_validation.py` |
| K-122 Subtitle Test | `tests/test_qc_video_checks.py` (additions); regression `tests/test_shorts_subtitles.py`, `tests/test_longform_subtitles.py` |
| K-123 Black Frame Test | `tests/test_qc_video_checks.py` (additions) |
| K-124 Render Integrity Test | `tests/test_qc_video_checks.py` (additions); regression `tests/test_video_render.py` |
| K-125 Metadata Test | `tests/test_qc_video_checks.py` (additions); regression `tests/test_shorts_metadata.py`, `tests/test_longform_metadata.py` |
| K-126 Rights QC | `tests/test_qc_rights.py` (new), `tests/test_bootstrap.py`; regression `tests/test_rights_report.py`, `tests/test_rights_gate.py` |
| K-127 Policy QC | `tests/test_qc_policy.py` (new), `tests/test_bootstrap.py`; regression `tests/test_policy_report.py`, `tests/test_policy_gate.py` |
| K-128 AI Disclosure QC | `tests/test_qc_disclosure.py` (new), `tests/test_bootstrap.py`; regression `tests/test_disclosure_decider.py`, `tests/test_disclosure_rule.py` |
| K-129 QC Report | `tests/test_qc_report.py` (new), `tests/test_bootstrap.py`; regression `tests/test_gate_matrix.py`, `tests/test_gates.py`, `tests/test_qc.py` |
| K-130 QC Regression Suite | `tests/test_qc_regression_suite.py` (new); full Phase K QC suite (`tests/test_qc_*.py`) |
| L-131 Preview Artifact API | `tests/test_preview_api.py` (new), `tests/test_bootstrap.py` |
| L-132 Preview Player | `tests/test_preview_api.py` (new), `tests/test_artifact_storage.py` (`text/vtt` extension), `tests/test_bootstrap.py` |
| L-133 Preview Metadata | `tests/test_preview_api.py` (new), `tests/test_bootstrap.py` |
| L-134 Preview Reports | `tests/test_preview_api.py` (new), `tests/test_bootstrap.py`; regression `tests/test_rights_report.py`, `tests/test_policy_report.py` |
| L-135 Version Comparison | `tests/test_preview_api.py` (new), `tests/test_bootstrap.py` |
| L-136 Preview Edit Loop | `tests/test_preview_edit_loop.py` (new), `tests/test_bootstrap.py`; regression `tests/test_version_invalidation.py`, `tests/test_qc_report.py` |
| L-137 Approval Request | `tests/test_approval_workflow.py` (new), `tests/test_bootstrap.py`; regression `tests/test_approval.py`, `tests/test_approval_gate.py` |
| L-138 Email Approval | `tests/test_approval_workflow.py` (new), `tests/test_bootstrap.py` |
| L-139 Approve Action | `tests/test_approval.py` (updated decision method list + approve tests), `tests/test_approval_workflow.py` (new); regression `tests/test_approval_gate.py` |
| L-140 Reject Action | `tests/test_approval.py` (updated decision method list + reject tests), `tests/test_approval_workflow.py` (new) |
| L-141 Request Changes | `tests/test_approval.py` (decision method list + `request_changes` tests), `tests/test_approval_scenarios.py` (new) |
| L-142 Approval Expiry | `tests/test_approval.py` (`expires_at`, `is_expired`, `expire` tests), `tests/test_approval_scenarios.py` (new), `tests/test_config.py` (`approval_ttl_hours`); regression `tests/test_approval_gate.py`, `tests/test_gate_matrix.py`, `tests/test_publish_gate.py` |
| L-143 Approval Audit | `tests/test_approval_scenarios.py` (new), `tests/test_bootstrap.py` |
| L-144 Approval Tests | `tests/test_approval.py`, `tests/test_approval_workflow.py`, `tests/test_approval_scenarios.py` |
| M-145 OAuth Contract | `tests/test_youtube_providers.py` (new), `tests/test_bootstrap.py` |
| M-146 Channel Provider | `tests/test_youtube_providers.py` (new), `tests/test_bootstrap.py` |
| M-147 Upload Provider | `tests/test_youtube_providers.py` (new), `tests/test_bootstrap.py` |
| M-148 Publish Metadata Mapper | `tests/test_publishers.py` (new), `tests/test_bootstrap.py` |
| M-149 Shorts Publisher | `tests/test_publishers.py` (new), `tests/test_bootstrap.py`; regression `tests/test_publish_gate.py`, `tests/test_idempotency_gate.py`, `tests/test_daily_limit.py` |
| M-150 LongForm Publisher | `tests/test_publishers.py` (new), `tests/test_bootstrap.py`; regression `tests/test_publish_gate.py`, `tests/test_idempotency_gate.py`, `tests/test_daily_limit.py` |
| M-151 Idempotency Key | `tests/test_publish_keys.py` (new); regression `tests/test_idempotency_gate.py` |
| M-152 Duplicate Guard | `tests/test_duplicate_guard.py` (new), `tests/publish_helpers.py` (new helper), `tests/test_publishers.py` (rewrite pending in M-157), `tests/test_bootstrap.py`; regression `tests/test_idempotency_gate.py`, `tests/test_repositories.py` |
| M-153 Upload Retry | `tests/test_upload_retry.py` (new); `tests/test_config.py`, `tests/test_bootstrap.py`; executor-level retry scenarios are in M-157 `tests/test_publish_scenarios.py` |
| M-154 Token Recovery | `tests/test_token_recovery.py` (new); executor-level token scenarios are in M-157 `tests/test_publish_scenarios.py`; regression `tests/test_youtube_providers.py` |
| M-155 Publish Result | `tests/test_publish_result.py` (new), `tests/test_bootstrap.py`; the app route tests (`tests/test_health.py`) for the new router |
| M-156 Publish Audit | `tests/test_publish_audit.py` (new), `tests/test_bootstrap.py`; regression `tests/test_approval_scenarios.py` |
| M-157 Publishing Tests | `tests/test_publish_scenarios.py` (new), `tests/test_publishers.py` (rewritten), `tests/publish_helpers.py` (helper); then the whole Phase M test set (`tests/test_youtube_providers.py`, `tests/test_publish_keys.py`, `tests/test_duplicate_guard.py`, `tests/test_upload_retry.py`, `tests/test_token_recovery.py`, `tests/test_publish_result.py`, `tests/test_publish_audit.py`) |
| N-158 Analytics Provider | `tests/test_analytics_provider.py` (new), `tests/test_bootstrap.py`, `tests/test_youtube_providers.py` (error classes) |
| N-159 Channel Metrics | `tests/test_channel_metrics.py` (new), `tests/test_bootstrap.py`; regression `tests/test_repositories.py -k metric` |
| N-160 Video Metrics | `tests/test_video_metrics.py` (new), `tests/test_channel_metrics.py` (shared helpers moved), `tests/test_bootstrap.py` |
| N-161 Shorts Metrics | `tests/test_shorts_metrics.py` (new); regression `tests/test_analytics_provider.py`, `tests/test_repositories.py` |
| N-162 LongForm Metrics | `tests/test_longform_metrics.py` (new) |
| N-163 Watch Time | `tests/test_watch_time.py` (new) |
| N-164 Retention | `tests/test_retention.py` (new) |
| N-165 CTR | `tests/test_ctr.py` (new) |
| N-166 Subscriber Change | `tests/test_subscriber_change.py` (new) |
| N-167 Freshness Model | `tests/test_analytics_freshness.py` (new) |
| N-168 Analytics Cache | `tests/test_analytics_cache.py` (new) |
| N-169 Analytics Tests | `tests/test_analytics_scenarios.py` (new); then the whole Phase N test set (`tests/test_analytics_provider.py`, `tests/test_channel_metrics.py`, `tests/test_video_metrics.py`, `tests/test_shorts_metrics.py`, `tests/test_longform_metrics.py`, `tests/test_watch_time.py`, `tests/test_retention.py`, `tests/test_ctr.py`, `tests/test_subscriber_change.py`, `tests/test_analytics_freshness.py`, `tests/test_analytics_cache.py`) |
| O-170 Revenue Provider | `tests/test_revenue_provider.py` (new), `tests/test_bootstrap.py` |
| O-171 Estimated Revenue | `tests/test_estimated_revenue.py` (new) |
| O-172 Final Revenue | `tests/test_final_revenue.py` (new) |
| O-173 Revenue Sources | `tests/test_revenue_sources.py` (new) |
| O-174 Cost Ledger | `tests/test_cost_ledger.py` (new) |
| O-175 LLM Cost | `tests/test_llm_cost.py` (new) |
| O-176 TTS Cost | `tests/test_tts_cost.py` (new) |
| O-177 Render Cost | `tests/test_render_cost.py` (new) |
| O-178 Storage/API Cost | `tests/test_storage_api_cost.py` (new) |
| O-179 Contribution Margin | `tests/test_contribution_margin.py` (new) |
| O-180 Budget Guard | `tests/test_budget_guard.py` (new), `tests/test_bootstrap.py` |
| O-181 Economics Tests | `tests/test_economics_scenarios.py` (new); then the whole Phase O test set (`tests/test_revenue_provider.py`, `tests/test_estimated_revenue.py`, `tests/test_final_revenue.py`, `tests/test_revenue_sources.py`, `tests/test_cost_ledger.py`, `tests/test_llm_cost.py`, `tests/test_tts_cost.py`, `tests/test_render_cost.py`, `tests/test_storage_api_cost.py`, `tests/test_contribution_margin.py`, `tests/test_budget_guard.py`) |
| P-182 Comment Provider | `tests/test_comment_provider.py` (new) |
| P-183 Comment Sync | `tests/test_comment_sync.py` (new) |
| P-184 Comment Classification | `tests/test_comment_classifier.py` (new) |
| P-185 Reply Draft | `tests/test_reply_draft.py` (new) |
| P-186 Reply Approval | `tests/test_reply_approval.py` (new) |
| P-187 Duplicate Reply Guard | `tests/test_duplicate_reply_guard.py` (new) |
| P-188 AUTO_REPLY Flag | `pytest tests/test_auto_reply.py` |
| P-189 Comment Audit | `pytest tests/test_comment_audit.py tests/test_bootstrap.py` |
| P-190 Comment Tests | `pytest tests/test_comment_scenarios.py` |
| Q-191 Dashboard Shell | `tests/test_dashboard_shell.py` (new); regression `tests/test_repositories.py` |
| Q-192 Home Screen | `tests/test_dashboard_home.py` |
| Q-193 Channel Overview | `tests/test_dashboard_channel.py` |
| Q-194 Content Screen | `tests/test_dashboard_content.py` |
| Q-195 Video Detail | `tests/test_dashboard_video_detail.py` |
| Q-196 Analytics Screen | `tests/test_dashboard_analytics.py` |
| Q-197 Revenue Screen | `tests/test_dashboard_revenue.py` |
| Q-198 Comments Screen | `tests/test_dashboard_comments.py` |
| Q-199 AI Center | `tests/test_dashboard_ai_center.py` |
| Q-200 Approval Center UI | `tests/test_dashboard_approvals.py`, `tests/test_bootstrap.py` |
| Q-201 Settings Screen | `tests/test_dashboard_settings.py`, `tests/test_bootstrap.py` |
| Q-202 Alerts | `tests/test_dashboard_alerts.py`, `tests/test_bootstrap.py` |
| Whole batch | full suite once (Settings, bootstrap, shared core changed); `ruff check .`, `ruff format --check .`, `uv build`; then commit per task |

## Open items

### H-088 TTS Job

- A job whose provider changes WAV format between attempts stays failed: the
  reused first chunk fixes the format. Recovery needs a store clear or a voice
  version bump. Possible fix: drop reused chunks when the first new chunk's
  format differs.
- `take_over` has no fence on the `ChunkStore`: a late `put` from a stale
  worker can spoil a later rebuild (`domain.tts_result_unavailable`). Fix with a
  durable chunk store keyed by attempt (deferred by H-089).
- `reused_chunks + synthesized_chunks` can exceed `chunks` when a chunk
  disappears between the two loops.
- The clock pinned to `max(now, job.updated_at)` can repeat `updated_at`,
  weakening the `expected_updated_at` guard.
- Two concurrent first runs raise a raw `sqlite3.IntegrityError` (unique
  idempotency key) instead of a domain error.
- Missing tests: "no audit events written"; a chunk of exactly 5000 characters.

### H-089 Audio Artifact

- `ArtifactStorage` is bytes-in-memory only. Large renders (#102, #113) need a
  file- or stream-based `write`, and ffmpeg steps (#099 audio mix, render) need
  a local path. Plan the Protocol extension before C8.
- The file write and fsync run while `BEGIN IMMEDIATE` holds the SQLite write
  lock; a large long-form WAV on a slow disk can push other writers past the
  busy timeout.
- A replay does not check that the latest version's file still exists or still
  matches its sha256.
- Re-recording an older result after a newer version creates a new version
  that rolls the audio back and invalidates approvals (by design: only the
  latest version is compared).
- DB backup and restore do not cover the artifact files; one `artifact_dir`
  per database is a documented rule, not enforced.
- Windows reserved device names (`CON`, `COM1`) pass the content item id
  pattern (real ids are uuid hex).
- The H-089 review fix round (UNC host rule, cleanup catching `Exception`,
  audio-only media types, full voice comparison) was not re-reviewed.
- If something raises after COMMIT but before the transaction block exits, the
  committed file is deleted and the row points at nothing (theoretical).

### H-092 Audio Cost

- Retried or failed provider calls are not billed: the cost is
  `TtsResult.characters` (the script text) once per artifact, not
  `provider_calls`. A real provider that charges per call needs a cost from the
  calls actually made.
- `CostRecordRepository.find_by_ref` filters on `category` and `ref`, and
  `cost_records` has no index on `ref`; it scans the table. Add an index in a
  migration when the ledger (#174) grows.
- The audio artifact and its cost are two commits. A crash between them leaves
  an artifact without a cost until `AudioCostRecorder.record` is called again
  (safe on a `created=False` replay).
- A price change after the cost was recorded is not applied to the existing
  cost; a correction is a new record (ledger, #174).
- The TTS currency is a setting; a channel whose budget currency differs gets
  `domain.audio_cost_currency_mismatch` until the setting or the budget is
  changed.
- `AudioCostRecorder` checks the currency with `CURRENCY_PATTERN.match`, so a
  trailing newline (`"USD
"`) passes; use `fullmatch` (the `Settings` field
  pattern is the first line of defence).

### H-090 Audio Validation

- Samples are decoded into Python lists; a file near the 512 MiB limit uses a
  lot of memory and time. Stream in windows if long-form audio gets large.
- The thresholds (`AudioLimits`, method `audio-rules-v1`) are heuristics and
  may give false WARNs; WARN never blocks.

### H-093 Voice Tests

- The different-voice scenario relies on the mock tone frequency differing for
  two voices (about 0.17% collision chance per pair).

### I-094 Shorts Project

- `save_timeline` does not refuse a stale project (script or audio no longer
  the latest); callers must check `is_current` and `rebind`.
- `rebind` always resets the timeline to draft and bumps the version, even
  when the inputs did not change.
- `create` and `rebind` treat audio whose `AudioMetadata.script_id` is not the
  latest script as `stale_input` (how D2 is enforced).
- `ShortsProjectError` keeps a per-code HTTP status (unlike
  `AudioArtifactError`).
- Tests written without running: `test_a_concurrent_create_is_reported_as_existing`
  monkeypatches the repository; two `World` fixtures share one database in
  `test_list_by_channel_*`; `World.add_script` uses a real clock.

### I-095 Shorts Timeline

- The 5% tolerance between the chunk-duration sum and the merged audio, the
  200-character scene title and the section index limit of 1000 are coder
  choices, not settled with the user.
- `tests/test_shorts_planner.py` imports helpers from `test_shorts_project`;
  this needs `tests/` on `sys.path` (as `factories` already does).
- A raw UPDATE inside `pytest.raises` in the SQL check test may leave the
  transaction in an unexpected state.

### I-096 Shorts Scene

- `plan()` rebuilds the timeline without assets, so planning again drops the
  chosen backgrounds and music.
- A timeline saved with a provenance snapshot is refused after the asset's
  provenance changes until the caller clears the id (by design, 409).
- Assets are checked when the timeline is saved, not again before render;
  #100/#103 must re-check (the rights gate stays authoritative at publish).
- New test fixtures (`Asset.create`, `AssetRepository.add`, `Provenance.create`)
  were written without running; ruff may flag long lines and import order.

### I-097 Shorts Captions

- The D3 defaults conflict: a full 32-character line at the default size 64 px
  is 1229 px wide, wider than the 885 px safe width at 1080p. `plan_captions`
  shrinks the font per caption (46 px at 1080p, 30 px at 720p); a hand-built
  caption at 64 px with a long line is refused as unsafe. Revisit the default
  size or line length with the user.
- The caption box is an estimate (0.6 x size per character, 1.2 x size per
  line), not a font measurement; the renderer (#100) may differ.
- `plan_captions` needs exactly one scene per script section.
- `CaptionOverlay` refuses text that is not NFC instead of normalising it.

### I-098 Shorts Subtitles

- Only SRT is stored; `render_vtt` exists but has no artifact. One
  `SUBTITLES` artifact kind holds one format per version.
- The stale-input check is a copy of `ShortsProjectService.is_current` inside
  the recorder's transaction; keep the two in step.
- The approval-invalidation test does not assert the content item reset.

### I-099 Shorts Audio Mix

- D-STORE (main-session decision, confirmed by the user on 2026-10-18 for
  I-100): the mix is not stored; the render step calls the mixer again. A
  stored mix needs a new artifact kind (an `artifacts.kind` CHECK change, a
  table rebuild) or its own table.
- Only generated assets (with `artifact_id`) have readable bytes; uploaded or
  licensed music and SFX give `asset_unavailable` until assets get a file
  reference.
- The engine loops per sample in Python: a 3-minute stereo 44.1 kHz mix may
  take seconds to tens of seconds.
- Level normalisation is RMS over voiced windows, not BS.1770 LUFS; after
  16-bit rounding the peak may exceed the ceiling by 1/32768.
- Constraints are module defaults, not part of the `ShortsConfig` snapshot,
  so a project does not record the constraints it was mixed with (the audit
  event does).

### I-100 Shorts Render

- The ffmpeg renderer, its filter graph and command line are untested against
  a real ffmpeg (the real-ffmpeg tests skip when the program is missing);
  `check` only runs `ffmpeg -version`, so a build without `libx264`, `aac` or
  the `drawtext` filter (freetype) passes the health check and fails at
  render time (`render.failed`).
- `-filter_complex_script` is deprecated in ffmpeg 7 and later (replaced by
  `-/filter_complex`); it still works but may be removed.
- A video clip shorter than its layer window keeps its last frame
  (`eof_action=repeat`) and the audio of a video clip is dropped (only the mix
  is mapped). Adjacent `between(t,S,E)` windows share the frame at exactly
  `E`, so two captions may show together for one frame.
- Captions are placed from the I-097 estimate (0.6 x size per character, 1.2 x
  size per line), not from glyph metrics; the drawn text may be wider or
  narrower than the checked safe-area box.
- Without `Settings.render_font_file` (and without a font asset) drawtext uses
  the fontconfig default, which differs between machines: the output is not
  reproducible. The configured font file is not part of the input sha256.
- The render key covers the renderer name and `RENDER_KEY_VERSION`, not the
  ffmpeg version or the encoder settings; bump `RENDER_KEY_VERSION` when the
  graph or the command changes, or an old video is replayed.
- D6: every call, replays included, runs the mixer (seconds for a long mix)
  and records an `audio.mixed` event.
- D2: there is no "rendered" project status; callers derive it from the
  latest VIDEO artifact and its `video.render` job.
- No render cost is recorded (#177).
- A replay does not check that the MP4 file of the latest artifact still
  exists or still matches its sha256 (as H-089).
- Only generated visual assets (with an `artifact_id`) have readable files
  through `ArtifactVisualSource`, and `LocalFileStorage` stores only WAV, SRT
  and MP4, so images cannot be stored as artifacts yet; uploaded or licensed
  pictures, clips and fonts give `asset_unavailable`. *(Note: I-101 added
  `image/png`, `image/jpeg`, `image/webp` and I-102 added `application/json`
  to `LocalFileStorage.MEDIA_EXTENSIONS`; external assets without an
  `artifact_id` still require a custom `VisualSource`.)*
- A RUNNING job left by a crashed worker needs `take_over=True`; there is no
  cancellation besides the ffmpeg timeout.
- The whole MP4 and every input are held in memory, and the file is written
  while the SQLite write lock is held (extends the H-089 item).
- The take-over fence relies on the job's `updated_at`; with a fixed clock
  (tests) the guard cannot tell two saves apart (as H-088).
- `ShortsRenderService` builds `RenderRequest` input keys `v1..` per scene
  asset, so an asset used in several scenes is sent several times.
- The `build_render_request` fallback font (`font_fallback`) is not used by the
  service; the ffmpeg renderer copies `Settings.render_font_file` itself.
- Two concurrent first renders of the same inputs raise a raw
  `sqlite3.IntegrityError` (unique idempotency key), as H-088.

### I-101 Shorts Thumbnail

- **Task:** `I-101 Shorts Thumbnail`
- **Issue:** `generate_thumbnail_png` produces a deterministic geometric
  placeholder PNG (solid background + frame + accent bar) rather than extracting
  a rendered video frame via `ffmpeg` or rendering title typography onto the
  canvas.
- **Potential impact:** Generated thumbnails are structurally valid (9:16,
  >= 640x640, <= 2 MiB) for pipeline and QC tests, but real channels will want
  either a selected `AssetKind.IMAGE` asset (`select`) or FFmpeg frame extraction.
- **Verification phase:** Phase K (`#117–#129` QC checks) and full test run after
  BUILD-FIRST batch.
- **Priority:** Medium.

### I-102 Shorts Metadata

- **Task:** `I-102 Shorts Metadata`
- **Issue:** `ShortsMetadata` is stored as a JSON `ArtifactKind.METADATA` file in
  `ArtifactStorage` without a dedicated SQLite metadata table, so `ShortsBundleBuilder`
  reads and parses the JSON artifact file to verify `project_version`,
  `timeline_sha256` and `disclosure_decision_id`.
- **Potential impact:** Extra small file read during bundle verification and
  publish preparation (#149); acceptable because metadata files are a few
  kilobytes.
- **Verification phase:** Phase K (`#125 Metadata Check`, `#128 Disclosure QC`)
  and Phase M (`#149 Shorts Publisher`).
- **Priority:** Low.

### I-103 Shorts Artifact Bundle

- **Task:** `I-103 Shorts Artifact Bundle`
- **Issue:** `ShortsBundleBuilder.build` computes the bundle on demand and
  verifies the latest `video.render` `AIJob` via a SQL query on `ai_jobs` and
  `job_checkpoints` (`step = 'video.rendered'`) rather than storing a dedicated
  bundle table.
- **Potential impact:** If a newer failed `video.render` job exists after a
  succeeded render for the same item, the query orders by `updated_at DESC`
  among `status = 'succeeded'` jobs and checks that the checkpoint's
  `artifact_id` matches the latest `VIDEO` artifact; must be verified on SQLite
  during the test run.
- **Verification phase:** Post-batch pytest run (`tests/test_shorts_bundle.py`,
  `tests/test_shorts_render_scenarios.py`) and `#116 Test Runner`.
- **Priority:** Medium.

### I-104 Shorts Render Tests

- **Task:** `I-104 Shorts Render Tests`
- **Issue:** `tests/test_shorts_render_scenarios.py` was written in BUILD-FIRST
  mode without executing `pytest`.
- **Potential impact:** Fixture setup or assertion details could fail on first
  execution.
- **Verification phase:** Immediate post-BUILD-FIRST test execution phase.
- **Priority:** High (run before next git checkpoint).

### J-105 LongForm Project & J-106 LongForm Timeline & J-107 Chapters

- **Task:** `J-105`, `J-106`, `J-107`
- **Issue:** Migration `0028_longform_projects.sql` and `LongFormProjectService`
  were written in BUILD-FIRST mode without running the migration test suite
  (`tests/test_migrations.py`). Also, `plan_longform_timeline` automatically
  populates `ChapterMarker`s when `config.chapters` is `True`, which requires at
  least 2 `SectionKind.CHAPTER` sections (so total markers >= 3 including the
  `00:00` intro marker) and >= 10s chapter spacing; a LongForm script with
  `config.chapters=True` whose chapter sections are shorter than 10s will fail
  `plan()` with `domain.longform_timeline_invalid`.
- **Potential impact:** Short test scripts with `chapters=True` must allocate at
  least 10s per chapter scene (or set `chapters=False` in `FormatSpec`).
- **Verification phase:** Post-BUILD-FIRST test run (`tests/test_migrations.py`,
  `tests/test_longform_project.py`, `tests/test_longform_timeline.py`,
  `tests/test_longform_chapters.py`).
- **Priority:** High (migration 0028 verification).

### J-108 B-roll Manager

- **Task:** `J-108 B-roll Manager`
- **Issue:** `BrollManager` checks `RightsRecordRepository.list_by_content_item`
  for unresolved `HIGH`/`UNKNOWN` records, whereas `Settings.rights_block_levels`
  can optionally include `MEDIUM`. Currently `BLOCKING_RISK_LEVELS` in
  `broll_manager.py` is fixed to `{HIGH, UNKNOWN}`.
- **Potential impact:** An asset with an unresolved `MEDIUM` rights record can
  be assigned as B-roll even if `Settings.rights_block_levels` includes `MEDIUM`
  (though `RightsGate` and `PublishGate` will still block at publish time).
- **Verification phase:** `#114 LongForm Artifact Bundle` and `#126 Rights QC`.
- **Priority:** Low.

### J-109 LongForm Subtitles & J-110 LongForm Audio Mix

- **Task:** `J-109`, `J-110`
- **Issue:** `LongFormSubtitleRecorder` allows recording either SRT or WebVTT under
  `ArtifactKind.SUBTITLES`; switching format from SRT to VTT creates a new
  `SUBTITLES` version because the bytes differ. `LongFormAudioMixer` uses the
  pure-Python per-sample loop in `content/audio_mix.py`, which can take several
  seconds on multi-minute LongForm audio tracks.
- **Potential impact:** Performance of `LongFormAudioMixer.mix` on 10–60 minute
  LongForm tracks (#110/#111).
- **Verification phase:** `J-111 LongForm Render`, `J-115 LongForm Render Tests`,
  and post-BUILD-FIRST test run.
- **Priority:** Medium.

### K-121..K-125 Video Artifact Checks (`qc_video_checks.py`)

- **Task:** `K-121 Audio Test`, `K-122 Subtitle Test`, `K-123 Black Frame Test`,
  `K-124 Render Integrity Test`, `K-125 Metadata Test`
- **Issue:** `check_black_frames` (`K-123`) estimates black and frozen frame
  ratios from raw MP4 `mdat` payload byte luminance/block variance when explicit
  frame telemetry (`black_frame_ratio` / `frozen_frame_ratio`) is not supplied
  on `VideoInspectionContext`. While deterministic for `stub_mp4` and raw frame
  buffers, compressed H.264 `mdat` bitstreams from real `ffmpeg` renders do not
  map byte values 1:1 to Y-luma samples without decoding frames via `ffmpeg` /
  `ffprobe`. Also, `VideoArtifactTestHarness.default_video_checks()` keeps the
  4 foundational checks (`K-117..K-120`) for backward compatibility while
  `all_video_checks()` (used by `QCReportBuilder`) runs all 9 `video.*` checks
  (`K-117..K-125`).
- **Potential impact:** On real compressed H.264 outputs, high-entropy `mdat`
  bytes will not falsely trigger black/frozen frame failures, whereas a truly
  black H.264 stream may compress to small repeating NAL blocks (`frozen_ratio`
  heuristic) unless `ffmpeg` `blackdetect`/`freezedetect` filter telemetry is
  passed into `VideoInspectionContext`.
- **Verification phase:** Post-BUILD-FIRST pytest run (`tests/test_qc_video_checks.py`)
  and real-FFmpeg pilot (`#226`, `#227`).
- **Priority:** Medium.

### K-126..K-130 Compliance QC, QC Report & Regression Suite

- **Task:** `K-126 Rights QC`, `K-127 Policy QC`, `K-128 AI Disclosure QC`,
  `K-129 QC Report`, `K-130 QC Regression Suite`
- **Issue:** `TestGate` (`GateName.TEST`) and `QCGate` (`GateName.QC`) are
  implemented in `content/qc_report.py` as `PipelineGate` classes guarding transitions
  to `PREVIEW_READY`, `AWAITING_APPROVAL`, `APPROVED`, and `PUBLISHING`, and are
  deliberately NOT added to `GUARDS` in `tests/test_gate_matrix.py` (which
  pins `set(GateName) - set(GUARDS) == {GateName.TEST, GateName.QC}`) or to
  `PublishGate` (whose gate order is pinned in `tests/test_publishing_blocker.py`);
  wiring `TestGate` and `QCGate` into the stage runner belongs to `#206 Pipeline Runner`.
- **Potential impact:** None for existing gate tests; `#206 Pipeline Runner`
  must include `TestGate` and `QCGate` when advancing items past `TESTING`.
- **Verification phase:** Post-BUILD-FIRST pytest run (`tests/test_qc_rights.py`,
  `tests/test_qc_policy.py`, `tests/test_qc_disclosure.py`, `tests/test_qc_report.py`,
  `tests/test_qc_regression_suite.py`) and `#206 Pipeline Runner`.
- **Priority:** Medium.

### L-131..L-136 Preview API, Player, Metadata, Reports, Comparison & Edit Loop

- **Task:** `L-131`..`L-136`
- **Issue:** `PreviewArtifactService.stream_artifact` reads the full artifact file
  into memory via `ArtifactStorage.read(uri)` to verify `size_bytes` and `sha256`
  before slicing a `Range: bytes=start-end` payload. For large LongForm MP4 files,
  repeated range requests during HTML5 video scrubbing will re-read and re-hash
  the file on each request until `ArtifactStorage` gains streaming/ranged read
  support (noted under `H-089`). Also, `PreviewEditLoopService.run_edit_loop`
  produces a deterministic re-rendered MP4 (`stub_mp4`) when `rerender_video=True`
  and `rendered_video_bytes=None` if a full `ShortsProject`/`LongFormProject`
  render pipeline is not invoked directly.
- **Potential impact:** Memory/CPU overhead when scrubbing multi-hundred-MB
  LongForm videos over `/previews/{id}/artifacts/{artifact_id}/stream`.
- **Verification phase:** Post-BUILD-FIRST pytest run (`tests/test_preview_api.py`,
  `tests/test_preview_edit_loop.py`) and `#144 Approval Tests`.
- **Priority:** Medium.

### L-137..L-140 Approval Request, Email Approval, Approve & Reject Actions

- **Task:** `L-137 Approval Request`, `L-138 Email Approval`, `L-139 Approve Action`,
  `L-140 Reject Action`
- **Issue:** To preserve `0001_initial_schema.sql` (`approval_requests` table
  columns) and `ApprovalRequestRepository` without altering existing migrations,
  `ApprovalRequest` keeps its 7 frozen fields while the decision actor, rejection
  reason, and approval note are recorded in the immutable `AuditLog` (`approval.approved`,
  `approval.rejected`) and returned in `ApprovalDecisionResult`. Dedicated
  approval audit query helpers and history views belong to `L-143 Approval Audit`.
  Also, `ApprovalEmailProvider` uses `MockApprovalEmailProvider` (in-memory outbox)
  by default so no real external emails are sent during development or automated
  runs; real SMTP/API email transport belongs to `#228 Email Approval Pilot`.
- **Potential impact:** `ApprovalRequest.as_dict()` shows `status` (`approved` /
  `rejected`), while the rejection `reason` and `decided_by` actor are carried
  on `ApprovalDecisionResult` and `audit_events` until `L-143 Approval Audit`.
- **Verification phase:** `L-141 Request Changes`, `L-142 Approval Expiry`,
  `L-143 Approval Audit`, `L-144 Approval Tests`, and post-BUILD-FIRST pytest run
  (`tests/test_approval.py`, `tests/test_approval_workflow.py`).
- **Priority:** Low.

### L-141..L-144 Request Changes, Approval Expiry & Approval Audit

- **Task:** `L-141 Request Changes`, `L-142 Approval Expiry`, `L-143 Approval Audit`,
  `L-144 Approval Tests`
- **Issue:** `ApprovalGate` accepts an optional `ttl: timedelta | None = None`
  parameter (defaulting to `None` to preserve backward compatibility with existing
  `tests/test_approval_gate.py`, `tests/test_gate_matrix.py`, and
  `tests/test_publish_gate.py` fixtures that advance the evaluation clock by +1
  to +30 days). `ApprovalService` enforces `Settings.approval_ttl_hours` (default
  72h) automatically during `approve()`, `expire_stale()`, and `revalidate()`.
  Also, in `TEST` environment `AuditSink` is `InMemoryAuditSink`, so
  `ApprovalAuditService` reads from `InMemoryAuditSink.events` when present and
  falls back to SQLite `audit_events` in non-test environments.
- **Potential impact:** `PublishGate` does not pass `ttl` to `ApprovalGate`
  unless explicitly configured, relying on `ApprovalService.expire_stale` or
  explicit `ApprovalGate(approvals, artifacts, ttl=...)` when TTL enforcement at
  publish-gate evaluation time is desired.
- **Verification phase:** Post-BUILD-FIRST pytest run (`tests/test_approval.py`,
  `tests/test_approval_scenarios.py`).
- **Priority:** Low.

### M-145..M-150 YouTube Providers, Metadata Mapper & Publishers

- **Task:** `M-145 OAuth Contract`, `M-146 Channel Provider`, `M-147 Upload Provider`,
  `M-148 Publish Metadata Mapper`, `M-149 Shorts Publisher`, `M-150 LongForm Publisher`
- **Issue:** `YouTubeOAuthProvider`, `YouTubeChannelProvider`, and
  `YouTubeUploadProvider` are wired to deterministic in-memory mocks
  (`MockYouTubeOAuthProvider`, `MockYouTubeChannelProvider`,
  `MockYouTubeUploadProvider`) in `bootstrap.py` so no real YouTube network
  calls or video uploads ever happen during development or tests. Dedicated
  upload-level idempotency key helpers, duplicate upload guards, retry policy,
  and manual/scheduled publish modes are deferred to `M-151`..`M-156`.
- **Potential impact:** If a failed publish attempt transitions `ContentItem`
  from `PUBLISHING -> FAILED`, `ContentItem` must be moved back to `APPROVED`
  before calling `ShortsPublisher.publish` / `LongFormPublisher.publish` again
   until `M-153 Publish Retry` adds automated retry orchestration.
- **Verification phase:** `M-151`..`M-156` and post-BUILD-FIRST pytest run
  (`tests/test_youtube_providers.py`, `tests/test_publishers.py`).
- **Priority:** Low.

### M-151 Idempotency Key

Task numbering note: earlier handoff text named M-152..M-156 as Duplicate Upload Guard, Publish Retry, Manual/Scheduled Publish, Publish Result and Publish Tests. That was a guess. The pack catalog in `docs/REQUIREMENTS.md` is 152 Duplicate Guard, 153 Upload Retry, 154 Token Recovery, 155 Publish Result, 156 Publish Audit, 157 Publishing Tests; Phase M ends at 157, and 158 Analytics Provider starts Phase N.

### M-152 Duplicate Guard

- A job left `IN_PROGRESS` by a crashed worker blocks the item with `IN_FLIGHT` forever: there is no lease or stale-takeover yet (compare `SourceCollector` leases, #062). A later task should add one before real publishing is enabled.
- Replays return the stored `PublishJob` result only; the `YouTubeUploadReceipt` details (privacy, thumbnail/captions flags) are in the audit event, not in a table.
- Review finding, not yet verified by any run: before this batch, `ShortsPublisher`/`LongFormPublisher` could not have been imported (`ArtifactNotFoundError`). Nothing in M-145..M-150 has ever been executed, so more mistakes of this kind are likely until the deferred full test run.

### M-153 Upload Retry

- A failure after which the item stays `PUBLISHING` has no timeout or automatic resume: someone must call publish again. A scheduler for resumes belongs to the pipeline runner (#206/#208).
- The retry waits use a blocking `time.sleep` inside the publish call; a long LongForm upload with the maximum settings (5 attempts, 60 s base) can block a worker for minutes. Revisit when the runner is asynchronous.
- The mock `upload` counter `calls` counts calls that passed local media validation, not every call.

### M-154 Token Recovery

- `MockYouTubeOAuthProvider` mints a fresh token on every refresh and never expires the refresh grant by itself, so the real behaviour of Google refresh grants (7-day expiry of unverified apps in testing mode, rotation) is not exercised. Revisit with the real provider.
- `TokenRecovery.renew` is allowed once per call site per publish call; a flapping authorisation would fail the publish as resumable each time and needs a human, by design.

### M-155 Publish Result

- The receipt details with no column (privacy status, disclosure flag, thumbnail/captions uploaded) exist only in the audit event; a table would need a migration (not added: migration tests pin the schema version and cannot be run in this batch).
- A crash between a successful provider upload and the database commit leaves a job `IN_PROGRESS` while the video exists on YouTube. A resume re-sends the same upload key, which a provider that stores keys answers with the existing receipt (the mock does); the real provider (a later task) must do the same or look the video up before uploading again.

### M-156 Publish Audit

- The audit trail reads the in-memory sink in the TEST environment and `SqliteAuditSink` otherwise; `AuditSink.events()` loads every event, so the trail is linear in the size of the log. Add an indexed query when the log grows.
- `publish.requested` is recorded when a publish is asked for, before the guard: a request that fails early (flags are checked first and write nothing; a not-found item has no context) leaves no event. Only requests that reached the item are audited.

### N-158 Analytics Provider

- The mock has no time dimension: the same seeded values answer every period, so it cannot exercise watch-time periods (#163), per-day series or subscriber gains by period (#166) beyond what a test seeds per call.
- `KNOWN_METRICS` is a closed list chosen from the YouTube Analytics and Reporting metrics; the real provider (not built) must map them to API names (`views`, `estimatedMinutesWatched`, `averageViewDuration`, `averageViewPercentage`, `subscribersGained`, `subscribersLost`, `likes`, `comments`, `shares`; impressions and click-through rate come from the Reporting API, not the Analytics API) and say which are unavailable.
- Review finding of this batch: the provider error classes of M-145..M-147 could not be built (`cause=`), so no provider test of those tasks could have passed. This is the second class of mistake found by reading APIs (after the M-149/M-150 storage calls); the first real test run is expected to find more.

### N-159 Channel Metrics

- OAuth errors (expired or revoked analytics authorisation) are raised unchanged; there is no renewal like `TokenRecovery` for analytics. Reusing `TokenRecovery` needs a neutral name (it raises `PublishAuthorizationError`).
- `call_with_retry` and `UploadRetryPolicy` live in `pipeline/upload_retry.py` and are reused for analytics reads with a hard-coded default (3 attempts, 2 s base). A neutral `retry` module and an analytics setting would be cleaner.
- The channel snapshot subject is the internal `Channel.id`; the YouTube channel id is not stored on the snapshot. Per-video snapshots (#160) use the YouTube video id.
- The sync is not scheduled and has no HTTP endpoint; the pipeline runner (#206) or a scheduler must call it.

### N-160 Video Metrics

- The per-video snapshot subject is the YouTube video id, not the internal content item id: relating metrics to a content item goes through the publish result (`PublishResultService`).
- A video published at the very start of the period still gets a whole-period snapshot; per-day series and "since publication" windows are not modelled.
- Videos are listed from `PublishResultService.list_for_channel`, which loads every published item of the channel; a channel with many thousands of videos needs an indexed query.
- The batch ends here: the next task is N-161 Shorts Metrics, then N-162 LongForm Metrics.

### O-180 Budget Guard

- `BudgetGuard` evaluates `day_window` and `month_window` in `strategy_time_zone(strategy)` and raises `BudgetGuardBlockedError` when `enforce` is called; wiring `BudgetGuard.enforce` before each provider-calling pipeline step belongs to the pipeline runner (`#206`).

### P-182..P-190 Comments & Community

- `CommentProvider` is wired to `MockCommentProvider` in `bootstrap.py` so no real YouTube Data API v3 comment calls or replies are made during development or tests.
- `AUTO_REPLY_ENABLED` defaults to `False` in `FeatureFlags`; `AutoReplyGuard` and `AutoReplyService` refuse automated reply posting with `AutoReplyDisabledError` (`comment.auto_reply_blocked` audit event) unless `flags.auto_reply_enabled` is explicitly `True`, and even when enabled still block `MODERATION`-classified comments.
- `CommentAuditService` queries `AuditSink.events()` linearly (matching `ApprovalAuditService` and `PublishAuditService`); add an indexed SQLite query if audit volume grows large.
