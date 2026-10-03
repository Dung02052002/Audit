"""Script validation rules and runs (Prompt Pack v8, prompt #072), context C5.

The rules were approved by the user on 2026-10-04. A script validation is
deterministic, with no AI model; the run records its method
(``SCRIPT_VALIDATION_METHOD`` = ``"script-rules-v1"``) so another method can be
added later. It checks one stored script version against the current strategy
of its channel, in this order (the order of the findings):

1. Sections (FAIL, a strict set in a strict order). Shorts are one HOOK, 1 to 3
   BODY and one CTA. LongForm is one HOOK, one INTRO, 3 to 12 chapter sections,
   one OUTRO and one CTA, where a chapter section is a CHAPTER (always titled:
   ``ScriptSection`` refuses a chapter without a title) when the strategy
   format has ``longform.chapters`` on, and a BODY when it is off. The shapes
   are those the Shorts (#066) and LongForm (#067) generators write, so a
   script they write passes. Codes:

   - ``missing_section``: a required kind is absent (one finding per kind;
     for Shorts, no BODY at all);
   - ``unexpected_section``: a section of a kind the type does not have, a
     second HOOK, INTRO, OUTRO or CTA, or a fourth Shorts BODY
     (``section_index`` = the section);
   - ``chapter_count`` (LongForm): fewer than 3 or more than 12 chapter
     sections (``actual``, ``minimum``, ``maximum`` = the count and the range);
   - ``chapter_kind`` (LongForm): every chapter-position section has the other
     kind (CHAPTER sections while chapters are off, BODY sections while they
     are on) and there are 3 to 12 of them: one finding instead of an
     ``unexpected_section`` for each and a ``chapter_count`` (``actual`` = how
     many, ``section_index`` = the first). Mixed kinds, or a count outside 3 to
     12, keep the codes above;
   - ``wrong_order``: the sections kept by the rules above are not in the order
     of the type (``section_index`` = the first section that is out of place in
     the surviving sequence, which is not always the CTA: [HOOK, CTA, BODY]
     gives the BODY, index 2). A HOOK that is not first and a CTA that is not
     last are found here.

   The kind of a missing or unexpected section is not stored: the script is
   immutable, so it can be read from the script itself.
2. Length (FAIL ``too_short`` or ``too_long``): ``Script.estimated_seconds``
   (the section ``seconds``, else 150 words per minute) against the script's
   own ``duration_target``, or, when it has none, the duration range of the
   content type in the current strategy format. The limits are inclusive.
   ``actual``, ``minimum`` and ``maximum`` are seconds.
3. Language (``language_mismatch``, FAIL or WARN). A deterministic EN/VI
   heuristic with its own word lists (``EN_WORDS``, ``VI_WORDS``): text is NFC,
   then casefold; a word is an English signal if it is an English function
   word, a Vietnamese signal if it is a Vietnamese function word or has a
   letter that is distinctive of Vietnamese (``VI_LETTERS``); words in both
   lists are dropped from both (do, no, me, an and ai: "AI" is an English
   term and "ai" a Vietnamese word, so it tells nothing). English terms inside
   Vietnamese text carry no signal, so code-switching is not a mismatch. At
   most ``MAX_WORDS`` (20,000) words are read (``MAX_CHARS`` characters), all
   sections, in order. The
   allowed languages are the base subtags of the strategy primary and
   secondary languages. The language is not checked (no finding, and the run
   records ``language_checked`` false) when no allowed language is ``en`` or
   ``vi``, or when there are fewer than ``MIN_HITS`` (10) signal hits. Else,
   when a known language that is not allowed holds 80% or more of the hits it
   is a FAIL, 60% to 80% a WARN (``score`` = its share rounded down to 3
   decimals, ``matched`` = its hits, ``actual`` = all hits).
4. Banned phrases (FAIL ``banned_phrase``): the brand banned phrases of the
   strategy (``find_banned_phrases``) in the text or title of a section: one
   finding per section, ``matched`` = how many distinct phrases, never the
   phrase.
5. Hook length (WARN ``hook_too_long``): the first HOOK section against
   ``HOOK_LIMITS`` of the content type (``check_hook``): one finding for each
   exceeded limit, ``actual`` and ``maximum`` being the words or the sentences
   (the maximum tells which: Shorts 15 words and 2 sentences, LongForm 60 and
   3).

LongForm scripts are validated even while ``LONGFORM_ENABLED`` is off. The
findings keep ids, counts, scores and seconds, never any script text.
``ScriptValidation`` is one stored run for one script, with its findings and
counts; its ``status`` is derived: the worst of its findings (FAIL, then WARN,
then PASS; no finding is PASS). A run where the language was not checked is not
a language pass: ``language_checked`` says so. A result is a record only: it
blocks nothing, and it is never changed or overridden. A later task may add a
separate table of overrides. ``strategy_version`` is the version of the
strategy the rules read; ``script_strategy_version`` is the one the script was
written from.
"""

import itertools
import unicodedata
import uuid
from collections import Counter
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum

from ai_youtube_agent.content.hook import (
    HOOK_LIMITS,
    HookIssueCode,
    check_hook,
    count_sentences,
    count_words,
    find_banned_phrases,
)
from ai_youtube_agent.content.script import (
    MAX_SECTIONS,
    DurationTarget,
    Script,
    SectionKind,
)
from ai_youtube_agent.content.similarity import WORD_PATTERN
from ai_youtube_agent.content.strategy import StrategyProfile
from ai_youtube_agent.content.text_prompts import banned_phrases
from ai_youtube_agent.core.audit import Actor
from ai_youtube_agent.core.content_item import ContentType

__all__ = [
    "EN_WORDS",
    "MAX_FINDINGS",
    "MAX_WORDS",
    "MIN_HITS",
    "SCRIPT_VALIDATION_METHOD",
    "VI_LETTERS",
    "VI_WORDS",
    "Finding",
    "ScriptAnalysis",
    "ScriptValidation",
    "ValidationCode",
    "ValidationStatus",
    "validate_script",
]

Clock = Callable[[], datetime]

SCRIPT_VALIDATION_METHOD = "script-rules-v1"
MAX_METHOD = 100
MAX_WORDS = 20_000  # words read for the language
MAX_CHARS = MAX_WORDS * 40  # characters read for the language, before the cap
MIN_HITS = 10
# Percentages, compared with whole numbers so a boundary is exact.
LANGUAGE_FAIL_PERCENT = 80
LANGUAGE_WARN_PERCENT = 60
SHORTS_MIN_BODY = 1
SHORTS_MAX_BODY = 3
LONGFORM_MIN_CHAPTERS = 3
LONGFORM_MAX_CHAPTERS = 12
# At most one unexpected section and one banned phrase finding per section,
# 4 missing kinds, the chapter count or kind, the order, the length, the
# language and 2 hook limits.
MAX_FINDINGS = 2 * MAX_SECTIONS + 10


class ValidationStatus(StrEnum):
    PASS = "pass"
    WARN = "warn"
    FAIL = "fail"


class ValidationCode(StrEnum):
    """The rule that gave a finding."""

    MISSING_SECTION = "missing_section"
    UNEXPECTED_SECTION = "unexpected_section"
    CHAPTER_COUNT = "chapter_count"
    CHAPTER_KIND = "chapter_kind"
    WRONG_ORDER = "wrong_order"
    TOO_SHORT = "too_short"
    TOO_LONG = "too_long"
    LANGUAGE_MISMATCH = "language_mismatch"
    BANNED_PHRASE = "banned_phrase"
    HOOK_TOO_LONG = "hook_too_long"


CODE = ValidationCode
PASS, WARN, FAIL = ValidationStatus.PASS, ValidationStatus.WARN, ValidationStatus.FAIL

# The statuses a code may have: only the language and the hook are not FAIL.
_STATUSES = {
    CODE.MISSING_SECTION: {FAIL},
    CODE.UNEXPECTED_SECTION: {FAIL},
    CODE.CHAPTER_COUNT: {FAIL},
    CODE.CHAPTER_KIND: {FAIL},
    CODE.WRONG_ORDER: {FAIL},
    CODE.TOO_SHORT: {FAIL},
    CODE.TOO_LONG: {FAIL},
    CODE.LANGUAGE_MISMATCH: {WARN, FAIL},
    CODE.BANNED_PHRASE: {FAIL},
    CODE.HOOK_TOO_LONG: {WARN},
}
_LENGTH_CODES = (CODE.TOO_SHORT, CODE.TOO_LONG)


# The language word lists (NFC, lower case). Words in both are dropped from both:
# do, no, me and an are words of both languages, and ai is the Vietnamese "who"
# and the English "AI" (an English term in Vietnamese text and in English text).

_EN = """
the and of to a in is it you that he was for on are with as i his they be at this
from or had by not but what all were we when your can there an which she how their
if will up about out then them these so some her would into has more could my than
who its now been have do no me our us just because also very most much only over
any new ai
"""
_VI = """
và là của có không cho được một những các này đó đây kia trong với để khi như nhưng
nếu thì mà rất cũng đã sẽ đang tôi bạn chúng ta họ anh chị em ở từ đến ra vào lại
còn hơn nhiều ít bởi vì nên rằng sao gì nào đâu ai thế vậy hay hoặc cả mọi mỗi người
việc làm biết muốn cần phải hôm nay ngày năm tháng về theo sau trước trên dưới giữa
tại qua cùng nhau mình ông bà cô chú rồi vẫn chỉ đều luôn thật quá lắm hãy đừng chưa
xin vâng ạ nhé nhỉ ơi ừ
do no me an ai
"""


def _words(text: str) -> frozenset[str]:
    return frozenset(unicodedata.normalize("NFC", text).casefold().split())


_RAW_EN, _RAW_VI = _words(_EN), _words(_VI)
EN_WORDS = _RAW_EN - _RAW_VI
VI_WORDS = _RAW_VI - _RAW_EN
# Letters distinctive of Vietnamese. The letters it shares with other Latin
# languages (a, e, i, o, u, y with an acute, grave or tilde; the circumflex
# vowels without a second mark) are left out.
VI_LETTERS = frozenset(
    unicodedata.normalize(
        "NFC",
        "ăắằẳẵặấầẩẫậđếềểễệốồổỗộơớờởỡợưứừửữựạảẹẻẽịỉĩọỏụủũỳỵỷỹ",
    )
)
_KNOWN = ("en", "vi")


def _whole(name: str, value: object, low: int, high: int | None = None) -> None:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < low
        or (high is not None and value > high)
    ):
        limit = f"{low} or more" if high is None else f"{low} to {high}"
        raise ValueError(f"{name} must be a whole number of {limit}")


@dataclass(frozen=True)
class Finding:
    """One thing the rules found, with ids, numbers and a score only."""

    code: ValidationCode
    status: ValidationStatus
    section_index: int | None = None
    actual: int | None = None
    minimum: int | None = None
    maximum: int | None = None
    score: float | None = None
    matched: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.code, ValidationCode):
            raise TypeError("code must be a ValidationCode")
        if not isinstance(self.status, ValidationStatus):
            raise TypeError("status must be a ValidationStatus")
        if self.status not in _STATUSES[self.code]:
            raise ValueError(f"the code {self.code.value} is not {self.status.value}")
        for name in ("section_index", "actual", "minimum", "maximum", "matched"):
            value = getattr(self, name)
            if value is not None:
                _whole(name, value, 0)
        if (
            self.minimum is not None
            and self.maximum is not None
            and self.minimum > self.maximum
        ):
            raise ValueError("minimum must not be greater than maximum")
        if self.score is not None and (
            isinstance(self.score, bool)
            or not isinstance(self.score, int | float)
            or not 0 <= self.score <= 1
        ):
            raise ValueError("score must be a number from 0 to 1")


# Reading a script


@dataclass(frozen=True)
class _Plan:
    """The sections of a content type: their order and the repeating kind."""

    order: tuple[SectionKind, ...]
    repeated: SectionKind
    extra_after: int | None  # a repeated section after this many is unexpected


def _plan(content_type: ContentType, strategy: StrategyProfile) -> _Plan:
    if content_type is ContentType.SHORTS:
        return _Plan(
            (SectionKind.HOOK, SectionKind.BODY, SectionKind.CTA),
            SectionKind.BODY,
            SHORTS_MAX_BODY,
        )
    chapter = (
        SectionKind.CHAPTER if strategy.format.longform.chapters else SectionKind.BODY
    )
    return _Plan(
        (
            SectionKind.HOOK,
            SectionKind.INTRO,
            chapter,
            SectionKind.OUTRO,
            SectionKind.CTA,
        ),
        chapter,
        None,
    )


def _other_chapter_kind(plan: _Plan, content_type: ContentType) -> SectionKind | None:
    """The kind a LongForm chapter-position section has when the strategy says
    the other (CHAPTER and BODY); ``None`` for Shorts."""
    if content_type is not ContentType.LONGFORM:
        return None
    if plan.repeated is SectionKind.CHAPTER:
        return SectionKind.BODY
    return SectionKind.CHAPTER


def _section_findings(
    script: Script, content_type: ContentType, strategy: StrategyProfile
) -> list[Finding]:
    plan = _plan(content_type, strategy)
    rank = {kind: position for position, kind in enumerate(plan.order)}
    seen: Counter[SectionKind] = Counter()
    other = _other_chapter_kind(plan, content_type)
    other_at: list[int] = []  # the sections of the other chapter kind
    unexpected: list[Finding] = []
    kept: list[tuple[int, int]] = []  # (section index, rank)
    for index, section in enumerate(script.sections):
        kind = section.kind
        if kind not in rank:
            unexpected.append(Finding(CODE.UNEXPECTED_SECTION, FAIL, index))
            if kind is other:
                other_at.append(index)
            continue
        seen[kind] += 1
        limit = plan.extra_after if kind is plan.repeated else 1
        if limit is not None and seen[kind] > limit:
            unexpected.append(Finding(CODE.UNEXPECTED_SECTION, FAIL, index))
            continue
        kept.append((index, rank[kind]))
    findings = [
        Finding(CODE.MISSING_SECTION, FAIL)
        for kind in plan.order
        if not seen[kind]
        and (kind is not plan.repeated or content_type is ContentType.SHORTS)
    ]
    count = seen[plan.repeated]
    wrong_kind = (
        not count and LONGFORM_MIN_CHAPTERS <= len(other_at) <= LONGFORM_MAX_CHAPTERS
    )
    if wrong_kind:
        flagged = set(other_at)
        unexpected = [f for f in unexpected if f.section_index not in flagged]
    findings += unexpected
    if wrong_kind:
        findings.append(
            Finding(CODE.CHAPTER_KIND, FAIL, other_at[0], actual=len(other_at))
        )
    elif (
        content_type is ContentType.LONGFORM
        and not LONGFORM_MIN_CHAPTERS <= count <= LONGFORM_MAX_CHAPTERS
    ):
        findings.append(
            Finding(
                CODE.CHAPTER_COUNT,
                FAIL,
                actual=count,
                minimum=LONGFORM_MIN_CHAPTERS,
                maximum=LONGFORM_MAX_CHAPTERS,
            )
        )
    highest = -1
    for index, position in kept:
        if position < highest:
            findings.append(Finding(CODE.WRONG_ORDER, FAIL, index))
            break
        highest = position
    return findings


def _length_findings(
    script: Script, content_type: ContentType, strategy: StrategyProfile
) -> list[Finding]:
    target = script.duration_target or DurationTarget.from_format(
        strategy.format, content_type
    )
    seconds = script.estimated_seconds
    if target.contains(seconds):
        return []
    code = CODE.TOO_SHORT if seconds < target.min_seconds else CODE.TOO_LONG
    return [
        Finding(
            code,
            FAIL,
            actual=seconds,
            minimum=target.min_seconds,
            maximum=target.max_seconds,
        )
    ]


def _signal_words(script: Script) -> Iterator[str]:
    """At most ``MAX_WORDS`` words of the script, NFC and lower case."""
    left = MAX_WORDS
    chars = MAX_CHARS
    for section in script.sections:
        if left <= 0 or chars <= 0:
            return
        read = section.text[:chars]
        chars -= len(read)
        text = unicodedata.normalize("NFC", read)
        for match in itertools.islice(WORD_PATTERN.finditer(text), left):
            left -= 1
            yield match.group().casefold()


def _hits(script: Script) -> dict[str, int]:
    """The English and Vietnamese signal words of the script."""
    hits = {"en": 0, "vi": 0}
    for word in _signal_words(script):
        if word in EN_WORDS:
            hits["en"] += 1
        elif word in VI_WORDS or not VI_LETTERS.isdisjoint(word):
            hits["vi"] += 1
    return hits


def _allowed_languages(strategy: StrategyProfile) -> set[str]:
    tags = (strategy.languages.primary, *strategy.languages.secondary)
    return {tag.split("-")[0].lower() for tag in tags}


def _share(numerator: int, denominator: int) -> float:
    """The share rounded down to 3 decimals, with integers: never up to a limit."""
    return numerator * 1000 // denominator / 1000


def _language(script: Script, strategy: StrategyProfile) -> tuple[int, list[Finding]]:
    """The signal hits read (0 when not read) and the language finding."""
    allowed = _allowed_languages(strategy)
    if allowed.isdisjoint(_KNOWN):
        return 0, []
    hits = _hits(script)
    total = sum(hits.values())
    if total < MIN_HITS:
        return total, []
    for language in _KNOWN:
        if language in allowed:
            continue
        found = hits[language]
        if found * 100 >= LANGUAGE_FAIL_PERCENT * total:
            status = FAIL
        elif found * 100 >= LANGUAGE_WARN_PERCENT * total:
            status = WARN
        else:
            continue
        return total, [
            Finding(
                CODE.LANGUAGE_MISMATCH,
                status,
                actual=total,
                score=_share(found, total),
                matched=found,
            )
        ]
    return total, []


def _banned_findings(script: Script, strategy: StrategyProfile) -> list[Finding]:
    banned = banned_phrases(strategy)
    if not banned:
        return []
    findings = []
    for index, section in enumerate(script.sections):
        # The title and the text are scanned apart, as the generators do: a
        # phrase never runs from the end of the title into the text.
        found = set(find_banned_phrases(section.text, banned))
        if section.title is not None:
            found.update(find_banned_phrases(section.title, banned))
        if found:
            findings.append(
                Finding(CODE.BANNED_PHRASE, FAIL, index, matched=len(found))
            )
    return findings


def _hook_findings(script: Script, content_type: ContentType) -> list[Finding]:
    index = next(
        (i for i, s in enumerate(script.sections) if s.kind is SectionKind.HOOK), None
    )
    if index is None:
        return []
    text = script.sections[index].text
    issues = {issue.code for issue in check_hook(text, content_type)}
    limits = HOOK_LIMITS[content_type]
    findings = []
    if HookIssueCode.TOO_MANY_WORDS in issues:
        findings.append(
            Finding(
                CODE.HOOK_TOO_LONG,
                WARN,
                index,
                actual=count_words(text),
                maximum=limits.max_words,
            )
        )
    if HookIssueCode.TOO_MANY_SENTENCES in issues:
        findings.append(
            Finding(
                CODE.HOOK_TOO_LONG,
                WARN,
                index,
                actual=count_sentences(text),
                maximum=limits.max_sentences,
            )
        )
    return findings


@dataclass(frozen=True)
class ScriptAnalysis:
    """What the rules found in one script, before it is stored."""

    words: int
    seconds: int
    language_checked: bool
    language_hits: int
    strategy_version: int
    findings: tuple[Finding, ...]


def validate_script(
    script: Script, content_type: ContentType, strategy: StrategyProfile
) -> ScriptAnalysis:
    """The findings for ``script`` of ``content_type``, by the module docstring.

    ``strategy`` must have a language and a format (``ValueError`` otherwise).
    """
    if strategy.languages is None or strategy.format is None:
        raise ValueError("the strategy needs a language and a format")
    findings = _section_findings(script, content_type, strategy)
    findings += _length_findings(script, content_type, strategy)
    hits, language = _language(script, strategy)
    findings += language
    findings += _banned_findings(script, strategy)
    findings += _hook_findings(script, content_type)
    return ScriptAnalysis(
        words=sum(section.words for section in script.sections),
        seconds=script.estimated_seconds,
        language_checked=hits >= MIN_HITS,
        language_hits=hits,
        strategy_version=strategy.version,
        findings=tuple(findings),
    )


# The run


@dataclass(frozen=True)
class ScriptValidation:
    id: str
    script_id: str
    content_item_id: str
    method: str
    words: int
    seconds: int
    language_checked: bool
    language_hits: int
    strategy_version: int
    script_strategy_version: int | None
    findings: tuple[Finding, ...]
    findings_count: int
    warn_count: int
    fail_count: int
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
        _whole("words", self.words, 0)
        _whole("seconds", self.seconds, 0)
        if not isinstance(self.language_checked, bool):
            raise TypeError("language_checked must be a bool")
        _whole("language_hits", self.language_hits, 0, MAX_WORDS)
        _whole("strategy_version", self.strategy_version, 1)
        if self.script_strategy_version is not None:
            _whole("script_strategy_version", self.script_strategy_version, 1)
        _whole("findings_count", self.findings_count, 0, MAX_FINDINGS)
        _whole("warn_count", self.warn_count, 0)
        _whole("fail_count", self.fail_count, 0)
        if self.warn_count + self.fail_count != self.findings_count:
            raise ValueError("every finding is either WARN or FAIL")
        self._check_findings()
        if not isinstance(self.requested_by, Actor):
            raise TypeError("requested_by must be an Actor")
        if not isinstance(
            self.created_at, datetime
        ) or self.created_at.utcoffset() != timedelta(0):
            raise ValueError("created_at must be timezone-aware UTC")

    def _check_findings(self) -> None:
        if not isinstance(self.findings, tuple):
            raise TypeError("findings must be a tuple")
        for finding in self.findings:
            if not isinstance(finding, Finding):
                raise TypeError("findings must be Finding values")
        if len(self.findings) != self.findings_count:
            raise ValueError("findings_count must count the findings")
        for status, name in (
            (WARN, "warn_count"),
            (FAIL, "fail_count"),
        ):
            counted = sum(finding.status is status for finding in self.findings)
            if counted != getattr(self, name):
                raise ValueError(f"{name} must count the {status.value} findings")
        if self.method != SCRIPT_VALIDATION_METHOD:
            return  # the rules below are those of the v1 method only
        if self.language_checked != (self.language_hits >= MIN_HITS):
            raise ValueError("the language is checked from 10 signal hits")
        codes = Counter(finding.code for finding in self.findings)
        if codes[CODE.TOO_SHORT] + codes[CODE.TOO_LONG] > 1:
            raise ValueError("a script has at most one length finding")
        if codes[CODE.CHAPTER_KIND] > 1 or (
            codes[CODE.CHAPTER_KIND] and codes[CODE.CHAPTER_COUNT]
        ):
            raise ValueError("one chapter_kind finding, and no chapter_count with it")
        if codes[CODE.LANGUAGE_MISMATCH] > 1 or (
            codes[CODE.LANGUAGE_MISMATCH] and not self.language_checked
        ):
            raise ValueError("one language finding, only for a checked language")
        for finding in self.findings:
            self._check_v1(finding)

    def _check_v1(self, finding: Finding) -> None:
        code = finding.code
        if finding.score is not None and finding.score != round(finding.score, 3):
            raise ValueError("score must have at most 3 decimals")
        needs = {
            CODE.MISSING_SECTION: (),
            CODE.UNEXPECTED_SECTION: ("section_index",),
            CODE.WRONG_ORDER: ("section_index",),
            CODE.CHAPTER_COUNT: ("actual", "minimum", "maximum"),
            CODE.CHAPTER_KIND: ("section_index", "actual"),
            CODE.TOO_SHORT: ("actual", "minimum", "maximum"),
            CODE.TOO_LONG: ("actual", "minimum", "maximum"),
            CODE.LANGUAGE_MISMATCH: ("actual", "score", "matched"),
            CODE.BANNED_PHRASE: ("section_index", "matched"),
            CODE.HOOK_TOO_LONG: ("section_index", "actual", "maximum"),
        }[code]
        if any(getattr(finding, name) is None for name in needs):
            raise ValueError(f"{code.value} needs {', '.join(needs)}")
        actual, minimum, maximum = finding.actual, finding.minimum, finding.maximum
        if code is CODE.TOO_SHORT and not actual < minimum:
            raise ValueError("too_short needs a length under the minimum")
        if code is CODE.TOO_LONG and not actual > maximum:
            raise ValueError("too_long needs a length over the maximum")
        if code in _LENGTH_CODES and actual != self.seconds:
            raise ValueError("a length finding names the seconds of the script")
        if code is CODE.CHAPTER_COUNT and minimum <= actual <= maximum:
            raise ValueError("chapter_count needs a count outside its range")
        if code is CODE.CHAPTER_KIND and not (
            LONGFORM_MIN_CHAPTERS <= actual <= LONGFORM_MAX_CHAPTERS
        ):
            raise ValueError("chapter_kind needs a count within the chapter range")
        if code is CODE.HOOK_TOO_LONG and not actual > maximum:
            raise ValueError("hook_too_long needs a hook over its limit")
        if code is CODE.BANNED_PHRASE and not finding.matched:
            raise ValueError("banned_phrase needs 1 or more phrases")
        if code is CODE.LANGUAGE_MISMATCH:
            fail_at = LANGUAGE_FAIL_PERCENT / 100
            share_ok = (
                finding.score >= fail_at
                if finding.status is FAIL
                else LANGUAGE_WARN_PERCENT / 100 <= finding.score < fail_at
            )
            if actual != self.language_hits or not share_ok:
                raise ValueError("language_mismatch must follow the signal hits")

    @property
    def status(self) -> ValidationStatus:
        """The worst finding: FAIL, then WARN, then PASS (no finding is PASS)."""
        if self.fail_count:
            return FAIL
        if self.warn_count:
            return WARN
        return PASS

    @classmethod
    def create(
        cls,
        script: Script,
        analysis: ScriptAnalysis,
        *,
        method: str = SCRIPT_VALIDATION_METHOD,
        requested_by: Actor,
        clock: Clock | None = None,
    ) -> "ScriptValidation":
        """A run for ``script`` with the findings of ``analysis``."""
        findings = tuple(analysis.findings)
        return cls(
            id=uuid.uuid4().hex,
            script_id=script.id,
            content_item_id=script.content_item_id,
            method=method,
            words=analysis.words,
            seconds=analysis.seconds,
            language_checked=analysis.language_checked,
            language_hits=analysis.language_hits,
            strategy_version=analysis.strategy_version,
            script_strategy_version=script.strategy_version,
            findings=findings,
            findings_count=len(findings),
            warn_count=sum(f.status is WARN for f in findings),
            fail_count=sum(f.status is FAIL for f in findings),
            requested_by=requested_by,
            created_at=clock() if clock else datetime.now(UTC),
        )

    def counts(self) -> dict[str, int | str | bool]:
        """The scalar counts of the run, as audited."""
        return {
            "words": self.words,
            "seconds": self.seconds,
            "language_checked": self.language_checked,
            "findings": self.findings_count,
            "warn": self.warn_count,
            "fail": self.fail_count,
            "status": self.status.value,
        }
