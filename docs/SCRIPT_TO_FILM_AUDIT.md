# Script-to-Film Engine — Phase 1 Capability Audit (`Code của Dũng`)

**Audit Date:** 2026-10-21  
**Baseline Commit:** `7a14362` (`main`)

---

## 1. Executive Summary

Prior to the Script-to-Film Engine refactor, the repository contained strong individual domain building blocks across **C6 (Script & Voice)**, **C7 (Assets & Compliance)**, **C8 (Shorts Production)**, **C9 (LongForm Production)**, **C10 (Quality / QC)**, and **C11 (Preview & Approval)**, as well as real PCM WAV audio mixing and an FFmpeg/Mock MP4 video renderer.

However, **an end-to-end Script-to-Video workflow did not exist as an integrated pipeline**:
1. **No Unified Entry Point / Orchestrator (`MISSING`):** Turning a script into a rendered MP4 required callers to manually invoke ~15 separate services in exact sequence.
2. **No Raw Script Analysis, Story Bible, Character/Location Extraction, or Shot Planning (`MISSING`):** `Script` only held pre-split `ScriptSection(kind, text, title, seconds)` records; there was no parser or analyzer for raw scripts/screenplays, no `StoryBible`, no separation of narrator vs. character dialogue, and no `ScenePlan` / `ShotPlan` (`wide_shot`, `medium_shot`, `close_up`, `insert_shot`, camera motion).
3. **No Visual Generation Provider Abstraction (`MISSING`):** Neither `ImageGenerator` (for `STORY_VIDEO` illustrated scenes) nor `VideoClipGenerator` (for `AI_FILM` shot clips) existed in `src/ai_youtube_agent/providers/`.
4. **Blocking Bugs in Existing LongForm Render & B-Roll Modules (`PARTIAL`):**
   - `src/ai_youtube_agent/content/longform_render.py` called non-existent attributes/kwargs (`mix.audio` instead of `mix.wav`, `VisualLayer(box=..., opacity=...)` instead of `rect=...`, `TextLine(box=..., font_size=..., text_colour=...)` instead of `rect=..., size_px=..., color=...`, wrong argument order on `ArtifactStorage.write`, and invalid positional args on `Artifact.create` and `invalidate_stale_approvals`).
   - `src/ai_youtube_agent/content/broll_manager.py` imported a non-existent `CropMode` from `shorts_timeline.py` (where the enum is `AssetFit`), passed `crop=` instead of `fit=` to `SceneAsset`, and passed `gain_db=0.0` for visual `B_ROLL` / `BACKGROUND` roles (which `SceneAsset.__post_init__` rejects because `gain_db` is only allowed on audio roles).
   - `src/ai_youtube_agent/content/qc_report.py` referenced `ContentStatus.READY_FOR_REVIEW` in `TestGate` and `QCGate`, whereas `ContentStatus` defines `ContentStatus.PREVIEW_READY`.
   - `src/ai_youtube_agent/bootstrap.py` did not wire `TtsJobRunner` or `InMemoryChunkStore`.

---

## 2. Module-by-Module Audit Classification

| Area / Capability | Key Modules | Status Before Refactor | Concrete Findings |
| :--- | :--- | :--- | :--- |
| **Script Ingestion & Raw Parsing** | `content/script.py`, `content/script_validator.py`, `content/shorts_script_generator.py`, `content/longform_script_generator.py` | `PARTIAL` | `Script` and `ScriptSection` validate structured sections (`HOOK`, `BODY`, `CTA`, `INTRO`, `CHAPTER`, `CONCLUSION`, `OUTRO`), but there is no raw text / screenplay / story-file parser that converts user-supplied scripts into structured sections while preserving plot and dialogue. |
| **Script Analysis, Story Bible, Character & Location Extraction** | *(none prior to refactor)* | `MISSING` | No `StoryBible`, `CharacterSpec`, `LocationSpec`, `StoryArc`, `ContinuityConstraint`, or dialogue vs. narrator segmentation existed. |
| **Scene Breakdown & Shot Planning** | `content/shorts_planner.py`, `content/longform_planner.py`, `content/shorts_timeline.py`, `content/longform_timeline.py` | `PARTIAL` | `plan_shorts_timeline` and `plan_longform_timeline` map `ScriptSection`s 1:1 to `ShortsScene` / `LongFormScene` with proportional timing, but lack cinematic `ScenePlan` metadata (characters, location, action, narrative purpose, continuity) and `ShotPlan` breakdown (`wide_shot`, `medium_shot`, `close_up`, `insert_shot`, camera motion). |
| **Shorts Production** | `content/shorts_project.py`, `content/shorts_service.py`, `content/shorts_captions.py`, `content/shorts_subtitles.py`, `content/shorts_audio_mix.py`, `content/shorts_render.py`, `content/shorts_thumbnail.py`, `content/shorts_metadata.py`, `content/shorts_bundle.py` | `IMPLEMENTED` | Full end-to-end Shorts domain services exist: 9:16 `1080x1920` timeline, caption layout, SRT export, 16-bit PCM WAV mixer with ducking/fade/normalization, MP4 render service, PNG thumbnail generator/selector, metadata recorder, and bundle builder. |
| **LongForm Production** | `content/longform_project.py`, `content/longform_service.py`, `content/longform_captions.py`, `content/longform_subtitles.py`, `content/longform_render.py`, `content/longform_thumbnail.py`, `content/longform_metadata.py`, `content/longform_bundle.py` | `PARTIAL` | Project, timeline, chapters, captions, subtitles, thumbnail, metadata, and bundle are implemented, **but `LongFormRenderService` had field/signature mismatches** with `VisualLayer`, `TextLine`, `MixOutcome`, `ArtifactStorage.write`, and `Artifact.create` that prevented execution until fixed in Phase 7. |
| **B-Roll & Asset Management** | `content/asset.py`, `content/asset_registry.py`, `content/provenance.py`, `content/provenance_recorder.py`, `content/rights_risk_engine.py`, `content/disclosure_decider.py`, `content/broll_manager.py` | `PARTIAL` | `AssetRegistry`, `ProvenanceRecorder`, `RightsRiskEngine`, and `DisclosureDecider` are `IMPLEMENTED`. `BrollManager` had broken `CropMode` / `gain_db` references that prevented attaching visual assets until fixed in Phase 5/7. |
| **Voice / TTS & Audio Mixing** | `providers/tts.py`, `providers/mock_tts.py`, `content/tts_job.py`, `content/audio_artifact.py`, `content/audio_validation.py`, `content/shorts_audio_mix.py` | `IMPLEMENTED` (with `MOCK_ONLY` default TTS provider) | `MockTTSProvider` synthesizes valid 24 kHz 16-bit mono PCM WAV audio with audible pitched sine/harmonic tone + envelope (not silent bytes). `TtsJobRunner` splits scripts into chunks, caches/persists chunks, concatenates WAVs, and supports checkpoint resume. `mix_shorts_audio` mixes voice + music + SFX with ducking and peak normalization. |
| **Image & Video Clip Generation Providers** | *(none prior to refactor)* | `MISSING` | No `ImageGenerator` or `VideoClipGenerator` protocol or provider existed. Added in Phase 5 (`providers/image_generation.py`, `providers/mock_image_generation.py`, `providers/video_generation.py`, `providers/mock_video_generation.py`). |
| **FFmpeg / Video Render Provider** | `providers/video_render.py`, `providers/ffmpeg_video_render.py` | `IMPLEMENTED` | `FfmpegVideoRenderer` invokes real `ffmpeg` CLI (`libx264` + `aac` + `scale`/`crop`/`drawbox`/`drawtext` + `+faststart`) when `ffmpeg` is on `PATH`. `MockVideoRenderer` builds a structurally valid ISO-BMFF MP4 container (`ftyp` + `moov`/`mvhd`/`trak`/`tkhd` + `mdat`) with real duration, dimensions, and non-black frame payload hashes when `ffmpeg` is absent. |
| **Subtitle, Thumbnail & Artifact Bundle** | `content/shorts_subtitles.py`, `content/longform_subtitles.py`, `content/shorts_thumbnail.py`, `content/longform_thumbnail.py`, `content/shorts_bundle.py`, `content/longform_bundle.py` | `IMPLEMENTED` | Generates valid UTF-8 SRT/VTT files, valid 8-bit RGB PNG thumbnails (`1080x1920` for Shorts, `1280x720` for LongForm), YouTube metadata JSON with chapters/disclosure/attributions, and immutable `ShortsBundle` / `LongFormBundle` descriptors. |
| **Pipeline Orchestrator, Persistence & Job Recovery** | `pipeline/job.py`, `core/db/repositories/jobs.py` | `PARTIAL` | `AIJob` and `JobCheckpoint` persistence exist in SQLite, but no orchestrator existed to run and resume the full script-to-video pipeline across stages. |
| **Preview, QC & Approval** | `content/qc_report.py`, `content/qc_video_checks.py`, `content/preview_artifact.py`, `content/approval_service.py` | `IMPLEMENTED` | 12-check `QCReportBuilder` (9 video checks + 3 compliance checks), byte-range `PreviewArtifactService`, and human-gated `ApprovalService` are implemented (`TestGate`/`QCGate` enum typo fixed in Phase 9). |
| **Configuration, Credentials & Provider Availability** | `core/config.py`, `core/flags.py`, `bootstrap.py` | `PARTIAL` | Settings and feature flags exist, but provider capability inspection (`STORY_VIDEO` vs `AI_FILM` readiness) was not exposed. |

---

## 3. Specific Verification Questions Answered

1. **Can the current system take a raw script and produce an MP4 automatically?**
   - **Before refactor:** No. There was no orchestrator, no raw script analyzer, no visual asset generator for scenes without pre-registered assets, and `LongFormRenderService` had runtime attribute errors.
   - **After refactor:** Yes, via `ScriptToVideoService.run(ScriptToVideoRequest(...))` and `POST /script-to-video/runs`.
2. **Does `VideoRenderer` create a real MP4 file or only metadata?**
   - Both `FfmpegVideoRenderer` (when `ffmpeg` binary is installed) and `MockVideoRenderer` (deterministic fallback) produce actual MP4 binary bytes written to `ArtifactStorage` (`ArtifactKind.VIDEO`, `video/mp4`) with valid ISO-BMFF `ftyp`, `moov`/`mvhd`, `trak`/`tkhd`, and `mdat` boxes that pass `probe_mp4` and all 9 `qc_video_checks`.
3. **Does `TTSProvider` synthesize real audio or only mock?**
   - `MockTTSProvider` synthesizes a real, valid RIFF/WAVE 16-bit mono PCM audio payload (`audio/wav`) with non-silent harmonic waveform samples whose duration scales with character count and speech rate, passing `audio_validation.analyze` (no clipping, no excessive silence, valid RMS/peak levels).
4. **How are scene visuals (`BACKGROUND`, `B_ROLL`) obtained?**
   - Previously, callers had to manually create files on disk, register them in `AssetRegistry`, attach them, record provenance, run rights assessment, and edit the timeline.
   - Now, `ScriptToVideoService` supports both user-supplied assets and automatic scene/shot illustration via `ImageGenerator` (`STORY_VIDEO` mode) or `VideoClipGenerator` (`AI_FILM` mode, when configured), automatically registering `Asset`, `AssetUsage`, `Provenance`, `RightsRecord`, and `DisclosureRecord`.
5. **Are Shorts and LongForm sharing a unified pipeline or separate?**
   - They share `ContentItem`, `Script`, `Artifact`, `Asset`, `Provenance`, `RightsRecord`, `DisclosureRecord`, `QCReportBuilder`, `PreviewArtifactService`, and `ApprovalService`, while using dedicated timeline/project services (`ShortsProjectService` vs `LongFormProjectService`). `ScriptToVideoService` unifies both under a single entry point and shared render layer helpers.
