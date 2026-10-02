"""E-060 Research Report (Prompt Pack v8, prompt #060).

Rules the user approved on 2026-10-03:

- claims are the evidence notes of kept sources (the verbatim quote if any,
  else the note), near-duplicate claims across sources merged (word Jaccard
  >= 0.8), each linked to the topics whose key phrases it contains; no new
  sentences are written;
- claim uncertainty: low with >= 3 hosts and a quote, medium with >= 2 sources
  or a quote, high otherwise; the report adds an overall level with reasons
  (partial/failed request, < 3 sources, sources > 365 days old, no claims);
- the report is JSON stored once per request with a schema version
  (migration 0012); ``to_markdown`` renders it on demand; research claims are
  separate from B-017 claims.
"""

import json
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from ai_youtube_agent.bootstrap import build_container
from ai_youtube_agent.content.report_generator import (
    ResearchReportGenerator,
    build_claims,
    report_uncertainty,
)
from ai_youtube_agent.content.research_report import (
    REPORT_SCHEMA_VERSION,
    ClaimEvidence,
    ResearchClaim,
    ResearchReport,
    Uncertainty,
    claim_id,
)
from ai_youtube_agent.content.research_request import (
    CollectedSource,
    CollectionFailure,
    CollectionOperation,
    ResearchRequest,
    ResearchRequestStateError,
)
from ai_youtube_agent.content.source import EvidenceNote, Source
from ai_youtube_agent.content.source_collector import ResearchRequestNotFoundError
from ai_youtube_agent.content.source_dedup import SourceDeduplicator
from ai_youtube_agent.content.topic import EvidenceField, Topic, TopicEvidence
from ai_youtube_agent.content.topic_extractor import TopicExtractor
from ai_youtube_agent.content.topic_scorer import TopicScorer
from ai_youtube_agent.core.audit import Actor, ActorKind
from ai_youtube_agent.core.config import Environment, Settings
from ai_youtube_agent.core.db.database import Database
from ai_youtube_agent.core.db.repositories.channel import (
    ChannelRepository,
    StrategyProfileRepository,
)
from ai_youtube_agent.core.db.repositories.research import (
    ResearchReportRepository,
    ResearchRequestRepository,
    SourceRepository,
)
from factories import make_channel, make_strategy_profile

T0 = datetime(2026, 10, 3, 12, 0, tzinfo=UTC)
USER = Actor(ActorKind.USER, "owner")
QUOTE = "Index funds have low fees."


def src(host: str, *notes: EvidenceNote, title: str = "Page", days_old=None) -> Source:
    return Source.create(
        f"https://{host}/page",
        title,
        "mock",
        T0,
        published_at=T0 - timedelta(days=days_old) if days_old is not None else None,
        evidence_notes=notes,
    )


def topic(label: str, *source_ids: str) -> Topic:
    return Topic(
        f"t-{label}",
        "r1",
        1,
        label,
        (label,),
        tuple(TopicEvidence(i, EvidenceField.TITLE, "t") for i in source_ids),
    )


# Claims


def test_a_claim_is_the_quote_else_the_note() -> None:
    a = src("a.org", EvidenceNote("Fees matter", QUOTE), EvidenceNote("Only a note"))

    claims = build_claims("r1", [a], [])

    assert [c.text for c in claims] == [QUOTE, "Only a note"]
    assert claims[0].evidence == (ClaimEvidence(a.id, "Fees matter", QUOTE),)
    assert claims[0].id == claim_id("r1", QUOTE)


def test_near_duplicate_claims_merge_across_sources() -> None:
    a = src("a.org", EvidenceNote("n", QUOTE))
    b = src("b.org", EvidenceNote("Index funds have low fees!"))
    c = src("c.org", EvidenceNote("Bonds are safer"))

    claims = build_claims("r1", [a, b, c], [])

    assert [c.text for c in claims] == [QUOTE, "Bonds are safer"]
    assert claims[0].source_ids == (a.id, b.id)


def test_one_source_counts_once_per_claim() -> None:
    a = src(
        "a.org", EvidenceNote("n", QUOTE), EvidenceNote("Index funds have low fees")
    )

    [claim] = build_claims("r1", [a], [])

    assert claim.source_ids == (a.id,)


def test_claims_link_the_topics_they_mention() -> None:
    a = src("a.org", EvidenceNote("About costs", QUOTE))
    topics = [topic("low fees", "x", "y"), topic("bonds", "x", "y")]

    [claim] = build_claims("r1", [a], topics)

    assert claim.topic_ids == ("t-low fees",)


@pytest.mark.parametrize(
    ("hosts", "quoted", "level"),
    [
        (["a.org", "b.org", "c.org"], True, Uncertainty.LOW),
        (["a.org", "b.org", "c.org"], False, Uncertainty.MEDIUM),
        (["a.org", "a.org:8080", "b.org"], True, Uncertainty.LOW),
        (["a.org", "b.org"], True, Uncertainty.MEDIUM),
        (["a.org"], True, Uncertainty.MEDIUM),
        (["a.org"], False, Uncertainty.HIGH),
    ],
)
def test_claim_uncertainty(hosts, quoted, level) -> None:
    sources = [
        Source.create(
            f"https://{host}/{n}",
            "Page",
            "mock",
            T0,
            evidence_notes=[
                EvidenceNote("Index funds have low fees", QUOTE if quoted else None)
            ],
        )
        for n, host in enumerate(hosts)
    ]

    [claim] = build_claims("r1", sources, [])

    assert claim.uncertainty is level
    assert claim.reasons[0].startswith(f"{len(hosts)} source(s)")


def test_a_claim_needs_unique_evidence() -> None:
    evidence = ClaimEvidence("s1", "n")
    with pytest.raises(ValueError):
        ResearchClaim("c", "text", (), (), Uncertainty.HIGH, ())
    with pytest.raises(ValueError):
        ResearchClaim("c", "text", (evidence, evidence), (), Uncertainty.HIGH, ())


# Report uncertainty


def finished(*, failures=(), sources=()) -> ResearchRequest:
    request = ResearchRequest.create("c1", ["q"], actor=USER).start()
    return request.finish(
        [CollectedSource(s.id, "q", n + 1) for n, s in enumerate(sources)], failures
    )


FAIL = CollectionFailure(CollectionOperation.FETCH, "https://x.org/", "e.x", 1)


def test_a_good_report_has_low_uncertainty() -> None:
    sources = [
        src(f"s{n}.org", EvidenceNote("n", QUOTE), days_old=10) for n in range(3)
    ]
    claims = build_claims("r1", sources, [])

    assert report_uncertainty(finished(sources=sources), sources, claims, T0) == (
        Uncertainty.LOW,
        (),
    )


def test_report_uncertainty_reasons() -> None:
    sources = [src("a.org", EvidenceNote("n", QUOTE), days_old=400)]
    claims = build_claims("r1", sources, [])

    level, reasons = report_uncertainty(
        finished(failures=[FAIL], sources=sources), sources, claims, T0
    )

    assert level is Uncertainty.MEDIUM
    assert reasons == (
        "1 search or fetch step(s) failed",
        "only 1 source(s) kept, fewer than 3",
        "1 source(s) older than 365 days",
    )


def test_no_claims_or_a_failed_request_is_high() -> None:
    sources = [src(f"s{n}.org") for n in range(3)]

    level, reasons = report_uncertainty(finished(sources=sources), sources, (), T0)
    assert level is Uncertainty.HIGH
    assert reasons == ("no claims: the sources have no evidence notes",)

    level, reasons = report_uncertainty(finished(failures=[FAIL]), [], (), T0)
    assert level is Uncertainty.HIGH
    assert reasons[0] == "the research request failed"


# The generator


class World:
    def __init__(self, database: Database) -> None:
        self.database = database
        deduplicator = SourceDeduplicator(database, clock=lambda: T0)
        extractor = TopicExtractor(database, deduplicator, clock=lambda: T0)
        self.generator = ResearchReportGenerator(
            database,
            deduplicator,
            TopicScorer(database, deduplicator, extractor, clock=lambda: T0),
            clock=lambda: T0,
        )
        self.channel = make_channel()
        with database.transaction() as connection:
            ChannelRepository(connection).add(self.channel)
            StrategyProfileRepository(connection).add(
                make_strategy_profile(self.channel)
            )

    def request(self, sources, *, failures=(), finish=True) -> ResearchRequest:
        request = ResearchRequest.create(
            self.channel.id, ["index funds"], language="vi", market="VN", actor=USER
        )
        with self.database.transaction() as connection:
            for source in sources:
                SourceRepository(connection).add(source)
            ResearchRequestRepository(connection).add(request)
        if not finish:
            return request
        running = request.start()
        done = running.finish(
            [
                CollectedSource(s.id, "index funds", n + 1)
                for n, s in enumerate(sources)
            ],
            failures,
        )
        with self.database.transaction() as connection:
            repository = ResearchRequestRepository(connection)
            repository.update(running, expected_updated_at=request.updated_at)
            repository.update(done, expected_updated_at=running.updated_at)
        return done


SOURCES = [
    src(
        "a.org",
        EvidenceNote("Costs", QUOTE),
        title="Index funds and low fees",
        days_old=20,
    ),
    src(
        "b.org",
        EvidenceNote("Same point", "Index funds have very low fees."),
        title="Why index funds win",
        days_old=40,
    ),
    src(
        "c.org",
        EvidenceNote("Risk", "Bonds lower portfolio risk."),
        title="Index funds versus bonds",
        days_old=900,
    ),
]


def test_generate_builds_and_stores_the_report(database: Database) -> None:
    world = World(database)
    request = world.request(SOURCES)

    report = world.generator.generate(request.id)

    assert report.request_id == request.id
    assert report.queries == ("index funds",)
    assert (report.language, report.market) == ("vi", "VN")
    assert report.request_status == "completed"
    assert [t.label for t in report.topics] == ["index funds", "low fees"]
    assert report.topics[0].source_ids == tuple(s.id for s in SOURCES)
    assert [c.text for c in report.claims] == [QUOTE, "Bonds lower portfolio risk."]
    assert report.claims[0].source_ids == (SOURCES[0].id, SOURCES[1].id)
    assert report.claims[0].topic_ids == tuple(t.topic_id for t in report.topics)
    assert report.claims[0].uncertainty is Uncertainty.MEDIUM
    assert report.uncertainty is Uncertainty.MEDIUM
    assert report.uncertainty_reasons == ("1 source(s) older than 365 days",)
    assert [s.host for s in report.sources] == ["a.org", "b.org", "c.org"]
    with database.transaction() as connection:
        repository = ResearchReportRepository(connection)
        assert repository.get_by_request(request.id) == report
        assert repository.get(report.id) == report


def test_generate_returns_the_stored_report(database: Database) -> None:
    world = World(database)
    request = world.request(SOURCES)
    first = world.generator.generate(request.id)

    assert world.generator.generate(request.id) == first


def test_failures_are_reported(database: Database) -> None:
    world = World(database)
    request = world.request(SOURCES[:1], failures=[FAIL])

    report = world.generator.generate(request.id)

    assert report.request_status == "partial"
    assert report.failures[0].code == "e.x"
    assert report.uncertainty is Uncertainty.MEDIUM


def test_only_finished_requests_get_a_report(database: Database) -> None:
    world = World(database)
    pending = world.request([], finish=False)

    with pytest.raises(ResearchRequestStateError):
        world.generator.generate(pending.id)
    with pytest.raises(ResearchRequestNotFoundError):
        world.generator.generate("missing")


def test_the_stored_json_carries_the_schema_version(database: Database) -> None:
    world = World(database)
    report = world.generator.generate(world.request(SOURCES).id)

    with database.transaction() as connection:
        row = connection.execute(
            "SELECT schema_version, uncertainty, report_json FROM research_reports"
        ).fetchone()

    assert row[0] == REPORT_SCHEMA_VERSION
    assert row[1] == "medium"
    assert json.loads(row[2]) == report.as_dict()
    with pytest.raises(sqlite3.IntegrityError), database.transaction() as conn:
        conn.execute("UPDATE research_reports SET uncertainty = 'unknown'")


def test_from_dict_round_trips_and_checks_the_version(database: Database) -> None:
    world = World(database)
    report = world.generator.generate(world.request(SOURCES).id)

    assert ResearchReport.from_dict(report.as_dict()) == report
    with pytest.raises(ValueError):
        ResearchReport.from_dict({**report.as_dict(), "schema_version": 99})


def test_markdown_lists_everything(database: Database) -> None:
    world = World(database)
    report = world.generator.generate(world.request(SOURCES).id)

    text = report.to_markdown()

    assert text.startswith("# Research report\n")
    assert "## Uncertainty: medium" in text
    assert "- 1 source(s) older than 365 days" in text
    assert "| index funds |" in text
    assert f'- "{QUOTE}" [1] [2] (uncertainty: medium' in text
    assert "1. Index funds and low fees - https://a.org/page, published" in text
    assert "## Failures" not in text


def test_markdown_of_an_empty_report(database: Database) -> None:
    world = World(database)
    report = world.generator.generate(world.request([]).id)

    text = report.to_markdown()

    assert report.uncertainty is Uncertainty.HIGH
    assert "No topics." in text and "No claims." in text and "No sources." in text


def test_bootstrap_registers_the_generator(tmp_path) -> None:
    container = build_container(
        Settings(environment=Environment.TEST, database_path=tmp_path / "a.db")
    )

    assert isinstance(
        container.resolve(ResearchReportGenerator), ResearchReportGenerator
    )
