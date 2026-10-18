"""Shorts Thumbnail (Prompt Pack v8, prompt I-101), context C8 Video Production.

``ShortsThumbnailRecorder`` selects an attached image asset or generates a
deterministic vertical thumbnail image for a ``ShortsProject`` (#094) and stores
it as the item's ``ArtifactKind.THUMBNAIL`` artifact with its
``ThumbnailMetadata``:

- Two paths produce a thumbnail:
  - ``select`` reads an attached ``AssetKind.IMAGE`` asset of the project's
    content item (or defaults to the first scene's ``IMAGE`` background asset
    when ``asset_id`` is omitted), enforces the D5 asset and provenance checks
    inside the transaction, reads the image bytes through ``VisualAssetSource``,
    probes the image format and dimensions, and records the ``THUMBNAIL``
    artifact with ``ThumbnailSourceKind.SELECTED``.
  - ``generate`` synthesises a deterministic 8-bit RGB PNG at the project's
    vertical canvas size (``project.config.canvas_size``, 9:16) from the
    project's ``id``, ``script_id`` and normalized ``title_text`` (or the
    content item's title when ``title_text`` is omitted), and records the
    ``THUMBNAIL`` artifact with ``ThumbnailSourceKind.GENERATED``.
- ``probe_image`` inspects PNG, JPEG and WebP headers in pure Python without
  external binary dependencies, checking ``THUMBNAIL_MEDIA_TYPES``
  (``image/png``, ``image/jpeg``, ``image/webp``), ``MAX_THUMBNAIL_BYTES``
  (2 MiB), minimum dimensions (``MIN_THUMBNAIL_WIDTH = 640``,
  ``MIN_THUMBNAIL_HEIGHT = 640``) and 9:16 vertical aspect ratio
  (``width * 16 == height * 9``).
- When the latest ``THUMBNAIL`` artifact already has the same ``sha256``, the
  existing artifact is returned with ``created=False`` and ``invalidation=None``
  without touching storage, approvals or the audit log.
- A new ``THUMBNAIL`` version invalidates pending approvals (#035) in the same
  transaction, removes the written file if the transaction fails, records
  ``approval.invalidated`` events after commit, and audits
  ``shorts.thumbnail_recorded`` with scalar metadata only (never raw title text
  or image bytes).
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
from ai_youtube_agent.content.provenance import Provenance
from ai_youtube_agent.content.shorts_project import ShortsProject
from ai_youtube_agent.content.shorts_render import (
    ArtifactVisualSource,
    VisualFile,
    VisualSource,
)
from ai_youtube_agent.content.shorts_service import (
    ShortsProjectNotFoundError,
    project_is_current,
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
from ai_youtube_agent.core.db.repositories.provenance import ProvenanceRepository
from ai_youtube_agent.core.db.repositories.shorts import ShortsProjectRepository
from ai_youtube_agent.core.errors import DomainError
from ai_youtube_agent.core.log import get_logger
from ai_youtube_agent.core.version_invalidation import (
    InvalidationResult,
    invalidate_stale_approvals,
    record_invalidations,
)

logger = get_logger(__name__)

THUMBNAIL_RECORDED: Final[str] = "shorts.thumbnail_recorded"
PNG_MEDIA_TYPE: Final[str] = "image/png"
JPEG_MEDIA_TYPE: Final[str] = "image/jpeg"
WEBP_MEDIA_TYPE: Final[str] = "image/webp"
THUMBNAIL_MEDIA_TYPES: Final[frozenset[str]] = frozenset(
    {PNG_MEDIA_TYPE, JPEG_MEDIA_TYPE, WEBP_MEDIA_TYPE}
)
MAX_THUMBNAIL_BYTES: Final[int] = 2 * 1024 * 1024
MIN_THUMBNAIL_WIDTH: Final[int] = 640
MIN_THUMBNAIL_HEIGHT: Final[int] = 640
MAX_THUMBNAIL_TITLE: Final[int] = 100
_PNG_SIGNATURE: Final[bytes] = b"\x89PNG\r\n\x1a\n"
_SOF_MARKERS: Final[frozenset[int]] = frozenset(
    {
        0xC0,
        0xC1,
        0xC2,
        0xC3,
        0xC5,
        0xC6,
        0xC7,
        0xC9,
        0xCA,
        0xCB,
        0xCD,
        0xCE,
        0xCF,
    }
)

Clock = Callable[[], datetime]


class ThumbnailSourceKind(StrEnum):
    SELECTED = "selected"
    GENERATED = "generated"


class ShortsThumbnailErrorCode(StrEnum):
    THUMBNAIL = "domain.shorts_thumbnail"
    TIMELINE_MISSING = "domain.shorts_thumbnail_timeline_missing"
    STALE_INPUT = "domain.shorts_thumbnail_stale_input"
    ASSET_NOT_FOUND = "domain.shorts_thumbnail_asset_not_found"
    ASSET_CHANNEL_MISMATCH = "domain.shorts_thumbnail_asset_channel_mismatch"
    ASSET_NOT_ATTACHED = "domain.shorts_thumbnail_asset_not_attached"
    ASSET_KIND_INVALID = "domain.shorts_thumbnail_asset_kind_invalid"
    ASSET_CATEGORY_UNKNOWN = "domain.shorts_thumbnail_asset_category_unknown"
    PROVENANCE_MISSING = "domain.shorts_thumbnail_provenance_missing"
    ASSET_UNREADABLE = "domain.shorts_thumbnail_asset_unreadable"
    INVALID_IMAGE = "domain.shorts_thumbnail_invalid_image"
    INVALID_TITLE = "domain.shorts_thumbnail_invalid_title"


USER_MESSAGES: Final[dict[ShortsThumbnailErrorCode, str]] = {
    ShortsThumbnailErrorCode.THUMBNAIL: (
        "The Shorts thumbnail could not be recorded."
    ),
    ShortsThumbnailErrorCode.TIMELINE_MISSING: (
        "This Shorts project does not have a timeline yet."
    ),
    ShortsThumbnailErrorCode.STALE_INPUT: (
        "The Shorts project is out of date with its content item, script or audio."
    ),
    ShortsThumbnailErrorCode.ASSET_NOT_FOUND: (
        "The thumbnail image asset does not exist."
    ),
    ShortsThumbnailErrorCode.ASSET_CHANNEL_MISMATCH: (
        "The thumbnail image asset belongs to another channel."
    ),
    ShortsThumbnailErrorCode.ASSET_NOT_ATTACHED: (
        "The thumbnail image asset is not attached to this content item."
    ),
    ShortsThumbnailErrorCode.ASSET_KIND_INVALID: (
        "A Shorts thumbnail asset must be an image asset."
    ),
    ShortsThumbnailErrorCode.ASSET_CATEGORY_UNKNOWN: (
        "The thumbnail image asset has an unknown provenance category."
    ),
    ShortsThumbnailErrorCode.PROVENANCE_MISSING: (
        "The thumbnail image asset does not have recorded provenance."
    ),
    ShortsThumbnailErrorCode.ASSET_UNREADABLE: (
        "The thumbnail image asset bytes could not be read."
    ),
    ShortsThumbnailErrorCode.INVALID_IMAGE: (
        "The thumbnail image format, size or 9:16 vertical aspect ratio is invalid."
    ),
    ShortsThumbnailErrorCode.INVALID_TITLE: (
        "The thumbnail title text must be 1 to 100 characters."
    ),
}


class ShortsThumbnailError(DomainError):
    default_code = ShortsThumbnailErrorCode.THUMBNAIL.value
    default_user_message = USER_MESSAGES[ShortsThumbnailErrorCode.THUMBNAIL]
    default_http_status = HTTPStatus.UNPROCESSABLE_ENTITY

    def __init__(
        self,
        detail: str = "",
        *,
        code: ShortsThumbnailErrorCode = ShortsThumbnailErrorCode.THUMBNAIL,
        http_status: int | None = None,
    ) -> None:
        status = (
            http_status
            if http_status is not None
            else (
                HTTPStatus.NOT_FOUND
                if code is ShortsThumbnailErrorCode.ASSET_NOT_FOUND
                else (
                    HTTPStatus.CONFLICT
                    if code
                    in (
                        ShortsThumbnailErrorCode.TIMELINE_MISSING,
                        ShortsThumbnailErrorCode.STALE_INPUT,
                    )
                    else HTTPStatus.UNPROCESSABLE_ENTITY
                )
            )
        )
        super().__init__(
            detail,
            code=code.value,
            user_message=USER_MESSAGES[code],
            http_status=status,
        )


class ShortsThumbnailConflictError(DomainError):
    default_code = "domain.shorts_thumbnail_conflict"
    default_user_message = (
        "This Shorts project was updated by someone else; reload and try again."
    )
    default_http_status = HTTPStatus.CONFLICT


@dataclass(frozen=True)
class ImageInfo:
    media_type: str
    width: int
    height: int
    size_bytes: int


@dataclass(frozen=True)
class ThumbnailMetadata:
    project_id: str
    project_version: int
    content_item_id: str
    source_kind: ThumbnailSourceKind
    media_type: str
    width: int
    height: int
    size_bytes: int
    sha256: str
    asset_id: str | None = None
    provenance_id: str | None = None
    title_text: str | None = None

    def __post_init__(self) -> None:
        for name in ("project_id", "content_item_id", "sha256"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must not be empty")
        if (
            isinstance(self.project_version, bool)
            or not isinstance(self.project_version, int)
            or self.project_version < 1
        ):
            raise ValueError("project_version must be an integer >= 1")
        if not isinstance(self.source_kind, ThumbnailSourceKind):
            raise TypeError("source_kind must be a ThumbnailSourceKind")
        if self.media_type not in THUMBNAIL_MEDIA_TYPES:
            raise ValueError(f"unsupported thumbnail media_type: {self.media_type!r}")
        for dim_name in ("width", "height"):
            dim = getattr(self, dim_name)
            if isinstance(dim, bool) or not isinstance(dim, int) or dim < 1:
                raise ValueError(f"{dim_name} must be a positive integer")
        if self.width < MIN_THUMBNAIL_WIDTH or self.height < MIN_THUMBNAIL_HEIGHT:
            raise ValueError(
                f"thumbnail dimensions {self.width}x{self.height} must be at least "
                f"{MIN_THUMBNAIL_WIDTH}x{MIN_THUMBNAIL_HEIGHT}"
            )
        if self.width * 16 != self.height * 9:
            raise ValueError(
                f"thumbnail dimensions {self.width}x{self.height} must have a 9:16 "
                f"vertical aspect ratio"
            )
        if (
            isinstance(self.size_bytes, bool)
            or not isinstance(self.size_bytes, int)
            or not 1 <= self.size_bytes <= MAX_THUMBNAIL_BYTES
        ):
            raise ValueError(
                f"size_bytes must be an integer between 1 and {MAX_THUMBNAIL_BYTES}"
            )
        if len(self.sha256) != 64 or any(
            ch not in "0123456789abcdef" for ch in self.sha256
        ):
            raise ValueError("sha256 must be a 64-character lowercase hex digest")
        if self.source_kind is ThumbnailSourceKind.SELECTED:
            if not isinstance(self.asset_id, str) or not self.asset_id.strip():
                raise ValueError("selected thumbnail requires asset_id")
            if (
                not isinstance(self.provenance_id, str)
                or not self.provenance_id.strip()
            ):
                raise ValueError("selected thumbnail requires provenance_id")
            if self.title_text is not None:
                raise ValueError("selected thumbnail must not set title_text")
        else:
            if self.asset_id is not None or self.provenance_id is not None:
                raise ValueError(
                    "generated thumbnail must not set asset_id or provenance_id"
                )
            normalized = _normalize_title(self.title_text)
            object.__setattr__(self, "title_text", normalized)

    def as_dict(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id,
            "project_version": self.project_version,
            "content_item_id": self.content_item_id,
            "source_kind": self.source_kind.value,
            "media_type": self.media_type,
            "width": self.width,
            "height": self.height,
            "size_bytes": self.size_bytes,
            "sha256": self.sha256,
            "asset_id": self.asset_id,
            "provenance_id": self.provenance_id,
            "title_text": self.title_text,
        }


@dataclass(frozen=True)
class ShortsThumbnailRecord:
    artifact: Artifact
    metadata: ThumbnailMetadata
    project: ShortsProject
    created: bool
    invalidation: InvalidationResult | None


def probe_image(data: bytes) -> ImageInfo:
    """Inspect a PNG, JPEG or WebP byte stream and return its ``ImageInfo``."""
    if not isinstance(data, bytes | bytearray):
        raise TypeError("data must be bytes")
    raw = bytes(data)
    size = len(raw)
    if size == 0:
        raise ShortsThumbnailError(
            "thumbnail image data is empty",
            code=ShortsThumbnailErrorCode.INVALID_IMAGE,
        )
    if size > MAX_THUMBNAIL_BYTES:
        raise ShortsThumbnailError(
            f"thumbnail image is {size} bytes, exceeding {MAX_THUMBNAIL_BYTES}",
            code=ShortsThumbnailErrorCode.INVALID_IMAGE,
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
        raise ShortsThumbnailError(
            "thumbnail image is not a valid PNG, JPEG or WebP file",
            code=ShortsThumbnailErrorCode.INVALID_IMAGE,
        )
    if width < MIN_THUMBNAIL_WIDTH or height < MIN_THUMBNAIL_HEIGHT:
        raise ShortsThumbnailError(
            f"thumbnail dimensions {width}x{height} are smaller than "
            f"{MIN_THUMBNAIL_WIDTH}x{MIN_THUMBNAIL_HEIGHT}",
            code=ShortsThumbnailErrorCode.INVALID_IMAGE,
        )
    if width * 16 != height * 9:
        raise ShortsThumbnailError(
            f"thumbnail dimensions {width}x{height} do not have a 9:16 vertical "
            f"aspect ratio",
            code=ShortsThumbnailErrorCode.INVALID_IMAGE,
        )
    return ImageInfo(
        media_type=media_type,
        width=width,
        height=height,
        size_bytes=size,
    )


def generate_thumbnail_png(
    *,
    width: int,
    height: int,
    project_id: str,
    script_id: str,
    title_text: str,
) -> bytes:
    """Synthesise a deterministic 8-bit RGB PNG thumbnail at ``width x height``."""
    if (
        isinstance(width, bool)
        or not isinstance(width, int)
        or width < MIN_THUMBNAIL_WIDTH
    ):
        raise ValueError(f"width must be an integer >= {MIN_THUMBNAIL_WIDTH}")
    if (
        isinstance(height, bool)
        or not isinstance(height, int)
        or height < MIN_THUMBNAIL_HEIGHT
    ):
        raise ValueError(f"height must be an integer >= {MIN_THUMBNAIL_HEIGHT}")
    if width * 16 != height * 9:
        raise ValueError(f"dimensions {width}x{height} must be 9:16 vertical")
    normalized_title = _normalize_title(title_text)
    seed = hashlib.sha256(
        f"shorts-thumbnail-v1|{project_id}|{script_id}|{normalized_title}|{width}x{height}".encode()
    ).digest()
    bg_rgb = (seed[0], seed[1], seed[2])
    band_rgb = (seed[3], seed[4], seed[5])
    accent_rgb = (seed[6], seed[7], seed[8])

    band_top = height // 3
    band_bottom = (height * 2) // 3
    border = max(8, width // 45)

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


VisualAssetSource = VisualSource


class ShortsThumbnailRecorder:
    def __init__(
        self,
        database: Database,
        storage: ArtifactStorage,
        audit: AuditLog,
        *,
        visuals: VisualSource | None = None,
        clock: Clock | None = None,
    ) -> None:
        self._database = database
        self._storage = storage
        self._audit = audit
        self._visuals = (
            visuals
            if visuals is not None
            else ArtifactVisualSource(database, storage)
        )
        self._clock = clock

    def select(
        self,
        project_id: str,
        *,
        asset_id: str | None = None,
        actor: Actor,
        expected_version: int | None = None,
    ) -> ShortsThumbnailRecord:
        """Record a Shorts thumbnail from an attached ``IMAGE`` asset."""
        _check_call_args(project_id, actor, expected_version)
        if asset_id is not None and (
            not isinstance(asset_id, str) or not asset_id.strip()
        ):
            raise TypeError("asset_id must be a non-empty string or None")

        def resolve(
            connection: sqlite3.Connection,
            project: ShortsProject,
            item: ContentItem,
        ) -> tuple[bytes, ThumbnailMetadata]:
            chosen_id = (
                asset_id.strip()
                if asset_id is not None
                else _default_scene_image_asset_id(connection, project)
            )
            asset, provenance = _check_image_asset(
                connection,
                chosen_id,
                channel_id=item.channel_id,
                content_item_id=project.content_item_id,
            )
            data = self._read_asset_bytes(connection, asset)
            info = probe_image(data)
            checksum = _sha256(data)
            metadata = ThumbnailMetadata(
                project_id=project.id,
                project_version=project.version,
                content_item_id=project.content_item_id,
                source_kind=ThumbnailSourceKind.SELECTED,
                media_type=info.media_type,
                width=info.width,
                height=info.height,
                size_bytes=info.size_bytes,
                sha256=checksum,
                asset_id=asset.id,
                provenance_id=provenance.id,
            )
            return data, metadata

        return self._record(
            project_id,
            actor=actor,
            expected_version=expected_version,
            resolver=resolve,
        )

    def generate(
        self,
        project_id: str,
        *,
        title_text: str | None = None,
        actor: Actor,
        expected_version: int | None = None,
    ) -> ShortsThumbnailRecord:
        """Synthesise and record a deterministic PNG thumbnail for the project."""
        _check_call_args(project_id, actor, expected_version)
        if title_text is not None and not isinstance(title_text, str):
            raise TypeError("title_text must be text or None")

        def resolve(
            connection: sqlite3.Connection,
            project: ShortsProject,
            item: ContentItem,
        ) -> tuple[bytes, ThumbnailMetadata]:
            raw_title = title_text if title_text is not None else item.title
            try:
                normalized_title = _normalize_title(raw_title)
            except ValueError as error:
                raise ShortsThumbnailError(
                    str(error),
                    code=ShortsThumbnailErrorCode.INVALID_TITLE,
                ) from error
            width, height = project.config.canvas_size
            data = generate_thumbnail_png(
                width=width,
                height=height,
                project_id=project.id,
                script_id=project.script_id,
                title_text=normalized_title,
            )
            info = probe_image(data)
            checksum = _sha256(data)
            metadata = ThumbnailMetadata(
                project_id=project.id,
                project_version=project.version,
                content_item_id=project.content_item_id,
                source_kind=ThumbnailSourceKind.GENERATED,
                media_type=info.media_type,
                width=info.width,
                height=info.height,
                size_bytes=info.size_bytes,
                sha256=checksum,
                title_text=normalized_title,
            )
            return data, metadata

        return self._record(
            project_id,
            actor=actor,
            expected_version=expected_version,
            resolver=resolve,
        )

    def _record(
        self,
        project_id: str,
        *,
        actor: Actor,
        expected_version: int | None,
        resolver: Callable[
            [sqlite3.Connection, ShortsProject, ContentItem],
            tuple[bytes, ThumbnailMetadata],
        ],
    ) -> ShortsThumbnailRecord:
        written: StoredFile | None = None
        try:
            with self._database.transaction() as connection:
                project, item = _load_project(
                    connection, project_id, expected_version
                )
                data, metadata = resolver(connection, project, item)
                item_id = project.content_item_id
                artifacts = ArtifactRepository(connection)
                latest = artifacts.latest(item_id, ArtifactKind.THUMBNAIL)
                if latest is not None and latest.sha256 == metadata.sha256:
                    return ShortsThumbnailRecord(
                        latest, metadata, project, False, None
                    )
                version = latest.version + 1 if latest else 1
                written = self._storage.write(
                    item_id,
                    ArtifactKind.THUMBNAIL,
                    version,
                    data,
                    media_type=metadata.media_type,
                )
                artifact = _artifact(
                    latest, item_id, written, metadata.media_type, self._clock
                )
                artifacts.add(artifact)
                invalidation = invalidate_stale_approvals(
                    connection, item_id, clock=self._clock
                )
        except BaseException:
            if written is not None:
                self._discard(written.uri)
            raise
        record_invalidations(self._audit, invalidation)
        self._audit.record(
            THUMBNAIL_RECORDED,
            actor,
            EntityRef("artifact", artifact.id),
            AuditResult.SUCCESS,
            _event_metadata(artifact, project, item, metadata),
        )
        return ShortsThumbnailRecord(artifact, metadata, project, True, invalidation)

    def _read_asset_bytes(
        self, connection: sqlite3.Connection, asset: Asset
    ) -> bytes:
        try:
            if isinstance(self._visuals, ArtifactVisualSource):
                if asset.artifact_id is None:
                    raise ValueError(f"asset {asset.id} has no stored file")
                stored_artifact = ArtifactRepository(connection).get(
                    asset.artifact_id
                )
                if stored_artifact is None:
                    raise ValueError(
                        f"artifact {asset.artifact_id} does not exist"
                    )
                raw = self._storage.read(stored_artifact.uri)
                if (
                    len(raw) != stored_artifact.size_bytes
                    or _sha256(raw) != stored_artifact.sha256
                ):
                    raise ValueError("the file differs from the stored artifact")
                return raw
            if hasattr(self._visuals, "read_file"):
                visual_file: VisualFile = self._visuals.read_file(asset)
                return visual_file.data
            data = self._visuals.read_bytes(asset)  # type: ignore[attr-defined]
        except Exception as error:
            raise ShortsThumbnailError(
                f"asset {asset.id} could not be read: {error}",
                code=ShortsThumbnailErrorCode.ASSET_UNREADABLE,
            ) from error
        if not isinstance(data, bytes | bytearray) or len(data) == 0:
            raise ShortsThumbnailError(
                f"asset {asset.id} returned empty bytes",
                code=ShortsThumbnailErrorCode.ASSET_UNREADABLE,
            )
        return bytes(data)

    def _discard(self, uri: str) -> None:
        try:
            self._storage.delete(uri)
        except Exception:
            logger.warning(
                "shorts thumbnail file could not be removed after a failed record",
                extra={"fields": {"uri": uri}},
            )


def _check_call_args(
    project_id: str, actor: Actor, expected_version: int | None
) -> None:
    if not isinstance(project_id, str):
        raise TypeError("project_id must be text")
    if not isinstance(actor, Actor):
        raise TypeError("actor must be an Actor")
    if expected_version is not None and (
        isinstance(expected_version, bool)
        or not isinstance(expected_version, int)
        or expected_version < 1
    ):
        raise ValueError("expected_version must be an integer >= 1")


def _load_project(
    connection: sqlite3.Connection,
    project_id: str,
    expected_version: int | None,
) -> tuple[ShortsProject, ContentItem]:
    project = ShortsProjectRepository(connection).get(project_id)
    if project is None:
        raise ShortsProjectNotFoundError(
            f"Shorts project {project_id} does not exist"
        )
    if expected_version is not None and project.version != expected_version:
        raise ShortsThumbnailConflictError(
            f"Shorts project {project.id} is at version {project.version}, "
            f"expected {expected_version}"
        )
    item = ContentItemRepository(connection).get(project.content_item_id)
    if item is None or not project_is_current(connection, project):
        raise ShortsThumbnailError(
            f"Shorts project {project.id} is no longer current for content item "
            f"{project.content_item_id}",
            code=ShortsThumbnailErrorCode.STALE_INPUT,
        )
    if not project.timeline.scenes:
        raise ShortsThumbnailError(
            f"Shorts project {project.id} has no timeline scenes",
            code=ShortsThumbnailErrorCode.TIMELINE_MISSING,
        )
    return project, item


def _default_scene_image_asset_id(
    connection: sqlite3.Connection, project: ShortsProject
) -> str:
    assets = AssetRepository(connection)
    for scene in project.timeline.scenes:
        for placed in scene.assets:
            if placed.role is AssetRole.BACKGROUND:
                asset = assets.get(placed.asset_id)
                if asset is not None and asset.kind is AssetKind.IMAGE:
                    return asset.id
    raise ShortsThumbnailError(
        f"Shorts project {project.id} has no scene with an IMAGE background asset",
        code=ShortsThumbnailErrorCode.ASSET_KIND_INVALID,
    )


def _check_image_asset(
    connection: sqlite3.Connection,
    asset_id: str,
    *,
    channel_id: str,
    content_item_id: str,
) -> tuple[Asset, Provenance]:
    assets = AssetRepository(connection)
    asset = assets.get(asset_id)
    if asset is None:
        raise ShortsThumbnailError(
            f"asset {asset_id} does not exist",
            code=ShortsThumbnailErrorCode.ASSET_NOT_FOUND,
        )
    if asset.channel_id != channel_id:
        raise ShortsThumbnailError(
            f"asset {asset.id} belongs to another channel than {channel_id}",
            code=ShortsThumbnailErrorCode.ASSET_CHANNEL_MISMATCH,
        )
    if AssetUsageRepository(connection).get_by_pair(asset.id, content_item_id) is None:
        raise ShortsThumbnailError(
            f"asset {asset.id} is not attached to content item {content_item_id}",
            code=ShortsThumbnailErrorCode.ASSET_NOT_ATTACHED,
        )
    if asset.kind is not AssetKind.IMAGE:
        raise ShortsThumbnailError(
            f"asset {asset.id} has kind {asset.kind.value}, expected image",
            code=ShortsThumbnailErrorCode.ASSET_KIND_INVALID,
        )
    if asset.category is AssetCategory.UNKNOWN:
        raise ShortsThumbnailError(
            f"asset {asset.id} has category unknown",
            code=ShortsThumbnailErrorCode.ASSET_CATEGORY_UNKNOWN,
        )
    provenance = ProvenanceRepository(connection).latest(asset.id)
    if provenance is None:
        raise ShortsThumbnailError(
            f"asset {asset.id} does not have recorded provenance",
            code=ShortsThumbnailErrorCode.PROVENANCE_MISSING,
        )
    return asset, provenance


def _normalize_title(value: str | None) -> str:
    if not isinstance(value, str):
        raise ValueError("title_text must be text")
    collapsed = " ".join(unicodedata.normalize("NFC", value).split())
    if not collapsed:
        raise ValueError("title_text must not be empty")
    if len(collapsed) > MAX_THUMBNAIL_TITLE:
        raise ValueError(
            f"title_text must be at most {MAX_THUMBNAIL_TITLE} characters"
        )
    return collapsed


def _probe_png(data: bytes) -> tuple[int, int]:
    if len(data) < 24 or data[12:16] != b"IHDR":
        raise ShortsThumbnailError(
            "PNG file is missing a valid IHDR chunk",
            code=ShortsThumbnailErrorCode.INVALID_IMAGE,
        )
    width, height = struct.unpack(">II", data[16:24])
    if width < 1 or height < 1:
        raise ShortsThumbnailError(
            "PNG dimensions must be positive",
            code=ShortsThumbnailErrorCode.INVALID_IMAGE,
        )
    return width, height


def _probe_jpeg(data: bytes) -> tuple[int, int]:
    idx = 2
    length = len(data)
    while idx + 4 <= length:
        if data[idx] != 0xFF:
            raise ShortsThumbnailError(
                "JPEG stream has a malformed marker",
                code=ShortsThumbnailErrorCode.INVALID_IMAGE,
            )
        while idx < length and data[idx] == 0xFF:
            idx += 1
        if idx >= length:
            break
        marker = data[idx]
        idx += 1
        if marker in (0xD8, 0xD9) or 0xD0 <= marker <= 0xD7 or marker == 0x01:
            continue
        if idx + 2 > length:
            break
        seg_len = struct.unpack(">H", data[idx : idx + 2])[0]
        if seg_len < 2 or idx + seg_len > length:
            raise ShortsThumbnailError(
                "JPEG segment length is truncated",
                code=ShortsThumbnailErrorCode.INVALID_IMAGE,
            )
        if marker in _SOF_MARKERS:
            if seg_len < 7:
                raise ShortsThumbnailError(
                    "JPEG SOF segment is too short",
                    code=ShortsThumbnailErrorCode.INVALID_IMAGE,
                )
            height, width = struct.unpack(">HH", data[idx + 3 : idx + 7])
            if width < 1 or height < 1:
                raise ShortsThumbnailError(
                    "JPEG dimensions must be positive",
                    code=ShortsThumbnailErrorCode.INVALID_IMAGE,
                )
            return width, height
        idx += seg_len
    raise ShortsThumbnailError(
        "JPEG file does not contain a Start of Frame (SOF) marker",
        code=ShortsThumbnailErrorCode.INVALID_IMAGE,
    )


def _probe_webp(data: bytes) -> tuple[int, int]:
    if len(data) < 30:
        raise ShortsThumbnailError(
            "WebP header is truncated",
            code=ShortsThumbnailErrorCode.INVALID_IMAGE,
        )
    chunk = data[12:16]
    if chunk == b"VP8X":
        width = 1 + int.from_bytes(data[24:27], "little")
        height = 1 + int.from_bytes(data[27:30], "little")
        return width, height
    if chunk == b"VP8 ":
        if data[23:26] != b"\x9d\x01\x2a":
            raise ShortsThumbnailError(
                "WebP VP8 keyframe signature is invalid",
                code=ShortsThumbnailErrorCode.INVALID_IMAGE,
            )
        raw_w, raw_h = struct.unpack("<HH", data[26:30])
        return raw_w & 0x3FFF, raw_h & 0x3FFF
    if chunk == b"VP8L":
        if data[20] != 0x2F:
            raise ShortsThumbnailError(
                "WebP VP8L signature is invalid",
                code=ShortsThumbnailErrorCode.INVALID_IMAGE,
            )
        bits = struct.unpack("<I", data[21:25])[0]
        width = (bits & 0x3FFF) + 1
        height = ((bits >> 14) & 0x3FFF) + 1
        return width, height
    raise ShortsThumbnailError(
        "WebP chunk is not VP8, VP8L or VP8X",
        code=ShortsThumbnailErrorCode.INVALID_IMAGE,
    )


def _png_chunk(kind: bytes, payload: bytes) -> bytes:
    crc = zlib.crc32(kind + payload) & 0xFFFFFFFF
    return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", crc)


def _artifact(
    latest: Artifact | None,
    item_id: str,
    stored: StoredFile,
    media_type: str,
    clock: Clock | None,
) -> Artifact:
    if latest is None:
        return Artifact.create(
            item_id,
            ArtifactKind.THUMBNAIL,
            uri=stored.uri,
            sha256=stored.sha256,
            size_bytes=stored.size_bytes,
            media_type=media_type,
            clock=clock,
        )
    return latest.next_version(
        uri=stored.uri,
        sha256=stored.sha256,
        size_bytes=stored.size_bytes,
        media_type=media_type,
        clock=clock,
    )


def _event_metadata(
    artifact: Artifact,
    project: ShortsProject,
    item: ContentItem,
    metadata: ThumbnailMetadata,
) -> Mapping[str, MetadataValue]:
    event: dict[str, MetadataValue] = {
        "content_item_id": artifact.content_item_id,
        "channel_id": item.channel_id,
        "project_id": project.id,
        "project_version": project.version,
        "version": artifact.version,
        "sha256": artifact.sha256,
        "size_bytes": artifact.size_bytes,
        "media_type": metadata.media_type,
        "width": metadata.width,
        "height": metadata.height,
        "source_kind": metadata.source_kind.value,
    }
    if metadata.asset_id is not None:
        event["asset_id"] = metadata.asset_id
    if metadata.provenance_id is not None:
        event["provenance_id"] = metadata.provenance_id
    return event


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
