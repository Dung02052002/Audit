"""F-068 Claim Extractor (Prompt Pack v8, prompt #068).

Rules the user approved on 2026-10-03:

- deterministic rules only, recorded as method ``rules-v1`` on the run;
- each section is split into sentences; a sentence is a claim when it has a
  number, %, currency, year or date, a capitalised word not at its start, a
  comparison or superlative, or an absolute word (English and Vietnamese
  lists); the CTA section, questions and opinions are skipped; the HOOK
  counts; other languages get only the language-neutral signals;
- one closed ``ClaimKind`` per claim, the strongest of numeric, date, entity,
  comparison, absolute;
- at most 200 claims of 1 to 1,000 characters, exact repeats dropped, every
  skip and drop counted on the run;
- one run per script version, stored with its claims (migration 0017), also
  with no claim; a second call returns it; ``claims.extracted`` is audited
  after commit; an unknown script is ``ScriptNotFoundError`` (404).
"""

import dataclasses
import sqlite3
import time
import unicodedata
from datetime import UTC, datetime, timedelta
from http import HTTPStatus
from pathlib import Path

import pytest

from ai_youtube_agent.bootstrap import build_container
from ai_youtube_agent.content.claim_extraction import (
    MAX_CLAIM_CHARS,
    MAX_CLAIMS,
    RULES_METHOD,
    ClaimExtraction,
    ClaimKind,
    SkipReason,
    assess_sentence,
    classify_sentence,
    extract_claims,
    split_sentences,
)
from ai_youtube_agent.content.claim_extractor import (
    ClaimExtractor,
    ScriptNotFoundError,
)
from ai_youtube_agent.content.script import (
    Claim,
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
    ScriptRepository,
)
from factories import make_channel, make_content_item, make_strategy_profile

T0 = datetime(2026, 10, 3, 12, 0, tzinfo=UTC)
USER = Actor(ActorKind.USER, "owner")
AI = Actor(ActorKind.AI, "mock/mock-1")

HOOK = "Banks took 2 billion dollars in fees last year. Did you notice?"
BODY = (
    "Vietcombank raised its transfer fee in 2024. I think that hurts. "
    "Online banks are cheaper than branches. Fees always add up. "
    "Let us look closer."
)
CTA = "Follow for more tips like the best 5 ways to save. Subscribe now!"


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
        channel = make_channel()
        strategy = make_strategy_profile(channel)
        self.item = make_content_item(channel, strategy)
        with database.transaction() as connection:
            ChannelRepository(connection).add(channel)
            StrategyProfileRepository(connection).add(strategy)
            ContentItemRepository(connection).add(self.item)
        self.sink = InMemoryAuditSink()
        self.extractor = ClaimExtractor(database, AuditLog(self.sink), clock=self.clock)

    def script(self, *sections: ScriptSection, parent: Script | None = None):
        sections = sections or (
            ScriptSection(SectionKind.HOOK, HOOK),
            ScriptSection(SectionKind.BODY, BODY),
            ScriptSection(SectionKind.CTA, CTA),
        )
        if parent is None:
            script = Script.create(
                self.item.id, sections=sections, created_by=AI, clock=self.clock
            )
        else:
            script = parent.next_version(
                sections=sections, created_by=AI, clock=self.clock
            )
        with self.database.transaction() as connection:
            ScriptRepository(connection).add(script)
        return script

    def stored(self, script_id: str) -> ClaimExtraction | None:
        with self.database.transaction() as connection:
            return ClaimExtractionRepository(connection).get_by_script(script_id)

    def claim_rows(self) -> int:
        with self.database.transaction() as connection:
            return connection.execute("SELECT count(*) FROM claims").fetchone()[0]


@pytest.fixture
def world(database: Database) -> World:
    return World(database)


def body(*sentences: str) -> Script:
    return Script.create(
        "item", sections=[ScriptSection(SectionKind.BODY, " ".join(sentences))]
    )


# Sentences


@pytest.mark.parametrize(
    ("text", "sentences"),
    [
        ("One. Two! Three? Four…", ["One.", "Two!", "Three?", "Four…"]),
        ("Line one\nLine two.\n\n Line three", ["Line one", "Line two.", "Line three"]),
        ('He said "Stop." Then left.', ['He said "Stop."', "Then left."]),
        ("It rose 3.5% today. Next.", ["It rose 3.5% today.", "Next."]),
        ("Banks in the U.S. charge fees.", ["Banks in the U.S. charge fees."]),
        ("Dr. Smith agrees, e.g. on fees.", ["Dr. Smith agrees, e.g. on fees."]),
        ("J. K. Rowling wrote it.", ["J. K. Rowling wrote it."]),
        ("1. Fees rose. 2. Rates fell.", ["1. Fees rose.", "2. Rates fell."]),
        ("The fee is 5. Then it is 6.", ["The fee is 5.", "Then it is 6."]),
        ("Dạ vâng ạ. Ngân hàng thu phí.", ["Dạ vâng ạ.", "Ngân hàng thu phí."]),
        ("Wait... then what.", ["Wait...", "then what."]),
        ("Wait... Then it happened.", ["Wait...", "Then it happened."]),
        (
            "Theo TS. Nguyễn Văn A, cà phê tốt cho tim. Hết.",
            ["Theo TS. Nguyễn Văn A, cà phê tốt cho tim.", "Hết."],
        ),
        (
            "Sống ở TP. Hồ Chí Minh rất vui. Hết.",
            ["Sống ở TP. Hồ Chí Minh rất vui.", "Hết."],
        ),
        ("Ranked No. 1 in the world. Next.", ["Ranked No. 1 in the world.", "Next."]),
        (
            "PGS. Lan và ThS. Minh ở Q. 1 nói vậy.",
            ["PGS. Lan và ThS. Minh ở Q. 1 nói vậy."],
        ),
    ],
)
def test_sections_are_split_into_verbatim_sentences(text, sentences) -> None:
    assert split_sentences(text) == sentences


def test_splitting_is_linear_when_every_period_is_an_abbreviation() -> None:
    text = "A. " * 33_334  # 100,002 characters, no sentence end

    started = time.perf_counter()
    sentences = split_sentences(text)
    elapsed = time.perf_counter() - started

    assert sentences == [text.strip()]
    assert elapsed < 1.0


# Signals and kinds


@pytest.mark.parametrize(
    ("sentence", "kind"),
    [
        ("Fees rose 12 points.", ClaimKind.NUMERIC),
        ("Fees rose by half a percent.", ClaimKind.NUMERIC),
        ("The fee is $5.", ClaimKind.NUMERIC),
        ("The fee is 20.000 VND.", ClaimKind.NUMERIC),
        ("Fees cost millions.", ClaimKind.NUMERIC),
        ("Phí tăng 5 phần trăm.", ClaimKind.NUMERIC),
        ("Phí là hai triệu.", ClaimKind.NUMERIC),
        ("In 2020 fees rose 5%.", ClaimKind.NUMERIC),  # numeric beats date
        ("In 2020, prices went up.", ClaimKind.DATE),
        ("It opened on October 3.", ClaimKind.DATE),
        ("It opened on 3 October.", ClaimKind.DATE),
        ("It opened on 2026-10-03.", ClaimKind.DATE),
        ("It opened on 3/10/2026.", ClaimKind.DATE),
        ("It was big in the 1990s.", ClaimKind.DATE),
        ("Ngân hàng mở cửa vào tháng 10.", ClaimKind.DATE),
        ("The fee came from Vietcombank.", ClaimKind.ENTITY),
        ("Phí ở Việt Nam đang tăng.", ClaimKind.ENTITY),
        ("Most people never check fees.", ClaimKind.COMPARISON),  # beats absolute
        ("Online banks are cheaper than branches.", ClaimKind.COMPARISON),
        ("This is the first bank to drop fees.", ClaimKind.COMPARISON),
        ("Phí ở đây cao hơn.", ClaimKind.COMPARISON),
        ("Đây là ngân hàng tốt nhất.", ClaimKind.COMPARISON),
        ("Fees always add up.", ClaimKind.ABSOLUTE),
        ("Cash is proven to be safer.", ClaimKind.ABSOLUTE),
        ("Late payments cause fees.", ClaimKind.ABSOLUTE),
        ("Ngân hàng không bao giờ miễn phí.", ClaimKind.ABSOLUTE),
        ("Điều này đã được chứng minh.", ClaimKind.ABSOLUTE),
    ],
)
def test_each_sentence_keeps_its_strongest_kind(sentence, kind) -> None:
    assert classify_sentence(sentence) is kind
    assert assess_sentence(sentence) is kind


@pytest.mark.parametrize(
    "sentence",
    [
        "Vietcombank charges fees.",  # a capital opening the sentence
        "Then I saw the bill.",  # the pronoun I
        'He said: "Wait for it.',  # after a colon or quote
        "Rather than paying, save.",
        "First, open the app.",  # a list word, not a ranking
        "Hơn nữa, phí cũng thay đổi.",  # "moreover", not a comparison
        "Hơn nữa phí cũng thay đổi.",
        "1. Open the app.",  # the list marker is not a number
        "Let us look closer.",
    ],
)
def test_sentences_without_a_signal_are_skipped(sentence) -> None:
    assert classify_sentence(sentence) is None
    assert assess_sentence(sentence) is SkipReason.NO_SIGNAL


@pytest.mark.parametrize(
    ("sentence", "reason"),
    [
        ("Did you pay 5% in fees?", SkipReason.QUESTION),
        ("Is this the best bank?!", SkipReason.QUESTION),
        ('Who said "never"?', SkipReason.QUESTION),
        ("Bạn có biết phí là 5% không?", SkipReason.QUESTION),
        ("I think fees rose 5%.", SkipReason.OPINION),
        ("In my opinion, Vietcombank is the best.", SkipReason.OPINION),
        ("Maybe fees will fall in 2027.", SkipReason.OPINION),
        ("Fees will probably rise.", SkipReason.OPINION),
        ("Theo mình, phí sẽ tăng 5%.", SkipReason.OPINION),
        ("Mình nghĩ ngân hàng này tốt nhất.", SkipReason.OPINION),
        ("Có lẽ phí sẽ giảm.", SkipReason.OPINION),
    ],
)
def test_questions_and_opinions_are_skipped(sentence, reason) -> None:
    assert assess_sentence(sentence) is reason


def test_linking_words_hide_only_themselves() -> None:
    assert classify_sentence("Hơn nữa, phí ở đây cao hơn.") is ClaimKind.COMPARISON
    assert classify_sentence("Phí hơn nữa là vô lý.") is ClaimKind.COMPARISON


def test_decomposed_vietnamese_keeps_its_signals() -> None:
    for sentence, expected in (
        ("Phí ở đây cao hơn.", ClaimKind.COMPARISON),
        ("Ngân hàng không bao giờ miễn phí.", ClaimKind.ABSOLUTE),
        ("Phí tăng năm 2024.", ClaimKind.DATE),
        ("Có lẽ phí sẽ giảm.", SkipReason.OPINION),
    ):
        decomposed = unicodedata.normalize("NFD", sentence)
        assert decomposed != sentence
        assert assess_sentence(decomposed) is expected


def test_decomposed_text_is_stored_verbatim_and_deduplicated() -> None:
    composed = "Phí ở đây cao hơn."
    decomposed = unicodedata.normalize("NFD", composed)

    extracted = extract_claims(body(decomposed, composed))

    assert [c.text for c in extracted.claims] == [decomposed]
    assert extracted.dropped_duplicates == 1


def test_other_languages_get_only_the_neutral_signals() -> None:
    assert classify_sentence("Die Gebühr stieg um 5 Prozent.") is ClaimKind.NUMERIC
    assert classify_sentence("Die Gebühr stieg 2024.") is ClaimKind.DATE
    assert classify_sentence("La banque Société Générale paie.") is ClaimKind.ENTITY
    # "immer" (always) and "meilleur" (best) are not in the word lists.
    assert classify_sentence("Gebühren steigen immer.") is None
    assert classify_sentence("la meilleure banque.") is None


# Extraction rules


def test_claims_come_from_every_section_but_the_cta() -> None:
    script = Script.create(
        "item",
        sections=[
            ScriptSection(SectionKind.HOOK, HOOK),
            ScriptSection(SectionKind.BODY, BODY),
            ScriptSection(SectionKind.CTA, CTA),
        ],
    )

    extracted = extract_claims(script)

    assert [(c.text, c.section_index, c.kind) for c in extracted.claims] == [
        ("Banks took 2 billion dollars in fees last year.", 0, ClaimKind.NUMERIC),
        ("Vietcombank raised its transfer fee in 2024.", 1, ClaimKind.DATE),
        ("Online banks are cheaper than branches.", 1, ClaimKind.COMPARISON),
        ("Fees always add up.", 1, ClaimKind.ABSOLUTE),
    ]
    assert extracted.skipped == {
        SkipReason.CTA: 2,
        SkipReason.QUESTION: 1,
        SkipReason.OPINION: 1,
        SkipReason.NO_SIGNAL: 1,
    }
    assert (
        extracted.dropped_duplicates,
        extracted.dropped_over_cap,
        extracted.dropped_too_long,
    ) == (0, 0, 0)


def test_repeats_and_long_sentences_are_dropped_and_counted() -> None:
    long = "The fee is 5 " + "x" * MAX_CLAIM_CHARS + "."
    script = body(
        "Fees rose 5%.", "fees   ROSE 5%.", long, "Fees rose 5%! ", "Fees rose 6%."
    )

    extracted = extract_claims(script)

    assert [c.text for c in extracted.claims] == [
        "Fees rose 5%.",
        "Fees rose 5%!",
        "Fees rose 6%.",
    ]
    assert (extracted.dropped_duplicates, extracted.dropped_too_long) == (1, 1)


def test_at_most_200_claims_are_kept_per_script() -> None:
    sentences = [f"Fee {n} costs {n} dollars." for n in range(MAX_CLAIMS + 5)]
    script = Script.create(
        "item",
        sections=[
            ScriptSection(SectionKind.BODY, " ".join(sentences[:100])),
            ScriptSection(SectionKind.BODY, " ".join(sentences[100:])),
        ],
    )

    extracted = extract_claims(script)

    assert len(extracted.claims) == MAX_CLAIMS
    assert extracted.claims[-1].text == sentences[MAX_CLAIMS - 1]
    assert extracted.claims[-1].section_index == 1
    assert extracted.dropped_over_cap == 5


def test_a_repeat_after_the_cap_counts_as_a_repeat() -> None:
    sentences = [f"Fee {n} costs {n} dollars." for n in range(MAX_CLAIMS + 1)]
    script = Script.create(
        "item",
        sections=[
            ScriptSection(SectionKind.BODY, " ".join(sentences[:100])),
            ScriptSection(SectionKind.BODY, " ".join([*sentences[100:], sentences[0]])),
        ],
    )

    extracted = extract_claims(script)

    assert len(extracted.claims) == MAX_CLAIMS
    assert (extracted.dropped_over_cap, extracted.dropped_duplicates) == (1, 1)


# The extractor


def test_claims_are_extracted_stored_and_audited(world: World) -> None:
    script = world.script()

    extraction = world.extractor.extract(script.id, actor=USER)

    assert extraction.script_id == script.id
    assert extraction.content_item_id == world.item.id
    assert extraction.method == RULES_METHOD == "rules-v1"
    assert extraction.requested_by == USER
    assert extraction.created_at == T0 + timedelta(seconds=2)
    assert [c.text for c in extraction.claims] == [
        "Banks took 2 billion dollars in fees last year.",
        "Vietcombank raised its transfer fee in 2024.",
        "Online banks are cheaper than branches.",
        "Fees always add up.",
    ]
    for claim in extraction.claims:
        assert claim.script_id == script.id
        assert claim.extraction_id == extraction.id
        assert claim.created_at == extraction.created_at
    assert extraction.counts() == {
        "claims": 4,
        "skipped_cta": 2,
        "skipped_question": 1,
        "skipped_opinion": 1,
        "skipped_no_signal": 1,
        "dropped_duplicates": 0,
        "dropped_over_cap": 0,
        "dropped_too_long": 0,
    }

    assert world.stored(script.id) == extraction
    with world.database.transaction() as connection:
        assert ClaimExtractionRepository(connection).get(extraction.id) == extraction
        stored_claims = ScriptRepository(connection).list_claims(script.id)
    assert stored_claims == list(extraction.claims)

    [event] = world.sink.events()
    assert (event.action, event.result, event.actor) == (
        "claims.extracted",
        AuditResult.SUCCESS,
        USER,
    )
    assert (event.entity.type, event.entity.id) == ("script", script.id)
    assert dict(event.metadata) == {
        "claim_extraction_id": extraction.id,
        "content_item_id": world.item.id,
        "version": 1,
        "method": "rules-v1",
        **extraction.counts(),
    }


def test_the_audit_event_is_recorded_after_the_commit(world: World) -> None:
    script = world.script()
    seen: list[ClaimExtraction | None] = []

    class CheckingSink(InMemoryAuditSink):
        def append(self, event) -> None:
            # A fresh connection only sees committed data.
            seen.append(world.stored(script.id))
            super().append(event)

    extraction = ClaimExtractor(world.database, AuditLog(CheckingSink())).extract(
        script.id, actor=USER
    )

    assert seen == [extraction]


def test_a_second_call_returns_the_stored_run(world: World) -> None:
    script = world.script()
    first = world.extractor.extract(script.id, actor=USER)
    rows = world.claim_rows()

    again = world.extractor.extract(script.id, actor=AI)

    assert again == first
    assert world.claim_rows() == rows
    assert [e.action for e in world.sink.events()] == ["claims.extracted"]


def test_a_run_without_claims_is_stored(world: World) -> None:
    script = world.script(
        ScriptSection(SectionKind.HOOK, "Want lower fees?"),
        ScriptSection(SectionKind.BODY, "Let us look closer. I think it helps."),
        ScriptSection(SectionKind.CTA, "Subscribe for 5 more tips."),
    )

    extraction = world.extractor.extract(script.id, actor=USER)

    assert extraction.claims == ()
    assert extraction.counts() == {
        "claims": 0,
        "skipped_cta": 1,
        "skipped_question": 1,
        "skipped_opinion": 1,
        "skipped_no_signal": 1,
        "dropped_duplicates": 0,
        "dropped_over_cap": 0,
        "dropped_too_long": 0,
    }
    assert world.stored(script.id) == extraction
    [event] = world.sink.events()
    assert (event.result, event.metadata["claims"]) == (AuditResult.SUCCESS, 0)


def test_each_script_version_gets_its_own_extraction(world: World) -> None:
    first = world.script()
    second = world.script(
        ScriptSection(SectionKind.BODY, "Fees rose 7% in 2025."), parent=first
    )
    old = world.extractor.extract(first.id, actor=USER)

    new = world.extractor.extract(second.id, actor=USER)

    assert new.id != old.id
    assert [(c.script_id, c.text, c.section_index) for c in new.claims] == [
        (second.id, "Fees rose 7% in 2025.", 0)
    ]
    assert world.stored(first.id) == old
    assert world.stored(second.id) == new


def test_an_unknown_script_is_not_found(world: World) -> None:
    with pytest.raises(ScriptNotFoundError) as caught:
        world.extractor.extract("missing", actor=USER)

    assert caught.value.code == "domain.script_not_found"
    assert caught.value.to_public().http_status == HTTPStatus.NOT_FOUND
    assert world.sink.events() == ()


def test_a_concurrent_run_stored_first_is_returned(world, monkeypatch) -> None:
    script = world.script()
    winner = world.extractor.extract(script.id, actor=USER)
    rows = world.claim_rows()
    real = ClaimExtractionRepository.get_by_script
    calls: list[str] = []

    def racing(self, script_id):
        # The first read happens before the other run is committed.
        calls.append(script_id)
        return None if len(calls) == 1 else real(self, script_id)

    monkeypatch.setattr(ClaimExtractionRepository, "get_by_script", racing)

    loser = world.extractor.extract(script.id, actor=AI)

    assert loser == winner
    assert len(calls) == 2
    assert world.claim_rows() == rows  # the losing insert was rolled back
    assert [e.action for e in world.sink.events()] == ["claims.extracted"]


def test_a_failed_claim_insert_rolls_back_the_run(world, monkeypatch) -> None:
    script = world.script()
    real = ScriptRepository.add_claim
    added: list[str] = []

    def failing(self, claim):
        if added:  # the run row and one claim are already written
            raise RuntimeError("disk full")
        added.append(claim.id)
        real(self, claim)

    monkeypatch.setattr(ScriptRepository, "add_claim", failing)

    with pytest.raises(RuntimeError):
        world.extractor.extract(script.id, actor=USER)
    assert world.stored(script.id) is None
    assert world.claim_rows() == 0
    with world.database.transaction() as connection:
        runs = connection.execute("SELECT count(*) FROM claim_extractions")
        assert runs.fetchone()[0] == 0
    assert world.sink.events() == ()


def test_other_integrity_errors_are_raised(world, monkeypatch) -> None:
    script = world.script()

    def broken(self, extraction):
        raise sqlite3.IntegrityError("broken")

    monkeypatch.setattr(ClaimExtractionRepository, "add", broken)

    with pytest.raises(sqlite3.IntegrityError):
        world.extractor.extract(script.id, actor=USER)
    assert world.stored(script.id) is None
    assert world.sink.events() == ()


def test_the_database_keeps_one_run_per_script(world: World) -> None:
    script = world.script()
    extraction = world.extractor.extract(script.id, actor=USER)
    copy = dataclasses.replace(extraction, id="other", claims=())

    with pytest.raises(sqlite3.IntegrityError), world.database.transaction() as c:
        ClaimExtractionRepository(c).add(copy)


def test_listed_claims_keep_the_extraction_order(world: World) -> None:
    sentences = [f"Fee {n} costs {n} dollars." for n in range(30)]
    script = world.script(ScriptSection(SectionKind.BODY, " ".join(sentences)))

    extraction = world.extractor.extract(script.id, actor=USER)

    with world.database.transaction() as connection:
        listed = ScriptRepository(connection).list_claims(script.id)
    assert [c.text for c in listed] == sentences
    assert listed == list(extraction.claims)


def test_claims_without_kind_or_run_still_load(world: World) -> None:
    script = world.script()
    plain = script.claim("A claim.", section_index=1, clock=world.clock)

    with world.database.transaction() as connection:
        ScriptRepository(connection).add_claim(plain)
        assert ScriptRepository(connection).list_claims(script.id) == [plain]
    assert (plain.kind, plain.extraction_id) == (None, None)


def test_migration_0017_keeps_claims_stored_before_it(tmp_path: Path) -> None:
    path = tmp_path / "app.db"
    migrate(path, migrations=default_migrations()[:16])
    database = Database(path)
    channel = make_channel()
    strategy = make_strategy_profile(channel)
    item = make_content_item(channel, strategy)
    script = Script.create(item.id, "Fees rose 5%.", clock=lambda: T0)
    with database.transaction() as connection:
        ChannelRepository(connection).add(channel)
        StrategyProfileRepository(connection).add(strategy)
        ContentItemRepository(connection).add(item)
        ScriptRepository(connection).add(script)
        connection.execute(
            "INSERT INTO claims VALUES ('c1', ?, 'Fees rose 5%.', ?, 0)",
            (script.id, format_datetime(T0)),
        )

    migrate(path)

    with database.transaction() as connection:
        [claim] = ScriptRepository(connection).list_claims(script.id)
        assert ClaimExtractionRepository(connection).get_by_script(script.id) is None
    assert (claim.section_index, claim.kind, claim.extraction_id) == (0, None, None)
    extraction = ClaimExtractor(database, AuditLog(InMemoryAuditSink())).extract(
        script.id, actor=USER
    )
    assert [c.text for c in extraction.claims] == ["Fees rose 5%."]


# The run


def test_runs_and_claims_reject_invalid_state(world: World) -> None:
    script = world.script()
    extraction = world.extractor.extract(script.id, actor=USER)
    claim = extraction.claims[0]
    other = Claim.create("other-script", "Fees rose 5%.", kind=ClaimKind.NUMERIC)
    for bad in (
        dict(method=" "),
        dict(method="x" * 101),
        dict(skipped_cta=-1),
        dict(dropped_over_cap=True),
        dict(claims=(claim, claim)),
        dict(claims=(other,)),
        dict(claims=(dataclasses.replace(claim, kind=None),)),
        dict(claims=(dataclasses.replace(claim, text="1" * 1001),)),
        dict(created_at=datetime(2026, 10, 3)),
        dict(content_item_id=""),
    ):
        with pytest.raises(ValueError):
            dataclasses.replace(extraction, **bad)
    with pytest.raises(TypeError):
        dataclasses.replace(extraction, requested_by="owner")
    with pytest.raises(TypeError):
        Claim.create("s", "x", kind="numeric")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        Claim.create("s", "x", extraction_id=" ")


def test_bootstrap_registers_the_extractor(tmp_path: Path) -> None:
    container = build_container(
        Settings(environment=Environment.TEST, database_path=tmp_path / "a.db")
    )
    assert isinstance(container.resolve(ClaimExtractor), ClaimExtractor)
