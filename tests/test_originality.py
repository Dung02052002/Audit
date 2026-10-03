"""F-071 Originality Check rules (Prompt Pack v8, prompt #071).

Rules the user approved on 2026-10-03, tested here on hand-made scripts (the
checker, storage and the prior content are in ``test_originality_checker.py``):

- reuse by the containment of word 5-gram shingles: 60% or more is a FAIL
  (near_duplicate), else 25% or more with 8 or more shared shingles a WARN
  (high_overlap); 3 or more distinct copied sentences of 8 or more words a WARN
  (copied_sentences); a script of fewer than 20 words has no reuse finding;
- structure, WARN only: repeated_hook, repeated_sentences, repeated_openers;
- text is NFC and casefold, stop words are kept, CTA sections are left out of
  every rule, and at most 20,000 words of a script are read;
- a result keeps ids, scores and counts only, and its status is the worst
  finding.
"""

import dataclasses
import time
import unicodedata
from datetime import UTC, datetime, timedelta, timezone

import pytest

from ai_youtube_agent.content.originality import (
    MAX_FINDINGS,
    MAX_PRIORS,
    MAX_WORDS,
    MIN_WORDS,
    ORIGINALITY_METHOD,
    Finding,
    OriginalityAnalysis,
    OriginalityCheck,
    OriginalityCode,
    OriginalityStatus,
    check_originality,
)
from ai_youtube_agent.content.script import Script, ScriptSection, SectionKind
from ai_youtube_agent.core.audit import Actor, ActorKind

USER = Actor(ActorKind.USER, "owner")
T0 = datetime(2026, 10, 3, 12, 0, tzinfo=UTC)
PASS, WARN, FAIL = (
    OriginalityStatus.PASS,
    OriginalityStatus.WARN,
    OriginalityStatus.FAIL,
)
CODE = OriginalityCode
HOOK, BODY, CTA = SectionKind.HOOK, SectionKind.BODY, SectionKind.CTA


def tokens(prefix: str, count: int, start: int = 0) -> list[str]:
    """Words that never repeat: ``prefix`` and a number."""
    return [f"{prefix}{i}" for i in range(start, start + count)]


def text(*words: str) -> str:
    return " ".join(words)


def sentence(prefix: str, number: int, length: int = 8) -> str:
    """A sentence of ``length`` words found nowhere else."""
    return text(*tokens(f"{prefix}{number}x", length)) + "."


def script(*sections: ScriptSection | str, item: str = "item") -> Script:
    return Script.create(
        item,
        sections=tuple(
            s if isinstance(s, ScriptSection) else ScriptSection(BODY, s)
            for s in sections
        ),
        clock=lambda: T0,
    )


def run(target: Script, *priors: Script) -> OriginalityAnalysis:
    """The findings; a prior of the item of the script is moved to its own item."""
    return check_originality(
        target,
        [
            dataclasses.replace(prior, content_item_id=f"prior-{number}")
            if prior.content_item_id == target.content_item_id
            else prior
            for number, prior in enumerate(priors)
        ],
    )


def codes(analysis: OriginalityAnalysis) -> list[str]:
    return [finding.code.value for finding in analysis.findings]


def only(analysis: OriginalityAnalysis, code: OriginalityCode) -> Finding:
    [found] = [f for f in analysis.findings if f.code is code]
    return found


def overlapping(base: list[str], shared_words: int, filler: str = "zz") -> str:
    """A text sharing its first ``shared_words`` words with ``base``."""
    return text(*base[:shared_words], *tokens(filler, 60))


# Reuse: containment of shingles


def test_a_script_without_priors_has_no_finding() -> None:
    analysis = run(script(text(*tokens("a", 40))))

    assert analysis == OriginalityAnalysis(40, 0, ())
    check = OriginalityCheck.create(
        script("x"), analysis, requested_by=USER, clock=lambda: T0
    )
    assert (check.status, check.findings_count) == (PASS, 0)


def test_unrelated_priors_have_no_finding() -> None:
    analysis = run(script(text(*tokens("a", 40))), script(text(*tokens("b", 40))))

    assert (analysis.findings, analysis.priors_count) == ((), 1)


def test_an_identical_prior_is_a_near_duplicate() -> None:
    base = tokens("a", 40)
    prior = script(text(*base), item="other")

    analysis = run(script(text(*base)), prior)

    assert analysis.findings == (
        Finding(
            CODE.NEAR_DUPLICATE,
            FAIL,
            prior.id,
            "other",
            score=1.0,
            matched=36,
        ),
    )


# 24 words are 20 shingles, so every shared shingle is 5% of the script.
TARGET = tokens("t", 24)


@pytest.mark.parametrize(
    ("shared_words", "code", "shared", "score"),
    [
        (16, CODE.NEAR_DUPLICATE, 12, 0.6),  # exactly 60%
        (15, CODE.HIGH_OVERLAP, 11, 0.55),
        (12, CODE.HIGH_OVERLAP, 8, 0.4),  # 8 shared shingles
        (11, None, 7, None),  # 35%, but only 7 shared shingles
        (9, None, 5, None),  # exactly 25%, but only 5 shared
        (4, None, 0, None),  # a shingle needs 5 words
    ],
)
def test_the_thresholds_of_the_containment(shared_words, code, shared, score) -> None:
    prior = script(overlapping(TARGET, shared_words), item="other")

    analysis = run(script(text(*TARGET)), prior)

    if code is None:
        assert analysis.findings == ()
        return
    assert analysis.findings == (
        Finding(
            code,
            FAIL if code is CODE.NEAR_DUPLICATE else WARN,
            prior.id,
            "other",
            score=score,
            matched=shared,
        ),
    )


def test_exactly_a_quarter_with_eight_shared_shingles_is_an_overlap() -> None:
    # 32 shingles, 8 of them shared: 25% and 8 shared.
    base = tokens("q", 36)
    prior = script(overlapping(base, 12), item="other")

    finding = only(run(script(text(*base)), prior), CODE.HIGH_OVERLAP)

    assert (finding.score, finding.matched) == (0.25, 8)


def test_the_score_is_rounded_to_three_decimals() -> None:
    base = tokens("r", 25)  # 21 shingles
    prior = script(overlapping(base, 17), item="other")  # 13 shared

    finding = only(run(script(text(*base)), prior), CODE.NEAR_DUPLICATE)

    assert (finding.score, finding.matched) == (0.619, 13)


def test_a_prior_is_only_compared_with_the_shingles_of_the_script() -> None:
    # The prior holds the whole script among much more text: the containment
    # is of the script in the prior, not the other way round.
    base = tokens("c", 30)
    prior = script(text(*tokens("pre", 200), *base, *tokens("post", 200)), item="other")

    finding = only(run(script(text(*base)), prior), CODE.NEAR_DUPLICATE)

    assert finding.score == 1.0


def test_a_shingle_does_not_cross_two_sections() -> None:
    base = tokens("s", 24)
    split = script(text(*base[:12]), text(*base[12:]))
    joined = script(text(*base), item="other")

    # 8 + 8 shingles inside each half; the 4 crossing the boundary are not.
    inside = only(run(split, joined), CODE.NEAR_DUPLICATE)
    assert (inside.matched, inside.score) == (16, 1.0)
    across = only(run(joined, split), CODE.NEAR_DUPLICATE)
    assert (across.matched, across.score) == (16, 0.8)  # of 20 shingles


def test_findings_follow_the_order_of_the_priors() -> None:
    base = tokens("o", 40)
    first = script(text(*base), item="first")
    second = script(text(*base), item="second")

    analysis = run(script(text(*base)), first, second)

    assert [
        (f.prior_script_id, f.prior_content_item_id) for f in analysis.findings
    ] == [
        (first.id, "first"),
        (second.id, "second"),
    ]
    assert analysis.priors_count == 2


# Reuse: copied sentences


def with_copied(count: int) -> tuple[Script, Script]:
    copied = [sentence("copy", i) for i in range(count)]
    filler_a = text(*tokens("fa", 200)) + "."
    filler_b = text(*tokens("fb", 200)) + "."
    target = script(filler_a, " ".join(copied))
    prior = script(" ".join(copied), filler_b, item="other")
    return target, prior


@pytest.mark.parametrize(("count", "flagged"), [(2, False), (3, True), (5, True)])
def test_three_copied_sentences_are_a_warning(count, flagged) -> None:
    target, prior = with_copied(count)

    analysis = run(target, prior)

    assert ("copied_sentences" in codes(analysis)) is flagged
    if flagged:
        assert only(analysis, CODE.COPIED_SENTENCES) == Finding(
            CODE.COPIED_SENTENCES, WARN, prior.id, "other", matched=count
        )
    assert "near_duplicate" not in codes(analysis)
    assert "high_overlap" not in codes(analysis)


def test_copied_sentences_count_distinct_sentences_of_eight_words() -> None:
    repeated = sentence("rep", 0)
    short = [sentence("short", i, length=7) for i in range(5)]
    filler = text(*tokens("f", 200)) + "."
    target = script(filler, " ".join([repeated] * 4 + short))
    prior = script(" ".join([repeated, *short]), text(*tokens("g", 200)), item="o")

    # One distinct long sentence, however often it is repeated, and sentences of
    # 7 words never count.
    assert "copied_sentences" not in codes(run(target, prior))


def test_sentences_are_compared_after_normalising_the_case_and_marks() -> None:
    copied = [sentence("Copy", i) for i in range(3)]
    target = script(text(*tokens("fa", 200)) + ".", " ".join(copied))
    prior = script(
        " ".join(copied).upper().replace(".", " !"),
        text(*tokens("fb", 200)) + ".",
        item="other",
    )

    assert "copied_sentences" in codes(run(target, prior))


def test_copied_sentences_come_with_a_near_duplicate() -> None:
    copied = " ".join(sentence("both", i) for i in range(4))

    analysis = run(script(copied), script(copied, item="other"))

    assert codes(analysis) == ["near_duplicate", "copied_sentences"]


# Reuse: what is left out or read differently


def test_a_script_of_fewer_than_twenty_words_has_no_reuse_finding() -> None:
    short = text(*tokens("w", MIN_WORDS - 1))
    enough = text(*tokens("w", MIN_WORDS))

    below = run(script(short), script(short, item="other"))
    at_least = run(script(enough), script(enough, item="other"))

    assert (below.words, below.findings) == (MIN_WORDS - 1, ())
    assert at_least.words == MIN_WORDS
    assert codes(at_least) == ["near_duplicate"]


def test_cta_sections_are_left_out_of_every_rule() -> None:
    cta = text(*tokens("cta", 40)) + ". " + " ".join([sentence("cta", 9)] * 3)
    body = script(text(*tokens("a", 30)), ScriptSection(CTA, cta))
    other = script(text(*tokens("b", 30)), ScriptSection(CTA, cta), item="other")

    analysis = run(body, other)

    assert analysis.words == 30  # the CTA words are not counted
    assert analysis.findings == ()  # same CTA, nothing else in common


def test_a_cta_does_not_repeat_sentences_or_openers() -> None:
    cta = " ".join(["Subscribe to the channel now."] * 6)

    analysis = run(script(text(*tokens("a", 30)), ScriptSection(CTA, cta)))

    assert analysis.findings == ()


def test_a_script_of_only_a_cta_reads_no_word() -> None:
    analysis = run(script(ScriptSection(CTA, "Subscribe to the channel now.")))

    assert (analysis.words, analysis.findings) == (0, ())


def test_case_is_ignored() -> None:
    base = tokens("Case", 40)

    analysis = run(script(text(*base).upper()), script(text(*base).lower(), item="o"))

    assert codes(analysis) == ["near_duplicate"]


def test_punctuation_and_spacing_are_ignored() -> None:
    base = tokens("p", 40)
    spaced = ", ".join(base).replace(",", " ,\n ")

    analysis = run(script(text(*base)), script(spaced, item="other"))

    assert codes(analysis) == ["near_duplicate"]


def test_decomposed_vietnamese_equals_composed_vietnamese() -> None:
    composed = text(*tokens("tiền", 30))
    decomposed = unicodedata.normalize("NFD", composed)
    assert decomposed != composed

    analysis = run(script(decomposed), script(composed, item="other"))

    assert analysis.words == 30  # a combining mark does not split a word
    assert codes(analysis) == ["near_duplicate"]


def test_stop_words_are_kept() -> None:
    stops = ["the", "of", "and", "to", "in", "is", "that", "it", "for", "as"]
    # 30 words of stop words only, in an order that never repeats a 5-gram.
    order = [stops[(i * i + i // 3) % len(stops)] for i in range(30)]

    analysis = run(script(text(*order)), script(text(*order), item="other"))

    assert analysis.words == 30
    assert codes(analysis) == ["near_duplicate"]


@pytest.mark.parametrize(
    ("shared", "code", "score"),
    [
        (1500, CODE.NEAR_DUPLICATE, 0.6),  # exactly 60% of 2,500 shingles
        (1499, CODE.HIGH_OVERLAP, 0.599),  # 59.96% is never shown as 0.6
        (625, CODE.HIGH_OVERLAP, 0.25),  # exactly 25%
        (624, None, None),  # 24.96%
    ],
)
def test_the_score_is_rounded_down_at_a_boundary(shared, code, score) -> None:
    base = tokens("w", 2_504)  # 2,500 shingles
    prior = script(overlapping(base, shared + 4), item="other")

    analysis = run(script(text(*base)), prior)

    if code is None:
        assert analysis.findings == ()
        return
    finding = only(analysis, code)
    assert (finding.score, finding.matched) == (score, shared)
    assert analysis.findings == (finding,)


# The word cap


def test_at_most_twenty_thousand_words_are_read() -> None:
    long = script(text(*tokens("L", MAX_WORDS + 500)))

    assert run(long).words == MAX_WORDS == 20_000


def test_the_cap_runs_over_the_sections_in_order() -> None:
    first = text(*tokens("a", 15_000))
    second = text(*tokens("b", 10_000))
    # The prior holds only the words of the second section beyond the cap.
    beyond = text(*tokens("b", 5_000, start=5_000))
    target = script(first, second)

    assert run(target).words == MAX_WORDS
    assert run(target, script(beyond, item="other")).findings == ()
    first_only = only(run(target, script(first, item="other")), CODE.NEAR_DUPLICATE)
    assert first_only.score == 0.75  # 14,996 of the 19,992 shingles read


def test_words_after_the_cap_are_not_compared() -> None:
    head = tokens("h", MAX_WORDS)
    tail = tokens("t", 300)
    target = script(text(*head, *tail))

    only_tail = run(target, script(text(*tail), item="other"))
    only_head = run(target, script(text(*head[:100]), item="other"))

    assert only_tail.findings == ()
    assert only_head.findings == ()  # 100 of 20,000 words is under 25%
    whole = run(target, script(text(*head), item="other"))
    assert codes(whole) == ["near_duplicate"]
    assert only(whole, CODE.NEAR_DUPLICATE).score == 1.0


def test_one_huge_section_is_read_quickly_up_to_the_cap() -> None:
    huge = script(("go. " * 5_000_000).strip())  # 20 million characters

    started = time.perf_counter()
    analysis = run(huge)
    elapsed = time.perf_counter() - started

    assert analysis.words == MAX_WORDS
    assert elapsed < 5


def test_a_script_of_spaces_and_marks_is_read_only_up_to_the_budget() -> None:
    marks = script("... " * 5_000_000 + "last words stay out.")

    assert run(marks).words == 0


def test_a_script_under_the_cap_is_read_whole() -> None:
    body = text(*tokens("u", 5_000))

    assert run(script(body)).words == 5_000


# Structure: the hook


def hook_priors(hook: str, count: int, other: str | None = None) -> list[Script]:
    return [
        script(
            ScriptSection(HOOK, f"{hook} {tokens(f'p{i}', 1)[0]}."),
            text(*tokens(f"unique{i}", 5)),
            item=f"item{i}",
        )
        for i in range(count)
    ] + ([script(ScriptSection(HOOK, other), item="different")] if other else [])


def test_three_priors_with_the_same_hook_are_a_warning() -> None:
    target = script(ScriptSection(HOOK, "Did you know that fees rise?"), "Body one.")

    analysis = run(target, *hook_priors("Did you know that banks", 3))

    assert only(analysis, CODE.REPEATED_HOOK) == Finding(
        CODE.REPEATED_HOOK, WARN, matched=3
    )


def test_two_priors_with_the_same_hook_are_not() -> None:
    target = script(ScriptSection(HOOK, "Did you know that fees rise?"), "Body one.")

    analysis = run(target, *hook_priors("Did you know that banks", 2))

    assert "repeated_hook" not in codes(analysis)


def test_only_the_priors_with_the_same_hook_are_counted() -> None:
    target = script(ScriptSection(HOOK, "Did you know that fees rise?"))
    priors = hook_priors("Did you know that banks", 3, other="Why do fees rise?")

    finding = only(run(target, *priors), CODE.REPEATED_HOOK)

    assert (finding.matched, finding.prior_script_id) == (3, None)


def test_a_hook_is_its_first_four_words_ignoring_case_and_marks() -> None:
    target = script(ScriptSection(HOOK, "DID YOU, KNOW that? Nobody does."))

    analysis = run(target, *hook_priors("did you know that", 3))

    assert only(analysis, CODE.REPEATED_HOOK).matched == 3


def test_a_different_fourth_word_is_a_different_hook() -> None:
    target = script(ScriptSection(HOOK, "Did you know this fact?"))

    assert "repeated_hook" not in codes(
        run(target, *hook_priors("Did you know that", 3))
    )


def test_only_the_first_sentence_of_the_hook_counts() -> None:
    # The first sentence has 2 words; the first 4 words of the hook would match.
    target = script(ScriptSection(HOOK, "Did you. Know that fees rise?"))

    assert "repeated_hook" not in codes(
        run(target, *hook_priors("Did you know that", 3))
    )


def test_a_hook_of_fewer_than_four_words_is_not_compared() -> None:
    target = script(ScriptSection(HOOK, "Fees rise."))
    priors = [script(ScriptSection(HOOK, "Fees rise."), item=f"i{i}") for i in range(3)]

    assert "repeated_hook" not in codes(run(target, *priors))


def test_without_a_hook_the_first_section_is_the_hook() -> None:
    target = script("Did you know that fees rise? Yes.", "Other words here today.")

    analysis = run(target, *hook_priors("Did you know that", 3))

    assert only(analysis, CODE.REPEATED_HOOK).matched == 3


def test_the_hook_section_is_found_wherever_it_stands() -> None:
    target = script(
        "Welcome back to the channel.",
        ScriptSection(HOOK, "Did you know that fees rise?"),
    )

    analysis = run(target, *hook_priors("Did you know that", 3))

    assert only(analysis, CODE.REPEATED_HOOK).matched == 3


def test_a_cta_is_never_the_hook() -> None:
    cta = "Did you know that fees rise?"
    target = script(ScriptSection(CTA, cta), "Body words here today.")
    priors = [script(ScriptSection(CTA, cta), item=f"i{i}") for i in range(3)]

    assert "repeated_hook" not in codes(run(target, *priors))


# Structure: repeated sentences


def test_a_sentence_repeated_three_times_is_two_repeats() -> None:
    line = "Fees always add up quickly."
    target = script(
        f"{line} Other one here now. {line} Another fact comes next. {line}"
    )

    finding = only(run(target), CODE.REPEATED_SENTENCES)

    assert finding == Finding(CODE.REPEATED_SENTENCES, WARN, section_index=0, matched=2)


def test_a_sentence_repeated_once_is_not_enough() -> None:
    line = "Fees always add up quickly."

    assert "repeated_sentences" not in codes(
        run(script(f"{line} Next fact now. {line}"))
    )


def test_two_sentences_repeated_once_each_are_two_repeats() -> None:
    first, second = "Fees always add up quickly.", "Banks change their rules often."
    target = script(f"{first} {second} A new fact. {first} {second}")

    assert only(run(target), CODE.REPEATED_SENTENCES).matched == 2


def test_short_sentences_do_not_count_as_repeated() -> None:
    target = script(" ".join(["Fees rise fast now."] * 4))  # 4 words each

    assert "repeated_sentences" not in codes(run(target))


def test_the_section_of_the_first_repeat_is_kept() -> None:
    line = "Fees always add up quickly."
    target = script(f"{line} First part.", f"A middle part. {line}", f"{line}")

    finding = only(run(target), CODE.REPEATED_SENTENCES)

    assert (finding.section_index, finding.matched) == (1, 2)


def test_repeats_ignore_case_and_marks() -> None:
    target = script(
        "Fees always add up fast! FEES ALWAYS ADD UP FAST? fees, always add up fast."
    )

    assert only(run(target), CODE.REPEATED_SENTENCES).matched == 2


def test_repeats_do_not_use_the_priors() -> None:
    line = "Fees always add up quickly."
    prior = script(f"{line} {line} {line}", item="other")

    assert "repeated_sentences" not in codes(run(script(f"{line} Other."), prior))


# Structure: repeated openers


def openers(*starts: str) -> str:
    return " ".join(
        f"{start} {tokens('n', 1, i)[0]} rest." for i, start in enumerate(starts)
    )


def test_two_of_five_sentences_with_the_same_opener_is_forty_percent() -> None:
    target = script(
        openers("Fees rise", "Fees rise", "Banks lie", "Rates fall", "Money")
    )

    finding = only(run(target), CODE.REPEATED_OPENERS)

    assert finding == Finding(CODE.REPEATED_OPENERS, WARN, score=0.4, matched=2)


def test_under_forty_percent_is_not_repetitive() -> None:
    target = script(
        openers(
            "Fees rise",
            "Fees rise",
            "Banks lie",
            "Rates fall",
            "Money talks",
            "Loans",
            "Debt",
        )
    )

    assert "repeated_openers" not in codes(run(target))  # 2 of 7


def test_the_score_of_the_openers_is_rounded() -> None:
    target = script(
        openers(
            "Fees rise",
            "Fees rise",
            "Fees rise",
            "Banks lie",
            "Rates fall",
            "Loans",
            "Debt",
        )
    )

    # 3 of 7 is 0.4285..., rounded down
    assert only(run(target), CODE.REPEATED_OPENERS).score == 0.428


def many_openers(same: int, total: int) -> str:
    """``total`` sentences, ``same`` of them with the opener "fees rise"."""
    return " ".join(
        "Fees rise now." if i < same else f"Opener{i} word{i} now."
        for i in range(total)
    )


def test_the_share_of_the_openers_is_exact_at_forty_percent() -> None:
    assert "repeated_openers" not in codes(run(script(many_openers(399, 1_000))))

    finding = only(run(script(many_openers(400, 1_000))), CODE.REPEATED_OPENERS)

    assert (finding.score, finding.matched) == (0.4, 400)


def test_fewer_than_four_sentences_have_no_repeated_openers() -> None:
    assert "repeated_openers" not in codes(
        run(script(openers("Fees rise", "Fees rise", "Fees rise")))
    )
    assert "repeated_openers" in codes(
        run(script(openers("Fees rise", "Fees rise", "Fees rise", "Fees rise")))
    )


def test_an_opener_is_two_words_ignoring_case() -> None:
    target = script(openers("Fees rise", "FEES RISE", "fees rise", "Banks lie"))

    assert only(run(target), CODE.REPEATED_OPENERS).matched == 3


def test_one_word_sentences_are_not_counted_for_openers() -> None:
    target = script("Yes. No. Maybe. Fine. Sure. Fees rise today. Fees rise again.")

    assert "repeated_openers" not in codes(run(target))  # 2 sentences of 2 words


def test_the_structure_rules_work_without_priors() -> None:
    line = "Fees always add up quickly."
    target = script(f"{line} {line} {line} {line}")

    assert codes(run(target)) == ["repeated_sentences", "repeated_openers"]


# The run


def finding(code: OriginalityCode, **fields) -> Finding:
    fields.setdefault("status", FAIL if code is CODE.NEAR_DUPLICATE else WARN)
    if code in (CODE.NEAR_DUPLICATE, CODE.HIGH_OVERLAP, CODE.COPIED_SENTENCES):
        fields.setdefault("prior_script_id", "prior")
        fields.setdefault("prior_content_item_id", "other")
    return Finding(code, **fields)


def check_of(*findings: Finding, words: int = 40, priors: int = 2) -> OriginalityCheck:
    return OriginalityCheck.create(
        script("x", item="item"),
        OriginalityAnalysis(words, priors, findings),
        requested_by=USER,
        clock=lambda: T0,
    )


def test_the_run_status_is_the_worst_finding() -> None:
    overlap = finding(CODE.HIGH_OVERLAP, score=0.3, matched=9)
    duplicate = finding(CODE.NEAR_DUPLICATE, score=0.7, matched=14)
    repeated = finding(CODE.REPEATED_SENTENCES, matched=2)

    assert check_of().status is PASS
    assert check_of(repeated).status is WARN
    assert check_of(overlap, repeated).status is WARN
    assert check_of(overlap, duplicate, repeated).status is FAIL
    assert check_of(duplicate).status is FAIL


def test_a_run_counts_its_findings() -> None:
    overlap = finding(CODE.HIGH_OVERLAP, score=0.3, matched=9)
    duplicate = finding(CODE.NEAR_DUPLICATE, score=0.7, matched=14)
    check = check_of(overlap, duplicate, finding(CODE.REPEATED_OPENERS, matched=3))

    assert (check.findings_count, check.warn_count, check.fail_count) == (3, 2, 1)
    assert check.method == ORIGINALITY_METHOD == "originality-rules-v1"
    assert check.counts() == {
        "words": 40,
        "priors": 2,
        "findings": 3,
        "warn": 2,
        "fail": 1,
        "status": "fail",
    }
    assert all(isinstance(value, str | int) for value in check.counts().values())


def test_a_run_is_made_for_the_script() -> None:
    target = script("Some words.", item="item-9")

    check = OriginalityCheck.create(
        target, OriginalityAnalysis(2, 0, ()), requested_by=USER, clock=lambda: T0
    )

    assert (check.script_id, check.content_item_id) == (target.id, "item-9")
    assert (check.requested_by, check.created_at) == (USER, T0)
    assert check.id


def test_the_limits_are_the_documented_ones() -> None:
    assert (MAX_PRIORS, MAX_WORDS, MIN_WORDS) == (50, 20_000, 20)
    assert MAX_FINDINGS == 103


def test_runs_reject_invalid_state() -> None:
    check = check_of(finding(CODE.HIGH_OVERLAP, score=0.3, matched=9))
    duplicate = finding(CODE.NEAR_DUPLICATE, score=0.7, matched=14)
    for bad in (
        dict(id=""),
        dict(script_id=""),
        dict(content_item_id=""),
        dict(method=" "),
        dict(method="x" * 101),
        dict(words=-1),
        dict(words=MAX_WORDS + 1),
        dict(words=True),
        dict(priors_count=-1),
        dict(priors_count=MAX_PRIORS + 1),
        dict(findings_count=0),  # must count the findings
        dict(warn_count=0, fail_count=1),  # must count the WARN findings
        dict(warn_count=2),
        dict(findings=check.findings + (duplicate,)),
        dict(findings=[check.findings[0]]),
        dict(words=MIN_WORDS - 1),  # reuse needs enough words
        dict(priors_count=0),  # reuse needs a prior script
        dict(created_at=datetime(2026, 10, 3)),
        dict(created_at=T0.astimezone(timezone(timedelta(hours=7)))),
    ):
        with pytest.raises((ValueError, TypeError)):
            dataclasses.replace(check, **bad)
    with pytest.raises(TypeError):
        dataclasses.replace(check, requested_by="owner")
    with pytest.raises(TypeError):
        dataclasses.replace(check, findings=("finding",))


def test_the_rules_of_v1_apply_only_to_runs_of_that_method() -> None:
    odd = (
        finding(CODE.HIGH_OVERLAP, score=0.12345, matched=3),
        finding(CODE.REPEATED_HOOK, matched=1),
    )
    base = check_of(words=3, priors=0)
    fields = dict(
        findings=odd,
        findings_count=2,
        warn_count=2,
        fail_count=0,
    )

    with pytest.raises(ValueError):
        dataclasses.replace(base, **fields)  # the v1 method refuses them
    other = dataclasses.replace(base, method="originality-rules-v2", **fields)

    assert other.status is WARN
    assert other.findings == odd


def test_a_script_is_not_a_prior_of_itself_in_a_run() -> None:
    check = check_of(finding(CODE.HIGH_OVERLAP, score=0.3, matched=9))
    own = dataclasses.replace(check.findings[0], prior_script_id=check.script_id)

    with pytest.raises(ValueError):
        dataclasses.replace(check, findings=(own,))


def test_a_repeated_hook_needs_three_of_the_priors() -> None:
    hook = finding(CODE.REPEATED_HOOK, matched=3)

    assert check_of(hook, priors=3).findings == (hook,)
    for bad in (
        dict(priors=2),  # fewer priors than matches
        dict(hook=finding(CODE.REPEATED_HOOK, matched=2)),
        dict(hook=finding(CODE.REPEATED_HOOK)),
        dict(hook=finding(CODE.REPEATED_HOOK, matched=4)),
    ):
        with pytest.raises(ValueError):
            check_of(bad.get("hook", hook), priors=bad.get("priors", 3))


def test_a_repeated_finding_needs_no_prior_nor_enough_words() -> None:
    repeated = finding(CODE.REPEATED_SENTENCES, matched=2)

    assert check_of(repeated, words=3, priors=0).status is WARN


@pytest.mark.parametrize(
    "bad",
    [
        dict(status=FAIL),  # high_overlap is a WARN
        dict(prior_script_id=None),  # a reuse finding names its prior
        dict(prior_content_item_id=None),
        dict(prior_script_id=" "),
        dict(prior_content_item_id=""),
        dict(section_index=-1),
        dict(section_index=True),
        dict(score=1.5),
        dict(score=-0.1),
        dict(score=True),
        dict(matched=-1),
        dict(matched=True),
    ],
)
def test_findings_reject_invalid_state(bad) -> None:
    good = dict(
        code=CODE.HIGH_OVERLAP,
        status=WARN,
        prior_script_id="prior",
        prior_content_item_id="other",
        section_index=1,
        score=0.5,
        matched=9,
    )

    assert Finding(**good).score == 0.5
    with pytest.raises(ValueError):
        Finding(**{**good, **bad})


def test_other_findings_name_no_prior() -> None:
    with pytest.raises(ValueError):
        Finding(CODE.REPEATED_OPENERS, WARN, "prior", "other", matched=3)
    with pytest.raises(ValueError):
        Finding(CODE.REPEATED_OPENERS, WARN, prior_script_id="prior")


@pytest.mark.parametrize("bad", [dict(code="high_overlap"), dict(status="warn")])
def test_findings_reject_values_of_the_wrong_type(bad) -> None:
    good = dict(
        code=CODE.HIGH_OVERLAP,
        status=WARN,
        prior_script_id="prior",
        prior_content_item_id="other",
    )

    with pytest.raises(TypeError):
        Finding(**{**good, **bad})


def test_only_a_near_duplicate_is_a_failure() -> None:
    for code in OriginalityCode:
        expected = FAIL if code is CODE.NEAR_DUPLICATE else WARN
        with pytest.raises(ValueError):
            Finding(code, PASS)  # a finding is never a pass
        assert finding(code).status is expected


def test_the_rules_need_priors_of_other_items() -> None:
    target = script("Some words here.", item="item")

    with pytest.raises(ValueError):
        check_originality(target, [script("Same item.", item="item")])
    with pytest.raises(ValueError):
        check_originality(
            target, [script("Other.", item=f"i{i}") for i in range(MAX_PRIORS + 1)]
        )
    assert (
        check_originality(
            target, [script("Other.", item=f"i{i}") for i in range(MAX_PRIORS)]
        ).priors_count
        == MAX_PRIORS
    )
