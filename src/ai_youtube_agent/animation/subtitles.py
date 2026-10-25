"""Subtitles: word alignment, cue building, safe-area layout, SRT/VTT, rendering.

Everything except ``render_cue``/``font_measure`` is pure Python. Pillow is
imported lazily inside those two, so the module loads without the ``demo``
extra. Times are milliseconds.
"""

from __future__ import annotations

import unicodedata
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Final, Protocol

from ai_youtube_agent.animation.errors import AnimationError
from ai_youtube_agent.animation.storyboard import text_tokens

FRAME_SIZE: Final[tuple[int, int]] = (1080, 1920)
WORD_BOUNDARY: Final[str] = "WordBoundary"
TIMING_WORD_BOUNDARY: Final[str] = "word_boundary"
TIMING_SENTENCE_ESTIMATE: Final[str] = "sentence_estimate"
TIMING_SOURCES: Final[tuple[str, ...]] = (
    TIMING_WORD_BOUNDARY,
    TIMING_SENTENCE_ESTIMATE,
)

MAX_LINE_CHARS: Final[int] = 32
MAX_CUE_LINES: Final[int] = 2
MIN_CUE_MS: Final[int] = 600
MAX_CUE_MS: Final[int] = 3200
PAUSE_BREAK_MS: Final[int] = 250
HOLD_OVER_MS: Final[int] = 120
ACTIVE_WORD_LEAD_MS: Final[int] = 40
_SENTENCE_END: Final[str] = ".!?…"
_PHRASE_END: Final[str] = ",;:" + _SENTENCE_END

TEXT_RGB: Final[tuple[int, int, int]] = (255, 255, 255)
ACTIVE_RGB: Final[tuple[int, int, int]] = (0xFF, 0xD8, 0x4A)
STROKE_RGB: Final[tuple[int, int, int]] = (26, 15, 10)
BOX_RGB: Final[tuple[int, int, int]] = (18, 12, 8)
BOX_ALPHA: Final[int] = 150
STROKE_PX: Final[int] = 6
MIN_CONTRAST: Final[float] = 7.0


@dataclass(frozen=True)
class Rect:
    left: int
    top: int
    right: int
    bottom: int

    @property
    def width(self) -> int:
        return self.right - self.left

    @property
    def height(self) -> int:
        return self.bottom - self.top

    def contains(self, other: Rect) -> bool:
        return (
            self.left <= other.left
            and self.top <= other.top
            and other.right <= self.right
            and other.bottom <= self.bottom
        )

    def intersects(self, other: Rect) -> bool:
        return (
            self.left < other.right
            and other.left < self.right
            and self.top < other.bottom
            and other.top < self.bottom
        )


# Reserved by the platforms: top 12% (y < 230), bottom 20% (y > 1536), right 12%
# (x > 950). Subtitle pixels must stay inside the safe area; the caption rect is
# where captions sit (faces stay above it).
SAFE_AREA: Final[Rect] = Rect(left=64, top=230, right=950, bottom=1536)
CAPTION_RECT: Final[Rect] = Rect(left=64, top=1260, right=950, bottom=1500)
CAPTION_BOTTOM_MARGIN: Final[int] = 24


@dataclass(frozen=True)
class WordTiming:
    """One spoken word (a Vietnamese syllable) with its time span."""

    index: int
    text: str
    start_ms: int
    end_ms: int

    def shifted(self, delta_ms: int) -> WordTiming:
        return WordTiming(
            self.index, self.text, self.start_ms + delta_ms, self.end_ms + delta_ms
        )


class BoundaryLike(Protocol):
    """What ``align_boundaries`` needs from an Edge boundary event."""

    kind: str
    start_ms: int
    end_ms: int
    text: str


@dataclass(frozen=True)
class TimingEvent:
    """A plain ``BoundaryLike`` (an Edge boundary or an estimated span)."""

    kind: str
    start_ms: int
    end_ms: int
    text: str


def _normal(token: str) -> str:
    folded = unicodedata.normalize("NFC", token).casefold()
    return "".join(ch for ch in folded if ch.isalnum())


def _tidy(words: list[WordTiming]) -> tuple[WordTiming, ...]:
    """Monotonic starts; each end after its start and not past the next start."""
    tidy: list[WordTiming] = []
    for position, word in enumerate(words):
        start = max(word.start_ms, tidy[-1].start_ms if tidy else 0)
        end = max(word.end_ms, start + 1)
        if position + 1 < len(words):
            end = max(start + 1, min(end, words[position + 1].start_ms))
        tidy.append(WordTiming(word.index, word.text, start, end))
    return tuple(tidy)


def align_boundaries(
    source_text: str,
    boundaries: Sequence[BoundaryLike],
    *,
    total_ms: int | None = None,
) -> tuple[tuple[WordTiming, ...], str]:
    """Words of ``source_text`` with times, and where the times came from.

    When Edge returned one word boundary per word of ``source_text`` (same words
    in the same order) the times are taken from Edge: ``word_boundary``. In any
    other case (sentence boundaries only, a different word count or text) the
    words are spread over the audio span in proportion to their letters:
    ``sentence_estimate``. That span is the first to the last boundary, or
    ``0..total_ms`` when there is none; with neither an ``AnimationError`` is
    raised (never an invented timing).
    """
    tokens = text_tokens(source_text)
    if not tokens:
        raise AnimationError("cannot time a text without words")
    word_events = [event for event in boundaries if event.kind == WORD_BOUNDARY]
    if len(word_events) == len(tokens) and all(
        _normal(event.text) == _normal(token)
        for event, token in zip(word_events, tokens, strict=True)
    ):
        return (
            _tidy(
                [
                    WordTiming(i, token, event.start_ms, event.end_ms)
                    for i, (token, event) in enumerate(
                        zip(tokens, word_events, strict=True)
                    )
                ]
            ),
            TIMING_WORD_BOUNDARY,
        )

    if boundaries:
        start = min(event.start_ms for event in boundaries)
        end = max(event.end_ms for event in boundaries)
    elif total_ms is not None and total_ms > 0:
        start, end = 0, total_ms
    else:
        raise AnimationError("no boundaries and no duration: cannot time the words")
    if end <= start:
        raise AnimationError("the boundary span is empty: cannot time the words")
    weights = [max(1, len(_normal(token))) for token in tokens]
    total_weight = sum(weights)
    words: list[WordTiming] = []
    cursor = 0
    for i, (token, weight) in enumerate(zip(tokens, weights, strict=True)):
        w_start = start + round((end - start) * cursor / total_weight)
        cursor += weight
        w_end = start + round((end - start) * cursor / total_weight)
        words.append(WordTiming(i, token, w_start, w_end))
    return _tidy(words), TIMING_SENTENCE_ESTIMATE


# ---------------------------------------------------------------------------
# Cues
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CueSource:
    """The words of one spoken line with absolute times (ms)."""

    line_id: str
    words: tuple[WordTiming, ...]


@dataclass(frozen=True)
class SubtitleCue:
    index: int
    line_id: str
    start_ms: int
    end_ms: int
    words: tuple[WordTiming, ...]
    lines: tuple[str, ...]

    @property
    def text(self) -> str:
        return "\n".join(self.lines)

    def as_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "line_id": self.line_id,
            "start_ms": self.start_ms,
            "end_ms": self.end_ms,
            "text": self.text,
        }


def wrap_balanced(
    tokens: Sequence[str],
    *,
    max_chars: int = MAX_LINE_CHARS,
    max_lines: int = MAX_CUE_LINES,
) -> tuple[str, ...] | None:
    """Break ``tokens`` into at most ``max_lines`` balanced lines of ``max_chars``.

    ``None`` when they do not fit. One line is preferred; with two the break is
    the one that makes the lines most equal in length.
    """
    whole = " ".join(tokens)
    if len(whole) <= max_chars:
        return (whole,)
    if max_lines < 2:
        return None
    best: tuple[int, int] | None = None
    for cut in range(1, len(tokens)):
        left = " ".join(tokens[:cut])
        right = " ".join(tokens[cut:])
        if len(left) <= max_chars and len(right) <= max_chars:
            gap = abs(len(left) - len(right))
            if best is None or gap < best[0]:
                best = (gap, cut)
    if best is None:
        return None
    cut = best[1]
    return (" ".join(tokens[:cut]), " ".join(tokens[cut:]))


def _fits(words: Sequence[WordTiming]) -> tuple[str, ...] | None:
    """The wrapped lines when ``words`` fit one cue (size and duration), else None."""
    lasts = words[-1].end_ms + HOLD_OVER_MS - (words[0].start_ms - ACTIVE_WORD_LEAD_MS)
    if lasts > MAX_CUE_MS:
        return None
    return wrap_balanced([word.text for word in words])


def _sentences(words: Sequence[WordTiming]) -> list[Sequence[WordTiming]]:
    """Split after each sentence end (. ! ? ...): a cue never crosses one."""
    sentences: list[Sequence[WordTiming]] = []
    begin = 0
    for position, word in enumerate(words):
        if word.text[-1] in _SENTENCE_END and position + 1 < len(words):
            sentences.append(words[begin : position + 1])
            begin = position + 1
    sentences.append(words[begin:])
    return sentences


def _split_to_fit(words: Sequence[WordTiming]) -> list[list[WordTiming]]:
    """``words`` as one cue, or split where punctuation or a pause allows.

    A group that does not fit is cut at the punctuation mark or pause of 250 ms
    or more that makes the two halves most equal in time (any word boundary if
    there is none), and each half is split again if it still does not fit.
    """
    if len(words) == 1 or _fits(words) is not None:
        return [list(words)]
    breaks = [
        cut
        for cut in range(1, len(words))
        if words[cut - 1].text[-1] in _PHRASE_END
        or words[cut].start_ms - words[cut - 1].end_ms >= PAUSE_BREAK_MS
    ]
    cut = min(
        breaks or range(1, len(words)),
        key=lambda c: abs(
            (words[c - 1].end_ms - words[0].start_ms)
            - (words[-1].end_ms - words[c].start_ms)
        ),
    )
    return _split_to_fit(words[:cut]) + _split_to_fit(words[cut:])


def _cue_groups(words: Sequence[WordTiming]) -> list[list[WordTiming]]:
    """Group the words of one line into cues that fit the line/length limits."""
    return [group for part in _sentences(words) for group in _split_to_fit(part)]


def build_cues(sources: Sequence[CueSource]) -> tuple[SubtitleCue, ...]:
    """Subtitle cues for the spoken lines, in time order, never overlapping.

    At most two lines of 32 characters, lasting 600 ms to 3.2 s and staying
    120 ms after the last word. A sentence is one cue when it fits, otherwise
    it is split at punctuation or a pause of 250 ms. A cue never spans two
    spoken lines or two sentences.
    """
    drafts: list[tuple[str, int, int, tuple[WordTiming, ...], tuple[str, ...]]] = []
    for source in sources:
        if not source.words:
            raise AnimationError(f"line '{source.line_id}' has no words to show")
        for group in _cue_groups(source.words):
            lines = _fits(group)
            if lines is None:
                raise AnimationError(
                    f"line '{source.line_id}': a single word does not fit a cue"
                )
            start = max(0, group[0].start_ms - ACTIVE_WORD_LEAD_MS)
            end = max(group[-1].end_ms + HOLD_OVER_MS, start + MIN_CUE_MS)
            drafts.append((source.line_id, start, end, tuple(group), lines))
    drafts.sort(key=lambda draft: draft[1])
    cues: list[SubtitleCue] = []
    for position, (line_id, start, end, group, lines) in enumerate(drafts):
        if position + 1 < len(drafts):
            end = min(end, drafts[position + 1][1])
        if end <= start:
            raise AnimationError(f"cue {position + 1} of '{line_id}' has no duration")
        cues.append(SubtitleCue(position + 1, line_id, start, end, group, lines))
    return tuple(cues)


def active_word_index(cue: SubtitleCue, t_ms: float) -> int | None:
    """Index in ``cue.words`` of the word being spoken at ``t_ms`` (40 ms early)."""
    active: int | None = None
    for position, word in enumerate(cue.words):
        if word.start_ms - ACTIVE_WORD_LEAD_MS <= t_ms:
            active = position
        else:
            break
    return active


def cue_at(cues: Sequence[SubtitleCue], t_ms: float) -> SubtitleCue | None:
    for cue in cues:
        if cue.start_ms <= t_ms < cue.end_ms:
            return cue
    return None


# ---------------------------------------------------------------------------
# SRT / VTT
# ---------------------------------------------------------------------------


def _clock(ms: int, decimal: str) -> str:
    return (
        f"{ms // 3_600_000:02d}:{(ms // 60_000) % 60:02d}:"
        f"{(ms // 1000) % 60:02d}{decimal}{ms % 1000:03d}"
    )


def format_srt(cues: Sequence[SubtitleCue]) -> str:
    blocks = [
        f"{cue.index}\n{_clock(cue.start_ms, ',')} --> {_clock(cue.end_ms, ',')}\n"
        f"{cue.text}\n"
        for cue in cues
    ]
    return "\n".join(blocks)


def format_vtt(cues: Sequence[SubtitleCue]) -> str:
    blocks = [
        f"{_clock(cue.start_ms, '.')} --> {_clock(cue.end_ms, '.')}\n{cue.text}\n"
        for cue in cues
    ]
    return "WEBVTT\n\n" + "\n".join(blocks)


def write_srt(cues: Sequence[SubtitleCue], path: Path) -> Path:
    path.write_text(format_srt(cues), encoding="utf-8")
    return path


def write_vtt(cues: Sequence[SubtitleCue], path: Path) -> Path:
    path.write_text(format_vtt(cues), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Layout (pure; the font metrics are injected)
# ---------------------------------------------------------------------------

Measure = Callable[[str, int], float]


@dataclass(frozen=True)
class PlacedWord:
    """A word of a cue and where it is drawn (``x`` relative to the box)."""

    word_index: int
    text: str
    x: float
    width: float


@dataclass(frozen=True)
class LineLayout:
    words: tuple[PlacedWord, ...]
    width: float
    y: float  # top of the line, relative to the box


@dataclass(frozen=True)
class CueLayout:
    font_px: int
    line_height: int
    pad_x: int
    pad_y: int
    lines: tuple[LineLayout, ...]
    box: Rect  # in frame pixels


def layout_cue(
    cue: SubtitleCue,
    measure: Measure,
    *,
    font_px: int = 58,
    min_font_px: int = 46,
    line_height_factor: float = 1.14,
    pad_x: int = 28,
    pad_y: int = 16,
    caption_rect: Rect = CAPTION_RECT,
) -> CueLayout:
    """Place the cue in the caption rect, bottom aligned and centred.

    ``measure(text, font_px)`` returns the width in pixels of ``text``. The font
    shrinks from ``font_px`` toward ``min_font_px`` until the words fit in at
    most two lines of the caption width; an ``AnimationError`` follows when they
    never do. The box always lies inside ``caption_rect``.
    """
    available = caption_rect.width - 2 * pad_x
    tokens = [word.text for word in cue.words]
    size = font_px
    chosen: list[list[int]] | None = None
    while size >= min_font_px and chosen is None:
        chosen = _split_by_width(
            tokens, lambda text, px=size: measure(text, px), available
        )
        if chosen is None:
            size -= 2
    if chosen is None:
        raise AnimationError(
            f"cue {cue.index} does not fit the caption area even at {min_font_px} px"
        )
    line_height = round(size * line_height_factor)
    lines: list[LineLayout] = []
    for row, indexes in enumerate(chosen):
        placed: list[PlacedWord] = []
        cursor = 0.0
        space = measure(" ", size)
        for position, index in enumerate(indexes):
            width = measure(tokens[index], size)
            placed.append(PlacedWord(index, tokens[index], cursor, width))
            cursor += width + (space if position + 1 < len(indexes) else 0)
        lines.append(LineLayout(tuple(placed), cursor, pad_y + row * line_height))
    text_width = max(line.width for line in lines)
    box_width = round(text_width + 2 * pad_x)
    box_height = len(lines) * line_height + 2 * pad_y
    left = round(caption_rect.left + (caption_rect.width - box_width) / 2)
    bottom = caption_rect.bottom - CAPTION_BOTTOM_MARGIN
    box = Rect(left, bottom - box_height, left + box_width, bottom)
    if not caption_rect.contains(box):
        raise AnimationError(f"cue {cue.index} leaves the caption area")
    centred = [
        LineLayout(
            tuple(
                PlacedWord(
                    w.word_index, w.text, w.x + (box_width - line.width) / 2, w.width
                )
                for w in line.words
            ),
            line.width,
            line.y,
        )
        for line in lines
    ]
    return CueLayout(size, line_height, pad_x, pad_y, tuple(centred), box)


def _split_by_width(
    tokens: Sequence[str], width: Callable[[str], float], available: float
) -> list[list[int]] | None:
    """Indexes of ``tokens`` in one line, else two lines with the most equal widths."""
    if width(" ".join(tokens)) <= available:
        return [list(range(len(tokens)))]
    best: tuple[float, int] | None = None
    for cut in range(1, len(tokens)):
        left = width(" ".join(tokens[:cut]))
        right = width(" ".join(tokens[cut:]))
        if left <= available and right <= available:
            spread = max(left, right)
            if best is None or spread < best[0]:
                best = (spread, cut)
    if best is None:
        return None
    cut = best[1]
    return [list(range(cut)), list(range(cut, len(tokens)))]


# ---------------------------------------------------------------------------
# Contrast
# ---------------------------------------------------------------------------


def _linear(channel: int) -> float:
    value = channel / 255
    return value / 12.92 if value <= 0.03928 else ((value + 0.055) / 1.055) ** 2.4


def relative_luminance(rgb: Sequence[int]) -> float:
    red, green, blue = (_linear(int(c)) for c in rgb[:3])
    return 0.2126 * red + 0.7152 * green + 0.0722 * blue


def contrast_ratio(foreground: Sequence[int], background: Sequence[int]) -> float:
    """WCAG contrast ratio (1 to 21) between two RGB colours."""
    a, b = relative_luminance(foreground), relative_luminance(background)
    light, dark = max(a, b), min(a, b)
    return (light + 0.05) / (dark + 0.05)


def blend_over(
    foreground: Sequence[int], alpha: int, background: Sequence[int]
) -> tuple[int, int, int]:
    """``foreground`` at ``alpha`` (0..255) composited over ``background``."""
    a = alpha / 255
    return (
        round(foreground[0] * a + background[0] * (1 - a)),
        round(foreground[1] * a + background[1] * (1 - a)),
        round(foreground[2] * a + background[2] * (1 - a)),
    )


# ---------------------------------------------------------------------------
# Rendering (Pillow, imported lazily)
# ---------------------------------------------------------------------------


@lru_cache(maxsize=16)
def _font(font_file: str, size: int) -> Any:
    from PIL import ImageFont

    return ImageFont.truetype(font_file, size)


def font_measure(font_file: Path | str) -> Measure:
    """A ``measure(text, px)`` for ``layout_cue`` backed by a TrueType font."""
    path = str(font_file)

    def measure(text: str, size: int) -> float:
        return float(_font(path, size).getlength(text))

    return measure


def render_cue(
    layout: CueLayout,
    cue: SubtitleCue,
    t_ms: float,
    font_file: Path | str,
) -> tuple[Any, tuple[int, int]]:
    """The cue as an RGBA image and its top-left position in the frame.

    White text with a 6 px dark stroke on a rounded box at alpha 150; the word
    being spoken at ``t_ms`` is drawn in ``ACTIVE_RGB``.
    """
    from PIL import Image, ImageDraw

    box = layout.box
    image = Image.new("RGBA", (box.width, box.height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle(
        [0, 0, box.width - 1, box.height - 1],
        radius=max(4, layout.font_px // 3),
        fill=(*BOX_RGB, BOX_ALPHA),
    )
    font = _font(str(font_file), layout.font_px)
    active = active_word_index(cue, t_ms)
    for line in layout.lines:
        for word in line.words:
            draw.text(
                (word.x, line.y),
                word.text,
                font=font,
                fill=(*(ACTIVE_RGB if word.word_index == active else TEXT_RGB), 255),
                stroke_width=STROKE_PX,
                stroke_fill=(*STROKE_RGB, 255),
            )
    return image, (box.left, box.top)
