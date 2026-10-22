"""Read views for the local web app (APP-01), context X3 Command Center.

The web app only reads through these views; it sends no publish, approve or
upload action. As the user approved for APP-01:

- ``ContentItemViewService`` lists content items (newest change first) with
  the latest ``pipeline.script_to_video`` job and video of each, shows one
  item with all its jobs and checkpoints, its stored script, its scenes and
  serves its verified MP4 as a download. Lists never read artifact files.
- Scenes come from the manifest of the newest run of this process when its
  video is still the latest one (``run_manifest``); otherwise they are rebuilt
  from persisted data (``persisted``): the scenes of the Shorts or LongForm
  timeline, each with the image of its background asset (attached with the
  purpose ``scene:{scene_id}:background``, stored as a THUMBNAIL artifact).
  The timeline scene of section ``i`` is the planner scene ``scene_{i+1:02d}``.
- ``SystemSettingsView`` shows a whitelist of the settings: no paths and no
  secrets.
"""

from __future__ import annotations

import re
import shutil
import unicodedata
from dataclasses import dataclass
from enum import StrEnum
from http import HTTPStatus
from pathlib import Path
from typing import Any, Final
from urllib.parse import quote

from ai_youtube_agent import __version__
from ai_youtube_agent.content.preview_artifact import (
    PreviewArtifactDescriptor,
    PreviewArtifactService,
)
from ai_youtube_agent.content.scene_planner import SceneStatus
from ai_youtube_agent.content.shorts_timeline import AssetRole
from ai_youtube_agent.core.artifact import Artifact, ArtifactKind
from ai_youtube_agent.core.config import Settings
from ai_youtube_agent.core.content_item import ContentItem, ContentStatus, ContentType
from ai_youtube_agent.core.db.database import Database
from ai_youtube_agent.core.db.repositories.asset import (
    AssetRepository,
    AssetUsageRepository,
)
from ai_youtube_agent.core.db.repositories.channel import ChannelRepository
from ai_youtube_agent.core.db.repositories.content import (
    ArtifactRepository,
    ContentItemRepository,
    ScriptRepository,
)
from ai_youtube_agent.core.db.repositories.jobs import AIJobRepository
from ai_youtube_agent.core.db.repositories.longform import LongFormProjectRepository
from ai_youtube_agent.core.db.repositories.shorts import ShortsProjectRepository
from ai_youtube_agent.core.errors import DomainError
from ai_youtube_agent.pipeline.job import AIJob
from ai_youtube_agent.pipeline.script_to_video import (
    SCRIPT_TO_VIDEO_JOB_KIND,
    VideoProjectManifest,
)
from ai_youtube_agent.pipeline.script_to_video_runner import (
    ScriptToVideoRunner,
    job_as_dict,
    job_progress,
)

DEFAULT_LIST_LIMIT: Final = 50
SCENE_PURPOSE_PATTERN: Final = re.compile(r"^scene:(.+):background$")


class AppViewErrorCode(StrEnum):
    CONTENT_ITEM_NOT_FOUND = "domain.content_item_not_found"
    SCRIPT_NOT_FOUND = "domain.script_not_found"
    VIDEO_NOT_FOUND = "domain.video_not_found"


USER_MESSAGES: Final[dict[AppViewErrorCode, str]] = {
    AppViewErrorCode.CONTENT_ITEM_NOT_FOUND: "This content item does not exist.",
    AppViewErrorCode.SCRIPT_NOT_FOUND: "This content item has no stored script.",
    AppViewErrorCode.VIDEO_NOT_FOUND: "This content item has no such video.",
}


class AppViewError(DomainError):
    """A read view refused the request; every code is a 404."""

    default_code = AppViewErrorCode.CONTENT_ITEM_NOT_FOUND.value
    default_user_message = USER_MESSAGES[AppViewErrorCode.CONTENT_ITEM_NOT_FOUND]
    default_http_status = HTTPStatus.NOT_FOUND

    def __init__(self, detail: str = "", *, code: AppViewErrorCode) -> None:
        super().__init__(
            detail,
            code=code.value,
            user_message=USER_MESSAGES[code],
            http_status=HTTPStatus.NOT_FOUND,
        )


@dataclass(frozen=True)
class VideoDownload:
    """The verified bytes of one MP4 and the headers to send them with."""

    data: bytes
    media_type: str
    sha256: str
    content_disposition: str


class ContentItemViewService:
    """Read views over content items, their jobs, scripts, scenes and video."""

    def __init__(
        self,
        database: Database,
        preview_service: PreviewArtifactService,
        runner: ScriptToVideoRunner,
    ) -> None:
        self._database = database
        self._preview = preview_service
        self._runner = runner

    def list_items(
        self,
        *,
        status: ContentStatus | None = None,
        content_type: ContentType | None = None,
        limit: int = DEFAULT_LIST_LIMIT,
    ) -> dict[str, Any]:
        with self._database.transaction() as connection:
            items_repo = ContentItemRepository(connection)
            items = [
                item
                for channel in ChannelRepository(connection).list_all()
                for item in items_repo.list_by_channel(channel.id)
                if (status is None or item.status is status)
                and (content_type is None or item.content_type is content_type)
            ]
            items.sort(key=lambda item: (item.updated_at, item.id), reverse=True)
            jobs_repo = AIJobRepository(connection)
            artifacts_repo = ArtifactRepository(connection)
            rows = [
                (
                    item,
                    _latest_script_job(jobs_repo.list_by_content_item(item.id)),
                    artifacts_repo.latest(item.id, ArtifactKind.VIDEO),
                )
                for item in items[:limit]
            ]
        return {
            "items": [
                {
                    **_item_summary(item),
                    "latest_job": self._job_summary(job),
                    "active_run_id": self._runner.active_run_id(item.id),
                    "video": _video_summary(video),
                }
                for item, job, video in rows
            ],
            "total": len(items),
        }

    def item_detail(self, content_item_id: str) -> dict[str, Any]:
        with self._database.transaction() as connection:
            item = _require_item(connection, content_item_id)
            jobs = AIJobRepository(connection).list_by_content_item(item.id)
            artifacts_repo = ArtifactRepository(connection)
            latest = {
                kind: artifacts_repo.latest(item.id, kind)
                for kind in ArtifactKind
                if kind is not ArtifactKind.THUMBNAIL
            }
            scene_artifact_ids = {
                asset.artifact_id
                for asset in AssetRepository(connection).list_by_content_item(item.id)
                if asset.artifact_id
            }
            thumbnails = [
                artifact
                for artifact in artifacts_repo.list_by_content_item(
                    item.id, kind=ArtifactKind.THUMBNAIL
                )
                if artifact.id not in scene_artifact_ids
            ]
            latest[ArtifactKind.THUMBNAIL] = thumbnails[-1] if thumbnails else None

        script_job = _latest_script_job(jobs)
        progress = (
            job_progress(script_job, self._runner.job_status(script_job)).as_dict()
            if script_job is not None
            else None
        )
        return {
            "item": _item_summary(item),
            "jobs": [
                {**job_as_dict(job), "status": self._runner.job_status(job).value}
                for job in jobs
            ],
            "latest_job": self._job_summary(script_job),
            "progress": progress,
            "artifacts": {
                kind.value: (
                    PreviewArtifactDescriptor.from_artifact(
                        artifact, is_latest=True
                    ).as_dict()
                    if artifact is not None
                    else None
                )
                for kind, artifact in latest.items()
            },
            "active_run_id": self._runner.active_run_id(item.id),
        }

    def script(self, content_item_id: str) -> dict[str, Any]:
        with self._database.transaction() as connection:
            item = _require_item(connection, content_item_id)
            versions = ScriptRepository(connection).list_by_content_item(item.id)
        if not versions:
            raise AppViewError(
                f"content item {item.id} has no script",
                code=AppViewErrorCode.SCRIPT_NOT_FOUND,
            )
        script = versions[-1]
        return {
            "script_id": script.id,
            "version": script.version,
            "title": item.title,
            "text": "\n\n".join(section.text for section in script.sections),
            "sections": [
                {
                    "kind": section.kind.value,
                    "title": section.title,
                    "text": section.text,
                }
                for section in script.sections
            ],
        }

    def scenes(self, content_item_id: str) -> dict[str, Any]:
        with self._database.transaction() as connection:
            item = _require_item(connection, content_item_id)
            video = ArtifactRepository(connection).latest(item.id, ArtifactKind.VIDEO)

        manifest = self._runner.latest_manifest(item.id)
        if (
            manifest is not None
            and video is not None
            and manifest.video_artifact_id == video.id
        ):
            return {"source": "run_manifest", "scenes": _manifest_scenes(manifest)}
        return self._persisted_scenes(item, rendered=video is not None)

    def _persisted_scenes(self, item: ContentItem, *, rendered: bool) -> dict[str, Any]:
        with self._database.transaction() as connection:
            if item.content_type is ContentType.SHORTS:
                project = ShortsProjectRepository(connection).get_by_content_item(
                    item.id
                )
            else:
                project = LongFormProjectRepository(connection).get_by_content_item(
                    item.id
                )
            scene_ids: dict[str, str] = {}
            for usage in AssetUsageRepository(connection).list_by_content_item(item.id):
                match = SCENE_PURPOSE_PATTERN.match(usage.purpose or "")
                if match is not None:
                    scene_ids.setdefault(usage.asset_id, match.group(1))
            assets = {
                asset.id: asset
                for asset in AssetRepository(connection).list_by_content_item(item.id)
            }
            artifacts_repo = ArtifactRepository(connection)

            def image_of(asset_id: str | None) -> Artifact | None:
                asset = assets.get(asset_id) if asset_id else None
                if asset is None or not asset.artifact_id:
                    return None
                return artifacts_repo.get(asset.artifact_id)

            scenes: list[dict[str, Any]] = []
            timeline = project.timeline.scenes if project is not None else ()
            if timeline:
                for index, scene in enumerate(timeline):
                    background = next(
                        (
                            placed.asset_id
                            for placed in scene.assets
                            if placed.role is AssetRole.BACKGROUND
                        ),
                        None,
                    )
                    image = image_of(background)
                    scenes.append(
                        _persisted_scene(
                            scene_id=scene_ids.get(background or "")
                            or f"scene_{scene.section_index + 1:02d}",
                            index=index,
                            title=scene.title,
                            start_ms=scene.start_ms,
                            end_ms=scene.end_ms,
                            image=image,
                            rendered=rendered,
                        )
                    )
            else:
                # No timeline: the scene images in the order they were attached.
                for index, (asset_id, scene_id) in enumerate(scene_ids.items()):
                    scenes.append(
                        _persisted_scene(
                            scene_id=scene_id,
                            index=index,
                            title=None,
                            start_ms=None,
                            end_ms=None,
                            image=image_of(asset_id),
                            rendered=rendered,
                        )
                    )
        return {"source": "persisted" if scenes else None, "scenes": scenes}

    def video_download(
        self, content_item_id: str, artifact_id: str | None = None
    ) -> VideoDownload:
        with self._database.transaction() as connection:
            item = _require_item(connection, content_item_id)
            artifacts_repo = ArtifactRepository(connection)
            video = (
                artifacts_repo.latest(item.id, ArtifactKind.VIDEO)
                if artifact_id is None
                else artifacts_repo.get(artifact_id)
            )
        if (
            video is None
            or video.content_item_id != item.id
            or video.kind is not ArtifactKind.VIDEO
        ):
            raise AppViewError(
                f"no video {artifact_id or 'latest'} for item {item.id}",
                code=AppViewErrorCode.VIDEO_NOT_FOUND,
            )
        # Verifies the size and sha256 of the stored file before it is sent.
        stream = self._preview.stream_artifact(item.id, video.id)
        return VideoDownload(
            data=stream.data,
            media_type=stream.descriptor.media_type,
            sha256=stream.descriptor.sha256,
            content_disposition=content_disposition(item.title, video.version),
        )

    def _job_summary(self, job: AIJob | None) -> dict[str, Any] | None:
        if job is None:
            return None
        last = job.last_checkpoint
        return {
            "id": job.id,
            "status": self._runner.job_status(job).value,
            "attempts": job.attempts,
            "last_error": job.last_error,
            "last_stage": last.step if last is not None else None,
            "updated_at": job.updated_at.isoformat(),
        }


class SystemSettingsView:
    """The settings the web app may show: a whitelist without paths or secrets."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def as_dict(self) -> dict[str, Any]:
        settings = self._settings
        return {
            "app_name": settings.app_name,
            "version": __version__,
            "environment": settings.environment.value,
            "debug": settings.debug,
            "log_level": settings.log_level.value,
            "providers": {
                "research": settings.research_provider.value,
                "text": settings.text_provider.value,
                "voice": settings.voice_provider.value,
                "render": settings.render_provider.value,
            },
            "render": {
                "provider": settings.render_provider.value,
                "ffmpeg_binary_name": Path(settings.ffmpeg_binary).name,
                "ffmpeg_found": shutil.which(settings.ffmpeg_binary) is not None,
                "timeout_seconds": settings.render_timeout_seconds,
                "font_file_configured": settings.render_font_file is not None,
            },
            "flags": settings.flags.model_dump(),
            "approval_ttl_hours": settings.approval_ttl_hours,
            "policy_rule_set_version": settings.policy_rule_set_version,
            "rights_block_levels": sorted(
                level.value for level in settings.rights_block_levels
            ),
            "job_max_attempts": settings.job_max_attempts,
            "upload_max_attempts": settings.upload_max_attempts,
        }


def content_disposition(title: str, version: int) -> str:
    """An attachment header that is latin-1 safe for any title.

    ``filename`` is an ASCII slug for old clients and ``filename*`` the exact
    title, percent-encoded as UTF-8 (RFC 6266 / RFC 5987).
    """
    ascii_title = (
        unicodedata.normalize("NFKD", title.replace("đ", "d").replace("Đ", "D"))
        .encode("ascii", "ignore")
        .decode("ascii")
    )
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_title.lower()).strip("-")[:60].strip("-")
    fallback = f"{slug or 'video'}-v{version}.mp4"
    exact = quote(f"{title.strip() or 'video'}-v{version}.mp4", safe="")
    return f"attachment; filename=\"{fallback}\"; filename*=UTF-8''{exact}"


def _require_item(connection: Any, content_item_id: str) -> ContentItem:
    item = ContentItemRepository(connection).get(content_item_id)
    if item is None:
        raise AppViewError(
            f"content item {content_item_id} not found",
            code=AppViewErrorCode.CONTENT_ITEM_NOT_FOUND,
        )
    return item


def _latest_script_job(jobs: list[AIJob]) -> AIJob | None:
    script_jobs = [job for job in jobs if job.kind == SCRIPT_TO_VIDEO_JOB_KIND]
    return max(script_jobs, key=lambda job: job.updated_at, default=None)


def _item_summary(item: ContentItem) -> dict[str, Any]:
    return {
        "id": item.id,
        "title": item.title,
        "channel_id": item.channel_id,
        "content_type": item.content_type.value,
        "status": item.status.value,
        "created_at": item.created_at.isoformat(),
        "updated_at": item.updated_at.isoformat(),
    }


def _video_summary(video: Artifact | None) -> dict[str, Any] | None:
    if video is None:
        return None
    return {
        "artifact_id": video.id,
        "version": video.version,
        "size_bytes": video.size_bytes,
        "media_type": video.media_type,
        "stream_url": _stream_url(video),
    }


def _image(artifact: Artifact | None) -> dict[str, Any] | None:
    if artifact is None:
        return None
    return {
        "artifact_id": artifact.id,
        "media_type": artifact.media_type,
        "stream_url": _stream_url(artifact),
    }


def _stream_url(artifact: Artifact) -> str:
    return f"/previews/{artifact.content_item_id}/artifacts/{artifact.id}/stream"


def _manifest_scenes(manifest: VideoProjectManifest) -> list[dict[str, Any]]:
    assets = {record.scene_id: record for record in manifest.scene_assets}
    scenes: list[dict[str, Any]] = []
    for scene in manifest.scene_breakdown.scenes:
        record = assets.get(scene.scene_id)
        scenes.append(
            {
                "scene_id": scene.scene_id,
                "index": scene.order_index,
                "title": scene.title,
                "start_ms": scene.start_ms,
                "end_ms": scene.end_ms,
                "duration_ms": scene.duration_ms,
                "visual_prompt": scene.visual_prompt,
                "narration": scene.narration_and_dialogue,
                "status": scene.status.value,
                "image": (
                    {
                        "artifact_id": record.artifact_id,
                        "media_type": record.media_type,
                        "stream_url": (
                            f"/previews/{manifest.content_item_id}/artifacts/"
                            f"{record.artifact_id}/stream"
                        ),
                    }
                    if record is not None
                    else None
                ),
                "reused": record.reused if record is not None else False,
            }
        )
    return scenes


def _persisted_scene(
    *,
    scene_id: str,
    index: int,
    title: str | None,
    start_ms: int | None,
    end_ms: int | None,
    image: Artifact | None,
    rendered: bool,
) -> dict[str, Any]:
    if rendered:
        status = SceneStatus.RENDERED
    elif image is not None:
        status = SceneStatus.ASSETS_READY
    else:
        status = SceneStatus.PLANNED
    return {
        "scene_id": scene_id,
        "index": index,
        "title": title,
        "start_ms": start_ms,
        "end_ms": end_ms,
        "duration_ms": (
            end_ms - start_ms if start_ms is not None and end_ms is not None else None
        ),
        "status": status.value,
        "image": _image(image),
        "reused": False,
    }


__all__ = [
    "AppViewError",
    "AppViewErrorCode",
    "ContentItemViewService",
    "SystemSettingsView",
    "VideoDownload",
    "content_disposition",
]
