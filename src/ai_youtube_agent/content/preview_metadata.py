"""Preview Metadata service (Prompt Pack v8, prompt L-133), context C11 Review.

Exposes the complete human-readable preview metadata for an exact rendered
artifact version:
- Title, description, tags, category, language, made-for-kids, and synthetic
  media disclosure status from ``ShortsMetadata`` or ``LongFormMetadata``.
- Thumbnail details (dimensions, format, version, checksum, and stream URL).
- Parsed subtitle cues (index, start/end timestamps, text, format, version).
- Formatted YouTube chapters (index, title, start/end ms, ``MM:SS`` / ``H:MM:SS``
  timestamp label).
"""

from dataclasses import dataclass
from typing import Any

from ai_youtube_agent.content.longform_metadata import LongFormMetadata
from ai_youtube_agent.content.longform_timeline import format_chapter_timestamp
from ai_youtube_agent.content.preview_artifact import (
    PreviewArtifactDescriptor,
    PreviewArtifactService,
    PreviewError,
    PreviewErrorCode,
    PreviewSnapshot,
)
from ai_youtube_agent.content.qc_video_checks import _parse_subtitle_cues
from ai_youtube_agent.content.shorts_metadata import ShortsMetadata
from ai_youtube_agent.content.shorts_thumbnail import probe_image
from ai_youtube_agent.core.artifact_storage import ArtifactStorage
from ai_youtube_agent.core.content_item import ContentStatus, ContentType
from ai_youtube_agent.core.db.database import Database
from ai_youtube_agent.core.db.repositories.content import (
    ArtifactRepository,
    ScriptRepository,
)
from ai_youtube_agent.core.db.repositories.longform import LongFormProjectRepository
from ai_youtube_agent.core.db.repositories.shorts import ShortsProjectRepository

# Metadata does not carry a category: the preview shows the publish default.
DEFAULT_PREVIEW_CATEGORY_ID = "22"


@dataclass(frozen=True)
class PreviewThumbnailView:
    """Thumbnail preview details bound to an exact ``THUMBNAIL`` artifact version."""

    artifact_id: str
    version: int
    sha256: str
    media_type: str
    size_bytes: int
    width: int | None
    height: int | None
    stream_url: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "artifact_id": self.artifact_id,
            "version": self.version,
            "sha256": self.sha256,
            "media_type": self.media_type,
            "size_bytes": self.size_bytes,
            "width": self.width,
            "height": self.height,
            "stream_url": self.stream_url,
        }


@dataclass(frozen=True)
class PreviewSubtitleCue:
    """One parsed subtitle cue shown in the preview metadata view."""

    index: int
    start_ms: int
    end_ms: int
    start_label: str
    end_label: str
    text: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "start_ms": self.start_ms,
            "end_ms": self.end_ms,
            "start_label": self.start_label,
            "end_label": self.end_label,
            "text": self.text,
        }


@dataclass(frozen=True)
class PreviewSubtitlesView:
    """Subtitles preview details and parsed cues for a ``SUBTITLES`` artifact."""

    artifact_id: str
    version: int
    sha256: str
    media_type: str
    subtitle_format: str
    cue_count: int
    cues: tuple[PreviewSubtitleCue, ...]
    stream_url: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "artifact_id": self.artifact_id,
            "version": self.version,
            "sha256": self.sha256,
            "media_type": self.media_type,
            "subtitle_format": self.subtitle_format,
            "cue_count": self.cue_count,
            "cues": [c.as_dict() for c in self.cues],
            "stream_url": self.stream_url,
        }


@dataclass(frozen=True)
class PreviewChapterView:
    """One chapter marker shown in the preview metadata view."""

    index: int
    title: str
    start_ms: int
    end_ms: int
    timestamp_label: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "title": self.title,
            "start_ms": self.start_ms,
            "end_ms": self.end_ms,
            "timestamp_label": self.timestamp_label,
        }


@dataclass(frozen=True)
class PreviewMetadataView:
    """Complete preview metadata view.

    (title, description, thumbnail, subtitles, chapters) (#133).
    """

    content_item_id: str
    channel_id: str
    content_type: ContentType
    status: ContentStatus
    video: PreviewArtifactDescriptor
    metadata_artifact: PreviewArtifactDescriptor
    title: str
    description: str
    tags: tuple[str, ...]
    category_id: str
    language: str
    made_for_kids: bool
    contains_synthetic_media: bool
    disclosure_decision_id: str
    policy_status: str
    project_id: str
    project_version: int
    script_id: str
    script_version: int
    thumbnail: PreviewThumbnailView | None
    subtitles: PreviewSubtitlesView | None
    chapters: tuple[PreviewChapterView, ...]
    is_current: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "content_item_id": self.content_item_id,
            "channel_id": self.channel_id,
            "content_type": self.content_type.value,
            "status": self.status.value,
            "video": self.video.as_dict(),
            "metadata_artifact": self.metadata_artifact.as_dict(),
            "title": self.title,
            "description": self.description,
            "tags": list(self.tags),
            "category_id": self.category_id,
            "language": self.language,
            "made_for_kids": self.made_for_kids,
            "contains_synthetic_media": self.contains_synthetic_media,
            "disclosure_decision_id": self.disclosure_decision_id,
            "policy_status": self.policy_status,
            "project_id": self.project_id,
            "project_version": self.project_version,
            "script_id": self.script_id,
            "script_version": self.script_version,
            "thumbnail": self.thumbnail.as_dict()
            if self.thumbnail is not None
            else None,
            "subtitles": self.subtitles.as_dict()
            if self.subtitles is not None
            else None,
            "chapters": [c.as_dict() for c in self.chapters],
            "is_current": self.is_current,
        }


def _format_cue_time(ms: int) -> str:
    hours = ms // 3_600_000
    rem = ms % 3_600_000
    minutes = rem // 60_000
    sec_rem = rem % 60_000
    seconds = sec_rem // 1000
    millis = sec_rem % 1000
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}.{millis:03d}"


class PreviewMetadataService:
    """Loads and formats title, description, thumbnail, subtitles and chapters.

    For preview (#133).
    """

    def __init__(
        self,
        database: Database,
        storage: ArtifactStorage,
        artifact_service: PreviewArtifactService | None = None,
    ) -> None:
        self._database = database
        self._storage = storage
        self._artifacts = artifact_service or PreviewArtifactService(database, storage)

    def get_preview_metadata(
        self,
        content_item_id: str,
        *,
        video_version: int | None = None,
    ) -> PreviewMetadataView:
        """Build ``PreviewMetadataView`` for ``content_item_id``."""
        snapshot: PreviewSnapshot = self._artifacts.get_snapshot(
            content_item_id, video_version=video_version
        )
        if snapshot.metadata is None:
            raise PreviewError(
                f"no publish metadata artifact exists for item {content_item_id}",
                code=PreviewErrorCode.ARTIFACT_NOT_FOUND,
            )

        with self._database.transaction() as connection:
            artifacts_repo = ArtifactRepository(connection)
            meta_art = artifacts_repo.get(snapshot.metadata.artifact_id)
            assert meta_art is not None
            thumb_art = (
                artifacts_repo.get(snapshot.thumbnail.artifact_id)
                if snapshot.thumbnail is not None
                else None
            )
            sub_art = (
                artifacts_repo.get(snapshot.subtitles.artifact_id)
                if snapshot.subtitles is not None
                else None
            )
            scripts = ScriptRepository(connection).list_by_content_item(content_item_id)
            latest_script = scripts[-1] if scripts else None
            shorts_proj = (
                ShortsProjectRepository(connection).get_by_content_item(content_item_id)
                if snapshot.content_type == ContentType.SHORTS
                else None
            )
            lf_proj = (
                LongFormProjectRepository(connection).get_by_content_item(
                    content_item_id
                )
                if snapshot.content_type == ContentType.LONGFORM
                else None
            )

        raw_meta = self._storage.read(meta_art.uri)
        try:
            if snapshot.content_type == ContentType.SHORTS:
                parsed_meta: ShortsMetadata | LongFormMetadata = (
                    ShortsMetadata.from_json_bytes(raw_meta)
                )
            else:
                parsed_meta = LongFormMetadata.from_bytes(raw_meta)
        except Exception as error:
            raise PreviewError(
                f"metadata artifact {meta_art.id} is invalid: {error}",
                code=PreviewErrorCode.ARTIFACT_CORRUPTED,
            ) from error

        chapters: list[PreviewChapterView] = []
        if isinstance(parsed_meta, LongFormMetadata):
            for idx, ch in enumerate(parsed_meta.chapters, start=1):
                chapters.append(
                    PreviewChapterView(
                        index=idx,
                        title=ch.title,
                        start_ms=ch.start_ms,
                        end_ms=ch.end_ms,
                        timestamp_label=format_chapter_timestamp(ch.start_ms),
                    )
                )

        thumbnail_view: PreviewThumbnailView | None = None
        if thumb_art is not None and snapshot.thumbnail is not None:
            raw_thumb = self._storage.read(thumb_art.uri)
            t_width: int | None = None
            t_height: int | None = None
            try:
                info = probe_image(raw_thumb)
                t_width, t_height = info.width, info.height
            except Exception:
                pass
            thumbnail_view = PreviewThumbnailView(
                artifact_id=snapshot.thumbnail.artifact_id,
                version=snapshot.thumbnail.version,
                sha256=snapshot.thumbnail.sha256,
                media_type=snapshot.thumbnail.media_type,
                size_bytes=snapshot.thumbnail.size_bytes,
                width=t_width,
                height=t_height,
                stream_url=snapshot.thumbnail.stream_url,
            )

        subtitles_view: PreviewSubtitlesView | None = None
        if sub_art is not None and snapshot.subtitles is not None:
            raw_sub = self._storage.read(sub_art.uri)
            try:
                sub_text = raw_sub.decode("utf-8")
                fmt_name, raw_cues = _parse_subtitle_cues(
                    sub_text, snapshot.subtitles.media_type
                )
            except Exception as error:
                raise PreviewError(
                    f"subtitles artifact {sub_art.id} is invalid: {error}",
                    code=PreviewErrorCode.ARTIFACT_CORRUPTED,
                ) from error

            cue_views = tuple(
                PreviewSubtitleCue(
                    index=c.index,
                    start_ms=c.start_ms,
                    end_ms=c.end_ms,
                    start_label=_format_cue_time(c.start_ms),
                    end_label=_format_cue_time(c.end_ms),
                    text=c.text,
                )
                for c in raw_cues
            )
            subtitles_view = PreviewSubtitlesView(
                artifact_id=snapshot.subtitles.artifact_id,
                version=snapshot.subtitles.version,
                sha256=snapshot.subtitles.sha256,
                media_type=snapshot.subtitles.media_type,
                subtitle_format=fmt_name,
                cue_count=len(cue_views),
                cues=cue_views,
                stream_url=snapshot.subtitles.stream_url,
            )

        is_current = snapshot.video.is_latest and snapshot.metadata.is_latest
        if (
            latest_script is not None
            and parsed_meta.script_version != latest_script.version
        ):
            is_current = False
        if (
            shorts_proj is not None
            and parsed_meta.project_version != shorts_proj.version
        ):
            is_current = False
        if lf_proj is not None and parsed_meta.project_version != lf_proj.version:
            is_current = False

        return PreviewMetadataView(
            content_item_id=snapshot.content_item_id,
            channel_id=snapshot.channel_id,
            content_type=snapshot.content_type,
            status=snapshot.status,
            video=snapshot.video,
            metadata_artifact=snapshot.metadata,
            title=parsed_meta.title,
            description=parsed_meta.description,
            tags=parsed_meta.tags,
            category_id=DEFAULT_PREVIEW_CATEGORY_ID,
            language=parsed_meta.language,
            made_for_kids=parsed_meta.made_for_kids,
            contains_synthetic_media=parsed_meta.contains_synthetic_media,
            disclosure_decision_id=parsed_meta.disclosure_decision_id,
            policy_status=parsed_meta.policy_status.value,
            project_id=parsed_meta.project_id,
            project_version=parsed_meta.project_version,
            script_id=parsed_meta.script_id,
            script_version=parsed_meta.script_version,
            thumbnail=thumbnail_view,
            subtitles=subtitles_view,
            chapters=tuple(chapters),
            is_current=is_current,
        )
