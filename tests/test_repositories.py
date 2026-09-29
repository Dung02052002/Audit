import dataclasses
import sqlite3
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest

from ai_youtube_agent.content.analytics import MetricScope
from ai_youtube_agent.content.approval import ApprovalStatus
from ai_youtube_agent.content.channel import ChannelStatus
from ai_youtube_agent.content.comment import CommentLabel, ReplyStatus
from ai_youtube_agent.content.revenue import RevenueStage
from ai_youtube_agent.content.rights import RiskLevel
from ai_youtube_agent.content.strategy import Budget, Market
from ai_youtube_agent.core.artifact import ArtifactKind
from ai_youtube_agent.core.content_item import ContentStatus
from ai_youtube_agent.core.db.database import (
    ConcurrencyError,
    Database,
    RecordNotFoundError,
)
from ai_youtube_agent.core.db.repositories.channel import (
    ChannelRepository,
    StrategyProfileRepository,
    VoiceProfileRepository,
)
from ai_youtube_agent.core.db.repositories.community import CommentRepository
from ai_youtube_agent.core.db.repositories.content import (
    ArtifactRepository,
    AudioMetadataRepository,
    ContentItemRepository,
    ScriptRepository,
)
from ai_youtube_agent.core.db.repositories.economics import (
    CostRecordRepository,
    MetricSnapshotRepository,
    RevenueRecordRepository,
)
from ai_youtube_agent.core.db.repositories.experiment import ExperimentRepository
from ai_youtube_agent.core.db.repositories.jobs import (
    AIJobRepository,
    SessionRepository,
)
from ai_youtube_agent.core.db.repositories.publish import PublishJobRepository
from ai_youtube_agent.core.db.repositories.review import (
    ApprovalRequestRepository,
    QCResultRepository,
    RightsRecordRepository,
)
from factories import (
    AI,
    SYSTEM,
    T0,
    USER,
    at,
    make_ai_job,
    make_approval_request,
    make_artifact,
    make_audio_metadata,
    make_channel,
    make_claim,
    make_classification,
    make_comment,
    make_content_item,
    make_cost_record,
    make_evidence,
    make_experiment,
    make_metric_snapshot,
    make_publish_job,
    make_qc_result,
    make_reply_draft,
    make_revenue_record,
    make_rights_record,
    make_script,
    make_session,
    make_strategy_profile,
    make_voice_profile,
)

T1 = T0 + timedelta(minutes=5)
T2 = T0 + timedelta(minutes=10)

IMMUTABLE_REPOSITORIES = [
    ArtifactRepository,
    ScriptRepository,
    AudioMetadataRepository,
    QCResultRepository,
    MetricSnapshotRepository,
    RevenueRecordRepository,
    CostRecordRepository,
]
ALL_REPOSITORIES = [
    *IMMUTABLE_REPOSITORIES,
    ChannelRepository,
    StrategyProfileRepository,
    VoiceProfileRepository,
    ContentItemRepository,
    RightsRecordRepository,
    ApprovalRequestRepository,
    PublishJobRepository,
    CommentRepository,
    SessionRepository,
    AIJobRepository,
    ExperimentRepository,
]


def save(database: Database, repository, method: str, *entities) -> None:
    with database.transaction() as connection:
        writer = getattr(repository(connection), method)
        for entity in entities:
            writer(entity)


def read(database: Database, repository, method: str, *args, **kwargs):
    with database.transaction() as connection:
        return getattr(repository(connection), method)(*args, **kwargs)


@pytest.fixture
def graph(database: Database) -> SimpleNamespace:
    """A stored channel, strategy profile and content item."""
    channel = make_channel()
    strategy = make_strategy_profile(channel)
    item = make_content_item(channel, strategy)
    save(database, ChannelRepository, "add", channel)
    save(database, StrategyProfileRepository, "add", strategy)
    save(database, ContentItemRepository, "add", item)
    return SimpleNamespace(channel=channel, strategy=strategy, item=item)


# Database and transactions


def test_transaction_commits_on_success(database: Database) -> None:
    channel = make_channel()
    save(database, ChannelRepository, "add", channel)

    connection = database.connect()
    try:
        assert ChannelRepository(connection).get(channel.id) == channel
    finally:
        connection.close()


def test_transaction_rolls_back_every_write_on_error(database: Database) -> None:
    first, second = make_channel(), make_channel()

    with pytest.raises(RuntimeError), database.transaction() as connection:
        repository = ChannelRepository(connection)
        repository.add(first)
        repository.add(second)
        raise RuntimeError("boom")

    assert read(database, ChannelRepository, "list_all") == []


def test_a_failed_statement_rolls_back_the_whole_transaction(
    database: Database,
) -> None:
    channel = make_channel()
    orphan = make_strategy_profile(make_channel())

    with pytest.raises(sqlite3.IntegrityError), database.transaction() as connection:
        ChannelRepository(connection).add(channel)
        StrategyProfileRepository(connection).add(orphan)

    assert read(database, ChannelRepository, "get", channel.id) is None


def test_connections_enforce_foreign_keys(database: Database) -> None:
    connection = database.connect()
    try:
        assert connection.execute("PRAGMA foreign_keys").fetchone() == (1,)
    finally:
        connection.close()


@pytest.mark.parametrize("repository", IMMUTABLE_REPOSITORIES)
def test_immutable_records_have_no_update(repository) -> None:
    assert not any(name.startswith("update") for name in dir(repository))


@pytest.mark.parametrize("repository", ALL_REPOSITORIES)
def test_no_repository_deletes(repository) -> None:
    assert not any(
        word in name for name in dir(repository) for word in ("delete", "remove")
    )


def test_datetimes_and_money_are_stored_in_canonical_text(
    database: Database, graph
) -> None:
    save(database, CostRecordRepository, "add", make_cost_record(graph.channel))
    connection = database.connect()
    try:
        row = connection.execute(
            "SELECT amount, incurred_at FROM cost_records"
        ).fetchone()
        strategy = connection.execute(
            "SELECT budget_daily_limit, created_at FROM strategy_profiles"
        ).fetchone()
    finally:
        connection.close()
    assert row == ("0.0421", "2026-09-30T08:00:00.123456Z")
    assert strategy == ("5.00", "2026-09-30T08:00:00.123456Z")


# Channel and strategy


def test_channel_round_trip_and_list(database: Database) -> None:
    first, second = make_channel(), make_channel(created_at=T0, updated_at=T1)
    save(database, ChannelRepository, "add", second, first)

    assert read(database, ChannelRepository, "get", first.id) == first
    assert read(database, ChannelRepository, "get", "missing") is None
    assert {c.id for c in read(database, ChannelRepository, "list_all")} == {
        first.id,
        second.id,
    }


def test_channel_update_uses_the_last_read_time(database: Database) -> None:
    channel = make_channel()
    save(database, ChannelRepository, "add", channel)
    changed = channel.with_status(ChannelStatus.ACTIVE, clock=at(T1))

    with database.transaction() as connection:
        ChannelRepository(connection).update(
            changed, expected_updated_at=channel.updated_at
        )

    assert read(database, ChannelRepository, "get", channel.id) == changed


def test_stale_channel_update_is_refused(database: Database) -> None:
    channel = make_channel()
    save(database, ChannelRepository, "add", channel)
    first = channel.rename("First edit", clock=at(T1))
    second = channel.rename("Second edit", clock=at(T2))
    with database.transaction() as connection:
        ChannelRepository(connection).update(
            first, expected_updated_at=channel.updated_at
        )

    with pytest.raises(ConcurrencyError), database.transaction() as connection:
        ChannelRepository(connection).update(
            second, expected_updated_at=channel.updated_at
        )

    assert read(database, ChannelRepository, "get", channel.id).title == "First edit"


def test_updating_a_missing_row_is_reported(database: Database) -> None:
    with pytest.raises(RecordNotFoundError), database.transaction() as connection:
        ChannelRepository(connection).update(make_channel(), expected_updated_at=T0)


def test_strategy_round_trip_keeps_value_objects_and_decimals(
    database: Database, graph
) -> None:
    stored = read(database, StrategyProfileRepository, "get", graph.strategy.id)

    assert stored == graph.strategy
    assert str(stored.budget.daily_limit) == "5.00"
    assert stored.languages.secondary == ("en-US",)
    assert stored.niche.pillars == ("budgeting", "investing")
    assert (
        read(database, StrategyProfileRepository, "get_by_channel", graph.channel.id)
        == graph.strategy
    )


def test_strategy_update_uses_the_version(database: Database, graph) -> None:
    changed = graph.strategy.update(
        actor=USER,
        clock=at(T1),
        market=Market("US"),
        budget=Budget("USD", Decimal("10.50"), Decimal("200")),
    )

    with database.transaction() as connection:
        StrategyProfileRepository(connection).update(changed, expected_version=1)
    with pytest.raises(ConcurrencyError), database.transaction() as connection:
        StrategyProfileRepository(connection).update(changed, expected_version=1)

    stored = read(database, StrategyProfileRepository, "get", graph.strategy.id)
    assert stored == changed
    assert stored.version == 2


def test_one_strategy_per_channel(database: Database, graph) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        save(
            database,
            StrategyProfileRepository,
            "add",
            make_strategy_profile(graph.channel),
        )


def test_voice_profile_round_trip_and_update(database: Database, graph) -> None:
    voice = make_voice_profile(graph.channel)
    save(database, VoiceProfileRepository, "add", voice)
    changed = voice.update(actor=USER, clock=at(T1), voice_id="vi-male-02")

    with database.transaction() as connection:
        VoiceProfileRepository(connection).update(changed, expected_version=1)

    assert read(database, VoiceProfileRepository, "get", voice.id) == changed
    assert read(
        database, VoiceProfileRepository, "list_by_channel", graph.channel.id
    ) == [changed]


# Content, artifacts and scripts


def test_content_item_round_trip_list_and_update(database: Database, graph) -> None:
    changed = graph.item.with_status(ContentStatus.GENERATING, clock=at(T1))
    with database.transaction() as connection:
        ContentItemRepository(connection).update(
            changed, expected_updated_at=graph.item.updated_at
        )

    assert read(database, ContentItemRepository, "get", graph.item.id) == changed
    assert read(
        database, ContentItemRepository, "list_by_channel", graph.channel.id
    ) == [changed]


def test_artifact_versions_round_trip_in_order(database: Database, graph) -> None:
    video = make_artifact(graph.item)
    newer = video.next_version(
        uri="store://v2", sha256="f" * 64, size_bytes=5, media_type="video/mp4"
    )
    audio = make_artifact(graph.item, ArtifactKind.AUDIO)
    save(database, ArtifactRepository, "add", newer, audio, video)

    assert read(database, ArtifactRepository, "get", video.id) == video
    assert read(
        database, ArtifactRepository, "list_by_content_item", graph.item.id
    ) == [audio, video, newer]


def test_artifact_version_is_unique(database: Database, graph) -> None:
    video = make_artifact(graph.item)
    save(database, ArtifactRepository, "add", video)
    with pytest.raises(sqlite3.IntegrityError):
        save(database, ArtifactRepository, "add", dataclasses.replace(video, id="x"))


def test_script_claims_and_evidence_round_trip(database: Database, graph) -> None:
    script = make_script(graph.item)
    second = script.next_version("A shorter hook.", clock=at(T1))
    claim = make_claim(script)
    evidence = make_evidence(claim)

    with database.transaction() as connection:
        repository = ScriptRepository(connection)
        repository.add(second)
        repository.add(script)
        repository.add_claim(claim)
        repository.add_evidence(evidence)

    assert read(database, ScriptRepository, "get", script.id) == script
    assert read(database, ScriptRepository, "list_by_content_item", graph.item.id) == [
        script,
        second,
    ]
    assert read(database, ScriptRepository, "list_claims", script.id) == [claim]
    assert read(database, ScriptRepository, "list_evidence", claim.id) == [evidence]
    assert read(database, ScriptRepository, "list_claims", second.id) == []


def test_audio_metadata_round_trip(database: Database, graph) -> None:
    artifact = make_artifact(graph.item, ArtifactKind.AUDIO)
    script = make_script(graph.item)
    voice = make_voice_profile(graph.channel)
    audio = make_audio_metadata(artifact, script, voice)
    save(database, ArtifactRepository, "add", artifact)
    save(database, ScriptRepository, "add", script)
    save(database, VoiceProfileRepository, "add", voice)
    save(database, AudioMetadataRepository, "add", audio)

    assert read(database, AudioMetadataRepository, "get", audio.id) == audio
    assert (
        read(database, AudioMetadataRepository, "get_by_artifact", artifact.id) == audio
    )


# Rights, QC and approval


def test_rights_record_round_trip_and_resolution(database: Database, graph) -> None:
    record = make_rights_record(graph.item)
    save(database, RightsRecordRepository, "add", record)
    resolved = record.with_risk_level(RiskLevel.HIGH, clock=at(T1)).resolve(
        actor=USER, clock=at(T2)
    )

    with database.transaction() as connection:
        RightsRecordRepository(connection).update(
            resolved, expected_updated_at=record.updated_at
        )

    assert read(database, RightsRecordRepository, "get", record.id) == resolved
    assert read(
        database, RightsRecordRepository, "list_by_content_item", graph.item.id
    ) == [resolved]


def test_qc_result_keeps_artifact_and_check_order(database: Database, graph) -> None:
    video = make_artifact(graph.item)
    subtitles = make_artifact(graph.item, ArtifactKind.SUBTITLES)
    save(database, ArtifactRepository, "add", video, subtitles)
    result = make_qc_result(graph.item, [subtitles, video])
    save(database, QCResultRepository, "add", result)

    stored = read(database, QCResultRepository, "get", result.id)
    assert stored == result
    assert stored.artifact_ids == (subtitles.id, video.id)
    assert [c.name for c in stored.checks] == ["video.fps", "audio.silence"]
    assert read(
        database, QCResultRepository, "list_by_content_item", graph.item.id
    ) == [result]


def test_approval_request_round_trip_and_status_update(
    database: Database, graph
) -> None:
    video = make_artifact(graph.item)
    save(database, ArtifactRepository, "add", video)
    qc = make_qc_result(graph.item, [video])
    save(database, QCResultRepository, "add", qc)
    request = make_approval_request(graph.item, [video], qc)
    save(database, ApprovalRequestRepository, "add", request)
    approved = dataclasses.replace(request, status=ApprovalStatus.APPROVED)

    with database.transaction() as connection:
        ApprovalRequestRepository(connection).update(
            approved, expected_status=ApprovalStatus.PENDING
        )
    with pytest.raises(ConcurrencyError), database.transaction() as connection:
        ApprovalRequestRepository(connection).update(
            request, expected_status=ApprovalStatus.PENDING
        )

    stored = read(database, ApprovalRequestRepository, "get", request.id)
    assert stored == approved
    assert stored.artifacts == request.artifacts
    assert read(
        database, ApprovalRequestRepository, "list_by_content_item", graph.item.id
    ) == [approved]


# Publishing


def test_publish_job_round_trip_through_success(database: Database, graph) -> None:
    video = make_artifact(graph.item)
    save(database, ArtifactRepository, "add", video)
    request = make_approval_request(graph.item, [video])
    save(database, ApprovalRequestRepository, "add", request)
    job = make_publish_job(request)
    save(database, PublishJobRepository, "add", job)
    done = job.start(clock=at(T1)).succeed("dQw4w9WgXcQ", clock=at(T2))

    with database.transaction() as connection:
        PublishJobRepository(connection).update(
            done, expected_updated_at=job.updated_at
        )

    assert read(database, PublishJobRepository, "get", job.id) == done
    assert (
        read(
            database,
            PublishJobRepository,
            "get_by_idempotency_key",
            job.idempotency_key,
        )
        == done
    )
    assert read(
        database, PublishJobRepository, "list_by_content_item", graph.item.id
    ) == [done]
    with pytest.raises(sqlite3.IntegrityError):
        save(database, PublishJobRepository, "add", dataclasses.replace(job, id="x"))


# Analytics and economics


def test_metric_snapshot_round_trip(database: Database) -> None:
    snapshot = make_metric_snapshot()
    save(database, MetricSnapshotRepository, "add", snapshot)

    stored = read(database, MetricSnapshotRepository, "get", snapshot.id)
    assert stored == snapshot
    assert stored.metrics["subscribers_net"] == Decimal("-3")
    assert str(stored.metrics["ctr"]) == "0.0412"
    assert read(
        database,
        MetricSnapshotRepository,
        "list_by_subject",
        MetricScope.VIDEO,
        "dQw4w9WgXcQ",
    ) == [snapshot]


def test_revenue_estimate_and_final_are_separate_rows(database: Database) -> None:
    estimate = make_revenue_record()
    final = make_revenue_record(
        stage=RevenueStage.FINAL,
        amount=Decimal("11.90"),
        retrieved_at=T0 + timedelta(days=3),
    )
    save(database, RevenueRecordRepository, "add", final, estimate)

    stored = read(
        database,
        RevenueRecordRepository,
        "list_by_subject",
        MetricScope.CHANNEL,
        "channel-1",
    )
    assert stored == [estimate, final]
    assert str(stored[0].amount) == "12.30"


def test_cost_records_by_channel_in_a_half_open_range(
    database: Database, graph
) -> None:
    before = make_cost_record(graph.channel, incurred_at=T0 - timedelta(seconds=1))
    at_start = make_cost_record(graph.channel, incurred_at=T0)
    inside = make_cost_record(
        graph.channel, incurred_at=T1, content_item_id=graph.item.id
    )
    at_end = make_cost_record(graph.channel, incurred_at=T2)
    save(database, CostRecordRepository, "add", at_end, inside, before, at_start)

    assert read(database, CostRecordRepository, "get", inside.id) == inside
    assert read(
        database, CostRecordRepository, "list_by_channel", graph.channel.id
    ) == [before, at_start, inside, at_end]
    assert read(
        database,
        CostRecordRepository,
        "list_by_channel",
        graph.channel.id,
        start=T0,
        end=T2,
    ) == [at_start, inside]


# Community


def test_comment_classification_and_reply_draft_round_trip(
    database: Database, graph
) -> None:
    comment = make_comment(graph.channel)
    first = make_classification(comment)
    second = make_classification(
        comment, label=CommentLabel.MODERATION, classified_by=USER, classified_at=T1
    )
    draft = make_reply_draft(comment)

    with database.transaction() as connection:
        repository = CommentRepository(connection)
        repository.add(comment)
        repository.add_classification(second)
        repository.add_classification(first)
        repository.add_reply_draft(draft)

    assert read(database, CommentRepository, "get", comment.id) == comment
    assert (
        read(
            database,
            CommentRepository,
            "get_by_youtube_comment_id",
            comment.youtube_comment_id,
        )
        == comment
    )
    assert read(database, CommentRepository, "list_by_channel", graph.channel.id) == [
        comment
    ]
    assert read(database, CommentRepository, "list_classifications", comment.id) == [
        first,
        second,
    ]
    assert read(database, CommentRepository, "get_reply_draft", draft.id) == draft
    assert read(database, CommentRepository, "list_reply_drafts", comment.id) == [draft]


def test_reply_draft_status_update(database: Database, graph) -> None:
    comment = make_comment(graph.channel)
    draft = make_reply_draft(comment)
    with database.transaction() as connection:
        CommentRepository(connection).add(comment)
        CommentRepository(connection).add_reply_draft(draft)
    posted = dataclasses.replace(
        draft, status=ReplyStatus.POSTED, youtube_reply_id="Ugx-reply-1"
    )

    with database.transaction() as connection:
        CommentRepository(connection).update_reply_draft(
            posted, expected_status=ReplyStatus.DRAFT
        )
    with pytest.raises(ConcurrencyError), database.transaction() as connection:
        CommentRepository(connection).update_reply_draft(
            posted, expected_status=ReplyStatus.DRAFT
        )

    assert read(database, CommentRepository, "get_reply_draft", draft.id) == posted


# Sessions and AI jobs


def test_session_round_trip_and_stop(database: Database) -> None:
    session = make_session()
    save(database, SessionRepository, "add", session)
    stopped = session.stop(clock=at(T1))

    with database.transaction() as connection:
        SessionRepository(connection).update(stopped, expected_status=session.status)
    with pytest.raises(ConcurrencyError), database.transaction() as connection:
        SessionRepository(connection).update(stopped, expected_status=session.status)

    assert read(database, SessionRepository, "get", session.id) == stopped


def test_ai_job_checkpoints_are_appended(database: Database, graph) -> None:
    session = make_session()
    save(database, SessionRepository, "add", session)
    job = make_ai_job(graph.item, session)
    save(database, AIJobRepository, "add", job)
    running = job.start(clock=at(T1)).checkpoint(
        "script.outline_saved", {"sections": 3, "model": "mock"}, clock=at(T1)
    )
    failed = running.fail("provider timeout", clock=at(T2))

    with database.transaction() as connection:
        AIJobRepository(connection).update(running, expected_updated_at=job.updated_at)
    with database.transaction() as connection:
        AIJobRepository(connection).update(
            failed, expected_updated_at=running.updated_at
        )

    stored = read(database, AIJobRepository, "get", job.id)
    assert stored == failed
    assert dict(stored.last_checkpoint.data) == {"sections": 3, "model": "mock"}
    assert (
        read(database, AIJobRepository, "get_by_idempotency_key", job.idempotency_key)
        == failed
    )
    assert read(database, AIJobRepository, "list_by_content_item", graph.item.id) == [
        failed
    ]


def test_ai_job_update_cannot_drop_checkpoints(database: Database) -> None:
    job = make_ai_job()
    running = job.start(clock=at(T1)).checkpoint("script.a", clock=at(T1))
    save(database, AIJobRepository, "add", running)
    without = dataclasses.replace(running.fail("x", clock=at(T2)), checkpoints=())

    with pytest.raises(ConcurrencyError), database.transaction() as connection:
        AIJobRepository(connection).update(
            without, expected_updated_at=running.updated_at
        )

    assert read(database, AIJobRepository, "get", job.id) == running


def test_ai_job_idempotency_key_is_unique(database: Database) -> None:
    job = make_ai_job()
    save(database, AIJobRepository, "add", job)
    with pytest.raises(sqlite3.IntegrityError):
        save(database, AIJobRepository, "add", dataclasses.replace(job, id="x"))


# Experiments


def test_experiment_round_trip_through_conclusion(database: Database, graph) -> None:
    experiment = make_experiment(graph.channel, graph.item)
    save(database, ExperimentRepository, "add", experiment)
    snapshot = make_metric_snapshot()
    save(database, MetricSnapshotRepository, "add", snapshot)
    running = experiment.start(actor=USER, clock=at(T1))
    concluded = running.conclude(
        actor=USER,
        winner_key="b",
        note="b had 1.4x CTR",
        metric_snapshot_ids=[snapshot.id],
        clock=at(T2),
    )

    with database.transaction() as connection:
        repository = ExperimentRepository(connection)
        repository.update(running, expected_updated_at=experiment.updated_at)
        repository.update(concluded, expected_updated_at=running.updated_at)
    with pytest.raises(ConcurrencyError), database.transaction() as connection:
        ExperimentRepository(connection).update(
            concluded, expected_updated_at=running.updated_at
        )

    stored = read(database, ExperimentRepository, "get", experiment.id)
    assert stored == concluded
    assert stored.conclusion.concluded_by == USER
    assert stored.variants == experiment.variants
    assert read(
        database, ExperimentRepository, "list_by_channel", graph.channel.id
    ) == [concluded]


def test_cancelled_experiment_round_trip(database: Database, graph) -> None:
    experiment = make_experiment(graph.channel)
    save(database, ExperimentRepository, "add", experiment)
    cancelled = experiment.cancel(actor=USER, clock=at(T1))

    with database.transaction() as connection:
        ExperimentRepository(connection).update(
            cancelled, expected_updated_at=experiment.updated_at
        )

    assert read(database, ExperimentRepository, "get", experiment.id) == cancelled


def test_factories_use_the_actors_the_rules_expect() -> None:
    assert (USER.kind.value, SYSTEM.kind.value, AI.kind.value) == (
        "user",
        "system",
        "ai",
    )
