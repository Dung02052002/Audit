"""HTTP API routes for the unified Script-to-Film / Script-to-Video Engine.

Exposes:
- ``GET /script-to-video/capabilities``: Runtime provider capability inspection.
- ``POST /script-to-video/analyze``: Parse script and build Story Bible + Scene
  Breakdown without rendering.
- ``POST /script-to-video/runs``: Execute full end-to-end Script-to-Video production
  pipeline.
"""

from __future__ import annotations

from http import HTTPStatus
from typing import Annotated, Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict

from ai_youtube_agent.content.story_bible import ProductionMode
from ai_youtube_agent.core.audit import Actor
from ai_youtube_agent.core.content_item import ContentType
from ai_youtube_agent.core.http import current_actor, provide
from ai_youtube_agent.pipeline.script_to_video import (
    ScriptToVideoError,
    ScriptToVideoErrorCode,
    ScriptToVideoRequest,
    ScriptToVideoService,
)

router = APIRouter(prefix="/script-to-video", tags=["script-to-video"])

ScriptToVideoServiceDep = Annotated[ScriptToVideoService, provide(ScriptToVideoService)]
CurrentActor = Annotated[Actor, Depends(current_actor)]


class ScriptToVideoPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    script_text: str | None = None
    script_path: str | None = None
    title: str | None = None
    channel_id: str | None = None
    content_type: str = "shorts"
    production_mode: str = "story_video"
    visual_style: str = "cinematic_story"
    language: str = "vi"
    voice_id: str | None = None
    target_duration_seconds: int | None = None
    subtitles_enabled: bool = True
    request_approval: bool = False
    auto_provision_channel: bool = True
    content_item_id: str | None = None

    def to_domain_request(self) -> ScriptToVideoRequest:
        try:
            ctype = ContentType(self.content_type.strip().lower())
        except ValueError as error:
            raise ScriptToVideoError(
                f"invalid content_type {self.content_type!r}; "
                "expected 'shorts' or 'longform'",
                code=ScriptToVideoErrorCode.INVALID_INPUT,
            ) from error
        try:
            pmode = ProductionMode(self.production_mode.strip().lower())
        except ValueError as error:
            raise ScriptToVideoError(
                f"invalid production_mode {self.production_mode!r}; "
                "expected 'story_video' or 'ai_film'",
                code=ScriptToVideoErrorCode.INVALID_INPUT,
            ) from error

        return ScriptToVideoRequest(
            script_text=self.script_text,
            script_path=self.script_path,
            title=self.title,
            channel_id=self.channel_id,
            content_type=ctype,
            production_mode=pmode,
            visual_style=self.visual_style,
            language=self.language,
            voice_id=self.voice_id,
            target_duration_seconds=self.target_duration_seconds,
            subtitles_enabled=self.subtitles_enabled,
            request_approval=self.request_approval,
            auto_provision_channel=self.auto_provision_channel,
            content_item_id=self.content_item_id,
        )


@router.get("/capabilities")
def get_capabilities(
    service: ScriptToVideoServiceDep,
) -> dict[str, Any]:
    """Return the runtime capabilities of voice, image, video-clip, and render.

    Providers.
    """
    return service.check_capabilities().as_dict()


@router.post("/analyze")
def analyze_script(
    payload: ScriptToVideoPayload,
    service: ScriptToVideoServiceDep,
) -> dict[str, Any]:
    """Parse and analyze a script into a StoryBible and SceneBreakdownPlan."""
    request = payload.to_domain_request()
    story_bible, breakdown = service.analyze_only(request)
    return {
        "story_bible": story_bible.as_dict(),
        "scene_breakdown": breakdown.as_dict(),
    }


@router.post("/runs", status_code=HTTPStatus.CREATED)
def run_script_to_video(
    payload: ScriptToVideoPayload,
    service: ScriptToVideoServiceDep,
    actor: CurrentActor,
) -> dict[str, Any]:
    """Execute the end-to-end Script-to-Video pipeline.

    Returns the project manifest.
    """
    request = payload.to_domain_request()
    manifest = service.run(request, actor=actor)
    return manifest.as_dict()
