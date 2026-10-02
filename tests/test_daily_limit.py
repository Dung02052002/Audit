"""C-036 Daily Limit Gate (Prompt Pack v8, prompt #036).

Rules the user approved on 2026-09-30:

- the limit is the channel's ``StrategyProfile.cadence`` for the item's content
  type, applied separately to production and to publishing;
- production is counted from the append-only ``production_starts`` log
  (migration 0002), written when an item moves draft -> generating;
- a day is 00:00-24:00 UTC;
- the gate is ``GateName.DAILY_LIMIT``.
"""

import dataclasses
import sqlite3
from datetime import UTC, datetime, timedelta, timezone

import pytest

from ai_youtube_agent.content.approval import ApprovalStatus
from ai_youtube_agent.content.strategy import Cadence
from ai_youtube_agent.core.audit import Actor, ActorKind
from ai_youtube_agent.core.content_item import (
    ContentItem,
    ContentStatus,
    ContentType,
)
from ai_youtube_agent.core.daily_limit_gate import DailyLimitGate, day_window
from ai_youtube_agent.core.db.database import Database
from ai_youtube_agent.core.db.repositories.channel import (
    ChannelRepository,
    StrategyProfileRepository,
)
from ai_youtube_agent.core.db.repositories.content import (
    ArtifactRepository,
    ContentItemRepository,
)
from ai_youtube_agent.core.db.repositories.publish import PublishJobRepository
from ai_youtube_agent.core.db.repositories.review import ApprovalRequestRepository
from ai_youtube_agent.core.db.repositories.usage import (
    DailyUsageRepository,
    ProductionStartRepository,
)
from ai_youtube_agent.core.errors import DomainError
from ai_youtube_agent.core.gates import (
    GateContext,
    GateName,
    GateOutcome,
    PipelineGate,
    evaluate_gates,
)
from ai_youtube_agent.core.production import start_production
from ai_youtube_agent.core.production_start import (
    ProductionStart,
    ProductionStartError,
)
from ai_youtube_agent.pipeline.publish import PublishJob
from factories import (
    make_approval_request,
    make_artifact,
    make_channel,
    make_content_item,
    make_strategy_profile,
)

DAY = datetime(2026, 9, 30, tzinfo=UTC)
NOON = DAY + timedelta(hours=12)
SYSTEM = Actor(ActorKind.SYSTEM, "pipeline")


def at(moment: datetime):
    return lambda: moment


class Graph:
    """A stored channel with a strategy, and helpers to add items and usage."""

    def __init__(self, database: Database, cadence: Cadence | None = None) -> None:
        self.database = database
        self.channel = make_channel()
        strategy = make_strategy_profile(self.channel)
        if cadence is not None:
            strategy = dataclasses.replace(strategy, cadence=cadence)
        self.strategy = strategy
        with database.transaction() as connection:
            ChannelRepository(connection).add(self.channel)
            StrategyProfileRepository(connection).add(strategy)

    def item(
        self,
        status: ContentStatus = ContentStatus.DRAFT,
        content_type: ContentType = ContentType.SHORTS,
        created: datetime = DAY - timedelta(days=1),
    ) -> ContentItem:
        item = make_content_item(
            self.channel,
            self.strategy,
            status=status,
            content_type=content_type,
            created_at=created,
            updated_at=created,
        )
        with self.database.transaction() as connection:
            ContentItemRepository(connection).add(item)
        return item

    def started(self, when: datetime, content_type=ContentType.SHORTS) -> None:
        # Created before ``when``, so the start never predates the item.
        item = self.item(content_type=content_type, created=when - timedelta(days=1))
        with self.database.transaction() as connection:
            start_production(connection, item, clock=at(when))

    def published(
        self,
        when: datetime,
        content_type: ContentType = ContentType.SHORTS,
        *,
        item: ContentItem | None = None,
        failed: bool = False,
    ) -> None:
        item = item or self.item(ContentStatus.APPROVED, content_type)
        video = make_artifact(item)
        approval = make_approval_request(item, [video], status=ApprovalStatus.APPROVED)
        job = PublishJob.create(
            approval, content_type, f"publish:{approval.id}", clock=at(when)
        )
        if failed:
            job = job.start(clock=at(when)).fail("upload error", clock=at(when))
        with self.database.transaction() as connection:
            ArtifactRepository(connection).add(video)
            ApprovalRequestRepository(connection).add(approval)
            PublishJobRepository(connection).add(job)

    def evaluate(self, item: ContentItem, target: ContentStatus, when=NOON):
        with self.database.transaction() as connection:
            gate = DailyLimitGate(
                StrategyProfileRepository(connection), DailyUsageRepository(connection)
            )
            return gate.evaluate(
                GateContext(item=item, target_status=target, actor=SYSTEM, at=when)
            )


def codes(result) -> list[str]:
    return [r.code for r in result.reasons]


# The day


@pytest.mark.parametrize(
    ("moment", "start"),
    [
        (NOON, DAY),
        (DAY, DAY),
        (DAY + timedelta(days=1) - timedelta(microseconds=1), DAY),
        (DAY + timedelta(days=1), DAY + timedelta(days=1)),
    ],
)
def test_a_day_is_a_utc_calendar_day(moment: datetime, start: datetime) -> None:
    assert day_window(moment) == (start, start + timedelta(days=1))


def test_the_day_of_a_non_utc_time_is_refused() -> None:
    with pytest.raises(ValueError):
        day_window(NOON.astimezone(timezone(timedelta(hours=7))))


# The production start log


def test_start_production_moves_a_draft_and_logs_it(database: Database) -> None:
    graph = Graph(database)
    item = graph.item()

    with database.transaction() as connection:
        moved, start = start_production(connection, item, clock=at(NOON))

    assert moved.status is ContentStatus.GENERATING
    assert (start.content_item_id, start.channel_id, start.content_type) == (
        item.id,
        graph.channel.id,
        ContentType.SHORTS,
    )
    assert start.started_at == NOON
    with database.transaction() as connection:
        assert ContentItemRepository(connection).get(item.id) == moved
        assert ProductionStartRepository(connection).list_by_content_item(item.id) == [
            start
        ]


@pytest.mark.parametrize(
    "status", [s for s in ContentStatus if s is not ContentStatus.DRAFT]
)
def test_only_a_draft_can_start_production(database: Database, status) -> None:
    graph = Graph(database)
    item = graph.item(status)

    with (
        pytest.raises(ProductionStartError) as caught,
        database.transaction() as connection,
    ):
        start_production(connection, item, clock=at(NOON))

    assert isinstance(caught.value, DomainError)
    assert caught.value.code == "domain.production_start"
    with database.transaction() as connection:
        assert ProductionStartRepository(connection).list_by_content_item(item.id) == []


def test_a_restart_after_failure_is_logged_again(database: Database) -> None:
    graph = Graph(database)
    item = graph.item()
    with database.transaction() as connection:
        moved, _ = start_production(connection, item, clock=at(NOON))
        failed = moved.with_status(ContentStatus.FAILED, clock=at(NOON))
        ContentItemRepository(connection).update(
            failed, expected_updated_at=moved.updated_at
        )
        draft = failed.with_status(ContentStatus.DRAFT, clock=at(NOON))
        ContentItemRepository(connection).update(
            draft, expected_updated_at=failed.updated_at
        )
        start_production(connection, draft, clock=at(NOON + timedelta(hours=1)))

    with database.transaction() as connection:
        starts = ProductionStartRepository(connection).list_by_content_item(item.id)
    assert [s.started_at for s in starts] == [NOON, NOON + timedelta(hours=1)]


def test_the_log_is_append_only(database: Database) -> None:
    graph = Graph(database)
    graph.started(NOON)

    for sql in (
        "UPDATE production_starts SET started_at = started_at",
        "DELETE FROM production_starts",
    ):
        with (
            pytest.raises(sqlite3.DatabaseError, match="append-only"),
            database.transaction() as connection,
        ):
            connection.execute(sql)


def test_production_start_fields_are_checked() -> None:
    good = dict(
        id="s1",
        content_item_id="i1",
        channel_id="c1",
        content_type=ContentType.SHORTS,
        started_at=NOON,
    )
    ProductionStart(**good)
    for name, value, error in [
        ("id", "", ValueError),
        ("content_item_id", " ", ValueError),
        ("channel_id", "", ValueError),
        ("content_type", "shorts", TypeError),
        ("started_at", NOON.replace(tzinfo=None), ValueError),
    ]:
        with pytest.raises(error):
            ProductionStart(**{**good, name: value})


# Counting


def test_production_is_counted_per_channel_type_and_day(database: Database) -> None:
    graph, other = Graph(database), Graph(database)
    graph.started(DAY)  # counts: the first microsecond of the day
    graph.started(NOON)  # counts
    graph.started(DAY - timedelta(microseconds=1))  # yesterday
    graph.started(DAY + timedelta(days=1))  # tomorrow
    graph.started(NOON, ContentType.LONGFORM)  # other type
    other.started(NOON)  # other channel

    with database.transaction() as connection:
        usage = DailyUsageRepository(connection)
        start, end = day_window(NOON)
        assert (
            usage.count_production_starts(
                graph.channel.id, ContentType.SHORTS, start, end
            )
            == 2
        )
        assert (
            usage.count_production_starts(
                graph.channel.id, ContentType.LONGFORM, start, end
            )
            == 1
        )


def test_publishes_are_counted_without_failed_jobs(database: Database) -> None:
    graph, other = Graph(database), Graph(database)
    graph.published(NOON)
    graph.published(DAY)
    graph.published(NOON, failed=True)  # a failed upload does not count
    graph.published(DAY - timedelta(microseconds=1))  # yesterday
    graph.published(NOON, ContentType.LONGFORM)
    other.published(NOON)
    mine = graph.item(ContentStatus.APPROVED)
    graph.published(NOON, item=mine)

    with database.transaction() as connection:
        usage = DailyUsageRepository(connection)
        start, end = day_window(NOON)
        count = usage.count_publishes
        assert count(graph.channel.id, ContentType.SHORTS, start, end) == 3
        assert (
            count(graph.channel.id, ContentType.SHORTS, start, end, exclude=mine.id)
            == 2
        )
        assert count(graph.channel.id, ContentType.LONGFORM, start, end) == 1


# The gate


def test_the_gate_follows_the_contract(database: Database) -> None:
    with database.transaction() as connection:
        gate = DailyLimitGate(
            StrategyProfileRepository(connection), DailyUsageRepository(connection)
        )
    assert isinstance(gate, PipelineGate)
    assert gate.name is GateName.DAILY_LIMIT


def test_production_passes_under_the_limit(database: Database) -> None:
    graph = Graph(database, Cadence(shorts_per_day=2, longform_per_day=0))
    graph.started(NOON - timedelta(hours=1))

    result = graph.evaluate(graph.item(), ContentStatus.GENERATING)

    assert result.outcome is GateOutcome.PASS
    assert result.gate is GateName.DAILY_LIMIT


def test_production_blocks_at_the_limit(database: Database) -> None:
    graph = Graph(database, Cadence(shorts_per_day=2, longform_per_day=0))
    graph.started(DAY)
    graph.started(NOON - timedelta(hours=1))

    result = graph.evaluate(graph.item(), ContentStatus.GENERATING)

    assert codes(result) == ["daily_limit.production_reached"]
    assert "2" in result.reasons[0].message
    assert "Shorts" in result.reasons[0].message


def test_yesterdays_production_does_not_count(database: Database) -> None:
    graph = Graph(database, Cadence(shorts_per_day=1, longform_per_day=0))
    graph.started(DAY - timedelta(hours=1))

    assert graph.evaluate(graph.item(), ContentStatus.GENERATING).is_passed


def test_each_content_type_has_its_own_limit(database: Database) -> None:
    graph = Graph(database, Cadence(shorts_per_day=1, longform_per_day=1))
    graph.started(NOON, ContentType.SHORTS)

    shorts = graph.item(content_type=ContentType.SHORTS)
    longform = graph.item(content_type=ContentType.LONGFORM)
    assert not graph.evaluate(shorts, ContentStatus.GENERATING).is_passed
    assert graph.evaluate(longform, ContentStatus.GENERATING).is_passed


def test_a_limit_of_zero_blocks_everything(database: Database) -> None:
    graph = Graph(database, Cadence(shorts_per_day=2, longform_per_day=0))
    item = graph.item(content_type=ContentType.LONGFORM)

    assert codes(graph.evaluate(item, ContentStatus.GENERATING)) == [
        "daily_limit.production_reached"
    ]
    approved = graph.item(ContentStatus.APPROVED, ContentType.LONGFORM)
    assert codes(graph.evaluate(approved, ContentStatus.PUBLISHING)) == [
        "daily_limit.publish_reached"
    ]


def test_publishing_passes_under_and_blocks_at_the_limit(
    database: Database,
) -> None:
    graph = Graph(database, Cadence(shorts_per_day=2, longform_per_day=0))
    graph.published(NOON - timedelta(hours=2))
    item = graph.item(ContentStatus.APPROVED)

    assert graph.evaluate(item, ContentStatus.PUBLISHING).is_passed

    graph.published(NOON - timedelta(hours=1))
    result = graph.evaluate(item, ContentStatus.PUBLISHING)
    assert codes(result) == ["daily_limit.publish_reached"]


def test_the_items_own_publish_job_does_not_count(database: Database) -> None:
    graph = Graph(database, Cadence(shorts_per_day=1, longform_per_day=0))
    item = graph.item(ContentStatus.APPROVED)
    graph.published(NOON, item=item)

    assert graph.evaluate(item, ContentStatus.PUBLISHING).is_passed


def test_a_failed_publish_frees_the_slot(database: Database) -> None:
    graph = Graph(database, Cadence(shorts_per_day=1, longform_per_day=0))
    graph.published(NOON, failed=True)

    item = graph.item(ContentStatus.APPROVED)
    assert graph.evaluate(item, ContentStatus.PUBLISHING).is_passed


def test_production_and_publishing_are_counted_separately(
    database: Database,
) -> None:
    graph = Graph(database, Cadence(shorts_per_day=1, longform_per_day=0))
    graph.started(NOON)
    graph.published(NOON)

    # Production is full, but that does not block a publish, and vice versa.
    assert not graph.evaluate(graph.item(), ContentStatus.GENERATING).is_passed
    assert not graph.evaluate(
        graph.item(ContentStatus.APPROVED), ContentStatus.PUBLISHING
    ).is_passed

    fresh = Graph(database, Cadence(shorts_per_day=1, longform_per_day=0))
    fresh.started(NOON)
    item = fresh.item(ContentStatus.APPROVED)
    assert fresh.evaluate(item, ContentStatus.PUBLISHING).is_passed


def test_a_channel_without_strategy_is_blocked(database: Database) -> None:
    channel = make_channel()
    with database.transaction() as connection:
        ChannelRepository(connection).add(channel)
    # The gate only reads strategy and usage, so the item need not be stored.
    item = make_content_item(channel, make_strategy_profile(channel))

    with database.transaction() as connection:
        gate = DailyLimitGate(
            StrategyProfileRepository(connection), DailyUsageRepository(connection)
        )
        result = gate.evaluate(
            GateContext(item, ContentStatus.GENERATING, SYSTEM, NOON)
        )

    assert codes(result) == ["daily_limit.no_strategy"]


@pytest.mark.parametrize(
    ("status", "target"),
    [
        (ContentStatus.DRAFT, ContentStatus.FAILED),
        (ContentStatus.TESTING, ContentStatus.GENERATING),  # regeneration
        (ContentStatus.APPROVED, ContentStatus.GENERATING),
        (ContentStatus.GENERATING, ContentStatus.TESTING),
        (ContentStatus.PUBLISHING, ContentStatus.PUBLISHED),
    ],
)
def test_other_moves_are_not_this_gates_concern(
    database: Database, status, target
) -> None:
    graph = Graph(database, Cadence(shorts_per_day=0, longform_per_day=0))
    assert graph.evaluate(graph.item(status), target).is_passed


def test_a_broken_source_blocks_through_evaluate_gates(database: Database) -> None:
    class Broken:
        def get_by_channel(self, channel_id: str):
            raise RuntimeError("database is locked")

    graph = Graph(database)
    item = graph.item()
    with database.transaction() as connection:
        gate = DailyLimitGate(Broken(), DailyUsageRepository(connection))
        report = evaluate_gates(
            [gate], GateContext(item, ContentStatus.GENERATING, SYSTEM, NOON)
        )
    assert [r.code for r in report.reasons] == ["gate.error"]


# The cadence time zone (D-050, user decision 2026-10-02)

HO_CHI_MINH = Cadence(
    shorts_per_day=1, longform_per_day=0, time_zone="Asia/Ho_Chi_Minh"
)


def test_a_day_in_a_time_zone_starts_at_local_midnight() -> None:
    # 2026-09-30 12:00 UTC is 19:00 in Ho Chi Minh City (UTC+7, no DST).
    start, end = day_window(NOON, "Asia/Ho_Chi_Minh")

    assert start == DAY - timedelta(hours=7)
    assert end == DAY + timedelta(hours=17)


@pytest.mark.parametrize(
    ("moment", "hours"),
    [
        (datetime(2026, 3, 8, 12, tzinfo=UTC), 23),  # US clocks go forward
        (datetime(2026, 11, 1, 12, tzinfo=UTC), 25),  # US clocks go back
        (datetime(2026, 6, 1, 12, tzinfo=UTC), 24),
    ],
)
def test_a_local_day_follows_daylight_saving(moment: datetime, hours: int) -> None:
    start, end = day_window(moment, "America/New_York")

    assert end - start == timedelta(hours=hours)
    assert start.utcoffset() == timedelta(0)


def test_the_gate_counts_the_local_day(database: Database) -> None:
    graph = Graph(database, HO_CHI_MINH)
    # 18:00 UTC on 29 Sep is 01:00 on 30 Sep in Ho Chi Minh City: the same
    # local day as NOON (19:00 local), though an earlier UTC day.
    graph.started(DAY - timedelta(hours=6))

    result = graph.evaluate(graph.item(), ContentStatus.GENERATING)

    assert codes(result) == ["daily_limit.production_reached"]


def test_the_gate_ignores_the_previous_local_day(database: Database) -> None:
    graph = Graph(database, HO_CHI_MINH)
    # 16:30 UTC on 29 Sep is 23:30 on 29 Sep local: yesterday there.
    graph.started(DAY - timedelta(hours=7, minutes=30))

    assert graph.evaluate(graph.item(), ContentStatus.GENERATING).is_passed
