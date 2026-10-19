"""Preview Artifact API service (Prompt Pack v8, prompt L-131), context C11 Review.

Exposes the exact rendered artifact version and companion production artifacts
for human preview before approval:
- Resolves either the latest or a specific rendered ``Artifact`` version for a
  ``ContentItem``.
- Verifies on-disk integrity (``size_bytes`` and ``sha256``) via ``ArtifactStorage``
  so a preview never serves a truncated, missing or mismatched file.
- Supports ``expected_version`` and ``expected_sha256`` guards (409 conflict when
  the caller's reference does not match the artifact).
- Supports byte-range streaming (``Range: bytes=start-end``, HTTP 206 Partial
  Content) for HTML5 video playback.
"""

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from http import HTTPStatus
from typing import Any, Final

from ai_youtube_agent.core.artifact import Artifact, ArtifactKind
from ai_youtube_agent.core.artifact_storage import ArtifactStorage
from ai_youtube_agent.core.content_item import ContentStatus, ContentType
from ai_youtube_agent.core.db.database import Database
from ai_youtube_agent.core.db.repositories.content import (
    ArtifactRepository,
    ContentItemRepository,
)
from ai_youtube_agent.core.errors import DomainError

_RANGE_HEADER_RE: Final[re.Pattern[str]] = re.compile(r"^bytes=(\d*)-(\d*)$")


class PreviewErrorCode(StrEnum):
    PREVIEW = "domain.preview"
    ITEM_NOT_FOUND = "domain.preview_item_not_found"
    ARTIFACT_NOT_FOUND = "domain.preview_artifact_not_found"
    ARTIFACT_CORRUPTED = "domain.preview_artifact_corrupted"
    ARTIFACT_MISMATCH = "domain.preview_artifact_mismatch"
    VERSION_INVALID = "domain.preview_version_invalid"
    INVALID_RANGE = "domain.preview_invalid_range"


USER_MESSAGES: Final[dict[PreviewErrorCode, str]] = {
    PreviewErrorCode.PREVIEW: "The preview could not be loaded.",
    PreviewErrorCode.ITEM_NOT_FOUND: "This content item does not exist.",
    PreviewErrorCode.ARTIFACT_NOT_FOUND: (
        "The requested preview artifact version does not exist."
    ),
    PreviewErrorCode.ARTIFACT_CORRUPTED: (
        "The stored artifact file is missing or failed integrity verification."
    ),
    PreviewErrorCode.ARTIFACT_MISMATCH: (
        "The artifact does not match the expected version or checksum."
    ),
    PreviewErrorCode.VERSION_INVALID: (
        "The requested artifact version must be a whole number of 1 or more."
    ),
    PreviewErrorCode.INVALID_RANGE: "The requested byte range is not satisfiable.",
}

_STATUS_BY_CODE: Final[dict[PreviewErrorCode, int]] = {
    PreviewErrorCode.PREVIEW: HTTPStatus.UNPROCESSABLE_ENTITY,
    PreviewErrorCode.ITEM_NOT_FOUND: HTTPStatus.NOT_FOUND,
    PreviewErrorCode.ARTIFACT_NOT_FOUND: HTTPStatus.NOT_FOUND,
    PreviewErrorCode.ARTIFACT_CORRUPTED: HTTPStatus.UNPROCESSABLE_ENTITY,
    PreviewErrorCode.ARTIFACT_MISMATCH: HTTPStatus.CONFLICT,
    PreviewErrorCode.VERSION_INVALID: HTTPStatus.UNPROCESSABLE_ENTITY,
    PreviewErrorCode.INVALID_RANGE: HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE,
}


class PreviewError(DomainError):
    default_code = PreviewErrorCode.PREVIEW.value
    default_user_message = USER_MESSAGES[PreviewErrorCode.PREVIEW]
    default_http_status = HTTPStatus.UNPROCESSABLE_ENTITY

    def __init__(
        self,
        detail: str = "",
        *,
        code: PreviewErrorCode = PreviewErrorCode.PREVIEW,
        http_status: int | None = None,
    ) -> None:
        status = (
            http_status
            if http_status is not None
            else _STATUS_BY_CODE.get(code, HTTPStatus.UNPROCESSABLE_ENTITY)
        )
        super().__init__(
            detail,
            code=code.value,
            user_message=USER_MESSAGES.get(code, USER_MESSAGES[PreviewErrorCode.PREVIEW]),
        )
        self.default_http_status = HTTPStatus(status)


@dataclass(frozen=True)
class PreviewArtifactDescriptor:
    """Metadata and safe stream URL for one exact ``Artifact`` version."""

    artifact_id: str
    content_item_id: str
    kind: ArtifactKind
    version: int
    sha256: str
    size_bytes: int
    media_type: str
    created_at: datetime
    stream_url: str
    is_latest: bool

    @classmethod
    def from_artifact(
        cls, artifact: Artifact, *, is_latest: bool
    ) -> "PreviewArtifactDescriptor":
        return cls(
            artifact_id=artifact.id,
            content_item_id=artifact.content_item_id,
            kind=artifact.kind,
            version=artifact.version,
            sha256=artifact.sha256,
            size_bytes=artifact.size_bytes,
            media_type=artifact.media_type,
            created_at=artifact.created_at,
            stream_url=(
                f"/previews/{artifact.content_item_id}/artifacts/{artifact.id}/stream"
            ),
            is_latest=is_latest,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "artifact_id": self.artifact_id,
            "content_item_id": self.content_item_id,
            "kind": self.kind.value,
            "version": self.version,
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
            "media_type": self.media_type,
            "created_at": self.created_at.isoformat(),
            "stream_url": self.stream_url,
            "is_latest": self.is_latest,
        }


@dataclass(frozen=True)
class PreviewSnapshot:
    """Exact rendered artifact versions exposed for previewing a content item."""

    content_item_id: str
    channel_id: str
    content_type: ContentType
    status: ContentStatus
    title: str
    video: PreviewArtifactDescriptor
    audio: PreviewArtifactDescriptor | None
    subtitles: PreviewArtifactDescriptor | None
    thumbnail: PreviewArtifactDescriptor | None
    metadata: PreviewArtifactDescriptor | None
    artifacts: tuple[PreviewArtifactDescriptor, ...]
    bundle_sha256: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "content_item_id": self.content_item_id,
            "channel_id": self.channel_id,
            "content_type": self.content_type.value,
            "status": self.status.value,
            "title": self.title,
            "video": self.video.as_dict(),
            "audio": self.audio.as_dict() if self.audio is not None else None,
            "subtitles": (
                self.subtitles.as_dict() if self.subtitles is not None else None
            ),
            "thumbnail": (
                self.thumbnail.as_dict() if self.thumbnail is not None else None
            ),
            "metadata": self.metadata.as_dict() if self.metadata is not None else None,
            "artifacts": [a.as_dict() for a in self.artifacts],
            "bundle_sha256": self.bundle_sha256,
        }


@dataclass(frozen=True)
class PreviewArtifactStream:
    """Verified byte payload or partial range for streaming an exact artifact."""

    descriptor: PreviewArtifactDescriptor
    data: bytes
    start_byte: int
    end_byte: int
    total_bytes: int
    is_partial: bool

    @property
    def content_range_header(self) -> str:
        return f"bytes {self.start_byte}-{self.end_byte}/{self.total_bytes}"


def _validate_positive_version(version: int, field_name: str = "version") -> int:
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        raise PreviewError(
            f"{field_name} must be an integer >= 1",
            code=PreviewErrorCode.VERSION_INVALID,
        )
    return version


def _parse_byte_range(range_header: str, total_bytes: int) -> tuple[int, int]:
    """Parse HTTP ``Range: bytes=start-end`` against ``total_bytes``."""
    match = _RANGE_HEADER_RE.match(range_header.strip())
    if match is None or total_bytes <= 0:
        raise PreviewError(
            f"invalid Range header {range_header!r}",
            code=PreviewErrorCode.INVALID_RANGE,
        )
    start_str, end_str = match.group(1), match.group(2)
    if not start_str and not end_str:
        raise PreviewError(
            "empty byte range",
            code=PreviewErrorCode.INVALID_RANGE,
        )
    if not start_str:
        suffix_len = int(end_str)
        if suffix_len <= 0:
            raise PreviewError(
                "suffix byte range must be > 0",
                code=PreviewErrorCode.INVALID_RANGE,
            )
        start = max(0, total_bytes - suffix_len)
        end = total_bytes - 1
        return (start, end)

    start = int(start_str)
    end = int(end_str) if end_str else total_bytes - 1
    if start >= total_bytes or start > end:
        raise PreviewError(
            f"byte range {start}-{end} out of bounds for {total_bytes} bytes",
            code=PreviewErrorCode.INVALID_RANGE,
        )
    end = min(end, total_bytes - 1)
    return (start, end)


def _compute_bundle_sha256(descriptors: tuple[PreviewArtifactDescriptor, ...]) -> str:
    canonical = json.dumps(
        [
            [d.kind.value, d.version, d.artifact_id, d.sha256]
            for d in descriptors
        ],
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


class PreviewArtifactService:
    """Resolves and verifies exact rendered artifact versions for preview (#131)."""

    def __init__(self, database: Database, storage: ArtifactStorage) -> None:
        self._database = database
        self._storage = storage

    def _read_and_verify(self, artifact: Artifact) -> bytes:
        try:
            raw = self._storage.read(artifact.uri)
        except Exception as error:
            raise PreviewError(
                f"artifact {artifact.id} could not be read from storage: {error}",
                code=PreviewErrorCode.ARTIFACT_CORRUPTED,
            ) from error

        if len(raw) != artifact.size_bytes:
            raise PreviewError(
                f"artifact {artifact.id} size {len(raw)} does not match recorded "
                f"{artifact.size_bytes} bytes",
                code=PreviewErrorCode.ARTIFACT_CORRUPTED,
            )

        actual_sha256 = hashlib.sha256(raw).hexdigest()
        if actual_sha256 != artifact.sha256:
            raise PreviewError(
                f"artifact {artifact.id} sha256 mismatch",
                code=PreviewErrorCode.ARTIFACT_CORRUPTED,
            )
        return raw

    def get_snapshot(
        self,
        content_item_id: str,
        *,
        video_version: int | None = None,
    ) -> PreviewSnapshot:
        """Return the preview snapshot for ``content_item_id`` at ``video_version`` (or latest)."""
        if video_version is not None:
            _validate_positive_version(video_version, "video_version")

        with self._database.transaction() as connection:
            item = ContentItemRepository(connection).get(content_item_id)
            if item is None:
                raise PreviewError(
                    f"content item {content_item_id} not found",
                    code=PreviewErrorCode.ITEM_NOT_FOUND,
                )
            all_artifacts = ArtifactRepository(connection).list_by_content_item(
                content_item_id
            )

        by_kind: dict[ArtifactKind, list[Artifact]] = {}
        for art in all_artifacts:
            by_kind.setdefault(art.kind, []).append(art)
        for kind_list in by_kind.values():
            kind_list.sort(key=lambda a: a.version)

        videos = by_kind.get(ArtifactKind.VIDEO, [])
        if not videos:
            raise PreviewError(
                f"no rendered video artifact exists for item {content_item_id}",
                code=PreviewErrorCode.ARTIFACT_NOT_FOUND,
            )

        latest_video = videos[-1]
        if video_version is None:
            selected_video = latest_video
        else:
            matching = [v for v in videos if v.version == video_version]
            if not matching:
                raise PreviewError(
                    f"video artifact version {video_version} not found for item {content_item_id}",
                    code=PreviewErrorCode.ARTIFACT_NOT_FOUND,
                )
            selected_video = matching[0]

        self._read_and_verify(selected_video)
        video_desc = PreviewArtifactDescriptor.from_artifact(
            selected_video,
            is_latest=(selected_video.version == latest_video.version),
        )

        companion_descs: dict[ArtifactKind, PreviewArtifactDescriptor | None] = {}
        for kind in (
            ArtifactKind.AUDIO,
            ArtifactKind.SUBTITLES,
            ArtifactKind.THUMBNAIL,
            ArtifactKind.METADATA,
        ):
            candidates = by_kind.get(kind, [])
            if not candidates:
                companion_descs[kind] = None
                continue
            latest_cand = candidates[-1]
            if video_version is not None:
                le_cands = [c for c in candidates if c.version <= video_version]
                chosen = le_cands[-1] if le_cands else latest_cand
            else:
                chosen = latest_cand
            self._read_and_verify(chosen)
            companion_descs[kind] = PreviewArtifactDescriptor.from_artifact(
                chosen,
                is_latest=(chosen.version == latest_cand.version),
            )

        ordered_descriptors: list[PreviewArtifactDescriptor] = [video_desc]
        for kind in (
            ArtifactKind.AUDIO,
            ArtifactKind.SUBTITLES,
            ArtifactKind.THUMBNAIL,
            ArtifactKind.METADATA,
        ):
            desc = companion_descs[kind]
            if desc is not None:
                ordered_descriptors.append(desc)

        descriptors_tuple = tuple(ordered_descriptors)
        return PreviewSnapshot(
            content_item_id=item.id,
            channel_id=item.channel_id,
            content_type=item.content_type,
            status=item.status,
            title=item.title,
            video=video_desc,
            audio=companion_descs[ArtifactKind.AUDIO],
            subtitles=companion_descs[ArtifactKind.SUBTITLES],
            thumbnail=companion_descs[ArtifactKind.THUMBNAIL],
            metadata=companion_descs[ArtifactKind.METADATA],
            artifacts=descriptors_tuple,
            bundle_sha256=_compute_bundle_sha256(descriptors_tuple),
        )

    def get_artifact(
        self,
        content_item_id: str,
        artifact_id: str,
        *,
        expected_version: int | None = None,
        expected_sha256: str | None = None,
    ) -> PreviewArtifactDescriptor:
        """Resolve and verify one exact artifact version by ID."""
        if expected_version is not None:
            _validate_positive_version(expected_version, "expected_version")

        with self._database.transaction() as connection:
            item = ContentItemRepository(connection).get(content_item_id)
            if item is None:
                raise PreviewError(
                    f"content item {content_item_id} not found",
                    code=PreviewErrorCode.ITEM_NOT_FOUND,
                )
            artifacts_repo = ArtifactRepository(connection)
            artifact = artifacts_repo.get(artifact_id)
            if artifact is None or artifact.content_item_id != item.id:
                raise PreviewError(
                    f"artifact {artifact_id} not found for item {content_item_id}",
                    code=PreviewErrorCode.ARTIFACT_NOT_FOUND,
                )
            latest = artifacts_repo.latest(item.id, artifact.kind)

        if expected_version is not None and artifact.version != expected_version:
            raise PreviewError(
                f"artifact {artifact.id} is version {artifact.version}, "
                f"expected {expected_version}",
                code=PreviewErrorCode.ARTIFACT_MISMATCH,
            )
        if (
            expected_sha256 is not None
            and artifact.sha256 != expected_sha256.strip().lower()
        ):
            raise PreviewError(
                f"artifact {artifact.id} sha256 does not match expected_sha256",
                code=PreviewErrorCode.ARTIFACT_MISMATCH,
            )

        self._read_and_verify(artifact)
        is_latest = latest is not None and latest.id == artifact.id
        return PreviewArtifactDescriptor.from_artifact(artifact, is_latest=is_latest)

    def stream_artifact(
        self,
        content_item_id: str,
        artifact_id: str,
        *,
        range_header: str | None = None,
        expected_version: int | None = None,
        expected_sha256: str | None = None,
    ) -> PreviewArtifactStream:
        """Read and verify the exact artifact bytes (or a byte range) for streaming."""
        descriptor = self.get_artifact(
            content_item_id,
            artifact_id,
            expected_version=expected_version,
            expected_sha256=expected_sha256,
        )
        with self._database.transaction() as connection:
            artifact = ArtifactRepository(connection).get(artifact_id)
            assert artifact is not None

        raw = self._read_and_verify(artifact)
        total = len(raw)

        if range_header is None or not range_header.strip():
            return PreviewArtifactStream(
                descriptor=descriptor,
                data=raw,
                start_byte=0,
                end_byte=total - 1,
                total_bytes=total,
                is_partial=False,
            )

        start, end = _parse_byte_range(range_header, total)
        return PreviewArtifactStream(
            descriptor=descriptor,
            data=raw[start : end + 1],
            start_byte=start,
            end_byte=end,
            total_bytes=total,
            is_partial=True,
        )
