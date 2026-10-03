"""Script entity (Prompt Pack v8, prompts #017, #064), context C5 Script & Fact Check.

Three frozen entities, related by id:

- ``Script``: one immutable version of the script of a content item. It has its
  own id, ``content_item_id``, a version that starts at 1, its sections and a
  UTC ``created_at``. ``next_version`` returns a new record with version + 1
  and refuses unchanged sections.
- ``Claim``: a factual claim made by one exact script version (``script_id``).
  A new script version starts with no claims; they are extracted again (#068).
- ``Evidence``: links a claim to a research source. ``source_ref`` is the id
  of that source (a ``Source`` id since #055), matched by #069. The optional
  ``excerpt`` is the supporting passage.

Adding claims or evidence never creates a new script version.

Script model (#064), approved by the user on 2026-10-03 (B-017 extended):

- ``sections`` are 1 to 100 ``ScriptSection``s in order. Each has a
  ``SectionKind`` (hook, intro, body, chapter, outro, cta), its text, an
  optional title (required for a chapter, at most 100 characters) and optional
  ``seconds`` (1 to 14,400). ``text`` is the sections' text joined by blank
  lines; ``Script.create(item, text)`` makes one body section. Rules about
  which sections a content type needs are #072.
- ``duration_target`` (``DurationTarget``, min and max seconds) is copied from
  the strategy's format for the content type when the script is made
  (``DurationTarget.from_format``). ``estimated_seconds`` adds each section's
  ``seconds``, or estimates it from its words at ``WORDS_PER_MINUTE`` (150);
  ``within_target`` only reports, it blocks nothing (#072 validates).
- Version history: each version records ``created_by`` (the actor), an
  optional ``reason`` (at most 500 characters), ``parent_id`` (the version it
  was made from; None exactly for version 1) and what it was made from:
  ``strategy_version`` and ``research_report_id``. Scripts stored before #064
  have no actor. Diffs are #073.
- A claim may name the section it comes from (``section_index``);
  ``Script.claim`` checks the index against that version.

Claim extraction (#068) adds two optional claim fields: ``kind`` (a closed
``ClaimKind``: numeric, date, entity, comparison, absolute) and
``extraction_id``, the extraction run that found the claim. Claims stored
before #068 have neither.
"""

import math
import re
import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any

from ai_youtube_agent.content.strategy import FormatSettings
from ai_youtube_agent.core.audit import Actor
from ai_youtube_agent.core.content_item import ContentType

Clock = Callable[[], datetime]

MAX_SECTIONS = 100
MAX_SECONDS = 14_400
MAX_TITLE = 100
MAX_REASON = 500
WORDS_PER_MINUTE = 150
_WORD = re.compile(r"\w+")


class ClaimKind(StrEnum):
    """What makes a sentence a factual claim (#068), strongest first."""

    NUMERIC = "numeric"
    DATE = "date"
    ENTITY = "entity"
    COMPARISON = "comparison"
    ABSOLUTE = "absolute"


class SectionKind(StrEnum):
    HOOK = "hook"
    INTRO = "intro"
    BODY = "body"
    CHAPTER = "chapter"
    OUTRO = "outro"
    CTA = "cta"


@dataclass(frozen=True)
class ScriptSection:
    kind: SectionKind
    text: str
    title: str | None = None
    seconds: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.kind, SectionKind):
            raise TypeError("kind must be a SectionKind")
        _require_text("section text", self.text)
        if self.text != self.text.strip():
            raise ValueError("section text must not start or end with spaces")
        if self.title is not None:
            _require_text("section title", self.title)
            if self.title != self.title.strip() or len(self.title) > MAX_TITLE:
                raise ValueError(
                    f"a section title is trimmed and at most {MAX_TITLE} characters"
                )
        if self.kind is SectionKind.CHAPTER and self.title is None:
            raise ValueError("a chapter needs a title")
        if self.seconds is not None:
            _whole("seconds", self.seconds, 1, MAX_SECONDS)

    @classmethod
    def create(
        cls,
        kind: SectionKind,
        text: str,
        *,
        title: str | None = None,
        seconds: int | None = None,
    ) -> "ScriptSection":
        return cls(
            kind, text.strip(), title.strip() if title is not None else None, seconds
        )

    @property
    def words(self) -> int:
        return len(_WORD.findall(self.text))

    @property
    def estimated_seconds(self) -> int:
        """The given seconds, else the words read at ``WORDS_PER_MINUTE``."""
        if self.seconds is not None:
            return self.seconds
        return math.ceil(self.words * 60 / WORDS_PER_MINUTE)

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "title": self.title,
            "text": self.text,
            "seconds": self.seconds,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ScriptSection":
        return cls(
            SectionKind(data["kind"]),
            data["text"],
            data.get("title"),
            data.get("seconds"),
        )


@dataclass(frozen=True)
class DurationTarget:
    min_seconds: int
    max_seconds: int

    def __post_init__(self) -> None:
        _whole("min_seconds", self.min_seconds, 1, MAX_SECONDS)
        _whole("max_seconds", self.max_seconds, 1, MAX_SECONDS)
        if self.min_seconds > self.max_seconds:
            raise ValueError("min_seconds must not be greater than max_seconds")

    @classmethod
    def from_format(
        cls, format_settings: FormatSettings, content_type: ContentType
    ) -> "DurationTarget":
        """The duration range of ``content_type`` in the strategy's format (#049)."""
        chosen = (
            format_settings.shorts
            if content_type is ContentType.SHORTS
            else format_settings.longform
        )
        return cls(chosen.min_seconds, chosen.max_seconds)

    def contains(self, seconds: int) -> bool:
        return self.min_seconds <= seconds <= self.max_seconds


@dataclass(frozen=True)
class Script:
    id: str
    content_item_id: str
    version: int
    sections: tuple[ScriptSection, ...]
    created_at: datetime
    duration_target: DurationTarget | None = None
    created_by: Actor | None = None
    reason: str | None = None
    parent_id: str | None = None
    strategy_version: int | None = None
    research_report_id: str | None = None

    def __post_init__(self) -> None:
        _require_ids(self, "id", "content_item_id")
        _whole("version", self.version, 1, 1_000_000)
        if not isinstance(self.sections, tuple) or not (
            1 <= len(self.sections) <= MAX_SECTIONS
        ):
            raise ValueError(f"a script has 1 to {MAX_SECTIONS} sections")
        if not all(isinstance(s, ScriptSection) for s in self.sections):
            raise TypeError("sections must be ScriptSection values")
        _require_utc(self.created_at)
        if self.duration_target is not None and not isinstance(
            self.duration_target, DurationTarget
        ):
            raise TypeError("duration_target must be a DurationTarget")
        if self.created_by is not None and not isinstance(self.created_by, Actor):
            raise TypeError("created_by must be an Actor")
        if self.reason is not None:
            _require_text("reason", self.reason)
            if self.reason != self.reason.strip() or len(self.reason) > MAX_REASON:
                raise ValueError(
                    f"a reason is trimmed and at most {MAX_REASON} characters"
                )
        if (self.parent_id is None) != (self.version == 1):
            raise ValueError("exactly the versions after 1 have a parent")
        if self.parent_id is not None:
            _require_ids(self, "parent_id")
            if self.parent_id == self.id:
                raise ValueError("a version cannot be its own parent")
        if self.strategy_version is not None:
            _whole("strategy_version", self.strategy_version, 1, 1_000_000)
        if self.research_report_id is not None:
            _require_ids(self, "research_report_id")

    @classmethod
    def create(
        cls,
        content_item_id: str,
        text: str | None = None,
        *,
        sections: Iterable[ScriptSection] | None = None,
        duration_target: DurationTarget | None = None,
        created_by: Actor | None = None,
        reason: str | None = None,
        strategy_version: int | None = None,
        research_report_id: str | None = None,
        clock: Clock | None = None,
    ) -> "Script":
        return cls(
            id=_new_id(),
            content_item_id=content_item_id,
            version=1,
            sections=_sections(text, sections),
            created_at=_now(clock),
            duration_target=duration_target,
            created_by=created_by,
            reason=_reason(reason),
            strategy_version=strategy_version,
            research_report_id=research_report_id,
        )

    def next_version(
        self,
        text: str | None = None,
        *,
        sections: Iterable[ScriptSection] | None = None,
        created_by: Actor | None = None,
        reason: str | None = None,
        duration_target: DurationTarget | None = None,
        strategy_version: int | None = None,
        research_report_id: str | None = None,
        clock: Clock | None = None,
    ) -> "Script":
        """A new version made from this one; settings not given are kept."""
        new_sections = _sections(text, sections)
        if new_sections == self.sections:
            raise ValueError("a new version needs different script text")
        return Script(
            id=_new_id(),
            content_item_id=self.content_item_id,
            version=self.version + 1,
            sections=new_sections,
            created_at=_now(clock),
            duration_target=duration_target or self.duration_target,
            created_by=created_by,
            reason=_reason(reason),
            parent_id=self.id,
            strategy_version=strategy_version or self.strategy_version,
            research_report_id=research_report_id or self.research_report_id,
        )

    @property
    def text(self) -> str:
        return "\n\n".join(section.text for section in self.sections)

    @property
    def estimated_seconds(self) -> int:
        return sum(section.estimated_seconds for section in self.sections)

    @property
    def within_target(self) -> bool | None:
        """Whether the estimate is in the duration target; None without one."""
        if self.duration_target is None:
            return None
        return self.duration_target.contains(self.estimated_seconds)

    def claim(
        self,
        text: str,
        *,
        section_index: int | None = None,
        kind: ClaimKind | None = None,
        extraction_id: str | None = None,
        clock: Clock | None = None,
    ) -> "Claim":
        """A claim made by this version, optionally in one of its sections."""
        if section_index is not None:
            _whole("section_index", section_index, 0, len(self.sections) - 1)
        return Claim.create(
            self.id,
            text,
            section_index=section_index,
            kind=kind,
            extraction_id=extraction_id,
            clock=clock,
        )

    def as_dict(self) -> dict[str, Any]:
        target = self.duration_target
        return {
            "id": self.id,
            "content_item_id": self.content_item_id,
            "version": self.version,
            "text": self.text,
            "sections": [section.as_dict() for section in self.sections],
            "created_at": self.created_at.isoformat(),
            "duration_target": (
                {"min_seconds": target.min_seconds, "max_seconds": target.max_seconds}
                if target
                else None
            ),
            "estimated_seconds": self.estimated_seconds,
            "within_target": self.within_target,
            "created_by": (
                {"kind": self.created_by.kind.value, "id": self.created_by.id}
                if self.created_by
                else None
            ),
            "reason": self.reason,
            "parent_id": self.parent_id,
            "strategy_version": self.strategy_version,
            "research_report_id": self.research_report_id,
        }


@dataclass(frozen=True)
class Claim:
    id: str
    script_id: str
    text: str
    created_at: datetime
    section_index: int | None = None
    kind: ClaimKind | None = None
    extraction_id: str | None = None

    def __post_init__(self) -> None:
        _require_ids(self, "id", "script_id")
        _require_text("claim text", self.text)
        _require_utc(self.created_at)
        if self.section_index is not None:
            _whole("section_index", self.section_index, 0, MAX_SECTIONS - 1)
        if self.kind is not None and not isinstance(self.kind, ClaimKind):
            raise TypeError("kind must be a ClaimKind")
        if self.extraction_id is not None:
            _require_ids(self, "extraction_id")

    @classmethod
    def create(
        cls,
        script_id: str,
        text: str,
        *,
        section_index: int | None = None,
        kind: ClaimKind | None = None,
        extraction_id: str | None = None,
        clock: Clock | None = None,
    ) -> "Claim":
        return cls(
            id=_new_id(),
            script_id=script_id,
            text=text.strip(),
            created_at=_now(clock),
            section_index=section_index,
            kind=kind,
            extraction_id=extraction_id,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "script_id": self.script_id,
            "text": self.text,
            "created_at": self.created_at.isoformat(),
            "section_index": self.section_index,
            "kind": self.kind.value if self.kind else None,
            "extraction_id": self.extraction_id,
        }


@dataclass(frozen=True)
class Evidence:
    id: str
    claim_id: str
    source_ref: str
    excerpt: str | None
    created_at: datetime

    def __post_init__(self) -> None:
        _require_ids(self, "id", "claim_id", "source_ref")
        if self.excerpt is not None:
            _require_text("evidence excerpt", self.excerpt)
        _require_utc(self.created_at)

    @classmethod
    def create(
        cls,
        claim_id: str,
        source_ref: str,
        excerpt: str | None = None,
        *,
        clock: Clock | None = None,
    ) -> "Evidence":
        return cls(
            id=_new_id(),
            claim_id=claim_id,
            source_ref=source_ref,
            excerpt=excerpt.strip() if excerpt is not None else None,
            created_at=_now(clock),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "claim_id": self.claim_id,
            "source_ref": self.source_ref,
            "excerpt": self.excerpt,
            "created_at": self.created_at.isoformat(),
        }


def _sections(
    text: str | None, sections: Iterable[ScriptSection] | None
) -> tuple[ScriptSection, ...]:
    if (text is None) == (sections is None):
        raise ValueError("give either the script text or its sections")
    if text is not None:
        _require_text("script text", text)
        return (ScriptSection(SectionKind.BODY, text.strip()),)
    return tuple(sections)


def _reason(reason: str | None) -> str | None:
    return reason.strip() if reason is not None else None


def _whole(name: str, value: object, low: int, high: int) -> None:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not low <= value <= high
    ):
        raise ValueError(f"{name} must be a whole number from {low} to {high}")


def _require_ids(entity: object, *names: str) -> None:
    for name in names:
        value = getattr(entity, name)
        if not value or not value.strip():
            raise ValueError(f"{name} must not be empty")


def _require_text(name: str, value: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must not be empty")


def _require_utc(moment: datetime) -> None:
    if not isinstance(moment, datetime) or moment.utcoffset() != timedelta(0):
        raise ValueError("created_at must be timezone-aware UTC")


def _new_id() -> str:
    return uuid.uuid4().hex


def _now(clock: Clock | None) -> datetime:
    return clock() if clock else datetime.now(UTC)
