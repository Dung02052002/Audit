"""Script revisions (Prompt Pack v8, prompt #073), context C5.

A revision is the diff metadata of one script version (version 2 or later)
against its parent, never against any other version. Version 1 has no parent
and so no revision. The rules were approved by the user on 2026-10-04. The
method is ``SCRIPT_DIFF_METHOD`` (``"script-diff-v1"``), a deterministic
comparison with no AI model; the run records its method so another can be added
later. No script text is stored in a revision, its entries or its audit event:
only counts, indexes, flags and hashes.

What is compared. A *section key* is the SHA-256 of one section's canonical
JSON, an object with the keys ``kind`` (the value of the ``SectionKind``),
``title`` (text or null), ``text`` and ``seconds`` (``estimated_seconds``: the
given seconds, else the words read at 150 per minute), written with sorted
keys, no spaces and non-ASCII characters kept (UTF-8 bytes). The text is not
normalised: it is compared exactly as stored. ``content_sha256`` is the SHA-256
of the canonical JSON array of the sections of the new version, in order,
each written as above. Both are defined here once (``section_sha256`` and
``content_sha256``). A section is *unchanged* when its key is the same, that
is when its kind, title, text and estimated seconds are all equal.
The hashes cover the *estimated* seconds, so two versions that differ only in
declared seconds against none (``None``), with the same estimate, get the same
section keys and the same ``content_sha256``: it is not a fingerprint of the
stored row.

Alignment (``diff_scripts``, pure, nothing is stored). The longest common
subsequence of the old and the new section keys gives the unchanged sections
(a tie keeps the earlier old index, so the result is deterministic). Inside
each gap between two matches, the old and the new sections of the same kind
are paired in order (a second longest common subsequence, over the kinds,
with the same tie rule): a pair is *changed*, with the flags ``text_changed``,
``title_changed`` and ``seconds_changed`` and the difference in words; an old
section left over is *removed* and a new one *added*. A section that moved
therefore shows as one removed and one added entry. There is one entry per
aligned section, in an order where both the old and the new indexes only grow;
there are at most ``MAX_SECTIONS`` sections on each side, so the work is at
most 100 x 100 steps. ``words_delta`` is the new words minus the old words of
the entry (the words of an added section, minus the words of a removed one, 0
for an unchanged one), so the deltas add up to ``words_after - words_before``.
Added and removed entries have all three flags 0. ``sha256`` of an entry is the
key of its new section, or of its old section when it is removed.

A revision with no change at all (every entry unchanged) is allowed. In
practice it arises only when the versions differ in declared seconds against
none with an equal estimate, or when a version is added directly to the
repository, because ``Script.next_version`` refuses identical sections.
"""

import hashlib
import json
import uuid
from collections.abc import Callable, Hashable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any

from ai_youtube_agent.content.script import (
    MAX_SECTIONS,
    Script,
    ScriptSection,
    SectionKind,
)
from ai_youtube_agent.core.audit import Actor

__all__ = [
    "MAX_ENTRIES",
    "MAX_METHOD",
    "MAX_VERSION",
    "SCRIPT_DIFF_METHOD",
    "ChangeKind",
    "RevisionSummary",
    "ScriptDiff",
    "ScriptHistoryEntry",
    "ScriptRevision",
    "SectionChange",
    "content_sha256",
    "diff_scripts",
    "section_sha256",
]

Clock = Callable[[], datetime]

SCRIPT_DIFF_METHOD = "script-diff-v1"
MAX_METHOD = 100
MAX_VERSION = 1_000_000
# Every old section removed and every new section added.
MAX_ENTRIES = 2 * MAX_SECTIONS
_HEX = frozenset("0123456789abcdef")


class ChangeKind(StrEnum):
    ADDED = "added"
    REMOVED = "removed"
    CHANGED = "changed"
    UNCHANGED = "unchanged"


def _whole(name: str, value: object, low: int, high: int | None = None) -> None:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < low
        or (high is not None and value > high)
    ):
        limit = f"{low} or more" if high is None else f"{low} to {high}"
        raise ValueError(f"{name} must be a whole number of {limit}")


def _sha(name: str, value: str) -> None:
    if not isinstance(value, str) or len(value) != 64 or not set(value) <= _HEX:
        raise ValueError(f"{name} must be 64 lowercase hexadecimal characters")


def _canonical(section: ScriptSection) -> dict[str, Any]:
    return {
        "kind": section.kind.value,
        "title": section.title,
        "text": section.text,
        "seconds": section.estimated_seconds,
    }


def _digest(value: object) -> str:
    text = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def section_sha256(section: ScriptSection) -> str:
    """The key of one section: the SHA-256 of its canonical JSON."""
    return _digest(_canonical(section))


def content_sha256(sections: Sequence[ScriptSection]) -> str:
    """The SHA-256 of the canonical JSON array of the sections, in order."""
    return _digest([_canonical(section) for section in sections])


@dataclass(frozen=True)
class SectionChange:
    """One aligned section: how it differs between the parent and the version."""

    change: ChangeKind
    kind: SectionKind
    old_index: int | None
    new_index: int | None
    text_changed: bool
    title_changed: bool
    seconds_changed: bool
    words_delta: int
    sha256: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.change, ChangeKind):
            raise TypeError("change must be a ChangeKind")
        if not isinstance(self.kind, SectionKind):
            raise TypeError("kind must be a SectionKind")
        for name in ("old_index", "new_index"):
            value = getattr(self, name)
            if value is not None:
                _whole(name, value, 0, MAX_SECTIONS - 1)
        flags = (self.text_changed, self.title_changed, self.seconds_changed)
        if not all(isinstance(flag, bool) for flag in flags):
            raise TypeError("the change flags must be bool")
        if isinstance(self.words_delta, bool) or not isinstance(self.words_delta, int):
            raise ValueError("words_delta must be a whole number")
        if self.sha256 is not None:
            _sha("sha256", self.sha256)
        paired = self.old_index is not None and self.new_index is not None
        if self.change is ChangeKind.ADDED:
            ok = self.old_index is None and self.new_index is not None
            ok = ok and not any(flags) and self.words_delta >= 0
        elif self.change is ChangeKind.REMOVED:
            ok = self.old_index is not None and self.new_index is None
            ok = ok and not any(flags) and self.words_delta <= 0
        elif self.change is ChangeKind.CHANGED:
            ok = paired and any(flags)
            ok = ok and (self.text_changed or self.words_delta == 0)
        else:
            ok = paired and not any(flags) and self.words_delta == 0
        if not ok:
            raise ValueError(f"the fields do not fit a {self.change.value} section")


@dataclass(frozen=True)
class ScriptDiff:
    """The result of ``diff_scripts``: entries and totals, nothing stored."""

    entries: tuple[SectionChange, ...]
    words_before: int
    words_after: int
    seconds_before: int
    seconds_after: int
    content_sha256: str

    def __post_init__(self) -> None:
        if not isinstance(self.entries, tuple) or not all(
            isinstance(entry, SectionChange) for entry in self.entries
        ):
            raise TypeError("entries must be a tuple of SectionChange values")
        if len(self.entries) > MAX_ENTRIES:
            raise ValueError(f"a diff has at most {MAX_ENTRIES} entries")
        for name in ("words_before", "words_after", "seconds_before", "seconds_after"):
            _whole(name, getattr(self, name), 0)
        _sha("content_sha256", self.content_sha256)

    def count(self, change: ChangeKind) -> int:
        return sum(entry.change is change for entry in self.entries)


def _lcs(old: Sequence[Hashable], new: Sequence[Hashable]) -> list[tuple[int, int]]:
    """The index pairs of a longest common subsequence, earliest old first."""
    rows, cols = len(old), len(new)
    size = [[0] * (cols + 1) for _ in range(rows + 1)]
    for i in range(rows - 1, -1, -1):
        for j in range(cols - 1, -1, -1):
            size[i][j] = (
                size[i + 1][j + 1] + 1
                if old[i] == new[j]
                else max(size[i + 1][j], size[i][j + 1])
            )
    pairs: list[tuple[int, int]] = []
    i = j = 0
    while i < rows and j < cols:
        if old[i] == new[j]:
            pairs.append((i, j))
            i += 1
            j += 1
        elif size[i][j + 1] >= size[i + 1][j]:
            j += 1  # a tie leaves old[i] free for a later new section
        else:
            i += 1
    return pairs


def _removed(section: ScriptSection, index: int, key: str) -> SectionChange:
    return SectionChange(
        ChangeKind.REMOVED,
        section.kind,
        index,
        None,
        False,
        False,
        False,
        -section.words,
        key,
    )


def _added(section: ScriptSection, index: int, key: str) -> SectionChange:
    return SectionChange(
        ChangeKind.ADDED,
        section.kind,
        None,
        index,
        False,
        False,
        False,
        section.words,
        key,
    )


def _changed(
    old: ScriptSection, new: ScriptSection, i: int, j: int, key: str
) -> SectionChange:
    return SectionChange(
        ChangeKind.CHANGED,
        new.kind,
        i,
        j,
        old.text != new.text,
        old.title != new.title,
        old.estimated_seconds != new.estimated_seconds,
        new.words - old.words,
        key,
    )


def diff_scripts(old: Script, new: Script) -> ScriptDiff:
    """The diff metadata of ``new`` against ``old``, with no storage."""
    old_sections, new_sections = old.sections, new.sections
    old_keys = [section_sha256(section) for section in old_sections]
    new_keys = [section_sha256(section) for section in new_sections]
    entries: list[SectionChange] = []
    start_old = start_new = 0
    matches = _lcs(old_keys, new_keys)
    for end_old, end_new in [*matches, (len(old_sections), len(new_sections))]:
        entries += _gap(
            old_sections,
            new_sections,
            old_keys,
            new_keys,
            (start_old, end_old),
            (start_new, end_new),
        )
        if end_old < len(old_sections):  # not the end of the sections: a match
            entries.append(
                SectionChange(
                    ChangeKind.UNCHANGED,
                    old_sections[end_old].kind,
                    end_old,
                    end_new,
                    False,
                    False,
                    False,
                    0,
                    new_keys[end_new],
                )
            )
        start_old, start_new = end_old + 1, end_new + 1
    return ScriptDiff(
        entries=tuple(entries),
        words_before=sum(section.words for section in old_sections),
        words_after=sum(section.words for section in new_sections),
        seconds_before=old.estimated_seconds,
        seconds_after=new.estimated_seconds,
        content_sha256=content_sha256(new_sections),
    )


def _gap(
    old_sections: Sequence[ScriptSection],
    new_sections: Sequence[ScriptSection],
    old_keys: Sequence[str],
    new_keys: Sequence[str],
    old_range: tuple[int, int],
    new_range: tuple[int, int],
) -> list[SectionChange]:
    """The entries of the sections between two matches, in index order."""
    (start_old, end_old), (start_new, end_new) = old_range, new_range
    old_kinds = [section.kind for section in old_sections[start_old:end_old]]
    new_kinds = [section.kind for section in new_sections[start_new:end_new]]
    pairs = [(start_old + a, start_new + b) for a, b in _lcs(old_kinds, new_kinds)]
    entries: list[SectionChange] = []
    i, j = start_old, start_new
    for pair_old, pair_new in [*pairs, (end_old, end_new)]:
        for k in range(i, pair_old):
            entries.append(_removed(old_sections[k], k, old_keys[k]))
        for k in range(j, pair_new):
            entries.append(_added(new_sections[k], k, new_keys[k]))
        if pair_old < end_old:  # not the end of the gap: a pair of one kind
            entries.append(
                _changed(
                    old_sections[pair_old],
                    new_sections[pair_new],
                    pair_old,
                    pair_new,
                    new_keys[pair_new],
                )
            )
        i, j = pair_old + 1, pair_new + 1
    return entries


def _check_diff_fits(diff: ScriptDiff, script: Script, parent: Script) -> None:
    """Refuse a diff that was not made from these two scripts."""
    old_indexes = [e.old_index for e in diff.entries if e.old_index is not None]
    new_indexes = [e.new_index for e in diff.entries if e.new_index is not None]
    if (
        diff.content_sha256 != content_sha256(script.sections)
        or len(old_indexes) != len(parent.sections)
        or len(new_indexes) != len(script.sections)
        or diff.words_before != sum(s.words for s in parent.sections)
        or diff.words_after != sum(s.words for s in script.sections)
        or diff.seconds_before != parent.estimated_seconds
        or diff.seconds_after != script.estimated_seconds
    ):
        raise ValueError("the diff does not belong to these two versions")


@dataclass(frozen=True)
class ScriptRevision:
    """The stored diff metadata of one script version against its parent."""

    id: str
    script_id: str
    parent_script_id: str
    content_item_id: str
    method: str
    version: int
    added_count: int
    removed_count: int
    changed_count: int
    unchanged_count: int
    entries_count: int
    words_before: int
    words_after: int
    seconds_before: int
    seconds_after: int
    content_sha256: str
    entries: tuple[SectionChange, ...]
    requested_by: Actor
    created_at: datetime

    def __post_init__(self) -> None:
        for name in ("id", "script_id", "parent_script_id", "content_item_id"):
            if not getattr(self, name):
                raise ValueError(f"{name} must not be empty")
        if self.script_id == self.parent_script_id:
            raise ValueError("a version cannot be its own parent")
        if (
            not isinstance(self.method, str)
            or not self.method.strip()
            or len(self.method) > MAX_METHOD
        ):
            raise ValueError(f"a method is 1 to {MAX_METHOD} characters")
        _whole("version", self.version, 2, MAX_VERSION)
        for name in (
            "added_count",
            "removed_count",
            "changed_count",
            "unchanged_count",
            "words_before",
            "words_after",
            "seconds_before",
            "seconds_after",
        ):
            _whole(name, getattr(self, name), 0)
        _whole("entries_count", self.entries_count, 0, MAX_ENTRIES)
        if (
            self.added_count
            + self.removed_count
            + self.changed_count
            + self.unchanged_count
            != self.entries_count
        ):
            raise ValueError("the counts must add up to entries_count")
        _sha("content_sha256", self.content_sha256)
        self._check_entries()
        if not isinstance(self.requested_by, Actor):
            raise TypeError("requested_by must be an Actor")
        if not isinstance(
            self.created_at, datetime
        ) or self.created_at.utcoffset() != timedelta(0):
            raise ValueError("created_at must be timezone-aware UTC")

    def _check_entries(self) -> None:
        if not isinstance(self.entries, tuple) or not all(
            isinstance(entry, SectionChange) for entry in self.entries
        ):
            raise TypeError("entries must be a tuple of SectionChange values")
        if len(self.entries) != self.entries_count:
            raise ValueError("entries_count must count the entries")
        for change, name in (
            (ChangeKind.ADDED, "added_count"),
            (ChangeKind.REMOVED, "removed_count"),
            (ChangeKind.CHANGED, "changed_count"),
            (ChangeKind.UNCHANGED, "unchanged_count"),
        ):
            counted = sum(entry.change is change for entry in self.entries)
            if counted != getattr(self, name):
                raise ValueError(f"{name} must count the {change.value} entries")
        if self.method != SCRIPT_DIFF_METHOD:
            return  # the rules below are those of the v1 method only
        if not self.entries:
            raise ValueError("a script has at least one section")
        for name in ("old_index", "new_index"):
            indexes = [
                getattr(entry, name)
                for entry in self.entries
                if getattr(entry, name) is not None
            ]
            if indexes != list(range(len(indexes))):
                raise ValueError(f"every {name} appears once, in order from 0")
        if sum(entry.words_delta for entry in self.entries) != (
            self.words_after - self.words_before
        ):
            raise ValueError("the word changes must add up to the words difference")

    @classmethod
    def create(
        cls,
        script: Script,
        parent: Script,
        diff: ScriptDiff,
        *,
        method: str = SCRIPT_DIFF_METHOD,
        requested_by: Actor,
        clock: Clock | None = None,
    ) -> "ScriptRevision":
        """A revision of ``script`` against its ``parent`` with ``diff``."""
        if script.parent_id != parent.id:
            raise ValueError("the parent is not the parent version of the script")
        _check_diff_fits(diff, script, parent)
        return cls(
            id=uuid.uuid4().hex,
            script_id=script.id,
            parent_script_id=parent.id,
            content_item_id=script.content_item_id,
            method=method,
            version=script.version,
            added_count=diff.count(ChangeKind.ADDED),
            removed_count=diff.count(ChangeKind.REMOVED),
            changed_count=diff.count(ChangeKind.CHANGED),
            unchanged_count=diff.count(ChangeKind.UNCHANGED),
            entries_count=len(diff.entries),
            words_before=diff.words_before,
            words_after=diff.words_after,
            seconds_before=diff.seconds_before,
            seconds_after=diff.seconds_after,
            content_sha256=diff.content_sha256,
            entries=diff.entries,
            requested_by=requested_by,
            created_at=clock() if clock else datetime.now(UTC),
        )

    def counts(self) -> dict[str, int]:
        """The scalar counts of the revision, as audited."""
        return {
            "added": self.added_count,
            "removed": self.removed_count,
            "changed": self.changed_count,
            "unchanged": self.unchanged_count,
            "words_before": self.words_before,
            "words_after": self.words_after,
            "seconds_before": self.seconds_before,
            "seconds_after": self.seconds_after,
        }

    def summary(self) -> "RevisionSummary":
        return RevisionSummary(
            id=self.id,
            added=self.added_count,
            removed=self.removed_count,
            changed=self.changed_count,
            unchanged=self.unchanged_count,
            words_before=self.words_before,
            words_after=self.words_after,
            seconds_before=self.seconds_before,
            seconds_after=self.seconds_after,
            content_sha256=self.content_sha256,
            requested_by=self.requested_by,
            created_at=self.created_at,
        )


@dataclass(frozen=True)
class RevisionSummary:
    """A revision without its entries, as the history shows it."""

    id: str
    added: int
    removed: int
    changed: int
    unchanged: int
    words_before: int
    words_after: int
    seconds_before: int
    seconds_after: int
    content_sha256: str
    requested_by: Actor
    created_at: datetime


@dataclass(frozen=True)
class ScriptHistoryEntry:
    """One version of a content item's script, with its revision when stored."""

    script_id: str
    version: int
    parent_id: str | None
    created_by: Actor | None
    reason: str | None
    strategy_version: int | None
    words: int
    seconds: int
    revision: RevisionSummary | None

    @classmethod
    def of(
        cls, script: Script, revision: ScriptRevision | None
    ) -> "ScriptHistoryEntry":
        return cls(
            script_id=script.id,
            version=script.version,
            parent_id=script.parent_id,
            created_by=script.created_by,
            reason=script.reason,
            strategy_version=script.strategy_version,
            words=sum(section.words for section in script.sections),
            seconds=script.estimated_seconds,
            revision=revision.summary() if revision else None,
        )
