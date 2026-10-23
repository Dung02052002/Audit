"""Background runner for the Script-to-Video pipeline (APP-01), context C16.

As the user approved for APP-01:

- One daemon worker thread consumes a FIFO ``queue.Queue`` of runs; it starts
  lazily on the first ``submit``. A run submitted while another one renders is
  ``queued``. Nothing is written to the database when the server starts.
- A run is checked in the caller's thread before it is queued: a blank script
  is ``EMPTY_SCRIPT`` (422), a production mode the providers cannot serve is
  ``MODE_UNAVAILABLE`` (409), and an existing content item may only be
  regenerated while it is draft, generating, testing, preview_ready or failed
  (409 ``domain.content_item_not_editable``). The item rule is checked again
  when the worker takes the run; an item no longer editable fails the run
  with that code before the pipeline is called. The same script, channel,
  content type, mode, style and content item as a queued or running run
  returns that run.
- The registry is in memory only (at most ``history`` runs; the oldest
  finished ones are evicted first) and keeps the final manifest of a run. The
  progress of a run is read from its ``AIJob`` checkpoints on every call.
- ``shutdown`` cancels the queued runs (failed, ``pipeline.run_cancelled``)
  and does not wait for the run in flight. A RUNNING ``pipeline.script_to_video``
  job that no run of this process follows reads as ``interrupted``; running it
  again takes it over through the existing ``take_over``.
- Errors keep only safe fields: the code, the user message, the stage and the
  code of the cause. Any other exception is ``application.internal`` with a
  generic message, and its detail goes to the log only.

BUDGET-01 (D3(a)): a run stopped by the paid-API budget ends
``awaiting_approval`` (with the quote ids to approve) or ``blocked``; neither
is active, and no thread waits for the approval. Its job is FAILED with the
budget code as ``last_error``, which ``job_status`` maps to the same statuses
for a job no run follows. The next stage of such a run is ``waiting`` or
``blocked``. ``retry`` submits the kept request of a stopped run again
(awaiting approval, blocked or failed), with the same checks as ``submit``;
the re-run consumes each approved quote once.
"""

from __future__ import annotations

import hashlib
import itertools
import queue
import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from http import HTTPStatus
from typing import Any, Final

from ai_youtube_agent.content.paid_api_budget import (
    BLOCKING_CODES,
    PaidBudgetErrorCode,
)
from ai_youtube_agent.core.audit import Actor
from ai_youtube_agent.core.content_item import ContentStatus
from ai_youtube_agent.core.db.database import Database
from ai_youtube_agent.core.db.repositories.content import ContentItemRepository
from ai_youtube_agent.core.db.repositories.jobs import AIJobRepository
from ai_youtube_agent.core.errors import DomainError
from ai_youtube_agent.core.log import get_logger
from ai_youtube_agent.pipeline.job import AIJob, AIJobStatus
from ai_youtube_agent.pipeline.script_to_video import (
    SCRIPT_TO_VIDEO_JOB_KIND,
    ScriptToVideoError,
    ScriptToVideoErrorCode,
    ScriptToVideoRequest,
    ScriptToVideoService,
    ScriptToVideoStage,
    VideoProjectManifest,
)

Clock = Callable[[], datetime]
logger = get_logger(__name__)

DEFAULT_HISTORY: Final = 100
LISTED_FINISHED_RUNS: Final = 50

# The stages in the order ``ScriptToVideoService.run`` records them.
PIPELINE_STAGE_ORDER: Final[tuple[ScriptToVideoStage, ...]] = (
    ScriptToVideoStage.SCRIPT_PARSED,
    ScriptToVideoStage.STORY_BIBLE_BUILT,
    ScriptToVideoStage.BUDGET_CHECKED,
    ScriptToVideoStage.VOICE_GENERATED,
    ScriptToVideoStage.SCENES_PLANNED,
    ScriptToVideoStage.ASSETS_READY,
    ScriptToVideoStage.TIMELINE_BUILT,
    ScriptToVideoStage.SUBTITLES_GENERATED,
    ScriptToVideoStage.THUMBNAIL_GENERATED,
    ScriptToVideoStage.AUDIO_MIXED,
    ScriptToVideoStage.VIDEO_RENDERED,
    ScriptToVideoStage.QC_COMPLETED,
    ScriptToVideoStage.PREVIEW_READY,
    ScriptToVideoStage.APPROVAL_REQUESTED,
)
OPTIONAL_STAGES: Final = frozenset(
    {ScriptToVideoStage.SUBTITLES_GENERATED, ScriptToVideoStage.APPROVAL_REQUESTED}
)
# D6: an existing item may be edited and regenerated only in these statuses.
EDITABLE_ITEM_STATUSES: Final = frozenset(
    {
        ContentStatus.DRAFT,
        ContentStatus.GENERATING,
        ContentStatus.TESTING,
        ContentStatus.PREVIEW_READY,
        ContentStatus.FAILED,
    }
)
REDACTED: Final = "[redacted]"
INTERNAL_ERROR_CODE: Final = "application.internal"
INTERNAL_ERROR_MESSAGE: Final = (
    "Something went wrong on our side. Please try again later."
)


class RunStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    INTERRUPTED = "interrupted"
    AWAITING_APPROVAL = "awaiting_approval"
    BLOCKED = "blocked"


ACTIVE_RUN_STATUSES: Final = frozenset({RunStatus.QUEUED, RunStatus.RUNNING})
RETRYABLE_RUN_STATUSES: Final = frozenset(
    {RunStatus.AWAITING_APPROVAL, RunStatus.BLOCKED, RunStatus.FAILED}
)


class StageState(StrEnum):
    DONE = "done"
    ACTIVE = "active"
    PENDING = "pending"
    FAILED = "failed"
    SKIPPED = "skipped"
    WAITING = "waiting"
    BLOCKED = "blocked"


class RunnerErrorCode(StrEnum):
    RUN_NOT_FOUND = "domain.run_not_found"
    JOB_NOT_FOUND = "domain.job_not_found"
    ITEM_NOT_EDITABLE = "domain.content_item_not_editable"
    RUNNER_STOPPED = "pipeline.runner_stopped"
    RUN_CANCELLED = "pipeline.run_cancelled"
    RUN_NOT_RETRYABLE = "domain.run_not_retryable"


USER_MESSAGES: Final[dict[RunnerErrorCode, str]] = {
    RunnerErrorCode.RUN_NOT_FOUND: "This background run does not exist.",
    RunnerErrorCode.JOB_NOT_FOUND: "This job does not exist.",
    RunnerErrorCode.ITEM_NOT_EDITABLE: (
        "This content item can no longer be edited and regenerated."
    ),
    RunnerErrorCode.RUNNER_STOPPED: (
        "The server is stopping and does not accept new runs."
    ),
    RunnerErrorCode.RUN_CANCELLED: (
        "The run was cancelled because the server stopped before it started."
    ),
    RunnerErrorCode.RUN_NOT_RETRYABLE: (
        "Only a run that stopped (awaiting approval, blocked or failed) can be "
        "run again; this run is still active or has succeeded."
    ),
}

_HTTP_STATUS: Final[dict[RunnerErrorCode, HTTPStatus]] = {
    RunnerErrorCode.RUN_NOT_FOUND: HTTPStatus.NOT_FOUND,
    RunnerErrorCode.JOB_NOT_FOUND: HTTPStatus.NOT_FOUND,
    RunnerErrorCode.ITEM_NOT_EDITABLE: HTTPStatus.CONFLICT,
    RunnerErrorCode.RUNNER_STOPPED: HTTPStatus.SERVICE_UNAVAILABLE,
    RunnerErrorCode.RUN_CANCELLED: HTTPStatus.CONFLICT,
    RunnerErrorCode.RUN_NOT_RETRYABLE: HTTPStatus.CONFLICT,
}


class RunnerError(DomainError):
    default_code = RunnerErrorCode.RUN_NOT_FOUND.value
    default_user_message = USER_MESSAGES[RunnerErrorCode.RUN_NOT_FOUND]
    default_http_status = HTTPStatus.NOT_FOUND

    def __init__(self, detail: str = "", *, code: RunnerErrorCode) -> None:
        super().__init__(
            detail,
            code=code.value,
            user_message=USER_MESSAGES[code],
            http_status=_HTTP_STATUS[code],
        )


@dataclass(frozen=True)
class RunRequestSummary:
    """What a run was asked to do, without the script text."""

    title: str | None
    content_type: str
    production_mode: str
    visual_style: str
    content_item_id: str | None
    script_chars: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "content_type": self.content_type,
            "production_mode": self.production_mode,
            "visual_style": self.visual_style,
            "content_item_id": self.content_item_id,
            "script_chars": self.script_chars,
        }


@dataclass(frozen=True)
class RunError:
    """The safe part of the error that ended a run."""

    code: str
    message: str
    stage: str | None
    cause_code: str | None
    retryable: bool
    # BUDGET-01: the quotes a budget stop waits on and its status.
    quote_ids: tuple[str, ...] = ()
    budget_status: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "stage": self.stage,
            "cause_code": self.cause_code,
            "retryable": self.retryable,
            "quote_ids": list(self.quote_ids),
            "budget_status": self.budget_status,
        }


@dataclass(frozen=True)
class RunResult:
    video_artifact_id: str
    video_size_bytes: int
    video_sha256: str
    item_status: str
    qc_status: str
    scene_count: int
    audio_duration_ms: int
    approval_id: str | None

    @classmethod
    def from_manifest(cls, manifest: VideoProjectManifest) -> RunResult:
        return cls(
            video_artifact_id=manifest.video_artifact_id,
            video_size_bytes=manifest.video_size_bytes,
            video_sha256=manifest.video_sha256,
            item_status=manifest.status.value,
            qc_status=manifest.qc_report.status.value,
            scene_count=len(manifest.scene_breakdown.scenes),
            audio_duration_ms=manifest.audio_duration_ms,
            approval_id=manifest.approval_id,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "video_artifact_id": self.video_artifact_id,
            "video_size_bytes": self.video_size_bytes,
            "video_sha256": self.video_sha256,
            "item_status": self.item_status,
            "qc_status": self.qc_status,
            "scene_count": self.scene_count,
            "audio_duration_ms": self.audio_duration_ms,
            "approval_id": self.approval_id,
        }


@dataclass(frozen=True)
class RunSnapshot:
    """An immutable view of one run at one moment."""

    run_id: str
    status: RunStatus
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    request: RunRequestSummary
    job_id: str | None
    content_item_id: str | None
    error: RunError | None
    result: RunResult | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "status": self.status.value,
            "created_at": self.created_at.isoformat(),
            "started_at": _iso(self.started_at),
            "finished_at": _iso(self.finished_at),
            "request": self.request.as_dict(),
            "job_id": self.job_id,
            "content_item_id": self.content_item_id,
            "error": self.error.as_dict() if self.error is not None else None,
            "result": self.result.as_dict() if self.result is not None else None,
        }


@dataclass(frozen=True)
class StageProgress:
    id: str
    state: StageState
    completed_at: datetime | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "state": self.state.value,
            "completed_at": _iso(self.completed_at),
        }


@dataclass(frozen=True)
class RunProgress:
    stages: tuple[StageProgress, ...]
    current_stage: str | None
    completed: int
    total: int
    reused: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "stages": [stage.as_dict() for stage in self.stages],
            "current_stage": self.current_stage,
            "completed": self.completed,
            "total": self.total,
            "reused": self.reused,
        }


def job_progress(
    job: AIJob | None,
    status: RunStatus,
    *,
    since: datetime | None = None,
    disabled_stages: frozenset[ScriptToVideoStage] | None = None,
) -> RunProgress:
    """The stage progress of the latest attempt of a script-to-video job.

    The attempt starts at the last ``script.parsed`` checkpoint (at or after
    ``since`` when given). A succeeded job that is entered again records no
    new checkpoints, so its previous attempt is shown with ``reused`` true.
    ``status`` decides the next stage to run after the last done one: active
    while running, failed after a failure (unless there is no job yet),
    waiting while awaiting a paid-API approval and blocked after a budget
    block (BUDGET-01); optional stages a succeeded run did not reach are
    skipped.

    ``disabled_stages`` are the optional stages the request turned off: they
    are skipped from the start and never chosen as the next stage. Without
    it (the request is not known) no optional stage is chosen as the next
    stage, since the request may have turned it off.
    """
    reached: dict[str, datetime] = {}
    reused = False
    if job is None and status is RunStatus.FAILED:
        # The run failed before it had a job: no stage of it ran.
        status = RunStatus.QUEUED
    if job is not None:
        starts = [
            index
            for index, saved in enumerate(job.checkpoints)
            if saved.step == ScriptToVideoStage.SCRIPT_PARSED.value
        ]
        current = [
            index
            for index in starts
            if since is None or job.checkpoints[index].created_at >= since
        ]
        first: int | None = None
        if current:
            first = current[-1]
        elif starts and job.status is AIJobStatus.SUCCEEDED:
            first = starts[-1]
            reused = True
        if first is not None:
            for saved in job.checkpoints[first:]:
                reached.setdefault(saved.step, saved.created_at)

    disabled = disabled_stages & OPTIONAL_STAGES if disabled_stages else frozenset()
    passed_over = disabled if disabled_stages is not None else OPTIONAL_STAGES
    last_done = max(
        (
            index
            for index, stage in enumerate(PIPELINE_STAGE_ORDER)
            if stage.value in reached
        ),
        default=-1,
    )
    next_index = next(
        (
            index
            for index, stage in enumerate(PIPELINE_STAGE_ORDER)
            if index > last_done
            and stage.value not in reached
            and stage not in passed_over
        ),
        None,
    )
    stages: list[StageProgress] = []
    for index, stage in enumerate(PIPELINE_STAGE_ORDER):
        if stage.value in reached:
            state = StageState.DONE
        elif (
            index < last_done
            or stage in disabled
            or (status is RunStatus.SUCCEEDED and stage in OPTIONAL_STAGES)
        ):
            state = StageState.SKIPPED
        elif index == next_index and status is RunStatus.RUNNING:
            state = StageState.ACTIVE
        elif index == next_index and status is RunStatus.FAILED:
            state = StageState.FAILED
        elif index == next_index and status is RunStatus.AWAITING_APPROVAL:
            state = StageState.WAITING
        elif index == next_index and status is RunStatus.BLOCKED:
            state = StageState.BLOCKED
        else:
            state = StageState.PENDING
        stages.append(StageProgress(stage.value, state, reached.get(stage.value)))

    current_stage = next(
        (
            stage.id
            for stage in stages
            if stage.state
            in (
                StageState.ACTIVE,
                StageState.FAILED,
                StageState.WAITING,
                StageState.BLOCKED,
            )
        ),
        None,
    ) or next((stage.id for stage in stages if stage.state is StageState.PENDING), None)
    return RunProgress(
        stages=tuple(stages),
        current_stage=current_stage,
        completed=sum(1 for stage in stages if stage.state is StageState.DONE),
        total=len(stages),
        reused=reused,
    )


def disabled_optional_stages(
    request: ScriptToVideoRequest,
) -> frozenset[ScriptToVideoStage]:
    """The optional stages a request turned off."""
    disabled: set[ScriptToVideoStage] = set()
    if not request.subtitles_enabled:
        disabled.add(ScriptToVideoStage.SUBTITLES_GENERATED)
    if not request.request_approval:
        disabled.add(ScriptToVideoStage.APPROVAL_REQUESTED)
    return frozenset(disabled)


def job_as_dict(job: AIJob) -> dict[str, Any]:
    """A job with its checkpoints, without the idempotency key.

    Checkpoint data may hold a lease ``token`` (``SqliteJobQueue``); every key
    named ``token`` (in any case, at any depth) is redacted.
    """
    return {
        "id": job.id,
        "kind": job.kind,
        "status": job.status.value,
        "attempts": job.attempts,
        "last_error": job.last_error,
        "created_at": job.created_at.isoformat(),
        "updated_at": job.updated_at.isoformat(),
        "checkpoints": [
            {**saved.as_dict(), "data": _redact_tokens(dict(saved.data))}
            for saved in job.checkpoints
        ],
    }


def _redact_tokens(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: REDACTED
            if isinstance(key, str) and key.casefold() == "token"
            else _redact_tokens(item)
            for key, item in value.items()
        }
    if isinstance(value, list | tuple):
        return [_redact_tokens(item) for item in value]
    return value


@dataclass
class _Run:
    run_id: str
    key: tuple[str, ...]
    request: ScriptToVideoRequest
    actor: Actor
    summary: RunRequestSummary
    created_at: datetime
    # Taken under the runner lock when the run is created and again when it
    # finishes: breaks ties between equal timestamps (Windows clock).
    sequence: int
    status: RunStatus = RunStatus.QUEUED
    started_at: datetime | None = None
    finished_at: datetime | None = None
    job_id: str | None = None
    content_item_id: str | None = None
    error: RunError | None = None
    result: RunResult | None = None
    manifest: VideoProjectManifest | None = None

    def recency(self) -> tuple[datetime, int]:
        return (self.finished_at or self.created_at, self.sequence)

    def snapshot(self) -> RunSnapshot:
        return RunSnapshot(
            run_id=self.run_id,
            status=self.status,
            created_at=self.created_at,
            started_at=self.started_at,
            finished_at=self.finished_at,
            request=self.summary,
            job_id=self.job_id,
            content_item_id=self.content_item_id,
            error=self.error,
            result=self.result,
        )


class ScriptToVideoRunner:
    """Runs ``ScriptToVideoService.run`` in one background worker thread."""

    def __init__(
        self,
        service_provider: Callable[[], ScriptToVideoService],
        database: Database,
        *,
        clock: Clock | None = None,
        start_worker: bool = True,
        history: int = DEFAULT_HISTORY,
    ) -> None:
        if history < 1:
            raise ValueError("history must be 1 or more")
        self._service_provider = service_provider
        self._database = database
        self._clock = clock
        self._start_worker = start_worker
        self._history = history
        self._lock = threading.Lock()
        self._runs: dict[str, _Run] = {}
        self._queue: queue.Queue[str | None] = queue.Queue()
        self._worker: threading.Thread | None = None
        self._closed = False
        self._sequence = itertools.count(1)

    def _now(self) -> datetime:
        return self._clock() if self._clock else datetime.now(UTC)

    # Submitting

    def submit(
        self, request: ScriptToVideoRequest, *, actor: Actor
    ) -> tuple[RunSnapshot, bool]:
        """Check and queue a run; ``False`` when an equal active run is returned."""
        script_text = request.script_text
        if script_text is None or not script_text.strip():
            raise ScriptToVideoError(
                "script_text is blank",
                code=ScriptToVideoErrorCode.EMPTY_SCRIPT,
                stage=ScriptToVideoStage.SCRIPT_PARSED,
            )
        capabilities = self._service_provider().check_capabilities()
        # A mode stopped only by the budget policy is queued: the run then ends
        # blocked with the budget code on its job (BUDGET-01).
        if not capabilities.accepts_mode(request.production_mode):
            raise ScriptToVideoError(
                capabilities.unavailable_reasons.get(
                    request.production_mode.value, "production mode unavailable"
                ),
                code=ScriptToVideoErrorCode.MODE_UNAVAILABLE,
                stage=ScriptToVideoStage.STORY_BIBLE_BUILT,
            )
        if request.content_item_id is not None:
            self._require_editable(request.content_item_id)

        key = _run_key(request, script_text)
        with self._lock:
            if self._closed:
                raise RunnerError(
                    "the runner is shut down", code=RunnerErrorCode.RUNNER_STOPPED
                )
            for run in self._runs.values():
                if run.key == key and run.status in ACTIVE_RUN_STATUSES:
                    return run.snapshot(), False
            run = _Run(
                run_id="run_" + uuid.uuid4().hex,
                key=key,
                request=request,
                actor=actor,
                summary=RunRequestSummary(
                    title=request.title,
                    content_type=request.content_type.value,
                    production_mode=request.production_mode.value,
                    visual_style=request.visual_style,
                    content_item_id=request.content_item_id,
                    script_chars=len(script_text),
                ),
                created_at=self._now(),
                sequence=next(self._sequence),
                content_item_id=request.content_item_id,
            )
            self._runs[run.run_id] = run
            self._evict()
            self._queue.put(run.run_id)
            if self._start_worker and self._worker is None:
                self._worker = threading.Thread(
                    target=self._work, name="script-to-video-runner", daemon=True
                )
                self._worker.start()
            return run.snapshot(), True

    def retry(self, run_id: str, *, actor: Actor) -> tuple[RunSnapshot, bool]:
        """Submit the kept request of a stopped run again (BUDGET-01).

        Only an awaiting-approval, blocked or failed run can be run again; its
        content item must still be editable (D6). Like ``submit``, an equal
        active run is returned with ``False``.
        """
        with self._lock:
            run = self._runs.get(run_id)
            if run is None:
                raise RunnerError(
                    f"run {run_id} not found", code=RunnerErrorCode.RUN_NOT_FOUND
                )
            status = run.status
            request = run.request
            content_item_id = run.content_item_id
        if status not in RETRYABLE_RUN_STATUSES:
            raise RunnerError(
                f"run {run_id} is {status.value}",
                code=RunnerErrorCode.RUN_NOT_RETRYABLE,
            )
        if content_item_id is not None:
            self._require_editable(content_item_id)
        return self.submit(request, actor=actor)

    def _require_editable(self, content_item_id: str) -> None:
        with self._database.transaction() as connection:
            item = ContentItemRepository(connection).get(content_item_id)
        if item is not None and item.status not in EDITABLE_ITEM_STATUSES:
            raise RunnerError(
                f"content item {content_item_id} is {item.status.value}",
                code=RunnerErrorCode.ITEM_NOT_EDITABLE,
            )

    def shutdown(self) -> None:
        """Cancel the queued runs and stop the worker after the run in flight."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            now = self._now()
            for run in self._runs.values():
                if run.status is RunStatus.QUEUED:
                    run.status = RunStatus.FAILED
                    run.finished_at = now
                    run.sequence = next(self._sequence)
                    run.error = RunError(
                        code=RunnerErrorCode.RUN_CANCELLED.value,
                        message=USER_MESSAGES[RunnerErrorCode.RUN_CANCELLED],
                        stage=None,
                        cause_code=None,
                        retryable=True,
                    )
            self._queue.put(None)

    # Reading

    def get(self, run_id: str) -> RunSnapshot | None:
        with self._lock:
            run = self._runs.get(run_id)
            return run.snapshot() if run is not None else None

    def require(self, run_id: str) -> RunSnapshot:
        snapshot = self.get(run_id)
        if snapshot is None:
            raise RunnerError(
                f"run {run_id} not found", code=RunnerErrorCode.RUN_NOT_FOUND
            )
        return snapshot

    def describe(self, snapshot: RunSnapshot) -> dict[str, Any]:
        """The snapshot with the progress of its job."""
        job = self._load_job(snapshot.job_id) if snapshot.job_id else None
        with self._lock:
            run = self._runs.get(snapshot.run_id)
            disabled = (
                disabled_optional_stages(run.request) if run is not None else None
            )
        progress = job_progress(
            job, snapshot.status, since=snapshot.started_at, disabled_stages=disabled
        )
        return {**snapshot.as_dict(), "progress": progress.as_dict()}

    def list_runs(self) -> list[RunSnapshot]:
        """Active runs oldest first, then the last finished runs newest first."""
        with self._lock:
            runs = list(self._runs.values())
            active = [
                run.snapshot() for run in runs if run.status in ACTIVE_RUN_STATUSES
            ]
            finished = sorted(
                (run for run in runs if run.status not in ACTIVE_RUN_STATUSES),
                key=_Run.recency,
                reverse=True,
            )
            return active + [run.snapshot() for run in finished[:LISTED_FINISHED_RUNS]]

    def is_tracked(self, job_id: str) -> bool:
        """Whether a running run of this process follows the job."""
        with self._lock:
            return any(
                run.job_id == job_id and run.status is RunStatus.RUNNING
                for run in self._runs.values()
            )

    def job_status(self, job: AIJob) -> RunStatus:
        """The status of a job as the app shows it."""
        if job.status is AIJobStatus.RUNNING:
            return (
                RunStatus.RUNNING if self.is_tracked(job.id) else RunStatus.INTERRUPTED
            )
        if job.status is AIJobStatus.SUCCEEDED:
            return RunStatus.SUCCEEDED
        if job.status is AIJobStatus.FAILED:
            return budget_stop_status(job.last_error) or RunStatus.FAILED
        if job.status is AIJobStatus.CANCELLED:
            return RunStatus.FAILED
        return RunStatus.QUEUED

    def job_view(self, job_id: str) -> dict[str, Any]:
        job = self._load_job(job_id)
        if job is None:
            raise RunnerError(
                f"job {job_id} not found", code=RunnerErrorCode.JOB_NOT_FOUND
            )
        status = self.job_status(job)
        progress = (
            job_progress(job, status).as_dict()
            if job.kind == SCRIPT_TO_VIDEO_JOB_KIND
            else None
        )
        return {
            "job": job_as_dict(job),
            "progress": progress,
            "tracked": self.is_tracked(job.id),
            "status": status.value,
        }

    def active_run_id(self, content_item_id: str) -> str | None:
        with self._lock:
            return next(
                (
                    run.run_id
                    for run in self._runs.values()
                    if run.content_item_id == content_item_id
                    and run.status in ACTIVE_RUN_STATUSES
                ),
                None,
            )

    def latest_manifest(self, content_item_id: str) -> VideoProjectManifest | None:
        """The manifest of the newest succeeded run of the item, if kept."""
        with self._lock:
            done = [
                run
                for run in self._runs.values()
                if run.manifest is not None
                and run.manifest.content_item_id == content_item_id
            ]
            if not done:
                return None
            newest = max(done, key=_Run.recency)
            return newest.manifest

    def _load_job(self, job_id: str) -> AIJob | None:
        with self._database.transaction() as connection:
            return AIJobRepository(connection).get(job_id)

    # Worker

    def _work(self) -> None:
        while True:
            run_id = self._queue.get()
            if run_id is None:
                return
            with self._lock:
                run = self._runs.get(run_id)
                if run is None or run.status is not RunStatus.QUEUED:
                    continue
                run.status = RunStatus.RUNNING
                run.started_at = self._now()
            self._execute(run)

    def _execute(self, run: _Run) -> None:
        if run.request.content_item_id is not None:
            # D6 again: the item may have left the editable statuses while
            # the run waited in the queue.
            try:
                self._require_editable(run.request.content_item_id)
            except RunnerError as error:
                logger.info(
                    "background script-to-video run refused",
                    extra={"fields": {"run_id": run.run_id, **error.log_fields()}},
                )
                self._finish(
                    run.run_id,
                    RunStatus.FAILED,
                    error=RunError(
                        code=error.code,
                        message=error.user_message,
                        stage=None,
                        cause_code=None,
                        retryable=False,
                    ),
                )
                return
        try:
            service = self._service_provider()
            manifest = service.run(
                run.request,
                actor=run.actor,
                on_job_started=lambda job_id, item_id: self._job_started(
                    run.run_id, job_id, item_id
                ),
            )
        except ScriptToVideoError as error:
            logger.info(
                "background script-to-video run failed",
                extra={"fields": {"run_id": run.run_id, **error.log_fields()}},
            )
            cause_code = getattr(error.__cause__, "code", None)
            budget_status = getattr(error, "budget_status", None)
            self._finish(
                run.run_id,
                _RUN_STATUS_BY_CODE.get(error.code, RunStatus.FAILED),
                error=RunError(
                    code=error.code,
                    message=error.user_message,
                    stage=error.stage.value if error.stage is not None else None,
                    cause_code=str(cause_code) if cause_code is not None else None,
                    retryable=False,
                    quote_ids=tuple(getattr(error, "quote_ids", ())),
                    budget_status=(
                        budget_status.value if budget_status is not None else None
                    ),
                ),
            )
        except Exception:
            logger.exception(
                "background script-to-video run crashed",
                extra={"fields": {"run_id": run.run_id}},
            )
            self._finish(
                run.run_id,
                RunStatus.FAILED,
                error=RunError(
                    code=INTERNAL_ERROR_CODE,
                    message=INTERNAL_ERROR_MESSAGE,
                    stage=None,
                    cause_code=None,
                    retryable=False,
                ),
            )
        else:
            self._finish(run.run_id, RunStatus.SUCCEEDED, manifest=manifest)

    def _job_started(self, run_id: str, job_id: str, content_item_id: str) -> None:
        with self._lock:
            run = self._runs.get(run_id)
            if run is not None:
                run.job_id = job_id
                run.content_item_id = content_item_id

    def _finish(
        self,
        run_id: str,
        status: RunStatus,
        *,
        error: RunError | None = None,
        manifest: VideoProjectManifest | None = None,
    ) -> None:
        with self._lock:
            run = self._runs.get(run_id)
            if run is None:
                return
            run.status = status
            run.finished_at = self._now()
            run.sequence = next(self._sequence)
            run.error = error
            if manifest is not None:
                run.manifest = manifest
                run.result = RunResult.from_manifest(manifest)
                run.job_id = manifest.job_id
                run.content_item_id = manifest.content_item_id
            self._evict()

    def _evict(self) -> None:
        """Drop the oldest finished runs above ``history`` (lock held)."""
        while len(self._runs) > self._history:
            finished = [
                run
                for run in self._runs.values()
                if run.status not in ACTIVE_RUN_STATUSES
            ]
            if not finished:
                return
            oldest = min(finished, key=_Run.recency)
            del self._runs[oldest.run_id]


_RUN_STATUS_BY_CODE: Final[dict[str, RunStatus]] = {
    ScriptToVideoErrorCode.AWAITING_PAID_APPROVAL.value: RunStatus.AWAITING_APPROVAL,
    ScriptToVideoErrorCode.BUDGET_BLOCKED.value: RunStatus.BLOCKED,
}


def budget_stop_status(last_error: str | None) -> RunStatus | None:
    """The run status of a job that failed with a budget code, else ``None``."""
    if last_error is None:
        return None
    if last_error.startswith(PaidBudgetErrorCode.AWAITING_APPROVAL.value):
        return RunStatus.AWAITING_APPROVAL
    if any(last_error.startswith(code.value) for code in BLOCKING_CODES):
        return RunStatus.BLOCKED
    return None


def _run_key(request: ScriptToVideoRequest, script_text: str) -> tuple[str, ...]:
    return (
        hashlib.sha256(script_text.encode("utf-8")).hexdigest(),
        request.channel_id or "",
        request.content_type.value,
        request.production_mode.value,
        request.visual_style,
        request.content_item_id or "",
    )


def _iso(moment: datetime | None) -> str | None:
    return moment.isoformat() if moment is not None else None


__all__ = [
    "EDITABLE_ITEM_STATUSES",
    "PIPELINE_STAGE_ORDER",
    "RETRYABLE_RUN_STATUSES",
    "RunError",
    "RunProgress",
    "RunResult",
    "RunSnapshot",
    "RunStatus",
    "RunnerError",
    "RunnerErrorCode",
    "ScriptToVideoRunner",
    "StageState",
    "budget_stop_status",
    "disabled_optional_stages",
    "job_as_dict",
    "job_progress",
]
