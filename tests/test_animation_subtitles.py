"""Subtitles: alignment, cues, layout, contrast, SRT/VTT (pure) and rendering."""

from __future__ import annotations

import pytest

from ai_youtube_agent.animation.errors import AnimationError
from ai_youtube_agent.animation.storyboard import text_tokens
from ai_youtube_agent.animation.subtitles import (
    ACTIVE_RGB,
    BOX_ALPHA,
    BOX_RGB,
    CAPTION_RECT,
    MIN_CONTRAST,
    SAFE_AREA,
    STROKE_RGB,
    TEXT_RGB,
    CueSource,
    Rect,
    SubtitleCue,
    TimingEvent,
    WordTiming,
    active_word_index,
    align_boundaries,
    blend_over,
    build_cues,
    contrast_ratio,
    cue_at,
    format_srt,
    format_vtt,
    layout_cue,
    wrap_balanced,
    write_srt,
    write_vtt,
)

LINE = "Mười năm rồi, Mai vẫn nấu theo công thức ấy."


def events(text: str, start: int = 100, step: int = 300) -> list[TimingEvent]:
    """One word boundary per token of ``text`` (punctuation stripped like Edge)."""
    return [
        TimingEvent(
            "WordBoundary",
            start + i * step,
            start + i * step + step - 40,
            "".join(c for c in token if c.isalnum()),
        )
        for i, token in enumerate(text_tokens(text))
    ]


def words_of(text: str, start: int = 100, step: int = 300) -> tuple[WordTiming, ...]:
    return align_boundaries(text, events(text, start, step))[0]


def fake_measure(text: str, size: int) -> float:
    return len(text) * size * 0.5


# --- alignment -----------------------------------------------------------------


def test_exact_word_boundaries_give_word_timings_with_the_source_punctuation() -> None:
    words, source = align_boundaries(LINE, events(LINE))

    assert source == "word_boundary"
    assert [w.text for w in words] == text_tokens(LINE)
    assert words[2].text == "rồi,"
    assert (words[0].start_ms, words[0].end_ms) == (100, 360)
    assert [w.index for w in words] == list(range(10))


def test_case_and_unicode_form_do_not_break_the_alignment() -> None:
    nfd = [
        TimingEvent("WordBoundary", 0, 200, "SÁNG"),
        TimingEvent("WordBoundary", 250, 500, "nay"),
    ]

    words, source = align_boundaries("Sáng nay,", nfd)

    assert source == "word_boundary"
    assert words[0].text == "Sáng"


def test_a_different_word_count_falls_back_to_a_flagged_estimate() -> None:
    wrong = events(LINE)[:-1]

    words, source = align_boundaries(LINE, wrong)

    assert source == "sentence_estimate"
    assert len(words) == 10
    assert words[0].start_ms == wrong[0].start_ms
    assert words[-1].end_ms == wrong[-1].end_ms
    assert all(a.end_ms <= b.start_ms for a, b in zip(words, words[1:], strict=False))


def test_sentence_boundaries_alone_are_an_estimate_over_their_span() -> None:
    sentence = [TimingEvent("SentenceBoundary", 100, 2100, LINE)]

    words, source = align_boundaries(LINE, sentence)

    assert source == "sentence_estimate"
    assert (words[0].start_ms, words[-1].end_ms) == (100, 2100)
    # Longer words get more time.
    assert words[8].end_ms - words[8].start_ms > words[3].end_ms - words[3].start_ms


def test_no_boundaries_uses_the_total_duration_or_fails() -> None:
    words, source = align_boundaries("Ngon như xưa", [], total_ms=900)

    assert source == "sentence_estimate"
    assert (words[0].start_ms, words[-1].end_ms) == (0, 900)
    with pytest.raises(AnimationError, match="cannot time the words"):
        align_boundaries("Ngon như xưa", [])
    with pytest.raises(AnimationError, match="without words"):
        align_boundaries("... !", [], total_ms=900)


def test_word_starts_never_go_backwards() -> None:
    jumbled = [
        TimingEvent("WordBoundary", 500, 700, "ngon"),
        TimingEvent("WordBoundary", 300, 450, "quá"),
    ]

    words, _ = align_boundaries("Ngon quá", jumbled)

    assert words[1].start_ms >= words[0].start_ms
    assert all(w.end_ms > w.start_ms for w in words)


# --- cues ----------------------------------------------------------------------


def test_wrap_balanced() -> None:
    assert wrap_balanced(["Sáng", "nay"]) == ("Sáng nay",)
    two = wrap_balanced(text_tokens(LINE))
    assert two == ("Mười năm rồi, Mai vẫn", "nấu theo công thức ấy.")
    assert wrap_balanced(["x" * 20, "y" * 20, "z" * 20]) is None
    assert wrap_balanced(["a" * 20, "b" * 20], max_lines=1) is None


def test_cues_obey_the_limits_and_never_overlap() -> None:
    sources = [
        CueSource("a", words_of("Sáng nay, quán của Mai vắng khách.", 400)),
        CueSource("b", words_of(LINE, 3500)),
        CueSource(
            "c", words_of("Mai chan nước dùng nóng hổi, thêm thịt, thêm hành.", 7000)
        ),
    ]

    cues = build_cues(sources)

    assert [c.index for c in cues] == list(range(1, len(cues) + 1))
    for cue in cues:
        assert len(cue.lines) <= 2
        assert all(len(line) <= 32 for line in cue.lines)
        assert cue.end_ms - cue.start_ms >= 600
        assert cue.end_ms - cue.start_ms <= 3200
        assert cue.words[0].start_ms - 40 == cue.start_ms
    assert all(a.end_ms <= b.start_ms for a, b in zip(cues, cues[1:], strict=False))
    assert sorted(c.start_ms for c in cues) == [c.start_ms for c in cues]


def test_a_cue_stays_120_ms_after_its_last_word() -> None:
    words = words_of("Ngon như ngày xưa đấy!", 1000, 400)

    (cue,) = build_cues([CueSource("c2", words)])

    assert cue.end_ms == words[-1].end_ms + 120
    assert cue.text == "Ngon như ngày xưa đấy!"


def test_a_cue_never_spans_two_spoken_lines() -> None:
    cues = build_cues(
        [
            CueSource("a", words_of("Bác nếm thử.", 0)),
            CueSource("b", words_of("Ngon quá!", 1000)),
        ]
    )

    assert [c.line_id for c in cues] == ["a", "b"]


def test_a_long_line_breaks_at_punctuation_when_it_is_too_long_for_one_cue() -> None:
    words = words_of("Mai chan nước dùng nóng hổi, thêm thịt, thêm hành.", 0, 330)

    cues = build_cues([CueSource("n5", words)])

    assert len(cues) == 2
    assert cues[0].words[-1].text == "hổi,"
    assert cues[1].words[0].text == "thêm"
    assert cues[1].text == "thêm thịt, thêm hành."


def test_a_pause_splits_a_cue_only_when_it_does_not_fit() -> None:
    first = list(words_of("Bỗng một bác", 0, 300))
    second = [
        WordTiming(i + 3, t, 2000 + i * 300, 2260 + i * 300)
        for i, t in enumerate(["lớn", "tuổi"])
    ]
    words = tuple(first + second)

    cues = build_cues([CueSource("n4", words)])

    assert len(cues) == 1  # the pause alone does not force a split when it fits
    long_words = tuple(
        WordTiming(i, "abcdefg", i * 600, i * 600 + 300) for i in range(7)
    )
    split = build_cues([CueSource("x", long_words)])
    assert len(split) >= 2  # 7 words * 600 ms exceed 3.2 s


def test_a_very_short_cue_is_stretched_to_the_minimum_unless_the_next_starts() -> None:
    (cue,) = build_cues([CueSource("a", (WordTiming(0, "Ừ.", 1000, 1200),))])
    assert cue.end_ms - cue.start_ms == 600
    pair = build_cues(
        [
            CueSource("a", (WordTiming(0, "Ừ.", 1000, 1100),)),
            CueSource("b", (WordTiming(0, "Vâng.", 1300, 1500),)),
        ]
    )
    assert pair[0].end_ms == pair[1].start_ms


def test_cues_without_words_are_an_error() -> None:
    with pytest.raises(AnimationError, match="no words"):
        build_cues([CueSource("x", ())])


def test_active_word_leads_by_40_ms() -> None:
    (cue,) = build_cues([CueSource("a", words_of("Ngon như xưa", 1000, 400))])

    assert active_word_index(cue, 900) is None
    assert active_word_index(cue, 960) == 0
    assert active_word_index(cue, 1359) == 0
    assert active_word_index(cue, 1360) == 1
    assert active_word_index(cue, 99999) == 2


def test_cue_at() -> None:
    cues = build_cues([CueSource("a", words_of("Ngon quá!", 1000))])

    assert cue_at(cues, 500) is None
    assert cue_at(cues, 1000) is cues[0]
    assert cue_at(cues, cues[0].end_ms) is None


# --- layout --------------------------------------------------------------------


def rgba_pixels(image) -> list[tuple[int, ...]]:
    data = image.tobytes()
    return [tuple(data[i : i + 4]) for i in range(0, len(data), 4)]


def cue_of(text: str) -> SubtitleCue:
    return build_cues([CueSource("x", words_of(text, 1000, 250))])[0]


def test_layout_centres_the_cue_in_the_caption_rect() -> None:
    layout = layout_cue(cue_of("Ngon như ngày xưa đấy!"), fake_measure)

    box = layout.box
    assert CAPTION_RECT.contains(box)
    assert SAFE_AREA.contains(box)
    assert box.bottom == CAPTION_RECT.bottom - 24
    assert (box.left + box.right) / 2 == pytest.approx(
        (CAPTION_RECT.left + CAPTION_RECT.right) / 2, abs=1
    )
    assert len(layout.lines) == 1
    assert layout.font_px == 58


def test_layout_breaks_into_two_lines_and_places_every_word() -> None:
    cue = cue_of(LINE)

    layout = layout_cue(cue, fake_measure, font_px=58)

    assert len(layout.lines) == 2
    placed = [w.word_index for line in layout.lines for w in line.words]
    assert placed == list(range(len(cue.words)))
    first, second = layout.lines
    assert second.y == first.y + layout.line_height
    for line in layout.lines:
        assert line.words[0].x >= layout.pad_x - 1
        assert line.words[-1].x + line.words[-1].width <= layout.box.width


def test_layout_shrinks_the_font_to_fit_and_fails_when_it_cannot() -> None:
    wide = lambda text, size: len(text) * size * 0.75  # noqa: E731
    cue = cue_of(LINE)

    layout = layout_cue(cue, wide, font_px=58)

    assert layout.font_px < 58
    with pytest.raises(AnimationError, match="does not fit the caption area"):
        layout_cue(cue, lambda text, size: len(text) * 40.0, font_px=58)


def test_rect_helpers() -> None:
    a = Rect(0, 0, 10, 10)

    assert a.contains(Rect(1, 1, 9, 9)) and not a.contains(Rect(5, 5, 11, 9))
    assert a.intersects(Rect(9, 9, 20, 20)) and not a.intersects(Rect(10, 0, 20, 10))
    assert (a.width, a.height) == (10, 10)


def test_safe_area_constants() -> None:
    assert SAFE_AREA.top == 230 and SAFE_AREA.bottom == 1536 and SAFE_AREA.right == 950
    assert (CAPTION_RECT.top, CAPTION_RECT.bottom) == (1260, 1500)
    assert SAFE_AREA.contains(CAPTION_RECT)


# --- contrast ------------------------------------------------------------------


def test_contrast_ratio_extremes() -> None:
    assert contrast_ratio((0, 0, 0), (255, 255, 255)) == pytest.approx(21.0)
    assert contrast_ratio((255, 255, 255), (255, 255, 255)) == pytest.approx(1.0)


def test_caption_text_has_wcag_aaa_contrast_against_its_outline() -> None:
    assert contrast_ratio(TEXT_RGB, STROKE_RGB) >= MIN_CONTRAST
    assert contrast_ratio(ACTIVE_RGB, STROKE_RGB) >= MIN_CONTRAST


def test_blend_over_composites_the_box() -> None:
    assert blend_over(BOX_RGB, 255, (255, 255, 255)) == BOX_RGB
    assert blend_over(BOX_RGB, 0, (10, 20, 30)) == (10, 20, 30)
    dim = blend_over(BOX_RGB, BOX_ALPHA, (255, 255, 255))
    assert 100 < dim[0] < 130


# --- SRT / VTT -----------------------------------------------------------------


def test_srt_and_vtt_formats(tmp_path) -> None:
    cues = build_cues(
        [
            CueSource("a", words_of("Ngon quá!", 3_723_000)),
            CueSource(
                "b", words_of("Mười năm rồi, Mai vẫn nấu theo công thức ấy.", 3_730_000)
            ),
        ]
    )

    srt = format_srt(cues)
    vtt = format_vtt(cues)

    assert srt.startswith("1\n01:02:02,960 --> 01:02:03,680\nNgon quá!\n\n2\n")
    assert "Mười năm rồi, Mai vẫn\nnấu theo công thức ấy." in srt
    assert vtt.startswith("WEBVTT\n\n01:02:02.960 --> 01:02:03.680\nNgon quá!\n")
    assert write_srt(cues, tmp_path / "a.srt").read_text(encoding="utf-8") == srt
    assert write_vtt(cues, tmp_path / "a.vtt").read_text(encoding="utf-8") == vtt


# --- rendering (Pillow) --------------------------------------------------------


def test_render_cue_draws_the_box_text_and_active_word(tmp_path) -> None:
    pytest.importorskip("PIL.Image")
    from ai_youtube_agent.animation.fonts import resolve_font_files
    from ai_youtube_agent.animation.pipeline_steps import StepBlocked
    from ai_youtube_agent.animation.subtitles import font_measure, render_cue

    try:
        fonts = resolve_font_files(None)
    except StepBlocked:
        pytest.skip("no Vietnamese-capable TrueType font on this machine")
    cue = cue_of("Ngon như ngày xưa đấy!")
    layout = layout_cue(cue, font_measure(fonts.bold))

    image, position = render_cue(layout, cue, 1000 + 2 * 250, fonts.bold)

    assert image.mode == "RGBA"
    assert image.size == (layout.box.width, layout.box.height)
    assert position == (layout.box.left, layout.box.top)
    pixels = rgba_pixels(image)
    assert any(p[3] == BOX_ALPHA for p in pixels)  # the translucent box
    assert any(p[:3] == TEXT_RGB and p[3] == 255 for p in pixels)  # white text
    assert any(p[:3] == ACTIVE_RGB and p[3] == 255 for p in pixels)  # active word
    before, _ = render_cue(layout, cue, 100, fonts.bold)
    assert not any(p[:3] == ACTIVE_RGB for p in rgba_pixels(before))
