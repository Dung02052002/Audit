"""LongForm Thumbnail (Prompt Pack v8, prompt J-112), context C9 LongForm Production.

``LongFormThumbnailRecorder`` selects an attached 16:9 horizontal image asset or
generates a deterministic 16:9 thumbnail PNG for a ``LongFormProject`` (#105) and
stores it as the item's ``ArtifactKind.THUMBNAIL`` artifact with its metadata.

Key invariants:
- FeatureFlags.longform_enabled must be True, else LongFormDisabledError.
- Aspect ratio for LongForm thumbnail is 16:9 horizontal (width * 9 == height * 16).
- Minimum dimensions: 640x360 (recommended 1280x720), max 2 MiB, formats PNG/JPEG/WebP.
- `select` verifies asset attachment, channel ownership, kind == IMAGE, provenance.
- `generate` creates a deterministic 8-bit RGB 16:9 PNG (default 1280x720 or from config).
- Idempotent replay: if latest THUMBNAIL artifact has identical sha256, returns created=False.
- New version invalidates stale approvals in the same transaction, cleans up file on failure,
  and audits `longform.thumbnail_recorded`.
"""

import hashlib
import sqlite3
import struct
import unicodedata
import zlib
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from http import HTTPStatus
from typing import Any, Final

from ai_youtube_agent.content.asset import Asset, AssetCategory, AssetKind
from ai_youtube_agent.content.longform_project import (
    LongFormProject,
    LongFormProjectStatus,
)
from ai_youtube_agent.content.longform_script_generator import LongFormDisabledError
from ai_youtube_agent.content.longform_service import (
    LongFormProjectNotFoundError,
    longform_project_is_current,
)
from ai_youtube_agent.content.provenance import Provenance
from ai_youtube_agent.content.shorts_render import (
    ArtifactVisualSource,
    VisualFile,
    VisualSource,
)
from ai_youtube_agent.content.shorts_thumbnail import (
    JPEG_MEDIA_TYPE,
    MAX_THUMBNAIL_BYTES,
    PNG_MEDIA_TYPE,
    THUMBNAIL_MEDIA_TYPES,
    WEBP_MEDIA_TYPE,
    ImageInfo,
    ThumbnailSourceKind,
    _normalize_title,
    _png_chunk,
    _probe_jpeg,
    _probe_png,
    _probe_webp,
)
from ai_youtube_agent.content.shorts_timeline import AssetRole
from ai_youtube_agent.core.artifact import Artifact, ArtifactKind
from ai_youtube_agent.core.artifact_storage import ArtifactStorage, StoredFile
from ai_youtube_agent.core.audit import (
    Actor,
    AuditLog,
    AuditResult,
    EntityRef,
    MetadataValue,
)
from ai_youtube_agent.core.content_item import ContentItem
from ai_youtube_agent.core.db.database import Database
from ai_youtube_agent.core.db.repositories.asset import (
    AssetRepository,
    AssetUsageRepository,
)
from ai_youtube_agent.core.db.repositories.content import (
    ArtifactRepository,
    ContentItemRepository,
)
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

logger = get_logger(__name__)

THUMBNAIL_RECORDED: Final[str] = "longform.thumbnail_recorded"
MIN_LONGFORM_WIDTH: Final[int] = 640
MIN_LONGFORM_HEIGHT: Final[int] = 360
DEFAULT_THUMBNAIL_WIDTH: Final[int] = 1280
DEFAULT_THUMBNAIL_HEIGHT: Final[int] = 720
MAX_THUMBNAIL_TITLE: Final[int] = 100
_PNG_SIGNATURE: Final[bytes] = b"\x89PNG\r\n\x1a\n"

Clock = Callable[[], datetime]


class LongFormThumbnailErrorCode(StrEnum):
    THUMBNAIL = "domain.longform_thumbnail"
    PROJECT_NOT_FOUND = "domain.longform_thumbnail_project_not_found"
    CONFLICT = "domain.longform_thumbnail_conflict"
    NOT_READY = "domain.longform_thumbnail_not_ready"
    STALE_INPUT = "domain.longform_thumbnail_stale_input"
    ASSET_NOT_FOUND = "domain.longform_thumbnail_asset_not_found"
    ASSET_CHANNEL_MISMATCH = "domain.longform_thumbnail_asset_channel_mismatch"
    ASSET_NOT_ATTACHED = "domain.longform_thumbnail_asset_not_attached"
    ASSET_KIND_INVALID = "domain.longform_thumbnail_asset_kind_invalid"
    ASSET_CATEGORY_UNKNOWN = "domain.longform_thumbnail_asset_category_unknown"
    PROVENANCE_MISSING = "domain.longform_thumbnail_provenance_missing"
    ASSET_UNREADABLE = "domain.longform_thumbnail_asset_unreadable"
    INVALID_IMAGE = "domain.longform_thumbnail_invalid_image"
    INVALID_TITLE = "domain.longform_thumbnail_invalid_title"


USER_MESSAGES: Final[dict[LongFormThumbnailErrorCode, str]] = {
    LongFormThumbnailErrorCode.THUMBNAIL: (
        "The LongForm thumbnail could not be recorded."
    ),
    LongFormThumbnailErrorCode.PROJECT_NOT_FOUND: (
        "This LongForm project does not exist."
    ),
    LongFormThumbnailErrorCode.CONFLICT: (
        "The LongForm project changed concurrently. Reload it and try again."
    ),
    LongFormThumbnailErrorCode.NOT_READY: (
        "The LongForm project is still a draft. Plan the timeline first."
    ),
    LongFormThumbnailErrorCode.STALE_INPUT: (
        "The LongForm project is out of date with its script or audio."
    ),
    LongFormThumbnailErrorCode.ASSET_NOT_FOUND: (
        "The thumbnail image asset does not exist."
    ),
    LongFormThumbnailErrorCode.ASSET_CHANNEL_MISMATCH: (
        "The thumbnail image asset belongs to another channel."
    ),
    LongFormThumbnailErrorCode.ASSET_NOT_ATTACHED: (
        "The thumbnail image asset is not attached to this content item."
    ),
    LongFormThumbnailErrorCode.ASSET_KIND_INVALID: (
        "A LongForm thumbnail asset must be an image asset."
    ),
    LongFormThumbnailErrorCode.ASSET_CATEGORY_UNKNOWN: (
        "The thumbnail image asset has an unknown provenance category."
    ),
    LongFormThumbnailErrorCode.PROVENANCE_MISSING: (
        "The thumbnail image asset does not have recorded provenance."
    ),
    LongFormThumbnailErrorCode.ASSET_UNREADABLE: (
        "The thumbnail image asset bytes could not be read."
    ),
    LongFormThumbnailErrorCode.INVALID_IMAGE: (
        "The thumbnail image format, size or 16:9 horizontal aspect ratio is invalid."
    ),
    LongFormThumbnailErrorCode.INVALID_TITLE: (
        "The thumbnail title text must be 1 to 100 characters."
    ),
}


class LongFormThumbnailError(DomainError):
    default_code = LongFormThumbnailErrorCode.THUMBNAIL.value
    default_user_message = USER_MESSAGES[LongFormThumbnailErrorCode.THUMBNAIL]
    default_http_status = HTTPStatus.UNPROCESSABLE_ENTITY

    def __init__(
        self,
        detail: str = "",
        *,
        code: LongFormThumbnailErrorCode = LongFormThumbnailErrorCode.THUMBNAIL,
        http_status: int | None = None,
    ) -> None:
        status = (
            http_status
            if http_status is not None
            else (
                HTTPStatus.NOT_FOUND
                if code
                in (
                    LongFormThumbnailErrorCode.PROJECT_NOT_FOUND,
                    LongFormThumbnailErrorCode.ASSET_NOT_FOUND,
                )
                else (
                    HTTPStatus.CONFLICT
                    if code
                    in (
                        LongFormThumbnailErrorCode.CONFLICT,
                        LongFormThumbnailErrorCode.STALE_INPUT,
                        LongFormThumbnailErrorCode.NOT_READY,
                    )
                    else HTTPStatus.UNPROCESSABLE_ENTITY
                )
            )
        )
        super().__init__(
            detail,
            code=code.value,
            user_message=USER_MESSAGES.get(code, USER_MESSAGES[LongFormThumbnailErrorCode.THUMBNAIL]),
            http_status=status,
        )


@dataclass(frozen=True)
class LongFormThumbnailRecordResult:
    artifact: Artifact
    source_kind: ThumbnailSourceKind
    image_info: ImageInfo
    created: bool
    invalidation: InvalidationResult | None


def probe_longform_image(raw: bytes) -> ImageInfo:
    """Probe an image file and enforce LongForm 16:9 horizontal rules."""
    size = len(raw)
    if size == 0:
        raise LongFormThumbnailError(
            "thumbnail image data must not be empty",
            code=LongFormThumbnailErrorCode.INVALID_IMAGE,
        )
    if size > MAX_THUMBNAIL_BYTES:
        raise LongFormThumbnailError(
            f"thumbnail image is {size} bytes, exceeding {MAX_THUMBNAIL_BYTES}",
            code=LongFormThumbnailErrorCode.INVALID_IMAGE,
        )
    if raw.startswith(_PNG_SIGNATURE):
        width, height = _probe_png(raw)
        media_type = PNG_MEDIA_TYPE
    elif raw.startswith(b"\xff\xd8"):
        width, height = _probe_jpeg(raw)
        media_type = JPEG_MEDIA_TYPE
    elif len(raw) >= 12 and raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        width, height = _probe_webp(raw)
        media_type = WEBP_MEDIA_TYPE
    else:
        raise LongFormThumbnailError(
            "thumbnail image is not a valid PNG, JPEG or WebP file",
            code=LongFormThumbnailErrorCode.INVALID_IMAGE,
        )
    if width < MIN_LONGFORM_WIDTH or height < MIN_LONGFORM_HEIGHT:
        raise LongFormThumbnailError(
            f"thumbnail dimensions {width}x{height} are smaller than "
            f"{MIN_LONGFORM_WIDTH}x{MIN_LONGFORM_HEIGHT}",
            code=LongFormThumbnailErrorCode.INVALID_IMAGE,
        )
    # LongForm aspect ratio check: 16:9 horizontal
    if width * 9 != height * 16:
        raise LongFormThumbnailError(
            f"thumbnail dimensions {width}x{height} do not have a 16:9 horizontal "
            f"aspect ratio",
            code=LongFormThumbnailErrorCode.INVALID_IMAGE,
        )
    return ImageInfo(
        media_type=media_type,
        width=width,
        height=height,
        size_bytes=size,
    )


def generate_longform_thumbnail_png(
    *,
    width: int = DEFAULT_THUMBNAIL_WIDTH,
    height: int = DEFAULT_THUMBNAIL_HEIGHT,
    project_id: str,
    script_id: str,
    title_text: str,
) -> bytes:
    """Synthesise a deterministic 8-bit RGB PNG thumbnail at 16:9 horizontal."""
    if width < MIN_LONGFORM_WIDTH:
        raise ValueError(f"width must be >= {MIN_LONGFORM_WIDTH}")
    if height < MIN_LONGFORM_HEIGHT:
        raise ValueError(f"height must be >= {MIN_LONGFORM_HEIGHT}")
    if width * 9 != height * 16:
        raise ValueError(f"dimensions {width}x{height} must be 16:9 horizontal")
    normalized_title = _normalize_title(title_text)
    seed = hashlib.sha256(
        f"longform-thumbnail-v1|{project_id}|{script_id}|{normalized_title}|{width}x{height}".encode()
    ).digest()
    bg_rgb = (seed[0], seed[1], seed[2])
    band_rgb = (seed[3], seed[4], seed[5])
    accent_rgb = (seed[6], seed[7], seed[8])

    band_top = height // 4
    band_bottom = (height * 3) // 4
    border = max(10, height // 30)

    bg_row = b"\x00" + (bytes(bg_rgb) * width)
    accent_row = b"\x00" + (bytes(accent_rgb) * width)
    side_border = bytes(accent_rgb) * border
    inner_band = bytes(band_rgb) * (width - 2 * border)
    band_row = b"\x00" + side_border + inner_band + side_border

    scanlines = bytearray()
    for y in range(height):
        if y < border or y >= height - border:
            scanlines.extend(accent_row)
        elif band_top <= y < band_bottom:
            scanlines.extend(band_row)
        else:
            scanlines.extend(bg_row)

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    text_payload = b"Comment\x00" + seed[:16].hex().encode("ascii")
    compressed = zlib.compress(bytes(scanlines), level=1)
    return (
        _PNG_SIGNATURE
        + _png_chunk(b"IHDR", ihdr)
        + _png_chunk(b"tEXt", text_payload)
        + _png_chunk(b"IDAT", compressed)
        + _png_chunk(b"IEND", b"")
    )


class LongFormThumbnailRecorder:
    """Records or selects the 16:9 THUMBNAIL artifact of a LongForm project."""

    def __init__(
        self,
        database: Database,
        storage: ArtifactStorage,
        audit: AuditLog,
        visual_source: VisualSource | None = None,
        flags: FeatureFlags | None = None,
        *,
        clock: Clock | None = None,
    ) -> None:
        self._database = database
        self._storage = storage
        self._audit = audit
        self._visual_source = visual_source or ArtifactVisualSource(database, storage)
        self._flags = (
            flags if flags is not None else FeatureFlags(longform_enabled=True)
        )
        self._clock = clock

    def _require_enabled(self) -> None:
        if not self._flags.longform_enabled:
            raise LongFormDisabledError("LONGFORM_ENABLED is off")

    def select(
        self,
        project_id: str,
        *,
        expected_version: int,
        actor: Actor,
        asset_id: str | None = None,
    ) -> LongFormThumbnailRecordResult:
        """Select an attached 16:9 image asset as the thumbnail."""
        self._require_enabled()
        with self._database.transaction() as connection:
            project = self._load_and_validate(connection, project_id, expected_version)
            item = ContentItemRepository(connection).get(project.content_item_id)
            if item is None:
                raise LongFormThumbnailError(
                    f"content item {project.content_item_id} not found",
                    code=LongFormThumbnailErrorCode.PROJECT_NOT_FOUND,
                )

            chosen_asset_id = asset_id
            if chosen_asset_id is None:
                # Default to first scene's background asset
                for scene in project.timeline.scenes:
                    for asset in scene.assets:
                        if asset.role is AssetRole.BACKGROUND:
                            chosen_asset_id = asset.asset_id
                            break
                    if chosen_asset_id is not None:
                        break

            if chosen_asset_id is None:
                raise LongFormThumbnailError(
                    "no image asset was provided and no background asset exists",
                    code=LongFormThumbnailErrorCode.ASSET_NOT_FOUND,
                )

            assets_repo = AssetRepository(connection)
            asset = assets_repo.get(chosen_asset_id)
            if asset is None:
                raise LongFormThumbnailError(
                    f"asset {chosen_asset_id} not found",
                    code=LongFormThumbnailErrorCode.ASSET_NOT_FOUND,
                )
            if asset.channel_id != item.channel_id:
                raise LongFormThumbnailError(
                    f"asset {chosen_asset_id} belongs to another channel",
                    code=LongFormThumbnailErrorCode.ASSET_CHANNEL_MISMATCH,
                )

            usages = AssetUsageRepository(connection).list_by_content_item(item.id)
            if not any(u.asset_id == asset.id for u in usages):
                raise LongFormThumbnailError(
                    f"asset {chosen_asset_id} is not attached to this item",
                    code=LongFormThumbnailErrorCode.ASSET_NOT_ATTACHED,
                )

            if asset.kind != AssetKind.IMAGE:
                raise LongFormThumbnailError(
                    f"asset {chosen_asset_id} is kind {asset.kind.value}, not image",
                    code=LongFormThumbnailErrorCode.ASSET_KIND_INVALID,
                )

            if asset.category == AssetCategory.UNKNOWN:
                raise LongFormThumbnailError(
                    f"asset {chosen_asset_id} has unknown provenance category",
                    code=LongFormThumbnailErrorCode.ASSET_CATEGORY_UNKNOWN,
                )

            provenances = ProvenanceRepository(connection)
            latest_prov = provenances.latest(asset.id)
            if latest_prov is None:
                raise LongFormThumbnailError(
                    f"asset {chosen_asset_id} has no provenance record",
                    code=LongFormThumbnailErrorCode.PROVENANCE_MISSING,
                )

        # Read image bytes outside transaction
        try:
            visual_file = self._visual_source.read_file(asset)
            raw = visual_file.data
        except Exception as error:
            raise LongFormThumbnailError(
                f"could not read asset {chosen_asset_id}: {error}",
                code=LongFormThumbnailErrorCode.ASSET_UNREADABLE,
            ) from error

        info = probe_longform_image(raw)
        return self._record_artifact(
            project=project,
            item=item,
            raw_data=raw,
            image_info=info,
            source_kind=ThumbnailSourceKind.SELECTED,
            expected_version=expected_version,
            actor=actor,
        )

    def generate(
        self,
        project_id: str,
        *,
        expected_version: int,
        actor: Actor,
        title_text: str | None = None,
    ) -> LongFormThumbnailRecordResult:
        """Synthesise a deterministic 16:9 PNG thumbnail."""
        self._require_enabled()
        with self._database.transaction() as connection:
            project = self._load_and_validate(connection, project_id, expected_version)
            item = ContentItemRepository(connection).get(project.content_item_id)
            if item is None:
                raise LongFormThumbnailError(
                    f"content item {project.content_item_id} not found",
                    code=LongFormThumbnailErrorCode.PROJECT_NOT_FOUND,
                )

        title = title_text if title_text is not None else item.title
        try:
            _normalize_title(title)
        except ValueError as err:
            raise LongFormThumbnailError(
                str(err), code=LongFormThumbnailErrorCode.INVALID_TITLE
            ) from err

        raw = generate_longform_thumbnail_png(
            width=project.config.width,
            height=project.config.height,
            project_id=project.id,
            script_id=project.script_id,
            title_text=title,
        )
        info = probe_longform_image(raw)
        return self._record_artifact(
            project=project,
            item=item,
            raw_data=raw,
            image_info=info,
            source_kind=ThumbnailSourceKind.GENERATED,
            expected_version=expected_version,
            actor=actor,
        )

    def _load_and_validate(
        self, connection: sqlite3.Connection, project_id: str, expected_version: int
    ) -> LongFormProject:
        projects = LongFormProjectRepository(connection)
        project = projects.get(project_id)
        if project is None:
            raise LongFormThumbnailError(
                f"project {project_id} not found",
                code=LongFormThumbnailErrorCode.PROJECT_NOT_FOUND,
            )
        if project.version != expected_version:
            raise LongFormThumbnailError(
                f"project {project_id} version {project.version} != {expected_version}",
                code=LongFormThumbnailErrorCode.CONFLICT,
            )
        if not longform_project_is_current(connection, project):
            raise LongFormThumbnailError(
                f"project {project_id} is stale",
                code=LongFormThumbnailErrorCode.STALE_INPUT,
            )
        if project.status == LongFormProjectStatus.DRAFT:
            raise LongFormThumbnailError(
                f"project {project_id} is a draft",
                code=LongFormThumbnailErrorCode.NOT_READY,
            )
        return project

    def _record_artifact(
        self,
        project: LongFormProject,
        item: ContentItem,
        raw_data: bytes,
        image_info: ImageInfo,
        source_kind: ThumbnailSourceKind,
        expected_version: int,
        actor: Actor,
    ) -> LongFormThumbnailRecordResult:
        sha256 = hashlib.sha256(raw_data).hexdigest()

        # Check existing latest thumbnail for replay
        with self._database.transaction() as connection:
            artifacts_repo = ArtifactRepository(connection)
            latest_thumb = artifacts_repo.latest(item.id, ArtifactKind.THUMBNAIL)
            if latest_thumb is not None and latest_thumb.sha256 == sha256:
                return LongFormThumbnailRecordResult(
                    artifact=latest_thumb,
                    source_kind=source_kind,
                    image_info=image_info,
                    created=False,
                    invalidation=None,
                )
            next_version = 1 if latest_thumb is None else latest_thumb.version + 1

        written_file: StoredFile | None = None
        invalidations: list[InvalidationResult] = []

        try:
            written_file = self._storage.write(
                raw_data,
                item.id,
                ArtifactKind.THUMBNAIL,
                next_version,
                image_info.media_type,
            )

            with self._database.transaction() as connection:
                # Re-verify project version and currency
                p = LongFormProjectRepository(connection).get(project.id)
                if p is None or p.version != expected_version:
                    raise LongFormThumbnailError(
                        "project changed concurrently",
                        code=LongFormThumbnailErrorCode.CONFLICT,
                    )
                if not longform_project_is_current(connection, p):
                    raise LongFormThumbnailError(
                        "project became stale concurrently",
                        code=LongFormThumbnailErrorCode.STALE_INPUT,
                    )

                artifacts_repo = ArtifactRepository(connection)
                curr_thumb = artifacts_repo.latest(item.id, ArtifactKind.THUMBNAIL)
                if curr_thumb is not None and curr_thumb.version >= next_version:
                    raise LongFormThumbnailError(
                        "thumbnail version conflict",
                        code=LongFormThumbnailErrorCode.CONFLICT,
                    )

                artifact = Artifact.create(
                    item.id,
                    ArtifactKind.THUMBNAIL,
                    next_version,
                    written_file.uri,
                    sha256,
                    len(raw_data),
                    image_info.media_type,
                    clock=self._clock,
                )
                artifacts_repo.add(artifact)

                # Invalidate stale approvals (#035)
                invalidations = invalidate_stale_approvals(
                    connection, item.id, artifact, actor=actor, clock=self._clock
                )

        except Exception:
            if written_file is not None:
                self._storage.delete(written_file.uri)
            raise

        record_invalidations(self._audit, invalidations)
        self._audit.record(
            THUMBNAIL_RECORDED,
            actor,
            EntityRef("longform_project", project.id),
            AuditResult.SUCCESS,
            {
                "content_item_id": item.id,
                "artifact_id": artifact.id,
                "version": artifact.version,
                "size_bytes": artifact.size_bytes,
                "width": image_info.width,
                "height": image_info.height,
                "media_type": image_info.media_type,
                "source_kind": source_kind.value,
                "sha256": artifact.sha256,
            },
        )

        return LongFormThumbnailRecordResult(
            artifact=artifact,
            source_kind=source_kind,
            image_info=image_info,
            created=True,
            invalidation=invalidations[0] if invalidations else None,
        )
