"""E-058 Topic Extractor (Prompt Pack v8, prompt #058).

Rules the user approved on 2026-10-02:

- deterministic keyword statistics, no AI model: phrases of 1 to 3 words from
  the titles, evidence notes and quotes of the sources kept by #057, not
  starting or ending with an English or Vietnamese stop word, not only digits;
- a topic needs at least 2 sources; a shorter phrase with the same sources as
  a longer phrase containing it is dropped; topics are ranked by sources, then
  length, then alphabetically; near-duplicate labels (word Jaccard >= 0.8)
  merge into the stronger topic;
- a topic has a label, key phrases and one evidence item (source, field, text)
  per supporting source; at most 20 per request, stored once (migration 0010).
"""

import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from ai_youtube_agent.bootstrap import build_container
from ai_youtube_agent.content.research_request import ResearchRequestStateError
from ai_youtube_agent.content.source import EvidenceNote, Source
from ai_youtube_agent.content.source_collector import (
    ResearchRequestNotFoundError,
    SourceCollector,
)
from ai_youtube_agent.content.source_dedup import SourceDeduplicator
from ai_youtube_agent.content.topic import (
    MAX_TOPICS,
    EvidenceField,
    Topic,
    TopicEvidence,
    TopicExtraction,
)
from ai_youtube_agent.content.topic_extractor import (
    TopicExtractor,
    extract_topics,
    phrases,
)
from ai_youtube_agent.core.audit import Actor, ActorKind, AuditLog, InMemoryAuditSink
from ai_youtube_agent.core.config import Environment, Settings
from ai_youtube_agent.core.db.database import Database
from ai_youtube_agent.core.db.repositories.channel import ChannelRepository
from ai_youtube_agent.core.db.repositories.research import TopicRepository
from ai_youtube_agent.providers.mock_research import MockHit, MockResearchProvider
from factories import make_channel

T0 = datetime(2026, 10, 2, 12, 0, tzinfo=UTC)
USER = Actor(ActorKind.USER, "owner")


def src(url: str, title: str, *notes: EvidenceNote) -> Source:
    return Source.create(url, title, "mock", T0, evidence_notes=notes)


def labels(topics) -> list[str]:
    return [topic.label for topic in topics]


# Phrases


def test_phrases_are_1_to_3_words_without_edge_stop_words() -> None:
    found = phrases("The cost of index funds in 2026")

    assert {"cost", "index", "funds", "index funds", "cost of index"} <= found
    assert "the cost" not in found  # starts with a stop word
    assert "funds in" not in found  # ends with a stop word
    assert "2026" not in found  # only digits
    assert "funds in 2026" in found  # only all-digit phrases are dropped
    assert "of index funds" not in found


def test_vietnamese_phrases_use_vietnamese_stop_words() -> None:
    found = phrases("Đầu tư quỹ chỉ số cho người mới")

    assert {"quỹ chỉ số", "đầu tư", "mới"} <= found
    assert "số cho" not in found
    assert "người" not in found


# Extraction


def test_a_phrase_needs_two_sources() -> None:
    topics = extract_topics(
        "r1",
        [
            src("https://a.org/", "Index funds for beginners"),
            src("https://b.org/", "Why index funds win"),
            src("https://c.org/", "Emergency savings"),
        ],
    )

    assert labels(topics) == ["index funds"]
    assert topics[0].support_count == 2


def test_a_shorter_phrase_with_the_same_sources_is_dropped() -> None:
    topics = extract_topics(
        "r1",
        [
            src("https://a.org/", "Low cost index funds"),
            src("https://b.org/", "Low cost index funds explained"),
            src("https://c.org/", "Index funds or bonds"),
        ],
    )

    # "low cost" and "cost index" have the same two sources as the longer
    # "low cost index" and "cost index funds", so only the longer ones stay.
    assert labels(topics) == ["index funds", "cost index funds", "low cost index"]
    assert topics[0].support_count == 3


def test_ranking_is_sources_then_length_then_alphabet() -> None:
    topics = extract_topics(
        "r1",
        [
            src("https://a.org/", "Budget apps and emergency fund basics"),
            src("https://b.org/", "Budget apps compared"),
            src("https://c.org/", "Emergency fund size"),
            src("https://d.org/", "Budget apps for students"),
        ],
    )

    assert labels(topics) == ["budget apps", "emergency fund"]
    assert [t.rank for t in topics] == [1, 2]


def test_near_duplicate_labels_merge_into_the_stronger_topic() -> None:
    # Labels have at most 3 words, so a word Jaccard of 0.8 or more means the
    # same words in another order.
    topics = extract_topics(
        "r1",
        [
            src("https://a.org/", "Index fund basics"),
            src("https://b.org/", "Index fund tips"),
            src("https://c.org/", "Fund index review"),
            src("https://d.org/", "Fund index ranking"),
        ],
    )

    [merged] = [t for t in topics if t.label == "fund index"]
    assert merged.keyphrases == ("fund index", "index fund")
    assert merged.support_count == 4
    assert "index fund" not in labels(topics)


def test_phrases_with_more_sources_rank_first() -> None:
    topics = extract_topics(
        "r1",
        [
            src("https://a.org/", "Passive index investing"),
            src("https://b.org/", "Passive index investing guide"),
            src("https://c.org/", "Index investing for students"),
        ],
    )

    assert labels(topics)[:2] == ["index investing", "passive index investing"]


def test_evidence_points_to_the_field_that_holds_the_phrase() -> None:
    a = src(
        "https://a.org/",
        "Saving money",
        EvidenceNote("Low fees matter", "Index funds have low fees."),
    )
    b = src("https://b.org/", "Low fees explained")

    [topic] = extract_topics("r1", [a, b])

    assert topic.label == "low fees"
    assert topic.evidence == (
        TopicEvidence(a.id, EvidenceField.NOTE, "Low fees matter"),
        TopicEvidence(b.id, EvidenceField.TITLE, "Low fees explained"),
    )


def test_quotes_count_as_evidence() -> None:
    a = src(
        "https://a.org/", "Article", EvidenceNote("A note", "Dollar cost averaging")
    )
    b = src("https://b.org/", "Dollar cost averaging in practice")

    [topic] = extract_topics("r1", [a, b])

    assert topic.evidence[0].field is EvidenceField.QUOTE


def test_at_most_twenty_topics() -> None:
    sources = [
        src(f"https://s{n}.org/", " ".join(f"topic{k}x" for k in range(30)))
        for n in range(2)
    ]

    topics = extract_topics("r1", sources)

    assert len(topics) == MAX_TOPICS


def test_no_sources_give_no_topics() -> None:
    assert extract_topics("r1", []) == ()


def test_extraction_is_deterministic_apart_from_ids() -> None:
    sources = [
        src("https://a.org/", "Index funds and bonds"),
        src("https://b.org/", "Bonds or index funds"),
    ]

    first, second = extract_topics("r1", sources), extract_topics("r1", sources)

    assert [(t.label, t.keyphrases, t.evidence) for t in first] == [
        (t.label, t.keyphrases, t.evidence) for t in second
    ]


# The entity


def evidence(*ids: str) -> tuple[TopicEvidence, ...]:
    return tuple(TopicEvidence(i, EvidenceField.TITLE, "text") for i in ids)


@pytest.mark.parametrize(
    "changes",
    [
        {"rank": 0},
        {"rank": 21},
        {"label": "Upper"},
        {"label": "x" * 101},
        {"keyphrases": ("other",)},
        {"keyphrases": ("label", "label")},
        {"evidence": evidence("s1")},
        {"evidence": evidence("s1", "s1")},
    ],
)
def test_topic_limits(changes) -> None:
    values = dict(
        id="t1",
        request_id="r1",
        rank=1,
        label="label",
        keyphrases=("label",),
        evidence=evidence("s1", "s2"),
    )
    with pytest.raises(ValueError):
        Topic(**{**values, **changes})


def test_evidence_limits() -> None:
    with pytest.raises(ValueError):
        TopicEvidence("s1", EvidenceField.NOTE, " ")
    with pytest.raises(ValueError):
        TopicEvidence("s1", EvidenceField.NOTE, "x" * 1001)
    with pytest.raises(TypeError):
        TopicEvidence("s1", "note", "x")  # type: ignore[arg-type]


def test_an_extraction_must_be_ranked_in_order() -> None:
    topic = Topic("t1", "r1", 2, "label", ("label",), evidence("s1", "s2"))

    with pytest.raises(ValueError):
        TopicExtraction("r1", (topic,), T0)


# The service


class World:
    def __init__(self, database: Database) -> None:
        self.database = database
        self.provider = MockResearchProvider(clock=lambda: T0)
        self.collector = SourceCollector(
            database,
            self.provider,
            AuditLog(InMemoryAuditSink()),
            sleep=lambda seconds: None,
        )
        self.extractor = TopicExtractor(
            database, SourceDeduplicator(database), clock=lambda: T0
        )
        self.channel = make_channel()
        with database.transaction() as connection:
            ChannelRepository(connection).add(self.channel)

    def finished(self, pages: list[tuple[str, str, str]]):
        hits = []
        for url, title, text in pages:
            self.provider.add_page(url, text, title=title)
            hits.append(MockHit(url, title))
        self.provider.add_results("q", hits)
        request = self.collector.request(self.channel.id, ["q"], actor=USER)
        return self.collector.collect(request.id)


def test_extract_uses_only_kept_sources_and_stores(database: Database) -> None:
    world = World(database)
    body = " ".join(f"w{n}" for n in range(300))
    request = world.finished(
        [
            ("https://a.org/", "Index funds guide", body),
            ("https://mirror.org/", "Index funds copy", body),  # duplicate of a
            ("https://b.org/", "Bonds and index funds", "other text entirely here"),
        ]
    )
    ids = [c.source_id for c in request.collected]

    extraction = world.extractor.extract(request.id)

    [topic] = extraction.topics
    assert topic.label == "index funds"
    assert topic.source_ids == (ids[0], ids[2])
    assert extraction.extracted_at == T0
    with database.transaction() as connection:
        assert TopicRepository(connection).get(request.id) == extraction


def test_running_again_returns_the_stored_topics(database: Database) -> None:
    world = World(database)
    request = world.finished(
        [
            ("https://a.org/", "Index funds guide", "alpha beta gamma"),
            ("https://b.org/", "Index funds explained", "delta epsilon zeta"),
        ]
    )
    first = world.extractor.extract(request.id)
    later = TopicExtractor(
        database,
        SourceDeduplicator(database),
        clock=lambda: T0 + timedelta(days=1),
    )

    assert later.extract(request.id) == first


def test_a_request_with_no_topics_stores_an_empty_result(database: Database) -> None:
    world = World(database)
    request = world.finished([("https://a.org/", "Lonely page", "text")])

    assert world.extractor.extract(request.id).topics == ()
    with database.transaction() as connection:
        assert TopicRepository(connection).get(request.id).topics == ()


def test_only_finished_requests_are_extracted(database: Database) -> None:
    world = World(database)
    pending = world.collector.request(world.channel.id, ["q"], actor=USER)

    with pytest.raises(ResearchRequestStateError):
        world.extractor.extract(pending.id)
    with pytest.raises(ResearchRequestNotFoundError):
        world.extractor.extract("missing")


def test_stored_topics_are_checked_by_the_table(database: Database) -> None:
    world = World(database)
    request = world.finished(
        [
            ("https://a.org/", "Index funds guide", "alpha beta"),
            ("https://b.org/", "Index funds explained", "gamma delta"),
        ]
    )
    world.extractor.extract(request.id)

    with pytest.raises(sqlite3.IntegrityError), database.transaction() as conn:
        conn.execute("UPDATE topic_evidence SET field = 'summary'")


def test_bootstrap_registers_the_extractor(tmp_path) -> None:
    container = build_container(
        Settings(environment=Environment.TEST, database_path=tmp_path / "a.db")
    )

    assert isinstance(container.resolve(TopicExtractor), TopicExtractor)
