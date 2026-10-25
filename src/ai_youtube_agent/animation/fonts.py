"""Fonts: a TrueType font with real Vietnamese glyphs, or a blocked step."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Final

from ai_youtube_agent.animation.pipeline_steps import StepBlocked

WINDOWS_FONTS: Final[Path] = Path("C:/Windows/Fonts")
# (regular, bold) candidates; both have Vietnamese glyphs.
FONT_CANDIDATES: Final[tuple[tuple[str, str], ...]] = (
    ("arial.ttf", "arialbd.ttf"),
    ("segoeui.ttf", "segoeuib.ttf"),
)
VIETNAMESE_PROBE: Final[str] = "ăâđêôơưạảấầệợữựỹ"


@dataclass(frozen=True)
class FontFiles:
    regular: Path
    bold: Path


def font_supports_vietnamese(path: Path) -> bool:
    """True when the font has real glyphs (not .notdef) for Vietnamese letters."""
    from PIL import ImageFont

    font = ImageFont.truetype(str(path), 32)
    missing = font.getmask("\u0378")  # unassigned code point: always .notdef
    missing_key = (missing.size, bytes(missing))
    for char in VIETNAMESE_PROBE:
        mask = font.getmask(char)
        if (mask.size, bytes(mask)) == missing_key:
            return False
    return True


def resolve_font_files(
    configured: Path | None, *, fonts_dir: Path = WINDOWS_FONTS
) -> FontFiles:
    """The configured font, else a Windows font; ``StepBlocked`` when none fits.

    ``configured`` is ``Settings.render_font_file``
    (``AI_YOUTUBE_AGENT_RENDER_FONT_FILE``). The Pillow default bitmap font is
    never used: it has no Vietnamese glyphs.
    """
    if configured is not None:
        path = Path(configured)
        if not path.is_file():
            raise StepBlocked(f"the configured font was not found: {path}")
        candidates = [(path, path)]
    else:
        candidates = [
            (
                fonts_dir / regular,
                fonts_dir / bold
                if (fonts_dir / bold).is_file()
                else fonts_dir / regular,
            )
            for regular, bold in FONT_CANDIDATES
            if (fonts_dir / regular).is_file()
        ]
    for regular, bold in candidates:
        try:
            if font_supports_vietnamese(regular) and font_supports_vietnamese(bold):
                return FontFiles(regular, bold)
        except OSError:
            continue
    raise StepBlocked(
        "no TrueType font with Vietnamese glyphs; set AI_YOUTUBE_AGENT_RENDER_FONT_FILE"
    )
