"""E-059 Topic Scoring (Prompt Pack v8, prompt #059).

Rules the user approved on 2026-10-02:

- relevance = share of the topic's words found in the niche name and pillars,
  the audience interests and the request queries (missing niche or audience
  named, the rest still used);
- novelty = 1 / (1 + earlier requests of the channel with a near-duplicate
  topic);
- support = supporting sources / kept sources;
- score = 0.5 relevance + 0.3 novelty + 0.2 support, every value rounded to 4
  places and explained in words;
- stored once per request with the strategy version used (migration 0011);
  scoring never changes the strategy.
"""

import dataclasses
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from ai_youtube_agent.bootstrap import build_container
from ai_youtube_agent.content.research_request import ResearchRequestStateError
from ai_youtube_agent.content.source_collector import (
    ResearchRequestNotFoundError,
    SourceCollector,
)
from ai_youtube_agent.content.source_dedup import SourceDeduplicator
from ai_youtube_agent.content.strategy import Audience, Niche, Pillar
from ai_youtube_agent.content.topic import EvidenceField, Topic, TopicEvidence
from ai_youtube_agent.content.topic_extractor import TopicExtractor
from ai_youtube_agent.content.topic_scorer import (
    TopicScorer,
    reference_words,
    score_topics,
)
from ai_youtube_agent.content.topic_scoring import (
    WEIGHTS,
    TopicScore,
    TopicScoring,
    combined_score,
)
from ai_youtube_agent.core.audit import Actor, ActorKind, AuditLog, InMemoryAuditSink
from ai_youtube_agent.core.config import Environment, Settings
from ai_youtube_agent.core.db.database import Database
from ai_youtube_agent.core.db.repositories.channel import (
    ChannelRepository,
    StrategyProfileRepository,
)
from ai_youtube_agent.core.db.repositories.research import TopicScoreRepository
from ai_youtube_agent.providers.mock_research import MockHit, MockResearchProvider
from factories import make_channel, make_strategy_profile

T0 = datetime(2026, 10, 2, 12, 0, tzinfo=UTC)
USER = Actor(ActorKind.USER, "owner")


def topic(label: str, sources: int = 2, rank: int = 1, *more: str) -> Topic:
    return Topic(
        id=f"t-{label}",
        request_id="r1",
        rank=rank,
        label=label,
        keyphrases=(label, *more),
        evidence=tuple(
            TopicEvidence(f"s{n}", EvidenceField.TITLE, "text") for n in range(sources)
        ),
    )


# Signals


def test_weights_sum_to_one() -> None:
    assert WEIGHTS == {"relevance": 0.5, "novelty": 0.3, "support": 0.2}
    assert sum(WEIGHTS.values()) == pytest.approx(1)
    assert combined_score(1, 1, 1) == 1.0
    assert combined_score(0.5, 1, 0.25) == 0.6


def test_reference_words_come_from_strategy_and_queries() -> None:
    strategy = make_strategy_profile(make_channel())

    reference, missing = reference_words(strategy, ["Best ETF for students"])

    assert {"personal", "finance", "budgeting", "investing", "etf", "students"} <= (
        reference
    )
    assert "for" not in reference
    assert missing == ()


def test_missing_niche_and_audience_are_named() -> None:
    strategy = dataclasses.replace(
        make_strategy_profile(make_channel()), niche=None, audience=None
    )

    reference, missing = reference_words(strategy, ["index funds"])

    assert reference == {"index", "funds"}
    assert missing == ("niche", "audience")
    assert reference_words(None, ["q"])[1] == ("niche", "audience")


def test_audience_interests_count() -> None:
    strategy = dataclasses.replace(
        make_strategy_profile(make_channel()),
        audience=Audience("Students", interests=("crypto",)),
    )

    reference, _ = reference_words(strategy, [])

    assert "crypto" in reference


def test_relevance_is_the_share_of_matching_topic_words() -> None:
    [score] = score_topics(
        [topic("index funds", 2, 1, "cheap index funds")],
        reference={"index", "cheap"},
        earlier={},
        kept_sources=4,
    )

    assert score.topic_words == ("cheap", "funds", "index")
    assert score.matched_words == ("cheap", "index")
    assert score.relevance == 0.6667
    assert score.support == 0.5
    assert score.novelty == 1.0
    assert score.score == combined_score(0.6667, 1.0, 0.5)


def test_novelty_falls_with_each_earlier_request_that_had_the_topic() -> None:
    earlier = {
        "old1": ["index funds", "bonds"],
        "old2": ["funds index"],
        "old3": ["crypto"],
    }

    [score] = score_topics(
        [topic("index funds")], reference=set(), earlier=earlier, kept_sources=2
    )

    assert score.seen_in == ("old1", "old2")
    assert score.novelty == round(1 / 3, 4)


def test_scores_are_ordered_highest_first_then_by_rank() -> None:
    topics = [
        topic("alpha", 2, 1),
        topic("beta", 2, 2),
        topic("index funds", 3, 3),
    ]

    scores = score_topics(topics, reference={"index"}, earlier={}, kept_sources=3)

    assert [s.label for s in scores] == ["index funds", "alpha", "beta"]


def test_reasons_explain_every_signal() -> None:
    [score] = score_topics(
        [topic("index funds")], reference={"index"}, earlier={}, kept_sources=4
    )

    assert score.reasons == (
        "relevance 0.5: 1 of 2 topic words match the strategy or the queries (index)",
        "novelty 1.0: seen in 0 earlier research request(s) of this channel",
        "support 0.5: 2 of 4 kept sources support it",
        "score 0.65 = 0.5 x relevance + 0.3 x novelty + 0.2 x support",
    )
    assert score.as_dict()["reasons"] == list(score.reasons)


@pytest.mark.parametrize(
    "changes",
    [
        {"relevance": 1.5},
        {"score": 0.1},
        {"matched_words": ("other",)},
        {"support_count": 5},
    ],
)
def test_score_invariants(changes) -> None:
    [score] = score_topics(
        [topic("index funds")], reference={"index"}, earlier={}, kept_sources=4
    )
    with pytest.raises(ValueError):
        dataclasses.replace(score, **changes)


def test_a_scoring_must_be_ordered_and_name_known_inputs() -> None:
    low, high = score_topics(
        [topic("a"), topic("index", 4, 2)],
        reference={"index"},
        earlier={},
        kept_sources=4,
    )[::-1]

    with pytest.raises(ValueError):
        TopicScoring("r1", (low, high), 1, (), T0)
    with pytest.raises(ValueError):
        TopicScoring("r1", (high, low), 1, ("budget",), T0)


# The service


class World:
    def __init__(self, database: Database) -> None:
        self.database = database
        self.provider = MockResearchProvider(clock=lambda: T0)
        self.clock_now = T0
        self.collector = SourceCollector(
            database,
            self.provider,
            AuditLog(InMemoryAuditSink()),
            clock=self.clock,
            sleep=lambda seconds: None,
        )
        deduplicator = SourceDeduplicator(database)
        self.scorer = TopicScorer(
            database,
            deduplicator,
            TopicExtractor(database, deduplicator),
            clock=lambda: T0,
        )
        self.channel = make_channel()
        self.strategy = make_strategy_profile(self.channel)
        with database.transaction() as connection:
            ChannelRepository(connection).add(self.channel)
            StrategyProfileRepository(connection).add(self.strategy)

    def clock(self) -> datetime:
        self.clock_now += timedelta(seconds=1)
        return self.clock_now

    def finished(self, query: str, titles: list[str]):
        hits = []
        for index, title in enumerate(titles):
            url = f"https://{query.replace(' ', '-')}-{index}.org/"
            self.provider.add_page(url, f"text {title}", title=title)
            hits.append(MockHit(url, title))
        self.provider.add_results(query, hits)
        request = self.collector.request(self.channel.id, [query], actor=USER)
        return self.collector.collect(request.id)


TITLES = [
    "Budgeting apps for students",
    "Best budgeting apps 2026",
    "Crypto wallets explained",
    "Crypto wallets compared",
]


def test_score_stores_signals_with_the_strategy_version(database) -> None:
    world = World(database)
    request = world.finished("money tools", TITLES)

    scoring = world.scorer.score(request.id)

    assert [s.label for s in scoring.scores] == ["budgeting apps", "crypto wallets"]
    budgeting, crypto = scoring.scores
    assert budgeting.matched_words == ("budgeting",)
    assert crypto.matched_words == ()
    assert budgeting.support == 0.5
    assert scoring.strategy_version == world.strategy.version
    assert scoring.missing_inputs == ()
    with database.transaction() as connection:
        assert TopicScoreRepository(connection).get(request.id) == scoring


def test_novelty_uses_only_earlier_requests_of_the_channel(database) -> None:
    world = World(database)
    first = world.finished("money tools", TITLES)
    world.scorer.score(first.id)
    second = world.finished(
        "more tools", TITLES[2:] + ["Index funds guide", "Index funds 101"]
    )

    scoring = world.scorer.score(second.id)

    by_label = {s.label: s for s in scoring.scores}
    assert by_label["crypto wallets"].seen_in == (first.id,)
    assert by_label["crypto wallets"].novelty == 0.5
    assert by_label["index funds"].novelty == 1.0
    assert world.scorer.score(first.id).scores[1].novelty == 1.0


def test_running_again_returns_the_stored_scores(database) -> None:
    world = World(database)
    request = world.finished("money tools", TITLES)
    first = world.scorer.score(request.id)
    changed = world.strategy.update(
        niche=Niche("Crypto", (Pillar("crypto wallets"),)), actor=USER
    )
    with database.transaction() as connection:
        StrategyProfileRepository(connection).update(
            changed, expected_version=world.strategy.version
        )

    assert world.scorer.score(request.id) == first


def test_scoring_never_changes_the_strategy(database) -> None:
    world = World(database)
    request = world.finished("money tools", TITLES)

    world.scorer.score(request.id)

    with database.transaction() as connection:
        stored = StrategyProfileRepository(connection).get_by_channel(world.channel.id)
    assert stored == world.strategy


def test_without_a_strategy_only_queries_count(database) -> None:
    world = World(database)
    channel = make_channel()
    with database.transaction() as connection:
        ChannelRepository(connection).add(channel)
    world.channel = channel
    request = world.finished("crypto", TITLES)

    scoring = world.scorer.score(request.id)

    assert scoring.strategy_version is None
    assert scoring.missing_inputs == ("niche", "audience")
    by_label = {s.label: s for s in scoring.scores}
    assert by_label["crypto wallets"].matched_words == ("crypto",)


def test_only_finished_requests_are_scored(database) -> None:
    world = World(database)
    pending = world.collector.request(world.channel.id, ["q"], actor=USER)

    with pytest.raises(ResearchRequestStateError):
        world.scorer.score(pending.id)
    with pytest.raises(ResearchRequestNotFoundError):
        world.scorer.score("missing")


def test_stored_scores_are_checked_by_the_table(database) -> None:
    world = World(database)
    request = world.finished("money tools", TITLES)
    world.scorer.score(request.id)

    with pytest.raises(sqlite3.IntegrityError), database.transaction() as conn:
        conn.execute("UPDATE topic_scores SET relevance = 2")


def test_bootstrap_registers_the_scorer(tmp_path) -> None:
    container = build_container(
        Settings(environment=Environment.TEST, database_path=tmp_path / "a.db")
    )

    assert isinstance(container.resolve(TopicScorer), TopicScorer)


def test_scores_round_trip_as_dicts() -> None:
    [score] = score_topics(
        [topic("index funds")], reference={"index"}, earlier={}, kept_sources=2
    )

    assert isinstance(score, TopicScore)
    assert score.as_dict()["score"] == score.score
