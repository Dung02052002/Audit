# Script-to-Film & Script-to-Video Architecture

## 1. End-to-End Pipeline Flow

```
Input Script (raw text or file path)
  │
  ▼
[Stage 1: ScriptAnalyzer.analyze] ──► Checkpoint: script.parsed, story_bible.built
  │  - Normalizes script text (NFC, dialogue/narration tags, scene headings)
  │  - Extracts StoryBible: CharacterSpec, LocationSpec, StoryArc, ContinuityConstraint
  │  - Builds ScriptSection tuple & RawSceneBeat tuple aligned with target format
  ▼
[Stage 2: Domain Script + TtsJobRunner + AudioArtifactRecorder + AudioValidator]
  │  - Persists versioned Script entity (ContentStatus: IDEA -> SCRIPTING -> SCRIPTED)
  │  - Synthesizes WAV voiceover via SpeechSynthesizer
  │  - Stores ArtifactKind.AUDIO + AudioMetadata + AudioValidator QC check
  │  - Checkpoint: voice.generated (ContentStatus -> VOICED)
  ▼
[Stage 3: Project Timeline + SceneBreakdownPlanner.plan]
  │  - Creates/rebinds ShortsProject (9:16) or LongFormProject (16:9)
  │  - Plans timeline scenes locked to exact audio duration_ms
  │  - Builds SceneBreakdownPlan & ShotPlan (ShotType, CameraMotion, visual prompts)
  │  - Checkpoint: scenes.planned
  ▼
[Stage 4: Visual Asset Generation / Reuse + Provenance + Rights + AI Disclosure]
  │  - STORY_VIDEO mode: ImageGenerator (DeterministicSceneImageGenerator / external provider)
  │  - AI_FILM mode: VideoClipGenerator (honestly refuses when unconfigured; never fakes film)
  │  - Reuses existing attached scene assets on resume (idempotent per scene)
  │  - Registers Asset + Provenance + AssetUsage, runs RightsRiskEngine.assess & DisclosureDecider.decide
  │  - Checkpoint: assets.ready
  ▼
[Stage 5: Timeline Assembly, Subtitles, Thumbnail, Metadata & MP4 Rendering]
  │  - Assigns scene backgrounds (save_timeline / BrollManager.assign)
  │  - Generates LongForm chapters (generate_chapters) and timed captions (caption)
  │  - Checkpoint: timeline.built
  │  - Records SRT Subtitles (ShortsSubtitleRecorder / LongFormSubtitleRecorder) -> subtitles.generated
  │  - Generates 9:16 or 16:9 PNG Thumbnail -> thumbnail.generated
  │  - Records policy-checked Metadata ->ArtifactKind.METADATA
  │  - Mixes audio (ShortsAudioMixer / LongFormAudioMixer) & renders MP4 via VideoRenderer
  │  - Verifies deliverable bundle (ShortsBundleBuilder / LongFormBundleBuilder)
  │  - Checkpoints: audio.mixed, video.rendered (ContentStatus -> RENDERED)
  ▼
[Stage 6: QC Report, Preview Snapshot & Optional Approval Request]
  │  - Runs QCReportBuilder.generate (9 video checks K-117..K-125 + Rights + Policy + Disclosure)
  │  - Checkpoint: qc.completed
  │  - Transitions ContentItem to PREVIEW_READY (when QC passes/warns) & loads PreviewSnapshot
  │  - Checkpoint: preview.ready
  │  - If request_approval=True and QC passed, calls ApprovalService.request_approval
  │  - Checkpoint: approval.requested (ContentStatus -> AWAITING_APPROVAL)
  ▼
VideoProjectManifest (with real MP4 file path, SHA-256, StoryBible, SceneBreakdownPlan, QC & Preview)
```

---

## 2. Production Modes (`ProductionMode`)

| Mode | Visual Provider | Asset Kind | Behavior |
|------|----------------|------------|----------|
| `STORY_VIDEO` (`story_video`) | `ImageGenerator` (`providers/image_generation.py`) | `AssetKind.IMAGE` (`image/png`) | Default mode. Generates scene-consistent 8-bit RGB illustrations (using location color palettes, character continuity sheets, and shot composition) and renders them with timed voiceover, captions, and camera framing into a playable MP4. |
| `AI_FILM` (`ai_film`) | `VideoClipGenerator` (`providers/video_generation.py`) | `AssetKind.VIDEO_CLIP` (`video/mp4`) | Motion video mode. Requires an active `VideoClipGenerator` (`capabilities().available == True`). By default, `UnconfiguredVideoClipGenerator` reports `available=False` and `ScriptToVideoService` raises `ScriptToVideoErrorCode.MODE_UNAVAILABLE` (HTTP 409) with a clear diagnostic message rather than faking an AI film from static images. |

---

## 3. Core Modules & Responsibilities

1. **`src/ai_youtube_agent/content/story_bible.py`**:
   - `ProductionMode`, `SpeakerKind`, `CharacterSpec`, `LocationSpec`, `StoryArc`, `DialogueSegment`, `ContinuityConstraint`, `RawSceneBeat`, `StoryBible`, `ScriptAnalysisOutput`, `ScriptAnalyzer`.
   - Parses dialogue (`Speaker: text`, `[Speaker] (emotion): text`), scene headings (`SCENE 1`, `CẢNH 1`, `INT./EXT.`), and third-person narration while preserving original story order.
2. **`src/ai_youtube_agent/content/scene_planner.py`**:
   - `ShotType`, `CameraMotion`, `SceneStatus`, `ShotPlan`, `ScenePlan`, `SceneBreakdownPlan`, `SceneBreakdownPlanner`.
   - Plans 1–3 shots per scene with camera motion (`STATIC`, `SLOW_PUSH_IN`, `SLOW_PULL_OUT`, `PAN_LEFT`, `PAN_RIGHT`, `GENTLE_DRIFT`) and continuity-aware visual prompts.
3. **`src/ai_youtube_agent/providers/image_generation.py` & `mock_image_generation.py`**:
   - `ImageGenerator` protocol and `DeterministicSceneImageGenerator` producing valid 8-bit RGB PNGs at `1080x1920` (Shorts) or `1920x1080` (LongForm) using the location's `palette_rgb` and character/shot seed.
4. **`src/ai_youtube_agent/providers/video_generation.py` & `mock_video_generation.py`**:
   - `VideoClipGenerator` protocol, `UnconfiguredVideoClipGenerator` (honest default when no motion-video model is configured), and `MockVideoClipGenerator` (for deterministic testing).
5. **`src/ai_youtube_agent/pipeline/script_to_video.py`**:
   - `ScriptToVideoService`, `ScriptToVideoRequest`, `VideoProjectManifest`, `ProviderCapabilityReport`, `ScriptToVideoStage`, `ScriptToVideoError`.
6. **`src/ai_youtube_agent/content/script_to_video_api.py`**:
   - `GET /script-to-video/capabilities`
   - `POST /script-to-video/analyze`
   - `POST /script-to-video/runs`

---

## 4. How to Run

### Python Service Usage

```python
from ai_youtube_agent.bootstrap import build_container, prepare_database
from ai_youtube_agent.core.config import get_settings
from ai_youtube_agent.core.content_item import ContentType
from ai_youtube_agent.content.story_bible import ProductionMode
from ai_youtube_agent.pipeline.script_to_video import (
    ScriptToVideoRequest,
    ScriptToVideoService,
)

settings = get_settings()
prepare_database(settings)
container = build_container(settings)
service = container.resolve(ScriptToVideoService)

manifest = service.run(
    ScriptToVideoRequest(
        script_text="""
        SCENE 1: PHÒNG TRÀ CỔ - BAN ĐÊM
        Minh (trầm ngâm): Mỗi tách trà nóng đều giữ lại hương vị của một mùa cũ.
        Narrator: Ánh đèn vàng phản chiếu trên mặt bàn gỗ tĩnh lặng.

        SCENE 2: HIÊN NHÀ MƯA - BAN ĐÊM
        Lan (dịu dàng): Khi cơn mưa đầu mùa ghé qua, câu chuyện năm ấy lại trở về.

        SCENE 3: PHÒNG TRÀ CỔ - BÌNH MINH
        Minh (mỉm cười): Và buổi sáng mới luôn bắt đầu từ sự bình yên trong tâm trí.
        """,
        title="Hương Trà Đêm Mưa",
        content_type=ContentType.SHORTS,
        production_mode=ProductionMode.STORY_VIDEO,
        language="vi",
        request_approval=True,
    )
)
print("MP4 file path:", manifest.video_file_path)
print("QC status:", manifest.qc_report.overall_status)
```

### HTTP API Usage

- `GET /script-to-video/capabilities`: Inspect active providers and supported modes.
- `POST /script-to-video/analyze`: Generate `story_bible` and `scene_breakdown` JSON without rendering.
- `POST /script-to-video/runs`: Execute full pipeline and receive `VideoProjectManifest` JSON including `video.file_path`, `qc_report`, and `preview_snapshot`.

