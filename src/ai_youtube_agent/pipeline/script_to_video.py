"""Unified Script-to-Film & Script-to-Video Orchestrator.

Implements the end-to-end production pipeline from a user-supplied script
(raw text or file path) to a verified, playable MP4 video artifact with
synchronized voice audio, scene visuals, timed captions/subtitles, thumbnail,
metadata, QC report, preview snapshot, and optional human approval request:

``Input Script → Script Analysis → Story Bible → Budget Check → Scene Breakdown
  → Shot Planning → Asset Generation/Selection → Voice & Dialogue → Timeline
  → Audio Mixing → Video Rendering → QC → Preview → Approval``

Every voice, image and video-clip call goes through the ``PaidRequestGuard``
(BUDGET-01): the service keeps only the guarded wrappers of its providers. The
``budget.checked`` stage checks the providers of the run against the paid-API
policy; a budget stop fails the job with the budget code and raises
``ScriptToVideoError`` with ``pipeline.script_to_video_awaiting_paid_approval``
(the quote ids to approve) or ``pipeline.script_to_video_budget_blocked``.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from http import HTTPStatus
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from urllib.request import url2pathname

from ai_youtube_agent.content.approval_service import ApprovalService
from ai_youtube_agent.content.asset import AssetCategory, AssetKind
from ai_youtube_agent.content.asset_registry import AssetRegistry
from ai_youtube_agent.content.audio_artifact import (
    AudioArtifactRecord,
    AudioArtifactRecorder,
)
from ai_youtube_agent.content.audio_validation import AudioValidator
from ai_youtube_agent.content.broll_manager import (
    BrollAssignment,
    BrollManager,
    CropMode,
)
from ai_youtube_agent.content.channel import Channel, ChannelStatus, YouTubeIdentifiers
from ai_youtube_agent.content.disclosure_decider import DisclosureDecider
from ai_youtube_agent.content.disclosure_rule import DISCLOSURE_RULES, DisclosureFacts
from ai_youtube_agent.content.longform_audio_mix import LongFormAudioMixer
from ai_youtube_agent.content.longform_bundle import LongFormBundleBuilder
from ai_youtube_agent.content.longform_metadata import LongFormMetadataRecorder
from ai_youtube_agent.content.longform_render import LongFormRenderService
from ai_youtube_agent.content.longform_service import LongFormProjectService
from ai_youtube_agent.content.longform_subtitles import LongFormSubtitleRecorder
from ai_youtube_agent.content.longform_thumbnail import LongFormThumbnailRecorder
from ai_youtube_agent.content.paid_api_budget import (
    PaidRequestContext,
    PaidRequestDecisionError,
    PaidRequestStatus,
)
from ai_youtube_agent.content.policy_report import PolicyReporter
from ai_youtube_agent.content.preview_artifact import (
    PreviewArtifactService,
    PreviewSnapshot,
)
from ai_youtube_agent.content.provenance_recorder import ProvenanceRecorder
from ai_youtube_agent.content.qc import QCStatus
from ai_youtube_agent.content.qc_report import QCReport, QCReportBuilder
from ai_youtube_agent.content.rights_report import RightsReporter
from ai_youtube_agent.content.rights_risk_engine import RightsRiskEngine
from ai_youtube_agent.content.scene_planner import (
    SceneBreakdownPlan,
    SceneBreakdownPlanner,
    ScenePlan,
    SceneStatus,
)
from ai_youtube_agent.content.script import Script
from ai_youtube_agent.content.shorts_audio_mix import ShortsAudioMixer
from ai_youtube_agent.content.shorts_bundle import ShortsBundleBuilder
from ai_youtube_agent.content.shorts_metadata import ShortsMetadataRecorder
from ai_youtube_agent.content.shorts_render import ShortsRenderService
from ai_youtube_agent.content.shorts_service import ShortsProjectService
from ai_youtube_agent.content.shorts_subtitles import ShortsSubtitleRecorder
from ai_youtube_agent.content.shorts_thumbnail import ShortsThumbnailRecorder
from ai_youtube_agent.content.shorts_timeline import AssetFit, AssetRole, SceneAsset
from ai_youtube_agent.content.story_bible import (
    ProductionMode,
    ScriptAnalysisError,
    ScriptAnalysisErrorCode,
    ScriptAnalysisOutput,
    ScriptAnalyzer,
    StoryBible,
)
from ai_youtube_agent.content.strategy import (
    AgeRange,
    Audience,
    AudienceLevel,
    Brand,
    Budget,
    Cadence,
    FormatSettings,
    LanguageSettings,
    LongFormFormat,
    Market,
    Niche,
    Pillar,
    Resolution,
    ShortsFormat,
    StrategyProfile,
    canonical_language_tag,
)
from ai_youtube_agent.content.tts_job import (
    InMemoryChunkStore,
    TtsJobRunner,
    plan_chunks,
)
from ai_youtube_agent.content.voice import VoiceProfile
from ai_youtube_agent.core.artifact import Artifact, ArtifactKind
from ai_youtube_agent.core.artifact_storage import ArtifactStorage
from ai_youtube_agent.core.audit import (
    Actor,
    ActorKind,
    AuditLog,
    AuditResult,
    EntityRef,
)
from ai_youtube_agent.core.config import get_settings
from ai_youtube_agent.core.content_item import ContentItem, ContentStatus, ContentType
from ai_youtube_agent.core.db.database import Database
from ai_youtube_agent.core.db.repositories.asset import AssetRepository
from ai_youtube_agent.core.db.repositories.channel import (
    ChannelRepository,
    StrategyProfileRepository,
    VoiceProfileRepository,
)
from ai_youtube_agent.core.db.repositories.content import (
    ArtifactRepository,
    AudioMetadataRepository,
    ContentItemRepository,
    ScriptRepository,
)
from ai_youtube_agent.core.db.repositories.jobs import AIJobRepository
from ai_youtube_agent.core.db.repositories.provenance import ProvenanceRepository
from ai_youtube_agent.core.errors import DomainError
from ai_youtube_agent.core.flags import FeatureFlags
from ai_youtube_agent.core.log import get_logger
from ai_youtube_agent.core.production import start_production
from ai_youtube_agent.pipeline.job import AIJob, AIJobStatus
from ai_youtube_agent.pipeline.paid_request_guard import PaidRequestGuard
from ai_youtube_agent.providers.image_generation import (
    ImageGenerationError,
    ImageGenerationRequest,
    ImageGenerator,
)
from ai_youtube_agent.providers.speech_synthesis import SpeechSynthesizer
from ai_youtube_agent.providers.video_generation import (
    VideoClipGenerationError,
    VideoClipGenerationRequest,
    VideoClipGenerator,
)
from ai_youtube_agent.providers.video_render import VideoRenderer

Clock = Callable[[], datetime]
logger = get_logger(__name__)

SCRIPT_TO_VIDEO_JOB_KIND = "pipeline.script_to_video"
DEFAULT_SYSTEM_ACTOR = Actor(ActorKind.SYSTEM, "script_to_video")
DEFAULT_PROVISION_USER = Actor(ActorKind.USER, "script_to_video_owner")


class ScriptToVideoStage(StrEnum):
    """Ordered pipeline stages recorded as AIJob checkpoints."""

    SCRIPT_PARSED = "script.parsed"
    STORY_BIBLE_BUILT = "story_bible.built"
    BUDGET_CHECKED = "budget.checked"
    SCENES_PLANNED = "scenes.planned"
    ASSETS_READY = "assets.ready"
    VOICE_GENERATED = "voice.generated"
    TIMELINE_BUILT = "timeline.built"
    AUDIO_MIXED = "audio.mixed"
    VIDEO_RENDERED = "video.rendered"
    SUBTITLES_GENERATED = "subtitles.generated"
    THUMBNAIL_GENERATED = "thumbnail.generated"
    QC_COMPLETED = "qc.completed"
    PREVIEW_READY = "preview.ready"
    APPROVAL_REQUESTED = "approval.requested"


class ScriptToVideoErrorCode(StrEnum):
    FAILED = "pipeline.script_to_video_failed"
    EMPTY_SCRIPT = "pipeline.script_to_video_empty_script"
    INVALID_INPUT = "pipeline.script_to_video_invalid_input"
    CHANNEL_NOT_FOUND = "pipeline.script_to_video_channel_not_found"
    MODE_UNAVAILABLE = "pipeline.script_to_video_mode_unavailable"
    STAGE_FAILED = "pipeline.script_to_video_stage_failed"
    AWAITING_PAID_APPROVAL = "pipeline.script_to_video_awaiting_paid_approval"
    BUDGET_BLOCKED = "pipeline.script_to_video_budget_blocked"


USER_MESSAGES: dict[ScriptToVideoErrorCode, str] = {
    ScriptToVideoErrorCode.FAILED: "The script-to-video pipeline could not complete.",
    ScriptToVideoErrorCode.EMPTY_SCRIPT: (
        "Please provide a non-empty script text or valid script file."
    ),
    ScriptToVideoErrorCode.INVALID_INPUT: "The script-to-video parameters are invalid.",
    ScriptToVideoErrorCode.CHANNEL_NOT_FOUND: "The specified channel does not exist.",
    ScriptToVideoErrorCode.MODE_UNAVAILABLE: (
        "The requested production mode is unavailable because its required "
        "provider is not configured."
    ),
    ScriptToVideoErrorCode.STAGE_FAILED: (
        "A production stage failed while building the video."
    ),
    ScriptToVideoErrorCode.AWAITING_PAID_APPROVAL: (
        "Lượt chạy dừng trước một yêu cầu API có thể tính phí. Hãy xem báo giá, "
        "duyệt chi phí rồi chạy lại. Chưa có khoản phí nào phát sinh."
    ),
    ScriptToVideoErrorCode.BUDGET_BLOCKED: (
        "Lượt chạy bị chặn bởi chính sách ngân sách: không gọi API trả phí. Hãy "
        "dùng công cụ cục bộ/miễn phí (ComfyUI, Edge TTS, chế độ Story video)."
    ),
}

_HTTP_STATUS: dict[ScriptToVideoErrorCode, HTTPStatus] = {
    ScriptToVideoErrorCode.FAILED: HTTPStatus.UNPROCESSABLE_ENTITY,
    ScriptToVideoErrorCode.EMPTY_SCRIPT: HTTPStatus.UNPROCESSABLE_ENTITY,
    ScriptToVideoErrorCode.INVALID_INPUT: HTTPStatus.UNPROCESSABLE_ENTITY,
    ScriptToVideoErrorCode.CHANNEL_NOT_FOUND: HTTPStatus.NOT_FOUND,
    ScriptToVideoErrorCode.MODE_UNAVAILABLE: HTTPStatus.CONFLICT,
    ScriptToVideoErrorCode.STAGE_FAILED: HTTPStatus.UNPROCESSABLE_ENTITY,
    ScriptToVideoErrorCode.AWAITING_PAID_APPROVAL: HTTPStatus.CONFLICT,
    ScriptToVideoErrorCode.BUDGET_BLOCKED: HTTPStatus.CONFLICT,
}


class ScriptToVideoError(DomainError):
    """Structured error raised by ``ScriptToVideoService`` with stage context."""

    default_code = ScriptToVideoErrorCode.FAILED.value
    default_user_message = USER_MESSAGES[ScriptToVideoErrorCode.FAILED]
    default_http_status = HTTPStatus.UNPROCESSABLE_ENTITY

    def __init__(
        self,
        detail: str = "",
        *,
        code: ScriptToVideoErrorCode = ScriptToVideoErrorCode.FAILED,
        stage: ScriptToVideoStage | None = None,
        quote_ids: tuple[str, ...] = (),
        budget_status: PaidRequestStatus | None = None,
        user_message: str | None = None,
    ) -> None:
        super().__init__(
            detail,
            code=code.value,
            user_message=user_message or USER_MESSAGES[code],
        )
        self.stage = stage
        # BUDGET-01: the quotes a budget stop waits on (or stored as blocked).
        self.quote_ids = tuple(quote_ids)
        self.budget_status = budget_status
        self.default_http_status = _HTTP_STATUS.get(
            code, HTTPStatus.UNPROCESSABLE_ENTITY
        )


@dataclass(frozen=True)
class ProviderCapabilityReport:
    """Honest runtime capability report across voice, image, video-clip, and render
    providers."""

    voice_provider: str
    voice_available: bool
    image_provider: str
    image_available: bool
    video_clip_provider: str
    video_clip_available: bool
    render_provider: str
    render_available: bool
    supported_modes: tuple[ProductionMode, ...]
    unavailable_reasons: Mapping[str, str]
    # BUDGET-01: modes unavailable only because the budget policy blocks a
    # paid provider (a run is still accepted, so the stop is recorded on its
    # job), and the cost class of each provider.
    budget_blocked_modes: tuple[ProductionMode, ...] = ()
    cost_classes: Mapping[str, str] | None = None

    def supports_mode(self, mode: ProductionMode) -> bool:
        return mode in self.supported_modes

    def accepts_mode(self, mode: ProductionMode) -> bool:
        """Whether a run of ``mode`` may start: supported, or stopped only by
        the budget policy (the run then fails with the budget code)."""
        return mode in self.supported_modes or mode in self.budget_blocked_modes

    def as_dict(self) -> dict[str, Any]:
        return {
            "voice_provider": self.voice_provider,
            "voice_available": self.voice_available,
            "image_provider": self.image_provider,
            "image_available": self.image_available,
            "video_clip_provider": self.video_clip_provider,
            "video_clip_available": self.video_clip_available,
            "render_provider": self.render_provider,
            "render_available": self.render_available,
            "supported_modes": [m.value for m in self.supported_modes],
            "unavailable_reasons": dict(self.unavailable_reasons),
            "budget_blocked_modes": [m.value for m in self.budget_blocked_modes],
            "cost_classes": dict(self.cost_classes or {}),
        }


@dataclass(frozen=True)
class ScriptToVideoRequest:
    """Unified input specification for the Script-to-Film / Script-to-Video engine."""

    script_text: str | None = None
    script_path: str | Path | None = None
    title: str | None = None
    channel_id: str | None = None
    content_type: ContentType = ContentType.SHORTS
    production_mode: ProductionMode = ProductionMode.STORY_VIDEO
    visual_style: str = "cinematic_story"
    language: str = "vi"
    voice_id: str | None = None
    target_duration_seconds: int | None = None
    subtitles_enabled: bool = True
    request_approval: bool = False
    auto_provision_channel: bool = True
    content_item_id: str | None = None


@dataclass(frozen=True)
class SceneAssetRecord:
    """Manifest entry for one scene's visual asset and provenance."""

    scene_id: str
    section_index: int
    asset_id: str
    artifact_id: str
    provenance_id: str
    media_type: str
    sha256: str
    width: int
    height: int
    reused: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "scene_id": self.scene_id,
            "section_index": self.section_index,
            "asset_id": self.asset_id,
            "artifact_id": self.artifact_id,
            "provenance_id": self.provenance_id,
            "media_type": self.media_type,
            "sha256": self.sha256,
            "width": self.width,
            "height": self.height,
            "reused": self.reused,
        }


@dataclass(frozen=True)
class VideoProjectManifest:
    """Complete output manifest produced by ``ScriptToVideoService.run``."""

    job_id: str
    content_item_id: str
    channel_id: str
    project_id: str
    content_type: ContentType
    production_mode: ProductionMode
    status: ContentStatus
    story_bible: StoryBible
    scene_breakdown: SceneBreakdownPlan
    script_id: str
    script_version: int
    audio_artifact_id: str
    audio_storage_key: str
    audio_duration_ms: int
    video_artifact_id: str
    video_storage_key: str
    video_file_path: str
    video_sha256: str
    video_size_bytes: int
    subtitle_artifact_id: str | None
    subtitle_storage_key: str | None
    thumbnail_artifact_id: str
    thumbnail_storage_key: str
    metadata_artifact_id: str
    metadata_storage_key: str
    scene_assets: tuple[SceneAssetRecord, ...]
    qc_report: QCReport
    preview_snapshot: PreviewSnapshot
    approval_id: str | None
    completed_stages: tuple[ScriptToVideoStage, ...]
    capabilities: ProviderCapabilityReport
    # BUDGET-01: free, or paid_approved with the quotes this run consumed.
    budget_status: PaidRequestStatus = PaidRequestStatus.FREE
    paid_quote_ids: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "content_item_id": self.content_item_id,
            "channel_id": self.channel_id,
            "project_id": self.project_id,
            "content_type": self.content_type.value,
            "production_mode": self.production_mode.value,
            "status": self.status.value,
            "story_bible": self.story_bible.as_dict(),
            "scene_breakdown": self.scene_breakdown.as_dict(),
            "script_id": self.script_id,
            "script_version": self.script_version,
            "audio": {
                "artifact_id": self.audio_artifact_id,
                "storage_key": self.audio_storage_key,
                "duration_ms": self.audio_duration_ms,
            },
            "video": {
                "artifact_id": self.video_artifact_id,
                "storage_key": self.video_storage_key,
                "file_path": self.video_file_path,
                "sha256": self.video_sha256,
                "size_bytes": self.video_size_bytes,
            },
            "subtitles": (
                {
                    "artifact_id": self.subtitle_artifact_id,
                    "storage_key": self.subtitle_storage_key,
                }
                if self.subtitle_artifact_id is not None
                else None
            ),
            "thumbnail": {
                "artifact_id": self.thumbnail_artifact_id,
                "storage_key": self.thumbnail_storage_key,
            },
            "metadata": {
                "artifact_id": self.metadata_artifact_id,
                "storage_key": self.metadata_storage_key,
            },
            "scene_assets": [sa.as_dict() for sa in self.scene_assets],
            "qc_report": self.qc_report.to_dict(),
            "preview_snapshot": self.preview_snapshot.as_dict(),
            "approval_id": self.approval_id,
            "completed_stages": [st.value for st in self.completed_stages],
            "capabilities": self.capabilities.as_dict(),
            "budget_status": self.budget_status.value,
            "paid_quote_ids": list(self.paid_quote_ids),
        }


def _uri_to_file_path(uri: str) -> str:
    """Convert a ``file://`` URI to a local filesystem path string."""
    if uri.startswith("file://"):
        parsed = urlsplit(uri)
        return str(Path(url2pathname(parsed.path)).resolve())
    return str(Path(uri).resolve())


def _idempotency_key(
    *,
    channel_id: str,
    raw_script_sha256: str,
    content_type: ContentType,
    production_mode: ProductionMode,
    visual_style: str,
    content_item_id: str | None,
) -> str:
    seed = (
        f"script-to-video-v1|{channel_id}|{raw_script_sha256}|"
        f"{content_type.value}|{production_mode.value}|{visual_style}|"
        f"{content_item_id or 'auto'}"
    )
    digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()[:24]
    return f"s2v:{digest}"


def _scene_source(story_bible: StoryBible, scene: ScenePlan) -> str:
    return (
        f"script_to_video:{story_bible.story_id}:{scene.scene_id}:"
        f"{story_bible.production_mode.value}"
    )


def _scene_title(scene: ScenePlan) -> str:
    return f"Scene {scene.section_index + 1} - {scene.title[:48]}"


def _clip_request(
    scene: ScenePlan, width: int, height: int
) -> VideoClipGenerationRequest:
    primary_shot = scene.shots[0]
    return VideoClipGenerationRequest(
        scene_id=scene.scene_id,
        shot_id=primary_shot.shot_id,
        prompt=scene.visual_prompt,
        duration_ms=scene.duration_ms,
        width=width,
        height=height,
        fps=30,
        camera_motion=primary_shot.camera_motion.value,
        character_ids=scene.character_ids,
        location_id=scene.location_id,
    )


def _image_request(
    scene: ScenePlan, story_bible: StoryBible, width: int, height: int
) -> ImageGenerationRequest:
    location = story_bible.location_by_id(scene.location_id)
    palette = (
        location.palette_rgb
        if location is not None
        else ((28, 44, 76), (68, 108, 158), (235, 185, 92))
    )
    return ImageGenerationRequest(
        prompt=scene.visual_prompt,
        width=width,
        height=height,
        scene_id=scene.scene_id,
        shot_id=scene.shots[0].shot_id,
        style=story_bible.visual_style,
        seed_key=f"{story_bible.story_id}:{scene.scene_id}",
        palette_rgb=palette,
    )


def _only_budget_blocked(*providers: tuple[bool, str | None]) -> bool:
    """Whether at least one provider is blocked by the budget policy and every
    other one is usable (each entry is ``(usable, budget block reason)``)."""
    return any(block is not None for _, block in providers) and all(
        usable or block is not None for usable, block in providers
    )


class ScriptToVideoService:
    """End-to-end orchestrator transforming an input script into a rendered MP4
    video."""

    def __init__(
        self,
        database: Database,
        storage: ArtifactStorage,
        audit: AuditLog,
        *,
        speech_synthesizer: SpeechSynthesizer,
        image_generator: ImageGenerator,
        video_clip_generator: VideoClipGenerator,
        video_renderer: VideoRenderer,
        paid_request_guard: PaidRequestGuard,
        script_analyzer: ScriptAnalyzer | None = None,
        scene_planner: SceneBreakdownPlanner | None = None,
        asset_registry: AssetRegistry | None = None,
        provenance_recorder: ProvenanceRecorder | None = None,
        rights_engine: RightsRiskEngine | None = None,
        disclosure_decider: DisclosureDecider | None = None,
        audio_recorder: AudioArtifactRecorder | None = None,
        audio_validator: AudioValidator | None = None,
        broll_manager: BrollManager | None = None,
        shorts_service: ShortsProjectService | None = None,
        shorts_subtitles: ShortsSubtitleRecorder | None = None,
        shorts_thumbnail: ShortsThumbnailRecorder | None = None,
        shorts_metadata: ShortsMetadataRecorder | None = None,
        shorts_render: ShortsRenderService | None = None,
        shorts_bundle: ShortsBundleBuilder | None = None,
        longform_service: LongFormProjectService | None = None,
        longform_subtitles: LongFormSubtitleRecorder | None = None,
        longform_thumbnail: LongFormThumbnailRecorder | None = None,
        longform_metadata: LongFormMetadataRecorder | None = None,
        longform_render: LongFormRenderService | None = None,
        longform_bundle: LongFormBundleBuilder | None = None,
        qc_builder: QCReportBuilder | None = None,
        preview_service: PreviewArtifactService | None = None,
        approval_service: ApprovalService | None = None,
        clock: Clock | None = None,
    ) -> None:
        self._database = database
        self._storage = storage
        self._audit = audit
        if (
            hasattr(speech_synthesizer, "with_wav_output")
            and getattr(speech_synthesizer, "output_format", None) == "audio/mpeg"
        ):
            speech_synthesizer = speech_synthesizer.with_wav_output()
        # BUDGET-01: only the guarded wrappers are kept, so no AI provider call
        # of this service bypasses the paid-API policy.
        self._guard = paid_request_guard
        self._speech = paid_request_guard.wrap_speech(speech_synthesizer)
        self._image_gen = paid_request_guard.wrap_image(image_generator)
        self._video_clip_gen = paid_request_guard.wrap_video(video_clip_generator)
        self._renderer = video_renderer
        self._clock = clock
        # One chunk store per service, so a rerun of a succeeded TTS job in
        # this process rebuilds its audio instead of failing (chunks are not
        # persisted across restarts).
        self._chunk_store = InMemoryChunkStore()

        self._analyzer = script_analyzer or ScriptAnalyzer()
        self._planner = scene_planner or SceneBreakdownPlanner()
        self._asset_registry = asset_registry or AssetRegistry(
            database, audit, clock=clock
        )
        self._provenance = provenance_recorder or ProvenanceRecorder(
            database, audit, clock=clock
        )
        self._rights_engine = rights_engine or RightsRiskEngine(
            database, audit, clock=clock
        )
        self._disclosure = disclosure_decider or DisclosureDecider(
            database, audit, clock=clock
        )
        self._audio_recorder = audio_recorder or AudioArtifactRecorder(
            database, storage, audit, clock=clock
        )
        self._audio_validator = audio_validator or AudioValidator(
            database, storage, audit, clock=clock
        )

        lf_flags = FeatureFlags(longform_enabled=True)
        active_settings = get_settings()
        rights_reporter = RightsReporter(database, active_settings, clock=clock)
        policy_reporter = PolicyReporter(database, clock=clock)
        shorts_mixer = ShortsAudioMixer(database, storage, audit)
        longform_mixer = LongFormAudioMixer(database, storage, audit, lf_flags)

        self._shorts_service = shorts_service or ShortsProjectService(
            database, audit, clock=clock
        )
        self._longform_service = longform_service or LongFormProjectService(
            database, audit, flags=lf_flags, clock=clock
        )
        self._broll = broll_manager or BrollManager(
            database,
            audit,
            flags=lf_flags,
            clock=clock,
        )
        self._shorts_subtitles = shorts_subtitles or ShortsSubtitleRecorder(
            database, storage, audit, clock=clock
        )
        self._shorts_thumbnail = shorts_thumbnail or ShortsThumbnailRecorder(
            database, storage, audit, clock=clock
        )
        self._shorts_metadata = shorts_metadata or ShortsMetadataRecorder(
            database, storage, audit, clock=clock
        )
        self._shorts_render = shorts_render or ShortsRenderService(
            database,
            storage,
            audit,
            self._renderer,
            shorts_mixer,
            clock=clock,
        )
        self._shorts_bundle = shorts_bundle or ShortsBundleBuilder(
            database,
            storage,
            rights_reporter,
            policy_reporter,
            audit,
            clock=clock,
        )

        self._longform_subtitles = longform_subtitles or LongFormSubtitleRecorder(
            database, storage, audit, flags=lf_flags, clock=clock
        )
        self._longform_thumbnail = longform_thumbnail or LongFormThumbnailRecorder(
            database, storage, audit, flags=lf_flags, clock=clock
        )
        self._longform_metadata = longform_metadata or LongFormMetadataRecorder(
            database, storage, audit, flags=lf_flags, clock=clock
        )
        self._longform_render = longform_render or LongFormRenderService(
            database,
            storage,
            audit,
            self._renderer,
            longform_mixer,
            flags=lf_flags,
            clock=clock,
        )
        self._longform_bundle = longform_bundle or LongFormBundleBuilder(
            database,
            storage,
            audit,
            active_settings,
            rights_reporter=rights_reporter,
            policy_reporter=policy_reporter,
            flags=lf_flags,
            clock=clock,
        )
        self._qc_builder = qc_builder or QCReportBuilder(
            database, storage, audit, active_settings, clock=clock
        )
        self._preview_service = preview_service or PreviewArtifactService(
            database, storage
        )
        self._approval_service = approval_service or ApprovalService(
            database, audit, clock=clock
        )

    def _now(self) -> datetime:
        return self._clock() if self._clock else datetime.now(UTC)

    def check_capabilities(self) -> ProviderCapabilityReport:
        """Inspect configured voice, image, video-clip, and render providers.

        A provider the budget policy blocks (BUDGET-01) is reported unavailable
        with that reason and is not called; a mode that is unavailable only for
        that reason is listed in ``budget_blocked_modes``.
        """
        voice_block = self._speech.budget_blocked_reason()
        image_block = self._image_gen.budget_blocked_reason()
        clip_block = self._video_clip_gen.budget_blocked_reason()

        voice_ok = voice_block is None
        if voice_ok:
            try:
                self._speech.check()
            except Exception:
                voice_ok = False

        img_caps = self._image_gen.capabilities_image_gen()
        img_ok = img_caps.available
        if img_ok:
            try:
                self._image_gen.check()
            except Exception:
                img_ok = False

        clip_caps = self._video_clip_gen.capabilities_video_clip()
        clip_ok = clip_caps.available
        if clip_ok:
            try:
                self._video_clip_gen.check()
            except Exception:
                clip_ok = False

        render_ok = True
        try:
            self._renderer.check()
        except Exception:
            render_ok = False

        supported: list[ProductionMode] = []
        budget_blocked: list[ProductionMode] = []
        unavailable: dict[str, str] = {}

        if voice_ok and render_ok and img_ok:
            supported.append(ProductionMode.STORY_VIDEO)
        else:
            reasons: list[str] = []
            if voice_block is not None:
                reasons.append(f"voice provider '{self._speech.name}' {voice_block}")
            elif not voice_ok:
                reasons.append(f"voice provider '{self._speech.name}' is unhealthy")
            if image_block is not None:
                reasons.append(f"image provider '{self._image_gen.name}' {image_block}")
            elif not img_ok:
                reasons.append(
                    f"image provider '{self._image_gen.name}' is unavailable"
                )
            if not render_ok:
                reasons.append(f"video renderer '{self._renderer.name}' is unhealthy")
            unavailable[ProductionMode.STORY_VIDEO.value] = "; ".join(reasons)
            if _only_budget_blocked(
                (voice_ok, voice_block), (img_ok, image_block), (render_ok, None)
            ):
                budget_blocked.append(ProductionMode.STORY_VIDEO)

        if voice_ok and render_ok and clip_ok:
            supported.append(ProductionMode.AI_FILM)
        else:
            clip_reasons: list[str] = []
            if clip_block is not None:
                clip_reasons.append(
                    f"video clip generator '{self._video_clip_gen.name}' {clip_block}"
                )
            elif not clip_ok:
                clip_reasons.append(
                    f"video clip generator '{self._video_clip_gen.name}' is not "
                    f"configured "
                    "for motion video synthesis (use story_video mode or configure a "
                    "video clip provider)"
                )
            if voice_block is not None:
                clip_reasons.append(
                    f"voice provider '{self._speech.name}' {voice_block}"
                )
            elif not voice_ok:
                clip_reasons.append(
                    f"voice provider '{self._speech.name}' is unhealthy"
                )
            if not render_ok:
                clip_reasons.append(
                    f"video renderer '{self._renderer.name}' is unhealthy"
                )
            unavailable[ProductionMode.AI_FILM.value] = "; ".join(clip_reasons)
            if _only_budget_blocked(
                (voice_ok, voice_block), (clip_ok, clip_block), (render_ok, None)
            ):
                budget_blocked.append(ProductionMode.AI_FILM)

        return ProviderCapabilityReport(
            voice_provider=self._speech.name,
            voice_available=voice_ok,
            image_provider=self._image_gen.name,
            image_available=img_ok,
            video_clip_provider=self._video_clip_gen.name,
            video_clip_available=clip_ok,
            render_provider=self._renderer.name,
            render_available=render_ok,
            supported_modes=tuple(supported),
            unavailable_reasons=unavailable,
            budget_blocked_modes=tuple(budget_blocked),
            cost_classes={
                "voice": self._speech.cost_declaration().cost_class.value,
                "image": self._image_gen.cost_declaration().cost_class.value,
                "video_clip": self._video_clip_gen.cost_declaration().cost_class.value,
            },
        )

    def analyze_only(
        self, request: ScriptToVideoRequest
    ) -> tuple[StoryBible, SceneBreakdownPlan]:
        """Parse and analyze the script into a StoryBible and SceneBreakdownPlan without
        rendering."""
        analysis = self._analyze_script(request)
        # The analyzer already scales the sections to target_duration_seconds.
        breakdown = self._planner.plan(analysis)
        return analysis.story_bible, breakdown

    def run(
        self,
        request: ScriptToVideoRequest,
        *,
        actor: Actor | None = None,
        take_over: bool = True,
        on_job_started: Callable[[str, str], None] | None = None,
    ) -> VideoProjectManifest:
        """Execute the full Script-to-Video pipeline and return the
        ``VideoProjectManifest``.

        ``on_job_started`` (APP-01) is called with the job id and the content
        item id once the job is started or resumed, so a background runner can
        follow its checkpoints; an error in it is logged and ignored.

        Every AI provider call of the run is decided by the ``PaidRequestGuard``
        (BUDGET-01) within the content item and job of the run.
        """
        active_actor = actor or DEFAULT_SYSTEM_ACTOR
        capabilities = self.check_capabilities()

        # 1. Parse and analyze script first so empty/invalid script errors surface
        # immediately
        analysis = self._analyze_script(request)
        story_bible = analysis.story_bible

        # 2. Honest capability check for the requested production mode; a mode
        # stopped only by the budget policy starts, so the stop is on its job.
        if not capabilities.accepts_mode(request.production_mode):
            reason = capabilities.unavailable_reasons.get(
                request.production_mode.value,
                f"production mode {request.production_mode.value!r} is unavailable",
            )
            raise ScriptToVideoError(
                reason,
                code=ScriptToVideoErrorCode.MODE_UNAVAILABLE,
                stage=ScriptToVideoStage.STORY_BIBLE_BUILT,
            )

        # 3. Ensure channel, StrategyProfile, and VoiceProfile exist
        channel_id, strategy, voice_profile = self._ensure_channel_and_profiles(
            request=request,
            story_bible=story_bible,
        )

        # 4. Create or resume AIJob and ContentItem
        job, item = self._start_or_resume_job(
            request=request,
            channel_id=channel_id,
            strategy=strategy,
            story_bible=story_bible,
            take_over=take_over,
        )
        if on_job_started is not None:
            try:
                on_job_started(job.id, item.id)
            except Exception:
                logger.exception("on_job_started hook failed")
        completed_stages: list[ScriptToVideoStage] = []
        budget_session = self._guard.start(PaidRequestContext(item.id, job.id))

        try:
            job = self._checkpoint(
                job,
                ScriptToVideoStage.SCRIPT_PARSED,
                {
                    "raw_script_sha256": story_bible.raw_script_sha256,
                    "beat_count": len(analysis.beats),
                },
                completed_stages,
            )
            job = self._checkpoint(
                job,
                ScriptToVideoStage.STORY_BIBLE_BUILT,
                {
                    "story_id": story_bible.story_id,
                    "character_count": len(story_bible.characters),
                    "location_count": len(story_bible.locations),
                },
                completed_stages,
            )

            # 4b. Budget check (BUDGET-01) of the providers this mode calls:
            # free_only stops here before any paid provider is called.
            preflight = self._guard.preflight(
                (
                    self._speech,
                    self._video_clip_gen
                    if request.production_mode is ProductionMode.AI_FILM
                    else self._image_gen,
                )
            )
            job = self._checkpoint(
                job,
                ScriptToVideoStage.BUDGET_CHECKED,
                {
                    "mode": preflight.mode.value,
                    "status": preflight.status,
                    "paid_providers": ",".join(preflight.paid_providers),
                },
                completed_stages,
            )

            # 5. Persist Script version and advance ContentItem to SCRIPTED
            script, item = self._store_domain_script(
                analysis=analysis,
                item=item,
                strategy=strategy,
            )

            # 6. Run TTS job, record AudioArtifact, and run AudioValidator
            tts_runner = TtsJobRunner(
                self._database,
                self._speech,
                self._chunk_store,
                clock=self._clock,
                sleep=lambda _s: None,
            )
            # Chunks live in memory only, so after a restart a succeeded TTS job
            # cannot be rebuilt; the recorded audio of the same script and voice
            # profile version is reused instead.
            audio_record = self._recorded_audio(item.id, script, voice_profile)
            chunk_count = 0
            if audio_record is None:
                # A paid voice: every chunk still to be made is quoted in one
                # pass before the first call (D1(b)); chunks a resumed TTS job
                # reuses are not quoted, a repeated text is quoted once per
                # occurrence, and each call then consumes its own approval.
                if self._speech.is_paid:
                    self._speech.authorize_batch(
                        (
                            voice_profile.speech_request(chunk.text)
                            for chunk in plan_chunks(script)
                        ),
                        reused=tts_runner.stored_chunks(script, voice_profile),
                    )
                tts_result = tts_runner.run(script, voice_profile, take_over=True)
                chunk_count = tts_result.chunks
                audio_record = self._audio_recorder.record(
                    tts_result, script, voice_profile
                )
            self._audio_validator.validate(audio_record.artifact, audio_record.audio)
            item = self._advance_item_status(item, ContentStatus.GENERATING)
            job = self._checkpoint(
                job,
                ScriptToVideoStage.VOICE_GENERATED,
                {
                    "audio_artifact_id": audio_record.artifact.id,
                    "duration_ms": audio_record.audio.duration_ms,
                    "chunks": chunk_count,
                    "reused": chunk_count == 0,
                },
                completed_stages,
            )

            # 7. Create/rebind project and plan timeline scenes from actual audio
            # duration
            if request.content_type is ContentType.SHORTS:
                project_id, timeline_scenes = self._prepare_shorts_scenes(
                    item.id, active_actor
                )
            else:
                project_id, timeline_scenes = self._prepare_longform_scenes(
                    item.id, active_actor
                )

            scene_breakdown = self._planner.plan(
                analysis,
                scene_windows_ms=[(sc.start_ms, sc.end_ms) for sc in timeline_scenes],
            )
            job = self._checkpoint(
                job,
                ScriptToVideoStage.SCENES_PLANNED,
                {
                    "project_id": project_id,
                    "scene_count": len(scene_breakdown.scenes),
                    "total_duration_ms": scene_breakdown.total_duration_ms,
                },
                completed_stages,
            )

            # 8. Generate or reuse visual assets per scene, register provenance, Rights
            # & Disclosure
            scene_assets, scene_breakdown = self._generate_or_reuse_scene_assets(
                item=item,
                story_bible=story_bible,
                breakdown=scene_breakdown,
                actor=active_actor,
            )
            self._rights_engine.assess(item.id, actor=active_actor)
            self._disclosure.decide(
                item.id,
                DisclosureFacts(
                    realistic_person=False,
                    realistic_event=False,
                    synthetic_voice_of_real_person=False,
                    realistic_visual=True,
                ),
                rule_set=DISCLOSURE_RULES,
                actor=active_actor,
            )
            job = self._checkpoint(
                job,
                ScriptToVideoStage.ASSETS_READY,
                {
                    "asset_count": len(scene_assets),
                    "reused_count": sum(1 for sa in scene_assets if sa.reused),
                },
                completed_stages,
            )

            # 9. Assign scene visuals, chapters, captions, subtitles, thumbnail,
            # metadata, and render MP4
            if request.content_type is ContentType.SHORTS:
                (
                    subtitle_art,
                    thumb_art,
                    meta_art,
                    video_art,
                ) = self._produce_shorts(
                    project_id=project_id,
                    item=item,
                    strategy=strategy,
                    story_bible=story_bible,
                    scene_assets=scene_assets,
                    subtitles_enabled=request.subtitles_enabled,
                    actor=active_actor,
                    job=job,
                    completed_stages=completed_stages,
                )
            else:
                (
                    subtitle_art,
                    thumb_art,
                    meta_art,
                    video_art,
                ) = self._produce_longform(
                    project_id=project_id,
                    item=item,
                    strategy=strategy,
                    story_bible=story_bible,
                    scene_assets=scene_assets,
                    subtitles_enabled=request.subtitles_enabled,
                    actor=active_actor,
                    job=job,
                    completed_stages=completed_stages,
                )

            item = self._advance_item_status(item, ContentStatus.TESTING)
            scene_breakdown = replace(
                scene_breakdown,
                scenes=tuple(
                    replace(sc, status=SceneStatus.RENDERED)
                    for sc in scene_breakdown.scenes
                ),
            )
            job = self._reload_job(job.id)

            # 10. Run QC report builder (video checks + rights + policy + disclosure)
            qc_report = self._qc_builder.generate(
                item.id,
                actor=active_actor,
                persist=True,
            )
            job = self._checkpoint(
                job,
                ScriptToVideoStage.QC_COMPLETED,
                {
                    "qc_result_id": qc_report.qc_result.id,
                    "overall_status": qc_report.status.value,
                    "passed": qc_report.status is not QCStatus.FAIL,
                },
                completed_stages,
            )

            # 11. Advance ContentItem to PREVIEW_READY (it stays TESTING if QC failed)
            # and build PreviewSnapshot
            if qc_report.status is not QCStatus.FAIL:
                item = self._advance_item_status(item, ContentStatus.PREVIEW_READY)
            with self._database.transaction() as conn:
                latest_item = ContentItemRepository(conn).get(item.id)
                if latest_item is not None:
                    item = latest_item
            preview_snapshot = self._preview_service.get_snapshot(item.id)
            job = self._checkpoint(
                job,
                ScriptToVideoStage.PREVIEW_READY,
                {
                    "video_artifact_id": video_art.id,
                    "status": item.status.value,
                },
                completed_stages,
            )

            # 12. Optional human approval request (never auto-approves or
            # auto-publishes)
            approval_id: str | None = None
            if request.request_approval and qc_report.status is not QCStatus.FAIL:
                approval_decision = self._approval_service.request_approval(
                    item.id,
                    requested_by=active_actor,
                    expected_video_artifact_id=video_art.id,
                )
                approval_id = approval_decision.approval.id
                item = approval_decision.content_item
                job = self._checkpoint(
                    job,
                    ScriptToVideoStage.APPROVAL_REQUESTED,
                    {"approval_id": approval_id},
                    completed_stages,
                )

            # Mark job succeeded
            with self._database.transaction() as conn:
                jobs_repo = AIJobRepository(conn)
                current_job = jobs_repo.get(job.id)
                if (
                    current_job is not None
                    and current_job.status is AIJobStatus.RUNNING
                ):
                    succeeded_job = current_job.succeed(clock=self._clock)
                    jobs_repo.update(
                        succeeded_job, expected_updated_at=current_job.updated_at
                    )
                    job = succeeded_job

            self._audit.record(
                "pipeline.script_to_video_completed",
                active_actor,
                EntityRef("content_item", item.id),
                AuditResult.SUCCESS,
                {
                    "job_id": job.id,
                    "channel_id": channel_id,
                    "project_id": project_id,
                    "content_type": request.content_type.value,
                    "production_mode": request.production_mode.value,
                    "video_artifact_id": video_art.id,
                    "qc_status": qc_report.status.value,
                    "approval_requested": approval_id is not None,
                    "budget_status": budget_session.status.value,
                },
            )

            return VideoProjectManifest(
                job_id=job.id,
                content_item_id=item.id,
                channel_id=channel_id,
                project_id=project_id,
                content_type=request.content_type,
                production_mode=request.production_mode,
                status=item.status,
                story_bible=story_bible,
                scene_breakdown=scene_breakdown,
                script_id=script.id,
                script_version=script.version,
                audio_artifact_id=audio_record.artifact.id,
                audio_storage_key=audio_record.artifact.uri,
                audio_duration_ms=audio_record.audio.duration_ms,
                video_artifact_id=video_art.id,
                video_storage_key=video_art.uri,
                video_file_path=_uri_to_file_path(video_art.uri),
                video_sha256=video_art.sha256,
                video_size_bytes=video_art.size_bytes,
                subtitle_artifact_id=subtitle_art.id if subtitle_art else None,
                subtitle_storage_key=subtitle_art.uri if subtitle_art else None,
                thumbnail_artifact_id=thumb_art.id,
                thumbnail_storage_key=thumb_art.uri,
                metadata_artifact_id=meta_art.id,
                metadata_storage_key=meta_art.uri,
                scene_assets=scene_assets,
                qc_report=qc_report,
                preview_snapshot=preview_snapshot,
                approval_id=approval_id,
                completed_stages=tuple(completed_stages),
                capabilities=capabilities,
                budget_status=budget_session.status,
                paid_quote_ids=tuple(budget_session.consumed_quote_ids),
            )
        except ScriptToVideoError:
            self._fail_job_safely(job.id, ScriptToVideoErrorCode.STAGE_FAILED.value)
            raise
        except PaidRequestDecisionError as error:
            # BUDGET-01 (D3(a)): the job fails with the budget code; nothing was
            # charged and no other provider was called.
            self._fail_job_safely(job.id, error.code)
            last_stage = completed_stages[-1] if completed_stages else None
            raise ScriptToVideoError(
                f"{error.code}: {error.detail}",
                code=(
                    ScriptToVideoErrorCode.AWAITING_PAID_APPROVAL
                    if error.status is PaidRequestStatus.AWAITING_APPROVAL
                    else ScriptToVideoErrorCode.BUDGET_BLOCKED
                ),
                stage=last_stage,
                quote_ids=error.quote_ids,
                budget_status=error.status,
                user_message=error.user_message,
            ) from error
        except DomainError as error:
            self._fail_job_safely(job.id, error.code)
            last_stage = completed_stages[-1] if completed_stages else None
            raise ScriptToVideoError(
                f"{error.code}: {error.detail or error.user_message}",
                code=ScriptToVideoErrorCode.STAGE_FAILED,
                stage=last_stage,
            ) from error
        except Exception as error:
            self._fail_job_safely(job.id, ScriptToVideoErrorCode.STAGE_FAILED.value)
            last_stage = completed_stages[-1] if completed_stages else None
            raise ScriptToVideoError(
                str(error),
                code=ScriptToVideoErrorCode.STAGE_FAILED,
                stage=last_stage,
            ) from error
        finally:
            budget_session.close()

    def _analyze_script(self, request: ScriptToVideoRequest) -> ScriptAnalysisOutput:
        try:
            return self._analyzer.analyze(
                script_text=request.script_text,
                script_path=request.script_path,
                title=request.title,
                content_type=request.content_type,
                production_mode=request.production_mode,
                visual_style=request.visual_style,
                language=request.language,
                voice_id=request.voice_id or "vi-VN-Standard-A",
                target_duration_seconds=request.target_duration_seconds,
                clock=self._clock,
            )
        except ScriptAnalysisError as error:
            if error.code in (
                ScriptAnalysisErrorCode.EMPTY_SCRIPT,
                ScriptAnalysisErrorCode.FILE_NOT_FOUND,
                ScriptAnalysisErrorCode.FILE_UNREADABLE,
            ):
                raise ScriptToVideoError(
                    error.detail,
                    code=ScriptToVideoErrorCode.EMPTY_SCRIPT,
                    stage=ScriptToVideoStage.SCRIPT_PARSED,
                ) from error
            raise ScriptToVideoError(
                error.detail,
                code=ScriptToVideoErrorCode.INVALID_INPUT,
                stage=ScriptToVideoStage.SCRIPT_PARSED,
            ) from error
        except (ValueError, TypeError) as error:
            raise ScriptToVideoError(
                str(error),
                code=ScriptToVideoErrorCode.INVALID_INPUT,
                stage=ScriptToVideoStage.SCRIPT_PARSED,
            ) from error

    def _ensure_channel_and_profiles(
        self,
        *,
        request: ScriptToVideoRequest,
        story_bible: StoryBible,
    ) -> tuple[str, StrategyProfile, VoiceProfile]:
        try:
            canon_lang = canonical_language_tag(request.language or "vi")
        except ValueError:
            canon_lang = "vi"
        provider_default_voice = (
            getattr(self._speech, "default_voice", None) or "vi-VN-Standard-A"
        )
        default_voice_id = (
            request.voice_id or provider_default_voice
        ).strip() or provider_default_voice

        with self._database.transaction() as conn:
            channels = ChannelRepository(conn)
            strategies = StrategyProfileRepository(conn)
            voices = VoiceProfileRepository(conn)

            channel: Channel | None = None
            if request.channel_id is not None:
                channel = channels.get(request.channel_id)
                if channel is None and not request.auto_provision_channel:
                    raise ScriptToVideoError(
                        f"channel {request.channel_id!r} does not exist",
                        code=ScriptToVideoErrorCode.CHANNEL_NOT_FOUND,
                    )
            else:
                existing_channels = channels.list_all()
                if existing_channels:
                    channel = existing_channels[0]

            if channel is None:
                if not request.auto_provision_channel:
                    raise ScriptToVideoError(
                        "channel_id is required when auto_provision_channel is False",
                        code=ScriptToVideoErrorCode.CHANNEL_NOT_FOUND,
                    )
                now = self._now()
                ch_id = request.channel_id or "ch_script_to_video_default"
                channel = Channel(
                    id=ch_id,
                    title="Script-to-Film Studio Channel",
                    youtube=YouTubeIdentifiers(
                        channel_id="UCS2V_DEFAULT_STUDIO_000",
                        handle="@scripttofilmstudio",
                    ),
                    status=ChannelStatus.ACTIVE,
                    created_at=now,
                    updated_at=now,
                )
                channels.add(channel)

            strategy = strategies.get_by_channel(channel.id)
            default_format = FormatSettings(
                shorts=ShortsFormat(
                    min_seconds=12,
                    max_seconds=90,
                    resolution=Resolution.FULL_HD_1080,
                    captions=request.subtitles_enabled,
                ),
                longform=LongFormFormat(
                    min_seconds=181,
                    max_seconds=3600,
                    resolution=Resolution.FULL_HD_1080,
                    chapters=True,
                    captions=request.subtitles_enabled,
                ),
            )
            if strategy is None:
                strategy = StrategyProfile.create(
                    channel.id,
                    actor=DEFAULT_PROVISION_USER,
                    market=Market("VN"),
                    languages=LanguageSettings.canonical(canon_lang, ("en",)),
                    audience=Audience(
                        description="Storytelling and cinematic AI video viewers",
                        age_range=AgeRange(18, 45),
                        level=AudienceLevel.MIXED,
                        interests=("storytelling", "cinema", "short films"),
                    ),
                    niche=Niche(
                        name="Narrative Storytelling & AI Film",
                        pillars=(
                            Pillar(
                                "Story Videos",
                                "Character-driven narrative stories and visual fables",
                            ),
                        ),
                    ),
                    brand=Brand(
                        name="Script-to-Film Studio",
                        tone="Cinematic, clear, engaging narrative delivery",
                        tone_keywords=("cinematic", "story", "expressive"),
                        voice_dos=("Stay faithful to the script",),
                        voice_donts=("Clickbait claims",),
                        banned_phrases=(),
                    ),
                    format=default_format,
                    cadence=Cadence(
                        shorts_per_day=5,
                        longform_per_day=2,
                        time_zone="Asia/Ho_Chi_Minh",
                    ),
                    budget=Budget(
                        daily_limit=Decimal("50.00"),
                        monthly_limit=Decimal("500.00"),
                        currency="USD",
                    ),
                    clock=self._clock,
                )
                strategies.add(strategy)
            elif strategy.format is None or strategy.languages is None:
                updates: dict[str, Any] = {}
                if strategy.format is None:
                    updates["format"] = default_format
                if strategy.languages is None:
                    updates["languages"] = LanguageSettings.canonical(canon_lang)
                updated_strategy = strategy.update(
                    actor=DEFAULT_PROVISION_USER,
                    clock=self._clock,
                    **updates,
                )
                strategies.update(updated_strategy, expected_version=strategy.version)
                strategy = updated_strategy

            channel_voices = voices.list_by_channel(channel.id)
            matching_voice = next(
                (
                    v
                    for v in channel_voices
                    if v.provider == self._speech.name
                    and (request.voice_id is None or v.voice_id == default_voice_id)
                ),
                None,
            )
            if matching_voice is None:
                voice_lang = (
                    strategy.languages.primary
                    if strategy.languages is not None
                    else canon_lang
                )
                matching_voice = VoiceProfile.create(
                    channel.id,
                    provider=self._speech.name,
                    voice_id=default_voice_id,
                    language=voice_lang,
                    speaking_style="narrative",
                    actor=DEFAULT_PROVISION_USER,
                    clock=self._clock,
                )
                voices.add(matching_voice)

            return channel.id, strategy, matching_voice

    def _start_or_resume_job(
        self,
        *,
        request: ScriptToVideoRequest,
        channel_id: str,
        strategy: StrategyProfile,
        story_bible: StoryBible,
        take_over: bool,
    ) -> tuple[AIJob, ContentItem]:
        idem_key = _idempotency_key(
            channel_id=channel_id,
            raw_script_sha256=story_bible.raw_script_sha256,
            content_type=request.content_type,
            production_mode=request.production_mode,
            visual_style=story_bible.visual_style,
            content_item_id=request.content_item_id,
        )
        with self._database.transaction() as conn:
            items_repo = ContentItemRepository(conn)
            jobs_repo = AIJobRepository(conn)

            existing_job = jobs_repo.get_by_idempotency_key(idem_key)
            item: ContentItem | None = None

            if request.content_item_id is not None:
                item = items_repo.get(request.content_item_id)
            elif existing_job is not None and existing_job.content_item_id is not None:
                item = items_repo.get(existing_job.content_item_id)

            if item is None:
                item = ContentItem.create(
                    channel_id=channel_id,
                    strategy_profile_id=strategy.id,
                    strategy_version=strategy.version,
                    content_type=request.content_type,
                    title=story_bible.title,
                    clock=self._clock,
                )
                if request.content_item_id is not None:
                    item = replace(item, id=request.content_item_id)
                items_repo.add(item)

            if existing_job is None:
                job = AIJob.create(
                    SCRIPT_TO_VIDEO_JOB_KIND,
                    idem_key,
                    content_item_id=item.id,
                    clock=self._clock,
                )
                job = job.start(clock=self._clock)
                jobs_repo.add(job)
            else:
                job = existing_job
                if job.status is AIJobStatus.RUNNING and take_over:
                    failed_job = job.fail("interrupted", clock=self._clock)
                    jobs_repo.update(failed_job, expected_updated_at=job.updated_at)
                    job = failed_job
                if job.status in (AIJobStatus.QUEUED, AIJobStatus.FAILED):
                    started_job = job.start(clock=self._clock)
                    jobs_repo.update(started_job, expected_updated_at=job.updated_at)
                    job = started_job
                elif job.status is AIJobStatus.SUCCEEDED:
                    # Allow re-entry to verify or rebuild manifest idempotently
                    pass

            return job, item

    def _checkpoint(
        self,
        job: AIJob,
        stage: ScriptToVideoStage,
        data: Mapping[str, Any],
        completed_stages: list[ScriptToVideoStage],
    ) -> AIJob:
        if stage not in completed_stages:
            completed_stages.append(stage)
        with self._database.transaction() as conn:
            jobs_repo = AIJobRepository(conn)
            current = jobs_repo.get(job.id)
            if current is None or current.status is not AIJobStatus.RUNNING:
                return current or job
            updated = current.checkpoint(stage.value, dict(data), clock=self._clock)
            jobs_repo.update(updated, expected_updated_at=current.updated_at)
            return updated

    def _reload_job(self, job_id: str) -> AIJob:
        with self._database.transaction() as conn:
            found = AIJobRepository(conn).get(job_id)
            if found is None:
                raise ValueError(f"job {job_id} disappeared")
            return found

    def _fail_job_safely(self, job_id: str, error_code: str) -> None:
        try:
            with self._database.transaction() as conn:
                jobs_repo = AIJobRepository(conn)
                current = jobs_repo.get(job_id)
                if current is not None and current.status is AIJobStatus.RUNNING:
                    failed = current.fail(error_code[:120], clock=self._clock)
                    jobs_repo.update(failed, expected_updated_at=current.updated_at)
        except Exception:
            pass

    def _store_domain_script(
        self,
        *,
        analysis: ScriptAnalysisOutput,
        item: ContentItem,
        strategy: StrategyProfile,
    ) -> tuple[Script, ContentItem]:
        with self._database.transaction() as conn:
            scripts_repo = ScriptRepository(conn)
            items_repo = ContentItemRepository(conn)
            current_item = items_repo.get(item.id) or item

            versions = scripts_repo.list_by_content_item(current_item.id)
            latest_script = versions[-1] if versions else None
            if latest_script is not None and [
                (s.kind, s.text, s.title) for s in latest_script.sections
            ] == [(s.kind, s.text, s.title) for s in analysis.sections]:
                script = latest_script
            else:
                script = self._analyzer.build_domain_script(
                    analysis,
                    content_item_id=current_item.id,
                    strategy_version=strategy.version,
                    previous=latest_script,
                    clock=self._clock,
                )
                scripts_repo.add(script)

            if current_item.status is ContentStatus.DRAFT:
                current_item, _ = start_production(
                    conn, current_item, clock=self._clock
                )

            return script, current_item

    def _advance_item_status(
        self, item: ContentItem, target: ContentStatus
    ) -> ContentItem:
        progression = (
            ContentStatus.DRAFT,
            ContentStatus.GENERATING,
            ContentStatus.TESTING,
            ContentStatus.PREVIEW_READY,
        )
        with self._database.transaction() as conn:
            repo = ContentItemRepository(conn)
            curr = repo.get(item.id) or item
            if curr.status == target:
                return curr
            if curr.status not in progression or target not in progression:
                return curr
            curr_idx = progression.index(curr.status)
            target_idx = progression.index(target)
            while curr_idx < target_idx:
                next_status = progression[curr_idx + 1]
                updated = curr.with_status(next_status, clock=self._clock)
                repo.update(updated, expected_updated_at=curr.updated_at)
                curr = updated
                curr_idx += 1
            return curr

    def _prepare_shorts_scenes(
        self, content_item_id: str, actor: Actor
    ) -> tuple[str, tuple[Any, ...]]:
        existing = self._shorts_service.get_by_content_item(content_item_id)
        if existing is None:
            proj = self._shorts_service.create(content_item_id, actor=actor)
        elif not self._shorts_service.is_current(existing.id):
            proj = self._shorts_service.rebind(
                existing.id, expected_version=existing.version, actor=actor
            )
        else:
            proj = existing

        if not proj.timeline.scenes:
            proj = self._shorts_service.plan(
                proj.id, expected_version=proj.version, actor=actor
            )
        return proj.id, proj.timeline.scenes

    def _prepare_longform_scenes(
        self, content_item_id: str, actor: Actor
    ) -> tuple[str, tuple[Any, ...]]:
        existing = self._longform_service.get_by_content_item(content_item_id)
        if existing is None:
            proj = self._longform_service.create(content_item_id, actor=actor)
        elif not self._longform_service.is_current(existing.id):
            proj = self._longform_service.rebind(
                existing.id, expected_version=existing.version, actor=actor
            )
        else:
            proj = existing

        if not proj.timeline.scenes:
            proj = self._longform_service.plan(
                proj.id, expected_version=proj.version, actor=actor
            )
        return proj.id, proj.timeline.scenes

    def _generate_or_reuse_scene_assets(
        self,
        *,
        item: ContentItem,
        story_bible: StoryBible,
        breakdown: SceneBreakdownPlan,
        actor: Actor,
    ) -> tuple[tuple[SceneAssetRecord, ...], SceneBreakdownPlan]:
        width, height = breakdown.canvas_size
        records: list[SceneAssetRecord] = []
        updated_scenes: list[ScenePlan] = []
        ai_film = story_bible.production_mode is ProductionMode.AI_FILM

        reusable = [
            self._try_reuse_scene_asset(
                item=item,
                scene=scene,
                source=_scene_source(story_bible, scene),
                title=_scene_title(scene),
                width=width,
                height=height,
            )
            for scene in breakdown.scenes
        ]
        # BUDGET-01 (D1(b)): a paid provider gets every new scene of the stage
        # quoted in one pass before the first call.
        to_generate = [
            scene
            for scene, reused in zip(breakdown.scenes, reusable, strict=True)
            if reused is None
        ]
        if ai_film and self._video_clip_gen.is_paid:
            self._video_clip_gen.authorize_batch(
                _clip_request(scene, width, height) for scene in to_generate
            )
        elif not ai_film and self._image_gen.is_paid:
            self._image_gen.authorize_batch(
                _image_request(scene, story_bible, width, height)
                for scene in to_generate
            )

        for scene, reused_rec in zip(breakdown.scenes, reusable, strict=True):
            source_id = _scene_source(story_bible, scene)
            title = _scene_title(scene)
            if reused_rec is not None:
                records.append(reused_rec)
                updated_scenes.append(replace(scene, status=SceneStatus.ASSETS_READY))
                continue

            if ai_film:
                try:
                    clip = self._video_clip_gen.generate_clip(
                        _clip_request(scene, width, height)
                    )
                except VideoClipGenerationError as error:
                    raise ScriptToVideoError(
                        error.detail,
                        code=ScriptToVideoErrorCode.MODE_UNAVAILABLE,
                        stage=ScriptToVideoStage.ASSETS_READY,
                    ) from error
                raw_bytes = clip.data
                media_type = clip.media_type
                asset_kind = AssetKind.VIDEO_CLIP
                provider_name = clip.provider
            else:
                try:
                    img = self._image_gen.generate(
                        _image_request(scene, story_bible, width, height)
                    )
                except ImageGenerationError as error:
                    raise ScriptToVideoError(
                        error.detail,
                        code=ScriptToVideoErrorCode.STAGE_FAILED,
                        stage=ScriptToVideoStage.ASSETS_READY,
                    ) from error
                raw_bytes = img.data
                media_type = img.media_type
                asset_kind = AssetKind.IMAGE
                provider_name = img.provider

            artifact = self._store_scene_visual_artifact(
                content_item_id=item.id,
                data=raw_bytes,
                media_type=media_type,
            )
            asset = self._asset_registry.register(
                item.channel_id,
                asset_kind,
                AssetCategory.GENERATED,
                title=title,
                source=source_id,
                artifact_id=artifact.id,
                license_ref="generated-internal-v1",
                owner=item.channel_id,
                actor=actor,
            )
            last_job = getattr(self._image_gen, "last_job", None)
            job_suffix = ""
            if last_job is not None and hasattr(last_job, "prompt_id"):
                job_suffix = (
                    f" (prompt_id={last_job.prompt_id}, "
                    f"seed={getattr(last_job, 'seed', '')}, "
                    f"checkpoint={getattr(last_job, 'checkpoint', '')})"
                )
            prov = self._provenance.record(
                asset.id,
                license_name="Internal Generated Story Visual",
                license_ref="generated-internal-v1",
                owner=item.channel_id,
                file_sha256=artifact.sha256,
                proof=(
                    f"Generated by {provider_name} for "
                    f"{story_bible.story_id}/{scene.scene_id}{job_suffix}"
                ),
                actor=actor,
            )
            self._asset_registry.attach(
                asset.id,
                item.id,
                purpose=f"scene:{scene.scene_id}:background",
                actor=actor,
            )
            records.append(
                SceneAssetRecord(
                    scene_id=scene.scene_id,
                    section_index=scene.section_index,
                    asset_id=asset.id,
                    artifact_id=artifact.id,
                    provenance_id=prov.id,
                    media_type=media_type,
                    sha256=artifact.sha256,
                    width=width,
                    height=height,
                    reused=False,
                )
            )
            updated_scenes.append(replace(scene, status=SceneStatus.ASSETS_READY))

        return (
            tuple(records),
            replace(breakdown, scenes=tuple(updated_scenes)),
        )

    def _recorded_audio(
        self, content_item_id: str, script: Script, profile: VoiceProfile
    ) -> AudioArtifactRecord | None:
        """The newest recorded audio of ``script`` with this exact voice profile
        version whose stored file still matches its checksum, else ``None``."""
        with self._database.transaction() as conn:
            audio_repo = AudioMetadataRepository(conn)
            artifacts = ArtifactRepository(conn).list_by_content_item(
                content_item_id, kind=ArtifactKind.AUDIO
            )
            for artifact in reversed(artifacts):
                audio = audio_repo.get_by_artifact(artifact.id)
                if (
                    audio is not None
                    and audio.script_id == script.id
                    and audio.voice_profile_id == profile.id
                    and audio.voice_profile_version == profile.version
                ):
                    break
            else:
                return None
        try:
            data = self._storage.read(artifact.uri)
        except Exception:
            return None
        if hashlib.sha256(data).hexdigest() != artifact.sha256:
            return None
        return AudioArtifactRecord(
            artifact=artifact, audio=audio, created=False, invalidation=None
        )

    def _try_reuse_scene_asset(
        self,
        *,
        item: ContentItem,
        scene: ScenePlan,
        source: str,
        title: str,
        width: int,
        height: int,
    ) -> SceneAssetRecord | None:
        with self._database.transaction() as conn:
            assets_repo = AssetRepository(conn)
            artifacts_repo = ArtifactRepository(conn)
            provs_repo = ProvenanceRepository(conn)
            attached = assets_repo.list_by_content_item(item.id)
            for asset in attached:
                if (
                    asset.source == source
                    and asset.title == title
                    and asset.artifact_id
                ):
                    art = artifacts_repo.get(asset.artifact_id)
                    prov = provs_repo.latest(asset.id)
                    if art is not None and prov is not None:
                        try:
                            data = self._storage.read(art.uri)
                            if len(data) == art.size_bytes:
                                return SceneAssetRecord(
                                    scene_id=scene.scene_id,
                                    section_index=scene.section_index,
                                    asset_id=asset.id,
                                    artifact_id=art.id,
                                    provenance_id=prov.id,
                                    media_type=art.media_type,
                                    sha256=art.sha256,
                                    width=width,
                                    height=height,
                                    reused=True,
                                )
                        except Exception:
                            return None
        return None

    def _store_scene_visual_artifact(
        self,
        *,
        content_item_id: str,
        data: bytes,
        media_type: str,
    ) -> Artifact:
        """Persist a scene visual file in ArtifactStorage and ArtifactRepository."""
        checksum = hashlib.sha256(data).hexdigest()
        with self._database.transaction() as conn:
            artifacts = ArtifactRepository(conn)
            existing = [
                a
                for a in artifacts.list_by_content_item(content_item_id)
                if a.kind is ArtifactKind.THUMBNAIL
                and a.sha256 == checksum
                and a.media_type == media_type
            ]
            if existing:
                return existing[-1]
            latest = artifacts.latest(content_item_id, ArtifactKind.THUMBNAIL)
            version = (latest.version + 1) if latest is not None else 1
            written = self._storage.write(
                content_item_id,
                ArtifactKind.THUMBNAIL,
                version,
                data,
                media_type=media_type,
            )
            read_back = self._storage.read(written.uri)
            if not read_back or hashlib.sha256(read_back).hexdigest() != written.sha256:
                raise ScriptToVideoError(
                    "stored scene visual artifact is empty or failed checksum check",
                    code=ScriptToVideoErrorCode.STAGE_FAILED,
                    stage=ScriptToVideoStage.ASSETS_READY,
                )
            if media_type.startswith("image/"):
                from ai_youtube_agent.providers.media_probe import (
                    MediaProbeError,
                    probe_image_bytes,
                )

                try:
                    probe_image_bytes(read_back)
                except MediaProbeError as exc:
                    raise ScriptToVideoError(
                        f"stored scene visual artifact is unreadable: {exc}",
                        code=ScriptToVideoErrorCode.STAGE_FAILED,
                        stage=ScriptToVideoStage.ASSETS_READY,
                    ) from exc
            if latest is None:
                art = Artifact.create(
                    content_item_id,
                    ArtifactKind.THUMBNAIL,
                    uri=written.uri,
                    sha256=written.sha256,
                    size_bytes=written.size_bytes,
                    media_type=media_type,
                    clock=self._clock,
                )
            else:
                art = latest.next_version(
                    uri=written.uri,
                    sha256=written.sha256,
                    size_bytes=written.size_bytes,
                    media_type=media_type,
                    clock=self._clock,
                )
            artifacts.add(art)
            return art

    def _produce_shorts(
        self,
        *,
        project_id: str,
        item: ContentItem,
        strategy: StrategyProfile,
        story_bible: StoryBible,
        scene_assets: tuple[SceneAssetRecord, ...],
        subtitles_enabled: bool,
        actor: Actor,
        job: AIJob,
        completed_stages: list[ScriptToVideoStage],
    ) -> tuple[Artifact | None, Artifact, Artifact, Artifact]:
        proj = self._shorts_service.get(project_id)
        updated_scenes = tuple(
            replace(
                scene,
                assets=(
                    SceneAsset(
                        asset_id=sa.asset_id,
                        role=AssetRole.BACKGROUND,
                        fit=AssetFit.COVER,
                        provenance_id=sa.provenance_id,
                    ),
                ),
            )
            for scene, sa in zip(proj.timeline.scenes, scene_assets, strict=True)
        )
        proj = self._shorts_service.save_timeline(
            proj.id,
            replace(proj.timeline, scenes=updated_scenes),
            expected_version=proj.version,
            actor=actor,
        )

        if proj.config.captions:
            proj = self._shorts_service.caption(
                proj.id, expected_version=proj.version, actor=actor
            )

        job = self._checkpoint(
            job,
            ScriptToVideoStage.TIMELINE_BUILT,
            {
                "project_id": proj.id,
                "project_version": proj.version,
                "caption_count": len(proj.timeline.captions),
            },
            completed_stages,
        )

        subtitle_art: Artifact | None = None
        if proj.config.captions:
            sub_res = self._shorts_subtitles.record(
                proj.id, expected_version=proj.version, actor=actor
            )
            subtitle_art = sub_res.artifact
            job = self._checkpoint(
                job,
                ScriptToVideoStage.SUBTITLES_GENERATED,
                {"subtitle_artifact_id": subtitle_art.id},
                completed_stages,
            )

        thumb_res = self._shorts_thumbnail.generate(
            proj.id,
            title_text=story_bible.title,
            expected_version=proj.version,
            actor=actor,
        )
        thumb_art = thumb_res.artifact
        job = self._checkpoint(
            job,
            ScriptToVideoStage.THUMBNAIL_GENERATED,
            {"thumbnail_artifact_id": thumb_art.id},
            completed_stages,
        )

        meta_lang = (
            strategy.languages.primary
            if strategy.languages is not None
            else story_bible.language
        )
        description = (
            f"{story_bible.story_arc.logline}\n\n"
            f"Visual style: {story_bible.visual_style}\n#Shorts"
        )
        meta_res = self._shorts_metadata.record(
            proj.id,
            title=story_bible.title,
            description=description,
            tags=("Shorts", "StoryVideo", "Narrative"),
            language=meta_lang,
            expected_version=proj.version,
            actor=actor,
        )
        meta_art = meta_res.artifact

        self._advance_item_status(item, ContentStatus.GENERATING)
        render_res = self._shorts_render.render(
            proj.id, expected_version=proj.version, actor=actor, take_over=True
        )
        video_art = render_res.artifact
        job = self._checkpoint(
            job,
            ScriptToVideoStage.AUDIO_MIXED,
            {"project_id": proj.id},
            completed_stages,
        )
        job = self._checkpoint(
            job,
            ScriptToVideoStage.VIDEO_RENDERED,
            {
                "video_artifact_id": video_art.id,
                "video_sha256": video_art.sha256,
                "size_bytes": video_art.size_bytes,
            },
            completed_stages,
        )

        # Verify complete bundle integrity
        self._shorts_bundle.build(proj.id, expected_version=proj.version, actor=actor)
        return subtitle_art, thumb_art, meta_art, video_art

    def _produce_longform(
        self,
        *,
        project_id: str,
        item: ContentItem,
        strategy: StrategyProfile,
        story_bible: StoryBible,
        scene_assets: tuple[SceneAssetRecord, ...],
        subtitles_enabled: bool,
        actor: Actor,
        job: AIJob,
        completed_stages: list[ScriptToVideoStage],
    ) -> tuple[Artifact | None, Artifact, Artifact, Artifact]:
        proj = self._longform_service.get(project_id)
        assignments = [
            BrollAssignment(
                scene_index=idx,
                asset_id=sa.asset_id,
                role=AssetRole.BACKGROUND,
                crop=CropMode.COVER,
                provenance_id=sa.provenance_id,
            )
            for idx, sa in enumerate(scene_assets)
        ]
        proj = self._broll.assign(
            proj.id,
            assignments,
            expected_version=proj.version,
            actor=actor,
            replace_role=True,
        )

        if proj.config.chapters and not proj.timeline.chapters:
            proj = self._longform_service.generate_chapters(
                proj.id, expected_version=proj.version, actor=actor
            )

        if proj.config.captions:
            proj = self._longform_service.caption(
                proj.id, expected_version=proj.version, actor=actor
            )

        job = self._checkpoint(
            job,
            ScriptToVideoStage.TIMELINE_BUILT,
            {
                "project_id": proj.id,
                "project_version": proj.version,
                "chapter_count": len(proj.timeline.chapters),
                "caption_count": len(proj.timeline.captions),
            },
            completed_stages,
        )

        subtitle_art: Artifact | None = None
        if proj.config.captions:
            sub_res = self._longform_subtitles.record(
                proj.id, expected_version=proj.version, actor=actor
            )
            subtitle_art = sub_res.artifact
            job = self._checkpoint(
                job,
                ScriptToVideoStage.SUBTITLES_GENERATED,
                {"subtitle_artifact_id": subtitle_art.id},
                completed_stages,
            )

        thumb_res = self._longform_thumbnail.generate(
            proj.id,
            title_text=story_bible.title,
            expected_version=proj.version,
            actor=actor,
        )
        thumb_art = thumb_res.artifact
        job = self._checkpoint(
            job,
            ScriptToVideoStage.THUMBNAIL_GENERATED,
            {"thumbnail_artifact_id": thumb_art.id},
            completed_stages,
        )

        description = (
            f"{story_bible.story_arc.logline}\n\n"
            f"Opening: {story_bible.story_arc.opening_situation}\n"
            f"Visual style: {story_bible.visual_style}"
        )
        meta_res = self._longform_metadata.record(
            proj.id,
            title=story_bible.title,
            description=description,
            tags=("StoryVideo", "Narrative", "Cinematic"),
            expected_version=proj.version,
            actor=actor,
        )
        meta_art = meta_res.artifact

        self._advance_item_status(item, ContentStatus.GENERATING)
        render_res = self._longform_render.render(
            proj.id, expected_version=proj.version, actor=actor, take_over=True
        )
        video_art = render_res.artifact
        job = self._checkpoint(
            job,
            ScriptToVideoStage.AUDIO_MIXED,
            {"project_id": proj.id},
            completed_stages,
        )
        job = self._checkpoint(
            job,
            ScriptToVideoStage.VIDEO_RENDERED,
            {
                "video_artifact_id": video_art.id,
                "video_sha256": video_art.sha256,
                "size_bytes": video_art.size_bytes,
            },
            completed_stages,
        )

        self._longform_bundle.build(proj.id, expected_version=proj.version, actor=actor)
        return subtitle_art, thumb_art, meta_art, video_art


__all__ = [
    "DEFAULT_PROVISION_USER",
    "DEFAULT_SYSTEM_ACTOR",
    "ProviderCapabilityReport",
    "SCRIPT_TO_VIDEO_JOB_KIND",
    "SceneAssetRecord",
    "ScriptToVideoError",
    "ScriptToVideoErrorCode",
    "ScriptToVideoRequest",
    "ScriptToVideoService",
    "ScriptToVideoStage",
    "VideoProjectManifest",
]
