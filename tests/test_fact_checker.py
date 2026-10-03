"""F-070 Fact Check Result (Prompt Pack v8, prompt #070).

Rules the user approved on 2026-10-03:

- deterministic rules only, recorded as method ``rules-v1`` on the run; per
  claim, the first matching row of the table in ``content/fact_check.py``
  wins; the uncertainty of a claim is the lowest of the research claims its
  links matched, read in the research report of the matching run;
- a claim without a link and a run without a report are WARN, never FAIL;
  differing numbers are FAIL only when no link agrees;
- a record only: no gate, immutable, no override;
- one run per evidence matching run, stored with its results in one
  transaction (migration 0019); a script-level status derived as the worst
  result; ``facts.checked`` is audited after commit; an unknown script is
  ``ScriptNotFoundError`` (404), a version without claims
  ``ClaimsNotExtractedError`` (409), without a matching run
  ``EvidenceNotMatchedError`` (409) and a missing report
  ``ResearchReportNotFoundError`` (404).
"""

import dataclasses
import sqlite3
from datetime import UTC, datetime, timedelta
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
from ai_youtube_agent.content.evidence_matching import EvidenceMatch
from ai_youtube_agent.content.fact_check import (
    FACT_CHECK_METHOD,
    STRONG_SCORE,
    ClaimCheck,
    FactCheck,
    FactCheckCode,
    FactCheckStatus,
    check_claims,
)
from ai_youtube_agent.content.fact_checker import (
    EvidenceNotMatchedError,
    FactChecker,
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
    NumberAgreement,
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
    FactCheckRepository,
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
PASS, WARN, FAIL = FactCheckStatus.PASS, FactCheckStatus.WARN, FactCheckStatus.FAIL
CODE = FactCheckCode
NUM, DATE, ENT, COMP, ABS = (
    ClaimKind.NUMERIC,
    ClaimKind.DATE,
    ClaimKind.ENTITY,
    ClaimKind.COMPARISON,
    ClaimKind.ABSOLUTE,
)
LOW, MEDIUM, HIGH = Uncertainty.LOW, Uncertainty.MEDIUM, Uncertainty.HIGH

FEES = "Banks took 2 billion dollars in fees last year."
VCB = "Vietcombank raised its transfer fee in 2024."
BODY = f"{FEES} {VCB} Online banks are cheaper than branches. Fees always add up."
FEES_QUOTE = "Banks took 3 billion dollars in fees last year."
VCB_NOTE = "Vietcombank raised the transfer fee in 2024."


# The rules, on hand-made links


def research_claim(
    claim_id: str, uncertainty: Uncertainty, *entries: ClaimEvidence
) -> ResearchClaim:
    return ResearchClaim(claim_id, entries[0].note, entries, (), uncertainty, ())


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


def rules_report() -> ResearchReport:
    return make_report(
        *(
            research_claim(f"rc-{u.value}", u, ClaimEvidence(f"src-{u.value}", "note"))
            for u in Uncertainty
        ),
        request_id="rules",
    )


def make_match(
    claims: list[Claim], links: list[Evidence], report_id: str | None
) -> EvidenceMatch:
    matched = len({link.claim_id for link in links})
    return EvidenceMatch(
        id="match",
        extraction_id="extraction",
        script_id="script",
        content_item_id="item",
        research_report_id=report_id,
        method="rules-v1",
        links=tuple(links),
        claims_count=len(claims),
        matched=matched,
        unmatched=len(claims) - matched,
        numbers_differ=sum(link.numbers is DIFFER for link in links),
        requested_by=USER,
        created_at=T0,
    )


def run_rules(
    kind: ClaimKind | None,
    specs: list[tuple[Uncertainty, float, NumberAgreement]],
    *,
    report: ResearchReport | None = None,
) -> ClaimCheck:
    """The result of one claim with one link per ``(uncertainty, score, numbers)``."""
    claim = Claim.create("script", "A claim.", kind=kind)
    links = [
        Evidence.create(
            claim.id,
            f"s{n}",
            "excerpt",
            match_id="match",
            research_claim_id=f"rc-{uncertainty.value}",
            score=score,
            numbers=numbers,
        )
        for n, (uncertainty, score, numbers) in enumerate(specs)
    ]
    report = report or rules_report()
    match = make_match([claim], links, report.id)
    [result] = check_claims([claim], match, report)
    assert result.claim_id == claim.id
    return result


# (id, kind, links as (uncertainty, score, numbers), code, numbers on the result)
CASES = [
    # 3: numeric/date, a differ link and no agree link
    ("differ", NUM, [(LOW, 0.9, DIFFER)], CODE.NUMBERS_DIFFER, DIFFER),
    (
        "date differ and none",
        DATE,
        [(LOW, 0.9, DIFFER), (LOW, 0.9, NONE)],
        CODE.NUMBERS_DIFFER,
        DIFFER,
    ),
    (
        "differ beats a high uncertainty",
        NUM,
        [(HIGH, 0.9, DIFFER)],
        CODE.NUMBERS_DIFFER,
        DIFFER,
    ),
    (
        "differ beats a low score",
        DATE,
        [(LOW, 0.5, DIFFER)],
        CODE.NUMBERS_DIFFER,
        DIFFER,
    ),
    # 4: an agree link and a differ link both exist
    (
        "numeric conflict",
        NUM,
        [(LOW, 0.9, AGREE), (LOW, 0.9, DIFFER)],
        CODE.SOURCES_CONFLICT,
        AGREE,
    ),
    (
        "date conflict, differ first",
        DATE,
        [(LOW, 0.9, DIFFER), (MEDIUM, 0.8, AGREE)],
        CODE.SOURCES_CONFLICT,
        AGREE,
    ),
    (
        "conflict beats a high uncertainty",
        NUM,
        [(HIGH, 0.9, AGREE), (HIGH, 0.9, DIFFER)],
        CODE.SOURCES_CONFLICT,
        AGREE,
    ),
    (
        "conflict also for another kind",
        ENT,
        [(LOW, 0.9, AGREE), (LOW, 0.9, DIFFER)],
        CODE.SOURCES_CONFLICT,
        AGREE,
    ),
    # 5: numeric/date and no agree link, all none
    ("numeric none", NUM, [(LOW, 0.9, NONE)], CODE.NUMBER_UNVERIFIED, NONE),
    (
        "date none twice",
        DATE,
        [(LOW, 0.9, NONE), (MEDIUM, 0.9, NONE)],
        CODE.NUMBER_UNVERIFIED,
        NONE,
    ),
    (
        "unverified beats a high uncertainty",
        NUM,
        [(HIGH, 0.9, NONE)],
        CODE.NUMBER_UNVERIFIED,
        NONE,
    ),
    # 6: all linked research claims have high uncertainty
    ("entity, high", ENT, [(HIGH, 0.9, NONE)], CODE.WEAK_EVIDENCE, NONE),
    ("numeric agree, high", NUM, [(HIGH, 0.9, AGREE)], CODE.WEAK_EVIDENCE, AGREE),
    (
        "two high links",
        COMP,
        [(HIGH, 0.9, NONE), (HIGH, 0.9, NONE)],
        CODE.WEAK_EVIDENCE,
        NONE,
    ),
    (
        "weak beats the absolute rule",
        ABS,
        [(HIGH, 0.9, NONE)],
        CODE.WEAK_EVIDENCE,
        NONE,
    ),
    (
        "one high link among others is not weak",
        ENT,
        [(HIGH, 0.9, NONE), (MEDIUM, 0.9, NONE)],
        CODE.SUPPORTED,
        NONE,
    ),
    # 7: absolute, exactly 1 link, uncertainty not low
    (
        "absolute, one medium link",
        ABS,
        [(MEDIUM, 0.9, NONE)],
        CODE.ABSOLUTE_NEEDS_SUPPORT,
        NONE,
    ),
    (
        "absolute rule beats a low score",
        ABS,
        [(MEDIUM, 0.6, NONE)],
        CODE.ABSOLUTE_NEEDS_SUPPORT,
        NONE,
    ),
    ("absolute, one low link", ABS, [(LOW, 0.9, NONE)], CODE.SUPPORTED, NONE),
    (
        "absolute, two links",
        ABS,
        [(MEDIUM, 0.9, NONE), (MEDIUM, 0.9, NONE)],
        CODE.SUPPORTED,
        NONE,
    ),
    # 8: exactly 1 link, best score under 0.75, numbers not agree
    ("entity, low score", ENT, [(MEDIUM, 0.6, NONE)], CODE.LOW_SCORE, NONE),
    ("absolute, low link, low score", ABS, [(LOW, 0.6, NONE)], CODE.LOW_SCORE, NONE),
    (
        "score at the strong score",
        ENT,
        [(MEDIUM, STRONG_SCORE, NONE)],
        CODE.SUPPORTED,
        NONE,
    ),
    (
        "two links with low scores",
        ENT,
        [(MEDIUM, 0.6, NONE), (MEDIUM, 0.5, NONE)],
        CODE.SUPPORTED,
        NONE,
    ),
    ("agree with a low score", NUM, [(MEDIUM, 0.6, AGREE)], CODE.SUPPORTED, AGREE),
    ("date agree with a low score", DATE, [(LOW, 0.5, AGREE)], CODE.SUPPORTED, AGREE),
    # a claim without a kind is not numeric
    ("no kind, differ", None, [(LOW, 0.9, DIFFER)], CODE.SUPPORTED, DIFFER),
    ("no kind, none", None, [(LOW, 0.9, NONE)], CODE.SUPPORTED, NONE),
    ("no kind, low score", None, [(LOW, 0.6, NONE)], CODE.LOW_SCORE, NONE),
    ("entity, differ only", ENT, [(LOW, 0.9, DIFFER)], CODE.SUPPORTED, DIFFER),
    # 9: otherwise
    ("entity", ENT, [(LOW, 0.9, NONE)], CODE.SUPPORTED, NONE),
    ("numeric agree", NUM, [(LOW, 1.0, AGREE)], CODE.SUPPORTED, AGREE),
    (
        "comparison, three links",
        COMP,
        [(MEDIUM, 0.5, NONE), (LOW, 0.5, NONE), (MEDIUM, 0.5, NONE)],
        CODE.SUPPORTED,
        NONE,
    ),
]
STATUS_OF = {CODE.NUMBERS_DIFFER: FAIL, CODE.SUPPORTED: PASS}


@pytest.mark.parametrize(
    ("kind", "specs", "code", "numbers"),
    [pytest.param(*case[1:], id=case[0]) for case in CASES],
)
def test_the_rules_pick_the_first_matching_row(kind, specs, code, numbers) -> None:
    result = run_rules(kind, specs)

    assert (result.status, result.code) == (STATUS_OF.get(code, WARN), code)
    assert result.links == len(specs)
    assert result.best_score == max(score for _, score, _ in specs)
    assert result.numbers is numbers
    order = [LOW, MEDIUM, HIGH]
    assert result.uncertainty is min((u for u, _, _ in specs), key=order.index)


def test_every_code_has_a_row_in_the_table() -> None:
    covered = {case[3] for case in CASES} | {CODE.NO_REPORT, CODE.UNMATCHED}

    assert covered == set(FactCheckCode)
    assert STRONG_SCORE == 0.75
    assert FACT_CHECK_METHOD == "rules-v1"


def test_a_claim_without_a_link_is_a_warn() -> None:
    claims = [
        Claim.create("script", "A claim.", kind=kind) for kind in (NUM, ENT, None)
    ]
    report = rules_report()
    match = make_match(claims, [], report.id)

    results = check_claims(claims, match, report)

    assert results == tuple(
        ClaimCheck(claim.id, WARN, CODE.UNMATCHED) for claim in claims
    )
    assert all(r.best_score is r.numbers is r.uncertainty is None for r in results)


def test_a_run_without_a_report_is_a_warn_for_every_claim() -> None:
    claims = [Claim.create("script", "A claim.", kind=kind) for kind in (NUM, ENT)]
    match = make_match(claims, [], None)

    results = check_claims(claims, match, None)

    assert results == tuple(
        ClaimCheck(claim.id, WARN, CODE.NO_REPORT) for claim in claims
    )


def test_the_lowest_uncertainty_of_the_links_is_kept() -> None:
    result = run_rules(ENT, [(HIGH, 0.9, NONE), (LOW, 0.9, NONE), (MEDIUM, 0.9, NONE)])

    assert (result.uncertainty, result.code) == (LOW, CODE.SUPPORTED)


def test_a_link_to_a_research_claim_missing_from_the_report_is_an_error() -> None:
    claim = Claim.create("script", "A claim.", kind=ENT)
    link = Evidence.create(
        claim.id,
        "s",
        "excerpt",
        match_id="match",
        research_claim_id="rc-gone",
        score=0.9,
        numbers=NONE,
    )
    report = rules_report()

    with pytest.raises(ValueError, match="rc-gone"):
        check_claims([claim], make_match([claim], [link], report.id), report)


def test_a_run_with_a_report_needs_that_report() -> None:
    claim = Claim.create("script", "A claim.", kind=ENT)
    report = rules_report()
    match = make_match([claim], [], report.id)

    with pytest.raises(ValueError):
        check_claims([claim], match, None)
    with pytest.raises(ValueError):
        check_claims([claim], match, make_report(request_id="other"))


def test_results_follow_the_order_of_the_claims() -> None:
    first, second = (Claim.create("script", text, kind=ENT) for text in ("A.", "B."))
    report = rules_report()
    link = Evidence.create(
        second.id,
        "s",
        "excerpt",
        match_id="match",
        research_claim_id="rc-low",
        score=0.9,
        numbers=NONE,
    )
    match = make_match([first, second], [link], report.id)

    results = check_claims([first, second], match, report)

    assert [(r.claim_id, r.code) for r in results] == [
        (first.id, CODE.UNMATCHED),
        (second.id, CODE.SUPPORTED),
    ]


# The service


def evidence(source_id: str, note: str, quote: str | None = None) -> ClaimEvidence:
    return ClaimEvidence(source_id, note, quote)


REPORT_CLAIMS = (
    research_claim(
        "rc-fees",
        MEDIUM,
        evidence("src-a", FEES),
        evidence("src-b", "Banks took 3 billion dollars in fees.", FEES_QUOTE),
    ),
    research_claim("rc-vcb", MEDIUM, evidence("src-c", VCB_NOTE)),
)
DIFFER_CLAIMS = (research_claim("rc-fees", LOW, evidence("src-b", FEES_QUOTE)),)


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
        self.checker = FactChecker(database, AuditLog(self.sink), clock=self.clock)
        self.extractor = ClaimExtractor(
            database, AuditLog(InMemoryAuditSink()), clock=self.clock
        )
        self.matcher = EvidenceMatcher(
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
        self,
        text: str = BODY,
        *,
        claims: tuple[ResearchClaim, ...] = REPORT_CLAIMS,
        with_report: bool = True,
    ) -> tuple[Script, ClaimExtraction]:
        report = self.report(*claims) if with_report else None
        script = self.script(text, report)
        return script, self.extractor.extract(script.id, actor=USER)

    def matched(self, text: str = BODY, **kwargs):
        script, extraction = self.extracted(text, **kwargs)
        match = self.matcher.match(script.id, actor=USER)
        return script, extraction, match

    def stored(self, match_id: str) -> FactCheck | None:
        with self.database.transaction() as connection:
            return FactCheckRepository(connection).get_by_match(match_id)

    def count(self, table: str) -> int:
        with self.database.transaction() as connection:
            return connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]


@pytest.fixture
def world(database: Database) -> World:
    return World(database)


def test_claims_are_checked_stored_and_audited(world: World) -> None:
    script, extraction, match = world.matched()
    fees, vcb, comparison, absolute = extraction.claims

    check = world.checker.check(script.id, actor=USER)

    assert check.match_id == match.id
    assert check.extraction_id == extraction.id
    assert (check.script_id, check.content_item_id) == (script.id, world.item.id)
    assert check.research_report_id == script.research_report_id
    assert check.method == FACT_CHECK_METHOD == "rules-v1"
    assert check.requested_by == USER
    assert check.results == (
        ClaimCheck(fees.id, WARN, CODE.SOURCES_CONFLICT, 2, 1.0, AGREE, MEDIUM),
        ClaimCheck(vcb.id, PASS, CODE.SUPPORTED, 1, 1.0, AGREE, MEDIUM),
        ClaimCheck(comparison.id, WARN, CODE.UNMATCHED),
        ClaimCheck(absolute.id, WARN, CODE.UNMATCHED),
    )
    assert (check.pass_count, check.warn_count, check.fail_count) == (1, 3, 0)
    assert check.status is WARN
    assert check.counts() == {
        "claims": 4,
        "pass": 1,
        "warn": 3,
        "fail": 0,
        "status": "warn",
        "no_report": False,
    }

    assert world.stored(match.id) == check
    with world.database.transaction() as connection:
        assert FactCheckRepository(connection).get(check.id) == check

    [event] = world.sink.events()
    assert (event.action, event.result, event.actor) == (
        "facts.checked",
        AuditResult.SUCCESS,
        USER,
    )
    assert (event.entity.type, event.entity.id) == ("script", script.id)
    assert dict(event.metadata) == {
        "fact_check_id": check.id,
        "evidence_match_id": match.id,
        "claim_extraction_id": extraction.id,
        "content_item_id": world.item.id,
        "version": 1,
        "research_report_id": script.research_report_id,
        "method": "rules-v1",
        **check.counts(),
    }
    assert all(
        isinstance(value, str | int | bool | None) for value in event.metadata.values()
    )


def test_a_failing_claim_makes_the_run_fail(world: World) -> None:
    script, extraction, _ = world.matched(FEES, claims=DIFFER_CLAIMS)

    check = world.checker.check(script.id, actor=USER)

    assert check.results == (
        ClaimCheck(
            extraction.claims[0].id, FAIL, CODE.NUMBERS_DIFFER, 1, 1.0, DIFFER, LOW
        ),
    )
    assert (check.pass_count, check.warn_count, check.fail_count) == (0, 0, 1)
    assert check.status is FAIL


def test_the_run_status_is_the_worst_result(world: World) -> None:
    script, _, _ = world.matched()
    check = world.checker.check(script.id, actor=USER)
    passed = ClaimCheck("c1", PASS, CODE.SUPPORTED, 1, 1.0, NONE, LOW)
    warned = ClaimCheck("c2", WARN, CODE.UNMATCHED)
    failed = ClaimCheck("c3", FAIL, CODE.NUMBERS_DIFFER, 1, 1.0, DIFFER, LOW)

    def status_of(*results: ClaimCheck) -> FactCheckStatus:
        counts = {s: sum(r.status is s for r in results) for s in FactCheckStatus}
        return dataclasses.replace(
            check,
            results=results,
            claims_count=len(results),
            pass_count=counts[PASS],
            warn_count=counts[WARN],
            fail_count=counts[FAIL],
        ).status

    assert status_of(passed) is PASS
    assert status_of(passed, warned) is WARN
    assert status_of(passed, warned, failed) is FAIL
    assert status_of(passed, failed) is FAIL


def test_a_script_without_claims_passes(world: World) -> None:
    script, extraction, match = world.matched("Hello there, my friend.")

    check = world.checker.check(script.id, actor=USER)

    assert extraction.claims_count == 0
    assert (check.results, check.claims_count, check.status) == ((), 0, PASS)
    assert (check.pass_count, check.warn_count, check.fail_count) == (0, 0, 0)
    assert world.stored(match.id) == check


def test_a_script_without_a_report_warns_for_every_claim(world: World) -> None:
    script, extraction, match = world.matched(with_report=False)

    check = world.checker.check(script.id, actor=USER)

    assert (check.research_report_id, check.no_report) == (None, True)
    assert check.results == tuple(
        ClaimCheck(claim.id, WARN, CODE.NO_REPORT) for claim in extraction.claims
    )
    assert (check.pass_count, check.warn_count, check.fail_count) == (0, 4, 0)
    assert check.status is WARN
    assert world.stored(match.id) == check
    [event] = world.sink.events()
    assert (event.metadata["research_report_id"], event.metadata["no_report"]) == (
        None,
        True,
    )


def test_the_audit_event_is_recorded_after_the_commit(world: World) -> None:
    script, _, match = world.matched()
    seen: list[FactCheck | None] = []

    class CheckingSink(InMemoryAuditSink):
        def append(self, event) -> None:
            # A fresh connection only sees committed data.
            seen.append(world.stored(match.id))
            super().append(event)

    check = FactChecker(world.database, AuditLog(CheckingSink())).check(
        script.id, actor=USER
    )

    assert seen == [check]


def test_a_second_call_returns_the_stored_run(world: World, monkeypatch) -> None:
    script, _, _ = world.matched()
    first = world.checker.check(script.id, actor=USER)
    rows = world.count("fact_check_results")

    def not_read(self, report_id):
        raise AssertionError("the report is not read again")

    monkeypatch.setattr(ResearchReportRepository, "get", not_read)

    again = world.checker.check(script.id, actor=AI)

    assert again == first
    assert world.count("fact_check_results") == rows
    assert world.count("fact_checks") == 1
    assert [e.action for e in world.sink.events()] == ["facts.checked"]


def test_only_the_claims_of_the_extraction_run_are_checked(world: World) -> None:
    script, extraction, _ = world.matched()
    manual = script.claim(FEES, clock=world.clock)
    with world.database.transaction() as connection:
        ScriptRepository(connection).add_claim(manual)

    check = world.checker.check(script.id, actor=USER)

    assert check.claims_count == len(extraction.claims) == 4
    assert {r.claim_id for r in check.results} == {c.id for c in extraction.claims}


def test_an_unknown_script_is_not_found(world: World) -> None:
    with pytest.raises(ScriptNotFoundError) as caught:
        world.checker.check("missing", actor=USER)

    assert caught.value.code == "domain.script_not_found"
    assert caught.value.to_public().http_status == HTTPStatus.NOT_FOUND
    assert world.sink.events() == ()


def test_a_version_without_extracted_claims_is_a_conflict(world: World) -> None:
    script = world.script()

    with pytest.raises(ClaimsNotExtractedError) as caught:
        world.checker.check(script.id, actor=USER)

    assert caught.value.code == "domain.claims_not_extracted"
    assert caught.value.to_public().http_status == HTTPStatus.CONFLICT
    assert world.count("fact_checks") == 0
    assert world.sink.events() == ()


def test_a_version_without_a_matching_run_is_a_conflict(world: World) -> None:
    script, _ = world.extracted()

    with pytest.raises(EvidenceNotMatchedError) as caught:
        world.checker.check(script.id, actor=USER)

    assert caught.value.code == "domain.evidence_not_matched"
    assert caught.value.to_public().http_status == HTTPStatus.CONFLICT
    assert world.count("fact_checks") == 0
    assert world.sink.events() == ()


def test_a_missing_report_is_not_found(world: World, monkeypatch) -> None:
    script, _, match = world.matched()
    monkeypatch.setattr(ResearchReportRepository, "get", lambda self, _: None)

    with pytest.raises(ResearchReportNotFoundError) as caught:
        world.checker.check(script.id, actor=USER)

    assert caught.value.code == "domain.research_report_not_found"
    assert caught.value.to_public().http_status == HTTPStatus.NOT_FOUND
    assert world.stored(match.id) is None
    assert world.sink.events() == ()


def test_a_research_claim_missing_from_the_report_stores_nothing(
    world: World, monkeypatch
) -> None:
    script, _, match = world.matched()
    other = make_report(
        research_claim("rc-other", LOW, evidence("src-x", "Note.")),
        request_id="other",
    )
    monkeypatch.setattr(ResearchReportRepository, "get", lambda self, _: other)

    with pytest.raises(ValueError):
        world.checker.check(script.id, actor=USER)

    assert world.stored(match.id) is None
    assert world.sink.events() == ()


def test_a_concurrent_run_stored_first_is_returned(world, monkeypatch) -> None:
    script, _, match = world.matched()
    winner = world.checker.check(script.id, actor=USER)
    rows = world.count("fact_check_results")
    real = FactCheckRepository.get_by_match
    calls: list[str] = []

    def racing(self, match_id):
        # The first read happens before the other run is committed.
        calls.append(match_id)
        return None if len(calls) == 1 else real(self, match_id)

    monkeypatch.setattr(FactCheckRepository, "get_by_match", racing)

    loser = world.checker.check(script.id, actor=AI)

    assert loser == winner
    assert calls == [match.id, match.id]
    assert world.count("fact_check_results") == rows  # the losing insert rolled back
    assert [e.action for e in world.sink.events()] == ["facts.checked"]


def test_a_failed_result_insert_rolls_back_the_run(world, monkeypatch) -> None:
    script, _, match = world.matched()
    real = FactCheckRepository._insert
    inserted: list[str] = []

    def failing(self, table, values):
        if table == "fact_check_results":
            if inserted:  # the run row and one result are already written
                raise RuntimeError("disk full")
            inserted.append(values["id"])
        real(self, table, values)

    monkeypatch.setattr(FactCheckRepository, "_insert", failing)

    with pytest.raises(RuntimeError):
        world.checker.check(script.id, actor=USER)
    assert world.stored(match.id) is None
    assert (world.count("fact_checks"), world.count("fact_check_results")) == (0, 0)
    assert world.sink.events() == ()


def test_other_integrity_errors_are_raised(world, monkeypatch) -> None:
    script, _, match = world.matched()

    def broken(self, fact_check):
        raise sqlite3.IntegrityError("broken")

    monkeypatch.setattr(FactCheckRepository, "add", broken)

    with pytest.raises(sqlite3.IntegrityError):
        world.checker.check(script.id, actor=USER)
    assert world.stored(match.id) is None
    assert world.sink.events() == ()


def test_earlier_runs_are_untouched_by_a_fact_check(world: World) -> None:
    script, extraction, match = world.matched()
    with world.database.transaction() as connection:
        before = ScriptRepository(connection).list_evidence(extraction.claims[0].id)
    rows = (world.count("evidence"), world.count("claims"))

    world.checker.check(script.id, actor=USER)

    with world.database.transaction() as connection:
        assert ClaimExtractionRepository(connection).get_by_script(script.id) == (
            extraction
        )
        assert EvidenceMatchRepository(connection).get_by_extraction(extraction.id) == (
            match
        )
        assert (
            ScriptRepository(connection).list_evidence(extraction.claims[0].id)
            == before
        )
    assert (world.count("evidence"), world.count("claims")) == rows


# Storage


def result_row(world: World, **change) -> dict:
    script, extraction, _ = world.matched()
    check = world.checker.check(script.id, actor=USER)
    manual = script.claim(FEES, clock=world.clock)
    with world.database.transaction() as connection:
        ScriptRepository(connection).add_claim(manual)
    row = {
        "id": "new",
        "fact_check_id": check.id,
        "claim_id": manual.id,
        "status": "pass",
        "code": "supported",
        "links": 1,
        "best_score": 0.9,
        "numbers": "agree",
        "uncertainty": "low",
    }
    if change.pop("duplicate", False):
        row["claim_id"] = extraction.claims[0].id
    row.update(change)
    return row


@pytest.mark.parametrize(
    ("change", "error"),
    [
        (dict(), None),
        (dict(links=0, best_score=None, numbers=None, uncertainty=None), None),
        (dict(duplicate=True), "UNIQUE"),
        (dict(status="skip"), "CHECK"),
        (dict(code=" "), "CHECK"),
        (dict(code="x" * 101), "CHECK"),
        (dict(links=4), "CHECK"),
        (dict(links=-1), "CHECK"),
        (dict(best_score=1.5), "CHECK"),
        (dict(best_score=-0.5), "CHECK"),
        (dict(numbers="contradicts"), "CHECK"),
        (dict(uncertainty="certain"), "CHECK"),
        (dict(links=0), "CHECK"),  # a snapshot without a link
        (dict(best_score=None), "CHECK"),  # links without a full snapshot
        (dict(numbers=None), "CHECK"),
        (dict(uncertainty=None), "CHECK"),
        (dict(claim_id="missing"), "FOREIGN KEY"),
        (dict(fact_check_id="missing"), "FOREIGN KEY"),
    ],
)
def test_the_database_checks_results(world: World, change, error) -> None:
    row = result_row(world, **change)
    sql = (
        f"INSERT INTO fact_check_results ({', '.join(row)}) "
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


@pytest.mark.parametrize(
    ("change", "error"),
    [
        (dict(), "UNIQUE"),  # a second run for the matching run
        (dict(method=" "), "CHECK"),
        (dict(method="x" * 101), "CHECK"),
        (dict(claims_count=201, pass_count=201), "CHECK"),
        (dict(pass_count=0), "CHECK"),  # the counts must add up
        (dict(fail_count=-1, pass_count=2), "CHECK"),
        (dict(requested_by_kind="robot"), "CHECK"),
        (dict(created_at="2026-10-03"), "CHECK"),
        (dict(match_id="missing"), "FOREIGN KEY"),
        (dict(extraction_id="missing"), "FOREIGN KEY"),
        (dict(research_report_id="missing"), "FOREIGN KEY"),
    ],
)
def test_the_database_checks_runs(world: World, change, error) -> None:
    script, _, match = world.matched()
    check = world.checker.check(script.id, actor=USER)
    row = {
        "id": "new",
        "match_id": match.id,
        "extraction_id": check.extraction_id,
        "script_id": check.script_id,
        "content_item_id": check.content_item_id,
        "research_report_id": check.research_report_id,
        "method": "rules-v1",
        "claims_count": check.claims_count,
        "pass_count": check.pass_count,
        "warn_count": check.warn_count,
        "fail_count": check.fail_count,
        "requested_by_kind": "user",
        "requested_by_id": "owner",
        "created_at": format_datetime(T0),
    }
    row.update(change)

    with (
        pytest.raises(sqlite3.IntegrityError, match=error),
        world.database.transaction() as connection,
    ):
        if error == "FOREIGN KEY":  # the unique match_id would fail first
            connection.execute("DELETE FROM fact_check_results")
            connection.execute("DELETE FROM fact_checks")
        connection.execute(
            f"INSERT INTO fact_checks ({', '.join(row)}) "
            f"VALUES ({', '.join('?' for _ in row)})",
            tuple(row.values()),
        )


def test_results_are_read_in_the_order_of_the_claims(world: World) -> None:
    script, extraction, match = world.matched()
    check = world.checker.check(script.id, actor=USER)

    with world.database.transaction() as connection:
        loaded = FactCheckRepository(connection).get(check.id)
        assert FactCheckRepository(connection).get("missing") is None
        assert FactCheckRepository(connection).get_by_match("missing") is None

    assert [r.claim_id for r in loaded.results] == [c.id for c in extraction.claims]
    assert world.stored(match.id) == check


def test_migration_0019_keeps_earlier_rows(tmp_path: Path) -> None:
    path = tmp_path / "app.db"
    migrate(path, migrations=default_migrations()[:18])
    database = Database(path)
    world = World(database)
    script, extraction, match = world.matched()
    with database.transaction() as connection:
        ids = [
            connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            for table in ("claims", "evidence", "claim_extractions", "evidence_matches")
        ]

    migrate(path)

    with database.transaction() as connection:
        assert ClaimExtractionRepository(connection).get_by_script(script.id) == (
            extraction
        )
        assert EvidenceMatchRepository(connection).get_by_extraction(extraction.id) == (
            match
        )
        assert [
            connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            for table in ("claims", "evidence", "claim_extractions", "evidence_matches")
        ] == ids
    check = FactChecker(database, AuditLog(InMemoryAuditSink())).check(
        script.id, actor=USER
    )
    assert (check.match_id, check.claims_count) == (match.id, 4)


# The run and its results


def test_runs_reject_invalid_state(world: World) -> None:
    script, _, _ = world.matched()
    check = world.checker.check(script.id, actor=USER)
    first = check.results[0]
    for bad in (
        dict(method=" "),
        dict(method="x" * 101),
        dict(match_id=""),
        dict(extraction_id=""),
        dict(research_report_id=" "),
        dict(research_report_id=None),  # results with links but no report
        dict(claims_count=-1),
        dict(claims_count=3),
        dict(claims_count=201, pass_count=201),
        dict(pass_count=check.pass_count + 1, warn_count=check.warn_count - 1),
        dict(fail_count=1, warn_count=check.warn_count - 1),
        dict(results=check.results + (first,), claims_count=5, pass_count=2),
        dict(
            results=(dataclasses.replace(first, claim_id=check.results[1].claim_id),)
            + check.results[1:]
        ),
        dict(created_at=datetime(2026, 10, 3)),
    ):
        with pytest.raises(ValueError):
            dataclasses.replace(check, **bad)
    with pytest.raises(TypeError):
        dataclasses.replace(check, requested_by="owner")
    with pytest.raises(TypeError):
        dataclasses.replace(check, results=("result",) * 4)


def test_a_run_without_a_report_only_has_no_report_results(world: World) -> None:
    script, _, _ = world.matched(with_report=False)
    check = world.checker.check(script.id, actor=USER)
    unmatched = ClaimCheck("c", WARN, CODE.UNMATCHED)

    with pytest.raises(ValueError):
        dataclasses.replace(check, results=(unmatched,) * 4)


def test_create_needs_the_matching_run_of_the_extraction(world: World) -> None:
    _, extraction, match = world.matched()
    other = dataclasses.replace(match, extraction_id="other")

    with pytest.raises(ValueError):
        FactCheck.create(extraction, other, (), requested_by=USER)


@pytest.mark.parametrize(
    "bad",
    [
        dict(claim_id=" "),
        dict(links=4),
        dict(links=-1),
        dict(links=True),
        dict(best_score=1.5),
        dict(best_score=-0.1),
        dict(best_score=True),
        dict(links=0),  # a snapshot without a link
        dict(best_score=None),  # links without a full snapshot
        dict(numbers=None),
        dict(uncertainty=None),
        dict(status=WARN),  # numbers_differ is a FAIL
        dict(code=CODE.UNMATCHED),  # links with a code without links
        dict(links=0, best_score=None, numbers=None, uncertainty=None),
    ],
)
def test_results_reject_invalid_state(bad) -> None:
    good = dict(
        claim_id="c",
        status=FAIL,
        code=CODE.NUMBERS_DIFFER,
        links=1,
        best_score=0.9,
        numbers=DIFFER,
        uncertainty=LOW,
    )

    assert ClaimCheck(**good).status is FAIL
    with pytest.raises(ValueError):
        ClaimCheck(**{**good, **bad})


@pytest.mark.parametrize(
    "bad",
    [
        dict(status="fail"),
        dict(code="numbers_differ"),
        dict(numbers="differ"),
        dict(uncertainty="low"),
    ],
)
def test_results_reject_values_of_the_wrong_type(bad) -> None:
    good = dict(
        claim_id="c",
        status=FAIL,
        code=CODE.NUMBERS_DIFFER,
        links=1,
        best_score=0.9,
        numbers=DIFFER,
        uncertainty=LOW,
    )

    with pytest.raises(TypeError):
        ClaimCheck(**{**good, **bad})


def test_bootstrap_registers_the_checker(tmp_path: Path) -> None:
    container = build_container(
        Settings(environment=Environment.TEST, database_path=tmp_path / "a.db")
    )
    assert isinstance(container.resolve(FactChecker), FactChecker)
