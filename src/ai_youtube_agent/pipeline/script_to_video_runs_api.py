"""HTTP API of the background Script-to-Video runner (APP-01).

Exposes:
- ``POST /script-to-video/background-runs``: check and queue a run (202 with a
  ``Location`` header), or return the equal run already queued or running
  (200). Only ``script_text`` is accepted; ``script_path`` is refused (422).
- ``GET /script-to-video/background-runs``: active runs and the last finished.
- ``GET /script-to-video/background-runs/{run_id}``: one run with the stage
  progress of its job (404 ``domain.run_not_found``).
- ``GET /script-to-video/jobs/{job_id}``: one job with its checkpoints; a
  running job that no run of this process follows is ``interrupted``.
- ``POST /script-to-video/background-runs/{run_id}/retry`` (BUDGET-01): queue
  the kept request of a stopped run (awaiting approval, blocked or failed)
  again, 202 with a ``Location`` header (200 when an equal run is already
  active); 404 ``domain.run_not_found``, 409 ``domain.run_not_retryable`` for
  an active or succeeded run, 409 ``domain.content_item_not_editable``.

The synchronous ``POST /script-to-video/runs`` is unchanged.
"""

from __future__ import annotations

from http import HTTPStatus
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Response
from pydantic import BaseModel, ConfigDict

from ai_youtube_agent.content.script_to_video_api import ScriptToVideoPayload
from ai_youtube_agent.core.audit import Actor
from ai_youtube_agent.core.http import current_actor, provide
from ai_youtube_agent.pipeline.script_to_video import ScriptToVideoRequest
from ai_youtube_agent.pipeline.script_to_video_runner import ScriptToVideoRunner

router = APIRouter(prefix="/script-to-video", tags=["script-to-video"])

RunnerDep = Annotated[ScriptToVideoRunner, provide(ScriptToVideoRunner)]
CurrentActor = Annotated[Actor, Depends(current_actor)]


class BackgroundRunPayload(BaseModel):
    """``ScriptToVideoPayload`` without ``script_path`` (D7: text only)."""

    model_config = ConfigDict(extra="forbid")

    script_text: str | None = None
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
        return ScriptToVideoPayload(**self.model_dump()).to_domain_request()


def _run_location(run_id: str) -> str:
    return f"/script-to-video/background-runs/{run_id}"


@router.post("/background-runs", status_code=HTTPStatus.ACCEPTED)
def submit_background_run(
    payload: BackgroundRunPayload,
    runner: RunnerDep,
    actor: CurrentActor,
    response: Response,
) -> dict[str, Any]:
    """Queue a run; an equal active run is returned with 200 instead."""
    snapshot, created = runner.submit(payload.to_domain_request(), actor=actor)
    response.headers["Location"] = _run_location(snapshot.run_id)
    if not created:
        response.status_code = HTTPStatus.OK
    return runner.describe(snapshot)


@router.get("/background-runs")
def list_background_runs(runner: RunnerDep) -> dict[str, Any]:
    return {"runs": [runner.describe(snapshot) for snapshot in runner.list_runs()]}


@router.get("/background-runs/{run_id}")
def get_background_run(run_id: str, runner: RunnerDep) -> dict[str, Any]:
    return runner.describe(runner.require(run_id))


@router.post("/background-runs/{run_id}/retry", status_code=HTTPStatus.ACCEPTED)
def retry_background_run(
    run_id: str,
    runner: RunnerDep,
    actor: CurrentActor,
    response: Response,
) -> dict[str, Any]:
    """Run a stopped run again; it consumes each approved quote once."""
    snapshot, created = runner.retry(run_id, actor=actor)
    response.headers["Location"] = _run_location(snapshot.run_id)
    if not created:
        response.status_code = HTTPStatus.OK
    return runner.describe(snapshot)


@router.get("/jobs/{job_id}")
def get_script_to_video_job(job_id: str, runner: RunnerDep) -> dict[str, Any]:
    return runner.job_view(job_id)
