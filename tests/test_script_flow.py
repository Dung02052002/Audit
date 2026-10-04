"""F-074 Script Tests (Prompt Pack v8, prompt #074).

End-to-end scenarios for the script chain, as the user approved on
2026-10-04 (pattern of E-063 ``tests/test_research_flow.py``): one ``Flow``
wires the services like bootstrap with one clock (a distinct tick per call)
and runs a script version through hook generation, the Shorts or LongForm
generator, claim extraction, evidence matching, fact check, originality,
validation and the revision. The modules are tested one by one in the
F-064..F-073 files; these tests check that they work together and the
evidence gaps (claims without evidence, differing numbers, a missing report,
conflicting or weak sources).

The mock ``TextGenerator`` answers are fixed queued JSON per scenario. The
research report is built through the mock research chain (so its topics and
sources are real) and then given hand-made ``ResearchClaim`` values with
``dataclasses.replace``, stored under a fresh request (the collector adds no
evidence notes, see E-063).
"""

import dataclasses
import json
import unicodedata
from dataclasses import dataclass
from http import HTTPStatus

import pytest

from ai_youtube_agent.content.claim_extraction import MAX_CLAIMS, ClaimExtraction
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
    FactCheck,
    FactCheckCode,
    FactCheckStatus,
)
from ai_youtube_agent.content.fact_checker import (
    EvidenceNotMatchedError,
    FactChecker,
)
from ai_youtube_agent.content.hook_generator import ResearchReportNotFoundError
from ai_youtube_agent.content.longform_script_generator import (
    LongFormScriptGenerator,
)
from ai_youtube_agent.content.originality import (
    OriginalityCheck,
    OriginalityCode,
    OriginalityStatus,
)
from ai_youtube_agent.content.originality_checker import OriginalityChecker
from ai_youtube_agent.content.research_report import (
    ClaimEvidence,
    ResearchClaim,
    ResearchReport,
    Uncertainty,
)
from ai_youtube_agent.content.research_request import ResearchRequest
from ai_youtube_agent.content.script import (
    ClaimKind,
    NumberAgreement,
    Script,
    ScriptSection,
    SectionKind,
)
from ai_youtube_agent.content.script_generation import (
    ScriptGenerationError,
    ScriptInputError,
)
from ai_youtube_agent.content.script_revision import (
    ChangeKind,
    ScriptRevision,
    content_sha256,
    diff_scripts,
)
from ai_youtube_agent.content.script_validation import (
    ScriptValidation,
    ValidationStatus,
)
from ai_youtube_agent.content.script_validator import ScriptValidator
from ai_youtube_agent.content.script_versioner import ScriptVersioner
from ai_youtube_agent.content.shorts_script_generator import ShortsScriptGenerator
from ai_youtube_agent.content.strategy import (
    FormatSettings,
    LongFormFormat,
    ShortsFormat,
)
from ai_youtube_agent.core.audit import Actor, ActorKind, AuditLog
from ai_youtube_agent.core.content_item import ContentType
from ai_youtube_agent.core.db.database import Database
from ai_youtube_agent.core.db.repositories.audit import SqliteAuditSink
from ai_youtube_agent.core.db.repositories.channel import (
    ChannelRepository,
    StrategyProfileRepository,
)
from ai_youtube_agent.core.db.repositories.content import (
    ClaimExtractionRepository,
    ContentItemRepository,
    ScriptRepository,
)
from ai_youtube_agent.core.db.repositories.research import (
    ResearchReportRepository,
    ResearchRequestRepository,
)
from ai_youtube_agent.core.flags import FeatureFlags
from factories import make_channel, make_content_item, make_strategy_profile
from test_hook_generator import BRAND, USER, World

AI = Actor(ActorKind.AI, "mock/mock-1")
SECRET = "ZEBRAQUARTZ"
PASS, WARN, FAIL = FactCheckStatus.PASS, FactCheckStatus.WARN, FactCheckStatus.FAIL
CODE = FactCheckCode
LOW, MEDIUM, HIGH = Uncertainty.LOW, Uncertainty.MEDIUM, Uncertainty.HIGH
AGREE, DIFFER, NONE = (
    NumberAgreement.AGREE,
    NumberAgreement.DIFFER,
    NumberAgreement.NONE,
)
NUM, DATE, ENT, COMP, ABS = (
    ClaimKind.NUMERIC,
    ClaimKind.DATE,
    ClaimKind.ENTITY,
    ClaimKind.COMPARISON,
    ClaimKind.ABSOLUTE,
)

HOOK = "Bank fees eat your savings."
HOOK_2 = "Ever checked your bank fees?"
CTA = "Follow for one money tip a day."
FEES = "Banks took 2 billion dollars in fees last year."
FEES_3 = "Banks took 3 billion dollars in fees last year."
VCB = "Vietcombank raised its transfer fee in 2024."
VCB_NOTE = "Vietcombank raised the transfer fee in 2024."
CHEAPER = "Online banks are cheaper than branches."
ALWAYS = "Fees always add up."
PADS = (
    "Check your statement today and compare every account you have.",
    "Write down each charge and ask your bank to explain it clearly.",
    "Small changes in how you pay can keep more money in your pocket.",
)
PAD = " ".join(PADS)
TABLES = (
    "claim_extractions",
    "evidence_matches",
    "fact_checks",
    "originality_checks",
    "script_validations",
    "script_revisions",
)


def answer(*body: str, cta: str = CTA) -> str:
    return json.dumps({"body": list(body), "cta": cta})


def words(n: int, word: str = "fees") -> str:
    return " ".join([word] * n) + "."


def evidence(index: int, note: str, quote: str | None = None) -> tuple:
    return (index, note, quote)


@dataclass(frozen=True)
class Chain:
    """What one script version got from every stage."""

    script: Script
    extraction: ClaimExtraction
    match: EvidenceMatch
    fact: FactCheck
    originality: OriginalityCheck
    validation: ScriptValidation
    revision: ScriptRevision | None

    def codes(self) -> list[FactCheckCode]:
        return [result.code for result in self.fact.results]

    def kinds(self) -> list[ClaimKind | None]:
        return [claim.kind for claim in self.extraction.claims]

    def claim_texts(self) -> list[str]:
        return [claim.text for claim in self.extraction.claims]


@dataclass(frozen=True)
class Made:
    item: object
    hooks: object
    script: Script


class Flow(World):
    """The script chain wired like bootstrap, with the clock of ``World``."""

    def __init__(self, database: Database, **strategy) -> None:
        super().__init__(database, **strategy)
        self.wire(AuditLog(self.sink))

    def wire(self, log: AuditLog) -> None:
        database, clock = self.database, self.clock
        self.log = log
        self.shorts = self.generator(self.text)
        self.longform = self.generator(self.text, longform=True)
        self.extractor = ClaimExtractor(database, log, clock=clock)
        self.matcher = EvidenceMatcher(database, log, clock=clock)
        self.checker = FactChecker(database, log, clock=clock)
        self.originality = OriginalityChecker(database, log, clock=clock)
        self.validator = ScriptValidator(database, log, clock=clock)
        self.versioner = ScriptVersioner(database, log, clock=clock)

    def generator(self, text, *, longform: bool = False):
        if longform:
            return LongFormScriptGenerator(
                self.database,
                text,
                self.log,
                FeatureFlags(longform_enabled=True),
                clock=self.clock,
                sleep=self.sleeps.append,
            )
        return ShortsScriptGenerator(
            self.database, text, self.log, clock=self.clock, sleep=self.sleeps.append
        )

    # Inputs

    def research_report(
        self, *claims: tuple[Uncertainty, tuple[tuple, ...]]
    ) -> ResearchReport:
        """A report of the mock research chain with hand-made claims.

        Each claim is ``(uncertainty, (evidence(source index, note, quote), ...))``.
        """
        base = self.report()
        request = ResearchRequest.create(
            self.channel.id, ["bank fees"], actor=USER, clock=self.clock
        )
        made = tuple(
            ResearchClaim(
                f"rc-{n}",
                entries[0][1],
                tuple(
                    ClaimEvidence(base.sources[i].source_id, note, quote)
                    for i, note, quote in entries
                ),
                (),
                uncertainty,
                (),
            )
            for n, (uncertainty, entries) in enumerate(claims)
        )
        report = dataclasses.replace(
            base, id=f"report-{request.id}", request_id=request.id, claims=made
        )
        with self.database.transaction() as connection:
            ResearchRequestRepository(connection).add(request)
            ResearchReportRepository(connection).add(report)
        return report

    def hooks_for(self, item, report, *texts: str):
        self.text.queue(*(texts or (HOOK, HOOK_2, "Fees add up.")))
        return self.hooks.generate(item.id, report.id, actor=USER)

    def write(self, item, hooks_run, *answers: str, index: int = 0, **kwargs):
        for text in answers:
            self.text.queue(text)
        generator = (
            self.longform if item.content_type is ContentType.LONGFORM else self.shorts
        )
        return generator.generate(item.id, hooks_run.id, index, actor=USER, **kwargs)

    def new_shorts(self, report, *answers: str, hook: str = HOOK) -> "Made":
        """A version 1 Shorts script of a new item, written from ``report``."""
        item = self.add_item(ContentType.SHORTS)
        hooks_run = self.hooks_for(item, report, hook, HOOK_2, "Fees add up.")
        return Made(item, hooks_run, self.write(item, hooks_run, *answers))

    def shorts_script(self, report, *answers: str, hook: str = HOOK) -> Script:
        return self.new_shorts(report, *answers, hook=hook).script

    def again(self, made: "Made", *answers: str, index: int = 0, **kwargs) -> Script:
        """The next version of ``made.item`` from the same hook candidates."""
        return self.write(made.item, made.hooks, *answers, index=index, **kwargs)

    def stored(self, item=None) -> list[Script]:
        with self.database.transaction() as connection:
            return ScriptRepository(connection).list_by_content_item(
                (item or self.item).id
            )

    def add_script(self, script: Script) -> Script:
        with self.database.transaction() as connection:
            ScriptRepository(connection).add(script)
        return script

    # Stages

    def facts(self, script: Script):
        extraction = self.extractor.extract(script.id, actor=USER)
        match = self.matcher.match(script.id, actor=USER)
        fact = self.checker.check(script.id, actor=USER)
        return extraction, match, fact

    def run(self, script: Script, *, revise: bool | None = None) -> Chain:
        extraction, match, fact = self.facts(script)
        originality = self.originality.check(script.id, actor=USER)
        validation = self.validator.validate(script.id, actor=USER)
        if revise is None:
            revise = script.version > 1
        revision = self.versioner.record(script.id, actor=USER) if revise else None
        return Chain(script, extraction, match, fact, originality, validation, revision)

    # The database

    def count(self, table: str) -> int:
        with self.database.transaction() as connection:
            return connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]

    def rows(self, *tables: str) -> list[tuple]:
        with self.database.transaction() as connection:
            return [
                (table, *tuple(row))
                for table in tables
                for row in connection.execute(f"SELECT * FROM {table} ORDER BY rowid")
            ]

    def raw(self, sql: str, params: tuple = ()) -> None:
        """Run a statement the way a damaged database would be, foreign keys off."""
        connection = self.database.connect()
        try:
            connection.execute("PRAGMA foreign_keys = OFF")
            connection.execute(sql, params)
        finally:
            connection.close()

    def add_raw(self, script: Script) -> None:
        connection = self.database.connect()
        try:
            connection.execute("PRAGMA foreign_keys = OFF")
            ScriptRepository(connection).add(script)
        finally:
            connection.close()

    def actions(self) -> list[str]:
        return [event.action for event in self.sink.events()]


@pytest.fixture
def flow(database: Database) -> Flow:
    return Flow(database)


FEES_REPORT = (
    (
        MEDIUM,
        (
            evidence(0, FEES),
            evidence(1, "Banks took 3 billion dollars in fees.", FEES_3),
        ),
    ),
    (MEDIUM, (evidence(2, VCB_NOTE),)),
)
BODY_A = f"{FEES} {VCB} {CHEAPER} {ALWAYS}"
BODY_A2 = f"{FEES} {VCB} {CHEAPER}"


# Scenario 1: a Shorts script from the hook to its history


def test_a_shorts_script_goes_through_the_whole_chain(flow: Flow) -> None:
    report = flow.research_report(*FEES_REPORT)
    hooks_run = flow.hooks_for(flow.item, report)

    first = flow.write(flow.item, hooks_run, answer(BODY_A, PAD))
    one = flow.run(first)

    assert [s.kind for s in first.sections] == [
        SectionKind.HOOK,
        SectionKind.BODY,
        SectionKind.BODY,
        SectionKind.CTA,
    ]
    assert first.research_report_id == report.id
    assert one.claim_texts() == [FEES, VCB, CHEAPER, ALWAYS]
    assert one.kinds() == [NUM, DATE, COMP, ABS]
    assert one.extraction.skipped_cta == 1
    assert (one.match.claims_count, one.match.matched, one.match.unmatched) == (
        4,
        2,
        2,
    )
    assert (one.match.links_count, one.match.numbers_differ) == (3, 1)
    # Mixed PASS and WARN: the fees claim has one agreeing and one differing
    # source, the 2024 claim is supported, the others have no evidence.
    assert one.codes() == [
        CODE.SOURCES_CONFLICT,
        CODE.SUPPORTED,
        CODE.UNMATCHED,
        CODE.UNMATCHED,
    ]
    assert [r.status for r in one.fact.results] == [WARN, PASS, WARN, WARN]
    assert (one.fact.pass_count, one.fact.warn_count, one.fact.fail_count) == (1, 3, 0)
    assert one.fact.status is WARN
    assert (one.fact.results[0].numbers, one.fact.results[1].numbers) == (AGREE, AGREE)
    assert one.validation.status is ValidationStatus.PASS
    assert one.validation.findings == () and one.validation.language_checked is True
    assert one.originality.status is OriginalityStatus.PASS
    assert one.revision is None

    # A second hook and an edited first paragraph make version 2.
    second = flow.write(
        flow.item,
        hooks_run,
        answer(BODY_A2, PAD),
        index=1,
        reason="second hook, shorter facts",
    )
    two = flow.run(second)

    assert (second.version, second.parent_id) == (2, first.id)
    assert second.sections[0].text == HOOK_2 and second.reason.startswith("second")
    assert two.claim_texts() == [FEES, VCB, CHEAPER]
    assert two.extraction.skipped_question == 1
    assert two.validation.status is ValidationStatus.PASS
    revision = two.revision
    assert revision.parent_script_id == first.id and revision.script_id == second.id
    assert [(e.change, e.kind) for e in revision.entries] == [
        (ChangeKind.CHANGED, SectionKind.HOOK),
        (ChangeKind.CHANGED, SectionKind.BODY),
        (ChangeKind.UNCHANGED, SectionKind.BODY),
        (ChangeKind.UNCHANGED, SectionKind.CTA),
    ]
    assert (revision.changed_count, revision.unchanged_count) == (2, 2)
    assert (revision.added_count, revision.removed_count) == (0, 0)
    deltas = [e.words_delta for e in revision.entries]
    assert deltas == [0, -len(ALWAYS.split()), 0, 0]
    assert sum(deltas) == revision.words_after - revision.words_before
    assert revision.words_before == sum(s.words for s in first.sections)
    assert revision.words_after == sum(s.words for s in second.sections)
    assert (revision.seconds_before, revision.seconds_after) == (
        first.estimated_seconds,
        second.estimated_seconds,
    )
    assert revision.content_sha256 == content_sha256(second.sections)

    history = flow.versioner.history(flow.item.id)
    assert [(h.version, h.script_id) for h in history] == [
        (1, first.id),
        (2, second.id),
    ]
    assert history[0].revision is None and history[0].parent_id is None
    assert history[1].parent_id == first.id
    assert history[1].revision == revision.summary()
    assert (history[0].words, history[1].seconds) == (
        revision.words_before,
        second.estimated_seconds,
    )
    assert flow.actions() == [
        "hook.generated",
        "script.generated",
        "claims.extracted",
        "evidence.matched",
        "facts.checked",
        "originality.checked",
        "script.validated",
        "script.generated",
        "claims.extracted",
        "evidence.matched",
        "facts.checked",
        "originality.checked",
        "script.validated",
        "script.revision_recorded",
    ]


# Scenario 2: a LongForm script, chapters on and off

CHAPTER_TITLES = ("Where fees hide", "How to compare banks", "Switching safely")
LONG_HOOK = "Bank fees eat your savings. Here is how to stop them."


def outline(titles=CHAPTER_TITLES) -> str:
    return json.dumps(
        {
            "intro": words(20, "intro"),
            "chapters": [{"title": t, "points": ["one point"]} for t in titles],
            "outro": words(20, "outro"),
            "cta": "Subscribe for more money tips.",
        }
    )


def chapter(text: str) -> str:
    return json.dumps({"text": text})


CH_1 = chapter(f"{FEES} {VCB} {words(480)}")
CH_2 = chapter(f"{CHEAPER} {words(500)}")
CH_3 = chapter(words(500))


def longform_flow(database: Database, *, chapters: bool) -> Flow:
    format_settings = FormatSettings(
        ShortsFormat(15, 60), LongFormFormat(480, 900, chapters=chapters)
    )
    return Flow(database, format=format_settings)


@pytest.mark.parametrize("chapters", [True, False])
def test_a_longform_script_goes_through_the_chain(database, chapters: bool) -> None:
    flow = longform_flow(database, chapters=chapters)
    item = flow.add_item(ContentType.LONGFORM)
    report = flow.research_report(*FEES_REPORT)
    hooks_run = flow.hooks_for(item, report, LONG_HOOK, "Why fees add up.", "Fees.")

    first = flow.write(item, hooks_run, outline(), CH_1, CH_2, CH_3)
    one = flow.run(first)

    middle = SectionKind.CHAPTER if chapters else SectionKind.BODY
    assert [s.kind for s in first.sections] == [
        SectionKind.HOOK,
        SectionKind.INTRO,
        middle,
        middle,
        middle,
        SectionKind.OUTRO,
        SectionKind.CTA,
    ]
    assert [s.title for s in first.sections[2:5]] == (
        list(CHAPTER_TITLES) if chapters else [None] * 3
    )
    assert first.within_target is True
    assert one.claim_texts() == [FEES, VCB, CHEAPER]
    assert one.kinds() == [NUM, DATE, COMP]
    assert one.codes() == [CODE.SOURCES_CONFLICT, CODE.SUPPORTED, CODE.UNMATCHED]
    assert one.validation.status is ValidationStatus.PASS
    assert one.validation.findings == ()
    assert one.originality.status is OriginalityStatus.PASS

    # Version 2 rewrites the second chapter only (50 words shorter).
    second = flow.write(
        item,
        hooks_run,
        outline(),
        CH_1,
        chapter(f"{CHEAPER} {words(450)}"),
        CH_3,
    )
    two = flow.run(second)

    assert (second.version, second.parent_id) == (2, first.id)
    assert two.validation.status is ValidationStatus.PASS
    revision = two.revision
    assert [e.change for e in revision.entries] == [
        ChangeKind.UNCHANGED,
        ChangeKind.UNCHANGED,
        ChangeKind.UNCHANGED,
        ChangeKind.CHANGED,
        ChangeKind.UNCHANGED,
        ChangeKind.UNCHANGED,
        ChangeKind.UNCHANGED,
    ]
    assert [e.words_delta for e in revision.entries][3] == -50
    assert revision.entries[3].text_changed is True
    assert (revision.changed_count, revision.unchanged_count) == (1, 6)
    assert revision.words_after - revision.words_before == -50
    history = flow.versioner.history(item.id)
    assert [h.revision is None for h in history] == [True, False]


# Scenario 3: evidence gaps, each on its own script

NOTE_NO_NUMBERS = "Banks took a lot of money in fees last year."
GAP_CASES = [
    pytest.param(
        [(MEDIUM, (evidence(0, VCB_NOTE),))],
        CHEAPER,
        [CODE.UNMATCHED],
        WARN,
        id="unmatched",
    ),
    pytest.param(
        [(MEDIUM, (evidence(0, FEES_3),))],
        FEES,
        [CODE.NUMBERS_DIFFER],
        FAIL,
        id="numbers-differ-only",
    ),
    pytest.param(
        [(MEDIUM, (evidence(0, FEES), evidence(1, FEES_3)))],
        FEES,
        [CODE.SOURCES_CONFLICT],
        WARN,
        id="agree-and-differ",
    ),
    pytest.param(
        [(MEDIUM, (evidence(0, NOTE_NO_NUMBERS),))],
        FEES,
        [CODE.NUMBER_UNVERIFIED],
        WARN,
        id="numeric-with-no-number-in-the-sources",
    ),
    pytest.param(
        [(HIGH, (evidence(0, "Online banks are cheaper than most branches."),))],
        CHEAPER,
        [CODE.WEAK_EVIDENCE],
        WARN,
        id="high-uncertainty-only",
    ),
    pytest.param([], f"{FEES} {CHEAPER}", [CODE.UNMATCHED] * 2, WARN, id="no-claims"),
]


@pytest.mark.parametrize(("claims", "sentences", "codes", "status"), GAP_CASES)
def test_an_evidence_gap_gives_its_result(
    flow: Flow, claims, sentences, codes, status
) -> None:
    report = flow.research_report(*claims)
    script = flow.shorts_script(report, answer(sentences, PAD))

    extraction, match, fact = flow.facts(script)

    assert [r.code for r in fact.results] == codes
    assert fact.status is status
    assert fact.research_report_id == report.id and not fact.no_report
    assert len(extraction.claims) == len(codes)
    assert flow.count("scripts") == 1


def test_numbers_differ_is_a_fail_that_blocks_nothing(flow: Flow) -> None:
    differ = flow.research_report((MEDIUM, (evidence(0, FEES_3),)))
    agree = flow.research_report((MEDIUM, (evidence(0, FEES),)))
    failing = flow.new_shorts(differ, answer(BODY_A2, PAD))
    passing = flow.new_shorts(agree, answer(BODY_A2, PAD))

    bad = flow.run(failing.script)
    good = flow.run(passing.script)

    assert bad.fact.status is FAIL and good.fact.status is WARN
    assert bad.codes()[0] is CODE.NUMBERS_DIFFER
    assert good.codes()[0] is CODE.SUPPORTED
    assert (bad.match.numbers_differ, good.match.numbers_differ) == (1, 0)
    # The failing script is stored and was validated; the fail changes nothing.
    assert flow.stored(failing.item) == [failing.script]
    assert bad.validation.status is good.validation.status is ValidationStatus.PASS
    assert bad.validation.findings == good.validation.findings == ()
    assert (bad.validation.words, bad.validation.seconds) == (
        good.validation.words,
        good.validation.seconds,
    )

    # A second version of both is written and revised the same way.
    revisions = []
    for made in (failing, passing):
        second = flow.again(made, answer(BODY_A2, PAD + " Keep it simple."))
        assert second.version == 2
        revisions.append(flow.run(second).revision)
    shapes = [[(e.change, e.kind, e.words_delta) for e in r.entries] for r in revisions]
    assert shapes[0] == shapes[1]
    assert revisions[0].counts() == revisions[1].counts()
    assert revisions[0].changed_count == 1
    assert [e.change for e in revisions[0].entries].count(ChangeKind.UNCHANGED) == 3


def test_a_script_without_a_research_report_warns_for_every_claim(flow: Flow) -> None:
    script = Script.create(
        flow.item.id,
        sections=(
            ScriptSection(SectionKind.HOOK, HOOK),
            ScriptSection(SectionKind.BODY, f"{BODY_A} {PAD}"),
            ScriptSection(SectionKind.CTA, CTA),
        ),
        created_by=AI,
        strategy_version=flow.strategy.version,
        clock=flow.clock,
    )
    flow.add_script(script)

    chain = flow.run(script)

    assert script.research_report_id is None
    assert chain.match.no_report and chain.match.research_report_id is None
    assert chain.match.links == () and chain.match.unmatched == 4
    assert chain.codes() == [CODE.NO_REPORT] * 4
    assert chain.fact.status is WARN and chain.fact.no_report
    assert chain.validation.status is ValidationStatus.PASS


# Scenario 4: Vietnamese text and Unicode forms

VI_HOOK = "Phí ngân hàng đang âm thầm ăn mất tiền tiết kiệm của bạn."
VI_FEES = "Năm 2024, các ngân hàng thu về hơn 2 tỷ đồng tiền phí chuyển khoản."
VI_ALWAYS = "Lãi suất vay luôn tăng theo từng năm."
VI_CHEAPER = "Ngân hàng số thường rẻ hơn chi nhánh truyền thống."
VI_FILLER = (
    "Bạn nên dùng online banking và cashback để theo dõi các khoản phí của mình, "
    "kiểm tra sao kê mỗi tháng và so sánh các loại phí với nhau."
)
VI_CTA = "Hãy theo dõi kênh để nhận mẹo tiền bạc mỗi ngày."
VI_REPORT = (
    (
        MEDIUM,
        (
            evidence(
                0,
                "Năm 2024 các ngân hàng thu về hơn 2 tỷ đồng tiền phí "
                "chuyển khoản trong nước.",
            ),
        ),
    ),
    (LOW, (evidence(1, "Ngân hàng số thường rẻ hơn chi nhánh truyền thống."),)),
)


def nfd(text: str) -> str:
    return unicodedata.normalize("NFD", text)


def nfc(text: str) -> str:
    return unicodedata.normalize("NFC", text)


def test_a_vietnamese_script_in_nfd_matches_a_report_in_nfc(flow: Flow) -> None:
    body = nfd(f"{VI_FEES} {VI_ALWAYS} {VI_CHEAPER}")
    assert body != nfc(body)
    report = flow.research_report(*VI_REPORT)
    item = flow.item
    hooks_run = flow.hooks_for(item, report, VI_HOOK, "Phí ngân hàng là gì?", "Phí.")

    script = flow.write(item, hooks_run, answer(body, nfd(VI_FILLER), cta=nfd(VI_CTA)))
    chain = flow.run(script)

    # Claims keep the stored sentence exactly (decomposed), kinds are read on NFC.
    assert chain.claim_texts() == [nfd(VI_FEES), nfd(VI_ALWAYS), nfd(VI_CHEAPER)]
    assert chain.claim_texts()[0] != VI_FEES
    assert chain.kinds() == [NUM, ABS, COMP]
    assert chain.match.matched == 2 and chain.match.unmatched == 1
    assert [link.numbers for link in chain.match.links] == [AGREE, NONE]
    assert chain.codes() == [CODE.SUPPORTED, CODE.UNMATCHED, CODE.SUPPORTED]
    with flow.database.transaction() as connection:
        stored = ClaimExtractionRepository(connection).get_by_script(script.id)
    assert [c.text for c in stored.claims] == chain.claim_texts()
    # English terms inside Vietnamese text carry no language signal.
    assert chain.validation.status is ValidationStatus.PASS
    assert chain.validation.language_checked is True
    assert chain.validation.language_hits >= 10
    assert chain.originality.status is OriginalityStatus.PASS


def test_the_content_hash_is_stable_and_exact(flow: Flow) -> None:
    report = flow.research_report(*VI_REPORT)
    hooks_run = flow.hooks_for(flow.item, report, VI_HOOK, "Phí ngân hàng là gì?", "P.")
    first = flow.write(flow.item, hooks_run, answer(nfc(VI_FEES), nfc(VI_FILLER)))
    second = flow.write(flow.item, hooks_run, answer(nfd(VI_FEES), nfc(VI_FILLER)))

    revision = flow.versioner.record(second.id, actor=USER)
    again = flow.versioner.record(second.id, actor=USER)
    reread = flow.stored()[1]

    # The same visible text in another Unicode form is a change, not equal.
    assert revision == again
    assert revision.content_sha256 == content_sha256(second.sections)
    assert content_sha256(reread.sections) == content_sha256(second.sections)
    assert content_sha256(first.sections) != content_sha256(second.sections)
    assert [e.change for e in revision.entries] == [
        ChangeKind.UNCHANGED,
        ChangeKind.CHANGED,
        ChangeKind.UNCHANGED,
        ChangeKind.UNCHANGED,
    ]
    assert revision.entries[1].text_changed
    assert diff_scripts(first, second).entries == revision.entries


def test_emoji_and_combining_marks_go_through_the_revision(flow: Flow) -> None:
    def make(body: str, parent: Script | None = None) -> Script:
        sections = (
            ScriptSection(SectionKind.HOOK, "Cà phê 🙂 sáng nay."),
            ScriptSection(SectionKind.BODY, body),
            ScriptSection(SectionKind.CTA, "Theo dõi nhé 👍."),
        )
        if parent is None:
            script = Script.create(
                flow.item.id, sections=sections, created_by=AI, clock=flow.clock
            )
        else:
            script = parent.next_version(
                sections=sections, created_by=AI, clock=flow.clock
            )
        return flow.add_script(script)

    base = "Café ngon 🙂 và ấm."
    first = make(base)
    second = make(base.replace("🙂", "🙂🙂"), first)
    third = make(nfc(base).replace("🙂", "🙂🙂"), second)
    only_marks = make("Café ngon 👨‍👩‍👧 và ấm.", third)

    revisions = [
        flow.versioner.record(s.id, actor=USER) for s in (second, third, only_marks)
    ]

    assert [r.changed_count for r in revisions] == [1, 1, 1]
    assert revisions[1].entries[1].text_changed  # é in two forms differs
    assert [r.content_sha256 for r in revisions] == [
        content_sha256(s.sections) for s in (second, third, only_marks)
    ]
    assert len({r.content_sha256 for r in revisions}) == 3
    # Stored and read back, the hash stays the same.
    for script, revision in zip(flow.stored()[1:], revisions, strict=True):
        assert content_sha256(script.sections) == revision.content_sha256


# Scenario 5: empty and boundary inputs


def test_a_script_without_claims_has_an_empty_match_and_passes(flow: Flow) -> None:
    report = flow.research_report(*FEES_REPORT)
    script = flow.shorts_script(report, answer(PAD))

    chain = flow.run(script)

    assert chain.extraction.claims == () and chain.extraction.skipped_cta == 1
    assert (chain.match.claims_count, chain.match.links) == (0, ())
    assert (chain.match.matched, chain.match.unmatched) == (0, 0)
    assert chain.match.research_report_id == report.id
    assert chain.fact.results == () and chain.fact.status is PASS
    assert chain.fact.claims_count == 0


def test_a_shorts_script_may_have_three_body_sections(flow: Flow) -> None:
    report = flow.research_report(*FEES_REPORT)
    item = flow.item
    hooks_run = flow.hooks_for(item, report)

    script = flow.write(item, hooks_run, answer(FEES, VCB, PAD))
    chain = flow.run(script)

    assert [s.kind for s in script.sections].count(SectionKind.BODY) == 3
    assert chain.validation.status is ValidationStatus.PASS

    # A fourth is asked again with the reason.
    flow.text.calls.clear()
    other = flow.write(
        item, hooks_run, answer("a b", "c d", "e f", "g h"), answer(FEES, PAD)
    )
    assert other.version == 2
    assert "1 to 3 paragraphs" in flow.text.calls[1].prompt


@pytest.mark.parametrize(
    ("wrong", "right", "seconds"),
    [(22, 23, 14), (138, 137, 61)],
    ids=["minimum", "maximum"],
)
def test_the_duration_limits_are_inclusive(flow: Flow, wrong, right, seconds) -> None:
    report = flow.research_report()
    hooks_run = flow.hooks_for(flow.item, report)
    flow.text.calls.clear()

    script = flow.write(
        flow.item, hooks_run, answer(words(wrong)), answer(words(right))
    )

    assert len(flow.text.calls) == 2
    assert (
        f"it takes about {seconds} seconds, not 15 to 60" in flow.text.calls[1].prompt
    )
    assert script.estimated_seconds in (15, 60)
    assert script.within_target is True
    assert flow.run(script).validation.status is ValidationStatus.PASS


def test_a_sentence_repeated_in_sections_is_one_claim(flow: Flow) -> None:
    report = flow.research_report(*FEES_REPORT)
    script = flow.shorts_script(
        report, answer(f"{FEES} {PAD}", FEES, FEES.lower() + "  ", cta=CTA)
    )

    chain = flow.run(script)

    assert chain.claim_texts() == [FEES]
    assert chain.extraction.dropped_duplicates == 2
    assert chain.extraction.claims[0].section_index == 1
    assert chain.fact.claims_count == 1


def test_the_claim_cap_holds_from_the_script_to_the_fact_check(database) -> None:
    flow = Flow(database)
    item = flow.add_item(ContentType.LONGFORM)
    report = flow.research_report()
    hooks_run = flow.hooks_for(item, report, LONG_HOOK, "Why fees add up.", "Fees.")

    def facts(start: int) -> str:
        sentences = " ".join(f"Fee {n} rose {n}%." for n in range(start, start + 70))
        return chapter(f"{sentences} {words(200)}")

    script = flow.write(item, hooks_run, outline(), facts(1), facts(71), facts(141))
    extraction, match, fact = flow.facts(script)

    assert len(extraction.claims) == MAX_CLAIMS == 200
    assert extraction.dropped_over_cap == 10
    assert {claim.kind for claim in extraction.claims} == {NUM}
    assert (match.claims_count, match.matched, match.unmatched) == (200, 0, 200)
    assert fact.claims_count == 200 and fact.warn_count == 200
    assert flow.count("fact_check_results") == 200


# Scenario 6: malformed answers


def test_malformed_answers_are_rejected_and_asked_again(flow: Flow) -> None:
    report = flow.research_report()
    hooks_run = flow.hooks_for(flow.item, report)
    flow.text.calls.clear()
    before = len(flow.actions())

    first = flow.write(
        flow.item,
        hooks_run,
        "Here is your script!",
        json.dumps({"body": [PAD], "cta": CTA, "title": "x"}),
        answer(FEES, PAD),
    )

    assert first.version == 1 and len(flow.text.calls) == 3
    assert "not JSON" in flow.text.calls[1].prompt
    assert '"body" and "cta" only' in flow.text.calls[2].prompt
    [event] = list(flow.sink.events())[before:]
    assert event.action == "script.generated"
    assert event.metadata["rejected_answers"] == 2

    flow.text.calls.clear()
    second = flow.write(
        flow.item,
        hooks_run,
        answer("Get rich quick with this one trick. " + PAD),
        answer(VCB, PAD),
    )

    assert second.version == 2 and len(flow.text.calls) == 2
    assert "banned phrase 'get rich quick'" in flow.text.calls[1].prompt
    assert list(flow.sink.events())[-1].metadata["rejected_answers"] == 1
    assert flow.run(second).validation.status is ValidationStatus.PASS


def test_three_rejected_answers_store_nothing_after_the_hook(flow: Flow) -> None:
    report = flow.research_report()
    hooks_run = flow.hooks_for(flow.item, report)
    before = len(flow.actions())

    with pytest.raises(ScriptGenerationError) as caught:
        flow.write(
            flow.item,
            hooks_run,
            "no",
            json.dumps({"body": [], "cta": CTA}),
            answer("you won't believe " + PAD),
        )

    assert len(caught.value.rejections) == 3
    assert caught.value.to_public().http_status == HTTPStatus.UNPROCESSABLE_ENTITY
    assert flow.stored() == []
    for table in ("scripts", "claims", "evidence", *TABLES):
        assert flow.count(table) == 0, table
    [event] = list(flow.sink.events())[before:]
    assert event.action == "script.generation_failed"
    assert event.metadata["answers"] == 3
    with pytest.raises(ScriptNotFoundError):
        flow.extractor.extract("nothing", actor=USER)


def test_a_malformed_longform_outline_stores_nothing(database) -> None:
    flow = Flow(database)
    item = flow.add_item(ContentType.LONGFORM)
    report = flow.research_report()
    hooks_run = flow.hooks_for(item, report, LONG_HOOK, "Why fees add up.", "Fees.")

    with pytest.raises(ScriptGenerationError) as caught:
        flow.write(item, hooks_run, "{}", outline(titles=("a", "b")), "[1]")

    assert len(caught.value.rejections) == 3
    assert flow.stored(item) == []
    assert flow.count("claim_extractions") == 0
    assert list(flow.sink.events())[-1].metadata["stage"] == "outline"


# Scenario 7: ordering and reruns


def snapshot(flow: Flow) -> list[tuple]:
    return flow.rows(
        "claims",
        "evidence",
        "claim_extractions",
        "evidence_matches",
        "fact_checks",
        "fact_check_results",
        "originality_checks",
        "originality_findings",
        "script_validations",
        "script_validation_findings",
        "script_revisions",
        "script_revision_sections",
    )


def test_each_stage_returns_the_stored_run_on_a_second_call(flow: Flow) -> None:
    report = flow.research_report(*FEES_REPORT)
    hooks_run = flow.hooks_for(flow.item, report)
    flow.write(flow.item, hooks_run, answer(BODY_A, PAD))
    second = flow.write(flow.item, hooks_run, answer(BODY_A2, PAD), index=1)
    chain = flow.run(second)
    rows, events = snapshot(flow), len(flow.sink.events())

    again = flow.run(second, revise=True)

    assert again == chain
    assert snapshot(flow) == rows and len(flow.sink.events()) == events
    assert flow.count("claim_extractions") == 1 and flow.count("script_revisions") == 1


def test_the_stages_can_only_run_in_order_except_the_independent_ones(
    flow: Flow,
) -> None:
    report = flow.research_report(*FEES_REPORT)
    script = flow.shorts_script(report, answer(BODY_A, PAD))
    events = len(flow.sink.events())

    for call in (flow.matcher.match, flow.checker.check):
        with pytest.raises(ClaimsNotExtractedError) as caught:
            call(script.id, actor=USER)
        assert caught.value.to_public().http_status == HTTPStatus.CONFLICT
    flow.extractor.extract(script.id, actor=USER)
    with pytest.raises(EvidenceNotMatchedError) as caught:
        flow.checker.check(script.id, actor=USER)
    assert caught.value.to_public().http_status == HTTPStatus.CONFLICT
    assert flow.count("evidence_matches") == 0 and flow.count("fact_checks") == 0
    assert len(flow.sink.events()) == events + 1  # only the extraction

    # Validation and originality need no other run.
    other = flow.shorts_script(report, answer(BODY_A2, PAD))
    validation = flow.validator.validate(other.id, actor=USER)
    flow.originality.check(other.id, actor=USER)
    assert validation.status is ValidationStatus.PASS
    assert flow.count("claim_extractions") == 1 and flow.count("evidence_matches") == 0


def test_a_rerun_leaves_the_rows_of_earlier_stages_unchanged(flow: Flow) -> None:
    report = flow.research_report(*FEES_REPORT)
    script = flow.shorts_script(report, answer(BODY_A, PAD))
    flow.extractor.extract(script.id, actor=USER)
    flow.matcher.match(script.id, actor=USER)
    stages = ("claims", "evidence", "claim_extractions", "evidence_matches")
    earlier = flow.rows(*stages)

    flow.checker.check(script.id, actor=USER)
    flow.validator.validate(script.id, actor=USER)
    flow.checker.check(script.id, actor=USER)

    assert flow.rows(*stages) == earlier
    assert flow.count("fact_checks") == 1


def test_the_history_follows_the_versions_not_the_order_of_the_revisions(
    flow: Flow,
) -> None:
    report = flow.research_report(*FEES_REPORT)
    made = flow.new_shorts(report, answer(f"{FEES} {words(30)}"))
    versions = [made.script] + [
        flow.again(made, answer(f"{FEES} {words(30 + n)}")) for n in range(1, 4)
    ]

    for script in (versions[3], versions[1]):
        flow.versioner.record(script.id, actor=USER)
    history = flow.versioner.history(made.item.id)

    assert [h.version for h in history] == [1, 2, 3, 4]
    assert [h.script_id for h in history] == [s.id for s in versions]
    assert [h.revision is None for h in history] == [True, False, True, False]


# Scenario 8: originality against the channel


def test_a_copy_of_another_item_is_found_and_the_original_is_not(flow: Flow) -> None:
    report = flow.research_report(*FEES_REPORT)
    original = flow.shorts_script(report, answer(BODY_A, PAD), hook=HOOK)
    copy = flow.shorts_script(report, answer(BODY_A, PAD), hook="Your bank hides it.")

    first = flow.originality.check(original.id, actor=USER)
    second = flow.originality.check(copy.id, actor=USER)

    assert first.status is OriginalityStatus.PASS and first.priors_count == 0
    assert second.status is OriginalityStatus.FAIL and second.priors_count == 1
    codes = {finding.code for finding in second.findings}
    assert OriginalityCode.NEAR_DUPLICATE in codes
    near = next(f for f in second.findings if f.code is OriginalityCode.NEAR_DUPLICATE)
    assert near.prior_script_id == original.id
    assert near.prior_content_item_id == original.content_item_id
    assert near.score >= 0.6
    # A fail is a record: the copy is still stored, validated and checked.
    assert flow.run(copy).validation.status is ValidationStatus.PASS


def test_earlier_versions_of_the_same_item_are_never_compared(flow: Flow) -> None:
    report = flow.research_report(*FEES_REPORT)
    made = flow.new_shorts(report, answer(BODY_A, PAD))
    second = flow.again(made, answer(BODY_A, PAD), index=1)  # only the hook differs

    check = flow.originality.check(second.id, actor=USER)

    assert check.priors_count == 0 and check.findings == ()
    assert check.status is OriginalityStatus.PASS


def test_a_script_of_another_channel_is_ignored(flow: Flow) -> None:
    channel = make_channel()
    strategy = make_strategy_profile(channel, brand=BRAND)
    foreign = make_content_item(channel, strategy)
    with flow.database.transaction() as connection:
        ChannelRepository(connection).add(channel)
        StrategyProfileRepository(connection).add(strategy)
        ContentItemRepository(connection).add(foreign)
    report = flow.research_report(*FEES_REPORT)
    flow.add_script(
        Script.create(
            foreign.id,
            sections=(
                ScriptSection(SectionKind.HOOK, "Your bank hides it."),
                ScriptSection(SectionKind.BODY, f"{BODY_A} {PAD}"),
                ScriptSection(SectionKind.CTA, CTA),
            ),
            created_by=AI,
            clock=flow.clock,
        )
    )
    script = flow.shorts_script(report, answer(BODY_A, PAD))

    check = flow.originality.check(script.id, actor=USER)

    assert check.priors_count == 0 and check.findings == ()


# Scenario 9: abnormal database data


def test_a_report_removed_after_matching_stops_the_fact_check(flow: Flow) -> None:
    report = flow.research_report(*FEES_REPORT)
    script = flow.shorts_script(report, answer(BODY_A, PAD))
    flow.extractor.extract(script.id, actor=USER)
    flow.matcher.match(script.id, actor=USER)
    flow.raw("DELETE FROM research_reports WHERE id = ?", (report.id,))
    events = len(flow.sink.events())

    with pytest.raises(ResearchReportNotFoundError) as caught:
        flow.checker.check(script.id, actor=USER)

    assert caught.value.to_public().http_status == HTTPStatus.NOT_FOUND
    assert flow.count("fact_checks") == 0 and flow.count("fact_check_results") == 0
    assert len(flow.sink.events()) == events


def test_a_script_pointing_at_a_removed_report_cannot_be_matched(flow: Flow) -> None:
    report = flow.research_report(*FEES_REPORT)
    script = flow.shorts_script(report, answer(BODY_A, PAD))
    ghost = dataclasses.replace(
        script,
        id="ghost-script",
        version=2,
        parent_id=script.id,
        research_report_id="no-such-report",
        sections=(*script.sections[:-1], ScriptSection(SectionKind.CTA, "Bye now.")),
    )
    flow.add_raw(ghost)
    flow.raw("DELETE FROM research_reports WHERE id = ?", (report.id,))
    flow.extractor.extract(script.id, actor=USER)
    flow.extractor.extract(ghost.id, actor=USER)
    events = len(flow.sink.events())

    for target in (script, ghost):
        with pytest.raises(ResearchReportNotFoundError):
            flow.matcher.match(target.id, actor=USER)

    assert flow.count("evidence_matches") == 0 and flow.count("evidence") == 0
    assert len(flow.sink.events()) == events


def corrupt_report(flow: Flow, report: ResearchReport, change) -> None:
    with flow.database.transaction() as connection:
        raw = connection.execute(
            "SELECT report_json FROM research_reports WHERE id = ?", (report.id,)
        ).fetchone()[0]
        data = json.loads(raw)
        change(data)
        connection.execute(
            "UPDATE research_reports SET report_json = ? WHERE id = ?",
            (json.dumps(data), report.id),
        )


def test_a_corrupted_report_claim_stops_the_match_and_stores_nothing(flow) -> None:
    report = flow.research_report(*FEES_REPORT)
    script = flow.shorts_script(report, answer(BODY_A, PAD))
    flow.extractor.extract(script.id, actor=USER)
    corrupt_report(flow, report, lambda data: data["claims"][0].update(evidence=[]))
    events = len(flow.sink.events())

    with pytest.raises(ValueError, match="needs evidence"):
        flow.matcher.match(script.id, actor=USER)

    assert flow.count("evidence_matches") == 0 and flow.count("evidence") == 0
    assert len(flow.sink.events()) == events


def test_a_report_claim_renamed_after_matching_stops_the_fact_check(flow) -> None:
    report = flow.research_report(*FEES_REPORT)
    script = flow.shorts_script(report, answer(BODY_A, PAD))
    flow.extractor.extract(script.id, actor=USER)
    flow.matcher.match(script.id, actor=USER)
    corrupt_report(flow, report, lambda data: data["claims"][0].update(id="renamed"))
    events = len(flow.sink.events())

    with pytest.raises(ValueError, match="is not in the report"):
        flow.checker.check(script.id, actor=USER)

    assert flow.count("fact_checks") == 0 and flow.count("fact_check_results") == 0
    assert len(flow.sink.events()) == events


def test_a_removed_parent_script_cannot_be_revised(flow: Flow) -> None:
    report = flow.research_report()
    made = flow.new_shorts(report, answer(BODY_A, PAD))
    second = flow.again(made, answer(BODY_A2, PAD))
    flow.raw("DELETE FROM scripts WHERE id = ?", (made.script.id,))
    events = len(flow.sink.events())

    with pytest.raises(ScriptInputError) as caught:
        flow.versioner.record(second.id, actor=USER)

    assert caught.value.to_public().http_status == HTTPStatus.UNPROCESSABLE_ENTITY
    assert flow.count("script_revisions") == 0
    assert len(flow.sink.events()) == events


# Scenario 10: revisions


def test_five_versions_are_revised_in_any_order(flow: Flow) -> None:
    report = flow.research_report()
    made = flow.new_shorts(report, answer(f"{FEES} {words(30)}"))
    versions = [made.script] + [
        flow.again(made, answer(f"{FEES} {words(30 + n)}")) for n in range(1, 5)
    ]
    guarded = ("approval_requests", "approval_artifacts", "artifacts")
    before = flow.rows(*guarded)

    first = {n: flow.versioner.record(versions[n - 1].id, actor=USER) for n in (4, 2)}
    partial = flow.versioner.history(made.item.id)
    rest = {n: flow.versioner.record(versions[n - 1].id, actor=USER) for n in (5, 3)}
    revisions = {**first, **rest}

    assert [h.revision is None for h in partial] == [True, False, True, False, True]
    assert sorted(revisions) == [2, 3, 4, 5]
    for n, revision in revisions.items():
        assert revision.version == n
        assert revision.parent_script_id == versions[n - 2].id
        assert revision.script_id == versions[n - 1].id
        assert (revision.changed_count, revision.unchanged_count) == (1, 2)
        assert revision.words_after - revision.words_before == 1
        assert revision.entries[1].words_delta == 1
    again = flow.versioner.record(versions[2].id, actor=USER)
    assert again == revisions[3]
    assert flow.count("script_revisions") == 4
    history = flow.versioner.history(made.item.id)
    assert [h.version for h in history] == [1, 2, 3, 4, 5]
    assert [h.revision.id for h in history[1:]] == [
        revisions[n].id for n in (2, 3, 4, 5)
    ]
    assert history[0].revision is None
    assert flow.rows(*guarded) == before == []


def test_the_first_version_has_no_revision(flow: Flow) -> None:
    report = flow.research_report()
    script = flow.shorts_script(report, answer(BODY_A, PAD))

    with pytest.raises(ScriptInputError) as caught:
        flow.versioner.record(script.id, actor=USER)

    assert caught.value.to_public().http_status == HTTPStatus.UNPROCESSABLE_ENTITY
    assert flow.count("script_revisions") == 0


def test_a_version_with_no_change_is_allowed_when_added_directly(flow: Flow) -> None:
    report = flow.research_report()
    first = flow.shorts_script(report, answer(BODY_A, PAD))
    with pytest.raises(ValueError, match="different script text"):
        first.next_version(sections=first.sections, clock=flow.clock)
    same = dataclasses.replace(
        first,
        id="same-version",
        version=2,
        parent_id=first.id,
        created_at=flow.clock(),
        reason="stored without a change",
    )
    flow.add_script(same)

    revision = flow.versioner.record(same.id, actor=USER)

    assert (revision.added_count, revision.removed_count) == (0, 0)
    assert (revision.changed_count, revision.unchanged_count) == (0, 4)
    assert all(e.change is ChangeKind.UNCHANGED for e in revision.entries)
    assert {e.words_delta for e in revision.entries} == {0}
    assert revision.words_before == revision.words_after
    assert revision.content_sha256 == content_sha256(first.sections)


# Scenario 11: audit and secrets


def test_the_audit_trail_and_the_tables_hold_no_script_text(database) -> None:
    flow = Flow(database)
    flow.wire(AuditLog(SqliteAuditSink(database)))
    secret_fact = f"Banks took 2 billion dollars in {SECRET} fees last year."
    report = flow.research_report(*FEES_REPORT)
    hooks_run = flow.hooks_for(flow.item, report)
    first = flow.write(flow.item, hooks_run, answer(f"{secret_fact} {PAD}"))
    second = flow.write(
        flow.item, hooks_run, answer(f"{secret_fact} {PAD} Extra."), index=1
    )
    flow.run(first)
    flow.run(second)

    events = SqliteAuditSink(database).events()

    chain = [
        "claims.extracted",
        "evidence.matched",
        "facts.checked",
        "originality.checked",
        "script.validated",
    ]
    assert [e.action for e in events] == [
        "script.generated",
        "script.generated",
        *chain,
        *chain,
        "script.revision_recorded",
    ]
    assert [e.entity.type for e in events] == [
        "content_item",
        "content_item",
        *["script"] * 11,
    ]
    with database.transaction() as connection:
        tables = [
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' "
                "AND name NOT LIKE 'sqlite_%'"
            )
        ]
        holders = {
            table
            for table in tables
            if any(
                SECRET in str(value)
                for row in connection.execute(f"SELECT * FROM {table}")
                for value in tuple(row)
            )
        }
    # Only the stored script and its claims hold the text, by design.
    assert holders == {"scripts", "claims"}
    assert SECRET not in repr([e.metadata for e in events])
    assert SECRET not in repr([e.metadata for e in flow.sink.events()])
