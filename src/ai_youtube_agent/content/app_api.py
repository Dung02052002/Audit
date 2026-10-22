"""HTTP API of the local web app's read views (APP-01), context X3.

Exposes:
- ``GET /content-items``: items, newest change first, filtered by ``status``
  and ``content_type``, at most ``limit`` (1..200, default 50).
- ``GET /content-items/{content_item_id}``: one item, all its jobs with their
  checkpoints, the progress of its latest script-to-video job and the latest
  artifact of each kind.
- ``GET /content-items/{content_item_id}/script``: the latest stored script.
- ``GET /content-items/{content_item_id}/scenes``: the scenes and their images.
- ``GET /content-items/{content_item_id}/video/download``: the verified MP4 as
  an attachment (the latest video, or ``artifact_id``).
- ``GET /system/settings``: a whitelist of the settings.

Nothing here publishes, approves or uploads.
"""

from typing import Annotated, Any

from fastapi import APIRouter, Query
from fastapi.responses import Response

from ai_youtube_agent.content.app_views import (
    DEFAULT_LIST_LIMIT,
    ContentItemViewService,
    SystemSettingsView,
)
from ai_youtube_agent.core.content_item import ContentStatus, ContentType
from ai_youtube_agent.core.http import provide

router = APIRouter(prefix="/content-items", tags=["content-items"])
system_router = APIRouter(prefix="/system", tags=["system"])

ViewServiceDep = Annotated[ContentItemViewService, provide(ContentItemViewService)]
SettingsViewDep = Annotated[SystemSettingsView, provide(SystemSettingsView)]


@router.get("")
def list_content_items(
    service: ViewServiceDep,
    status: ContentStatus | None = None,
    content_type: ContentType | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = DEFAULT_LIST_LIMIT,
) -> dict[str, Any]:
    return service.list_items(status=status, content_type=content_type, limit=limit)


@router.get("/{content_item_id}")
def get_content_item(content_item_id: str, service: ViewServiceDep) -> dict[str, Any]:
    return service.item_detail(content_item_id)


@router.get("/{content_item_id}/script")
def get_content_item_script(
    content_item_id: str, service: ViewServiceDep
) -> dict[str, Any]:
    return service.script(content_item_id)


@router.get("/{content_item_id}/scenes")
def get_content_item_scenes(
    content_item_id: str, service: ViewServiceDep
) -> dict[str, Any]:
    return service.scenes(content_item_id)


@router.get("/{content_item_id}/video/download")
def download_content_item_video(
    content_item_id: str,
    service: ViewServiceDep,
    artifact_id: str | None = None,
) -> Response:
    download = service.video_download(content_item_id, artifact_id)
    return Response(
        content=download.data,
        media_type=download.media_type,
        headers={
            "Content-Disposition": download.content_disposition,
            "Content-Length": str(len(download.data)),
            "ETag": f'"{download.sha256}"',
        },
    )


@system_router.get("/settings")
def get_system_settings(view: SettingsViewDep) -> dict[str, Any]:
    return view.as_dict()
