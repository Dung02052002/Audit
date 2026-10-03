"""Hook rules and results (Prompt Pack v8, prompt #065), context C5.

The rules were approved by the user on 2026-10-03:

- A hook is the opening of a script (``SectionKind.HOOK``, F-064). Its limits
  depend on the content type (``HOOK_LIMITS``): Shorts at most 2 sentences and
  15 words (about 6 seconds at 150 words per minute), LongForm at most 3
  sentences and 60 words (about 24 seconds).
- For both, a hook must not be empty and must not contain a brand banned
  phrase (ignoring case, as whole words). Its language is the strategy's
  primary language, recorded with the result, not detected.
- ``normalize_hook`` cleans a generated text before it is checked: surrounding
  spaces, quotes and a leading list marker go, inner spaces collapse.
- ``check_hook`` returns the ``HookIssue``s of a text (none means valid).
- ``HookGeneration`` is one stored run of the generator for a content item:
  up to 3 valid ``HookCandidate``s, the rejected texts with their issues, the
  inputs used (language, strategy version, research report, topic, angle)
  and the provider and model. The caller picks a candidate; it becomes the
  HOOK section of a script (#066, #067) through ``HookCandidate.as_section``.
"""

import re
import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any

from ai_youtube_agent.content.script import ScriptSection, SectionKind
from ai_youtube_agent.core.audit import Actor
from ai_youtube_agent.core.content_item import ContentType

Clock = Callable[[], datetime]

MAX_CANDIDATES = 3
MAX_ANGLE = 300
_WORD = re.compile(r"\w+")
_SENTENCE_END = re.compile(r"(?<=[.!?…])\s+")
_LIST_MARKER = re.compile(r"^(?:[-*•]\s+|\d{1,2}[.)]\s+)")
_QUOTES = {'"': '"', "'": "'", "“": "”", "‘": "’", "«": "»"}


@dataclass(frozen=True)
class HookLimits:
    max_sentences: int
    max_words: int


HOOK_LIMITS = {
    ContentType.SHORTS: HookLimits(max_sentences=2, max_words=15),
    ContentType.LONGFORM: HookLimits(max_sentences=3, max_words=60),
}


class HookIssueCode(StrEnum):
    EMPTY = "empty"
    TOO_MANY_WORDS = "too_many_words"
    TOO_MANY_SENTENCES = "too_many_sentences"
    BANNED_PHRASE = "banned_phrase"
    DUPLICATE = "duplicate"


@dataclass(frozen=True)
class HookIssue:
    code: HookIssueCode
    detail: str

    def as_dict(self) -> dict[str, str]:
        return {"code": self.code.value, "detail": self.detail}


def normalize_hook(text: str) -> str:
    text = " ".join(text.split())
    text = _LIST_MARKER.sub("", text)
    while len(text) >= 2 and _QUOTES.get(text[0]) == text[-1]:
        text = text[1:-1].strip()
    return text


def count_words(text: str) -> int:
    return len(_WORD.findall(text))


def count_sentences(text: str) -> int:
    return len([part for part in _SENTENCE_END.split(text.strip()) if part])


def check_hook(
    text: str, content_type: ContentType, banned_phrases: Iterable[str] = ()
) -> tuple[HookIssue, ...]:
    if not text.strip():
        return (HookIssue(HookIssueCode.EMPTY, "the hook is empty"),)
    limits = HOOK_LIMITS[content_type]
    issues = []
    words = count_words(text)
    if words > limits.max_words:
        issues.append(
            HookIssue(
                HookIssueCode.TOO_MANY_WORDS,
                f"{words} words, at most {limits.max_words}",
            )
        )
    sentences = count_sentences(text)
    if sentences > limits.max_sentences:
        issues.append(
            HookIssue(
                HookIssueCode.TOO_MANY_SENTENCES,
                f"{sentences} sentences, at most {limits.max_sentences}",
            )
        )
    for phrase in banned_phrases:
        words = r"\s+".join(re.escape(word) for word in phrase.split())
        pattern = r"(?<!\w)" + words + r"(?!\w)"
        if re.search(pattern, text, re.IGNORECASE):
            issues.append(
                HookIssue(HookIssueCode.BANNED_PHRASE, f"contains {phrase!r}")
            )
    return tuple(issues)


@dataclass(frozen=True)
class HookCandidate:
    text: str

    def __post_init__(self) -> None:
        if not isinstance(self.text, str) or not self.text.strip():
            raise ValueError("a hook candidate must not be empty")

    @property
    def words(self) -> int:
        return count_words(self.text)

    def as_section(self) -> ScriptSection:
        return ScriptSection(SectionKind.HOOK, self.text)


@dataclass(frozen=True)
class HookRejection:
    text: str
    issues: tuple[HookIssue, ...]

    def __post_init__(self) -> None:
        if not self.issues:
            raise ValueError("a rejection needs at least one issue")

    def as_dict(self) -> dict[str, Any]:
        return {"text": self.text, "issues": [i.as_dict() for i in self.issues]}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "HookRejection":
        return cls(
            data["text"],
            tuple(
                HookIssue(HookIssueCode(i["code"]), i["detail"]) for i in data["issues"]
            ),
        )


@dataclass(frozen=True)
class HookGeneration:
    id: str
    content_item_id: str
    content_type: ContentType
    language: str
    strategy_version: int
    research_report_id: str
    topic_id: str | None
    topic_label: str | None
    angle: str | None
    candidates: tuple[HookCandidate, ...]
    rejected: tuple[HookRejection, ...]
    provider: str
    model: str
    requested_by: Actor
    created_at: datetime

    def __post_init__(self) -> None:
        for name in ("id", "content_item_id", "language", "research_report_id"):
            if not getattr(self, name):
                raise ValueError(f"{name} must not be empty")
        if not isinstance(self.content_type, ContentType):
            raise TypeError("content_type must be a ContentType")
        if (self.topic_id is None) != (self.topic_label is None):
            raise ValueError("a topic has both an id and a label, or neither")
        if self.angle is not None and (
            not self.angle.strip() or len(self.angle) > MAX_ANGLE
        ):
            raise ValueError(f"an angle is 1 to {MAX_ANGLE} characters")
        if len(self.candidates) > MAX_CANDIDATES:
            raise ValueError(f"at most {MAX_CANDIDATES} candidates")
        if len({c.text.casefold() for c in self.candidates}) != len(self.candidates):
            raise ValueError("candidates must not repeat")
        if not self.provider or not self.model:
            raise ValueError("provider and model must not be empty")
        if not isinstance(self.requested_by, Actor):
            raise TypeError("requested_by must be an Actor")
        if not isinstance(
            self.created_at, datetime
        ) or self.created_at.utcoffset() != timedelta(0):
            raise ValueError("created_at must be timezone-aware UTC")

    @classmethod
    def create(cls, **values: Any) -> "HookGeneration":
        clock: Clock | None = values.pop("clock", None)
        return cls(
            id=uuid.uuid4().hex,
            created_at=clock() if clock else datetime.now(UTC),
            **values,
        )
