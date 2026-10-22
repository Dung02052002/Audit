"""QC Test Harness (Prompt Pack v8, prompt K-116), context C10 Quality (Test & QC).

``VideoArtifactTestHarness`` provides a reusable, extensible inspection harness
for rendered video artifacts:
- Registers and runs discrete ``VideoCheck`` functions against a
  ``VideoInspectionContext``.
- Integrates the Phase K video and artifact checks:
  - ``video.container`` (#117)
  - ``video.resolution`` (#118)
  - ``video.fps`` (#119)
  - ``video.duration`` (#120)
  - ``video.audio`` (#121)
  - ``video.subtitles`` (#122)
  - ``video.black_frames`` (#123)
  - ``video.integrity`` (#124)
  - ``video.metadata`` (#125)
- Builds and records an immutable ``QCResult`` entity (with derived worst-of status)
  into ``QCResultRepository`` and audits ``qc.evaluated``.
"""

from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from enum import StrEnum
from http import HTTPStatus
from typing import Final

from ai_youtube_agent.content.disclosure import DisclosureDecision
from ai_youtube_agent.content.qc import QCCheck, QCResult, QCStatus
from ai_youtube_agent.content.qc_video_checks import (
    AUDIO_CHECK_NAME,
    BLACK_FRAME_CHECK_NAME,
    CONTAINER_CHECK_NAME,
    DURATION_CHECK_NAME,
    FPS_CHECK_NAME,
    INTEGRITY_CHECK_NAME,
    METADATA_CHECK_NAME,
    RESOLUTION_CHECK_NAME,
    SUBTITLE_CHECK_NAME,
    VideoInspectionContext,
    check_audio,
    check_black_frames,
    check_container,
    check_duration,
    check_fps,
    check_metadata,
    check_render_integrity,
    check_resolution,
    check_subtitles,
)
from ai_youtube_agent.core.artifact import Artifact, ArtifactKind
from ai_youtube_agent.core.artifact_storage import ArtifactStorage
from ai_youtube_agent.core.audit import (
    Actor,
    AuditLog,
    AuditResult,
    EntityRef,
)
from ai_youtube_agent.core.content_item import ContentItem, ContentType
from ai_youtube_agent.core.db.database import Database
from ai_youtube_agent.core.db.repositories.channel import StrategyProfileRepository
from ai_youtube_agent.core.db.repositories.content import (
    ArtifactRepository,
    AudioMetadataRepository,
    ContentItemRepository,
)
from ai_youtube_agent.core.db.repositories.disclosure import DisclosureRepository
from ai_youtube_agent.core.db.repositories.jobs import AIJobRepository
from ai_youtube_agent.core.db.repositories.longform import LongFormProjectRepository
from ai_youtube_agent.core.db.repositories.provenance import ProvenanceRepository
from ai_youtube_agent.core.db.repositories.review import QCResultRepository
from ai_youtube_agent.core.db.repositories.shorts import ShortsProjectRepository
from ai_youtube_agent.core.errors import DomainError
from ai_youtube_agent.core.log import get_logger

logger = get_logger(__name__)

QC_EVALUATED: Final[str] = "qc.evaluated"

Clock = Callable[[], datetime]
VideoCheck = Callable[[VideoInspectionContext], QCCheck]


class QCHarnessErrorCode(StrEnum):
    HARNESS = "domain.qc_harness"
    ITEM_NOT_FOUND = "domain.qc_item_not_found"
    VIDEO_MISSING = "domain.qc_video_missing"
    VIDEO_CORRUPTED = "domain.qc_video_corrupted"
    STRATEGY_MISSING = "domain.qc_strategy_missing"


USER_MESSAGES: Final[dict[QCHarnessErrorCode, str]] = {
    QCHarnessErrorCode.HARNESS: "The video QC inspection could not be completed.",
    QCHarnessErrorCode.ITEM_NOT_FOUND: "The content item does not exist.",
    QCHarnessErrorCode.VIDEO_MISSING: (
        "No rendered video artifact exists for this item."
    ),
    QCHarnessErrorCode.VIDEO_CORRUPTED: "The video artifact data could not be read.",
    QCHarnessErrorCode.STRATEGY_MISSING: (
        "The channel has no strategy profile format settings."
    ),
}


class QCHarnessError(DomainError):
    default_code = QCHarnessErrorCode.HARNESS.value
    default_user_message = USER_MESSAGES[QCHarnessErrorCode.HARNESS]
    default_http_status = HTTPStatus.UNPROCESSABLE_ENTITY

    def __init__(
        self,
        detail: str = "",
        *,
        code: QCHarnessErrorCode = QCHarnessErrorCode.HARNESS,
        http_status: int | None = None,
    ) -> None:
        status = (
            http_status
            if http_status is not None
            else (
                HTTPStatus.NOT_FOUND
                if code
                in (
                    QCHarnessErrorCode.ITEM_NOT_FOUND,
                    QCHarnessErrorCode.VIDEO_MISSING,
                )
                else HTTPStatus.UNPROCESSABLE_ENTITY
            )
        )
        super().__init__(
            detail,
            code=code.value,
            user_message=USER_MESSAGES.get(
                code, USER_MESSAGES[QCHarnessErrorCode.HARNESS]
            ),
            http_status=status,
        )


def default_video_checks() -> dict[str, VideoCheck]:
    """Foundational MP4 container and stream checks (K-117 through K-120)."""
    return {
        CONTAINER_CHECK_NAME: check_container,
        RESOLUTION_CHECK_NAME: check_resolution,
        FPS_CHECK_NAME: check_fps,
        DURATION_CHECK_NAME: check_duration,
    }


def all_video_checks() -> dict[str, VideoCheck]:
    """Complete technical and artifact check suite (K-117 through K-125)."""
    return {
        CONTAINER_CHECK_NAME: check_container,
        RESOLUTION_CHECK_NAME: check_resolution,
        FPS_CHECK_NAME: check_fps,
        DURATION_CHECK_NAME: check_duration,
        AUDIO_CHECK_NAME: check_audio,
        SUBTITLE_CHECK_NAME: check_subtitles,
        BLACK_FRAME_CHECK_NAME: check_black_frames,
        INTEGRITY_CHECK_NAME: check_render_integrity,
        METADATA_CHECK_NAME: check_metadata,
    }


class VideoArtifactTestHarness:
    """Test harness for inspecting rendered video artifacts and recording QC results."""

    def __init__(
        self,
        database: Database,
        storage: ArtifactStorage,
        audit: AuditLog,
        checks: Mapping[str, VideoCheck] | None = None,
        *,
        clock: Clock | None = None,
    ) -> None:
        self._database = database
        self._storage = storage
        self._audit = audit
        if checks is not None:
            self._checks: dict[str, VideoCheck] = dict(checks)
            self._default_names: list[str] = list(checks.keys())
        else:
            self._checks = all_video_checks()
            self._default_names = list(default_video_checks().keys())
        self._clock = clock or (lambda: datetime.now(UTC))

    @property
    def registered_checks(self) -> dict[str, VideoCheck]:
        return dict(self._checks)

    def register_check(self, name: str, check_fn: VideoCheck) -> None:
        """Register a new video QC inspection check."""
        self._checks[name] = check_fn
        if name not in self._default_names:
            self._default_names.append(name)

    def run_checks(
        self,
        context: VideoInspectionContext,
        check_names: Sequence[str] | None = None,
    ) -> list[QCCheck]:
        """Execute registered video checks against the inspection context."""
        selected_names = (
            list(check_names) if check_names is not None else list(self._default_names)
        )
        results: list[QCCheck] = []
        for name in selected_names:
            if name not in self._checks:
                raise ValueError(f"unknown QC check {name!r}")
            check_fn = self._checks[name]
            results.append(check_fn(context))
        return results

    def build_inspection_context(
        self,
        content_item_id: str,
        *,
        video_artifact_id: str | None = None,
        actual_width: int | None = None,
        actual_height: int | None = None,
        actual_fps: float | None = None,
        expected_duration_ms: int | None = None,
        frame_luma_samples: Sequence[float] | None = None,
        frame_hashes: Sequence[str] | None = None,
    ) -> tuple[ContentItem, list[Artifact], VideoInspectionContext]:
        """Load item, project, and artifact files into a ``VideoInspectionContext``."""
        with self._database.transaction() as connection:
            items = ContentItemRepository(connection)
            item = items.get(content_item_id)
            if item is None:
                raise QCHarnessError(
                    f"content item {content_item_id} not found",
                    code=QCHarnessErrorCode.ITEM_NOT_FOUND,
                )

            artifacts_repo = ArtifactRepository(connection)
            if video_artifact_id is not None:
                video = artifacts_repo.get(video_artifact_id)
            else:
                video = artifacts_repo.latest(item.id, ArtifactKind.VIDEO)

            if video is None or video.content_item_id != item.id:
                raise QCHarnessError(
                    f"video artifact not found for item {content_item_id}",
                    code=QCHarnessErrorCode.VIDEO_MISSING,
                )

            audio = artifacts_repo.latest(item.id, ArtifactKind.AUDIO)
            subtitles = artifacts_repo.latest(item.id, ArtifactKind.SUBTITLES)
            thumbnail = artifacts_repo.latest(item.id, ArtifactKind.THUMBNAIL)
            metadata_art = artifacts_repo.latest(item.id, ArtifactKind.METADATA)

            audio_duration_ms = 0
            if audio is not None:
                audio_meta = AudioMetadataRepository(connection).get_by_artifact(
                    audio.id
                )
                if audio_meta is not None:
                    audio_duration_ms = audio_meta.duration_ms

            strategies = StrategyProfileRepository(connection)
            strategy = strategies.get_by_channel(item.channel_id) or strategies.get(
                item.strategy_profile_id
            )
            if strategy is None or strategy.format is None:
                raise QCHarnessError(
                    f"strategy format missing for channel {item.channel_id}",
                    code=QCHarnessErrorCode.STRATEGY_MISSING,
                )

            expected_language = (
                strategy.languages.primary if strategy.languages is not None else None
            )

            format_setting = strategy.format
            captions_enabled = True
            expected_caption_count: int | None = None
            scene_windows_ms: tuple[tuple[int, int], ...] | None = None
            expected_project_id: str | None = None
            expected_project_version: int | None = None
            expected_timeline_sha256: str | None = None
            expected_script_id: str | None = None
            expected_script_version: int | None = None
            expected_chapters_enabled: bool | None = None
            expected_chapter_count: int | None = None
            expected_attributions: list[str] = []

            if item.content_type == ContentType.SHORTS:
                fmt = format_setting.shorts
                expected_width, expected_height = 1080, 1920
                if fmt.resolution.value == "720p":
                    expected_width, expected_height = 720, 1280
                elif fmt.resolution.value == "2160p":
                    expected_width, expected_height = 2160, 3840
                min_duration_ms = fmt.min_seconds * 1000
                max_duration_ms = fmt.max_seconds * 1000
                expected_fps = 30
                captions_enabled = fmt.captions

                shorts_proj = ShortsProjectRepository(connection).get_by_content_item(
                    item.id
                )
                if shorts_proj is not None:
                    expected_width, expected_height = shorts_proj.config.canvas_size
                    expected_fps = shorts_proj.config.fps
                    captions_enabled = shorts_proj.config.captions
                    if audio_duration_ms <= 0:
                        audio_duration_ms = shorts_proj.audio_duration_ms
                    expected_project_id = shorts_proj.id
                    expected_project_version = shorts_proj.version
                    expected_timeline_sha256 = shorts_proj.timeline.sha256
                    expected_script_id = shorts_proj.script_id
                    expected_script_version = shorts_proj.script_version
                    expected_caption_count = len(shorts_proj.timeline.captions)
                    scene_windows_ms = tuple(
                        (s.start_ms, s.end_ms) for s in shorts_proj.timeline.scenes
                    )
                    asset_ids = {
                        sa.asset_id
                        for sc in shorts_proj.timeline.scenes
                        for sa in sc.assets
                    } | {m.asset_id for m in shorts_proj.timeline.music}
                    provs = ProvenanceRepository(connection)
                    for aid in sorted(asset_ids):
                        p = provs.latest(aid)
                        if (
                            p is not None
                            and p.attribution
                            and p.attribution not in expected_attributions
                        ):
                            expected_attributions.append(p.attribution)
            else:
                fmt = format_setting.longform
                expected_width, expected_height = 1920, 1080
                if fmt.resolution.value == "720p":
                    expected_width, expected_height = 1280, 720
                elif fmt.resolution.value == "2160p":
                    expected_width, expected_height = 3840, 2160
                min_duration_ms = fmt.min_seconds * 1000
                max_duration_ms = fmt.max_seconds * 1000
                expected_fps = 30
                captions_enabled = fmt.captions
                expected_chapters_enabled = fmt.chapters

                lf_proj = LongFormProjectRepository(connection).get_by_content_item(
                    item.id
                )
                if lf_proj is not None:
                    expected_width, expected_height = lf_proj.config.canvas_size
                    expected_fps = lf_proj.config.fps
                    captions_enabled = lf_proj.config.captions
                    expected_chapters_enabled = lf_proj.config.chapters
                    if audio_duration_ms <= 0:
                        audio_duration_ms = lf_proj.audio_duration_ms
                    expected_project_id = lf_proj.id
                    expected_project_version = lf_proj.version
                    expected_timeline_sha256 = lf_proj.timeline.sha256
                    expected_script_id = lf_proj.script_id
                    expected_script_version = lf_proj.script_version
                    expected_caption_count = len(lf_proj.timeline.captions)
                    expected_chapter_count = len(lf_proj.timeline.chapters)
                    scene_windows_ms = tuple(
                        (s.start_ms, s.end_ms) for s in lf_proj.timeline.scenes
                    )
                    asset_ids = {
                        sa.asset_id
                        for sc in lf_proj.timeline.scenes
                        for sa in sc.assets
                    } | {m.asset_id for m in lf_proj.timeline.music}
                    provs = ProvenanceRepository(connection)
                    for aid in sorted(asset_ids):
                        p = provs.latest(aid)
                        if (
                            p is not None
                            and p.attribution
                            and p.attribution not in expected_attributions
                        ):
                            expected_attributions.append(p.attribution)

            disclosure = DisclosureRepository(connection).latest(item.id)
            expected_disclosure_id = disclosure.id if disclosure is not None else None
            expected_synthetic_media = (
                disclosure.decision is DisclosureDecision.REQUIRED
                if disclosure is not None
                else None
            )

            render_job_status: str | None = None
            render_job_video_sha256: str | None = None
            render_job_timeline_sha256: str | None = None
            jobs = AIJobRepository(connection).list_by_content_item(item.id)
            render_jobs = [j for j in jobs if j.kind == "video.render"]
            if render_jobs:
                latest_job = render_jobs[-1]
                render_job_status = latest_job.status.value
                for cp in reversed(latest_job.checkpoints):
                    if cp.step == "video.rendered":
                        v_sha = cp.data.get("video_sha256") or cp.data.get("sha256")
                        t_sha = cp.data.get("timeline_sha256")
                        if isinstance(v_sha, str):
                            render_job_video_sha256 = v_sha
                        if isinstance(t_sha, str):
                            render_job_timeline_sha256 = t_sha
                        break

        try:
            video_data = self._storage.read(video.uri)
        except Exception as error:
            raise QCHarnessError(
                f"could not read video artifact {video.id}: {error}",
                code=QCHarnessErrorCode.VIDEO_CORRUPTED,
            ) from error

        audio_data: bytes | None = None
        if audio is not None:
            try:
                audio_data = self._storage.read(audio.uri)
            except Exception:
                audio_data = b""

        subtitle_data: bytes | None = None
        if subtitles is not None:
            try:
                subtitle_data = self._storage.read(subtitles.uri)
            except Exception:
                subtitle_data = b""

        metadata_data: bytes | None = None
        if metadata_art is not None:
            try:
                metadata_data = self._storage.read(metadata_art.uri)
            except Exception:
                metadata_data = b""

        bound_artifacts: list[Artifact] = [video]
        for art in (audio, subtitles, thumbnail, metadata_art):
            if art is not None:
                bound_artifacts.append(art)

        resolved_expected_duration_ms = (
            expected_duration_ms
            if expected_duration_ms is not None
            else audio_duration_ms
        )

        context = VideoInspectionContext(
            content_item_id=item.id,
            content_type=item.content_type,
            video_data=video_data,
            expected_width=expected_width,
            expected_height=expected_height,
            expected_fps=expected_fps,
            expected_duration_ms=resolved_expected_duration_ms,
            min_duration_ms=min_duration_ms,
            max_duration_ms=max_duration_ms,
            actual_width=actual_width,
            actual_height=actual_height,
            actual_fps=actual_fps,
            audio_data=audio_data,
            audio_sha256=audio.sha256 if audio is not None else None,
            captions_enabled=captions_enabled,
            subtitle_data=subtitle_data,
            subtitle_media_type=subtitles.media_type if subtitles is not None else None,
            expected_caption_count=expected_caption_count,
            scene_windows_ms=scene_windows_ms,
            frame_luma_samples=frame_luma_samples,
            frame_hashes=frame_hashes,
            expected_video_sha256=video.sha256,
            expected_video_size=video.size_bytes,
            render_job_status=render_job_status,
            render_job_video_sha256=render_job_video_sha256,
            render_job_timeline_sha256=render_job_timeline_sha256,
            current_timeline_sha256=expected_timeline_sha256,
            metadata_data=metadata_data,
            expected_project_id=expected_project_id,
            expected_project_version=expected_project_version,
            expected_timeline_sha256=expected_timeline_sha256,
            expected_script_id=expected_script_id,
            expected_script_version=expected_script_version,
            expected_language=expected_language,
            expected_chapters_enabled=expected_chapters_enabled,
            expected_chapter_count=expected_chapter_count,
            expected_attributions=tuple(expected_attributions),
            expected_disclosure_id=expected_disclosure_id,
            expected_synthetic_media=expected_synthetic_media,
        )
        return item, bound_artifacts, context

    def inspect_and_record(
        self,
        content_item_id: str,
        *,
        video_artifact_id: str | None = None,
        actor: Actor,
        check_names: Sequence[str] | None = None,
        actual_width: int | None = None,
        actual_height: int | None = None,
        actual_fps: float | None = None,
        frame_luma_samples: Sequence[float] | None = None,
        frame_hashes: Sequence[str] | None = None,
    ) -> QCResult:
        """Inspect the video artifact, construct QCResult, store it, and audit."""
        item, bound_artifacts, context = self.build_inspection_context(
            content_item_id,
            video_artifact_id=video_artifact_id,
            actual_width=actual_width,
            actual_height=actual_height,
            actual_fps=actual_fps,
            frame_luma_samples=frame_luma_samples,
            frame_hashes=frame_hashes,
        )
        video = bound_artifacts[0]
        checks = self.run_checks(context, check_names=check_names)

        result = QCResult.create(
            content_item_id=item.id,
            artifacts=[video],
            checks=checks,
            clock=self._clock,
        )

        with self._database.transaction() as connection:
            QCResultRepository(connection).add(result)

        self._audit.record(
            QC_EVALUATED,
            actor,
            EntityRef("content_item", item.id),
            AuditResult.SUCCESS,
            {
                "qc_result_id": result.id,
                "overall_status": result.status.value,
                "checks_count": len(result.checks),
                "pass_count": sum(c.status == QCStatus.PASS for c in result.checks),
                "warn_count": sum(c.status == QCStatus.WARN for c in result.checks),
                "fail_count": sum(c.status == QCStatus.FAIL for c in result.checks),
                "video_artifact_id": video.id,
            },
        )

        return result
