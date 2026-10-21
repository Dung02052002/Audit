"""LongForm render (Prompt Pack v8, prompt J-111), context C9 LongForm Production.

``LongFormRenderService.render`` turns a ``ready`` LongForm project into a horizontal
MP4 (16:9) through the ``VideoRenderer`` provider (``providers/video_render.py``) and
stores it as the next version of the item's ``VIDEO`` ``Artifact``.

Key invariants & rules:
- Guard: FeatureFlags.longform_enabled must be True, else LongFormDisabledError.
- Inputs verified in one read transaction:
  - LongForm project exists, expected_version matches, status earns READY.
  - Script and audio bindings are current (longform_project_is_current).
  - All visual assets and fonts pass check_longform_timeline_assets (D5).
  - Provenance snapshots are verified (asset_changed / ASSET_CHANGED if drifted).
- Audio is mixed in-memory via LongFormAudioMixer.mix on every render call.
- Visual files read via VisualSource (default ArtifactVisualSource).
- Build render request: 16:9 canvas, scene visual layers (background, b-roll, overlays),
  caption text lines, audio mix.
- Idempotency & AIJob reuse: kind video.render with render_key. Succeeded duplicate replayed.
- Video rendered through VideoRenderer.render outside transaction.
- Output validated with probe_mp4; duration matched against audio within tolerance.
- Written to ArtifactStorage, ArtifactKind.VIDEO created, stale approvals invalidated,
  job succeeded with video.rendered checkpoint.
- Audits longform.rendered on success, longform.render_failed on failure.
"""

import hashlib
import json
import sqlite3
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from http import HTTPStatus
from typing import Final, Protocol, runtime_checkable

from ai_youtube_agent.content.asset import Asset
from ai_youtube_agent.content.longform_audio_mix import (
    LongFormAudioMixer,
)
from ai_youtube_agent.content.longform_captions import caption_box
from ai_youtube_agent.content.longform_project import (
    LongFormProject,
    LongFormProjectStatus,
)
from ai_youtube_agent.content.longform_script_generator import LongFormDisabledError
from ai_youtube_agent.content.longform_service import (
    LongFormProjectError,
    LongFormProjectErrorCode,
    check_longform_timeline_assets,
    longform_project_is_current,
    longform_readiness,
)
from ai_youtube_agent.content.longform_timeline import (
    LongFormTimeline,
)
from ai_youtube_agent.content.shorts_audio_mix import MixOutcome
from ai_youtube_agent.content.shorts_captions import wrap
from ai_youtube_agent.content.shorts_render import (
    ArtifactVisualSource,
    VisualFile,
    VisualSource,
)
from ai_youtube_agent.content.shorts_timeline import (
    AssetRole,
    Box,
    CaptionOverlay,
    Scene,
    SceneAsset,
)
from ai_youtube_agent.core.artifact import Artifact, ArtifactKind
from ai_youtube_agent.core.artifact_storage import ArtifactStorage, StoredFile
from ai_youtube_agent.core.audit import (
    Actor,
    AuditLog,
    AuditResult,
    EntityRef,
    MetadataValue,
)
from ai_youtube_agent.core.db.database import Database
from ai_youtube_agent.core.db.repositories.content import (
    ArtifactRepository,
    ContentItemRepository,
)
from ai_youtube_agent.core.db.repositories.jobs import AIJobRepository
from ai_youtube_agent.core.db.repositories.longform import LongFormProjectRepository
from ai_youtube_agent.core.db.repositories.provenance import ProvenanceRepository
from ai_youtube_agent.core.errors import DomainError
from ai_youtube_agent.core.flags import FeatureFlags
from ai_youtube_agent.core.log import get_logger
from ai_youtube_agent.core.version_invalidation import (
    InvalidationResult,
    invalidate_stale_approvals,
    record_invalidations,
)
from ai_youtube_agent.pipeline.idempotency import RENDER_KIND, render_key
from ai_youtube_agent.pipeline.job import AIJob, AIJobStatus, JobCheckpoint
from ai_youtube_agent.providers.video_render import (
    AUDIO_INPUT_MEDIA_TYPE,
    AUDIO_KEY,
    FONT_MEDIA_TYPES,
    IMAGE_MEDIA_TYPES,
    MP4_MEDIA_TYPE,
    VIDEO_MEDIA_TYPES,
    LayerFit,
    Mp4Info,
    PixelRect,
    RenderedVideo,
    RenderInput,
    RenderRequest,
    TextAlign,
    TextLine,
    VideoRenderer,
    VideoRenderError,
    VisualLayer,
    probe_mp4,
)

logger = get_logger(__name__)

Clock = Callable[[], datetime]

RENDERED: Final[str] = "longform.rendered"
RENDER_FAILED: Final[str] = "longform.render_failed"
RENDERED_STEP: Final[str] = "video.rendered"
RENDER_KEY_VERSION: Final[int] = 1
JOB_KIND: Final[str] = RENDER_KIND
PERMILLE: Final[int] = 1000
VISUAL_MEDIA_TYPES: Final[frozenset[str]] = IMAGE_MEDIA_TYPES | VIDEO_MEDIA_TYPES

# Layer ordering within a scene, bottom to top.
_LAYER_ORDER = {AssetRole.BACKGROUND: 0, AssetRole.B_ROLL: 1, AssetRole.OVERLAY: 2}


class LongFormRenderErrorCode(StrEnum):
    RENDER = "domain.longform_render"
    PROJECT_NOT_FOUND = "domain.longform_render_project_not_found"
    CONFLICT = "domain.longform_render_conflict"
    NOT_READY = "domain.longform_render_not_ready"
    STALE_INPUT = "domain.longform_render_stale_input"
    ASSET_UNAVAILABLE = "domain.longform_render_asset_unavailable"
    ASSET_RIGHTS = "domain.longform_render_asset_rights_unknown"
    ASSET_UNSUPPORTED = "domain.longform_render_asset_unsupported"
    ASSET_CHANGED = "domain.longform_render_asset_changed"
    IN_PROGRESS = "domain.longform_render_in_progress"
    STORAGE_FAILED = "domain.longform_render_storage_failed"
    RENDER_FAILED = "domain.longform_render_failed"
    RENDER_INVALID = "domain.longform_render_invalid"


USER_MESSAGES = {
    LongFormRenderErrorCode.RENDER: "The LongForm video could not be rendered.",
    LongFormRenderErrorCode.PROJECT_NOT_FOUND: "This LongForm project does not exist.",
    LongFormRenderErrorCode.CONFLICT: (
        "The LongForm project changed in the meantime. Reload it and try again."
    ),
    LongFormRenderErrorCode.NOT_READY: (
        "The LongForm project is not ready to render yet. Plan scenes, b-roll, chapters "
        "and captions first."
    ),
    LongFormRenderErrorCode.STALE_INPUT: (
        "The LongForm project is not current with the script or audio. Rebind it first."
    ),
    LongFormRenderErrorCode.ASSET_UNAVAILABLE: (
        "A visual asset or font referenced in the timeline could not be read."
    ),
    LongFormRenderErrorCode.ASSET_RIGHTS: (
        "A visual asset referenced in the timeline has an unknown rights status."
    ),
    LongFormRenderErrorCode.ASSET_UNSUPPORTED: (
        "A visual asset referenced in the timeline has an unsupported media type."
    ),
    LongFormRenderErrorCode.ASSET_CHANGED: (
        "The provenance of an asset changed after the timeline was saved. "
        "Update the timeline."
    ),
    LongFormRenderErrorCode.IN_PROGRESS: (
        "A render for this LongForm video is already running. Wait for it or take it over."
    ),
    LongFormRenderErrorCode.STORAGE_FAILED: (
        "The rendered video could not be written to storage."
    ),
    LongFormRenderErrorCode.RENDER_FAILED: (
        "The video rendering provider failed to produce an MP4."
    ),
    LongFormRenderErrorCode.RENDER_INVALID: (
        "The rendered video is not a valid MP4 or its duration does not match the project."
    ),
}

HTTP_STATUS = {
    LongFormRenderErrorCode.PROJECT_NOT_FOUND: HTTPStatus.NOT_FOUND,
    LongFormRenderErrorCode.CONFLICT: HTTPStatus.CONFLICT,
    LongFormRenderErrorCode.NOT_READY: HTTPStatus.CONFLICT,
    LongFormRenderErrorCode.STALE_INPUT: HTTPStatus.CONFLICT,
    LongFormRenderErrorCode.IN_PROGRESS: HTTPStatus.CONFLICT,
    LongFormRenderErrorCode.ASSET_CHANGED: HTTPStatus.CONFLICT,
}

_PROJECT_CODES = LongFormProjectErrorCode
ASSET_CODES = {
    _PROJECT_CODES.ASSET_NOT_FOUND.value: LongFormRenderErrorCode.ASSET_UNAVAILABLE,
    _PROJECT_CODES.ASSET_OTHER_CHANNEL.value: LongFormRenderErrorCode.ASSET_UNAVAILABLE,
    _PROJECT_CODES.ASSET_NOT_ATTACHED.value: LongFormRenderErrorCode.ASSET_UNAVAILABLE,
    _PROJECT_CODES.ASSET_RIGHTS_UNKNOWN.value: LongFormRenderErrorCode.ASSET_RIGHTS,
    _PROJECT_CODES.ASSET_KIND_MISMATCH.value: LongFormRenderErrorCode.ASSET_UNSUPPORTED,
}


class LongFormRenderError(DomainError):
    """A LongForm render was refused or failed; ``code`` is a ``LongFormRenderErrorCode``."""

    default_code = LongFormRenderErrorCode.RENDER.value
    default_user_message = USER_MESSAGES[LongFormRenderErrorCode.RENDER]

    def __init__(
        self,
        detail: str = "",
        *,
        code: LongFormRenderErrorCode = LongFormRenderErrorCode.RENDER,
    ) -> None:
        super().__init__(detail, code=code.value, user_message=USER_MESSAGES[code])
        if code in HTTP_STATUS:
            self.default_http_status = HTTP_STATUS[code]


@dataclass(frozen=True)
class LongFormRenderResult:
    artifact: Artifact
    created: bool
    input_sha256: str
    job_id: str


def _scaled(size: int, permille: int) -> int:
    """``size * permille / 1000`` in whole pixels, halves rounded up."""
    return (size * permille + PERMILLE // 2) // PERMILLE


def _rect(box: Box | None, width: int, height: int) -> PixelRect:
    if box is None:
        return PixelRect(0, 0, width, height)
    return PixelRect(
        _scaled(width, box.x),
        _scaled(height, box.y),
        max(1, _scaled(width, box.width)),
        max(1, _scaled(height, box.height)),
    )


def visual_placements(timeline: LongFormTimeline) -> list[tuple[Scene, SceneAsset]]:
    """The visual scene assets, scene by scene, bottom layer first."""
    placed: list[tuple[Scene, SceneAsset]] = []
    for scene in timeline.scenes:
        visual = [asset for asset in scene.assets if asset.role in _LAYER_ORDER]
        visual.sort(key=lambda asset: _LAYER_ORDER[asset.role])
        placed.extend((scene, asset) for asset in visual)
    return placed


def caption_font_ids(timeline: LongFormTimeline) -> tuple[str, ...]:
    """The font assets the captions use, sorted."""
    fonts = {caption.style.font_asset_id for caption in timeline.captions}
    return tuple(sorted(font for font in fonts if font is not None))


def _input(key: str, file: VisualFile) -> RenderInput:
    return RenderInput(key, file.data, file.media_type, file.sha256)


def _text_lines(
    project: LongFormProject, font_keys: Mapping[str, str], fallback: str | None
) -> list[TextLine]:
    config = project.config
    lines_out: list[TextLine] = []
    if not config.captions:
        return lines_out
    for caption in project.timeline.captions:
        style = caption.style
        lines = wrap(caption.text, config.caption_rules)
        box = caption_box(lines, caption.position, style, config.safe_rect)
        line_height = box.height // len(lines)
        font_key = font_keys[style.font_asset_id] if style.font_asset_id else fallback
        for index, line in enumerate(lines):
            lines_out.append(
                TextLine(
                    text=line,
                    start_ms=caption.start_ms,
                    end_ms=caption.end_ms,
                    rect=PixelRect(
                        box.x,
                        box.y + index * line_height,
                        box.width,
                        line_height,
                    ),
                    align=TextAlign(style.align.value),
                    size_px=style.size_px,
                    color=style.color,
                    stroke_color=style.stroke_color,
                    stroke_px=style.stroke_px,
                    background=style.background,
                    font_key=font_key,
                )
            )
    return lines_out


def build_longform_render_request(
    project: LongFormProject,
    mix: MixOutcome,
    visual_files: Mapping[str, VisualFile],
    fallback_font_file: VisualFile | None = None,
) -> RenderRequest:
    """Build the deterministic ``RenderRequest`` for a LongForm project."""
    config = project.config
    width, height = config.width, config.height
    inputs: list[RenderInput] = [
        RenderInput(AUDIO_KEY, mix.wav, AUDIO_INPUT_MEDIA_TYPE, mix.sha256)
    ]
    layers: list[VisualLayer] = []
    asset_keys: dict[str, str] = {}

    def _get_key(asset_id: str, file: VisualFile) -> str:
        if asset_id not in asset_keys:
            key = f"v{len(asset_keys) + 1}"
            asset_keys[asset_id] = key
            inputs.append(_input(key, file))
        return asset_keys[asset_id]

    timeline = project.timeline
    for scene, placed in visual_placements(timeline):
        file = visual_files[placed.asset_id]
        key = _get_key(placed.asset_id, file)
        video = file.media_type in VIDEO_MEDIA_TYPES
        end_ms = scene.duration_ms if placed.end_ms is None else placed.end_ms
        layers.append(
            VisualLayer(
                input_key=key,
                start_ms=scene.start_ms + placed.start_ms,
                end_ms=scene.start_ms + end_ms,
                rect=_rect(placed.box, width, height),
                fit=LayerFit(placed.fit.value),
                source_in_ms=placed.source_in_ms if video else None,
                source_out_ms=placed.source_out_ms if video else None,
            )
        )

    font_keys: dict[str, str] = {}
    for font_id in caption_font_ids(timeline):
        file = visual_files[font_id]
        font_keys[font_id] = _get_key(font_id, file)

    fallback_key: str | None = None
    if fallback_font_file is not None:
        fallback_key = "fontdef"
        inputs.append(_input(fallback_key, fallback_font_file))

    texts = _text_lines(project, font_keys, fallback_key)

    return RenderRequest(
        width=width,
        height=height,
        fps=config.fps,
        duration_ms=timeline.duration_ms,
        audio=inputs[0],
        inputs=tuple(inputs[1:]),
        layers=tuple(layers),
        texts=tuple(texts),
    )


def longform_render_input_sha256(
    renderer_name: str,
    project: LongFormProject,
    request: RenderRequest,
    mix_outcome: MixOutcome,
) -> str:
    """Deterministic hash of all render inputs."""
    payload = {
        "version": RENDER_KEY_VERSION,
        "renderer": renderer_name,
        "project_id": project.id,
        "timeline_sha256": project.timeline.sha256,
        "audio_sha256": project.audio_sha256,
        "mix_sha256": mix_outcome.sha256,
        "config": {
            "width": project.config.width,
            "height": project.config.height,
            "fps": project.config.fps,
            "captions": project.config.captions,
            "chapters": project.config.chapters,
        },
        "description": request.describe(),
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


class LongFormRenderService:
    """Coordinates the rendering and artifact recording of a LongForm project."""

    def __init__(
        self,
        database: Database,
        storage: ArtifactStorage,
        audit: AuditLog,
        renderer: VideoRenderer,
        mixer: LongFormAudioMixer,
        visual_source: VisualSource | None = None,
        flags: FeatureFlags | None = None,
        *,
        clock: Clock | None = None,
    ) -> None:
        self._database = database
        self._storage = storage
        self._audit = audit
        self._renderer = renderer
        self._mixer = mixer
        self._visual_source = visual_source or ArtifactVisualSource(database, storage)
        self._flags = (
            flags if flags is not None else FeatureFlags(longform_enabled=True)
        )
        self._clock = clock or (lambda: datetime.now(UTC))

    def _require_enabled(self) -> None:
        if not self._flags.longform_enabled:
            raise LongFormDisabledError("LONGFORM_ENABLED is off")

    def render(
        self,
        project_id: str,
        *,
        expected_version: int,
        actor: Actor,
        take_over: bool = False,
        fallback_font_file: VisualFile | None = None,
    ) -> LongFormRenderResult:
        """Render the LongForm project to MP4 and store the VIDEO artifact."""
        self._require_enabled()

        # Step 1: Read and validate project and assets in read transaction
        with self._database.transaction() as connection:
            projects = LongFormProjectRepository(connection)
            project = projects.get(project_id)
            if project is None:
                raise LongFormRenderError(
                    f"project {project_id} not found",
                    code=LongFormRenderErrorCode.PROJECT_NOT_FOUND,
                )
            if project.version != expected_version:
                raise LongFormRenderError(
                    f"project {project_id} version {project.version} != {expected_version}",
                    code=LongFormRenderErrorCode.CONFLICT,
                )
            if not longform_project_is_current(connection, project):
                raise LongFormRenderError(
                    f"project {project_id} is stale",
                    code=LongFormRenderErrorCode.STALE_INPUT,
                )
            if longform_readiness(project.config, project.timeline) != LongFormProjectStatus.READY:
                raise LongFormRenderError(
                    f"project {project_id} is not ready",
                    code=LongFormRenderErrorCode.NOT_READY,
                )

            item = ContentItemRepository(connection).get(project.content_item_id)
            if item is None:
                raise LongFormRenderError(
                    f"item {project.content_item_id} not found",
                    code=LongFormRenderErrorCode.PROJECT_NOT_FOUND,
                )

            # D5 checks for all timeline assets
            font_ids = caption_font_ids(project.timeline)
            scene_assets = [
                asset
                for scene in project.timeline.scenes
                for asset in scene.assets
            ]
            try:
                assets = check_longform_timeline_assets(
                    connection, item, scene_assets, font_ids
                )
            except LongFormProjectError as error:
                code = ASSET_CODES.get(error.code, LongFormRenderErrorCode.ASSET_UNAVAILABLE)
                raise LongFormRenderError(str(error), code=code) from None

            # D7 provenance checks
            provenances = ProvenanceRepository(connection)
            for placed in scene_assets:
                if placed.provenance_id is not None:
                    latest_prov = provenances.latest(placed.asset_id)
                    if latest_prov is None or latest_prov.id != placed.provenance_id:
                        raise LongFormRenderError(
                            f"asset {placed.asset_id} provenance drifted",
                            code=LongFormRenderErrorCode.ASSET_CHANGED,
                        )

            # Read latest video artifact to determine next version
            latest_video = ArtifactRepository(connection).latest(
                item.id, ArtifactKind.VIDEO
            )
            next_version = 1 if latest_video is None else latest_video.version + 1

        # Step 2: Mix audio (runs mixer and audits audio.mixed)
        mix = self._mixer.mix(
            project_id, expected_version=expected_version, actor=actor
        )

        # Step 3: Read visual files
        visual_files: dict[str, VisualFile] = {}
        for asset_id, asset in assets.items():
            try:
                visual_files[asset_id] = self._visual_source.read_file(asset)
            except Exception as error:
                raise LongFormRenderError(
                    f"could not read asset {asset_id}: {error}",
                    code=LongFormRenderErrorCode.ASSET_UNAVAILABLE,
                ) from error

        # Step 4: Build render request and compute input sha256
        request = build_longform_render_request(
            project, mix, visual_files, fallback_font_file
        )
        input_sha256 = longform_render_input_sha256(
            self._renderer.name, project, request, mix
        )

        # Step 5: Check AIJob idempotency
        idempotency_key = render_key(item.id, input_sha256, next_version)
        with self._database.transaction() as connection:
            jobs = AIJobRepository(connection)
            job = jobs.get_by_idempotency_key(idempotency_key)
            if job is not None:
                if job.status == AIJobStatus.SUCCEEDED and latest_video is not None:
                    # Idempotent replay: return existing artifact
                    return LongFormRenderResult(
                        artifact=latest_video,
                        created=False,
                        input_sha256=input_sha256,
                        job_id=job.id,
                    )
                if job.status == AIJobStatus.RUNNING and not take_over:
                    raise LongFormRenderError(
                        f"render job {job.id} is already in progress",
                        code=LongFormRenderErrorCode.IN_PROGRESS,
                    )
                if job.status == AIJobStatus.RUNNING and take_over:
                    prev_updated = job.updated_at
                    job = job.fail("take_over", clock=self._clock)
                    jobs.update(job, expected_updated_at=prev_updated)

            # Start or resume job
            if job is None:
                job = AIJob.create(
                    JOB_KIND,
                    idempotency_key,
                    content_item_id=item.id,
                    clock=self._clock,
                )
                job = job.start(clock=self._clock)
                jobs.add(job)
            else:
                prev_updated = job.updated_at
                job = job.start(clock=self._clock)
                jobs.update(job, expected_updated_at=prev_updated)

        # Step 6: Render MP4 outside transaction
        try:
            rendered = self._renderer.render(request)
        except VideoRenderError as error:
            with self._database.transaction() as connection:
                jobs = AIJobRepository(connection)
                j = jobs.get(job.id)
                if j is not None and j.status == AIJobStatus.RUNNING:
                    prev_updated = j.updated_at
                    jobs.update(
                        j.fail(error.code, clock=self._clock),
                        expected_updated_at=prev_updated,
                    )
            self._audit.record(
                RENDER_FAILED,
                actor,
                EntityRef("longform_project", project.id),
                AuditResult.FAILURE,
                {
                    "content_item_id": item.id,
                    "error_code": error.code,
                    "renderer": self._renderer.name,
                },
            )
            raise LongFormRenderError(
                f"rendering failed: {error}",
                code=LongFormRenderErrorCode.RENDER_FAILED,
            ) from error

        # Step 7: Probe rendered MP4 and validate duration
        try:
            info = probe_mp4(rendered.data)
        except Exception as error:
            raise LongFormRenderError(
                f"rendered output is not valid MP4: {error}",
                code=LongFormRenderErrorCode.RENDER_INVALID,
            ) from error

        tolerance_ms = max(250, 2000 // project.config.fps)
        if abs(info.duration_ms - project.audio_duration_ms) > tolerance_ms:
            raise LongFormRenderError(
                f"rendered video duration {info.duration_ms} ms differs from "
                f"project duration {project.audio_duration_ms} ms",
                code=LongFormRenderErrorCode.RENDER_INVALID,
            )

        # Step 8: Store MP4 and record artifact in write transaction
        video_sha256 = hashlib.sha256(rendered.data).hexdigest()
        written_file: StoredFile | None = None
        invalidation: InvalidationResult | None = None

        try:
            written_file = self._storage.write(
                item.id,
                ArtifactKind.VIDEO,
                next_version,
                rendered.data,
                media_type=MP4_MEDIA_TYPE,
            )

            with self._database.transaction() as connection:
                # Re-verify project version and currency
                p = LongFormProjectRepository(connection).get(project.id)
                if p is None or p.version != expected_version:
                    raise LongFormRenderError(
                        "project changed concurrently",
                        code=LongFormRenderErrorCode.CONFLICT,
                    )
                if not longform_project_is_current(connection, p):
                    raise LongFormRenderError(
                        "project became stale concurrently",
                        code=LongFormRenderErrorCode.STALE_INPUT,
                    )

                artifacts = ArtifactRepository(connection)
                curr_video = artifacts.latest(item.id, ArtifactKind.VIDEO)
                if curr_video is not None and curr_video.version >= next_version:
                    raise LongFormRenderError(
                        "video version conflict",
                        code=LongFormRenderErrorCode.CONFLICT,
                    )

                if curr_video is None:
                    artifact = Artifact.create(
                        item.id,
                        ArtifactKind.VIDEO,
                        uri=written_file.uri,
                        sha256=video_sha256,
                        size_bytes=len(rendered.data),
                        media_type=MP4_MEDIA_TYPE,
                        clock=self._clock,
                    )
                else:
                    artifact = curr_video.next_version(
                        uri=written_file.uri,
                        sha256=video_sha256,
                        size_bytes=len(rendered.data),
                        media_type=MP4_MEDIA_TYPE,
                        clock=self._clock,
                    )
                artifacts.add(artifact)

                # Invalidate stale approvals (#035)
                invalidation = invalidate_stale_approvals(
                    connection, item.id, clock=self._clock
                )

                # Checkpoint & succeed AIJob
                jobs = AIJobRepository(connection)
                j = jobs.get(job.id)
                if j is not None and j.status == AIJobStatus.RUNNING:
                    prev_updated = j.updated_at
                    j = j.checkpoint(
                        RENDERED_STEP,
                        {
                            "artifact_id": artifact.id,
                            "artifact_version": artifact.version,
                            "sha256": artifact.sha256,
                            "timeline_sha256": project.timeline.sha256,
                            "duration_ms": info.duration_ms,
                        },
                        clock=self._clock,
                    )
                    j = j.succeed(clock=self._clock)
                    jobs.update(j, expected_updated_at=prev_updated)

        except Exception:
            if written_file is not None:
                self._storage.delete(written_file.uri)
            raise

        # Post-commit audit
        if invalidation is not None:
            record_invalidations(self._audit, invalidation)
        self._audit.record(
            RENDERED,
            actor,
            EntityRef("longform_project", project.id),
            AuditResult.SUCCESS,
            {
                "content_item_id": item.id,
                "artifact_id": artifact.id,
                "version": artifact.version,
                "size_bytes": artifact.size_bytes,
                "duration_ms": info.duration_ms,
                "sha256": artifact.sha256,
                "renderer": self._renderer.name,
                "job_id": job.id,
            },
        )

        return LongFormRenderResult(
            artifact=artifact,
            created=True,
            input_sha256=input_sha256,
            job_id=job.id,
        )
