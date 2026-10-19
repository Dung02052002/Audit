"""Preview HTTP API (Prompt Pack v8, prompts #131–#136), context C11 Review.

Exposes the HTTP endpoints used by the review UI and Command Center to inspect
rendered preview artifacts, player configuration/HTML, metadata, QC/rights/policy
reports, version comparisons, and the preview edit loop:

- ``GET /previews/{content_item_id}`` (#131): exact rendered artifact snapshot.
- ``GET /previews/{content_item_id}/artifacts/{artifact_id}`` (#131): exact
  artifact descriptor with optional ``expected_version`` and ``expected_sha256``.
- ``GET /previews/{content_item_id}/artifacts/{artifact_id}/stream`` (#131):
  verified byte stream with HTTP ``Range`` (206 Partial Content) support.
- ``GET /previews/{content_item_id}/player`` (#132): player configuration JSON.
- ``GET /previews/{content_item_id}/player.html`` (#132): rendered HTML5 player.
- ``GET /previews/{content_item_id}/subtitles.vtt`` (#132): WebVTT subtitle track.
- ``GET /previews/{content_item_id}/metadata`` (#133): title, description,
  thumbnail, subtitles and chapters.
- ``GET /previews/{content_item_id}/reports`` (#134): QC, rights and policy reports.
- ``GET /previews/{content_item_id}/compare`` (#135): version comparison diff.
- ``POST /previews/{content_item_id}/edit-loop`` (#136): change -> rerender ->
  retest -> new preview cycle.
"""

from http import HTTPStatus
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel, ConfigDict

from ai_youtube_agent.content.preview_artifact import PreviewArtifactService
from ai_youtube_agent.content.preview_edit_loop import (
    PreviewEditChanges,
    PreviewEditLoopService,
)
from ai_youtube_agent.content.preview_metadata import PreviewMetadataService
from ai_youtube_agent.content.preview_player import PreviewPlayerService
from ai_youtube_agent.content.preview_reports import PreviewReportService
from ai_youtube_agent.content.version_comparison import VersionComparisonService
from ai_youtube_agent.core.audit import Actor
from ai_youtube_agent.core.http import current_actor, provide

router = APIRouter(prefix="/previews", tags=["previews"])

ArtifactServiceDep = Annotated[
    PreviewArtifactService, provide(PreviewArtifactService)
]
PlayerServiceDep = Annotated[PreviewPlayerService, provide(PreviewPlayerService)]
MetadataServiceDep = Annotated[
    PreviewMetadataService, provide(PreviewMetadataService)
]
ReportServiceDep = Annotated[PreviewReportService, provide(PreviewReportService)]
ComparisonServiceDep = Annotated[
    VersionComparisonService, provide(VersionComparisonService)
]
EditLoopServiceDep = Annotated[
    PreviewEditLoopService, provide(PreviewEditLoopService)
]
CurrentActor = Annotated[Actor, Depends(current_actor)]


class PreviewEditRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str | None = None
    description: str | None = None
    tags: list[str] | None = None
    rerender_video: bool = True
    regenerate_thumbnail: bool = False
    subtitle_text: str | None = None
    subtitle_media_type: str | None = None

    def to_changes(self) -> PreviewEditChanges:
        return PreviewEditChanges(
            title=self.title,
            description=self.description,
            tags=tuple(self.tags) if self.tags is not None else None,
            rerender_video=self.rerender_video,
            regenerate_thumbnail=self.regenerate_thumbnail,
            subtitle_bytes=(
                self.subtitle_text.encode("utf-8")
                if self.subtitle_text is not None
                else None
            ),
            subtitle_media_type=self.subtitle_media_type,
        )


@router.get("/{content_item_id}")
def get_preview_snapshot(
    content_item_id: str,
    service: ArtifactServiceDep,
    version: int | None = None,
) -> dict[str, Any]:
    return service.get_snapshot(
        content_item_id, video_version=version
    ).as_dict()


@router.get("/{content_item_id}/artifacts/{artifact_id}")
def get_preview_artifact(
    content_item_id: str,
    artifact_id: str,
    service: ArtifactServiceDep,
    expected_version: int | None = None,
    expected_sha256: str | None = None,
) -> dict[str, Any]:
    return service.get_artifact(
        content_item_id,
        artifact_id,
        expected_version=expected_version,
        expected_sha256=expected_sha256,
    ).as_dict()


@router.get("/{content_item_id}/artifacts/{artifact_id}/stream")
def stream_preview_artifact(
    content_item_id: str,
    artifact_id: str,
    service: ArtifactServiceDep,
    expected_version: int | None = None,
    expected_sha256: str | None = None,
    range_header: Annotated[str | None, Header(alias="Range")] = None,
) -> Response:
    stream = service.stream_artifact(
        content_item_id,
        artifact_id,
        range_header=range_header,
        expected_version=expected_version,
        expected_sha256=expected_sha256,
    )
    headers = {
        "Accept-Ranges": "bytes",
        "Content-Length": str(len(stream.data)),
        "ETag": f'"{stream.descriptor.sha256}"',
        "X-Artifact-Id": stream.descriptor.artifact_id,
        "X-Artifact-Version": str(stream.descriptor.version),
    }
    if stream.is_partial:
        headers["Content-Range"] = stream.content_range_header
        status_code = HTTPStatus.PARTIAL_CONTENT
    else:
        status_code = HTTPStatus.OK
    return Response(
        content=stream.data,
        status_code=status_code,
        media_type=stream.descriptor.media_type,
        headers=headers,
    )


@router.get("/{content_item_id}/player")
def get_preview_player(
    content_item_id: str,
    service: PlayerServiceDep,
    version: int | None = None,
) -> dict[str, Any]:
    return service.build_player(
        content_item_id, video_version=version
    ).as_dict()


@router.get("/{content_item_id}/player.html")
def get_preview_player_html(
    content_item_id: str,
    service: PlayerServiceDep,
    version: int | None = None,
) -> HTMLResponse:
    html_text = service.render_player_html(
        content_item_id, video_version=version
    )
    return HTMLResponse(content=html_text)


@router.get("/{content_item_id}/subtitles.vtt")
def get_preview_subtitles_vtt(
    content_item_id: str,
    service: PlayerServiceDep,
    artifact_id: str | None = None,
    expected_sha256: str | None = None,
) -> Response:
    desc, vtt_text = service.subtitles_as_vtt(
        content_item_id,
        artifact_id=artifact_id,
        expected_sha256=expected_sha256,
    )
    return Response(
        content=vtt_text.encode("utf-8"),
        media_type="text/vtt; charset=utf-8",
        headers={
            "ETag": f'"{desc.sha256}"',
            "X-Artifact-Id": desc.artifact_id,
            "X-Artifact-Version": str(desc.version),
        },
    )


@router.get("/{content_item_id}/metadata")
def get_preview_metadata(
    content_item_id: str,
    service: MetadataServiceDep,
    version: int | None = None,
) -> dict[str, Any]:
    return service.get_preview_metadata(
        content_item_id, video_version=version
    ).as_dict()


@router.get("/{content_item_id}/reports")
def get_preview_reports(
    content_item_id: str,
    service: ReportServiceDep,
) -> dict[str, Any]:
    return service.get_reports(content_item_id).as_dict()


@router.get("/{content_item_id}/compare")
def compare_preview_versions(
    content_item_id: str,
    service: ComparisonServiceDep,
    base_version: int | None = None,
    target_version: int | None = None,
) -> dict[str, Any]:
    return service.compare(
        content_item_id,
        base_version=base_version,
        target_version=target_version,
    ).as_dict()


@router.post("/{content_item_id}/edit-loop")
def run_preview_edit_loop(
    content_item_id: str,
    body: PreviewEditRequest,
    service: EditLoopServiceDep,
    actor: CurrentActor,
) -> dict[str, Any]:
    return service.run_edit_loop(
        content_item_id,
        body.to_changes(),
        actor=actor,
    ).as_dict()
