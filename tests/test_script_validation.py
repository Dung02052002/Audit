"""F-072 Script Validator rules (Prompt Pack v8, prompt #072).

Rules the user approved on 2026-10-04, tested here on hand-made scripts and
strategies (the validator, storage and the generators' scripts are in
``test_script_validator.py``):

- sections (FAIL): Shorts are HOOK, 1-3 BODY, CTA; LongForm are HOOK, INTRO,
  3-12 chapter sections (CHAPTER when the format has chapters on, BODY when it
  is off), OUTRO, CTA; strict set and order;
- length (FAIL): the estimated seconds against the script's duration target,
  else the strategy format range of the content type, limits inclusive;
- language (FAIL or WARN): a deterministic EN/VI heuristic, 10 signal hits
  needed, 80% / 60% of the hits in a known language that is not allowed;
- banned phrases (FAIL) and hook length (WARN), never the phrase in a result;
- a result keeps ids, counts, seconds and scores only, and its status is the
  worst finding.
"""

import dataclasses
import itertools
import unicodedata
from datetime import UTC, datetime

import pytest

from ai_youtube_agent.content import longform_script_generator as generator
from ai_youtube_agent.content import script_validation as rules
from ai_youtube_agent.content.hook import HOOK_LIMITS
from ai_youtube_agent.content.script import (
    DurationTarget,
    Script,
    ScriptSection,
    SectionKind,
)
from ai_youtube_agent.content.script_validation import (
    EN_WORDS,
    MAX_FINDINGS,
    MAX_WORDS,
    MIN_HITS,
    SCRIPT_VALIDATION_METHOD,
    VI_LETTERS,
    VI_WORDS,
    Finding,
    ScriptAnalysis,
    ScriptValidation,
    ValidationCode,
    ValidationStatus,
    validate_script,
)
from ai_youtube_agent.content.strategy import (
    Brand,
    FormatSettings,
    LanguageSettings,
    LongFormFormat,
    ShortsFormat,
)
from ai_youtube_agent.core.audit import Actor, ActorKind
from ai_youtube_agent.core.content_item import ContentType
from factories import make_channel, make_strategy_profile

USER = Actor(ActorKind.USER, "owner")
T0 = datetime(2026, 10, 4, 12, 0, tzinfo=UTC)
PASS, WARN, FAIL = (
    ValidationStatus.PASS,
    ValidationStatus.WARN,
    ValidationStatus.FAIL,
)
CODE = ValidationCode
HOOK, INTRO, BODY, CHAPTER, OUTRO, CTA = (
    SectionKind.HOOK,
    SectionKind.INTRO,
    SectionKind.BODY,
    SectionKind.CHAPTER,
    SectionKind.OUTRO,
    SectionKind.CTA,
)
SHORTS, LONGFORM = ContentType.SHORTS, ContentType.LONGFORM
FILLER = "zzz"  # a word that is a signal of no language
HOOK_TEXT = "Short hook."
CTA_TEXT = "Follow please."
PLAIN = "Plain words here."  # no signal word of either language


def strategy(
    primary: str = "en",
    secondary: tuple[str, ...] = (),
    *,
    banned: tuple[str, ...] = (),
    chapters: bool = True,
    **change,
):
    values = {
        "languages": LanguageSettings(primary, secondary),
        "brand": Brand("Brand", banned_phrases=banned),
        "format": FormatSettings(
            ShortsFormat(15, 60), LongFormFormat(480, 900, chapters=chapters)
        ),
    }
    return make_strategy_profile(make_channel(), **{**values, **change})


def section(kind: SectionKind, text: str = PLAIN) -> ScriptSection:
    title = "A chapter" if kind is CHAPTER else None
    return ScriptSection(kind, text, title)


def build(*sections: ScriptSection, total: int = 30, **change) -> Script:
    """A script whose estimated seconds are exactly ``total``."""
    seconds = [1] * len(sections)
    seconds[0] += total - len(sections)
    timed = tuple(
        dataclasses.replace(s, seconds=n)
        for s, n in zip(sections, seconds, strict=True)
    )
    return Script.create("item", sections=timed, clock=lambda: T0, **change)


def shorts(*bodies: str, hook: str = HOOK_TEXT, **change) -> Script:
    return build(
        section(HOOK, hook),
        *(section(BODY, text) for text in (bodies or (PLAIN,))),
        section(CTA, CTA_TEXT),
        **change,
    )


def longform(
    chapters: int = 3, kind: SectionKind = CHAPTER, total: int = 600, **change
) -> Script:
    return build(
        section(HOOK, HOOK_TEXT),
        section(INTRO),
        *(section(kind) for _ in range(chapters)),
        section(OUTRO),
        section(CTA, CTA_TEXT),
        total=total,
        **change,
    )


def run(script: Script, *, content_type=SHORTS, profile=None) -> ScriptAnalysis:
    return validate_script(script, content_type, profile or strategy())


def found(analysis: ScriptAnalysis) -> list[tuple[str, int | None]]:
    return [(f.code.value, f.section_index) for f in analysis.findings]


def codes(analysis: ScriptAnalysis) -> list[str]:
    return [f.code.value for f in analysis.findings]


def cycle(words: frozenset[str], count: int) -> str:
    return " ".join(itertools.islice(itertools.cycle(sorted(words)), count))


def english(count: int) -> str:
    return cycle(EN_WORDS, count)


def vietnamese(count: int) -> str:
    return cycle(VI_WORDS, count)


def join(*parts: str) -> str:
    return " ".join(part for part in parts if part)


def mixed(vi: int, en: int, filler: int = 5) -> str:
    return join(vietnamese(vi), english(en), *[FILLER] * filler)


# Sections: Shorts


def test_a_good_shorts_script_passes() -> None:
    analysis = run(shorts("One body.", "Another body."))

    assert analysis.findings == ()
    assert analysis.strategy_version == 1


@pytest.mark.parametrize("bodies", [1, 2, 3])
def test_one_to_three_body_sections_are_valid(bodies: int) -> None:
    assert run(shorts(*["A body."] * bodies)).findings == ()


def test_a_fourth_body_section_is_unexpected() -> None:
    analysis = run(shorts(*["A body."] * 4))

    assert analysis.findings == (Finding(CODE.UNEXPECTED_SECTION, FAIL, 4),)


def test_no_body_section_is_missing() -> None:
    script = build(section(HOOK), section(CTA))

    assert run(script).findings == (Finding(CODE.MISSING_SECTION, FAIL),)


@pytest.mark.parametrize(
    "kinds",
    [
        (BODY, CTA),
        (HOOK, BODY),
        (BODY,),
    ],
)
def test_each_missing_required_kind_is_one_finding(kinds) -> None:
    script = build(*(section(kind) for kind in kinds))
    expected = {HOOK, BODY, CTA} - set(kinds)

    assert codes(run(script)) == ["missing_section"] * len(expected)


@pytest.mark.parametrize("kind", [INTRO, CHAPTER, OUTRO])
def test_a_kind_shorts_do_not_have_is_unexpected(kind: SectionKind) -> None:
    script = build(section(HOOK), section(kind), section(BODY), section(CTA))

    assert found(run(script)) == [("unexpected_section", 1)]


@pytest.mark.parametrize("kind", [HOOK, CTA])
def test_a_second_hook_or_cta_is_unexpected(kind: SectionKind) -> None:
    sections = [section(HOOK), section(BODY), section(CTA)]
    sections.insert(2 if kind is HOOK else 3, section(kind))

    assert found(run(build(*sections))) == [("unexpected_section", 2 + (kind is CTA))]


def test_the_hook_must_come_first() -> None:
    script = build(section(BODY), section(HOOK), section(CTA))

    assert found(run(script)) == [("wrong_order", 1)]


def test_the_cta_must_come_last() -> None:
    script = build(section(HOOK), section(CTA), section(BODY))

    assert found(run(script)) == [("wrong_order", 2)]


def test_only_the_first_section_out_of_order_is_found() -> None:
    script = build(section(CTA), section(BODY), section(HOOK), section(BODY))

    assert found(run(script)) == [("wrong_order", 1)]


def test_an_unexpected_section_does_not_break_the_order() -> None:
    script = build(section(HOOK), section(OUTRO), section(BODY), section(CTA))

    assert found(run(script)) == [("unexpected_section", 1)]


def test_a_script_of_one_body_section_misses_the_hook_and_the_cta() -> None:
    script = Script.create("item", "Just plain text.", clock=lambda: T0)

    assert codes(run(script)) == ["missing_section", "missing_section", "too_short"]


# Sections: LongForm


def test_a_good_longform_script_passes() -> None:
    assert run(longform(), content_type=LONGFORM).findings == ()


@pytest.mark.parametrize("chapters", [3, 12])
def test_three_to_twelve_chapters_are_valid(chapters: int) -> None:
    assert run(longform(chapters), content_type=LONGFORM).findings == ()


@pytest.mark.parametrize(("chapters", "count"), [(0, 0), (1, 1), (2, 2), (13, 13)])
def test_a_chapter_count_outside_three_to_twelve_fails(chapters, count) -> None:
    analysis = run(longform(chapters), content_type=LONGFORM)

    assert analysis.findings == (
        Finding(CODE.CHAPTER_COUNT, FAIL, actual=count, minimum=3, maximum=12),
    )


def test_a_longform_script_misses_each_of_its_fixed_sections() -> None:
    script = build(section(CHAPTER), section(CHAPTER), section(CHAPTER), total=600)

    assert codes(run(script, content_type=LONGFORM)) == ["missing_section"] * 4


@pytest.mark.parametrize("kind", [HOOK, INTRO, OUTRO, CTA])
def test_a_second_fixed_longform_section_is_unexpected(kind: SectionKind) -> None:
    sections = list(longform().sections)
    sections.insert(len(sections), section(kind))
    script = build(*sections, total=600)
    analysis = run(script, content_type=LONGFORM)

    assert found(analysis)[0] == ("unexpected_section", len(sections) - 1)


def mixed_kinds(chapters: int, bodies: int) -> Script:
    """A LongForm script with CHAPTER sections, then BODY sections."""
    return build(
        section(HOOK, HOOK_TEXT),
        section(INTRO),
        *[section(CHAPTER)] * chapters,
        *[section(BODY)] * bodies,
        section(OUTRO),
        section(CTA, CTA_TEXT),
        total=600,
    )


@pytest.mark.parametrize("count", [3, 7, 12])
def test_body_sections_are_one_chapter_kind_finding_when_chapters_are_on(
    count: int,
) -> None:
    analysis = run(longform(count, kind=BODY), content_type=LONGFORM)

    assert analysis.findings == (Finding(CODE.CHAPTER_KIND, FAIL, 2, actual=count),)


@pytest.mark.parametrize("count", [3, 7, 12])
def test_chapter_sections_are_one_chapter_kind_finding_when_chapters_are_off(
    count: int,
) -> None:
    profile = strategy(chapters=False)

    analysis = run(longform(count), content_type=LONGFORM, profile=profile)

    assert analysis.findings == (Finding(CODE.CHAPTER_KIND, FAIL, 2, actual=count),)
    assert analysis.findings[0].status is FAIL


def test_the_chapter_kind_finding_points_to_the_first_such_section() -> None:
    script = build(
        section(HOOK, HOOK_TEXT),
        section(INTRO),
        *[section(BODY)] * 3,
        section(OUTRO),
        section(CTA, CTA_TEXT),
        total=600,
    )
    other = [i for i, s in enumerate(script.sections) if s.kind is BODY]

    analysis = run(script, content_type=LONGFORM)

    assert analysis.findings[0].section_index == other[0] == 2
    assert analysis.findings[0].actual == len(other) == 3


def test_mixed_chapter_kinds_keep_unexpected_section_and_chapter_count() -> None:
    analysis = run(mixed_kinds(chapters=3, bodies=2), content_type=LONGFORM)

    assert found(analysis) == [("unexpected_section", 5), ("unexpected_section", 6)]
    assert CODE.CHAPTER_KIND not in {f.code for f in analysis.findings}
    few = run(mixed_kinds(chapters=1, bodies=3), content_type=LONGFORM)
    assert found(few) == [
        ("unexpected_section", 3),
        ("unexpected_section", 4),
        ("unexpected_section", 5),
        ("chapter_count", None),
    ]


@pytest.mark.parametrize("count", [1, 2, 13])
def test_the_other_kind_with_a_wrong_count_keeps_the_old_codes(count: int) -> None:
    analysis = run(longform(count, kind=BODY), content_type=LONGFORM)

    assert codes(analysis) == ["unexpected_section"] * count + ["chapter_count"]
    assert analysis.findings[-1] == Finding(
        CODE.CHAPTER_COUNT, FAIL, actual=0, minimum=3, maximum=12
    )
    profile = strategy(chapters=False)
    off = run(longform(count), content_type=LONGFORM, profile=profile)
    assert codes(off) == ["unexpected_section"] * count + ["chapter_count"]


def test_shorts_never_give_a_chapter_kind_finding() -> None:
    script = build(
        section(HOOK, HOOK_TEXT),
        *[section(CHAPTER)] * 3,
        section(CTA, CTA_TEXT),
    )

    assert set(codes(run(script))) == {"missing_section", "unexpected_section"}


def test_the_chapter_kind_is_next_to_the_other_findings() -> None:
    script = build(
        section(HOOK, HOOK_TEXT),
        section(INTRO),
        section(INTRO),
        *[section(BODY)] * 3,
        section(OUTRO),
        section(CTA, CTA_TEXT),
        total=600,
    )

    assert found(run(script, content_type=LONGFORM)) == [
        ("unexpected_section", 2),
        ("chapter_kind", 3),
    ]


@pytest.mark.parametrize("chapters", [3, 12])
def test_body_sections_are_the_chapters_when_chapters_are_off(chapters) -> None:
    profile = strategy(chapters=False)

    analysis = run(
        longform(chapters, kind=BODY), content_type=LONGFORM, profile=profile
    )

    assert analysis.findings == ()


def test_the_chapter_count_of_body_sections_is_checked_when_chapters_are_off() -> None:
    profile = strategy(chapters=False)

    analysis = run(longform(2, kind=BODY), content_type=LONGFORM, profile=profile)

    assert analysis.findings == (
        Finding(CODE.CHAPTER_COUNT, FAIL, actual=2, minimum=3, maximum=12),
    )


@pytest.mark.parametrize(
    ("kinds", "index"),
    [
        ((HOOK, CHAPTER, INTRO, CHAPTER, CHAPTER, OUTRO, CTA), 2),
        ((HOOK, INTRO, CHAPTER, CHAPTER, OUTRO, CHAPTER, CTA), 5),
        ((HOOK, INTRO, CHAPTER, CHAPTER, CHAPTER, CTA, OUTRO), 6),
        ((INTRO, HOOK, CHAPTER, CHAPTER, CHAPTER, OUTRO, CTA), 1),
    ],
)
def test_the_longform_order_is_strict(kinds, index) -> None:
    script = build(*(section(kind) for kind in kinds), total=600)

    assert found(run(script, content_type=LONGFORM)) == [("wrong_order", index)]


def test_a_chapter_needs_a_title() -> None:
    # The rule "a chapter has a title" is held by the section itself, so a
    # stored script never has an untitled chapter.
    with pytest.raises(ValueError, match="needs a title"):
        ScriptSection(CHAPTER, PLAIN)


def test_the_longform_flag_is_not_read_by_the_rules() -> None:
    # The rules take the script and the strategy only: a LongForm script is
    # validated whatever the LONGFORM_ENABLED flag.
    parameters = rules.validate_script.__annotations__
    assert set(parameters) == {"script", "content_type", "strategy", "return"}


def test_the_chapter_limits_match_the_generator() -> None:
    assert rules.LONGFORM_MIN_CHAPTERS == generator.MIN_CHAPTERS == 3
    assert rules.LONGFORM_MAX_CHAPTERS == generator.MAX_CHAPTERS == 12


# Length


def test_the_strategy_range_is_used_without_a_duration_target() -> None:
    assert run(shorts(total=15)).findings == ()
    assert run(shorts(total=60)).findings == ()
    assert run(shorts(total=40)).findings == ()


def test_a_script_under_the_minimum_is_too_short() -> None:
    analysis = run(shorts(total=14))

    assert analysis.findings == (
        Finding(CODE.TOO_SHORT, FAIL, actual=14, minimum=15, maximum=60),
    )
    assert analysis.seconds == 14


def test_a_script_over_the_maximum_is_too_long() -> None:
    analysis = run(shorts(total=61))

    assert analysis.findings == (
        Finding(CODE.TOO_LONG, FAIL, actual=61, minimum=15, maximum=60),
    )


def test_a_longform_script_uses_the_longform_range() -> None:
    assert run(longform(total=480), content_type=LONGFORM).findings == ()
    assert run(longform(total=900), content_type=LONGFORM).findings == ()
    assert codes(run(longform(total=479), content_type=LONGFORM)) == ["too_short"]
    assert codes(run(longform(total=901), content_type=LONGFORM)) == ["too_long"]


def test_the_scripts_own_duration_target_wins() -> None:
    target = DurationTarget(100, 200)

    short = run(shorts(total=40, duration_target=target))
    exact = run(shorts(total=200, duration_target=target))

    assert short.findings == (
        Finding(CODE.TOO_SHORT, FAIL, actual=40, minimum=100, maximum=200),
    )
    assert exact.findings == ()


def test_the_estimate_is_made_from_the_words_without_section_seconds() -> None:
    # 150 words per minute, rounded up per section: 1 + 14 + 1 seconds.
    text = " ".join(["word"] * 35)
    script = Script.create(
        "item",
        sections=(
            ScriptSection(HOOK, "Two words"),
            ScriptSection(BODY, text),
            ScriptSection(CTA, "Follow"),
        ),
        clock=lambda: T0,
    )

    analysis = run(script)

    assert analysis.seconds == script.estimated_seconds == 16
    assert analysis.words == 38
    assert analysis.findings == ()


# Language


def lang(body: str, profile=None, **change) -> ScriptAnalysis:
    return run(shorts(body.strip(), total=40), profile=profile or strategy(**change))


def test_english_text_passes_for_an_english_channel() -> None:
    analysis = lang(english(40))

    assert analysis.findings == ()
    assert (analysis.language_checked, analysis.language_hits) == (True, 40)


def test_vietnamese_text_passes_for_a_vietnamese_channel() -> None:
    analysis = lang(vietnamese(40), strategy("vi"))

    assert analysis.findings == ()
    assert analysis.language_checked is True


def test_a_vietnamese_text_in_an_english_channel_is_a_mismatch() -> None:
    analysis = lang(vietnamese(40))

    [finding] = analysis.findings
    assert finding.code is CODE.LANGUAGE_MISMATCH
    assert (finding.status, finding.score, finding.matched) == (FAIL, 1.0, 40)
    assert finding.actual == analysis.language_hits == 40


def test_an_english_text_in_a_vietnamese_channel_is_a_mismatch() -> None:
    analysis = lang(english(40), strategy("vi"))

    assert [(f.code, f.status) for f in analysis.findings] == [
        (CODE.LANGUAGE_MISMATCH, FAIL)
    ]


def test_the_secondary_languages_are_allowed() -> None:
    assert lang(vietnamese(40), strategy("en", ("vi",))).findings == ()
    assert lang(english(40), strategy("vi", ("en-US",))).findings == ()


def test_tags_are_matched_by_their_base_subtag() -> None:
    assert lang(vietnamese(40), strategy("vi-VN")).findings == ()
    assert lang(english(40), strategy("en-GB")).findings == ()
    assert lang(vietnamese(40), strategy("en-GB", ("fr-CA",))).findings != ()


@pytest.mark.parametrize(
    ("vi", "en", "status"),
    [
        (100, 0, FAIL),
        (80, 20, FAIL),  # 80% exactly
        (79, 21, WARN),
        (70, 30, WARN),
        (60, 40, WARN),  # 60% exactly
        (59, 41, None),
        (10, 90, None),
        (0, 100, None),
    ],
)
def test_the_language_thresholds(vi: int, en: int, status) -> None:
    analysis = lang(mixed(vi, en))

    assert analysis.language_hits == vi + en
    if status is None:
        assert analysis.findings == ()
        return
    [finding] = analysis.findings
    assert finding.status is status
    assert finding.matched == vi
    assert finding.score == vi / (vi + en)


def test_the_score_is_rounded_down() -> None:
    # 2 of 3 is 0.666..., never rounded up to 0.667.
    analysis = lang(mixed(20, 10))

    assert analysis.findings[0].score == 0.666


def test_the_same_thresholds_hold_for_an_english_text_in_a_vietnamese_channel() -> None:
    assert lang(mixed(20, 80), strategy("vi")).findings[0].status is FAIL
    assert lang(mixed(30, 70), strategy("vi")).findings[0].status is WARN
    assert lang(mixed(50, 50), strategy("vi")).findings == ()


@pytest.mark.parametrize("hits", [0, 1, 9])
def test_fewer_than_ten_hits_are_not_checked(hits: int) -> None:
    analysis = lang(join(vietnamese(hits), *[FILLER] * 40))

    assert analysis.findings == ()
    assert (analysis.language_checked, analysis.language_hits) == (False, hits)


def test_ten_hits_are_checked() -> None:
    analysis = lang(join(vietnamese(MIN_HITS), *[FILLER] * 40))

    assert analysis.language_checked is True
    assert [f.code for f in analysis.findings] == [CODE.LANGUAGE_MISMATCH]


def test_the_language_is_not_checked_when_no_allowed_language_is_known() -> None:
    analysis = lang(vietnamese(40), strategy("fr", ("de",)))

    assert analysis.findings == ()
    assert (analysis.language_checked, analysis.language_hits) == (False, 0)


def test_a_known_language_that_is_not_allowed_is_found_next_to_an_unknown_one() -> None:
    analysis = lang(vietnamese(40), strategy("fr", ("en",)))

    assert [f.code for f in analysis.findings] == [CODE.LANGUAGE_MISMATCH]


def test_both_known_languages_allowed_never_mismatch() -> None:
    analysis = lang(mixed(30, 70), strategy("vi", ("en",)))

    assert analysis.findings == ()
    assert analysis.language_checked is True


def test_vietnamese_with_english_terms_passes() -> None:
    text = (
        "Hôm nay chúng ta sẽ tìm hiểu về budget và cách tiết kiệm tiền. "
        "Bạn có thể đặt mục tiêu cho từng tháng, theo dõi chi tiêu trong app "
        "và xem the dashboard with your friends. Đó là cách đơn giản nhất."
    )

    analysis = lang(text, strategy("vi"))

    assert analysis.findings == ()
    assert analysis.language_checked is True


def test_english_terms_alone_carry_no_signal() -> None:
    analysis = lang("budget dashboard app savings investing " * 10, strategy("vi"))

    assert (analysis.language_checked, analysis.language_hits) == (False, 0)


def test_nfd_vietnamese_is_read_as_nfc() -> None:
    text = vietnamese(40)
    decomposed = unicodedata.normalize("NFD", text)
    assert decomposed != text

    analysis = lang(decomposed, strategy("vi"))

    assert (analysis.language_checked, analysis.language_hits) == (True, 40)
    assert lang(decomposed).findings[0].status is FAIL


def test_case_is_ignored() -> None:
    assert lang(vietnamese(40).upper(), strategy("vi")).language_hits == 40
    assert lang(english(40).upper()).language_hits == 40


def test_the_distinctive_vietnamese_letters_are_signals() -> None:
    # None of these words is in the word lists; each has a letter of VI_LETTERS.
    words = ["kiệm", "giải", "tiết", "chẽ", "đồ", "ướt", "mạnh", "yếu", "lộc", "ăn"]
    assert not set(words) & VI_WORDS

    analysis = lang(" ".join(words), strategy("vi"))

    assert analysis.language_hits == 10


def test_the_letters_shared_with_other_languages_are_not_signals() -> None:
    # é, è, à, ñ, ü, ã, â, ê, ô are ordinary letters of French, Spanish and others.
    analysis = lang("café très señor über maçã bâtiment fenêtre hôtel " * 3)

    assert (analysis.language_checked, analysis.language_hits) == (False, 0)


def test_the_word_lists_do_not_overlap() -> None:
    assert not EN_WORDS & VI_WORDS
    for dropped in ("do", "no", "me", "an", "ai"):  # words of both languages
        assert dropped not in EN_WORDS | VI_WORDS


def test_the_lists_are_lower_case_nfc() -> None:
    for word in EN_WORDS | VI_WORDS:
        assert word == unicodedata.normalize("NFC", word).casefold()
    assert all(len(letter) == 1 for letter in VI_LETTERS)
    assert {unicodedata.normalize("NFC", c) for c in VI_LETTERS} == VI_LETTERS
    assert len(EN_WORDS) > 50 and len(VI_WORDS) > 50


def test_the_words_of_both_languages_are_dropped() -> None:
    analysis = lang(" ".join(["do no me an ai"] * 10 + [FILLER] * 5))

    assert (analysis.language_checked, analysis.language_hits) == (False, 0)


def test_the_english_ai_is_not_a_vietnamese_signal() -> None:
    # An English Shorts about "top AI tools": 40 "AI" and 12 English words.
    text = join(*["Top AI tools"] * 20, english(12))

    analysis = lang(text)

    assert "ai" not in VI_WORDS | EN_WORDS
    assert analysis.findings == ()
    assert (analysis.language_checked, analysis.language_hits) == (True, 12)
    # Alone, "AI" is no signal at all, in either channel language.
    assert lang("AI " * 40).language_hits == 0
    assert lang("AI " * 40, strategy("vi")).language_hits == 0


def test_vietnamese_text_without_ai_still_gets_enough_hits() -> None:
    analysis = lang(vietnamese(40), strategy("vi"))

    assert analysis.language_hits == 40 >= MIN_HITS
    assert analysis.findings == ()
    assert lang(vietnamese(40)).findings[0].code is CODE.LANGUAGE_MISMATCH


def test_every_section_is_read_for_the_language() -> None:
    # The hook, the call to action and every body count; nothing is left out.
    script = build(
        section(HOOK, vietnamese(5)),
        section(BODY, vietnamese(5)),
        section(CTA, vietnamese(5)),
        total=40,
    )

    analysis = run(script)

    assert analysis.language_hits == 15
    assert [f.code for f in analysis.findings] == [CODE.LANGUAGE_MISMATCH]


def test_only_the_first_20000_words_are_read() -> None:
    body = vietnamese(30_000)

    analysis = lang(body, strategy("vi"))

    # The 2 words of the hook are read first.
    assert analysis.language_hits == MAX_WORDS - 2 == 19_998
    assert analysis.words == 30_000 + 4


def test_only_the_first_800000_characters_are_read() -> None:
    body = "." * 900_000 + " " + vietnamese(20)

    analysis = lang(body, strategy("vi"))

    assert (analysis.language_checked, analysis.language_hits) == (False, 0)


# Banned phrases


def banned_run(*texts: str, banned=("get rich quick", "guaranteed returns")):
    sections = [section(HOOK), *(section(BODY, t) for t in texts), section(CTA)]
    return run(build(*sections), profile=strategy(banned=banned))


def test_a_banned_phrase_is_a_fail_with_its_section_and_count() -> None:
    analysis = banned_run("Clean text.", "Try to get rich quick today.")

    assert analysis.findings == (Finding(CODE.BANNED_PHRASE, FAIL, 2, matched=1),)


def test_the_finding_never_holds_the_phrase() -> None:
    analysis = banned_run("Try to get rich quick with guaranteed returns.")

    [finding] = analysis.findings
    assert finding.matched == 2
    assert "rich" not in repr(finding) and "guaranteed" not in repr(finding)
    assert "rich" not in repr(analysis)


def test_each_section_with_a_banned_phrase_is_one_finding() -> None:
    analysis = banned_run("get rich quick", "fine", "Get Rich Quick")

    assert found(analysis) == [("banned_phrase", 1), ("banned_phrase", 3)]


def test_a_banned_phrase_is_found_as_whole_words_ignoring_case() -> None:
    assert banned_run("GET   RICH quick!").findings != ()
    assert banned_run("get rich quicker").findings == ()
    assert banned_run("forget rich quick").findings == ()


def test_the_hook_and_the_cta_are_checked() -> None:
    script = build(
        section(HOOK, "Get rich quick."),
        section(BODY),
        section(CTA, "Guaranteed returns!"),
    )

    analysis = run(
        script, profile=strategy(banned=("get rich quick", "guaranteed returns"))
    )

    assert found(analysis) == [("banned_phrase", 0), ("banned_phrase", 2)]


def test_a_chapter_title_is_checked() -> None:
    sections = [section(HOOK), section(INTRO)]
    sections.append(ScriptSection(CHAPTER, "Fine text.", "Get rich quick"))
    sections += [section(CHAPTER)] * 2 + [section(OUTRO), section(CTA)]

    analysis = run(
        build(*sections, total=600),
        content_type=LONGFORM,
        profile=strategy(banned=("get rich quick",)),
    )

    assert found(analysis) == [("banned_phrase", 2)]


def titled(title: str, text: str, banned=("act now",)) -> ScriptAnalysis:
    sections = [section(HOOK), section(INTRO)]
    sections.append(ScriptSection(CHAPTER, text, title))
    sections += [section(CHAPTER)] * 2 + [section(OUTRO), section(CTA)]
    return run(
        build(*sections, total=600),
        content_type=LONGFORM,
        profile=strategy(banned=banned),
    )


def test_a_phrase_does_not_run_from_the_title_into_the_text() -> None:
    # "act now" is split by the end of the title: neither part holds it.
    analysis = titled("Why you should act", "Now we look at the numbers.")

    assert analysis.findings == ()


def test_a_phrase_in_the_title_alone_or_the_text_alone_is_a_finding() -> None:
    in_title = titled("Act now", "Plain words here.")
    in_text = titled("A title", "You should act now, really.")

    assert found(in_title) == [("banned_phrase", 2)]
    assert found(in_text) == [("banned_phrase", 2)]


def test_the_phrases_of_the_title_and_the_text_are_counted_once_each() -> None:
    banned = ("act now", "get rich quick")
    both = titled("Act now", "Get rich quick, act now.", banned)
    same = titled("Act now", "Then act now.", banned)

    assert both.findings == (Finding(CODE.BANNED_PHRASE, FAIL, 2, matched=2),)
    assert same.findings == (Finding(CODE.BANNED_PHRASE, FAIL, 2, matched=1),)


def test_no_banned_phrases_means_no_finding() -> None:
    assert banned_run("get rich quick", banned=()).findings == ()
    brandless = strategy(brand=None)
    assert run(shorts("get rich quick"), profile=brandless).findings == ()


# Hook length


def hook_run(hook: str, content_type=SHORTS):
    script = (
        shorts(hook=hook)
        if content_type is SHORTS
        else build(
            section(HOOK, hook),
            section(INTRO),
            *[section(CHAPTER)] * 3,
            section(OUTRO),
            section(CTA),
            total=600,
        )
    )
    return run(script, content_type=content_type)


def test_a_shorts_hook_within_the_limits_passes() -> None:
    assert hook_run(" ".join(["word"] * 15)).findings == ()
    assert hook_run("One. Two.").findings == ()


def test_a_long_shorts_hook_is_a_warn() -> None:
    analysis = hook_run(" ".join(["word"] * 16))

    assert analysis.findings == (
        Finding(CODE.HOOK_TOO_LONG, WARN, 0, actual=16, maximum=15),
    )


def test_a_hook_of_too_many_sentences_is_a_warn() -> None:
    analysis = hook_run("One. Two. Three.")

    assert analysis.findings == (
        Finding(CODE.HOOK_TOO_LONG, WARN, 0, actual=3, maximum=2),
    )


def test_both_limits_give_two_findings() -> None:
    analysis = hook_run(". ".join(["word"] * 16) + ".")

    assert [(f.actual, f.maximum) for f in analysis.findings] == [(16, 15), (16, 2)]
    assert {f.code for f in analysis.findings} == {CODE.HOOK_TOO_LONG}


def test_the_longform_hook_limits_are_looser() -> None:
    assert hook_run(" ".join(["word"] * 60), LONGFORM).findings == ()
    assert hook_run("One. Two. Three.", LONGFORM).findings == ()
    assert hook_run(" ".join(["word"] * 61), LONGFORM).findings[0].maximum == 60
    assert hook_run("One. Two. Three. Four.", LONGFORM).findings[0].maximum == 3


def test_the_limits_are_those_of_the_hook_rules() -> None:
    assert HOOK_LIMITS[SHORTS].max_words == 15
    assert HOOK_LIMITS[LONGFORM].max_sentences == 3


def test_the_hook_found_is_the_first_hook_section() -> None:
    script = build(
        section(BODY),
        section(HOOK, " ".join(["word"] * 16)),
        section(CTA),
    )

    analysis = run(script)

    assert found(analysis) == [("wrong_order", 1), ("hook_too_long", 1)]


def test_a_script_without_a_hook_has_no_hook_finding() -> None:
    assert codes(run(build(section(BODY), section(CTA)))) == ["missing_section"]


# The order of the findings, the status and the bounds


def test_the_findings_come_in_the_order_of_the_rules() -> None:
    hook = join("Get rich quick", english(10), vietnamese(40), *["x"] * 20)
    script = build(section(CTA), section(HOOK, hook), section(BODY), total=5)

    analysis = run(script, profile=strategy(banned=("get rich quick",)))

    assert codes(analysis) == [
        "wrong_order",
        "too_short",
        "language_mismatch",
        "banned_phrase",
        "hook_too_long",
    ]


def test_the_output_is_deterministic() -> None:
    script = shorts(vietnamese(30), total=70)

    assert run(script) == run(script)


def test_the_most_findings_stay_within_the_bound() -> None:
    sections = [section(BODY, "get rich quick")] * 100
    script = build(*sections, total=600)

    analysis = run(
        script, content_type=LONGFORM, profile=strategy(banned=("get rich quick",))
    )

    # 4 missing, 100 unexpected, the chapter count and 100 banned phrases.
    assert len(analysis.findings) == 205 <= MAX_FINDINGS == 210
    assert rules.MAX_FINDINGS == 2 * 100 + 10


def test_validation_needs_a_language_and_a_format() -> None:
    for missing in ("languages", "format"):
        with pytest.raises(ValueError, match="language and a format"):
            validate_script(shorts(), SHORTS, strategy(**{missing: None}))


def test_the_strategy_version_is_the_current_one() -> None:
    profile = dataclasses.replace(strategy(), version=7)

    assert run(shorts(), profile=profile).strategy_version == 7


# Findings


@pytest.mark.parametrize(
    ("code", "status", "ok"),
    [
        (CODE.MISSING_SECTION, FAIL, True),
        (CODE.MISSING_SECTION, WARN, False),
        (CODE.CHAPTER_KIND, FAIL, True),
        (CODE.CHAPTER_KIND, WARN, False),
        (CODE.TOO_SHORT, FAIL, True),
        (CODE.TOO_LONG, WARN, False),
        (CODE.LANGUAGE_MISMATCH, FAIL, True),
        (CODE.LANGUAGE_MISMATCH, WARN, True),
        (CODE.BANNED_PHRASE, FAIL, True),
        (CODE.HOOK_TOO_LONG, WARN, True),
        (CODE.HOOK_TOO_LONG, FAIL, False),
        (CODE.WRONG_ORDER, PASS, False),
    ],
)
def test_a_code_has_its_own_statuses(code, status, ok) -> None:
    if ok:
        assert Finding(code, status).status is status
        return
    with pytest.raises(ValueError, match="is not"):
        Finding(code, status)


@pytest.mark.parametrize(
    "change",
    [
        dict(code="too_short"),
        dict(status="fail"),
        dict(section_index=-1),
        dict(section_index=True),
        dict(actual=-1),
        dict(minimum=2.5),
        dict(maximum=-1),
        dict(minimum=5, maximum=4),
        dict(score=1.5),
        dict(score=-0.1),
        dict(score=True),
        dict(matched=-1),
    ],
)
def test_a_finding_checks_its_values(change) -> None:
    values = dict(code=CODE.TOO_SHORT, status=FAIL) | change

    with pytest.raises((ValueError, TypeError)):
        Finding(**values)


def test_a_finding_is_frozen_and_holds_numbers_only() -> None:
    finding = Finding(CODE.TOO_SHORT, FAIL, actual=1, minimum=2, maximum=3)

    with pytest.raises(dataclasses.FrozenInstanceError):
        finding.actual = 5  # type: ignore[misc]
    assert {f.name for f in dataclasses.fields(Finding)} == {
        "code",
        "status",
        "section_index",
        "actual",
        "minimum",
        "maximum",
        "score",
        "matched",
    }


# The run


def analysis_of(*findings: Finding, **change) -> ScriptAnalysis:
    values = dict(
        words=50,
        seconds=40,
        language_checked=False,
        language_hits=0,
        strategy_version=1,
        findings=findings,
    )
    return ScriptAnalysis(**(values | change))


def make_run(*findings: Finding, method=SCRIPT_VALIDATION_METHOD, **change):
    script = shorts(total=change.pop("script_seconds", 40))
    analysis = analysis_of(*findings, **change)
    return ScriptValidation.create(
        script, analysis, method=method, requested_by=USER, clock=lambda: T0
    )


def test_a_run_without_a_finding_passes() -> None:
    run_ = make_run()

    assert run_.status is PASS
    assert (run_.findings_count, run_.warn_count, run_.fail_count) == (0, 0, 0)
    assert run_.method == "script-rules-v1" == SCRIPT_VALIDATION_METHOD


def test_the_status_is_the_worst_finding() -> None:
    warn = Finding(CODE.HOOK_TOO_LONG, WARN, 0, actual=16, maximum=15)
    fail = Finding(CODE.WRONG_ORDER, FAIL, 1)

    assert make_run(warn).status is WARN
    assert make_run(fail).status is FAIL
    assert make_run(warn, fail).status is FAIL
    assert make_run(warn, fail).warn_count == make_run(warn, fail).fail_count == 1


def test_a_run_not_checked_for_language_is_a_pass_with_the_flag_off() -> None:
    run_ = make_run()

    assert run_.status is PASS and run_.language_checked is False


def test_a_run_records_both_strategy_versions() -> None:
    script = shorts(strategy_version=3)
    run_ = ScriptValidation.create(
        script,
        analysis_of(strategy_version=5, seconds=40),
        requested_by=USER,
        clock=lambda: T0,
    )

    assert (run_.strategy_version, run_.script_strategy_version) == (5, 3)
    assert make_run().script_strategy_version is None


def test_the_counts_are_scalars() -> None:
    counts = make_run(Finding(CODE.WRONG_ORDER, FAIL, 1)).counts()

    assert counts == {
        "words": 50,
        "seconds": 40,
        "language_checked": False,
        "findings": 1,
        "warn": 0,
        "fail": 1,
        "status": "fail",
    }


@pytest.mark.parametrize(
    "change",
    [
        dict(id=""),
        dict(script_id=""),
        dict(content_item_id=""),
        dict(method=" "),
        dict(method="x" * 101),
        dict(words=-1),
        dict(seconds=-1),
        dict(language_checked=1),
        dict(language_hits=20_001),
        dict(strategy_version=0),
        dict(script_strategy_version=0),
        dict(findings_count=1),
        dict(warn_count=1),
        dict(fail_count=1),
        dict(findings=[]),
        dict(findings=(1,), findings_count=1, fail_count=1),
        dict(requested_by="owner"),
        dict(created_at=datetime(2026, 10, 4)),
    ],
)
def test_a_run_checks_its_values(change) -> None:
    run_ = make_run()

    with pytest.raises((ValueError, TypeError)):
        dataclasses.replace(run_, **change)


def test_too_many_findings_are_refused() -> None:
    finding = Finding(CODE.UNEXPECTED_SECTION, FAIL, 1)
    run_ = make_run()

    with pytest.raises(ValueError, match="findings_count"):
        dataclasses.replace(
            run_,
            findings=(finding,) * (MAX_FINDINGS + 1),
            findings_count=MAX_FINDINGS + 1,
            fail_count=MAX_FINDINGS + 1,
        )


@pytest.mark.parametrize(
    ("finding", "change"),
    [
        (Finding(CODE.UNEXPECTED_SECTION, FAIL), {}),
        (Finding(CODE.WRONG_ORDER, FAIL), {}),
        (Finding(CODE.CHAPTER_COUNT, FAIL, actual=5, minimum=3, maximum=12), {}),
        (Finding(CODE.CHAPTER_COUNT, FAIL), {}),
        (Finding(CODE.CHAPTER_KIND, FAIL, 2), {}),
        (Finding(CODE.CHAPTER_KIND, FAIL, actual=3), {}),
        (Finding(CODE.CHAPTER_KIND, FAIL, 2, actual=2), {}),
        (Finding(CODE.CHAPTER_KIND, FAIL, 2, actual=13), {}),
        (Finding(CODE.TOO_SHORT, FAIL), {}),
        (Finding(CODE.TOO_SHORT, FAIL, actual=50, minimum=15, maximum=60), {}),
        (Finding(CODE.TOO_LONG, FAIL, actual=40, minimum=15, maximum=60), {}),
        (Finding(CODE.TOO_SHORT, FAIL, actual=39, minimum=40, maximum=60), {}),
        (Finding(CODE.BANNED_PHRASE, FAIL, 1), {}),
        (Finding(CODE.BANNED_PHRASE, FAIL, 1, matched=0), {}),
        (Finding(CODE.BANNED_PHRASE, FAIL, matched=1), {}),
        (Finding(CODE.HOOK_TOO_LONG, WARN, 0, actual=10, maximum=15), {}),
        (Finding(CODE.HOOK_TOO_LONG, WARN, 0), {}),
        (Finding(CODE.LANGUAGE_MISMATCH, FAIL, actual=40, score=1.0, matched=40), {}),
        (
            Finding(CODE.LANGUAGE_MISMATCH, FAIL, actual=40, score=0.5, matched=20),
            dict(language_checked=True, language_hits=40),
        ),
        (
            Finding(CODE.LANGUAGE_MISMATCH, WARN, actual=40, score=0.9, matched=36),
            dict(language_checked=True, language_hits=40),
        ),
        (
            Finding(CODE.LANGUAGE_MISMATCH, WARN, actual=40, score=0.5, matched=20),
            dict(language_checked=True, language_hits=40),
        ),
        (
            Finding(CODE.LANGUAGE_MISMATCH, FAIL, actual=39, score=1.0, matched=39),
            dict(language_checked=True, language_hits=40),
        ),
        (
            Finding(CODE.LANGUAGE_MISMATCH, FAIL, actual=40, score=0.8333, matched=1),
            dict(language_checked=True, language_hits=40),
        ),
    ],
)
def test_the_v1_rules_check_the_values_of_a_finding(finding, change) -> None:
    with pytest.raises(ValueError):
        make_run(finding, **change)


def test_the_v1_rules_accept_the_findings_the_rules_make() -> None:
    findings = (
        Finding(CODE.MISSING_SECTION, FAIL),
        Finding(CODE.UNEXPECTED_SECTION, FAIL, 2),
        Finding(CODE.CHAPTER_COUNT, FAIL, actual=2, minimum=3, maximum=12),
        Finding(CODE.WRONG_ORDER, FAIL, 1),
        Finding(CODE.TOO_LONG, FAIL, actual=40, minimum=10, maximum=30),
        Finding(CODE.LANGUAGE_MISMATCH, WARN, actual=40, score=0.7, matched=28),
        Finding(CODE.BANNED_PHRASE, FAIL, 1, matched=2),
        Finding(CODE.HOOK_TOO_LONG, WARN, 0, actual=16, maximum=15),
    )

    run_ = make_run(*findings, language_checked=True, language_hits=40)

    assert run_.findings == findings


def test_a_run_accepts_a_chapter_kind_finding() -> None:
    finding = Finding(CODE.CHAPTER_KIND, FAIL, 2, actual=3)

    assert make_run(finding).findings == (finding,)


def test_a_chapter_kind_finding_is_alone_and_has_no_chapter_count() -> None:
    kind = Finding(CODE.CHAPTER_KIND, FAIL, 2, actual=3)
    count = Finding(CODE.CHAPTER_COUNT, FAIL, actual=2, minimum=3, maximum=12)
    with pytest.raises(ValueError, match="chapter_kind"):
        make_run(kind, kind)
    with pytest.raises(ValueError, match="chapter_kind"):
        make_run(kind, count)


def test_a_language_run_needs_ten_hits() -> None:
    with pytest.raises(ValueError, match="10 signal hits"):
        make_run(language_checked=True, language_hits=9)
    with pytest.raises(ValueError, match="10 signal hits"):
        make_run(language_checked=False, language_hits=10)


def test_a_language_finding_needs_a_checked_language() -> None:
    finding = Finding(CODE.LANGUAGE_MISMATCH, FAIL, actual=0, score=1.0, matched=0)

    with pytest.raises(ValueError, match="language"):
        make_run(finding)


def test_at_most_one_length_and_one_language_finding() -> None:
    length = Finding(CODE.TOO_LONG, FAIL, actual=40, minimum=1, maximum=2)
    with pytest.raises(ValueError, match="one length"):
        make_run(length, dataclasses.replace(length))
    language = Finding(CODE.LANGUAGE_MISMATCH, FAIL, actual=40, score=1.0, matched=40)
    with pytest.raises(ValueError, match="language"):
        make_run(language, language, language_checked=True, language_hits=40)


def test_another_method_is_not_held_to_the_v1_value_rules() -> None:
    # A later rule version may differ: its values are not judged by v1.
    odd = Finding(CODE.TOO_SHORT, FAIL)
    score = Finding(CODE.LANGUAGE_MISMATCH, WARN, score=0.12345)

    run_ = make_run(odd, score, method="script-rules-v2", language_hits=3)

    assert run_.method == "script-rules-v2"
    assert run_.findings_count == 2
    with pytest.raises(ValueError, match="every finding"):  # the counts hold anyway
        dataclasses.replace(run_, findings_count=1)
    with pytest.raises(ValueError, match="at most 3 decimals"):
        make_run(score, language_checked=True, language_hits=40)


def test_the_score_has_at_most_three_decimals_for_v1() -> None:
    finding = Finding(CODE.LANGUAGE_MISMATCH, FAIL, actual=40, score=0.8333, matched=34)

    with pytest.raises(ValueError, match="3 decimals"):
        make_run(finding, language_checked=True, language_hits=40)


def test_a_run_is_frozen() -> None:
    with pytest.raises(dataclasses.FrozenInstanceError):
        make_run().words = 1  # type: ignore[misc]
