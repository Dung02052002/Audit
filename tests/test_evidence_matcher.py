"""F-069 Evidence Matcher (Prompt Pack v8, prompt #069).

Rules the user approved on 2026-10-03:

- deterministic rules only, recorded as method ``rules-v1`` on the run;
- the score is containment: the share of the claim's content words (no EN or
  VI stop words) found in the evidence text; a link needs at least 0.5 and
  2 shared words; an entity claim also needs a shared capitalised word;
  numeric claims compare normalised numbers and date claims compare dates,
  recording ``numbers`` = agree, differ or none (a neutral observation);
- the pool is the script version's own research report only; each evidence
  entry of each report claim is compared on its quote and note (the better
  counts); the quote is stored as excerpt when present, otherwise the note;
- at most 3 links per claim, from distinct sources, numbers that agree first,
  then score, then report order;
- on demand only; one run per claim extraction, stored with its links in one
  transaction (migration 0018), also without a report; ``evidence.matched``
  is audited after commit; an unknown script is ``ScriptNotFoundError``
  (404), a version without a claim extraction ``ClaimsNotExtractedError``
  (409) and a missing report ``ResearchReportNotFoundError`` (404).
"""

import dataclasses
import sqlite3
import time
import unicodedata
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from http import HTTPStatus
from pathlib import Path

import pytest

from ai_youtube_agent.bootstrap import build_container
from ai_youtube_agent.content.claim_extraction import ClaimExtraction
from ai_youtube_agent.content.claim_extractor import (
    ClaimExtractor,
    ScriptNotFoundError,
)
from ai_youtube_agent.content.evidence_matcher import (
    ClaimsNotExtractedError,
    EvidenceMatcher,
)
from ai_youtube_agent.content.evidence_matching import (
    MATCH_METHOD,
    MAX_LINKS,
    MIN_SHARED_WORDS,
    THRESHOLD,
    DateMention,
    EvidenceMatch,
    NumberAgreement,
    capitalised_words,
    content_words,
    dates,
    match_claims,
    numbers,
    score_candidate,
)
from ai_youtube_agent.content.hook_generator import ResearchReportNotFoundError
from ai_youtube_agent.content.research_report import (
    ClaimEvidence,
    ReportSource,
    ResearchClaim,
    ResearchReport,
    Uncertainty,
)
from ai_youtube_agent.content.research_request import ResearchRequest
from ai_youtube_agent.content.script import (
    Claim,
    ClaimKind,
    Evidence,
    Script,
    ScriptSection,
    SectionKind,
)
from ai_youtube_agent.core.audit import (
    Actor,
    ActorKind,
    AuditLog,
    AuditResult,
    InMemoryAuditSink,
)
from ai_youtube_agent.core.config import Environment, Settings
from ai_youtube_agent.core.db.codec import format_datetime
from ai_youtube_agent.core.db.database import Database
from ai_youtube_agent.core.db.migrate import default_migrations, migrate
from ai_youtube_agent.core.db.repositories.channel import (
    ChannelRepository,
    StrategyProfileRepository,
)
from ai_youtube_agent.core.db.repositories.content import (
    ClaimExtractionRepository,
    ContentItemRepository,
    EvidenceMatchRepository,
    ScriptRepository,
)
from ai_youtube_agent.core.db.repositories.research import (
    ResearchReportRepository,
    ResearchRequestRepository,
)
from factories import make_channel, make_content_item, make_strategy_profile

T0 = datetime(2026, 10, 3, 12, 0, tzinfo=UTC)
USER = Actor(ActorKind.USER, "owner")
AI = Actor(ActorKind.AI, "mock/mock-1")
AGREE, DIFFER, NONE = (
    NumberAgreement.AGREE,
    NumberAgreement.DIFFER,
    NumberAgreement.NONE,
)

FEES = "Banks took 2 billion dollars in fees last year."
VCB = "Vietcombank raised its transfer fee in 2024."
BODY = f"{FEES} {VCB} Online banks are cheaper than branches. Fees always add up."
FEES_QUOTE = "Banks took 3 billion dollars in fees last year."
VCB_NOTE = "Vietcombank raised the transfer fee in 2024."


def evidence(source_id: str, note: str, quote: str | None = None) -> ClaimEvidence:
    return ClaimEvidence(source_id, note, quote)


def research_claim(claim_id: str, *entries: ClaimEvidence) -> ResearchClaim:
    return ResearchClaim(claim_id, entries[0].note, entries, (), Uncertainty.MEDIUM, ())


def make_report(
    *claims: ResearchClaim, request_id: str = "request", channel_id: str = "channel"
) -> ResearchReport:
    sources = dict.fromkeys(s for c in claims for s in c.source_ids)
    return ResearchReport(
        id=f"report-{request_id}",
        request_id=request_id,
        channel_id=channel_id,
        queries=("bank fees",),
        language="en",
        market="VN",
        request_status="succeeded",
        topics=(),
        claims=claims,
        sources=tuple(
            ReportSource(s, s, f"https://{s}.org/", f"{s}.org", None, T0)
            for s in sources
        ),
        failures=(),
        uncertainty=Uncertainty.MEDIUM,
        uncertainty_reasons=(),
        generated_at=T0,
    )


REPORT_CLAIMS = (
    research_claim(
        "rc-fees",
        evidence("src-a", FEES),
        evidence("src-b", "Banks took 3 billion dollars in fees.", FEES_QUOTE),
    ),
    research_claim("rc-vcb", evidence("src-c", VCB_NOTE)),
)


def claim(text: str, kind: ClaimKind | None) -> Claim:
    return Claim.create("script", text, kind=kind)


def links_of(claims, report) -> list[tuple]:
    return [
        (link.source_id, link.research_claim_id, link.excerpt, link.score, link.numbers)
        for link in match_claims(claims, report).links
    ]


class Clock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> datetime:
        self.now += timedelta(seconds=1)
        return self.now


class World:
    def __init__(self, database: Database) -> None:
        self.database = database
        self.clock = Clock()
        self.channel = make_channel()
        strategy = make_strategy_profile(self.channel)
        self.item = make_content_item(self.channel, strategy)
        with database.transaction() as connection:
            ChannelRepository(connection).add(self.channel)
            StrategyProfileRepository(connection).add(strategy)
            ContentItemRepository(connection).add(self.item)
        self.sink = InMemoryAuditSink()
        self.matcher = EvidenceMatcher(database, AuditLog(self.sink), clock=self.clock)
        self.extractor = ClaimExtractor(
            database, AuditLog(InMemoryAuditSink()), clock=self.clock
        )

    def report(self, *claims: ResearchClaim) -> ResearchReport:
        request = ResearchRequest.create(
            self.channel.id, ["bank fees"], actor=USER, clock=self.clock
        )
        report = make_report(*claims, request_id=request.id, channel_id=self.channel.id)
        with self.database.transaction() as connection:
            ResearchRequestRepository(connection).add(request)
            ResearchReportRepository(connection).add(report)
        return report

    def script(self, text: str = BODY, report: ResearchReport | None = None):
        script = Script.create(
            self.item.id,
            sections=(ScriptSection(SectionKind.BODY, text),),
            created_by=AI,
            research_report_id=report.id if report else None,
            clock=self.clock,
        )
        with self.database.transaction() as connection:
            ScriptRepository(connection).add(script)
        return script

    def extracted(
        self, text: str = BODY, *, with_report: bool = True
    ) -> tuple[Script, ClaimExtraction]:
        report = self.report(*REPORT_CLAIMS) if with_report else None
        script = self.script(text, report)
        return script, self.extractor.extract(script.id, actor=USER)

    def stored(self, extraction_id: str) -> EvidenceMatch | None:
        with self.database.transaction() as connection:
            return EvidenceMatchRepository(connection).get_by_extraction(extraction_id)

    def count(self, table: str) -> int:
        with self.database.transaction() as connection:
            return connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]


@pytest.fixture
def world(database: Database) -> World:
    return World(database)


# Words, numbers and dates


def test_content_words_drop_stop_words_numbers_and_scale_words() -> None:
    assert content_words(FEES) == {"banks", "took", "dollars", "fees", "last", "year"}
    assert content_words("Phí của ngân hàng tăng 5 phần trăm, 2 triệu đồng.") == {
        "phí",
        "ngân",
        "hàng",
        "tăng",
        "đồng",
    }
    assert content_words("Fees rose 5 per cent per year.") == {
        "fees",
        "rose",
        "per",  # only the phrase "per cent" is a number word
        "year",
    }
    assert not content_words("5 thousands, 3 hundreds, 2 trillions")


def test_capitalised_words_follow_the_entity_rule_of_the_extractor() -> None:
    text = "1. Vietcombank raised fees: The Bank said I Think, in the US and G7"

    assert capitalised_words(text) == {"bank", "think", "us", "g7"}


def ranges(*values) -> set:
    """Expected numbers: "5" or ("5", "%") or ("5", "7", "%")."""
    found = set()
    for value in values:
        value = (value, "") if isinstance(value, str) else value
        low, *high, unit = value
        found.add((Decimal(low), Decimal(high[0] if high else low), unit))
    return found


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("1.000 người", ranges("1000")),
        ("1,000 users", ranges("1000")),
        ("1 000 000 users", ranges("1000000")),
        ("1,5 triệu đồng", ranges("1500000")),
        ("1.5 million", ranges("1500000")),
        ("2,5 tỷ", ranges("2500000000")),
        ("5%", ranges(("5", "%"))),
        ("5 %", ranges(("5", "%"))),
        ("5 percent", ranges(("5", "%"))),
        ("5 per cent", ranges(("5", "%"))),
        ("5 phần trăm", ranges(("5", "%"))),
        ("7,5%", ranges(("7.5", "%"))),
        ("2 billion dollars", ranges("2000000000")),
        ("$2,000,000,000", ranges("2000000000")),
        ("1 nghìn tỷ", ranges("1000000000000")),
        ("2 trillions", ranges("2000000000000")),
        ("5 thousands", ranges("5000")),
        ("1,234.5 or 1.234,5", ranges("1234.5")),
        ("12.345.678", ranges("12345678")),
        ("1,000, 2,000 and 3", ranges("1000", "2000", "3")),
        ("5 millionaires", ranges("5")),
        ("Fees rose 7% in 2025 and on 3/10/2026.", ranges(("7", "%"))),
        ("No numbers here.", set()),
        # Abbreviations attached to the digits.
        ("$5bn", ranges("5000000000")),
        ("5 bn", ranges("5")),
        ("5tr đồng", ranges("5000000")),
        ("5tỷ", ranges("5000000000")),
        ("5k views", ranges("5000")),
        ("5mn users", ranges("5000000")),
        ("$5m", ranges("5000000")),
        ("USD 5b", ranges("5000000000")),
        ("5m tall", ranges("5")),  # m and b only after a currency
        # Ranges: the unit and scale apply to both ends.
        ("5-7%", ranges(("5", "7", "%"))),
        ("5–7%", ranges(("5", "7", "%"))),
        ("5 to 7%", ranges(("5", "7", "%"))),
        ("5 đến 7%", ranges(("5", "7", "%"))),
        ("từ 5 đến 7%", ranges(("5", "7", "%"))),
        ("5% to 7", ranges(("5", "7", "%"))),
        ("7 to 5 million", ranges(("5000000", "7000000", ""))),
        ("5% to 7 million", ranges(("5", "%"), "7000000")),
        # Signs and numbers that are not numbers.
        ("-5 degrees", ranges("-5")),
        ("−5 degrees", ranges("-5")),
        ("COVID-19", ranges("19")),
        ("1,234,5", set()),
        ("Python 3.12.10", set()),
        ("version 3.12.2024", set()),
        ("phiên bản 1.2.2024", set()),
        ("20 may apply", ranges("20")),
        ("Mỗi ngày 8 ly nước", ranges("8")),
        ("mỗi tháng 3 lần", ranges("3")),
        ("tháng 13", ranges("13")),
    ],
)
def test_numbers_are_normalised(text, expected) -> None:
    assert numbers(text) == expected


def test_a_percentage_is_not_a_plain_number() -> None:
    claim_numbers, plain = numbers("5%"), numbers("5 people")

    assert claim_numbers != plain
    assert {unit for *_, unit in claim_numbers | plain} == {"%", ""}


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("2026-10-03", {DateMention(2026, 10, 3)}),
        ("13/10/2026", {DateMention(2026, 10, 13)}),
        ("3/10/2026", {DateMention(2026, 10, 3, ambiguous=True)}),
        ("3/3/26", {DateMention(2026, 3, 3)}),
        ("13.10.2026", {DateMention(2026, 10, 13)}),
        ("13.10.26", set()),  # a dotted date needs a 4-digit year
        ("10/31/2026", {DateMention(2026, 10, 31)}),
        ("31/31/2026", set()),
        ("3 October 2026", {DateMention(2026, 10, 3)}),
        ("3rd of October", {DateMention(None, 10, 3)}),
        ("October 3, 2026", {DateMention(2026, 10, 3)}),
        ("October 2026", {DateMention(2026, 10, None)}),
        ("20 May 2024", {DateMention(2024, 5, 20)}),
        ("May 2024", {DateMention(2024, 5, None)}),
        ("20 may apply", set()),
        ("ngày 5", {DateMention(None, None, 5)}),
        ("ngày 5.", {DateMention(None, None, 5)}),
        ("ngày 3 tháng 10 năm 2026", {DateMention(2026, 10, 3)}),
        ("ngày 13/10/2026", {DateMention(2026, 10, 13)}),
        ("tháng 10/2026", {DateMention(2026, 10, None)}),
        ("tháng 3 năm 2020", {DateMention(2020, 3, None)}),
        ("ngày 32", set()),
        ("tháng 13", set()),
        ("Mỗi ngày 8 ly nước", set()),
        ("hằng tháng 3 lần", set()),
        ("in 2024.", {DateMention(2024)}),
        ("the 1990s", {DateMention(1990, decade=True)}),
        ("2000 người", {DateMention(2000)}),  # a year is a date, never a number
        ("Python 3.12.10", set()),
        ("version 3.12.2024", set()),
        ("v3.12.2024", set()),
        ("phiên bản 1.2.2024", set()),
        ("1.2.3.2024", set()),
        ("1,5 triệu", set()),
    ],
)
def test_dates_are_read_in_english_and_vietnamese(text, expected) -> None:
    assert dates(text) == expected


@pytest.mark.parametrize(
    ("first", "second", "agree"),
    [
        (DateMention(2024), DateMention(2024, 10, 3), True),
        (DateMention(2024, 10, 3), DateMention(2024, 5, 5), False),
        (DateMention(None, 10, 3), DateMention(2024, 10, 3), True),
        (DateMention(1990, decade=True), DateMention(1994), True),
        (DateMention(1990, decade=True), DateMention(2004), False),
        (DateMention(2024), DateMention(None, 10, None), False),
        # 03/04/2026 may be 3 April or 4 March.
        (DateMention(2026, 4, 3, ambiguous=True), DateMention(2026, 3, 4), True),
        (DateMention(2026, 4, 3, ambiguous=True), DateMention(2026, 4, 3), True),
        (DateMention(2026, 4, 3, ambiguous=True), DateMention(2026, 3, None), True),
        (DateMention(2026, 4, 3, ambiguous=True), DateMention(2026, 5, 5), False),
        (DateMention(2026, 4, 3, ambiguous=True), DateMention(2025, 4, 3), False),
    ],
)
def test_dates_agree_on_a_shared_part_without_a_conflict(first, second, agree):
    assert first.agrees(second) is agree
    assert second.agrees(first) is agree


def test_decomposed_text_is_compared_in_nfc() -> None:
    text = "Phí chuyển khoản tăng 5% trong năm nay."
    decomposed = claim(unicodedata.normalize("NFD", text), ClaimKind.NUMERIC)

    candidate = score_candidate(decomposed, "Phí chuyển khoản tăng 5 phần trăm.")

    assert (candidate.score, candidate.numbers, candidate.qualifies) == (
        0.667,
        AGREE,
        True,
    )


# Scores


@pytest.mark.parametrize(
    ("text", "score", "shared", "qualifies"),
    [
        ("Online banks charge lower fees than branches.", 0.75, 3, True),
        ("Online banks are popular.", 0.5, 2, True),
        ("Banks are popular.", 0.25, 1, False),
        ("Nothing alike.", 0.0, 0, False),
    ],
)
def test_the_score_is_the_share_of_claim_words_found(text, score, shared, qualifies):
    comparison = claim("Online banks are cheaper than branches.", ClaimKind.COMPARISON)

    candidate = score_candidate(comparison, text)

    assert (candidate.score, candidate.shared_words) == (score, shared)
    assert candidate.qualifies is qualifies
    assert candidate.numbers is NONE
    assert THRESHOLD == 0.5


def test_a_link_needs_two_shared_words() -> None:
    absolute = claim("It never works.", ClaimKind.ABSOLUTE)

    half = score_candidate(absolute, "That never happens.")
    full = score_candidate(absolute, "It never works well.")

    assert (half.score, half.shared_words, half.qualifies) == (0.5, 1, False)
    assert (full.score, full.shared_words, full.qualifies) == (1.0, 2, True)
    assert MIN_SHARED_WORDS == 2


def test_scores_are_rounded_to_three_decimals() -> None:
    absolute = claim("Fees never fall.", ClaimKind.ABSOLUTE)

    assert score_candidate(absolute, "Fees never rise.").score == 0.667


def test_a_claim_without_content_words_never_matches() -> None:
    assert score_candidate(claim("It is.", None), "It is.").qualifies is False


def test_an_entity_claim_needs_a_shared_capitalised_word() -> None:
    entity = claim("Fees at Vietcombank rose sharply.", ClaimKind.ENTITY)
    other_bank = "Fees rose sharply at Techcombank."

    refused = score_candidate(entity, other_bank)
    kept = score_candidate(entity, "Vietcombank fees rose.")
    as_comparison = score_candidate(
        dataclasses.replace(entity, kind=ClaimKind.COMPARISON), other_bank
    )

    assert (refused.score, refused.qualifies) == (0.75, False)
    assert (kept.score, kept.qualifies) == (0.75, True)
    assert as_comparison.qualifies is True


@pytest.mark.parametrize(
    ("text", "kind", "evidence_text", "agreement"),
    [
        (FEES, ClaimKind.NUMERIC, "Banks took 2,000,000,000 dollars in fees.", AGREE),
        (FEES, ClaimKind.NUMERIC, FEES_QUOTE, DIFFER),
        (FEES, ClaimKind.NUMERIC, "Banks took dollars in fees last year.", NONE),
        ("Fees cost millions.", ClaimKind.NUMERIC, "Fees cost 5 million.", NONE),
        (VCB, ClaimKind.DATE, "Vietcombank raised the fee on 1 March 2024.", AGREE),
        (VCB, ClaimKind.DATE, "Vietcombank raised the transfer fee in 2023.", DIFFER),
        (VCB, ClaimKind.DATE, "Vietcombank raised the transfer fee.", NONE),
        (FEES, ClaimKind.ABSOLUTE, FEES_QUOTE, NONE),
    ],
)
def test_numbers_and_dates_are_compared_by_claim_kind(
    text, kind, evidence_text, agreement
) -> None:
    candidate = score_candidate(claim(text, kind), evidence_text)

    assert candidate.numbers is agreement
    assert candidate.qualifies is True  # differing numbers do not stop a link


@pytest.mark.parametrize(
    ("text", "evidence_text"),
    [
        (
            "Sales rose sharply in the US market.",
            "Sales rose sharply in the US market.",
        ),
        (
            "The vaccine was approved by the WHO panel.",
            "The WHO panel approved the vaccine.",
        ),
        ("Leaders met at the G7 summit in Japan.", "The G7 summit brought leaders."),
        ("Cases of H5N1 rose in Asia.", "H5N1 cases rose fast."),
    ],
)
def test_entity_capitals_may_be_stop_words_or_hold_digits(text, evidence_text):
    candidate = score_candidate(claim(text, ClaimKind.ENTITY), evidence_text)

    assert candidate.qualifies is True


@pytest.mark.parametrize(
    ("claim_text", "agreement"),
    [
        ("Fees rose 5% last year.", AGREE),  # an end
        ("Fees rose 6% last year.", AGREE),  # inside
        ("Fees rose 7% last year.", AGREE),  # the other end
        ("Fees rose 8% last year.", DIFFER),
        ("Fees rose 6 points last year.", DIFFER),  # not a percentage
        ("Fees rose 6-9% last year.", AGREE),  # overlapping ranges
    ],
)
def test_a_number_inside_a_range_agrees(claim_text, agreement) -> None:
    for evidence_text in (
        "Fees rose 5-7% last year.",
        "Fees rose from 5 to 7% last year.",
        "Phí tăng từ 5 đến 7% năm ngoái, fees rose last year.",
    ):
        candidate = score_candidate(claim(claim_text, ClaimKind.NUMERIC), evidence_text)
        assert candidate.numbers is agreement, evidence_text


def test_a_negative_number_differs_from_a_positive_one() -> None:
    numeric = claim("The lake froze at -5 degrees.", ClaimKind.NUMERIC)

    candidate = score_candidate(numeric, "The lake froze at 5 degrees.")

    assert (candidate.qualifies, candidate.numbers) == (True, DIFFER)
    assert score_candidate(numeric, "The lake froze at −5 degrees.").numbers is AGREE


def test_abbreviated_scales_match_written_ones() -> None:
    numeric = claim("Banks earned $5bn in fees last year.", ClaimKind.NUMERIC)

    candidate = score_candidate(numeric, "Banks earned 5 billion dollars in fees.")

    assert candidate.numbers is AGREE


@pytest.mark.parametrize(
    ("evidence_text", "agreement"),
    [
        ("The fee changed on March 4, 2026.", AGREE),
        ("The fee changed on April 3, 2026.", AGREE),
        ("The fee changed on May 5, 2026.", DIFFER),
    ],
)
def test_an_ambiguous_numeric_date_never_differs_by_its_order(
    evidence_text, agreement
) -> None:
    dated = claim("The fee changed on 03/04/2026.", ClaimKind.DATE)

    assert score_candidate(dated, evidence_text).numbers is agreement


def test_an_unambiguous_numeric_date_keeps_its_reading() -> None:
    dated = claim("The fee changed on 13/04/2026.", ClaimKind.DATE)

    assert score_candidate(dated, "The fee changed on April 13, 2026.").numbers is AGREE
    assert score_candidate(dated, "The fee changed on April 14, 2026.").numbers is (
        DIFFER
    )


def test_a_daily_amount_in_vietnamese_is_a_number_not_a_date() -> None:
    numeric = claim("Uống 8 ly nước mỗi ngày.", ClaimKind.NUMERIC)

    candidate = score_candidate(numeric, "Mỗi ngày 8 ly nước, tức 2 lít.")

    assert (candidate.qualifies, candidate.numbers) == (True, AGREE)


def test_may_the_verb_is_not_a_month() -> None:
    numeric = claim("About 20 fees may apply to transfers.", ClaimKind.NUMERIC)

    candidate = score_candidate(numeric, "Up to 20 fees may apply to transfers.")

    assert candidate.numbers is AGREE


# Matching


def test_three_links_from_distinct_sources_numbers_that_agree_first() -> None:
    numeric = claim(FEES, ClaimKind.NUMERIC)
    report = make_report(
        research_claim(
            "rc1", evidence("src-c", "Banks took dollars in fees last year.")
        ),
        research_claim(
            "rc2", evidence("src-b", FEES_QUOTE), evidence("src-d", "Banks took fees.")
        ),
        research_claim("rc3", evidence("src-a", "Banks took 2,000,000,000 dollars.")),
        research_claim("rc4", evidence("src-a", f"{FEES} A report says so.")),
    )

    result = match_claims([numeric], report)

    assert links_of([numeric], report) == [
        ("src-a", "rc4", f"{FEES} A report says so.", 1.0, AGREE),
        ("src-c", "rc1", "Banks took dollars in fees last year.", 1.0, NONE),
        ("src-b", "rc2", FEES_QUOTE, 1.0, DIFFER),
    ]
    assert (result.claims, result.matched) == (1, 1)
    assert MAX_LINKS == 3


def test_quote_and_note_are_compared_and_the_quote_is_stored() -> None:
    numeric = claim(FEES, ClaimKind.NUMERIC)
    report = make_report(
        research_claim(
            "rc1",
            evidence("src-q", "Banks took 2 billion dollars.", FEES_QUOTE),
            evidence("src-n", FEES),
            evidence("src-u", FEES, "Nothing to see here."),
        )
    )

    assert links_of([numeric], report) == [
        ("src-n", "rc1", FEES, 1.0, AGREE),
        # Only the note matched; the quote is still the excerpt.
        ("src-u", "rc1", "Nothing to see here.", 1.0, AGREE),
        # The note agrees, so it beats the higher scoring quote.
        ("src-q", "rc1", FEES_QUOTE, 0.5, AGREE),
    ]


def test_without_a_report_or_report_claims_nothing_matches() -> None:
    claims = [claim(FEES, ClaimKind.NUMERIC), claim(VCB, ClaimKind.DATE)]

    for report in (None, make_report()):
        result = match_claims(claims, report)
        assert (result.links, result.claims, result.matched) == ((), 2, 0)


def test_matching_stays_fast_at_the_claim_cap() -> None:
    claims = [
        claim(f"Bank {n} charged fee {n} of {n * 7}% in region {n}.", ClaimKind.NUMERIC)
        for n in range(200)
    ]
    report = make_report(
        *(
            research_claim(
                f"rc{n}",
                *(
                    evidence(
                        f"src-{n}-{k}",
                        f"Region {n} bank charged a fee of {n * 7 + k}%.",
                        f"The bank in region {n} charged fee number {n} at {k}%.",
                    )
                    for k in range(3)
                ),
            )
            for n in range(200)
        )
    )

    started = time.perf_counter()
    result = match_claims(claims, report)
    elapsed = time.perf_counter() - started

    assert result.matched == 200
    assert elapsed < 5.0


def test_reading_numbers_and_dates_is_linear() -> None:
    text = (
        "1.1,1 2/2/ 3 October 1 000 -5-7% từ 5 đến 6tr mỗi ngày 8 v1.2.2024 "
    ) * 2_000

    started = time.perf_counter()
    _ = numbers(text), dates(text), content_words(text)
    elapsed = time.perf_counter() - started

    assert elapsed < 2.0


# The matcher


def test_claims_are_matched_stored_and_audited(world: World) -> None:
    script, extraction = world.extracted()
    fees, vcb, *_ = extraction.claims

    match = world.matcher.match(script.id, actor=USER)

    assert match.extraction_id == extraction.id
    assert (match.script_id, match.content_item_id) == (script.id, world.item.id)
    assert match.research_report_id == script.research_report_id
    assert match.method == MATCH_METHOD == "rules-v1"
    assert match.requested_by == USER
    assert [
        (
            link.claim_id,
            link.source_ref,
            link.research_claim_id,
            link.excerpt,
            link.score,
            link.numbers,
        )
        for link in match.links
    ] == [
        (fees.id, "src-a", "rc-fees", FEES, 1.0, AGREE),
        (fees.id, "src-b", "rc-fees", FEES_QUOTE, 1.0, DIFFER),
        (vcb.id, "src-c", "rc-vcb", VCB_NOTE, 1.0, AGREE),
    ]
    for link in match.links:
        assert link.match_id == match.id
        assert link.created_at == match.created_at
    assert match.counts() == {
        "claims": 4,
        "matched": 2,
        "unmatched": 2,
        "links": 3,
        "numbers_differ": 1,
        "no_report": False,
    }

    assert world.stored(extraction.id) == match
    with world.database.transaction() as connection:
        assert EvidenceMatchRepository(connection).get(match.id) == match
        listed = ScriptRepository(connection).list_evidence(fees.id)
    assert listed == list(match.links[:2])

    [event] = world.sink.events()
    assert (event.action, event.result, event.actor) == (
        "evidence.matched",
        AuditResult.SUCCESS,
        USER,
    )
    assert (event.entity.type, event.entity.id) == ("script", script.id)
    assert dict(event.metadata) == {
        "evidence_match_id": match.id,
        "claim_extraction_id": extraction.id,
        "content_item_id": world.item.id,
        "version": 1,
        "research_report_id": script.research_report_id,
        "method": "rules-v1",
        **match.counts(),
    }


def test_the_audit_event_is_recorded_after_the_commit(world: World) -> None:
    script, extraction = world.extracted()
    seen: list[EvidenceMatch | None] = []

    class CheckingSink(InMemoryAuditSink):
        def append(self, event) -> None:
            # A fresh connection only sees committed data.
            seen.append(world.stored(extraction.id))
            super().append(event)

    match = EvidenceMatcher(world.database, AuditLog(CheckingSink())).match(
        script.id, actor=USER
    )

    assert seen == [match]


def test_a_second_call_returns_the_stored_run(world: World) -> None:
    script, _ = world.extracted()
    first = world.matcher.match(script.id, actor=USER)
    rows = world.count("evidence")

    again = world.matcher.match(script.id, actor=AI)

    assert again == first
    assert world.count("evidence") == rows
    assert world.count("evidence_matches") == 1
    assert [e.action for e in world.sink.events()] == ["evidence.matched"]


def test_a_script_without_a_report_gets_a_run_without_links(world: World) -> None:
    script, extraction = world.extracted(with_report=False)

    match = world.matcher.match(script.id, actor=USER)

    assert (match.research_report_id, match.links, match.no_report) == (None, (), True)
    assert match.counts() == {
        "claims": 4,
        "matched": 0,
        "unmatched": 4,
        "links": 0,
        "numbers_differ": 0,
        "no_report": True,
    }
    assert world.stored(extraction.id) == match
    [event] = world.sink.events()
    assert (event.metadata["research_report_id"], event.metadata["no_report"]) == (
        None,
        True,
    )


def test_only_the_claims_of_the_extraction_run_are_matched(world: World) -> None:
    script, extraction = world.extracted()
    manual = script.claim(FEES, clock=world.clock)
    with world.database.transaction() as connection:
        ScriptRepository(connection).add_claim(manual)

    match = world.matcher.match(script.id, actor=USER)

    assert match.claims_count == len(extraction.claims) == 4
    assert {link.claim_id for link in match.links} <= {c.id for c in extraction.claims}
    with world.database.transaction() as connection:
        assert ScriptRepository(connection).list_evidence(manual.id) == []


def test_an_unknown_script_is_not_found(world: World) -> None:
    with pytest.raises(ScriptNotFoundError) as caught:
        world.matcher.match("missing", actor=USER)

    assert caught.value.code == "domain.script_not_found"
    assert caught.value.to_public().http_status == HTTPStatus.NOT_FOUND
    assert world.sink.events() == ()


def test_a_version_without_extracted_claims_is_a_conflict(world: World) -> None:
    script = world.script()

    with pytest.raises(ClaimsNotExtractedError) as caught:
        world.matcher.match(script.id, actor=USER)

    assert caught.value.code == "domain.claims_not_extracted"
    assert caught.value.to_public().http_status == HTTPStatus.CONFLICT
    assert world.count("evidence_matches") == 0
    assert world.sink.events() == ()


def test_a_missing_report_is_not_found(world: World, monkeypatch) -> None:
    script, extraction = world.extracted()
    monkeypatch.setattr(ResearchReportRepository, "get", lambda self, _: None)

    with pytest.raises(ResearchReportNotFoundError) as caught:
        world.matcher.match(script.id, actor=USER)

    assert caught.value.code == "domain.research_report_not_found"
    assert caught.value.to_public().http_status == HTTPStatus.NOT_FOUND
    assert world.stored(extraction.id) is None
    assert world.sink.events() == ()


def test_a_concurrent_run_stored_first_is_returned(world, monkeypatch) -> None:
    script, _ = world.extracted()
    winner = world.matcher.match(script.id, actor=USER)
    rows = world.count("evidence")
    real = EvidenceMatchRepository.get_by_extraction
    calls: list[str] = []

    def racing(self, extraction_id):
        # The first read happens before the other run is committed.
        calls.append(extraction_id)
        return None if len(calls) == 1 else real(self, extraction_id)

    monkeypatch.setattr(EvidenceMatchRepository, "get_by_extraction", racing)

    loser = world.matcher.match(script.id, actor=AI)

    assert loser == winner
    assert len(calls) == 2
    assert world.count("evidence") == rows  # the losing insert was rolled back
    assert [e.action for e in world.sink.events()] == ["evidence.matched"]


def test_a_failed_link_insert_rolls_back_the_run(world, monkeypatch) -> None:
    script, extraction = world.extracted()
    real = ScriptRepository.add_evidence
    added: list[str] = []

    def failing(self, link):
        if added:  # the run row and one link are already written
            raise RuntimeError("disk full")
        added.append(link.id)
        real(self, link)

    monkeypatch.setattr(ScriptRepository, "add_evidence", failing)

    with pytest.raises(RuntimeError):
        world.matcher.match(script.id, actor=USER)
    assert world.stored(extraction.id) is None
    assert (world.count("evidence"), world.count("evidence_matches")) == (0, 0)
    assert world.sink.events() == ()


def test_other_integrity_errors_are_raised(world, monkeypatch) -> None:
    script, extraction = world.extracted()

    def broken(self, match):
        raise sqlite3.IntegrityError("broken")

    monkeypatch.setattr(EvidenceMatchRepository, "add", broken)

    with pytest.raises(sqlite3.IntegrityError):
        world.matcher.match(script.id, actor=USER)
    assert world.stored(extraction.id) is None
    assert world.sink.events() == ()


# Storage


def test_the_database_keeps_one_run_per_extraction(world: World) -> None:
    script, _ = world.extracted()
    match = world.matcher.match(script.id, actor=USER)
    copy = dataclasses.replace(
        match, id="other", links=(), matched=0, unmatched=4, numbers_differ=0
    )

    with pytest.raises(sqlite3.IntegrityError), world.database.transaction() as c:
        EvidenceMatchRepository(c).add(copy)


@pytest.mark.parametrize(
    ("change", "error"),
    [
        (None, None),
        ("same source", "UNIQUE"),  # the same claim and source in one run
        (dict(score=1.5), "CHECK"),
        (dict(score=-0.5), "CHECK"),
        (dict(numbers="contradicts"), "CHECK"),
        (dict(research_claim_id=" "), "CHECK"),
        (dict(match_id="missing"), "FOREIGN KEY"),
    ],
)
def test_the_database_checks_matched_evidence(world: World, change, error) -> None:
    script, _ = world.extracted()
    match = world.matcher.match(script.id, actor=USER)
    link = match.links[0]
    row = {
        "id": "new",
        "claim_id": link.claim_id,
        "source_ref": "src-new",
        "excerpt": link.excerpt,
        "created_at": format_datetime(link.created_at),
        "match_id": link.match_id,
        "research_claim_id": link.research_claim_id,
        "score": link.score,
        "numbers": link.numbers.value,
    }
    row.update(
        {"source_ref": link.source_ref} if change == "same source" else change or {}
    )
    sql = (
        f"INSERT INTO evidence ({', '.join(row)}) "
        f"VALUES ({', '.join('?' for _ in row)})"
    )

    if error is None:
        with world.database.transaction() as connection:
            connection.execute(sql, tuple(row.values()))
        return
    with (
        pytest.raises(sqlite3.IntegrityError, match=error),
        world.database.transaction() as connection,
    ):
        connection.execute(sql, tuple(row.values()))


def test_evidence_without_a_match_still_loads(world: World) -> None:
    script, extraction = world.extracted()
    plain = Evidence.create(
        extraction.claims[0].id, "src-x", "A passage.", clock=world.clock
    )

    with world.database.transaction() as connection:
        ScriptRepository(connection).add_evidence(plain)
        assert ScriptRepository(connection).list_evidence(plain.claim_id) == [plain]
    assert (plain.match_id, plain.research_claim_id, plain.score, plain.numbers) == (
        None,
        None,
        None,
        None,
    )


def test_migration_0018_keeps_evidence_stored_before_it(tmp_path: Path) -> None:
    path = tmp_path / "app.db"
    migrate(path, migrations=default_migrations()[:17])
    database = Database(path)
    channel = make_channel()
    strategy = make_strategy_profile(channel)
    item = make_content_item(channel, strategy)
    script = Script.create(item.id, FEES, clock=lambda: T0)
    with database.transaction() as connection:
        ChannelRepository(connection).add(channel)
        StrategyProfileRepository(connection).add(strategy)
        ContentItemRepository(connection).add(item)
        ScriptRepository(connection).add(script)
        connection.execute(
            "INSERT INTO claims (id, script_id, text, created_at) VALUES (?, ?, ?, ?)",
            ("c1", script.id, FEES, format_datetime(T0)),
        )
        connection.execute(
            "INSERT INTO evidence VALUES ('e1', 'c1', 'src-1', 'A passage.', ?)",
            (format_datetime(T0),),
        )

    migrate(path)

    with database.transaction() as connection:
        assert ScriptRepository(connection).list_evidence("c1") == [
            Evidence("e1", "c1", "src-1", "A passage.", T0)
        ]
    audit = AuditLog(InMemoryAuditSink())
    ClaimExtractor(database, audit).extract(script.id, actor=USER)
    match = EvidenceMatcher(database, audit).match(script.id, actor=USER)
    assert (match.no_report, match.claims_count, match.unmatched) == (True, 1, 1)


# The run and its links


def test_runs_reject_invalid_state(world: World) -> None:
    script, _ = world.extracted()
    match = world.matcher.match(script.id, actor=USER)
    link = match.links[0]
    four = tuple(
        dataclasses.replace(link, id=f"e{n}", source_ref=f"s{n}") for n in range(4)
    )
    for bad in (
        dict(method=" "),
        dict(method="x" * 101),
        dict(extraction_id=""),
        dict(research_report_id=" "),
        dict(research_report_id=None),  # links without a report
        dict(matched=-1),
        dict(unmatched=match.unmatched + 1),
        dict(claims_count=201, unmatched=201 - match.matched),
        dict(matched=1, unmatched=3),
        dict(numbers_differ=0),
        dict(links=(link, link)),
        dict(links=four),
        dict(links=(dataclasses.replace(link, match_id="other"),)),
        dict(links=(dataclasses.replace(link, score=0.4),)),
        dict(links=(dataclasses.replace(link, numbers=None),)),
        dict(links=(dataclasses.replace(link, research_claim_id=None),)),
        dict(created_at=datetime(2026, 10, 3)),
    ):
        with pytest.raises(ValueError):
            dataclasses.replace(match, **bad)
    with pytest.raises(TypeError):
        dataclasses.replace(match, requested_by="owner")
    with pytest.raises(TypeError):
        dataclasses.replace(match, links=("link",))


@pytest.mark.parametrize(
    "bad",
    [
        dict(score=-0.1),
        dict(score=1.5),
        dict(score=float("nan")),
        dict(score=True),
        dict(match_id=" "),
        dict(research_claim_id=""),
    ],
)
def test_evidence_rejects_invalid_match_fields(bad) -> None:
    with pytest.raises(ValueError):
        Evidence.create("claim", "source", **bad)


def test_evidence_match_fields_are_optional_and_typed() -> None:
    kept = Evidence.create(
        "claim", "source", match_id="m", research_claim_id="rc", score=0, numbers=NONE
    )

    assert (kept.score, kept.numbers) == (0, NONE)
    assert kept.as_dict()["numbers"] == "none"
    with pytest.raises(TypeError):
        Evidence.create("claim", "source", numbers="agree")  # type: ignore[arg-type]


def test_bootstrap_registers_the_matcher(tmp_path: Path) -> None:
    container = build_container(
        Settings(environment=Environment.TEST, database_path=tmp_path / "a.db")
    )
    assert isinstance(container.resolve(EvidenceMatcher), EvidenceMatcher)


def test_claim_extraction_is_unchanged_by_matching(world: World) -> None:
    script, extraction = world.extracted()

    world.matcher.match(script.id, actor=USER)

    with world.database.transaction() as connection:
        assert ClaimExtractionRepository(connection).get_by_script(script.id) == (
            extraction
        )
