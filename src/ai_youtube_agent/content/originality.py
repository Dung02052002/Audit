"""Originality rules and runs (Prompt Pack v8, prompt #071), context C5.

The rules were approved by the user on 2026-10-03. An originality check is
deterministic, with no AI model; the run records its method
(``ORIGINALITY_METHOD`` = ``"originality-rules-v1"``) so another method can be
added later. It looks for excessive reuse of earlier content and for
repetitive structure.

- Prior content is chosen by the caller (``ScriptRepository.list_prior_latest``):
  the latest version of every other content item of the same channel, among
  the scripts created strictly before the script, the newest ``MAX_PRIORS``
  (50) of them, whatever the status of the item. Earlier versions of the same
  item and other channels are never compared.
- Text is read as words: NFC, then the lower-case (``casefold``) letters and
  digit runs of ``content/similarity.py``. Stop words are kept. Every CTA
  section is left out of all the rules below. At most ``MAX_WORDS`` (20,000)
  words of a script are read, in order; the rest is ignored. The cost is
  bounded before any text is normalised or split: at most ``MAX_CHARS``
  (``MAX_WORDS`` * 40 = 800,000) characters of a script are read, taken
  section by section in order and cut where the budget ends, so a script of
  fewer characters than that (every ordinary script) is read whole, while a
  longer one that is mostly spaces or punctuation is read only in part. The
  words read are stored as ``words``. Languages that
  write without spaces between words (Chinese, Japanese, Thai) are not
  detected by word n-grams: their text is one long word or none.
- Reuse. The shingles of a script are its word 5-grams (a set, built per
  section, so none crosses two sections). The containment of a prior script is
  the share of the script's shingles also found in it. A script of fewer than
  ``MIN_WORDS`` (20) words gets no reuse finding. For each prior, in order:

  ===========================================================  ====  =================
  containment of 60% or more                                   FAIL  near_duplicate
  else containment of 25% or more and 8 or more shared         WARN  high_overlap
  shingles
  3 or more distinct sentences of 8 or more words that appear  WARN  copied_sentences
  exactly in the prior (words equal after normalising)
  ===========================================================  ====  =================

  ``copied_sentences`` does not depend on the first two rows: it can come with
  either. "Exactly" means a whole sentence of the script equals a whole
  sentence of the prior after normalising; a sentence that the prior only
  contains inside a longer sentence is not counted (the shingles see it
  instead). Sentences are split by ``claim_extraction.split_sentences``.
  Scores are the shares rounded down to 3 decimals, so a score never reaches a
  threshold the share has not reached (1499 of 2500 is 0.599, a WARN; 1500 of
  2500 is 0.6, a FAIL).
- Structure (WARN only, one finding per rule, never about one prior):

  - ``repeated_hook``: the first 4 words of the first sentence of the HOOK
    section (section 0 when there is no HOOK) equal those of 3 or more of the
    compared priors (``matched`` = how many priors);
  - ``repeated_sentences``: sentences of 5 or more words that repeat inside the
    script, with 2 or more repeats in all (``matched`` = repeats,
    ``section_index`` = the section of the first repeat);
  - ``repeated_openers``: of the sentences of 2 or more words, at least 4 and
    40% or more start with the same 2 words (``matched`` = how many,
    ``score`` = the share).

- A finding has a ``code`` (closed ``OriginalityCode``) and the status of that
  code; it keeps ids, a score and a count, never any script text.
- ``OriginalityCheck`` is one stored run for one script, with its findings and
  counts. Its ``status`` is derived: the worst of its findings (FAIL, then
  WARN, then PASS; no prior and no finding is PASS).
- A result is a record only: it blocks nothing, and it is never changed or
  overridden. A later task may add a separate table of overrides.
"""

import itertools
import unicodedata
import uuid
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum

from ai_youtube_agent.content.claim_extraction import split_sentences
from ai_youtube_agent.content.script import Script, SectionKind
from ai_youtube_agent.content.similarity import WORD_PATTERN
from ai_youtube_agent.core.audit import Actor

__all__ = [
    "MAX_FINDINGS",
    "MAX_PRIORS",
    "MAX_WORDS",
    "MIN_WORDS",
    "ORIGINALITY_METHOD",
    "Finding",
    "OriginalityAnalysis",
    "OriginalityCheck",
    "OriginalityCode",
    "OriginalityStatus",
    "check_originality",
]

Clock = Callable[[], datetime]

ORIGINALITY_METHOD = "originality-rules-v1"
MAX_METHOD = 100
MAX_PRIORS = 50
MAX_WORDS = 20_000
MAX_CHARS = MAX_WORDS * 40  # characters read of a script before the word cap
MIN_WORDS = 20
SHINGLE_WORDS = 5
# Percentages, compared with whole numbers so a boundary is exact.
NEAR_DUPLICATE_PERCENT = 60
HIGH_OVERLAP_PERCENT = 25
MIN_SHARED_SHINGLES = 8
COPIED_SENTENCE_WORDS = 8
MIN_COPIED_SENTENCES = 3
HOOK_WORDS = 4
HOOK_CHARS = 1_000  # only the first sentence of a hook is read
MIN_REPEATED_HOOKS = 3
REPEATED_SENTENCE_WORDS = 5
MIN_REPEATS = 2
OPENER_WORDS = 2
MIN_OPENER_SENTENCES = 4
OPENER_SHARE_PERCENT = 40
# Per prior at most near_duplicate or high_overlap, and copied_sentences; and
# the three structure findings.
MAX_FINDINGS = 2 * MAX_PRIORS + 3


class OriginalityStatus(StrEnum):
    PASS = "pass"
    WARN = "warn"
    FAIL = "fail"


class OriginalityCode(StrEnum):
    """The rule that gave a finding."""

    NEAR_DUPLICATE = "near_duplicate"
    HIGH_OVERLAP = "high_overlap"
    COPIED_SENTENCES = "copied_sentences"
    REPEATED_HOOK = "repeated_hook"
    REPEATED_SENTENCES = "repeated_sentences"
    REPEATED_OPENERS = "repeated_openers"


_PRIOR_CODES = (
    OriginalityCode.NEAR_DUPLICATE,
    OriginalityCode.HIGH_OVERLAP,
    OriginalityCode.COPIED_SENTENCES,
)


def status_of(code: OriginalityCode) -> OriginalityStatus:
    """The status a code stands for: only near_duplicate is not a WARN."""
    if code is OriginalityCode.NEAR_DUPLICATE:
        return OriginalityStatus.FAIL
    return OriginalityStatus.WARN


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
    """One thing the rules found, with ids, a score and a count only."""

    code: OriginalityCode
    status: OriginalityStatus
    prior_script_id: str | None = None
    prior_content_item_id: str | None = None
    section_index: int | None = None
    score: float | None = None
    matched: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.code, OriginalityCode):
            raise TypeError("code must be an OriginalityCode")
        if not isinstance(self.status, OriginalityStatus):
            raise TypeError("status must be an OriginalityStatus")
        if self.status is not status_of(self.code):
            raise ValueError(f"the code {self.code.value} is not {self.status.value}")
        ids = (self.prior_script_id, self.prior_content_item_id)
        for value in ids:
            if value is not None and (not isinstance(value, str) or not value.strip()):
                raise ValueError("a prior id must not be empty")
        if (self.code in _PRIOR_CODES) != all(value is not None for value in ids):
            raise ValueError("only the findings about a prior script name it")
        if (self.prior_script_id is None) != (self.prior_content_item_id is None):
            raise ValueError("a prior script and its content item come together")
        if self.section_index is not None:
            _whole("section_index", self.section_index, 0)
        if self.score is not None and (
            isinstance(self.score, bool)
            or not isinstance(self.score, int | float)
            or not 0 <= self.score <= 1
        ):
            raise ValueError("score must be a number from 0 to 1")
        if self.matched is not None:
            _whole("matched", self.matched, 0)


# Reading a script


@dataclass(frozen=True)
class _Sentence:
    section_index: int
    words: tuple[str, ...]


@dataclass(frozen=True)
class _Profile:
    """What the rules read of a script: its words, shingles and sentences."""

    words: int
    shingles: frozenset[tuple[str, ...]]
    sentences: tuple[_Sentence, ...]

    @property
    def copyable(self) -> frozenset[tuple[str, ...]]:
        return frozenset(
            s.words for s in self.sentences if len(s.words) >= COPIED_SENTENCE_WORDS
        )


def _tokens(text: str, limit: int) -> list[str]:
    """At most ``limit`` words of an NFC text, lower case."""
    found = itertools.islice(WORD_PATTERN.finditer(text), limit)
    return [match.group().casefold() for match in found]


def _profile(script: Script) -> _Profile:
    shingles: set[tuple[str, ...]] = set()
    sentences: list[_Sentence] = []
    total = 0
    chars = MAX_CHARS
    for index, section in enumerate(script.sections):
        if section.kind is SectionKind.CTA:
            continue
        if total >= MAX_WORDS or chars <= 0:
            break
        read = section.text[:chars]
        chars -= len(read)
        section_words: list[str] = []
        for sentence in split_sentences(unicodedata.normalize("NFC", read)):
            room = MAX_WORDS - total - len(section_words)
            if room <= 0:
                break
            tokens = _tokens(sentence, room)
            if tokens:
                sentences.append(_Sentence(index, tuple(tokens)))
                section_words.extend(tokens)
        total += len(section_words)
        shingles.update(
            zip(*(section_words[i:] for i in range(SHINGLE_WORDS)), strict=False)
        )
    return _Profile(total, frozenset(shingles), tuple(sentences))


def _hook_key(script: Script) -> tuple[str, ...] | None:
    """The first words of the first sentence of the hook, or None."""
    section = next(
        (s for s in script.sections if s.kind is SectionKind.HOOK), script.sections[0]
    )
    if section.kind is SectionKind.CTA:
        return None
    text = unicodedata.normalize("NFC", section.text[:HOOK_CHARS])
    sentences = split_sentences(text)
    if not sentences:
        return None
    tokens = _tokens(sentences[0], HOOK_WORDS)
    return tuple(tokens) if len(tokens) == HOOK_WORDS else None


# The rules


def _share(numerator: int, denominator: int) -> float:
    """The share rounded down to 3 decimals, with integers: never up to a limit."""
    return numerator * 1000 // denominator / 1000


def _reuse_findings(
    target: _Profile, prior: Script, profile: _Profile
) -> list[Finding]:
    findings = []
    total = len(target.shingles)
    shared = len(target.shingles & profile.shingles)
    if total and shared * 100 >= NEAR_DUPLICATE_PERCENT * total:
        code = OriginalityCode.NEAR_DUPLICATE
    elif (
        total
        and shared * 100 >= HIGH_OVERLAP_PERCENT * total
        and shared >= MIN_SHARED_SHINGLES
    ):
        code = OriginalityCode.HIGH_OVERLAP
    else:
        code = None
    if code is not None:
        findings.append(
            Finding(
                code,
                status_of(code),
                prior.id,
                prior.content_item_id,
                score=_share(shared, total),
                matched=shared,
            )
        )
    copied = len(target.copyable & profile.copyable)
    if copied >= MIN_COPIED_SENTENCES:
        findings.append(
            Finding(
                OriginalityCode.COPIED_SENTENCES,
                OriginalityStatus.WARN,
                prior.id,
                prior.content_item_id,
                matched=copied,
            )
        )
    return findings


def _repeated_sentences(target: _Profile) -> Finding | None:
    seen: set[tuple[str, ...]] = set()
    repeats = 0
    first: int | None = None
    for sentence in target.sentences:
        if len(sentence.words) < REPEATED_SENTENCE_WORDS:
            continue
        if sentence.words in seen:
            repeats += 1
            if first is None:
                first = sentence.section_index
        seen.add(sentence.words)
    if repeats < MIN_REPEATS:
        return None
    return Finding(
        OriginalityCode.REPEATED_SENTENCES,
        OriginalityStatus.WARN,
        section_index=first,
        matched=repeats,
    )


def _repeated_openers(target: _Profile) -> Finding | None:
    openers = Counter(
        s.words[:OPENER_WORDS] for s in target.sentences if len(s.words) >= OPENER_WORDS
    )
    total = sum(openers.values())
    if total < MIN_OPENER_SENTENCES:
        return None
    count = max(openers.values())
    if count * 100 < OPENER_SHARE_PERCENT * total:
        return None
    return Finding(
        OriginalityCode.REPEATED_OPENERS,
        OriginalityStatus.WARN,
        score=_share(count, total),
        matched=count,
    )


@dataclass(frozen=True)
class OriginalityAnalysis:
    """What the rules found in one script, before it is stored."""

    words: int
    priors_count: int
    findings: tuple[Finding, ...]


def check_originality(script: Script, priors: Sequence[Script]) -> OriginalityAnalysis:
    """The findings for ``script`` against ``priors``, by the module docstring.

    ``priors`` are scripts of other content items (at most ``MAX_PRIORS``),
    newest first; anything else raises ``ValueError``.
    """
    priors = tuple(priors)
    if len(priors) > MAX_PRIORS:
        raise ValueError(f"at most {MAX_PRIORS} prior scripts")
    if any(prior.content_item_id == script.content_item_id for prior in priors):
        raise ValueError("a prior script belongs to the content item of the script")
    target = _profile(script)
    findings: list[Finding] = []
    hook = _hook_key(script)
    same_hook = 0
    for prior in priors:
        if target.words >= MIN_WORDS:
            findings.extend(_reuse_findings(target, prior, _profile(prior)))
        if hook is not None and _hook_key(prior) == hook:
            same_hook += 1
    if same_hook >= MIN_REPEATED_HOOKS:
        findings.append(
            Finding(
                OriginalityCode.REPEATED_HOOK,
                OriginalityStatus.WARN,
                matched=same_hook,
            )
        )
    findings.extend(
        finding
        for finding in (_repeated_sentences(target), _repeated_openers(target))
        if finding is not None
    )
    return OriginalityAnalysis(target.words, len(priors), tuple(findings))


# The run


@dataclass(frozen=True)
class OriginalityCheck:
    id: str
    script_id: str
    content_item_id: str
    method: str
    words: int
    priors_count: int
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
        _whole("words", self.words, 0, MAX_WORDS)
        _whole("priors_count", self.priors_count, 0, MAX_PRIORS)
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
            (OriginalityStatus.WARN, "warn_count"),
            (OriginalityStatus.FAIL, "fail_count"),
        ):
            counted = sum(finding.status is status for finding in self.findings)
            if counted != getattr(self, name):
                raise ValueError(f"{name} must count the {status.value} findings")
        for finding in self.findings:
            if finding.prior_script_id == self.script_id:
                raise ValueError("a script is not a prior script of itself")
            if self.method != ORIGINALITY_METHOD:
                continue  # the rules below are those of the v1 method only
            if finding.score is not None and finding.score != round(finding.score, 3):
                raise ValueError("score must have at most 3 decimals")
            if finding.code in _PRIOR_CODES and (
                self.priors_count == 0 or self.words < MIN_WORDS
            ):
                raise ValueError(
                    "reuse needs a prior script and a script of enough words"
                )
            if finding.code is OriginalityCode.REPEATED_HOOK and not (
                MIN_REPEATED_HOOKS <= (finding.matched or 0) <= self.priors_count
            ):
                raise ValueError("repeated_hook needs 3 or more of the priors")

    @property
    def status(self) -> OriginalityStatus:
        """The worst finding: FAIL, then WARN, then PASS (no finding is PASS)."""
        if self.fail_count:
            return OriginalityStatus.FAIL
        if self.warn_count:
            return OriginalityStatus.WARN
        return OriginalityStatus.PASS

    @classmethod
    def create(
        cls,
        script: Script,
        analysis: OriginalityAnalysis,
        *,
        method: str = ORIGINALITY_METHOD,
        requested_by: Actor,
        clock: Clock | None = None,
    ) -> "OriginalityCheck":
        """A run for ``script`` with the findings of ``analysis``."""
        findings = tuple(analysis.findings)
        return cls(
            id=uuid.uuid4().hex,
            script_id=script.id,
            content_item_id=script.content_item_id,
            method=method,
            words=analysis.words,
            priors_count=analysis.priors_count,
            findings=findings,
            findings_count=len(findings),
            warn_count=sum(f.status is OriginalityStatus.WARN for f in findings),
            fail_count=sum(f.status is OriginalityStatus.FAIL for f in findings),
            requested_by=requested_by,
            created_at=clock() if clock else datetime.now(UTC),
        )

    def counts(self) -> dict[str, int | str]:
        """The scalar counts of the run, as audited."""
        return {
            "words": self.words,
            "priors": self.priors_count,
            "findings": self.findings_count,
            "warn": self.warn_count,
            "fail": self.fail_count,
            "status": self.status.value,
        }
