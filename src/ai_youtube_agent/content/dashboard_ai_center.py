"""AI Center screen view and service (Prompt Pack v8, prompt Q-199), context X3 Command Center.

Builds the Command Center AI Center screen view model and iOS-style HTML rendering,
presenting:
- Active and recent ``Session`` states (user-initiated sessions, started_by,
  started_at, stopped_at, status).
- ``AIJob`` records grouped into active (``running``, ``queued``, ``waiting``),
  failed (``failed`` with ``last_error`` and ``attempts`` / ``retry_count``), and
  completed (``succeeded``, ``cancelled``), including checkpoint progress
  (``checkpoint_count``, ``last_checkpoint_step``, ``last_checkpoint_sequence``).
- Configured AI/render providers (``research_provider``, ``text_provider``,
  ``voice_provider``, ``render_provider``) and tracked AI/provider costs by
  category and by provider from ``CostLedger``.
"""

import html
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, Final

from ai_youtube_agent.content.cost_ledger import CostLedger
from ai_youtube_agent.content.dashboard_shell import (
    BadgeTone,
    DashboardScreenId,
    DashboardShellService,
    DashboardShellState,
    ViewLoadState,
    _select_channel,
    render_dashboard_shell_html,
)
from ai_youtube_agent.core.audit import AuditLog
from ai_youtube_agent.core.config import Settings
from ai_youtube_agent.core.db.database import Database
from ai_youtube_agent.core.db.repositories.channel import ChannelRepository
from ai_youtube_agent.core.db.repositories.content import ContentItemRepository
from ai_youtube_agent.core.db.repositories.jobs import (
    AIJobRepository,
    SessionRepository,
)
from ai_youtube_agent.pipeline.job import AIJob, AIJobStatus, Session, SessionStatus

Clock = Callable[[], datetime]

_JOB_TONES: Final[dict[AIJobStatus, BadgeTone]] = {
    AIJobStatus.RUNNING: BadgeTone.INFO,
    AIJobStatus.QUEUED: BadgeTone.NEUTRAL,
    AIJobStatus.WAITING: BadgeTone.WARNING,
    AIJobStatus.SUCCEEDED: BadgeTone.SUCCESS,
    AIJobStatus.FAILED: BadgeTone.DANGER,
    AIJobStatus.CANCELLED: BadgeTone.NEUTRAL,
}


@dataclass(frozen=True)
class SessionStateView:
    session_id: str
    status: str
    tone: BadgeTone
    started_by_kind: str
    started_by_id: str
    started_at: datetime
    stopped_at: datetime | None
    job_count: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "status": self.status,
            "tone": self.tone.value,
            "started_by_kind": self.started_by_kind,
            "started_by_id": self.started_by_id,
            "started_at": self.started_at.isoformat(),
            "stopped_at": self.stopped_at.isoformat() if self.stopped_at else None,
            "job_count": self.job_count,
        }


@dataclass(frozen=True)
class AIJobViewItem:
    job_id: str
    kind: str
    idempotency_key: str
    content_item_id: str | None
    session_id: str | None
    status: str
    tone: BadgeTone
    attempts: int
    retry_count: int
    last_error: str | None
    checkpoint_count: int
    last_checkpoint_step: str | None
    last_checkpoint_sequence: int | None
    checkpoints: tuple[dict[str, Any], ...]
    provider_name: str
    created_at: datetime
    updated_at: datetime

    def as_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "kind": self.kind,
            "idempotency_key": self.idempotency_key,
            "content_item_id": self.content_item_id,
            "session_id": self.session_id,
            "status": self.status,
            "tone": self.tone.value,
            "attempts": self.attempts,
            "retry_count": self.retry_count,
            "last_error": self.last_error,
            "checkpoint_count": self.checkpoint_count,
            "last_checkpoint_step": self.last_checkpoint_step,
            "last_checkpoint_sequence": self.last_checkpoint_sequence,
            "checkpoints": list(self.checkpoints),
            "provider_name": self.provider_name,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }


@dataclass(frozen=True)
class ProviderCostSummary:
    research_provider: str
    text_provider: str
    voice_provider: str
    render_provider: str
    currency: str
    total_cost: Decimal
    by_category: dict[str, Decimal]
    by_provider: dict[str, Decimal]

    def as_dict(self) -> dict[str, Any]:
        return {
            "research_provider": self.research_provider,
            "text_provider": self.text_provider,
            "voice_provider": self.voice_provider,
            "render_provider": self.render_provider,
            "currency": self.currency,
            "total_cost": str(self.total_cost),
            "by_category": {k: str(v) for k, v in self.by_category.items()},
            "by_provider": {k: str(v) for k, v in self.by_provider.items()},
        }


@dataclass(frozen=True)
class AICenterScreenView:
    shell: DashboardShellState
    load_state: ViewLoadState
    channel_id: str | None
    active_session: SessionStateView | None
    recent_sessions: tuple[SessionStateView, ...]
    active_jobs: tuple[AIJobViewItem, ...]
    failed_jobs: tuple[AIJobViewItem, ...]
    completed_jobs: tuple[AIJobViewItem, ...]
    all_jobs: tuple[AIJobViewItem, ...]
    provider_and_costs: ProviderCostSummary
    empty_message: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "shell": self.shell.as_dict(),
            "load_state": self.load_state.value,
            "channel_id": self.channel_id,
            "active_session": (
                self.active_session.as_dict() if self.active_session else None
            ),
            "recent_sessions": [s.as_dict() for s in self.recent_sessions],
            "active_jobs": [j.as_dict() for j in self.active_jobs],
            "failed_jobs": [j.as_dict() for j in self.failed_jobs],
            "completed_jobs": [j.as_dict() for j in self.completed_jobs],
            "all_jobs": [j.as_dict() for j in self.all_jobs],
            "provider_and_costs": self.provider_and_costs.as_dict(),
            "empty_message": self.empty_message,
        }


class AICenterScreenService:
    """Builds the AI Center screen view model and renders its HTML."""

    def __init__(
        self,
        database: Database,
        *,
        settings: Settings | None = None,
        cost_ledger: CostLedger | None = None,
        shell_service: DashboardShellService | None = None,
        clock: Clock | None = None,
    ) -> None:
        self._database = database
        self._settings = settings or Settings()
        self._cost_ledger = cost_ledger or CostLedger(
            database, AuditLog(database), clock=clock
        )
        self._clock = clock
        self._shell_service = shell_service or DashboardShellService(
            database, clock=clock
        )

    def load(self, channel_id: str | None = None) -> AICenterScreenView:
        shell = self._shell_service.build_shell(
            DashboardScreenId.AI_CENTER,
            channel_id=channel_id,
        )

        connection = self._database.connect()
        try:
            channels = ChannelRepository(connection).list_all()
            selected_channel = _select_channel(channels, channel_id)
            sessions = SessionRepository(connection).list_all()
            all_jobs_raw = AIJobRepository(connection).list_all()
            if selected_channel is not None:
                channel_items = {
                    item.id
                    for item in ContentItemRepository(connection).list_by_channel(
                        selected_channel.id
                    )
                }
                jobs_raw = [
                    job
                    for job in all_jobs_raw
                    if job.content_item_id is None
                    or job.content_item_id in channel_items
                ]
            else:
                jobs_raw = all_jobs_raw
        finally:
            connection.close()

        jobs_by_session: dict[str, int] = {}
        for job in all_jobs_raw:
            if job.session_id is not None:
                jobs_by_session[job.session_id] = (
                    jobs_by_session.get(job.session_id, 0) + 1
                )

        session_views = tuple(
            _to_session_view(s, jobs_by_session.get(s.id, 0))
            for s in reversed(sessions)
        )
        active_session = next(
            (s for s in session_views if s.status == SessionStatus.ACTIVE.value),
            None,
        )

        job_items = tuple(
            _to_job_view(job, self._settings)
            for job in sorted(
                jobs_raw, key=lambda j: (j.updated_at, j.id), reverse=True
            )
        )
        active_jobs = tuple(
            j
            for j in job_items
            if j.status
            in (
                AIJobStatus.RUNNING.value,
                AIJobStatus.QUEUED.value,
                AIJobStatus.WAITING.value,
            )
        )
        failed_jobs = tuple(
            j for j in job_items if j.status == AIJobStatus.FAILED.value
        )
        completed_jobs = tuple(
            j
            for j in job_items
            if j.status
            in (AIJobStatus.SUCCEEDED.value, AIJobStatus.CANCELLED.value)
        )

        if selected_channel is not None:
            cost_summary = self._cost_ledger.summarize(selected_channel.id)
            currency = cost_summary.currency or "USD"
            total_cost = cost_summary.total_amount
            by_cat = cost_summary.by_category
            by_prov = cost_summary.by_provider
        else:
            currency = "USD"
            total_cost = Decimal("0")
            by_cat = {}
            by_prov = {}

        provider_summary = ProviderCostSummary(
            research_provider=self._settings.research_provider.value,
            text_provider=self._settings.text_provider.value,
            voice_provider=self._settings.voice_provider.value,
            render_provider=self._settings.render_provider.value,
            currency=currency,
            total_cost=total_cost,
            by_category=by_cat,
            by_provider=by_prov,
        )

        has_activity = bool(session_views or job_items or total_cost > 0)

        return AICenterScreenView(
            shell=shell,
            load_state=ViewLoadState.READY if has_activity else ViewLoadState.EMPTY,
            channel_id=selected_channel.id if selected_channel else None,
            active_session=active_session,
            recent_sessions=session_views,
            active_jobs=active_jobs,
            failed_jobs=failed_jobs,
            completed_jobs=completed_jobs,
            all_jobs=job_items,
            provider_and_costs=provider_summary,
            empty_message=(
                "No AI sessions or jobs recorded yet."
                if not has_activity
                else None
            ),
        )

    def render_html(self, view: AICenterScreenView) -> str:
        sess = view.active_session
        if sess is not None:
            session_html = (
                f'<p>Active Session: <code>{html.escape(sess.session_id)}</code> '
                f"by <strong>{html.escape(sess.started_by_id)}</strong> "
                f"({sess.job_count} jobs)</p>"
            )
            sess_badge = '<span class="yt-pill yt-pill--success">active</span>'
        else:
            session_html = '<p class="yt-card__subtitle">No active user session.</p>'
            sess_badge = '<span class="yt-pill yt-pill--neutral">stopped</span>'

        pc = view.provider_and_costs
        job_rows = "".join(
            f"<tr><td><code>{html.escape(j.job_id[:8])}</code></td>"
            f"<td>{html.escape(j.kind)}</td>"
            f'<td><span class="yt-pill yt-pill--{j.tone.value}">{html.escape(j.status)}</span></td>'
            f"<td>{html.escape(j.last_checkpoint_step or '—')} ({j.checkpoint_count})</td>"
            f"<td>{j.attempts} (retries: {j.retry_count})</td>"
            f"<td>{html.escape(j.provider_name)}</td>"
            f"<td>{html.escape(j.last_error or '—')}</td></tr>"
            for j in view.all_jobs
        )
        jobs_table = (
            '<table class="yt-table" data-testid="ai-center-jobs-table">'
            "<thead><tr><th>Job</th><th>Kind</th><th>Status</th>"
            "<th>Progress / Checkpoint</th><th>Attempts</th><th>Provider</th><th>Error</th></tr></thead>"
            f"<tbody>{job_rows}</tbody></table>"
            if job_rows
            else '<p class="yt-empty">No AI jobs recorded.</p>'
        )

        body = f"""<div class="yt-grid" data-testid="ai-center-screen">
  <section class="yt-card yt-grid__span-6" data-testid="ai-center-session-card">
    <div class="yt-card__header">
      <div>
        <p class="yt-card__subtitle">Session Control</p>
        <h2 class="yt-card__title">Session State</h2>
      </div>
      {sess_badge}
    </div>
    {session_html}
    <p class="yt-card__subtitle">Active jobs: {len(view.active_jobs)} · Failed: {len(view.failed_jobs)} · Completed: {len(view.completed_jobs)}</p>
  </section>

  <section class="yt-card yt-grid__span-6" data-testid="ai-center-providers-card">
    <div class="yt-card__header">
      <div>
        <p class="yt-card__subtitle">Providers &amp; AI Cost</p>
        <h2 class="yt-card__title">Provider Telemetry</h2>
      </div>
      <span class="yt-pill yt-pill--info">{html.escape(str(pc.total_cost))} {html.escape(pc.currency)}</span>
    </div>
    <p class="yt-card__subtitle">Research: <code>{html.escape(pc.research_provider)}</code> · LLM: <code>{html.escape(pc.text_provider)}</code> · TTS: <code>{html.escape(pc.voice_provider)}</code> · Render: <code>{html.escape(pc.render_provider)}</code></p>
  </section>

  <section class="yt-card yt-grid__span-12" data-testid="ai-center-jobs-card">
    <div class="yt-card__header">
      <div>
        <p class="yt-card__subtitle">Job Execution &amp; Checkpoints</p>
        <h2 class="yt-card__title">AI Jobs · Progress · Retries · Errors</h2>
      </div>
      <span class="yt-pill yt-pill--neutral">{len(view.all_jobs)} jobs</span>
    </div>
    {jobs_table}
  </section>
</div>"""
        return render_dashboard_shell_html(
            view.shell,
            body_html=body,
            page_title="AI Center — YouTube Command Center",
        )


def _to_session_view(session: Session, job_count: int) -> SessionStateView:
    tone = (
        BadgeTone.SUCCESS
        if session.status is SessionStatus.ACTIVE
        else BadgeTone.NEUTRAL
    )
    return SessionStateView(
        session_id=session.id,
        status=session.status.value,
        tone=tone,
        started_by_kind=session.started_by.kind.value,
        started_by_id=session.started_by.id,
        started_at=session.started_at,
        stopped_at=session.stopped_at,
        job_count=job_count,
    )


def _resolve_provider_for_kind(kind: str, settings: Settings) -> str:
    prefix = kind.split(".", 1)[0].lower()
    if prefix in ("research", "idea", "topic"):
        return settings.research_provider.value
    if prefix in ("voice", "tts", "audio"):
        return settings.voice_provider.value
    if prefix in ("render", "video"):
        return settings.render_provider.value
    return settings.text_provider.value


def _to_job_view(job: AIJob, settings: Settings) -> AIJobViewItem:
    last_cp = job.last_checkpoint
    return AIJobViewItem(
        job_id=job.id,
        kind=job.kind,
        idempotency_key=job.idempotency_key,
        content_item_id=job.content_item_id,
        session_id=job.session_id,
        status=job.status.value,
        tone=_JOB_TONES[job.status],
        attempts=job.attempts,
        retry_count=max(0, job.attempts - 1),
        last_error=job.last_error,
        checkpoint_count=len(job.checkpoints),
        last_checkpoint_step=last_cp.step if last_cp else None,
        last_checkpoint_sequence=last_cp.sequence if last_cp else None,
        checkpoints=tuple(cp.as_dict() for cp in job.checkpoints),
        provider_name=_resolve_provider_for_kind(job.kind, settings),
        created_at=job.created_at,
        updated_at=job.updated_at,
    )

