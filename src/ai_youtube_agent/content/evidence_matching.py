"""Evidence matching rules and runs (Prompt Pack v8, prompt #069), context C5.

The rules were approved by the user on 2026-10-03, with the number rules
refined after review the same day. Matching is deterministic, with no AI
model; the run records its method (``MATCH_METHOD`` = ``"rules-v1"``) so
another matcher can be added later.

- The pool is the research report the script version was made from
  (``Script.research_report_id``) and nothing else. Each evidence entry of
  each report claim (``ClaimEvidence``: source, note, optional quote) is one
  candidate. It is compared on its quote and on its note, and the better of
  the two counts (a qualifying text first, then numbers that agree, then the
  higher score; the quote wins a tie). The link stores the quote as excerpt
  when there is one, otherwise the note, and keeps the research claim id.
- Words (``content_words``): the words of ``content/similarity.py`` (case
  folded runs of letters and digits) of the NFC form of the text, without the
  English and Vietnamese stop words of the topic extractor
  (``topic_extractor.STOP_WORDS``), without words holding a digit, the scale
  words below and ``percent``, and without the phrases ``per cent`` and
  ``phần trăm``, since numbers are compared on their own. The stored texts
  stay verbatim.
- Score: the share of the claim's distinct content words found in the
  candidate text (containment), stored rounded to 3 decimals. A candidate
  qualifies with a score of at least ``THRESHOLD`` (0.5) and at least
  ``MIN_SHARED_WORDS`` (2) shared words. An entity claim also needs one of
  its capitalised words (``capitalised_words``: capitalised where #068 sees
  an entity, so not the first word, not after a colon, an opening quote or
  bracket, and not ``I``; stop words such as ``US`` or ``WHO`` and words with
  digits such as ``G7`` count) among all the case folded words of the text.
  Comparison and absolute claims use words only.
- Numbers (``numbers``), for numeric claims, are read outside dates. A
  number is ``\\d{1,3}([.,]\\d{3})+`` (thousands groups; ``1.000`` and
  ``1,000`` are 1000, and a decimal part may follow with the other
  separator: ``1,234.5``, ``1.234,5``), ``\\d{1,3}( \\d{3})+`` (thousands
  groups separated by spaces: ``1 000 000``, so ``chapter 5 100`` also reads
  5100), or digits with an optional ``.`` or ``,`` decimal point (``1,5`` and
  ``1.5`` are 1.5). A number followed by another separator and digit
  (``1,234,5``, ``3.12.10``) is no number. A ``-`` or ``−`` directly before
  the digits, not after a letter or digit, makes it negative. ``%``,
  ``percent``, ``per cent`` and ``phần trăm`` make a percentage, which never
  equals a plain number. Up to two scale words multiply the value
  (``hundred(s)``, ``trăm``: 100; ``thousand(s)``, ``nghìn``, ``ngàn``:
  1,000; ``million(s)``, ``triệu``; ``billion(s)``, ``tỷ``, ``tỉ``;
  ``trillion(s)``), so ``2 billion`` is ``2.000.000.000`` and ``1 nghìn tỷ``
  is 10^12. Abbreviations count when attached to the digits: ``k`` (1,000),
  ``mn`` and ``tr`` (million), ``bn`` (billion); ``m`` (million) and ``b``
  (billion) only after a currency sign or code (``$5m``), as ``5m`` alone
  may be metres or bytes. Other currency signs and codes are ignored.
- Ranges: two numbers joined by ``-``, ``–``, ``to``, ``đến`` or ``tới``
  (``từ`` before them is read too) are one range; an end without a unit or
  scale takes the other end's (``5-7%`` is 5% to 7%). A single number is a
  range with equal ends. Two numbers agree when they have the same unit and
  their ranges overlap: a number equal to an end or inside a range agrees
  with it.
- Dates (``dates``), for date claims; a year from 1800 to 2099 is always a
  date, never a number, so ``2000 người`` is not compared as a number. The
  dates are: ISO dates (``2026-10-03``); numeric dates with ``/`` and a 2 or
  4-digit year (20xx) or with ``.`` and a 4-digit year, not after
  ``version``, ``v`` or ``phiên bản`` and not followed by another part; an
  English month with a day and/or a year (``3 October 2026``, ``October 3``,
  ``October 2026``; ``May`` only capitalised, so ``20 may apply`` stays a
  number); Vietnamese ``ngày`` with a day (``ngày 5``, ``ngày 3 tháng 10 năm
  2026``) and ``tháng`` with a month (``tháng 10/2026``, ``tháng 3 năm
  2020``), unless ``mỗi``, ``hằng``, ``hàng``, ``mọi``, ``một`` or ``cả``
  comes before (``mỗi ngày 8 ly`` keeps 8 as a number); and a year or its
  decade (``1990s``). Days must be 1 to 31 and months 1 to 12, else the text
  is no date. A numeric date is read day first; month first when the second
  number cannot be a month; when both numbers could be the month and differ
  (``03/04/2026``) either reading is allowed. Two dates agree when, for some
  reading, they share at least one part (year, month, day) and no part both
  give differs; a decade agrees with each of its years.
- ``numbers`` on a link: ``agree`` when the claim and the text share a number
  (numeric claims) or a date (date claims); ``differ`` when the text has
  numbers (or dates) but none is shared; ``none`` when the text has none,
  the claim has none, or the claim is of another kind. Differing numbers do
  not stop a link that qualifies on words: they are a neutral observation,
  not a verdict (the Fact Check Result, #070, owns verdicts).
- Each claim gets at most ``MAX_LINKS`` (3) links from distinct sources.
  Candidates are ranked by numbers that agree first, then by score (highest
  first), then by their order in the report; the best candidate of each
  source is kept.
- Cost: each report text is read once; each claim is compared with every
  candidate by set operations, at most 200 claims times the report's
  evidence entries. Each pattern looks only a few characters back or ahead
  at each position, so reading a text is linear in its length.
- ``EvidenceMatch`` is one stored run for one claim extraction run, with its
  links (``Evidence`` rows; zero is allowed) and counts. Without a research
  report the run has no report id and no link, and every claim is unmatched.
  ``ScriptRepository.list_evidence`` orders a claim's evidence by
  ``created_at`` and then insertion order (rowid; it was the id before #069),
  so the links of one run keep their rank.
"""

import re
import unicodedata
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from ai_youtube_agent.content.claim_extraction import MAX_CLAIMS, ClaimExtraction
from ai_youtube_agent.content.research_report import ResearchReport
from ai_youtube_agent.content.script import (
    Claim,
    ClaimKind,
    Evidence,
    NumberAgreement,
)
from ai_youtube_agent.content.similarity import words
from ai_youtube_agent.content.topic_extractor import STOP_WORDS
from ai_youtube_agent.core.audit import Actor

__all__ = [
    "MATCH_METHOD",
    "MAX_LINKS",
    "MIN_SHARED_WORDS",
    "THRESHOLD",
    "Candidate",
    "DateMention",
    "EvidenceMatch",
    "FoundLink",
    "MatchedClaims",
    "NumberAgreement",
    "NumberRange",
    "capitalised_words",
    "content_words",
    "dates",
    "match_claims",
    "numbers",
    "score_candidate",
]

Clock = Callable[[], datetime]

MATCH_METHOD = "rules-v1"
MAX_LINKS = 3
THRESHOLD = 0.5
MIN_SHARED_WORDS = 2
MAX_METHOD = 100


def _nfc(text: str) -> str:
    return unicodedata.normalize("NFC", text)


# Number words

_SCALES = {
    "hundred": 100,
    "hundreds": 100,
    "trăm": 100,
    "thousand": 10**3,
    "thousands": 10**3,
    "nghìn": 10**3,
    "ngàn": 10**3,
    "million": 10**6,
    "millions": 10**6,
    "triệu": 10**6,
    "billion": 10**9,
    "billions": 10**9,
    "tỷ": 10**9,
    "tỉ": 10**9,
    "trillion": 10**12,
    "trillions": 10**12,
}
_ABBREVIATIONS = {"k": 10**3, "mn": 10**6, "tr": 10**6, "bn": 10**9}
_CURRENCY_ABBREVIATIONS = {"m": 10**6, "b": 10**9}  # only after a currency
_NUMBER_WORDS = frozenset(_SCALES) | {"percent"}
_PERCENT_PHRASE = re.compile(r"(?<!\w)(?:per\s+cent|phần\s+trăm)(?!\w)", re.I)


# Words


def content_words(text: str) -> frozenset[str]:
    """The distinct content words of a text (see the module docstring)."""
    return frozenset(
        word
        for word in words(_PERCENT_PHRASE.sub(" ", _nfc(text)))
        if word not in STOP_WORDS
        and word not in _NUMBER_WORDS
        and not any(char.isdigit() for char in word)
    )


_WORD = re.compile(r"\w+")
_LIST_MARKER = re.compile(r"^\s*(?:[-*•]|\d{1,2}[.)])\s+")
_ENTITY_RESET = ':"“‘«(['  # the next word may be capitalised for another reason


def capitalised_words(text: str) -> frozenset[str]:
    """The words, case folded, that #068 would read as an entity."""
    text = _LIST_MARKER.sub("", _nfc(text), count=1)
    found = set()
    for index, match in enumerate(_WORD.finditer(text)):
        word = match.group()
        if index == 0 or not word[0].isupper() or word == "I":
            continue
        before = match.start() - 1
        while before >= 0 and text[before].isspace():
            before -= 1
        if before >= 0 and text[before] in _ENTITY_RESET:
            continue
        found.add(word.casefold())
    return frozenset(found)


# Dates


@dataclass(frozen=True)
class DateMention:
    """A date as written: any of its parts may be missing.

    ``ambiguous`` marks a numeric date whose two numbers could each be the
    month; it is stored day first and may be read either way.
    """

    year: int | None = None
    month: int | None = None
    day: int | None = None
    decade: bool = False
    ambiguous: bool = False

    def _readings(self) -> tuple[tuple[int | None, int | None], ...]:
        if self.ambiguous:
            return ((self.month, self.day), (self.day, self.month))
        return ((self.month, self.day),)

    def agrees(self, other: "DateMention") -> bool:
        """For some reading, they share a part and no part both give differs."""
        shared_year = False
        if self.year is not None and other.year is not None:
            if self.decade or other.decade:
                if self.year // 10 != other.year // 10:
                    return False
            elif self.year != other.year:
                return False
            shared_year = True
        for mine in self._readings():
            for theirs in other._readings():
                shared, conflict = shared_year, False
                for a, b in zip(mine, theirs, strict=True):
                    if a is not None and b is not None:
                        conflict = conflict or a != b
                        shared = True
                if shared and not conflict:
                    return True
        return False


_MONTH_NAMES = (
    "january",
    "february",
    "march",
    "april",
    "may",
    "june",
    "july",
    "august",
    "september",
    "october",
    "november",
    "december",
)
_MONTH = (
    "(?:"
    + "|".join(name for name in _MONTH_NAMES if name != "may")
    + r"|(?-i:May))(?!\w)"
)
_ORDINAL = r"(?:st|nd|rd|th)?"
_PER = r"(?<!mỗi\s)(?<!hằng\s)(?<!hàng\s)(?<!mọi\s)(?<!một\s)(?<!cả\s)(?<!\w)"
_DATES = re.compile(
    r"(?<![\d.,])(?P<iso_y>\d{4})-(?P<iso_m>\d{1,2})-(?P<iso_d>\d{1,2})(?!\d)"
    r"|(?<![\d.,/])(?P<sl_a>\d{1,2})/(?P<sl_b>\d{1,2})/"
    r"(?P<sl_y>\d{4}|\d{2})(?![./]?\d)"
    r"|(?<![\d.,/])(?P<dot_a>\d{1,2})\.(?P<dot_b>\d{1,2})\.(?P<dot_y>\d{4})"
    r"(?![./]?\d)"
    rf"|(?<![\d.,])(?P<dm_d>\d{{1,2}}){_ORDINAL}\s+(?:of\s+)?(?P<dm_m>{_MONTH})"
    r"(?:,?\s+(?P<dm_y>\d{4})(?!\d))?"
    rf"|(?<!\w)(?P<md_m>{_MONTH})\s+(?P<md_d>\d{{1,2}}){_ORDINAL}(?!\w)"
    r"(?:,?\s+(?P<md_y>\d{4})(?!\d))?"
    rf"|(?<!\w)(?P<my_m>{_MONTH})\s+(?P<my_y>\d{{4}})(?!\d)"
    rf"|{_PER}ngày\s+(?P<vd_d>\d{{1,2}})(?!\d|[.,/]\d)"
    r"(?:\s+tháng\s+(?P<vd_m>\d{1,2})(?!\d)(?:\s+năm\s+(?P<vd_y>\d{4})(?!\d))?)?"
    rf"|{_PER}tháng\s+(?P<vm_m>\d{{1,2}})(?!\d|[.,]\d)"
    r"(?:\s*/\s*(?P<vm_y>\d{4})(?!\d)|\s+năm\s+(?P<vm_y2>\d{4})(?!\d))?"
    r"|(?<![\d.,])(?P<y_y>(?:1[89]|20)\d{2})(?P<y_s>s)?(?!\d)",
    re.I,
)
_VERSION_BEFORE = re.compile(r"(?:(?<!\w)v|version|phiên\s+bản)\s*\Z", re.I)
_VERSION_WINDOW = 20


def _mention(text: str, match: re.Match[str]) -> DateMention | None:
    """The date of a match, or None when it is no valid date."""
    g = match.groupdict()

    def number(name: str) -> int | None:
        return int(g[name]) if g[name] is not None else None

    def month(name: str) -> int | None:
        name_text = g[name]
        return _MONTH_NAMES.index(name_text.casefold()) + 1 if name_text else None

    if g["iso_y"] is not None:
        return _valid(number("iso_y"), number("iso_m"), number("iso_d"))
    for prefix in ("sl", "dot"):
        if g[f"{prefix}_a"] is not None:
            window = text[max(0, match.start() - _VERSION_WINDOW) : match.start()]
            if _VERSION_BEFORE.search(window):
                return None
            year = number(f"{prefix}_y")
            if year < 100:
                year += 2000
            return _numeric(year, number(f"{prefix}_a"), number(f"{prefix}_b"))
    if g["dm_d"] is not None:
        return _valid(number("dm_y"), month("dm_m"), number("dm_d"))
    if g["md_m"] is not None:
        return _valid(number("md_y"), month("md_m"), number("md_d"))
    if g["my_m"] is not None:
        return _valid(number("my_y"), month("my_m"), None)
    if g["vd_d"] is not None:
        return _valid(number("vd_y"), number("vd_m"), number("vd_d"))
    if g["vm_m"] is not None:
        return _valid(number("vm_y") or number("vm_y2"), number("vm_m"), None)
    return DateMention(number("y_y"), decade=g["y_s"] is not None)


def _numeric(year: int, first: int, second: int) -> DateMention | None:
    """A numeric date: day first, month first when the second cannot be one."""
    if second > 12 >= first:
        return _valid(year, first, second)
    if first <= 12 and second <= 12 and first != second:
        mention = _valid(year, second, first)
        return DateMention(year, second, first, ambiguous=True) if mention else None
    return _valid(year, second, first)


def _valid(year: int | None, month: int | None, day: int | None) -> DateMention | None:
    if month is not None and not 1 <= month <= 12:
        return None
    if day is not None and not 1 <= day <= 31:
        return None
    return DateMention(year, month, day)


def _dated(text: str) -> list[tuple[re.Match[str], DateMention]]:
    """The valid dates of an NFC text with their matches."""
    found = []
    for match in _DATES.finditer(text):
        mention = _mention(text, match)
        if mention is not None:
            found.append((match, mention))
    return found


def dates(text: str) -> frozenset[DateMention]:
    """The dates of a text (see the module docstring)."""
    return frozenset(mention for _, mention in _dated(_nfc(text)))


# Numbers

NumberRange = tuple[Decimal, Decimal, str]  # low, high and unit ("%" or "")

_SCALE = "(?:" + "|".join(sorted(_SCALES, key=len, reverse=True)) + ")"
_ABBREVIATION = (
    "(?:"
    + "|".join(
        sorted([*_ABBREVIATIONS, *_CURRENCY_ABBREVIATIONS], key=len, reverse=True)
    )
    + ")"
)
_GROUP_SPACE = "[   ]"
_COMMA_THOUSANDS = re.compile(r"\d{1,3}(?:,\d{3})+(?:\.\d+)?")
_DOT_THOUSANDS = re.compile(r"\d{1,3}(?:\.\d{3})+(?:,\d+)?")
_SPACE_THOUSANDS = re.compile(rf"\d{{1,3}}(?:{_GROUP_SPACE}\d{{3}})+(?:[.,]\d+)?")


def _end(p: str) -> str:
    """One number of a range, its groups named with the prefix ``p``."""
    return (
        rf"(?:(?<![\w.,])(?P<{p}_neg>[-−]))?"
        rf"(?<![\w.,])(?P<{p}_value>"
        r"\d{1,3}(?:,\d{3})+(?:\.\d+)?"
        r"|\d{1,3}(?:\.\d{3})+(?:,\d+)?"
        rf"|\d{{1,3}}(?:{_GROUP_SPACE}\d{{3}})+(?:[.,]\d+)?"
        r"|\d+(?:[.,]\d+)?"
        r")(?![.,]?\d)"
        rf"(?:\s*(?P<{p}_sign>%)"
        rf"|\s*(?P<{p}_word>percent|per\s+cent|phần\s+trăm)(?!\w)"
        rf"|\s*(?P<{p}_scale>{_SCALE}(?:\s+{_SCALE})?)(?!\w)"
        rf"|(?P<{p}_abbr>{_ABBREVIATION})(?!\w))?"
    )


_NUMBER = re.compile(
    r"(?:(?<!\w)từ\s+)?"
    + _end("lo")
    + r"(?:(?:\s*[-–]\s*|\s+(?:to|đến|tới)\s+)"
    + _end("hi")
    + ")?",
    re.I,
)
_SPACES = re.compile(r"\s+")
_CURRENCY_BEFORE = re.compile(r"(?:[$€£¥₫₹]|(?<!\w)(?:USD|EUR|GBP|JPY|VND))\s?\Z")


def _value(text: str) -> Decimal:
    if _COMMA_THOUSANDS.fullmatch(text):
        return Decimal(text.replace(",", ""))
    if _DOT_THOUSANDS.fullmatch(text):
        return Decimal(text.replace(".", "").replace(",", "."))
    if _SPACE_THOUSANDS.fullmatch(text):
        return Decimal(_SPACES.sub("", text).replace(",", "."))
    return Decimal(text.replace(",", "."))


def _suffix(match: re.Match[str], p: str, currency: bool) -> tuple[int, str] | None:
    """The multiplier and unit written after one end, or None without one."""
    if match.group(f"{p}_sign") or match.group(f"{p}_word"):
        return 1, "%"
    if match.group(f"{p}_scale"):
        multiplier = 1
        for word in _SPACES.split(match.group(f"{p}_scale")):
            multiplier *= _SCALES[word.casefold()]
        return multiplier, ""
    abbreviation = (match.group(f"{p}_abbr") or "").casefold()
    if abbreviation in _ABBREVIATIONS:
        return _ABBREVIATIONS[abbreviation], ""
    if abbreviation and currency:
        return _CURRENCY_ABBREVIATIONS[abbreviation], ""
    return None


def _number(match: re.Match[str], p: str, suffix: tuple[int, str]) -> Decimal:
    value = _value(match.group(f"{p}_value")) * suffix[0]
    return -value if match.group(f"{p}_neg") else value


def numbers(text: str) -> frozenset[NumberRange]:
    """The numbers of a text outside its dates (see the module docstring)."""
    text = _nfc(text)
    parts, last = [], 0
    for match, _ in _dated(text):
        parts += [text[last : match.start()], " "]
        last = match.end()
    text = "".join([*parts, text[last:]])
    found = set()
    for match in _NUMBER.finditer(text):
        before = text[max(0, match.start("lo_value") - 6) : match.start("lo_value")]
        currency = _CURRENCY_BEFORE.search(before.rstrip("-−")) is not None
        low = _suffix(match, "lo", currency)
        if match.group("hi_value") is None:
            value = _number(match, "lo", low or (1, ""))
            found.add((value, value, (low or (1, ""))[1]))
            continue
        high = _suffix(match, "hi", currency)
        low, high = low or high or (1, ""), high or low or (1, "")
        ends = sorted((_number(match, "lo", low), _number(match, "hi", high)))
        if low[1] == high[1]:
            found.add((ends[0], ends[1], low[1]))
        else:  # different units make two numbers, not a range
            found.add((_number(match, "lo", low),) * 2 + (low[1],))
            found.add((_number(match, "hi", high),) * 2 + (high[1],))
    return frozenset(found)


def _overlap(first: NumberRange, second: NumberRange) -> bool:
    return first[2] == second[2] and first[0] <= second[1] and second[0] <= first[1]


# Scoring


@dataclass(frozen=True)
class _Text:
    """What matching reads from one text, computed once."""

    words: frozenset[str]
    all_words: frozenset[str]
    numbers: frozenset[NumberRange]
    dates: frozenset[DateMention]

    @classmethod
    def of(cls, text: str) -> "_Text":
        return cls(
            content_words(text),
            frozenset(words(_nfc(text))),
            numbers(text),
            dates(text),
        )


@dataclass(frozen=True)
class Candidate:
    """How one evidence text compares with one claim."""

    ratio: float
    shared_words: int
    numbers: NumberAgreement
    qualifies: bool

    @property
    def score(self) -> float:
        return round(self.ratio, 3)

    def rank(self) -> tuple[bool, bool, float]:
        """Larger is better: qualifying, then numbers that agree, then score."""
        return (self.qualifies, self.numbers is NumberAgreement.AGREE, self.ratio)


def _agreement(kind: ClaimKind | None, claim: _Text, text: _Text) -> NumberAgreement:
    if kind is ClaimKind.NUMERIC:
        if not claim.numbers or not text.numbers:
            return NumberAgreement.NONE
        shared = any(_overlap(a, b) for a in claim.numbers for b in text.numbers)
    elif kind is ClaimKind.DATE:
        if not claim.dates or not text.dates:
            return NumberAgreement.NONE
        shared = any(a.agrees(b) for a in claim.dates for b in text.dates)
    else:
        return NumberAgreement.NONE
    return NumberAgreement.AGREE if shared else NumberAgreement.DIFFER


def _score(
    kind: ClaimKind | None,
    claim: _Text,
    capitals: frozenset[str],
    text: _Text,
) -> Candidate:
    shared = claim.words & text.words
    ratio = len(shared) / len(claim.words) if claim.words else 0.0
    qualifies = ratio >= THRESHOLD and len(shared) >= MIN_SHARED_WORDS
    if kind is ClaimKind.ENTITY and not capitals & text.all_words:
        qualifies = False
    return Candidate(ratio, len(shared), _agreement(kind, claim, text), qualifies)


def score_candidate(claim: Claim, text: str) -> Candidate:
    """How ``text`` compares with ``claim`` by the rules of the docstring."""
    capitals = (
        capitalised_words(claim.text) if claim.kind is ClaimKind.ENTITY else frozenset()
    )
    return _score(claim.kind, _Text.of(claim.text), capitals, _Text.of(text))


# Matching


@dataclass(frozen=True)
class FoundLink:
    claim_id: str
    source_id: str
    research_claim_id: str
    excerpt: str
    score: float
    numbers: NumberAgreement


@dataclass(frozen=True)
class MatchedClaims:
    links: tuple[FoundLink, ...]
    claims: int
    matched: int


@dataclass(frozen=True)
class _Entry:
    """One evidence entry of a report claim, read once."""

    research_claim_id: str
    source_id: str
    excerpt: str
    texts: tuple[_Text, ...]  # the quote first, when there is one


def _entries(report: ResearchReport | None) -> list[_Entry]:
    if report is None:
        return []
    entries = []
    for research_claim in report.claims:
        for evidence in research_claim.evidence:
            quote = evidence.quote if (evidence.quote or "").strip() else None
            texts = (quote, evidence.note) if quote else (evidence.note,)
            entries.append(
                _Entry(
                    research_claim.id,
                    evidence.source_id,
                    quote or evidence.note,
                    tuple(_Text.of(text) for text in texts),
                )
            )
    return entries


def _links(claim: Claim, entries: Sequence[_Entry]) -> list[FoundLink]:
    mine = _Text.of(claim.text)
    capitals = (
        capitalised_words(claim.text) if claim.kind is ClaimKind.ENTITY else frozenset()
    )
    ranked: list[tuple[Candidate, int, _Entry]] = []
    for order, entry in enumerate(entries):
        best = None
        for text in entry.texts:
            candidate = _score(claim.kind, mine, capitals, text)
            if best is None or candidate.rank() > best.rank():
                best = candidate
        if best is not None and best.qualifies:
            ranked.append((best, order, entry))
    ranked.sort(
        key=lambda item: (
            item[0].numbers is not NumberAgreement.AGREE,
            -item[0].ratio,
            item[1],
        )
    )
    links: list[FoundLink] = []
    sources: set[str] = set()
    for candidate, _, entry in ranked:
        if entry.source_id in sources:
            continue
        sources.add(entry.source_id)
        links.append(
            FoundLink(
                claim.id,
                entry.source_id,
                entry.research_claim_id,
                entry.excerpt,
                candidate.score,
                candidate.numbers,
            )
        )
        if len(links) == MAX_LINKS:
            break
    return links


def match_claims(
    claims: Sequence[Claim], report: ResearchReport | None
) -> MatchedClaims:
    """The evidence links of ``claims`` in ``report`` (none without one)."""
    entries = _entries(report)
    links: list[FoundLink] = []
    matched = 0
    for claim in claims:
        found = _links(claim, entries)
        matched += bool(found)
        links.extend(found)
    return MatchedClaims(tuple(links), len(claims), matched)


# The stored run


@dataclass(frozen=True)
class EvidenceMatch:
    id: str
    extraction_id: str
    script_id: str
    content_item_id: str
    research_report_id: str | None
    method: str
    links: tuple[Evidence, ...]
    claims_count: int
    matched: int
    unmatched: int
    numbers_differ: int
    requested_by: Actor
    created_at: datetime

    def __post_init__(self) -> None:
        for name in ("id", "extraction_id", "script_id", "content_item_id"):
            if not getattr(self, name):
                raise ValueError(f"{name} must not be empty")
        if self.research_report_id is not None and not self.research_report_id.strip():
            raise ValueError("research_report_id must not be empty")
        if (
            not isinstance(self.method, str)
            or not self.method.strip()
            or len(self.method) > MAX_METHOD
        ):
            raise ValueError(f"a method is 1 to {MAX_METHOD} characters")
        for name in ("claims_count", "matched", "unmatched", "numbers_differ"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a whole number of 0 or more")
        if self.claims_count > MAX_CLAIMS:
            raise ValueError(f"at most {MAX_CLAIMS} claims")
        if self.matched + self.unmatched != self.claims_count:
            raise ValueError("every claim is either matched or unmatched")
        self._check_links()
        if not isinstance(self.requested_by, Actor):
            raise TypeError("requested_by must be an Actor")
        if not isinstance(
            self.created_at, datetime
        ) or self.created_at.utcoffset() != timedelta(0):
            raise ValueError("created_at must be timezone-aware UTC")

    def _check_links(self) -> None:
        per_claim: dict[str, set[str]] = {}
        for link in self.links:
            if not isinstance(link, Evidence):
                raise TypeError("links must be Evidence values")
            if link.match_id != self.id:
                raise ValueError("every link must belong to this run")
            if (
                link.research_claim_id is None
                or link.score is None
                or link.score < THRESHOLD
                or link.numbers is None
            ):
                raise ValueError(
                    "a matched link has a research claim, a score of at least "
                    f"{THRESHOLD} and a numbers comparison"
                )
            sources = per_claim.setdefault(link.claim_id, set())
            if link.source_ref in sources:
                raise ValueError("the links of a claim come from distinct sources")
            sources.add(link.source_ref)
            if len(sources) > MAX_LINKS:
                raise ValueError(f"at most {MAX_LINKS} links per claim")
        if len(per_claim) != self.matched:
            raise ValueError("matched must count the claims with a link")
        differ = sum(link.numbers is NumberAgreement.DIFFER for link in self.links)
        if differ != self.numbers_differ:
            raise ValueError("numbers_differ must count the links that differ")
        if self.research_report_id is None and self.links:
            raise ValueError("a run without a research report has no links")

    @property
    def links_count(self) -> int:
        return len(self.links)

    @property
    def no_report(self) -> bool:
        return self.research_report_id is None

    @classmethod
    def create(
        cls,
        extraction: ClaimExtraction,
        research_report_id: str | None,
        matched: MatchedClaims,
        *,
        method: str = MATCH_METHOD,
        requested_by: Actor,
        clock: Clock | None = None,
    ) -> "EvidenceMatch":
        """A run for ``extraction``; its links share the run's time."""
        match_id = uuid.uuid4().hex
        now = clock() if clock else datetime.now(UTC)
        links = tuple(
            Evidence.create(
                link.claim_id,
                link.source_id,
                link.excerpt,
                match_id=match_id,
                research_claim_id=link.research_claim_id,
                score=link.score,
                numbers=link.numbers,
                clock=lambda: now,
            )
            for link in matched.links
        )
        return cls(
            id=match_id,
            extraction_id=extraction.id,
            script_id=extraction.script_id,
            content_item_id=extraction.content_item_id,
            research_report_id=research_report_id,
            method=method,
            links=links,
            claims_count=matched.claims,
            matched=matched.matched,
            unmatched=matched.claims - matched.matched,
            numbers_differ=sum(
                link.numbers is NumberAgreement.DIFFER for link in links
            ),
            requested_by=requested_by,
            created_at=now,
        )

    def counts(self) -> dict[str, int | bool]:
        """The scalar counts of the run, as audited."""
        return {
            "claims": self.claims_count,
            "matched": self.matched,
            "unmatched": self.unmatched,
            "links": self.links_count,
            "numbers_differ": self.numbers_differ,
            "no_report": self.no_report,
        }
