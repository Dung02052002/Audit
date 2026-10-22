"""Tests for AI Center screen view and service (Prompt Pack v8, prompt Q-199)."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from ai_youtube_agent.content.channel import ChannelStatus
from ai_youtube_agent.content.cost import CostCategory, CostRecord
from ai_youtube_agent.content.dashboard_ai_center import AICenterScreenService
from ai_youtube_agent.content.dashboard_shell import DashboardScreenId, ViewLoadState
from ai_youtube_agent.core.audit import Actor, ActorKind
from ai_youtube_agent.core.content_item import ContentType
from ai_youtube_agent.core.db.database import Database
from ai_youtube_agent.core.db.repositories.channel import (
    ChannelRepository,
    StrategyProfileRepository,
)
from ai_youtube_agent.core.db.repositories.content import ContentItemRepository
from ai_youtube_agent.core.db.repositories.economics import CostRecordRepository
from ai_youtube_agent.core.db.repositories.jobs import (
    AIJobRepository,
    SessionRepository,
)
from ai_youtube_agent.pipeline.job import AIJob, Session
from factories import make_channel, make_content_item, make_strategy_profile

FIXED_NOW = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)


def test_ai_center_empty_when_no_sessions_or_jobs(database: Database) -> None:
    service = AICenterScreenService(database, clock=lambda: FIXED_NOW)
    view = service.load()

    assert view.load_state is ViewLoadState.EMPTY
    assert view.active_session is None
    assert len(view.all_jobs) == 0


def test_ai_center_shows_active_jobs_progress_errors_retries_provider_cost_and_session(
    database: Database,
) -> None:
    channel = make_channel(title="AI Ops Studio", status=ChannelStatus.ACTIVE)
    profile = make_strategy_profile(channel)
    item = make_content_item(
        channel,
        profile,
        content_type=ContentType.SHORTS,
        title="Agentic Workflow Demo",
    )
    session = Session.start(
        actor=Actor(ActorKind.USER, "operator-1"),
        clock=lambda: FIXED_NOW - timedelta(minutes=15),
    )

    running_job = (
        AIJob.create(
            "render.shorts",
            "idem-render-1",
            content_item_id=item.id,
            session_id=session.id,
            clock=lambda: FIXED_NOW - timedelta(minutes=10),
        )
        .start(clock=lambda: FIXED_NOW - timedelta(minutes=9))
        .checkpoint(
            "render.frames_composited",
            {"percent": 65},
            clock=lambda: FIXED_NOW - timedelta(minutes=5),
        )
    )

    failed_job = (
        AIJob.create(
            "voice.synthesize",
            "idem-tts-1",
            content_item_id=item.id,
            session_id=session.id,
            clock=lambda: FIXED_NOW - timedelta(minutes=14),
        )
        .start(clock=lambda: FIXED_NOW - timedelta(minutes=13))
        .fail("rate limit exceeded", clock=lambda: FIXED_NOW - timedelta(minutes=12))
        .start(clock=lambda: FIXED_NOW - timedelta(minutes=11))
        .fail("upstream timeout", clock=lambda: FIXED_NOW - timedelta(minutes=8))
    )

    cost_rec = CostRecord.create(
        channel.id,
        CostCategory.LLM,
        provider="mock",
        amount=Decimal("1.25"),
        currency="USD",
        content_item_id=item.id,
        clock=lambda: FIXED_NOW - timedelta(minutes=6),
    )

    with database.transaction() as conn:
        ChannelRepository(conn).add(channel)
        StrategyProfileRepository(conn).add(profile)
        ContentItemRepository(conn).add(item)
        SessionRepository(conn).add(session)
        AIJobRepository(conn).add(running_job)
        AIJobRepository(conn).add(failed_job)
        CostRecordRepository(conn).add(cost_rec)

    service = AICenterScreenService(database, clock=lambda: FIXED_NOW)
    view = service.load(channel.id)

    assert view.shell.active_screen is DashboardScreenId.AI_CENTER
    assert view.load_state is ViewLoadState.READY
    assert view.active_session is not None
    assert view.active_session.session_id == session.id
    assert view.active_session.job_count == 2

    assert len(view.active_jobs) == 1
    assert view.active_jobs[0].job_id == running_job.id
    assert view.active_jobs[0].checkpoint_count == 1
    assert view.active_jobs[0].last_checkpoint_step == "render.frames_composited"

    assert len(view.failed_jobs) == 1
    assert view.failed_jobs[0].job_id == failed_job.id
    assert view.failed_jobs[0].attempts == 2
    assert view.failed_jobs[0].retry_count == 1
    assert view.failed_jobs[0].last_error == "upstream timeout"

    assert view.provider_and_costs.total_cost == Decimal("1.250000")
    assert view.provider_and_costs.by_category["llm"] == Decimal("1.250000")

    html_out = service.render_html(view)
    assert 'data-testid="ai-center-screen"' in html_out
    assert "render.frames_composited" in html_out
    assert "upstream timeout" in html_out
