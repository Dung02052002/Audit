"""F-073 Script Versioning: the diff of a script version against its parent.

Rules the user approved on 2026-10-04 (the storage and the versioner are tested
in ``test_script_versioner.py``):

- a revision is the diff metadata of a version (2 or later) against its parent:
  counts added, removed, changed and unchanged (their sum is the entry count),
  the words and estimated seconds before and after, the SHA-256 of the whole
  script and one entry per aligned section; no script text anywhere;
- alignment: the longest common subsequence of the exact section keys (kind,
  title, text, estimated seconds), then, in each gap between two matches, the
  sections of one kind paired in order as changed, the rest removed and added;
  a moved section is a removal and an addition; a tie keeps the earlier old
  index; ``diff_scripts`` is pure.
"""

import dataclasses
import hashlib
import json
import random
from datetime import UTC, datetime

import pytest

from ai_youtube_agent.content.script import (
    MAX_SECTIONS,
    Script,
    ScriptSection,
    SectionKind,
)
from ai_youtube_agent.content.script_revision import (
    MAX_ENTRIES,
    MAX_METHOD,
    SCRIPT_DIFF_METHOD,
    ChangeKind,
    ScriptDiff,
    ScriptRevision,
    SectionChange,
    content_sha256,
    diff_scripts,
    section_sha256,
)
from ai_youtube_agent.core.audit import Actor, ActorKind

T0 = datetime(2026, 10, 4, 12, 0, tzinfo=UTC)
USER = Actor(ActorKind.USER, "owner")
SECRET = "zebrafish"  # a word of the script that no revision may hold
HOOK, INTRO, BODY, CHAPTER, OUTRO, CTA = (
    SectionKind.HOOK,
    SectionKind.INTRO,
    SectionKind.BODY,
    SectionKind.CHAPTER,
    SectionKind.OUTRO,
    SectionKind.CTA,
)
ADDED, REMOVED, CHANGED, UNCHANGED = (
    ChangeKind.ADDED,
    ChangeKind.REMOVED,
    ChangeKind.CHANGED,
    ChangeKind.UNCHANGED,
)


def sec(
    text: str,
    kind: SectionKind = BODY,
    title: str | None = None,
    seconds: int | None = 10,
) -> ScriptSection:
    return ScriptSection(kind, text, title, seconds)


def script(*sections: ScriptSection, item: str = "item") -> Script:
    return Script.create(item, sections=sections)


def diff(old: tuple[ScriptSection, ...], new: tuple[ScriptSection, ...]) -> ScriptDiff:
    return diff_scripts(script(*old), script(*new))


def shape(result: ScriptDiff) -> list[tuple]:
    """(change, old_index, new_index) of every entry, in order."""
    return [(e.change, e.old_index, e.new_index) for e in result.entries]


def counts(result: ScriptDiff) -> tuple[int, int, int, int]:
    return tuple(
        result.count(change) for change in (ADDED, REMOVED, CHANGED, UNCHANGED)
    )


A = sec("Alpha one.")
B = sec("Bravo two words.")
C = sec("Charlie three here now.")


# The sections key and the script hash


def test_the_section_key_is_the_hash_of_its_canonical_json() -> None:
    section = sec("Hello world.")

    assert section_sha256(section) == (
        "c2cdeb59794f784692cf1493cabb7bfff1b5586f79f05a5a934c886b3d1bdc8e"
    )
    canonical = '{"kind":"body","seconds":10,"text":"Hello world.","title":null}'
    assert section_sha256(section) == hashlib.sha256(canonical.encode()).hexdigest()


def test_the_script_hash_is_the_hash_of_the_array_of_sections() -> None:
    sections = (sec("Hello world.", HOOK), sec("Bye now.", CTA, seconds=3))

    expected = json.dumps(
        [
            {"kind": "hook", "seconds": 10, "text": "Hello world.", "title": None},
            {"kind": "cta", "seconds": 3, "text": "Bye now.", "title": None},
        ],
        sort_keys=True,
        separators=(",", ":"),
    )

    assert content_sha256(sections) == hashlib.sha256(expected.encode()).hexdigest()
    assert content_sha256(sections) != content_sha256(sections[::-1])


def test_non_ascii_text_is_hashed_as_utf8_not_escaped() -> None:
    section = sec("Xin chào các bạn.")

    raw = '{"kind":"body","seconds":10,"text":"Xin chào các bạn.","title":null}'
    assert section_sha256(section) == hashlib.sha256(raw.encode("utf-8")).hexdigest()


def test_each_part_of_a_section_changes_its_key() -> None:
    base = sec("Same text.", CHAPTER, "Title")
    variants = [
        sec("Same text.", BODY, "Title"),
        sec("Same text.", CHAPTER, "Other"),
        sec("Same text!", CHAPTER, "Title"),
        sec("Same text.", CHAPTER, "Title", seconds=11),
        sec("Same text.", BODY),  # the kind and the title differ
    ]

    keys = {section_sha256(section) for section in [base, *variants]}

    assert len(keys) == len(variants) + 1


def test_the_key_uses_the_estimated_seconds() -> None:
    text = " ".join(["word"] * 25)  # 25 words at 150 per minute: 10 seconds
    given = ScriptSection(BODY, text, seconds=10)
    estimated = ScriptSection(BODY, text)

    assert estimated.estimated_seconds == 10
    assert section_sha256(given) == section_sha256(estimated)
    assert content_sha256([given]) == content_sha256([estimated])
    assert section_sha256(ScriptSection(BODY, text, seconds=11)) != section_sha256(
        given
    )


def test_the_hashes_are_lowercase_hex_and_stable() -> None:
    result = diff((A, B), (A, C))

    assert result.content_sha256 == content_sha256((A, C))
    assert diff((A, B), (A, C)).content_sha256 == result.content_sha256
    assert len(result.content_sha256) == 64
    assert result.content_sha256 == result.content_sha256.lower()


# Adding, removing and changing


def test_an_added_section() -> None:
    result = diff((A,), (A, B))

    assert shape(result) == [(UNCHANGED, 0, 0), (ADDED, None, 1)]
    added = result.entries[1]
    assert (added.kind, added.words_delta, added.sha256) == (
        BODY,
        3,
        section_sha256(B),
    )
    assert not (added.text_changed or added.title_changed or added.seconds_changed)
    assert counts(result) == (1, 0, 0, 1)
    assert (result.words_before, result.words_after) == (2, 5)
    assert (result.seconds_before, result.seconds_after) == (10, 20)


def test_a_removed_section() -> None:
    result = diff((A, B), (A,))

    assert shape(result) == [(UNCHANGED, 0, 0), (REMOVED, 1, None)]
    removed = result.entries[1]
    assert (removed.words_delta, removed.sha256) == (-3, section_sha256(B))
    assert counts(result) == (0, 1, 0, 1)
    assert (result.words_before, result.words_after) == (5, 2)
    assert (result.seconds_before, result.seconds_after) == (20, 10)


def test_a_changed_text() -> None:
    result = diff((A, B), (A, sec("Bravo two words and more.")))

    assert shape(result) == [(UNCHANGED, 0, 0), (CHANGED, 1, 1)]
    changed = result.entries[1]
    assert (changed.text_changed, changed.title_changed, changed.seconds_changed) == (
        True,
        False,
        False,
    )
    assert changed.words_delta == 2
    assert changed.sha256 == section_sha256(sec("Bravo two words and more."))
    assert counts(result) == (0, 0, 1, 1)


def test_a_changed_title_only() -> None:
    old = (sec("Same text.", CHAPTER, "Old title"),)
    new = (sec("Same text.", CHAPTER, "New title"),)

    [entry] = diff(old, new).entries

    assert (entry.change, entry.kind) == (CHANGED, CHAPTER)
    assert (entry.text_changed, entry.title_changed, entry.seconds_changed) == (
        False,
        True,
        False,
    )
    assert entry.words_delta == 0


def test_a_title_added_or_removed_is_a_title_change() -> None:
    [added] = diff(
        (sec("Same text."),), (sec("Same text.", BODY, "Now titled"),)
    ).entries
    [removed] = diff((sec("Same text.", BODY, "Titled"),), (sec("Same text."),)).entries

    assert (added.change, added.title_changed) == (CHANGED, True)
    assert (removed.change, removed.title_changed) == (CHANGED, True)


def test_a_changed_seconds_only() -> None:
    [entry] = diff(
        (sec("Same text.", seconds=10),), (sec("Same text.", seconds=40),)
    ).entries

    assert entry.change is CHANGED
    assert (entry.text_changed, entry.title_changed, entry.seconds_changed) == (
        False,
        False,
        True,
    )
    assert entry.words_delta == 0


def test_a_text_change_without_given_seconds_changes_the_estimate() -> None:
    old = (ScriptSection(BODY, " ".join(["word"] * 25)),)  # 10 seconds
    new = (ScriptSection(BODY, " ".join(["word"] * 50)),)  # 20 seconds

    result = diff(old, new)

    [entry] = result.entries
    assert (entry.text_changed, entry.seconds_changed) == (True, True)
    assert (result.seconds_before, result.seconds_after) == (10, 20)


def test_all_three_parts_can_change_at_once() -> None:
    old = (sec("Old text.", CHAPTER, "Old", 10),)
    new = (sec("New text here now.", CHAPTER, "New", 20),)

    [entry] = diff(old, new).entries

    assert (entry.text_changed, entry.title_changed, entry.seconds_changed) == (
        True,
        True,
        True,
    )
    assert entry.words_delta == 2


def test_an_identical_script_has_only_unchanged_entries() -> None:
    result = diff((A, B, C), (A, B, C))

    assert shape(result) == [(UNCHANGED, 0, 0), (UNCHANGED, 1, 1), (UNCHANGED, 2, 2)]
    assert counts(result) == (0, 0, 0, 3)
    assert result.words_before == result.words_after
    assert all(entry.words_delta == 0 for entry in result.entries)


# Alignment


def test_a_section_inserted_in_the_middle_shifts_the_new_indexes() -> None:
    x = sec("Xray inserted.")

    result = diff((A, B, C), (A, x, B, C))

    assert shape(result) == [
        (UNCHANGED, 0, 0),
        (ADDED, None, 1),
        (UNCHANGED, 1, 2),
        (UNCHANGED, 2, 3),
    ]


def test_a_section_removed_from_the_middle_shifts_the_old_indexes() -> None:
    result = diff((A, B, C), (A, C))

    assert shape(result) == [(UNCHANGED, 0, 0), (REMOVED, 1, None), (UNCHANGED, 2, 1)]


def test_a_moved_section_is_a_removal_and_an_addition() -> None:
    result = diff((A, B, C), (B, C, A))

    assert shape(result) == [
        (REMOVED, 0, None),
        (UNCHANGED, 1, 0),
        (UNCHANGED, 2, 1),
        (ADDED, None, 2),
    ]
    assert counts(result) == (1, 1, 0, 2)
    assert result.entries[0].sha256 == result.entries[3].sha256 == section_sha256(A)


def test_swapped_sections_keep_the_earlier_old_index_matched() -> None:
    result = diff((A, B), (B, A))

    assert shape(result) == [(ADDED, None, 0), (UNCHANGED, 0, 1), (REMOVED, 1, None)]


def test_duplicate_sections_match_the_earliest_pair() -> None:
    assert shape(diff((A, A), (A,))) == [(UNCHANGED, 0, 0), (REMOVED, 1, None)]
    assert shape(diff((A,), (A, A))) == [(UNCHANGED, 0, 0), (ADDED, None, 1)]
    assert shape(diff((A, B, A), (A, A))) == [
        (UNCHANGED, 0, 0),
        (REMOVED, 1, None),
        (UNCHANGED, 2, 1),
    ]


def test_sections_of_one_kind_in_a_gap_are_paired_in_order() -> None:
    old = (sec("old a one."), sec("old b two."), sec("old c three."))
    new = (sec("new a one."), sec("new b two."))

    result = diff(old, new)

    assert shape(result) == [(CHANGED, 0, 0), (CHANGED, 1, 1), (REMOVED, 2, None)]


def test_a_change_between_matches_is_found_in_its_gap() -> None:
    hook, cta = sec("Hook words.", HOOK), sec("Follow.", CTA)

    result = diff(
        (hook, sec("Old body text."), cta), (hook, sec("New body text here."), cta)
    )

    assert shape(result) == [(UNCHANGED, 0, 0), (CHANGED, 1, 1), (UNCHANGED, 2, 2)]
    assert [e.kind for e in result.entries] == [HOOK, BODY, CTA]


def test_sections_of_different_kinds_are_not_paired() -> None:
    old = (sec("Same words here.", BODY),)
    new = (sec("Same words here.", CHAPTER, "A title"),)

    result = diff(old, new)

    assert shape(result) == [(REMOVED, 0, None), (ADDED, None, 0)]
    assert [e.kind for e in result.entries] == [BODY, CHAPTER]


def test_crossing_kinds_are_paired_once_and_stay_in_order() -> None:
    old = (sec("old body.", BODY), sec("old chapter.", CHAPTER, "T"))
    new = (sec("new chapter.", CHAPTER, "T"), sec("new body.", BODY))

    result = diff(old, new)

    assert shape(result) == [(ADDED, None, 0), (CHANGED, 0, 1), (REMOVED, 1, None)]


def test_a_changed_section_reports_the_kind_of_both_sides() -> None:
    [entry] = diff((sec("old.", CHAPTER, "T"),), (sec("new.", CHAPTER, "T"),)).entries

    assert (entry.change, entry.kind) == (CHANGED, CHAPTER)


def test_the_words_of_the_entries_add_up_to_the_words_difference() -> None:
    result = diff((A, B, C), (C, sec("Brand new content of eight whole words."), A))

    assert (
        sum(entry.words_delta for entry in result.entries)
        == result.words_after - result.words_before
    )


# Size, determinism


def test_a_script_of_one_hundred_sections() -> None:
    old = tuple(sec(f"old section number {n}.") for n in range(MAX_SECTIONS))
    new = old[:50] + (sec("one changed section here."),) + old[51:]

    result = diff(old, new)

    assert counts(result) == (0, 0, 1, 99)
    assert [e.old_index for e in result.entries] == list(range(100))
    assert [e.new_index for e in result.entries] == list(range(100))


def test_a_full_rewrite_of_one_hundred_sections_has_two_hundred_entries() -> None:
    old = tuple(sec(f"old section number {n}.") for n in range(MAX_SECTIONS))
    new = tuple(
        sec(f"new chapter number {n}.", CHAPTER, f"Title {n}")
        for n in range(MAX_SECTIONS)
    )
    before, after = script(*old), script(*new)

    result = diff_scripts(before, after)
    revision = ScriptRevision.create(
        dataclasses.replace(after, version=2, parent_id=before.id),
        before,
        result,
        requested_by=USER,
    )

    assert len(result.entries) == MAX_ENTRIES == 200
    assert counts(result) == (100, 100, 0, 0)
    assert revision.entries_count == 200


def test_the_diff_is_deterministic() -> None:
    old = (A, B, A, C, B)
    new = (B, A, C, A, A)

    first = diff(old, new)

    assert diff(old, new) == first
    # The ids and the times of the scripts play no part.
    assert diff_scripts(script(*old, item="x"), script(*new, item="y")) == first


@pytest.mark.parametrize("seed", range(40))
def test_any_pair_of_scripts_gives_a_consistent_alignment(seed: int) -> None:
    rng = random.Random(seed)
    pool = [
        sec(text, kind, "T" if kind is CHAPTER else None, seconds)
        for text in ("one", "two words", "three words here")
        for kind in (HOOK, BODY, CHAPTER)
        for seconds in (None, 5, 9)
    ]
    old = tuple(rng.choice(pool) for _ in range(rng.randint(1, 12)))
    new = tuple(rng.choice(pool) for _ in range(rng.randint(1, 12)))

    result = diff(old, new)

    old_indexes = [e.old_index for e in result.entries if e.old_index is not None]
    new_indexes = [e.new_index for e in result.entries if e.new_index is not None]
    assert old_indexes == list(range(len(old)))
    assert new_indexes == list(range(len(new)))
    assert sum(counts(result)) == len(result.entries)
    assert (
        sum(entry.words_delta for entry in result.entries)
        == result.words_after - result.words_before
    )
    for entry in result.entries:
        if entry.change is UNCHANGED:
            # "unchanged" is defined on the estimated seconds, so compare keys.
            assert section_sha256(old[entry.old_index]) == section_sha256(
                new[entry.new_index]
            )
        if entry.change is CHANGED:
            assert old[entry.old_index].kind == new[entry.new_index].kind
            assert section_sha256(old[entry.old_index]) != section_sha256(
                new[entry.new_index]
            )
    # The unchanged sections are a longest common subsequence: never fewer than
    # the sections both scripts keep in order, here checked with a brute force.
    keys_old = [section_sha256(s) for s in old]
    keys_new = [section_sha256(s) for s in new]
    assert result.count(UNCHANGED) == longest_common(keys_old, keys_new)


def longest_common(left: list[str], right: list[str]) -> int:
    table = [[0] * (len(right) + 1) for _ in range(len(left) + 1)]
    for i, a in enumerate(left):
        for j, b in enumerate(right):
            table[i + 1][j + 1] = (
                table[i][j] + 1 if a == b else max(table[i][j + 1], table[i + 1][j])
            )
    return table[-1][-1]


# No script text


def test_a_diff_and_its_revision_hold_no_script_text() -> None:
    old = (sec(f"A {SECRET} hook.", HOOK), sec(f"The {SECRET} body.", BODY, "Zebra"))
    new = (sec(f"A {SECRET} hook!", HOOK), sec(f"Another {SECRET} body.", BODY))
    before = script(*old)
    after = dataclasses.replace(script(*new), version=2, parent_id=before.id)

    result = diff_scripts(before, after)
    revision = ScriptRevision.create(after, before, result, requested_by=USER)

    for value in (result, revision, revision.summary(), revision.counts()):
        assert SECRET not in repr(value) and "Zebra" not in repr(value)
    assert all(isinstance(v, int) for v in revision.counts().values())


# The entry


def entry(**change) -> SectionChange:
    fields = {
        "change": UNCHANGED,
        "kind": BODY,
        "old_index": 0,
        "new_index": 0,
        "text_changed": False,
        "title_changed": False,
        "seconds_changed": False,
        "words_delta": 0,
        "sha256": "a" * 64,
    }
    return SectionChange(**{**fields, **change})


def test_a_valid_entry_of_each_change() -> None:
    entry()
    entry(change=ADDED, old_index=None, new_index=0, words_delta=3)
    entry(change=REMOVED, old_index=0, new_index=None, words_delta=-3)
    entry(change=CHANGED, text_changed=True, words_delta=-2)
    entry(change=CHANGED, title_changed=True)
    entry(change=CHANGED, seconds_changed=True, sha256=None)
    entry(change=ADDED, old_index=None, words_delta=0)


@pytest.mark.parametrize(
    "change",
    [
        dict(change="added"),
        dict(kind="body"),
        dict(old_index=-1),
        dict(old_index=100),
        dict(new_index=100),
        dict(old_index=True),
        dict(old_index=1.0),
        dict(text_changed=1),
        dict(title_changed=None),
        dict(words_delta=1.5),
        dict(words_delta=True),
        dict(sha256="A" * 64),
        dict(sha256="a" * 63),
        dict(sha256="g" * 64),
        # unchanged: no flag, no word change, both indexes
        dict(text_changed=True),
        dict(words_delta=1),
        dict(new_index=None),
        dict(old_index=None),
        # added: no old index, a new one, no flag, no negative words
        dict(change=ADDED),
        dict(change=ADDED, old_index=None, new_index=None),
        dict(change=ADDED, old_index=None, text_changed=True),
        dict(change=ADDED, old_index=None, words_delta=-1),
        # removed: an old index, no new one, no flag, no positive words
        dict(change=REMOVED),
        dict(change=REMOVED, new_index=None, old_index=None),
        dict(change=REMOVED, new_index=None, seconds_changed=True),
        dict(change=REMOVED, new_index=None, words_delta=1),
        # changed: both indexes, a flag, words only with a text change
        dict(change=CHANGED),
        dict(change=CHANGED, text_changed=True, new_index=None),
        dict(change=CHANGED, title_changed=True, words_delta=2),
    ],
)
def test_an_invalid_entry_is_refused(change) -> None:
    with pytest.raises((ValueError, TypeError)):
        entry(**change)


# The revision


def versions(
    old: tuple[ScriptSection, ...] = (A, B), new: tuple[ScriptSection, ...] = (A, C)
) -> tuple[Script, Script]:
    first = script(*old)
    return first, first.next_version(sections=new, created_by=USER)


def revision(**change) -> ScriptRevision:
    first, second = versions()
    made = ScriptRevision.create(
        second, first, diff_scripts(first, second), requested_by=USER, clock=lambda: T0
    )
    return dataclasses.replace(made, **change) if change else made


def test_a_revision_of_a_version_against_its_parent() -> None:
    first, second = versions()

    made = ScriptRevision.create(
        second, first, diff_scripts(first, second), requested_by=USER, clock=lambda: T0
    )

    assert (made.script_id, made.parent_script_id) == (second.id, first.id)
    assert (made.content_item_id, made.version) == ("item", 2)
    assert made.method == SCRIPT_DIFF_METHOD == "script-diff-v1"
    assert (made.requested_by, made.created_at) == (USER, T0)
    assert (made.added_count, made.removed_count) == (0, 0)
    assert (made.changed_count, made.unchanged_count, made.entries_count) == (1, 1, 2)
    assert (made.words_before, made.words_after) == (5, 6)
    assert (made.seconds_before, made.seconds_after) == (20, 20)
    assert made.content_sha256 == content_sha256((A, C))
    assert made.counts() == {
        "added": 0,
        "removed": 0,
        "changed": 1,
        "unchanged": 1,
        "words_before": 5,
        "words_after": 6,
        "seconds_before": 20,
        "seconds_after": 20,
    }
    summary = made.summary()
    assert (summary.id, summary.added, summary.changed) == (made.id, 0, 1)
    assert summary.content_sha256 == made.content_sha256
    assert not hasattr(summary, "entries")


def test_a_revision_without_a_change_is_allowed() -> None:
    first = script(A, B)
    second = dataclasses.replace(script(A, B), version=2, parent_id=first.id)

    made = ScriptRevision.create(
        second, first, diff_scripts(first, second), requested_by=USER
    )

    assert (made.changed_count, made.unchanged_count) == (0, 2)
    assert made.content_sha256 == content_sha256((A, B))


def test_a_revision_needs_the_parent_of_the_script() -> None:
    first, second = versions()
    other = script(A, B)

    with pytest.raises(ValueError, match="parent"):
        ScriptRevision.create(
            second, other, diff_scripts(other, second), requested_by=USER
        )


def test_a_revision_needs_a_diff_of_these_two_versions() -> None:
    first, second = versions()
    other = script(B, A, C)
    third = script(A, C, B)
    wrong = (
        diff_scripts(first, other),  # another content hash
        diff_scripts(first, third),  # one entry too many for the sections
        diff_scripts(other, second),  # the old sections are not the parent
    )

    for bad in wrong:
        with pytest.raises(ValueError, match="does not belong"):
            ScriptRevision.create(second, first, bad, requested_by=USER)


def test_declared_and_missing_seconds_with_one_estimate_share_a_hash() -> None:
    declared = sec("one two three four five six", seconds=3)
    missing = sec("one two three four five six", seconds=None)
    assert declared.estimated_seconds == missing.estimated_seconds

    assert section_sha256(declared) == section_sha256(missing)
    assert content_sha256((declared,)) == content_sha256((missing,))


def test_the_first_version_has_no_revision() -> None:
    first = script(A)

    with pytest.raises(ValueError):
        ScriptRevision.create(
            first, first, diff_scripts(first, first), requested_by=USER
        )


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (dict(id=""), "id"),
        (dict(script_id=""), "script_id"),
        (dict(parent_script_id=""), "parent_script_id"),
        (dict(content_item_id=""), "content_item_id"),
        (dict(parent_script_id="same"), "own parent"),
        (dict(method=" "), "method"),
        (dict(method="x" * (MAX_METHOD + 1)), "method"),
        (dict(version=1), "version"),
        (dict(version=True), "version"),
        (dict(added_count=-1), "added_count"),
        (dict(changed_count=2), "add up"),
        (dict(entries_count=3), "add up"),
        (dict(entries_count=MAX_ENTRIES + 1), "entries_count"),
        (dict(words_before=-1), "words_before"),
        (dict(words_after=1.5), "words_after"),
        (dict(seconds_before=-1), "seconds_before"),
        (dict(seconds_after=-1), "seconds_after"),
        (dict(content_sha256="X" * 64), "content_sha256"),
        (dict(content_sha256="a" * 10), "content_sha256"),
        (dict(entries=[]), "tuple"),
        (dict(entries=("x", "y")), "tuple"),
        (dict(entries=()), "entries_count"),
        (dict(requested_by="owner"), "Actor"),
        (dict(created_at=datetime(2026, 10, 4)), "UTC"),
    ],
)
def test_an_invalid_revision_is_refused(change, message) -> None:
    made = revision()
    if change.get("parent_script_id") == "same":
        change = {"parent_script_id": made.script_id}

    with pytest.raises((ValueError, TypeError), match=message):
        dataclasses.replace(made, **change)


def test_the_counts_must_count_the_entries() -> None:
    made = revision()

    with pytest.raises(ValueError, match="added_count"):
        dataclasses.replace(made, added_count=1, changed_count=0)


def test_the_v1_rules_check_the_indexes_and_the_words() -> None:
    made = revision()
    first, second = made.entries
    swapped = (second, first)

    with pytest.raises(ValueError, match="in order"):
        dataclasses.replace(made, entries=swapped)
    with pytest.raises(ValueError, match="in order"):
        dataclasses.replace(
            made, entries=(first, dataclasses.replace(second, new_index=3))
        )
    with pytest.raises(ValueError, match="words"):
        dataclasses.replace(made, words_after=made.words_after + 1)


def test_the_v1_rules_do_not_bind_another_method() -> None:
    made = revision()
    first, second = made.entries

    other = dataclasses.replace(
        made,
        method="script-diff-v2",
        entries=(second, first),
        words_after=made.words_after + 1,
    )

    assert other.method == "script-diff-v2"
