"""Shorts captions (Prompt Pack v8, prompt #097), context C8 Shorts Production.

The caption overlays themselves (``CaptionOverlay``, ``CaptionPosition``,
``CaptionStyle``) live in ``content/shorts_timeline.py``, because a ``Timeline``
holds and decodes them and checks their shape: absolute times, ordered, no
overlap and inside their scene. This module holds the rules that need the
project's ``ShortsConfig`` and the planner:

- ``wrap(text, rules)``: greedy word wrap into at most ``max_lines`` lines of at
  most ``max_chars_per_line`` characters. The text is normalised to NFC first, so
  composed and decomposed Vietnamese text count the same. A word longer than a
  line, or text that needs more lines, is a ``ValueError``.
- ``check_captions(config, timeline)``: every rule that depends on the
  configuration, as a ``CaptionCheck`` of ``errors`` and ``warnings`` (it never
  raises). Errors are ``invalid`` (the format has captions off but the timeline
  has some, display time outside ``min_display_ms``..``max_display_ms``, a
  caption that does not wrap) or ``unsafe`` (a caption box, or the box of an
  overlay asset, outside the safe area). More than ``MAX_CHARS_PER_SECOND``
  characters per second is only a warning. Full-bleed backgrounds and b-roll are
  allowed; an overlay without a ``box`` is not measured.
- ``caption_box``: the estimate of the box of one caption in pixels. It has
  ``lines x round(size_px * 1.2)`` height and ``longest line x size_px * 0.6``
  width (rounded up), sits in the safe area by ``position.anchor`` (top, center
  or bottom of the safe rect, then moved down by ``offset_px``) and horizontally
  by ``style.align`` (left, center or right of the safe rect).
- ``plan_captions(project, script, timeline)``: for every scene the text of its
  script section is cut into phrases (sentence ends, then commas, then words
  when a clause does not fit the wrap limits) and timed inside the scene by word
  weight, in whole milliseconds that add up to the scene exactly (no forced
  alignment: the voice is not analysed). Phrases that would be shown for less
  than ``min_display_ms`` are merged with a neighbour and phrases over
  ``max_display_ms`` are split; when that cannot be done a ``ValueError`` says
  why. The font size is the default size, made smaller when the longest line
  would not fit the safe width. Only a timeline with one scene per section can
  be captioned.
"""

import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum

from ai_youtube_agent.content.script import Script
from ai_youtube_agent.content.shorts_config import CaptionRules, Rect, ShortsConfig
from ai_youtube_agent.content.shorts_project import ShortsProject
from ai_youtube_agent.content.shorts_timeline import (
    MIN_FONT_SIZE,
    AssetRole,
    CaptionAlign,
    CaptionAnchor,
    CaptionOverlay,
    CaptionPosition,
    CaptionStyle,
    Timeline,
)

MAX_CHARS_PER_SECOND = 20
DEFAULT_FONT_SIZE = CaptionStyle().size_px
MAX_ROUNDS = 1000
PERMILLE = 1000
_SENTENCE_ENDS = ".!?…"
_CLAUSE_ENDS = ",;:"


class IssueKind(StrEnum):
    INVALID = "invalid"
    UNSAFE = "unsafe"
    FAST = "fast"


@dataclass(frozen=True)
class CaptionIssue:
    """One finding: ``target`` is a caption id, a scene asset or ``None``."""

    kind: IssueKind
    target: str | None
    message: str


@dataclass(frozen=True)
class CaptionCheck:
    errors: tuple[CaptionIssue, ...] = ()
    warnings: tuple[CaptionIssue, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.errors

    def errors_of(self, kind: IssueKind) -> tuple[CaptionIssue, ...]:
        return tuple(issue for issue in self.errors if issue.kind is kind)


@dataclass(frozen=True)
class PixelBox:
    """The estimated box of a caption; it may stick out of the canvas."""

    x: int
    y: int
    width: int
    height: int

    def inside(self, rect: Rect) -> bool:
        return (
            rect.x <= self.x
            and rect.y <= self.y
            and self.x + self.width <= rect.right
            and self.y + self.height <= rect.bottom
        )


# Wrapping


def _words(text: str) -> list[str]:
    return unicodedata.normalize("NFC", text).split()


def _lines(words: Sequence[str], width: int) -> list[str]:
    """Greedy lines of at most ``width`` characters; ValueError for a long word."""
    lines: list[str] = []
    current = ""
    for word in words:
        if len(word) > width:
            raise ValueError(f"the word {word!r} is longer than {width} characters")
        if not current:
            current = word
        elif len(current) + 1 + len(word) <= width:
            current = f"{current} {word}"
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines


def wrap(text: str, rules: CaptionRules) -> tuple[str, ...]:
    """The lines of ``text`` under ``rules``; ValueError when it does not fit."""
    words = _words(text)
    if not words:
        raise ValueError("a caption needs text")
    lines = _lines(words, rules.max_chars_per_line)
    if len(lines) > rules.max_lines:
        raise ValueError(
            f"the text needs {len(lines)} lines, at most {rules.max_lines} of "
            f"{rules.max_chars_per_line} characters are allowed"
        )
    return tuple(lines)


def _fits(words: Sequence[str], rules: CaptionRules) -> bool:
    return len(_lines(words, rules.max_chars_per_line)) <= rules.max_lines


# Safe area


def _line_height(size_px: int) -> int:
    return (size_px * 12 + 5) // 10


def _box_width(chars: int, size_px: int) -> int:
    """``chars * size_px * 0.6`` rounded up, in whole pixels."""
    return (chars * size_px * 6 + 9) // 10


def caption_box(
    lines: Sequence[str], position: CaptionPosition, style: CaptionStyle, safe: Rect
) -> PixelBox:
    """The estimated box of a caption of ``lines`` placed in ``safe``."""
    height = len(lines) * _line_height(style.size_px)
    width = _box_width(max(len(line) for line in lines), style.size_px)
    if style.align is CaptionAlign.LEFT:
        x = safe.x
    elif style.align is CaptionAlign.RIGHT:
        x = safe.right - width
    else:
        x = safe.x + (safe.width - width) // 2
    if position.anchor is CaptionAnchor.TOP:
        y = safe.y
    elif position.anchor is CaptionAnchor.CENTER:
        y = safe.y + (safe.height - height) // 2
    else:
        y = safe.bottom - height
    return PixelBox(x, y + position.offset_px, width, height)


def _overlay_inside(box, config: ShortsConfig, safe: Rect) -> bool:
    """A permille ``Box`` against the safe rect, exactly (no rounding)."""
    return (
        box.x * config.width >= safe.x * PERMILLE
        and box.y * config.height >= safe.y * PERMILLE
        and (box.x + box.width) * config.width <= safe.right * PERMILLE
        and (box.y + box.height) * config.height <= safe.bottom * PERMILLE
    )


# Checking


def check_captions(config: ShortsConfig, timeline: Timeline) -> CaptionCheck:
    """Every caption rule that needs ``config``; errors and warnings, no raise."""
    errors: list[CaptionIssue] = []
    warnings: list[CaptionIssue] = []
    rules = config.caption_rules
    safe = config.safe_rect
    if timeline.captions and not config.captions:
        errors.append(
            CaptionIssue(
                IssueKind.INVALID,
                None,
                "the format has captions off, so none may be set",
            )
        )
        return CaptionCheck(tuple(errors), ())
    for caption in timeline.captions:
        shown = caption.duration_ms
        if not rules.min_display_ms <= shown <= rules.max_display_ms:
            errors.append(
                CaptionIssue(
                    IssueKind.INVALID,
                    caption.id,
                    f"caption {caption.id} is shown for {shown} ms, the limits are "
                    f"{rules.min_display_ms} to {rules.max_display_ms} ms",
                )
            )
        try:
            lines = wrap(caption.text, rules)
        except ValueError as error:
            errors.append(
                CaptionIssue(IssueKind.INVALID, caption.id, f"{caption.id}: {error}")
            )
            continue
        if len(caption.text) * 1000 > MAX_CHARS_PER_SECOND * shown:
            warnings.append(
                CaptionIssue(
                    IssueKind.FAST,
                    caption.id,
                    f"caption {caption.id} has {len(caption.text)} characters in "
                    f"{shown} ms, more than {MAX_CHARS_PER_SECOND} per second",
                )
            )
        box = caption_box(lines, caption.position, caption.style, safe)
        if not box.inside(safe):
            errors.append(
                CaptionIssue(
                    IssueKind.UNSAFE,
                    caption.id,
                    f"caption {caption.id} (about {box.width}x{box.height} px at "
                    f"{box.x},{box.y}) is not inside the safe area "
                    f"({safe.x},{safe.y} to {safe.right},{safe.bottom})",
                )
            )
    for scene in timeline.scenes:
        for placed in scene.assets:
            if (
                placed.role is AssetRole.OVERLAY
                and placed.box is not None
                and not _overlay_inside(placed.box, config, safe)
            ):
                errors.append(
                    CaptionIssue(
                        IssueKind.UNSAFE,
                        f"{scene.id}:{placed.asset_id}",
                        f"the overlay {placed.asset_id} of scene {scene.id} is not "
                        "inside the safe area",
                    )
                )
    return CaptionCheck(tuple(errors), tuple(warnings))


def covers_every_scene(timeline: Timeline) -> bool:
    """True when every scene shows at least one caption (each voices its text)."""
    covered = {caption.scene_id for caption in timeline.captions}
    return bool(timeline.scenes) and all(
        scene.id in covered for scene in timeline.scenes
    )


# Planning


def _shares(total: int, weights: Sequence[int]) -> list[int]:
    """``total`` shared by ``weights`` in whole ms, largest remainder first."""
    weight_sum = sum(weights)
    shares = [total * weight // weight_sum for weight in weights]
    order = sorted(
        range(len(weights)),
        key=lambda index: (-(total * weights[index] % weight_sum), index),
    )
    for index in order[: total - sum(shares)]:
        shares[index] += 1
    return shares


def _clauses(text: str) -> list[list[str]]:
    """The words of ``text`` cut after sentence ends, then after commas."""
    clauses: list[list[str]] = []
    current: list[str] = []
    for word in text.split():
        current.append(word)
        if word[-1] in _SENTENCE_ENDS or word[-1] in _CLAUSE_ENDS:
            clauses.append(current)
            current = []
    if current:
        clauses.append(current)
    return clauses


def _chunks(words: list[str], rules: CaptionRules) -> list[list[str]]:
    """``words`` cut into the longest runs that still wrap within the rules."""
    chunks: list[list[str]] = []
    current: list[str] = []
    for word in words:
        if current and not _fits([*current, word], rules):
            chunks.append(current)
            current = []
        current.append(word)
        _lines(current, rules.max_chars_per_line)  # a word that is too long
    if current:
        chunks.append(current)
    return chunks


def _split(
    phrases: list[list[str]], index: int, total: int, rules: CaptionRules
) -> list[list[str]]:
    words = phrases[index]
    cuts = sorted(range(1, len(words)), key=lambda cut: abs(cut - len(words) / 2))
    for cut in cuts:
        left, right = words[:cut], words[cut:]
        if not (_fits(left, rules) and _fits(right, rules)):
            continue
        candidate = [*phrases[:index], left, right, *phrases[index + 1 :]]
        shares = _shares(total, [len(phrase) for phrase in candidate])
        if (
            shares[index] >= rules.min_display_ms
            and shares[index + 1] >= rules.min_display_ms
        ):
            return candidate
    raise ValueError(
        f"a phrase of {len(words)} words is shown longer than "
        f"{rules.max_display_ms} ms and cannot be split into parts of at least "
        f"{rules.min_display_ms} ms"
    )


def _merge(
    phrases: list[list[str]], index: int, total: int, rules: CaptionRules
) -> list[list[str]]:
    neighbours = [
        other for other in (index + 1, index - 1) if 0 <= other < len(phrases)
    ]
    neighbours.sort(key=lambda other: len(phrases[other]))
    for other in neighbours:
        first, second = sorted((index, other))
        merged = [*phrases[first], *phrases[second]]
        if not _fits(merged, rules):
            continue
        candidate = [*phrases[:first], merged, *phrases[second + 1 :]]
        shares = _shares(total, [len(phrase) for phrase in candidate])
        if shares[first] <= rules.max_display_ms:
            return candidate
    raise ValueError(
        f"a phrase of {len(phrases[index])} words is shown for less than "
        f"{rules.min_display_ms} ms and cannot be merged with a neighbour"
    )


def _time_phrases(
    phrases: list[list[str]], total: int, rules: CaptionRules
) -> tuple[list[list[str]], list[int]]:
    """Merge and split ``phrases`` until each share is within the display limits."""
    for _ in range(MAX_ROUNDS):
        shares = _shares(total, [len(phrase) for phrase in phrases])
        for index, share in enumerate(shares):
            if share > rules.max_display_ms:
                phrases = _split(phrases, index, total, rules)
                break
            if share < rules.min_display_ms:
                phrases = _merge(phrases, index, total, rules)
                break
        else:
            return phrases, shares
    raise ValueError("the phrases cannot be timed within the display limits")


def _style_for(lines: Sequence[str], safe: Rect) -> CaptionStyle:
    """The default style, with a smaller font when the lines are too wide."""
    longest = max(len(line) for line in lines)
    size = min(DEFAULT_FONT_SIZE, safe.width * 10 // (6 * longest))
    if size < MIN_FONT_SIZE or len(lines) * _line_height(size) > safe.height:
        raise ValueError(
            f"{len(lines)} lines of {longest} characters do not fit the safe area "
            f"at a font size of {MIN_FONT_SIZE} px or more"
        )
    return CaptionStyle(size_px=size)


def plan_captions(
    project: ShortsProject, script: Script, timeline: Timeline
) -> tuple[CaptionOverlay, ...]:
    """Captions for every scene of ``timeline``, from the bound ``script``."""
    if script.id != project.script_id or script.version != project.script_version:
        raise ValueError(
            f"script {script.id} version {script.version} is not the script "
            f"version the project is bound to ({project.script_id} version "
            f"{project.script_version})"
        )
    config = project.config
    if not config.captions:
        raise ValueError("the format of this project has captions off")
    if not timeline.scenes:
        raise ValueError("plan the scenes before the captions")
    rules = config.caption_rules
    safe = config.safe_rect
    sections = script.sections
    used: set[int] = set()
    captions: list[CaptionOverlay] = []
    for scene in timeline.scenes:
        if scene.section_index >= len(sections):
            raise ValueError(f"scene {scene.id} shows a section the script lacks")
        if scene.section_index in used:
            raise ValueError(
                f"scene {scene.id} shares section {scene.section_index} with an "
                "earlier scene, captions need one scene per section"
            )
        used.add(scene.section_index)
        text = " ".join(_words(sections[scene.section_index].text))
        try:
            chunks = [
                chunk for clause in _clauses(text) for chunk in _chunks(clause, rules)
            ]
            phrases, shares = _time_phrases(chunks, scene.duration_ms, rules)
            start = scene.start_ms
            for words, share in zip(phrases, shares, strict=True):
                phrase = " ".join(words)
                style = _style_for(wrap(phrase, rules), safe)
                captions.append(
                    CaptionOverlay(
                        f"c{len(captions) + 1}",
                        scene.id,
                        phrase,
                        start,
                        start + share,
                        CaptionPosition(),
                        style,
                    )
                )
                start += share
        except ValueError as error:
            raise ValueError(f"scene {scene.id}: {error}") from None
    return tuple(captions)
