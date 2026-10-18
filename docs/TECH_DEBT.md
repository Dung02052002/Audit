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
| Whole batch | full suite once (Settings, bootstrap, shared core changed); migration chain once (I-094 adds 0027); `ruff check .`, `ruff format --check .`, `uv build`; then commit per task |

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
  pictures, clips and fonts give `asset_unavailable`.
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
