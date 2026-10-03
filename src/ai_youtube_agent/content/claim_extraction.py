"""Claim extraction rules and runs (Prompt Pack v8, prompt #068), context C5.

The rules were approved by the user on 2026-10-03. Extraction is
deterministic, with no AI model; the run records its method (``RULES_METHOD``
= ``"rules-v1"``) so a provider extractor can be added later.

- Each section of the script is split into sentences (``split_sentences``):
  every line on its own, then after ``.``, ``!``, ``?`` or ``…`` (closing
  quotes or brackets may follow) when a space comes next. A period does not
  end a sentence after a capital initial (``J. K.``), dotted letters
  (``U.S.``, ``e.g.``), a known English or Vietnamese abbreviation (``Mr.``,
  ``Dr.``, ``etc.``, ``No.``, ``TS.``, ``ThS.``, ``PGS.``, ``TP.``, ``Q.``
  ...) or a list number opening the line (``1.``); a decimal point never
  does, as no space follows it. Only the word just before the period is
  looked at, so splitting stays linear in the text length. A leading list
  marker (``-``, ``1.``, ``2)``) is kept in the claim text but ignored when
  the sentence is assessed.
- Skipped (``SkipReason``): every sentence of a CTA section, then questions
  (ending with ``?``), then opinions (an opinion or hedge marker, such as
  ``I think``, ``in my opinion``, ``maybe``, ``probably``, ``theo mình``,
  ``mình nghĩ``, ``có lẽ``), then sentences with no claim signal. The HOOK
  section counts like any other section.
- A sentence is a claim when it has at least one signal (``classify_sentence``;
  ``assess_sentence`` applies the skips first); the claim keeps the
  strongest, in the order of ``ClaimKind``:

  1. ``numeric``: a digit that is not part of a date, ``%``, a currency sign
     or code (``$``, ``€``, ``₫``, ``USD``, ``VND`` ...), or a scale or
     percent word (``million``, ``percent``, ``triệu``, ``tỷ`` ...);
  2. ``date``: a year from 1800 to 2099 (or its decade, ``1990s``), a numeric
     date (``3/10/2026``, ``2026-10-03``), an English month name next to a
     day number, or a Vietnamese ``ngày``/``tháng``/``năm`` with a number;
  3. ``entity``: a capitalised word that is not the first word of the
     sentence, not the first word after a colon, an opening quote or bracket,
     and not the English pronoun ``I``;
  4. ``comparison``: comparatives and superlatives (``more than``, ``... -er
     than`` except ``rather than`` and ``other than``, ``compared to``,
     ``most``, ``best``, ``first``, ``hơn``, ``nhất``, ``so với``, ``đầu
     tiên`` ...); ``First,`` or ``Hơn nữa`` (moreover) opening a sentence
     is a linking word, not a ranking, and does not count;
  5. ``absolute``: absolute, certainty or causal words (``always``,
     ``never``, ``proven``, ``causes``, ``guaranteed``, ``luôn``, ``không
     bao giờ``, ``chứng minh``, ``gây ra`` ...).

  The order puts the most precisely checkable signal first; a year is a date,
  not a number. Words and phrases match as whole words, ignoring case and
  spacing, on the NFC form of the sentence (so decomposed Vietnamese text
  keeps its signals); the stored claim text stays verbatim. Skips and drops
  count sentences (a CTA section counts each of its sentences).
- The word lists are English and Vietnamese only. A script in another
  language gets only the language-neutral signals: digits, ``%``, currency
  signs, numeric dates and capitalisation (and ``?`` for questions).
- Limits (``extract_claims``): a claim is the verbatim sentence; a sentence
  longer than ``MAX_CLAIM_CHARS`` (1,000) is dropped; an exact repeat of an
  earlier claim (ignoring case and spacing) is dropped; at most
  ``MAX_CLAIMS`` (200) are kept per script. Repeats are compared on the NFC
  form, and a repeat counts as a repeat also after the cap is reached. Every
  skip and drop is counted on the run.
- ``ClaimExtraction`` is one stored run for one script version, with its
  claims (zero is allowed) and counts.
"""

import re
import unicodedata
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum

from ai_youtube_agent.content.script import Claim, ClaimKind, Script, SectionKind
from ai_youtube_agent.core.audit import Actor

__all__ = [
    "MAX_CLAIM_CHARS",
    "MAX_CLAIMS",
    "RULES_METHOD",
    "ClaimExtraction",
    "ClaimKind",
    "ExtractedClaims",
    "FoundClaim",
    "SkipReason",
    "assess_sentence",
    "classify_sentence",
    "dedup_key",
    "extract_claims",
    "split_sentences",
]

Clock = Callable[[], datetime]

RULES_METHOD = "rules-v1"
MAX_CLAIMS = 200
MAX_CLAIM_CHARS = 1_000
MAX_METHOD = 100


class SkipReason(StrEnum):
    CTA = "cta"
    QUESTION = "question"
    OPINION = "opinion"
    NO_SIGNAL = "no_signal"


# Sentences

_CLOSERS = "\"'”’»)]"
_BOUNDARY = re.compile(r"[.!?…]+[" + re.escape(_CLOSERS) + r"]*(?=\s)")
_ABBREVIATION_WORDS = (
    "mr mrs ms dr prof sr jr st vs etc inc ltd corp no"  # English
    " ts ths pgs gs bs tp q p"  # Vietnamese titles and places (TS., TP., Q.)
)
_ABBREVIATIONS = frozenset(_ABBREVIATION_WORDS.split())
_MAX_ABBREVIATION = 20  # longer words before a period are never abbreviations
_WORD_BEFORE = re.compile(r"(?<![\w.])[\w.]+\Z")
_DOTTED = re.compile(r"(?:[^\W\d_]\.)+[^\W\d_]")  # U.S, e.g, a.m
_LIST_NUMBER = re.compile(r"\s*\d{1,2}\.")
_LIST_MARKER = re.compile(r"^\s*(?:[-*•]|\d{1,2}[.)])\s+")


def split_sentences(text: str) -> list[str]:
    """The sentences of a text, verbatim and trimmed, in order."""
    sentences = []
    for line in text.splitlines():
        start = 0
        for match in _BOUNDARY.finditer(line):
            if match.group() == "." and _is_abbreviation(line, start, match.start()):
                continue
            sentences.append(line[start : match.end()])
            start = match.end()
        sentences.append(line[start:])
    return [s.strip() for s in sentences if s.strip()]


def _is_abbreviation(line: str, start: int, period: int) -> bool:
    """Whether the period at ``period`` does not end the sentence at ``start``.

    Only a bounded window before the period is searched, without slicing.
    """
    if _LIST_NUMBER.fullmatch(line, start, period + 1):
        return True
    match = _WORD_BEFORE.search(line, max(start, period - _MAX_ABBREVIATION), period)
    if match is None:
        return False
    word = match.group()
    return (
        (len(word) == 1 and word.isupper())
        or word.casefold() in _ABBREVIATIONS
        or _DOTTED.fullmatch(word) is not None
    )


# Signals


def _phrases(*phrases: str) -> re.Pattern[str]:
    """Whole words or phrases, ignoring case and spacing."""
    alternatives = sorted(
        (r"\s+".join(re.escape(word) for word in phrase.split()) for phrase in phrases),
        key=len,
        reverse=True,
    )
    return re.compile(r"(?<!\w)(?:" + "|".join(alternatives) + r")(?!\w)", re.I)


_OPINION = _phrases(
    # English
    "I think",
    "I believe",
    "I feel",
    "I guess",
    "in my opinion",
    "in my view",
    "maybe",
    "perhaps",
    "probably",
    # Vietnamese
    "theo mình",
    "theo tôi",
    "mình nghĩ",
    "tôi nghĩ",
    "mình thấy",
    "tôi thấy",
    "có lẽ",
)
_SCALE_WORDS = _phrases(
    "hundred",
    "thousand",
    "million",
    "millions",
    "billion",
    "billions",
    "trillion",
    "percent",
    "per cent",
    "nghìn",
    "ngàn",
    "triệu",
    "tỷ",
    "tỉ",
    "phần trăm",
)
_CURRENCY = re.compile(r"[%$€£¥₫₹]|(?<!\w)(?:USD|EUR|GBP|JPY|VND)(?!\w)")
_MONTHS = (
    "January|February|March|April|May|June|July|August|September|October"
    "|November|December"
)
_DATE = re.compile(
    r"(?<!\d)(?:"
    r"\d{4}-\d{1,2}-\d{1,2}"  # 2026-10-03
    r"|\d{1,2}[/.]\d{1,2}[/.]\d{2,4}"  # 3/10/2026, 03.10.2026
    rf"|\d{{1,2}}(?:st|nd|rd|th)?\s+(?:{_MONTHS})"  # 3 October
    r"|(?:1[89]|20)\d{2}s?"  # 1990, 1990s
    r")(?!\d)"
    rf"|(?<!\w)(?:{_MONTHS})\s+\d{{1,2}}(?:st|nd|rd|th)?(?!\d)"  # October 3
    r"|(?<!\w)(?:ngày|tháng|năm)\s+\d{1,4}(?!\d)",  # ngày 3, tháng 10
    re.I,
)
_DIGIT = re.compile(r"\d")
_COMPARISON = _phrases(
    # English
    "more than",
    "less than",
    "fewer than",
    "compared to",
    "compared with",
    "most",
    "least",
    "best",
    "worst",
    "first",
    "biggest",
    "largest",
    "smallest",
    "highest",
    "lowest",
    "fastest",
    "cheapest",
    "richest",
    "poorest",
    # Vietnamese
    "hơn",
    "nhất",
    "kém",
    "so với",
    "đầu tiên",
)
_ER_THAN = re.compile(r"(?<!\w)(?!rather\b|other\b)\w+er\s+than(?!\w)", re.I)
_LINKING_OPENER = re.compile(r"^\W*(?:first\s*,|hơn\s+nữa(?!\w))", re.I)
_ABSOLUTE = _phrases(
    # English
    "always",
    "never",
    "nobody",
    "no one",
    "proven",
    "proves",
    "proved",
    "cause",
    "causes",
    "caused",
    "guaranteed",
    "definitely",
    "certainly",
    # Vietnamese
    "luôn",
    "không bao giờ",
    "chưa bao giờ",
    "không ai",
    "chứng minh",
    "gây ra",
    "chắc chắn",
)
_WORD = re.compile(r"\w+")
_QUESTION = re.compile(r"\?[!?…" + re.escape(_CLOSERS) + r"]*$")
_ENTITY_RESET = ':"“‘«(['  # the next word may be capitalised for another reason


def assess_sentence(sentence: str) -> ClaimKind | SkipReason:
    """The claim kind of a sentence outside a CTA, or why it is skipped."""
    sentence = unicodedata.normalize("NFC", sentence)
    if _QUESTION.search(sentence):
        return SkipReason.QUESTION
    if _OPINION.search(sentence):
        return SkipReason.OPINION
    return classify_sentence(sentence) or SkipReason.NO_SIGNAL


def classify_sentence(sentence: str) -> ClaimKind | None:
    """The strongest claim signal of a sentence, or None without one."""
    sentence = _LIST_MARKER.sub("", unicodedata.normalize("NFC", sentence), count=1)
    without_dates = _DATE.sub(" ", sentence)
    if (
        _DIGIT.search(without_dates)
        or _CURRENCY.search(sentence)
        or _SCALE_WORDS.search(sentence)
    ):
        return ClaimKind.NUMERIC
    if without_dates != sentence:
        return ClaimKind.DATE
    if _has_entity(sentence):
        return ClaimKind.ENTITY
    if _has_comparison(sentence):
        return ClaimKind.COMPARISON
    if _ABSOLUTE.search(sentence):
        return ClaimKind.ABSOLUTE
    return None


def _has_entity(sentence: str) -> bool:
    for index, match in enumerate(_WORD.finditer(sentence)):
        word = match.group()
        if index == 0 or not word[0].isupper() or word == "I":
            continue
        before = sentence[: match.start()].rstrip()
        if before and before[-1] in _ENTITY_RESET:
            continue
        return True
    return False


def _has_comparison(sentence: str) -> bool:
    if _ER_THAN.search(sentence):
        return True
    text = _LINKING_OPENER.sub(" ", sentence)
    return _COMPARISON.search(text) is not None


# Extraction


def dedup_key(text: str) -> str:
    """The text as compared for repeats: NFC, collapsed spaces, casefolded."""
    return " ".join(unicodedata.normalize("NFC", text).split()).casefold()


@dataclass(frozen=True)
class FoundClaim:
    text: str
    section_index: int
    kind: ClaimKind


@dataclass(frozen=True)
class ExtractedClaims:
    claims: tuple[FoundClaim, ...]
    skipped: dict[SkipReason, int]
    dropped_duplicates: int
    dropped_over_cap: int
    dropped_too_long: int


def extract_claims(script: Script) -> ExtractedClaims:
    """The claims of a script version by the rules of the module docstring."""
    found: list[FoundClaim] = []
    skipped = dict.fromkeys(SkipReason, 0)
    seen: set[str] = set()
    duplicates = over_cap = too_long = 0
    for index, section in enumerate(script.sections):
        for sentence in split_sentences(section.text):
            if section.kind is SectionKind.CTA:
                skipped[SkipReason.CTA] += 1
                continue
            kind = assess_sentence(sentence)
            if isinstance(kind, SkipReason):
                skipped[kind] += 1
                continue
            if len(sentence) > MAX_CLAIM_CHARS:
                too_long += 1
                continue
            key = dedup_key(sentence)
            if key in seen:
                duplicates += 1
                continue
            seen.add(key)
            if len(found) == MAX_CLAIMS:
                over_cap += 1
                continue
            found.append(FoundClaim(sentence, index, kind))
    return ExtractedClaims(tuple(found), skipped, duplicates, over_cap, too_long)


# The stored run


@dataclass(frozen=True)
class ClaimExtraction:
    id: str
    script_id: str
    content_item_id: str
    method: str
    claims: tuple[Claim, ...]
    skipped_cta: int
    skipped_question: int
    skipped_opinion: int
    skipped_no_signal: int
    dropped_duplicates: int
    dropped_over_cap: int
    dropped_too_long: int
    requested_by: Actor
    created_at: datetime

    def __post_init__(self) -> None:
        for name in ("id", "script_id", "content_item_id"):
            if not getattr(self, name):
                raise ValueError(f"{name} must not be empty")
        if (
            not isinstance(self.method, str)
            or not self.method.strip()
            or len(self.method) > MAX_METHOD
        ):
            raise ValueError(f"a method is 1 to {MAX_METHOD} characters")
        if len(self.claims) > MAX_CLAIMS:
            raise ValueError(f"at most {MAX_CLAIMS} claims")
        for claim in self.claims:
            if not isinstance(claim, Claim):
                raise TypeError("claims must be Claim values")
            if claim.script_id != self.script_id or claim.extraction_id != self.id:
                raise ValueError("every claim must belong to this run and script")
            if claim.kind is None or len(claim.text) > MAX_CLAIM_CHARS:
                raise ValueError(
                    f"an extracted claim has a kind and 1 to {MAX_CLAIM_CHARS} "
                    "characters"
                )
        if len({dedup_key(c.text) for c in self.claims}) != len(self.claims):
            raise ValueError("claims must not repeat")
        for name in (
            "skipped_cta",
            "skipped_question",
            "skipped_opinion",
            "skipped_no_signal",
            "dropped_duplicates",
            "dropped_over_cap",
            "dropped_too_long",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a whole number of 0 or more")
        if not isinstance(self.requested_by, Actor):
            raise TypeError("requested_by must be an Actor")
        if not isinstance(
            self.created_at, datetime
        ) or self.created_at.utcoffset() != timedelta(0):
            raise ValueError("created_at must be timezone-aware UTC")

    @property
    def claims_count(self) -> int:
        return len(self.claims)

    @classmethod
    def create(
        cls,
        script: Script,
        extracted: ExtractedClaims,
        *,
        method: str = RULES_METHOD,
        requested_by: Actor,
        clock: Clock | None = None,
    ) -> "ClaimExtraction":
        """A run for ``script``; its claims share the run's time."""
        extraction_id = uuid.uuid4().hex
        now = clock() if clock else datetime.now(UTC)
        return cls(
            id=extraction_id,
            script_id=script.id,
            content_item_id=script.content_item_id,
            method=method,
            claims=tuple(
                script.claim(
                    found.text,
                    section_index=found.section_index,
                    kind=found.kind,
                    extraction_id=extraction_id,
                    clock=lambda: now,
                )
                for found in extracted.claims
            ),
            skipped_cta=extracted.skipped[SkipReason.CTA],
            skipped_question=extracted.skipped[SkipReason.QUESTION],
            skipped_opinion=extracted.skipped[SkipReason.OPINION],
            skipped_no_signal=extracted.skipped[SkipReason.NO_SIGNAL],
            dropped_duplicates=extracted.dropped_duplicates,
            dropped_over_cap=extracted.dropped_over_cap,
            dropped_too_long=extracted.dropped_too_long,
            requested_by=requested_by,
            created_at=now,
        )

    def counts(self) -> dict[str, int]:
        """The scalar counts of the run, as audited."""
        return {
            "claims": self.claims_count,
            "skipped_cta": self.skipped_cta,
            "skipped_question": self.skipped_question,
            "skipped_opinion": self.skipped_opinion,
            "skipped_no_signal": self.skipped_no_signal,
            "dropped_duplicates": self.dropped_duplicates,
            "dropped_over_cap": self.dropped_over_cap,
            "dropped_too_long": self.dropped_too_long,
        }
