"""Shorts render (Prompt Pack v8, prompt #100), context C8 Shorts Production.

``ShortsRenderService.render`` turns a ``ready`` Shorts project into a vertical
MP4 through the ``VideoRenderer`` provider (``providers/video_render.py``) and
stores it as the next version of the item's ``VIDEO`` ``Artifact``. The rules
were decided by the user on 2026-10-18:

- Inputs. The project is read, and its assets are checked, in one read
  transaction: it must exist (``project_not_found``), be at the
  ``expected_version`` the caller read (``conflict``), earn ``ready`` from
  ``readiness`` again (``not_ready``) and still be bound to the latest script
  and audio of its item (``stale_input``). Every visual scene asset and caption
  font passes the I-096 asset checks again (``check_timeline_assets``:
  missing, of another channel or not attached is ``asset_unavailable``, rights
  unknown is ``asset_rights``, a kind its role refuses is
  ``asset_unsupported``), and a scene asset whose current provenance is not
  the one the timeline was saved with is ``asset_changed`` (D7, 409).
- The audio is mixed again on every call (D6) by ``ShortsAudioMixer.mix`` (its
  ``audio.mixed`` event is audited each time and a ``ShortsMixError`` passes
  through); the mix is not stored and goes to the renderer in memory.
- The files of the visual assets and fonts come from a ``VisualSource``
  (``read_file(asset)``). The default, ``ArtifactVisualSource``, reads the
  ``Artifact`` of a generated asset through the ``ArtifactStorage`` and checks
  its sha256 and size; a file that cannot be read is ``asset_unavailable``
  (D3) and a media type the role cannot use is ``asset_unsupported``.
- ``build_render_request`` is pure: the canvas, frame rate and duration of the
  project, one layer per visual scene asset (scene by scene: background, then
  b-roll, then overlays, each in timeline order; absolute times; no box is the
  full canvas, a permille box is scaled to pixels with halves rounded up),
  one ``TextLine`` per wrapped caption line placed in the ``caption_box`` of
  I-097 (D4; none when the format has captions off) and the mix as the audio.
  ``render_input_sha256`` hashes it with the renderer name, the project id, the
  timeline, voice and mix checksums, the configuration and
  ``RENDER_KEY_VERSION``.
- Job (D1). A render is an ``AIJob`` of kind ``video.render`` with the key
  ``render_key(item id, input sha256, next video version)`` (no migration).
  When the latest VIDEO artifact was made by a succeeded render job from the
  same input sha256 it is returned with ``created=False``: no renderer call,
  no write, no audit. A succeeded job of the key is replayed the same way; a
  cancelled or waiting job, or a running one without ``take_over``, is
  ``in_progress`` (409); ``take_over=True`` saves the running attempt as failed
  (``interrupted``) and starts a new one; a failed job starts its next
  attempt.
- The renderer runs outside any transaction. A ``VideoRenderError`` fails the
  attempt with its code, audits ``shorts.render_failed`` and is raised again.
  The result must be an MP4 that ``probe_mp4`` reads with a duration within
  ``max(250, 2000 // fps)`` ms of the project, else ``render_invalid``.
- Storing. In one write transaction the project must still be at the same
  version and current and the latest VIDEO artifact unchanged (``conflict`` or
  ``stale_input``, the attempt fails). A video with the latest artifact's
  sha256 is not a new version: the job succeeds pointing at it
  (``created=False``) and nothing is written or audited. Otherwise the file is
  written through the ``ArtifactStorage`` as ``video/mp4``
  (``storage_failed``), the artifact is stored, stale approvals are
  invalidated (#035) and the ``video.rendered`` checkpoint is saved with
  ``succeed`` through the job's ``updated_at`` guard, so a worker that was
  taken over gets a ``ConcurrencyError``. The file is deleted again when
  anything fails.
- The project row and its status are never changed (D2): "rendered" is the
  latest VIDEO artifact and its job. Nothing here approves or publishes.
- After the commit each invalidated approval is audited as
  ``approval.invalidated`` and the new version as ``shorts.rendered`` with ids,
  checksums, counts and sizes, never any caption text, bytes or engine output.
"""

import hashlib
import json
import sqlite3
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from http import HTTPStatus
from typing import Protocol, runtime_checkable

from ai_youtube_agent.content.asset import Asset
from ai_youtube_agent.content.shorts_audio_mix import MixOutcome, ShortsAudioMixer
from ai_youtube_agent.content.shorts_captions import caption_box, wrap
from ai_youtube_agent.content.shorts_project import ShortsProject, ShortsProjectStatus
from ai_youtube_agent.content.shorts_service import (
    ShortsProjectError,
    ShortsProjectErrorCode,
    check_timeline_assets,
    project_is_current,
    readiness,
)
from ai_youtube_agent.content.shorts_timeline import (
    AssetRole,
    Box,
    Scene,
    SceneAsset,
    Timeline,
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
from ai_youtube_agent.core.db.repositories.provenance import ProvenanceRepository
from ai_youtube_agent.core.db.repositories.shorts import ShortsProjectRepository
from ai_youtube_agent.core.errors import DomainError
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

RENDERED = "shorts.rendered"
RENDER_FAILED = "shorts.render_failed"
RENDERED_STEP = "video.rendered"
RENDER_KEY_VERSION = 1
JOB_KIND = RENDER_KIND
PERMILLE = 1000
VISUAL_MEDIA_TYPES = IMAGE_MEDIA_TYPES | VIDEO_MEDIA_TYPES
# Layers of one scene, bottom to top.
_LAYER_ORDER = {AssetRole.BACKGROUND: 0, AssetRole.B_ROLL: 1, AssetRole.OVERLAY: 2}


class ShortsRenderErrorCode(StrEnum):
    RENDER = "domain.shorts_render"
    PROJECT_NOT_FOUND = "domain.shorts_render_project_not_found"
    CONFLICT = "domain.shorts_render_conflict"
    NOT_READY = "domain.shorts_render_not_ready"
    STALE_INPUT = "domain.shorts_render_stale_input"
    IN_PROGRESS = "domain.shorts_render_in_progress"
    ASSET_UNAVAILABLE = "domain.shorts_render_asset_unavailable"
    ASSET_UNSUPPORTED = "domain.shorts_render_asset_unsupported"
    ASSET_RIGHTS = "domain.shorts_render_asset_rights"
    ASSET_CHANGED = "domain.shorts_render_asset_changed"
    RENDER_INVALID = "domain.shorts_render_invalid"
    STORAGE_FAILED = "domain.shorts_render_storage_failed"
    INTERRUPTED = "domain.shorts_render_interrupted"


USER_MESSAGES = {
    ShortsRenderErrorCode.RENDER: "The video could not be rendered.",
    ShortsRenderErrorCode.PROJECT_NOT_FOUND: "This Shorts project does not exist.",
    ShortsRenderErrorCode.CONFLICT: "The Shorts project changed in the meantime. "
    "Reload it and try again.",
    ShortsRenderErrorCode.NOT_READY: "Finish the scenes, backgrounds and captions "
    "of this Short before rendering it.",
    ShortsRenderErrorCode.STALE_INPUT: "The script or audio of this project is out "
    "of date. Rebind the project first.",
    ShortsRenderErrorCode.IN_PROGRESS: "This Short is already being rendered.",
    ShortsRenderErrorCode.ASSET_UNAVAILABLE: "A picture, video or font of this "
    "Short is missing or has changed.",
    ShortsRenderErrorCode.ASSET_UNSUPPORTED: "A file of this Short is not in a "
    "format the video can use.",
    ShortsRenderErrorCode.ASSET_RIGHTS: "A file of this Short has no declared "
    "rights category yet.",
    ShortsRenderErrorCode.ASSET_CHANGED: "The source record of a file of this "
    "Short changed. Save the timeline again first.",
    ShortsRenderErrorCode.RENDER_INVALID: "The rendered video did not pass its checks.",
    ShortsRenderErrorCode.STORAGE_FAILED: "The video file could not be stored.",
    ShortsRenderErrorCode.INTERRUPTED: "The earlier render of this Short was "
    "interrupted.",
}
HTTP_STATUS = {
    ShortsRenderErrorCode.PROJECT_NOT_FOUND: HTTPStatus.NOT_FOUND,
    ShortsRenderErrorCode.CONFLICT: HTTPStatus.CONFLICT,
    ShortsRenderErrorCode.STALE_INPUT: HTTPStatus.CONFLICT,
    ShortsRenderErrorCode.IN_PROGRESS: HTTPStatus.CONFLICT,
    ShortsRenderErrorCode.ASSET_CHANGED: HTTPStatus.CONFLICT,
}
# How the I-096 asset failures are reported by the render.
_PROJECT_CODES = ShortsProjectErrorCode
ASSET_CODES = {
    _PROJECT_CODES.ASSET_NOT_FOUND.value: ShortsRenderErrorCode.ASSET_UNAVAILABLE,
    _PROJECT_CODES.ASSET_OTHER_CHANNEL.value: ShortsRenderErrorCode.ASSET_UNAVAILABLE,
    _PROJECT_CODES.ASSET_NOT_ATTACHED.value: ShortsRenderErrorCode.ASSET_UNAVAILABLE,
    _PROJECT_CODES.ASSET_RIGHTS_UNKNOWN.value: ShortsRenderErrorCode.ASSET_RIGHTS,
    _PROJECT_CODES.ASSET_KIND_MISMATCH.value: ShortsRenderErrorCode.ASSET_UNSUPPORTED,
}


class ShortsRenderError(DomainError):
    """A render was refused or failed; ``code`` is a ``ShortsRenderErrorCode``."""

    default_code = ShortsRenderErrorCode.RENDER.value
    default_user_message = USER_MESSAGES[ShortsRenderErrorCode.RENDER]

    def __init__(
        self,
        detail: str = "",
        *,
        code: ShortsRenderErrorCode = ShortsRenderErrorCode.RENDER,
    ) -> None:
        super().__init__(detail, code=code.value, user_message=USER_MESSAGES[code])
        if code in HTTP_STATUS:
            self.default_http_status = HTTP_STATUS[code]


# Visual files


@dataclass(frozen=True)
class VisualFile:
    """The bytes of a visual asset or font, their media type and sha256."""

    data: bytes = field(repr=False)
    media_type: str
    sha256: str

    def __post_init__(self) -> None:
        if not isinstance(self.data, bytes) or not self.data:
            raise ValueError("data must be non-empty bytes")
        if not isinstance(self.media_type, str) or not self.media_type:
            raise ValueError("media_type must not be empty")
        if self.sha256 != hashlib.sha256(self.data).hexdigest():
            raise ValueError("the sha256 does not match the data")

    @classmethod
    def of(cls, data: bytes, media_type: str) -> "VisualFile":
        """A file whose sha256 is computed from ``data``."""
        if not isinstance(data, bytes):
            raise ValueError("data must be non-empty bytes")
        return cls(data, media_type, hashlib.sha256(data).hexdigest())


@runtime_checkable
class VisualSource(Protocol):
    """Gives the file of an asset; raises OSError or ValueError if it can't."""

    def read_file(self, asset: Asset) -> VisualFile: ...


class ArtifactVisualSource:
    """Reads the file of a generated asset through its ``Artifact``."""

    def __init__(self, database: Database, storage: ArtifactStorage) -> None:
        self._database = database
        self._storage = storage

    def read_file(self, asset: Asset) -> VisualFile:
        if asset.artifact_id is None:
            raise ValueError(f"asset {asset.id} has no stored file")
        with self._database.transaction() as connection:
            artifact = ArtifactRepository(connection).get(asset.artifact_id)
        if artifact is None:
            raise ValueError(f"artifact {asset.artifact_id} does not exist")
        data = self._storage.read(artifact.uri)
        if (
            len(data) != artifact.size_bytes
            or hashlib.sha256(data).hexdigest() != artifact.sha256
        ):
            raise ValueError("the file differs from the stored artifact")
        return VisualFile(data, artifact.media_type, artifact.sha256)


@dataclass(frozen=True)
class RenderResult:
    artifact: Artifact
    created: bool
    input_sha256: str
    job_id: str


# The render request


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


def visual_placements(timeline: Timeline) -> list[tuple[Scene, SceneAsset]]:
    """The visual scene assets, scene by scene, bottom layer first."""
    placed: list[tuple[Scene, SceneAsset]] = []
    for scene in timeline.scenes:
        visual = [asset for asset in scene.assets if asset.role in _LAYER_ORDER]
        visual.sort(key=lambda asset: _LAYER_ORDER[asset.role])  # stable
        placed.extend((scene, asset) for asset in visual)
    return placed


def caption_font_ids(timeline: Timeline) -> tuple[str, ...]:
    """The font assets the captions use, sorted."""
    fonts = {caption.style.font_asset_id for caption in timeline.captions}
    return tuple(sorted(font for font in fonts if font is not None))


def _input(key: str, file: VisualFile) -> RenderInput:
    return RenderInput(key, file.data, file.media_type, file.sha256)


def _text_lines(
    project: ShortsProject, font_keys: Mapping[str, str], fallback: str | None
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
                        box.x, box.y + index * line_height, box.width, line_height
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


def build_render_request(
    project: ShortsProject,
    mix: MixOutcome,
    files: Mapping[str, VisualFile],
    fonts: Mapping[str, VisualFile],
    *,
    font_fallback: VisualFile | None = None,
) -> RenderRequest:
    """The render request of ``project``; ValueError when it cannot be built.

    ``files`` and ``fonts`` give the file of each visual asset and caption font
    by asset id. ``font_fallback`` is the font of captions without a font
    asset; without it the renderer chooses its own.
    """
    config = project.config
    inputs: list[RenderInput] = []
    layers: list[VisualLayer] = []
    for number, (scene, placed) in enumerate(
        visual_placements(project.timeline), start=1
    ):
        key = f"v{number}"
        file = files[placed.asset_id]
        inputs.append(_input(key, file))
        video = file.media_type in VIDEO_MEDIA_TYPES
        end_ms = scene.duration_ms if placed.end_ms is None else placed.end_ms
        layers.append(
            VisualLayer(
                input_key=key,
                start_ms=scene.start_ms + placed.start_ms,
                end_ms=scene.start_ms + end_ms,
                rect=_rect(placed.box, config.width, config.height),
                fit=LayerFit(placed.fit.value),
                source_in_ms=placed.source_in_ms if video else None,
                source_out_ms=placed.source_out_ms if video else None,
            )
        )
    font_keys: dict[str, str] = {}
    for number, font_id in enumerate(sorted(fonts), start=1):
        font_keys[font_id] = f"f{number}"
        inputs.append(_input(font_keys[font_id], fonts[font_id]))
    fallback = None
    if font_fallback is not None:
        fallback = "font"
        inputs.append(_input(fallback, font_fallback))
    return RenderRequest(
        width=config.width,
        height=config.height,
        fps=config.fps,
        duration_ms=project.audio_duration_ms,
        audio=RenderInput(AUDIO_KEY, mix.wav, AUDIO_INPUT_MEDIA_TYPE, mix.sha256),
        inputs=tuple(inputs),
        layers=tuple(layers),
        texts=tuple(_text_lines(project, font_keys, fallback)),
    )


def render_input_sha256(
    renderer_name: str,
    project: ShortsProject,
    mix: MixOutcome,
    request: RenderRequest,
) -> str:
    """The sha256 of everything a render reads, the binding of its job key."""
    canonical = json.dumps(
        {
            "v": RENDER_KEY_VERSION,
            "renderer": renderer_name,
            "project_id": project.id,
            "timeline_sha256": project.timeline.sha256,
            "config": project.config.to_dict(),
            "voice_sha256": mix.voice_sha256,
            "mix_sha256": mix.sha256,
            "request": request.describe(),
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# The service


class _Attempt:
    """The job of one run; every save is its own guarded transaction."""

    def __init__(self, database: Database, job: AIJob) -> None:
        self._database = database
        self.job = job

    def save(self, job: AIJob) -> None:
        with self._database.transaction() as connection:
            AIJobRepository(connection).update(
                job, expected_updated_at=self.job.updated_at
            )
        self.job = job


@dataclass(frozen=True)
class _Run:
    """Everything one started render needs after the renderer was called."""

    project: ShortsProject
    mix: MixOutcome
    request: RenderRequest
    input_sha256: str
    asset_ids: tuple[str, ...]
    latest: Artifact | None
    attempt: _Attempt
    actor: Actor


class ShortsRenderService:
    def __init__(
        self,
        database: Database,
        storage: ArtifactStorage,
        audit: AuditLog,
        renderer: VideoRenderer,
        mixer: ShortsAudioMixer,
        visual_source: VisualSource | None = None,
        *,
        clock: Clock | None = None,
    ) -> None:
        self._database = database
        self._storage = storage
        self._audit = audit
        self._renderer = renderer
        self._mixer = mixer
        self._visuals = visual_source or ArtifactVisualSource(database, storage)
        self._clock = clock

    def render(
        self,
        project_id: str,
        *,
        expected_version: int,
        actor: Actor,
        take_over: bool = False,
    ) -> RenderResult:
        project, visuals, fonts = self._read(project_id, expected_version)
        mix = self._mixer.mix(
            project_id, expected_version=expected_version, actor=actor
        )
        files = {
            asset_id: self._file(asset, VISUAL_MEDIA_TYPES, "picture or video")
            for asset_id, asset in visuals.items()
        }
        font_files = {
            asset_id: self._file(asset, FONT_MEDIA_TYPES, "caption font")
            for asset_id, asset in fonts.items()
        }
        try:
            request = build_render_request(project, mix, files, font_files)
        except ValueError as error:
            raise ShortsRenderError(str(error)) from None
        input_sha256 = render_input_sha256(self._renderer.name, project, mix, request)
        started = self._start(project, input_sha256, take_over)
        if isinstance(started, RenderResult):
            return started
        attempt, latest = started
        run = _Run(
            project=project,
            mix=mix,
            request=request,
            input_sha256=input_sha256,
            asset_ids=tuple(sorted({*visuals, *fonts})),
            latest=latest,
            attempt=attempt,
            actor=actor,
        )
        video, info = self._render(run)
        return self._store(run, video, info)

    # Reading

    def _read(
        self, project_id: str, expected_version: int
    ) -> tuple[ShortsProject, dict[str, Asset], dict[str, Asset]]:
        """The project and its checked visual assets and fonts, in one read."""
        with self._database.transaction() as connection:
            project = ShortsProjectRepository(connection).get(project_id)
            if project is None:
                raise ShortsRenderError(
                    f"Shorts project {project_id} does not exist",
                    code=ShortsRenderErrorCode.PROJECT_NOT_FOUND,
                )
            if project.version != expected_version:
                raise ShortsRenderError(
                    f"Shorts project {project.id} is version {project.version}, "
                    f"not {expected_version}",
                    code=ShortsRenderErrorCode.CONFLICT,
                )
            status = readiness(project.config, project.timeline)
            if status is not ShortsProjectStatus.READY:
                raise ShortsRenderError(
                    f"Shorts project {project.id} is {status.value}, not ready",
                    code=ShortsRenderErrorCode.NOT_READY,
                )
            if not project_is_current(connection, project):
                raise ShortsRenderError(
                    f"the script or audio bound to project {project.id} is no "
                    "longer the latest of its content item",
                    code=ShortsRenderErrorCode.STALE_INPUT,
                )
            item = ContentItemRepository(connection).get(project.content_item_id)
            if item is None:
                raise ShortsRenderError(
                    f"content item {project.content_item_id} does not exist",
                    code=ShortsRenderErrorCode.PROJECT_NOT_FOUND,
                )
            placed = [asset for _, asset in visual_placements(project.timeline)]
            font_ids = caption_font_ids(project.timeline)
            try:
                assets = check_timeline_assets(connection, item, placed, font_ids)
            except ShortsProjectError as error:
                raise ShortsRenderError(
                    error.detail,
                    code=ASSET_CODES.get(error.code, ShortsRenderErrorCode.RENDER),
                ) from None
            _check_provenance(connection, placed)
        visuals = {asset.asset_id: assets[asset.asset_id] for asset in placed}
        fonts = {font_id: assets[font_id] for font_id in font_ids}
        return project, visuals, fonts

    def _file(self, asset: Asset, allowed: frozenset[str], use: str) -> VisualFile:
        try:
            file = self._visuals.read_file(asset)
        except (OSError, ValueError) as error:
            raise ShortsRenderError(
                f"the file of asset {asset.id} is unavailable: {error}",
                code=ShortsRenderErrorCode.ASSET_UNAVAILABLE,
            ) from None
        if file.media_type not in allowed:
            raise ShortsRenderError(
                f"asset {asset.id} is {file.media_type}, which a {use} cannot use",
                code=ShortsRenderErrorCode.ASSET_UNSUPPORTED,
            )
        return file

    # The job

    def _start(
        self, project: ShortsProject, input_sha256: str, take_over: bool
    ) -> RenderResult | tuple[_Attempt, Artifact | None]:
        """A replayed result, or the started attempt and the latest video."""
        item_id = project.content_item_id
        with self._database.transaction() as connection:
            artifacts = ArtifactRepository(connection)
            jobs = AIJobRepository(connection)
            latest = artifacts.latest(item_id, ArtifactKind.VIDEO)
            if latest is not None:
                done = _rendered_by(
                    jobs.list_by_content_item(item_id), latest.id, input_sha256
                )
                if done is not None:
                    return RenderResult(latest, False, input_sha256, done.id)
            version = (latest.version if latest else 0) + 1
            key = render_key(item_id, input_sha256, version)
            job = jobs.get_by_idempotency_key(key)
            if job is None:
                job = AIJob.create(
                    JOB_KIND, key, content_item_id=item_id, clock=self._clock
                )
                jobs.add(job)
            elif job.status is AIJobStatus.SUCCEEDED:
                artifact = _checkpoint_artifact(artifacts, job)
                return RenderResult(artifact, False, input_sha256, job.id)
        if job.status in (AIJobStatus.CANCELLED, AIJobStatus.WAITING) or (
            job.status is AIJobStatus.RUNNING and not take_over
        ):
            raise ShortsRenderError(
                f"render job {job.id} is {job.status.value}",
                code=ShortsRenderErrorCode.IN_PROGRESS,
            )
        attempt = _Attempt(self._database, job)
        if job.status is AIJobStatus.RUNNING:
            attempt.save(
                job.fail(
                    ShortsRenderErrorCode.INTERRUPTED.value, clock=self._after(job)
                )
            )
        attempt.save(attempt.job.start(clock=self._after(attempt.job)))
        return attempt, latest

    def _render(self, run: _Run) -> tuple[RenderedVideo, Mp4Info]:
        """Call the renderer and check that its answer is the expected MP4."""
        try:
            video = self._renderer.render(run.request)
        except VideoRenderError as error:
            self._fail(run, error.code)
            raise
        try:
            info = probe_mp4(video.data)
        except ValueError as error:
            raise self._failed(
                run,
                ShortsRenderError(
                    f"the renderer answered with a file that is not an MP4: {error}",
                    code=ShortsRenderErrorCode.RENDER_INVALID,
                ),
            ) from None
        tolerance = max(250, 2000 // run.request.fps)
        if abs(info.duration_ms - run.request.duration_ms) > tolerance:
            raise self._failed(
                run,
                ShortsRenderError(
                    f"the video is {info.duration_ms} ms, the project "
                    f"{run.request.duration_ms} ms (tolerance {tolerance} ms)",
                    code=ShortsRenderErrorCode.RENDER_INVALID,
                ),
            )
        return video, info

    # Storing

    def _store(self, run: _Run, video: RenderedVideo, info: Mp4Info) -> RenderResult:
        project = run.project
        item_id = project.content_item_id
        checksum = hashlib.sha256(video.data).hexdigest()
        written: StoredFile | None = None
        invalidation: InvalidationResult | None = None
        try:
            with self._database.transaction() as connection:
                _recheck(connection, run)
                latest = run.latest
                if latest is not None and latest.sha256 == checksum:
                    artifact = latest
                else:
                    version = latest.version + 1 if latest else 1
                    try:
                        written = self._storage.write(
                            item_id,
                            ArtifactKind.VIDEO,
                            version,
                            video.data,
                            media_type=MP4_MEDIA_TYPE,
                        )
                    except (OSError, ValueError) as error:
                        raise ShortsRenderError(
                            f"the video of project {project.id} could not be "
                            f"stored: {error}",
                            code=ShortsRenderErrorCode.STORAGE_FAILED,
                        ) from error
                    artifact = _artifact(latest, item_id, written, self._clock)
                    ArtifactRepository(connection).add(artifact)
                    invalidation = invalidate_stale_approvals(
                        connection, item_id, clock=self._clock
                    )
                job = run.attempt.job
                saved = job.checkpoint(
                    RENDERED_STEP,
                    {
                        "artifact_id": artifact.id,
                        "version": artifact.version,
                        "sha256": artifact.sha256,
                        "size_bytes": artifact.size_bytes,
                        "duration_ms": info.duration_ms,
                        "input_sha256": run.input_sha256,
                        "timeline_sha256": project.timeline.sha256,
                        "mix_sha256": run.mix.sha256,
                        "created": written is not None,
                    },
                    clock=self._after(job),
                )
                finished = saved.succeed(clock=self._after(saved))
                AIJobRepository(connection).update(
                    finished, expected_updated_at=job.updated_at
                )
        except BaseException as error:
            if written is not None:
                self._discard(written.uri)
            if isinstance(error, ShortsRenderError):
                self._fail(run, error.code)
            raise
        run.attempt.job = finished
        if written is None or invalidation is None:
            return RenderResult(artifact, False, run.input_sha256, finished.id)
        record_invalidations(self._audit, invalidation)
        self._audit.record(
            RENDERED,
            run.actor,
            EntityRef("artifact", artifact.id),
            AuditResult.SUCCESS,
            self._event_metadata(run, artifact, video, info, finished),
        )
        return RenderResult(artifact, True, run.input_sha256, finished.id)

    def _discard(self, uri: str) -> None:
        try:
            self._storage.delete(uri)
        except Exception:
            # The first error must win; the cleanup failure is only logged.
            logger.warning(
                "video file could not be removed after a failed render",
                extra={"fields": {"uri": uri}},
            )

    # Failures

    def _failed(self, run: _Run, error: ShortsRenderError) -> ShortsRenderError:
        self._fail(run, error.code)
        return error

    def _fail(self, run: _Run, code: str) -> None:
        """Save the attempt as failed with ``code`` and audit it."""
        attempt = run.attempt
        attempt.save(attempt.job.fail(code, clock=self._after(attempt.job)))
        self._audit.record(
            RENDER_FAILED,
            run.actor,
            EntityRef("shorts_project", run.project.id),
            AuditResult.FAILURE,
            {
                "project_id": run.project.id,
                "content_item_id": run.project.content_item_id,
                "job_id": attempt.job.id,
                "attempts": attempt.job.attempts,
                "input_sha256": run.input_sha256,
                "renderer": self._renderer.name,
                "code": code,
            },
        )

    def _after(self, job: AIJob) -> Clock:
        """A clock that never goes back before the job's last change."""
        now = self._clock() if self._clock else datetime.now(UTC)
        moment = max(now, job.updated_at)
        return lambda: moment

    def _event_metadata(
        self,
        run: _Run,
        artifact: Artifact,
        video: RenderedVideo,
        info: Mp4Info,
        job: AIJob,
    ) -> Mapping[str, MetadataValue]:
        project = run.project
        return {
            "project_id": project.id,
            "content_item_id": artifact.content_item_id,
            "project_version": project.version,
            "version": artifact.version,
            "sha256": artifact.sha256,
            "size_bytes": artifact.size_bytes,
            "media_type": artifact.media_type,
            "duration_ms": info.duration_ms,
            "width": video.width,
            "height": video.height,
            "fps": video.fps,
            "renderer": self._renderer.name,
            "input_sha256": run.input_sha256,
            "timeline_sha256": project.timeline.sha256,
            "mix_sha256": run.mix.sha256,
            "voice_sha256": run.mix.voice_sha256,
            "layer_count": len(run.request.layers),
            "text_line_count": len(run.request.texts),
            "caption_count": len(project.timeline.captions),
            "asset_ids": ",".join(run.asset_ids),
            "job_id": job.id,
            "attempts": job.attempts,
        }


def _check_provenance(
    connection: sqlite3.Connection, placements: Sequence[SceneAsset]
) -> None:
    """Refuse a scene asset whose provenance changed since the timeline was saved."""
    provenance = ProvenanceRepository(connection)
    for placed in placements:
        latest = provenance.latest(placed.asset_id)
        current = latest.id if latest is not None else None
        if placed.provenance_id != current:
            raise ShortsRenderError(
                f"asset {placed.asset_id} has provenance {current}, but the "
                f"timeline was saved with {placed.provenance_id}",
                code=ShortsRenderErrorCode.ASSET_CHANGED,
            )


def _recheck(connection: sqlite3.Connection, run: _Run) -> None:
    """Nothing the render read may have changed while it ran."""
    project = run.project
    current = ShortsProjectRepository(connection).get(project.id)
    if current is None or current.version != project.version:
        raise ShortsRenderError(
            f"Shorts project {project.id} changed while it was rendered",
            code=ShortsRenderErrorCode.CONFLICT,
        )
    if not project_is_current(connection, current):
        raise ShortsRenderError(
            f"the script or audio bound to project {project.id} changed while it "
            "was rendered",
            code=ShortsRenderErrorCode.STALE_INPUT,
        )
    latest = ArtifactRepository(connection).latest(
        project.content_item_id, ArtifactKind.VIDEO
    )
    before = run.latest.id if run.latest is not None else None
    if (latest.id if latest is not None else None) != before:
        raise ShortsRenderError(
            f"another video of content item {project.content_item_id} was stored "
            "while this one was rendered",
            code=ShortsRenderErrorCode.CONFLICT,
        )


def _latest_checkpoint(job: AIJob, step: str) -> JobCheckpoint | None:
    for saved in reversed(job.checkpoints):
        if saved.step == step:
            return saved
    return None


def _rendered_by(
    jobs: Sequence[AIJob], artifact_id: str, input_sha256: str
) -> AIJob | None:
    """The succeeded render job that made ``artifact_id`` from ``input_sha256``."""
    for job in reversed(jobs):
        if job.kind != JOB_KIND or job.status is not AIJobStatus.SUCCEEDED:
            continue
        saved = _latest_checkpoint(job, RENDERED_STEP)
        if (
            saved is not None
            and saved.data.get("artifact_id") == artifact_id
            and saved.data.get("input_sha256") == input_sha256
        ):
            return job
    return None


def _checkpoint_artifact(artifacts: ArtifactRepository, job: AIJob) -> Artifact:
    """The artifact a succeeded render job points at."""
    saved = _latest_checkpoint(job, RENDERED_STEP)
    artifact_id = saved.data.get("artifact_id") if saved is not None else None
    artifact = artifacts.get(str(artifact_id)) if artifact_id else None
    if artifact is None:
        raise ShortsRenderError(
            f"render job {job.id} succeeded but its video is no longer stored"
        )
    return artifact


def _artifact(
    latest: Artifact | None, item_id: str, stored: StoredFile, clock: Clock | None
) -> Artifact:
    if latest is None:
        return Artifact.create(
            item_id,
            ArtifactKind.VIDEO,
            uri=stored.uri,
            sha256=stored.sha256,
            size_bytes=stored.size_bytes,
            media_type=MP4_MEDIA_TYPE,
            clock=clock,
        )
    return latest.next_version(
        uri=stored.uri,
        sha256=stored.sha256,
        size_bytes=stored.size_bytes,
        media_type=MP4_MEDIA_TYPE,
        clock=clock,
    )
