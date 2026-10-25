"""Props drawn as vectors at the exact scale of the frame (never upscaled).

Every ``draw_*`` takes a ``Pen`` whose local origin is the prop's anchor (the
base centre of standing props, the hook of hanging ones) and draws with the
shared outline and shade rules. ``render_prop`` makes a clipped, supersampled
sprite of one prop for the renderer; the characters draw held props straight
into their own canvas.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Final

from PIL import ImageFont

from ai_youtube_agent.animation.actions import (
    BOWL_H as BOWL_H_STAGE,
)
from ai_youtube_agent.animation.actions import (
    POT_H as POT_H_STAGE,
)
from ai_youtube_agent.animation.actions import (
    PROP_SCALE,
)
from ai_youtube_agent.animation.actions import (
    STOOL_H as STOOL_H_STAGE,
)
from ai_youtube_agent.animation.actions import (
    SURFACE_HALF_W as SURFACE_HALF_W_STAGE,
)
from ai_youtube_agent.animation.actions import (
    SURFACE_TOP as SURFACE_TOP_STAGE,
)
from ai_youtube_agent.animation.draw import (
    Pen,
    composite_at,
    finish_canvas,
    new_canvas,
    plan_canvas,
    rgba,
)
from ai_youtube_agent.animation.rig import OUTLINE_RGB, Rgb, shade_of

Point = tuple[float, float]

# The props are drawn in local units and scaled by PROP_SCALE when placed.
POT_H: Final[float] = POT_H_STAGE / PROP_SCALE
BOWL_H: Final[float] = BOWL_H_STAGE / PROP_SCALE
STOOL_H: Final[float] = STOOL_H_STAGE / PROP_SCALE
SURFACE_TOP: Final[dict[str, float]] = {
    kind: value / PROP_SCALE for kind, value in SURFACE_TOP_STAGE.items()
}
SURFACE_HALF_W: Final[dict[str, float]] = {
    kind: value / PROP_SCALE for kind, value in SURFACE_HALF_W_STAGE.items()
}

STEEL: Final[Rgb] = (172, 184, 198)
STEEL_LIGHT: Final[Rgb] = (206, 214, 224)
STEEL_DARK: Final[Rgb] = (96, 106, 122)
WOOD: Final[Rgb] = (176, 110, 58)
WOOD_LIGHT: Final[Rgb] = (222, 168, 104)
WOOD_DARK: Final[Rgb] = (128, 78, 42)
CREAM: Final[Rgb] = (252, 240, 214)
CERAMIC: Final[Rgb] = (251, 247, 238)
BLUE: Final[Rgb] = (64, 112, 182)
BROTH: Final[Rgb] = (232, 170, 70)
BROTH_DARK: Final[Rgb] = (198, 128, 48)
NOODLE: Final[Rgb] = (250, 234, 196)
BEEF: Final[Rgb] = (150, 76, 48)
HERB: Final[Rgb] = (84, 168, 80)
RED: Final[Rgb] = (214, 70, 60)
GOLD: Final[Rgb] = (236, 182, 76)
TEAL: Final[Rgb] = (52, 128, 118)
STOVE: Final[Rgb] = (232, 218, 188)
FLAME_A: Final[Rgb] = (255, 150, 46)
FLAME_B: Final[Rgb] = (255, 214, 96)

# Local extents (x0, y0, x1, y1) of each prop around its anchor.
EXTENTS: Final[dict[str, tuple[float, float, float, float]]] = {
    "pot": (-125, -POT_H - 40, 125, 14),
    "stove": (-170, -290, 170, 12),
    "counter": (-260, -300, 260, 12),
    "table": (-175, -285, 175, 12),
    "stool": (-72, -135, 72, 14),
    "bowl_pho": (-96, -BOWL_H - 40, 96, 14),
    "ladle": (-70, -100, 70, 150),
    "hanging_ladle": (-70, -50, 70, 190),
    "spoon": (-40, -110, 40, 90),
    "tongs": (-40, -110, 40, 90),
    "lantern": (-64, -20, 64, 190),
    "sign": (-250, -20, 250, 190),
}


@dataclass(frozen=True)
class PropLook:
    """Per-frame variation of a prop."""

    broth: float = 0.0
    beef: float = 0.0
    herbs: float = 0.0
    scoop: float = 0.0
    t_s: float = 0.0
    glow: float = 0.0  # stove flame or lantern light, 0..1
    sway: float = 0.0  # degrees
    flip: float = 1.0  # facing of a held ladle (+1 right, -1 left)
    text: str = ""
    font_file: str = ""
    bubbling: bool = False


# ---------------------------------------------------------------------------
# Kitchen
# ---------------------------------------------------------------------------


def draw_pot(pen: Pen, look: PropLook) -> None:
    half = 82
    pen.rounded(-half, -POT_H, half, 0, 16, STEEL, shade=True)
    pen.polygon(
        [(-58, -POT_H + 22), (-46, -POT_H + 22), (-46, -26), (-58, -26)],
        STEEL_LIGHT,
        outline=False,
    )
    pen.rounded(-112, -92, -76, -70, 8, STEEL_DARK, shade=True)
    pen.rounded(76, -92, 112, -70, 8, STEEL_DARK, shade=True)
    pen.ellipse(0, -POT_H, 92, 21, STEEL_LIGHT, shade=True)
    pen.ellipse(0, -POT_H + 2, 78, 14, BROTH, shade=False)
    pen.ellipse(8, -POT_H + 4, 52, 8, BROTH_DARK, outline=False)
    t = look.t_s
    count = 8 if look.bubbling else 4
    for i in range(count):
        phase = (t * (0.55 + 0.11 * i) + i * 0.37) % 1.0
        bx = -56 + (i * 41) % 112
        grow = math.sin(math.pi * phase)
        r = 2.5 + 5 * grow
        by = -POT_H + 3 + ((i * 5) % 7 - 3)
        pen.ellipse(bx, by - 2 * grow, r, r * 0.6, (255, 222, 150), outline=False)
        pen.ellipse(
            bx - r * 0.3,
            by - 2 * grow - r * 0.2,
            r * 0.3,
            r * 0.2,
            (255, 246, 214),
            outline=False,
        )


def draw_stove(pen: Pen, look: PropLook) -> None:
    top = SURFACE_TOP["stove"]
    half = SURFACE_HALF_W["stove"]
    pen.rounded(-half + 6, -top, half - 6, 0, 14, STOVE, shade=True)
    pen.rounded(-half, -top - 10, half, -top + 16, 8, (92, 82, 78), shade=True)
    for x in (-100, -34, 34, 100):
        pen.ellipse(x, -top + 44, 12, 12, (96, 86, 82))
        pen.ellipse(x - 3, -top + 41, 3.5, 3.5, (236, 226, 212), outline=False)
    pen.rounded(-116, -top + 78, 116, -34, 12, (122, 90, 70), shade=True)
    pen.rounded(-84, -top + 92, 84, -top + 104, 5, STOVE)
    pen.rounded(-98, -top + 118, 98, -52, 8, (150, 112, 88), outline=False)
    if look.glow > 0.0:
        for i, x in enumerate((-134, -120, 120, 134)):
            flick = 0.5 + 0.5 * math.sin(look.t_s * (9 + i * 2.3) + i * 1.7)
            h = (14 + 16 * flick) * (0.5 + 0.5 * look.glow)
            pen.polygon(
                [(x - 8, -top - 8), (x, -top - 10 - h), (x + 8, -top - 8)],
                FLAME_A,
            )
            pen.polygon(
                [(x - 4, -top - 8), (x, -top - 8 - h * 0.62), (x + 4, -top - 8)],
                FLAME_B,
                outline=False,
            )


def _leaf(pen: Pen, x: float, y: float, rx: float, ry: float, color: Rgb) -> None:
    pen.ellipse(x, y, rx, ry, color, outline=False)


def draw_bowl(pen: Pen, look: PropLook) -> None:
    pen.rounded(-30, -12, 30, 0, 5, CERAMIC, shade=True)
    steps = 16
    body = [(-76 + 152 * i / steps, -BOWL_H) for i in range(steps + 1)]
    curve = [
        (
            76 * math.cos(math.pi * i / steps),
            -BOWL_H + 52 * math.sin(math.pi * i / steps),
        )
        for i in range(steps + 1)
    ]
    pen.polygon(body[:1] + curve + body[::-1][:1], CERAMIC, shade=BOWL_H * 0.2)
    ring = [(x, -BOWL_H + 17 + 5 * (1 - (x / 72) ** 2)) for x in range(-66, 67, 6)]
    pen.stroke(ring, 6.5, BLUE)
    for x in (-40, -14, 14, 40):
        pen.ellipse(
            x, -BOWL_H + 31 + 4 * (1 - (x / 70) ** 2), 4, 3.2, BLUE, outline=False
        )
    pen.ellipse(0, -BOWL_H, 76, 16, CERAMIC)
    pen.ellipse(0, -BOWL_H + 1, 68, 11.5, NOODLE, outline=False)
    if look.broth > 0.02:
        pen.ellipse(
            0,
            -BOWL_H + 1,
            68 * (0.35 + 0.65 * look.broth),
            11.5 * (0.35 + 0.65 * look.broth),
            BROTH,
            outline=False,
        )
        pen.ellipse(
            6, -BOWL_H + 3, 40 * look.broth, 5 * look.broth, BROTH_DARK, outline=False
        )
    if look.beef > 0.02:
        drop = (1 - look.beef) * -16
        for x, y in ((-26, 0), (4, 3), (30, -1)):
            pen.ellipse(x, -BOWL_H + y + drop, 14 * look.beef, 5.2 * look.beef, BEEF)
            pen.ellipse(
                x - 2,
                -BOWL_H + y - 1 + drop,
                7 * look.beef,
                1.6 * look.beef,
                (196, 120, 90),
                outline=False,
            )
    if look.herbs > 0.02:
        drop = (1 - look.herbs) * -20
        for i, (x, y) in enumerate(
            ((-38, 4), (-12, -4), (16, 5), (40, 1), (-2, 6), (24, -3))
        ):
            _leaf(
                pen,
                x,
                -BOWL_H + y + drop,
                7 * look.herbs,
                3.6 * look.herbs,
                HERB if i % 2 else (126, 198, 98),
            )
        for x in (-20, 8, 34):
            pen.stroke(
                [(x, -BOWL_H + drop + 1), (x + 12, -BOWL_H - 1 + drop)],
                2.8 * look.herbs,
                (198, 232, 150),
                round_caps=True,
            )


def draw_ladle(pen: Pen, look: PropLook, *, hung: bool = False) -> None:
    """A ladle whose origin is the grip; the cup is 96 units below along the axis."""
    flip = look.flip
    handle_top = -68.0
    cup = (0.0, 96.0)
    if hung:
        pen.rounded(-30, -100, 30, -88, 4, WOOD_DARK, shade=True)
        pen.ellipse(0, -84, 8, 8, STEEL_DARK, outline=True)
        pen.stroke([(0, -80), (0, handle_top + 6)], 4.0, STEEL_DARK)
    pen.limb([(0, handle_top), (0, 62)], 11, WOOD, shade=True)
    pen.ellipse(0, handle_top, 10, 10, WOOD_DARK)
    pen.ellipse(cup[0], cup[1], 29, 28, STEEL, shade=True)
    inner_x = -flip * 10
    pen.ellipse(cup[0] + inner_x, cup[1] + 1, 16, 22, STEEL_DARK, outline=True)
    if look.scoop > 0.05:
        fill = min(1.0, look.scoop + 0.2)
        pen.ellipse(
            cup[0] + inner_x, cup[1] + 3, 13 * fill, 18 * fill, BROTH, outline=False
        )


def draw_spoon(pen: Pen, look: PropLook) -> None:
    pen.limb([(0, -70), (0, 36)], 9, WOOD_LIGHT, shade=True)
    pen.ellipse(0, 54, 17, 23, STEEL_LIGHT, shade=True)
    if look.scoop > 0.05:
        pen.ellipse(0, 55, 12, 17, BROTH, outline=False)


def draw_tongs(pen: Pen, look: PropLook, *, with_beef: bool) -> None:
    pen.limb([(-6, -64), (-2, 60)], 6, WOOD_DARK, shade=False)
    pen.limb([(7, -64), (3, 60)], 6, WOOD_DARK, shade=False)
    if with_beef:
        pen.ellipse(0, 66, 24, 8, BEEF)
        pen.ellipse(-3, 64, 11, 2.4, (196, 120, 90), outline=False)


def draw_herb_pinch(pen: Pen, look: PropLook) -> None:
    for x, y, c in (
        (-9, 4, HERB),
        (5, -2, (126, 198, 98)),
        (12, 9, HERB),
        (-2, 12, (198, 232, 150)),
    ):
        pen.ellipse(x, y, 9, 4.6, c, outline=True)


# ---------------------------------------------------------------------------
# Furniture
# ---------------------------------------------------------------------------


def draw_counter(pen: Pen, look: PropLook) -> None:
    top = SURFACE_TOP["counter"]
    half = SURFACE_HALF_W["counter"]
    pen.rounded(-half + 8, -top + 16, half - 8, 0, 8, WOOD, shade=True)
    for x in range(-int(half) + 70, int(half) - 40, 66):
        pen.stroke([(x, -top + 44), (x, -10)], 3, WOOD_DARK, round_caps=False)
    pen.frame_rect(-half + 30, -top + 56, half - 30, -26, 10, WOOD_DARK, 3.0)
    pen.polygon(
        [
            (-half - 4, -top + 4),
            (-half + 14, -top - 14),
            (half - 14, -top - 14),
            (half + 4, -top + 4),
        ],
        WOOD_LIGHT,
        shade=6,
    )
    pen.rounded(-half - 4, -top + 4, half + 4, -top + 26, 8, WOOD_LIGHT, shade=True)
    pen.rounded(-half + 30, -top + 28, half - 30, -top + 38, 4, TEAL, outline=False)


def draw_table(pen: Pen, look: PropLook) -> None:
    top = SURFACE_TOP["table"]
    half = SURFACE_HALF_W["table"]
    for x in (-half + 22, half - 22):
        pen.rounded(x - 11, -top + 26, x + 11, 0, 5, WOOD_DARK, shade=True)
    pen.rounded(-half + 12, -top + 14, half - 12, -top + 34, 6, WOOD, shade=True)
    pen.polygon(
        [
            (-half + 8, -top + 12),
            (-half + 30, -top - 16),
            (half - 30, -top - 16),
            (half - 8, -top + 12),
        ],
        WOOD_LIGHT,
        shade=5,
    )
    pen.rounded(-half, -top + 4, half, -top + 24, 8, WOOD_LIGHT, shade=True)
    pen.rounded(-half + 36, -top + 9, half - 36, -top + 14, 2, WOOD, outline=False)


def draw_stool(pen: Pen, look: PropLook) -> None:
    pen.limb([(-38, -STOOL_H + 20), (-50, -4)], 11, RED, shade=True)
    pen.limb([(38, -STOOL_H + 20), (50, -4)], 11, RED, shade=True)
    pen.limb([(0, -STOOL_H + 16), (0, -4)], 10, (190, 56, 48), shade=False)
    pen.rounded(-52, -STOOL_H + 2, 52, -STOOL_H + 26, 8, RED, shade=True)
    pen.ellipse(0, -STOOL_H, 56, 14, (234, 92, 78), shade=True)


# ---------------------------------------------------------------------------
# Hanging
# ---------------------------------------------------------------------------


def draw_lantern(pen: Pen, look: PropLook) -> None:
    pen.stroke([(0, 0), (0, 38)], 3.5, OUTLINE_RGB)
    lit = look.glow
    body = (
        round(214 + 36 * lit),
        round(70 + 70 * lit),
        round(60 + 30 * lit),
    )
    pen.rounded(-20, 36, 20, 52, 5, GOLD, shade=True)
    pen.ellipse(0, 108, 50, 58, body, shade=True)
    for dy in (-30, -12, 8, 28):
        half = 50 * math.sqrt(max(0.0, 1 - (dy / 58) ** 2))
        pen.stroke(
            [(-half * 0.96, 108 + dy), (0, 108 + dy + 7), (half * 0.96, 108 + dy)],
            2.6,
            shade_of(body, 0.45),
        )
    pen.ellipse(-16, 86, 11, 17, (255, 200, 170), outline=False) if lit > 0.3 else None
    pen.rounded(-20, 160, 20, 176, 5, GOLD, shade=True)
    pen.stroke([(0, 176), (0, 204)], 3.5, RED)
    pen.ellipse(0, 210, 5.5, 8, RED)


@lru_cache(maxsize=8)
def _sign_font(font_file: str, px: int) -> Any:
    return ImageFont.truetype(font_file, px)


def draw_sign(pen: Pen, look: PropLook) -> None:
    half_w, top, bottom = 196.0, 40.0, 140.0
    pen.stroke(
        [(-half_w + 30, 0), (-half_w + 30, top + 4)], 4, OUTLINE_RGB, round_caps=False
    )
    pen.stroke(
        [(half_w - 30, 0), (half_w - 30, top + 4)], 4, OUTLINE_RGB, round_caps=False
    )
    pen.rounded(-half_w, top, half_w, bottom, 16, WOOD_DARK, shade=True)
    pen.rounded(
        -half_w + 12, top + 12, half_w - 12, bottom - 12, 8, WOOD, outline=False
    )
    if look.text and look.font_file:
        font = _sign_font(look.font_file, max(8, round(62 * pen.k)))
        x, y = pen.xy(0, (top + bottom) / 2 + 2)
        pen.draw.text(
            (x, y),
            look.text,
            font=font,
            fill=rgba(CREAM),
            anchor="mm",
            stroke_width=max(1, round(3.2 * pen.k)),
            stroke_fill=rgba(OUTLINE_RGB),
        )
    pen.ellipse(-half_w + 30, 0, 6, 6, STEEL_DARK)
    pen.ellipse(half_w - 30, 0, 6, 6, STEEL_DARK)


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------


def draw_prop(pen: Pen, kind: str, look: PropLook | None = None) -> None:
    """Draw ``kind`` at the pen's origin."""
    look = look or PropLook()
    if kind == "pot":
        draw_pot(pen, look)
    elif kind == "stove":
        draw_stove(pen, look)
    elif kind == "bowl_pho":
        draw_bowl(pen, look)
    elif kind == "ladle":
        draw_ladle(pen, look)
    elif kind == "hanging_ladle":
        pen_origin = pen.oy
        pen.oy = pen_origin + 84 * pen.k
        draw_ladle(pen, look, hung=True)
        pen.oy = pen_origin
    elif kind == "spoon":
        draw_spoon(pen, look)
    elif kind == "tongs":
        draw_tongs(pen, look, with_beef=False)
    elif kind == "tongs_beef":
        draw_tongs(pen, look, with_beef=True)
    elif kind == "herb_pinch":
        draw_herb_pinch(pen, look)
    elif kind == "counter":
        draw_counter(pen, look)
    elif kind == "table":
        draw_table(pen, look)
    elif kind == "stool":
        draw_stool(pen, look)
    elif kind == "lantern":
        draw_lantern(pen, look)
    elif kind == "sign":
        draw_sign(pen, look)
    else:
        raise KeyError(f"unknown prop kind '{kind}'")


def prop_extent(kind: str) -> tuple[float, float, float, float]:
    if kind == "tongs_beef":
        return EXTENTS["tongs"]
    if kind == "herb_pinch":
        return (-30, -30, 30, 30)
    return EXTENTS[kind]


def rotated_extent(
    extent: tuple[float, float, float, float], angle: float
) -> tuple[float, float, float, float]:
    """The extent of a prop turned by ``angle`` degrees about its anchor."""
    if not angle:
        return extent
    a = math.radians(angle)
    c, s = math.cos(a), math.sin(a)
    corners = [
        (x * c - y * s, x * s + y * c)
        for x in (extent[0], extent[2])
        for y in (extent[1], extent[3])
    ]
    return (
        min(p[0] for p in corners),
        min(p[1] for p in corners),
        max(p[0] for p in corners),
        max(p[1] for p in corners),
    )


def blit_prop(
    target: Any,
    kind: str,
    anchor: Point,
    k: float,
    look: PropLook | None = None,
    *,
    angle: float = 0.0,
) -> None:
    """Draw ``kind`` onto the RGBA ``target`` at ``anchor`` (target pixels).

    ``k`` is target pixels per stage pixel. A rotated prop is drawn on a small
    canvas at the same scale, turned about its anchor and composited, so it is
    never an upscaled raster.
    """
    k = k * PROP_SCALE
    if not angle:
        draw_prop(Pen(target, anchor, k), kind, look)
        return
    from PIL import Image

    x0, y0, x1, y1 = prop_extent(kind)
    pad = 4
    width = math.ceil((x1 - x0) * k) + 2 * pad
    height = math.ceil((y1 - y0) * k) + 2 * pad
    local_origin = (pad - x0 * k, pad - y0 * k)
    local = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw_prop(Pen(local, local_origin, k), kind, look)
    # rotate about the anchor on a canvas big enough for any angle
    radius = (
        math.ceil(math.hypot(max(abs(x0), abs(x1)), max(abs(y0), abs(y1))) * k) + pad
    )
    big = Image.new("RGBA", (2 * radius, 2 * radius), (0, 0, 0, 0))
    big.paste(local, (round(radius - local_origin[0]), round(radius - local_origin[1])))
    turned = big.rotate(
        -angle, resample=Image.Resampling.BICUBIC, center=(radius, radius)
    )
    composite_at(target, turned, round(anchor[0] - radius), round(anchor[1] - radius))


def render_prop(
    kind: str,
    anchor: Point,
    k: float,
    viewport: tuple[int, int],
    look: PropLook | None = None,
    *,
    angle: float = 0.0,
    preferred_ss: float = 2.0,
) -> tuple[Any, int, int] | None:
    """A clipped sprite of ``kind`` for ``anchor`` (screen px): ``(image, left, top)``.

    ``k`` is screen pixels per stage pixel. A rotated prop turns about its
    anchor. ``None`` when it is out of view.
    """
    extent = rotated_extent(prop_extent(kind), angle)
    plan = plan_canvas(
        extent, anchor, k * PROP_SCALE, viewport, preferred_ss=preferred_ss
    )
    if plan is None:
        return None
    image = new_canvas(plan)
    blit_prop(image, kind, plan.origin, plan.k / PROP_SCALE, look, angle=angle)
    return finish_canvas(image, plan), plan.left, plan.top
