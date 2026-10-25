"""Vector-style drawing on a Pillow canvas: one outline and one shade rule.

``Pen`` maps local units to canvas pixels (``origin`` and ``k``) and draws shapes
with a clean outline of a shared colour and width, a flat fill and an optional
soft shade crescent (the fill mixed with the shared shade tone, offset toward
the lower right). The canvas is drawn at a supersample factor chosen by
``choose_supersample`` and reduced afterwards, so close-ups stay crisp and fast.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Final

from PIL import Image, ImageChops, ImageDraw

from ai_youtube_agent.animation.rig import OUTLINE_RGB, OUTLINE_WIDTH, Rgb, shade_of

Point = tuple[float, float]
MAX_CANVAS_PIXELS: Final[int] = 6_000_000
Painter = Callable[[Any, Callable[[float, float], Point], float, tuple[int, ...]], None]
RGBA = tuple[int, int, int, int]


def choose_supersample(width: int, height: int, preferred: float = 2.0) -> float:
    """The supersample factor for a ``width`` x ``height`` canvas (<= ~6 MP drawn)."""
    factor = preferred
    while factor > 1.0 and width * height * factor * factor > MAX_CANVAS_PIXELS:
        factor -= 0.25
    return max(1.0, factor)


def rgba(rgb: Sequence[int], alpha: int = 255) -> RGBA:
    return (int(rgb[0]), int(rgb[1]), int(rgb[2]), alpha)


def reduce_canvas(image: Any, factor: float, size: tuple[int, int]) -> Any:
    """Bring a supersampled canvas back to ``size`` (premultiplied by Pillow)."""
    if size == image.size:
        return image
    if factor == 2.0 and (image.width, image.height) == (size[0] * 2, size[1] * 2):
        return image.reduce(2)
    return image.resize(size, Image.Resampling.LANCZOS)


class Pen:
    """Draws shapes given in local units on ``image`` (origin at ``origin`` px)."""

    def __init__(
        self,
        image: Any,
        origin: Point,
        k: float,
        *,
        outline: Rgb = OUTLINE_RGB,
        outline_w: float = OUTLINE_WIDTH,
        shade_dir: Point = (1.0, 1.0),
    ) -> None:
        self.image = image
        self.draw = ImageDraw.Draw(image)
        self.ox, self.oy = origin
        self.k = k
        self.outline = rgba(outline)
        self.ow = max(1.0, outline_w * k)  # pixels
        self.shade_dir = shade_dir

    # --- coordinates ----------------------------------------

    def xy(self, x: float, y: float) -> Point:
        return (self.ox + x * self.k, self.oy + y * self.k)

    def px(self, value: float) -> float:
        return value * self.k

    # --- painters ----------------------------------------

    @staticmethod
    def ellipse_painter(cx: float, cy: float, rx: float, ry: float) -> Painter:
        def paint(
            draw: Any, to_px: Callable[..., Point], grow: float, fill: Any
        ) -> None:
            (x0, y0), (x1, y1) = to_px(cx - rx, cy - ry), to_px(cx + rx, cy + ry)
            draw.ellipse([x0 - grow, y0 - grow, x1 + grow, y1 + grow], fill=fill)

        return paint

    @staticmethod
    def rect_painter(
        x0: float, y0: float, x1: float, y1: float, radius: float
    ) -> Painter:
        def paint(
            draw: Any, to_px: Callable[..., Point], grow: float, fill: Any
        ) -> None:
            (a, b), (c, d) = to_px(x0, y0), to_px(x1, y1)
            r = radius * (c - a) / max(1e-6, x1 - x0)
            draw.rounded_rectangle(
                [a - grow, b - grow, c + grow, d + grow], radius=r + grow, fill=fill
            )

        return paint

    @staticmethod
    def polygon_painter(points: Sequence[Point]) -> Painter:
        def paint(
            draw: Any, to_px: Callable[..., Point], grow: float, fill: Any
        ) -> None:
            pts = [to_px(x, y) for x, y in points]
            if grow > 0:
                draw.line(
                    pts + [pts[0]],
                    fill=fill,
                    width=max(1, round(grow * 2)),
                    joint="curve",
                )
                for p in pts:
                    draw.ellipse(
                        [p[0] - grow, p[1] - grow, p[0] + grow, p[1] + grow], fill=fill
                    )
            draw.polygon(pts, fill=fill)

        return paint

    @staticmethod
    def capsule_painter(points: Sequence[Point], width: float) -> Painter:
        """A thick polyline with round caps and joints."""

        def paint(
            draw: Any, to_px: Callable[..., Point], grow: float, fill: Any
        ) -> None:
            pts = [to_px(x, y) for x, y in points]
            (ax, _), (bx, _) = to_px(0, 0), to_px(width, 0)
            w = abs(bx - ax) + 2 * grow
            draw.line(pts, fill=fill, width=max(1, round(w)), joint="curve")
            r = w / 2
            for p in pts:
                draw.ellipse([p[0] - r, p[1] - r, p[0] + r, p[1] + r], fill=fill)

        return paint

    @staticmethod
    def taper_painter(p0: Point, r0: float, p1: Point, r1: float) -> Painter:
        """Two circles joined by their outer tangents (a rounded trapezoid)."""

        def paint(
            draw: Any, to_px: Callable[..., Point], grow: float, fill: Any
        ) -> None:
            a, b = to_px(*p0), to_px(*p1)
            (ax, _), (bx, _) = to_px(0, 0), to_px(1, 0)
            unit = abs(bx - ax)
            ra, rb = r0 * unit + grow, r1 * unit + grow
            dx, dy = b[0] - a[0], b[1] - a[1]
            dist = math.hypot(dx, dy) or 1.0
            nx, ny = -dy / dist, dx / dist
            tilt = (ra - rb) / dist
            tilt = max(-0.999, min(0.999, tilt))
            ca = math.sqrt(1 - tilt * tilt)
            ux, uy = dx / dist, dy / dist
            # tangent normals tilted toward the smaller circle
            n1 = (nx * ca + ux * tilt, ny * ca + uy * tilt)
            n2 = (-nx * ca + ux * tilt, -ny * ca + uy * tilt)
            quad = [
                (a[0] + n1[0] * ra, a[1] + n1[1] * ra),
                (b[0] + n1[0] * rb, b[1] + n1[1] * rb),
                (b[0] + n2[0] * rb, b[1] + n2[1] * rb),
                (a[0] + n2[0] * ra, a[1] + n2[1] * ra),
            ]
            draw.polygon(quad, fill=fill)
            draw.ellipse([a[0] - ra, a[1] - ra, a[0] + ra, a[1] + ra], fill=fill)
            draw.ellipse([b[0] - rb, b[1] - rb, b[0] + rb, b[1] + rb], fill=fill)

        return paint

    # --- shapes ----------------------------------------

    def shape(
        self,
        painter: Painter,
        fill: Rgb | None,
        *,
        shade: float | None = None,
        bounds: tuple[float, float, float, float] | None = None,
        outline: bool = True,
        shade_color: Rgb | None = None,
    ) -> None:
        """Outline, flat fill and, when ``shade`` is an offset in px, a shade."""
        if outline:
            painter(self.draw, self.xy, self.ow, self.outline)
        if fill is not None:
            painter(self.draw, self.xy, 0.0, rgba(fill))
            if shade and bounds is not None:
                self._shade(painter, bounds, shade_color or shade_of(fill), shade)

    def _shade(
        self,
        painter: Painter,
        bounds: tuple[float, float, float, float],
        color: Rgb,
        offset: float,
    ) -> None:
        x0, y0 = self.xy(bounds[0], bounds[1])
        x1, y1 = self.xy(bounds[2], bounds[3])
        left = max(0, math.floor(min(x0, x1) - 2))
        top = max(0, math.floor(min(y0, y1) - 2))
        right = min(self.image.width, math.ceil(max(x0, x1) + 2))
        bottom = min(self.image.height, math.ceil(max(y0, y1) + 2))
        if right <= left or bottom <= top:
            return
        size = (right - left, bottom - top)
        shift_x, shift_y = offset * self.shade_dir[0], offset * self.shade_dir[1]

        def to_crop(dx: float, dy: float) -> Callable[[float, float], Point]:
            def convert(x: float, y: float) -> Point:
                return (
                    self.ox + x * self.k - left + dx,
                    self.oy + y * self.k - top + dy,
                )

            return convert

        whole = Image.new("L", size, 0)
        painter(ImageDraw.Draw(whole), to_crop(0, 0), 0.0, 255)
        moved = Image.new("L", size, 0)
        painter(ImageDraw.Draw(moved), to_crop(-shift_x, -shift_y), 0.0, 255)
        crescent = ImageChops.subtract(whole, moved)
        self.image.paste(rgba(color), (left, top), crescent)

    # --- conveniences ----------------------------------------

    def ellipse(
        self,
        cx: float,
        cy: float,
        rx: float,
        ry: float,
        fill: Rgb | None,
        *,
        shade: bool = False,
        outline: bool = True,
        shade_color: Rgb | None = None,
    ) -> None:
        offset = min(rx, ry) * self.k * 0.3 if shade else None
        self.shape(
            self.ellipse_painter(cx, cy, rx, ry),
            fill,
            shade=offset,
            bounds=(cx - rx, cy - ry, cx + rx, cy + ry),
            outline=outline,
            shade_color=shade_color,
        )

    def rounded(
        self,
        x0: float,
        y0: float,
        x1: float,
        y1: float,
        radius: float,
        fill: Rgb | None,
        *,
        shade: bool = False,
        outline: bool = True,
    ) -> None:
        offset = min(x1 - x0, y1 - y0) * self.k * 0.12 if shade else None
        self.shape(
            self.rect_painter(x0, y0, x1, y1, radius),
            fill,
            shade=offset,
            bounds=(x0, y0, x1, y1),
            outline=outline,
        )

    def polygon(
        self,
        points: Sequence[Point],
        fill: Rgb | None,
        *,
        shade: float = 0.0,
        outline: bool = True,
    ) -> None:
        xs = [p[0] for p in points]
        ys = [p[1] for p in points]
        self.shape(
            self.polygon_painter(points),
            fill,
            shade=shade * self.k if shade else None,
            bounds=(min(xs), min(ys), max(xs), max(ys)),
            outline=outline,
        )

    def limb(
        self,
        points: Sequence[Point],
        width: float,
        fill: Rgb,
        *,
        shade: bool = True,
    ) -> None:
        xs = [p[0] for p in points]
        ys = [p[1] for p in points]
        pad = width / 2
        self.shape(
            self.capsule_painter(points, width),
            fill,
            shade=width * self.k * 0.2 if shade else None,
            bounds=(min(xs) - pad, min(ys) - pad, max(xs) + pad, max(ys) + pad),
        )

    def taper(
        self,
        p0: Point,
        r0: float,
        p1: Point,
        r1: float,
        fill: Rgb,
        *,
        shade: bool = True,
    ) -> None:
        pad = max(r0, r1)
        self.shape(
            self.taper_painter(p0, r0, p1, r1),
            fill,
            shade=pad * self.k * 0.26 if shade else None,
            bounds=(
                min(p0[0], p1[0]) - pad,
                min(p0[1], p1[1]) - pad,
                max(p0[0], p1[0]) + pad,
                max(p0[1], p1[1]) + pad,
            ),
        )

    def blob(
        self,
        circles: Sequence[tuple[float, float, float]],
        fill: Rgb,
        *,
        shade: bool = True,
    ) -> None:
        """The union of circles with a single outline around it."""
        for x, y, r in circles:
            a, b = self.xy(x - r, y - r), self.xy(x + r, y + r)
            self.draw.ellipse(
                [a[0] - self.ow, a[1] - self.ow, b[0] + self.ow, b[1] + self.ow],
                fill=self.outline,
            )
        for x, y, r in circles:
            a, b = self.xy(x - r, y - r), self.xy(x + r, y + r)
            self.draw.ellipse([*a, *b], fill=rgba(fill))
        if shade:
            xs = [c[0] for c in circles]
            ys = [c[1] for c in circles]
            rmax = max(c[2] for c in circles)
            painters = [self.ellipse_painter(x, y, r, r) for x, y, r in circles]

            def union(
                draw: Any, to_px: Callable[..., Point], grow: float, color: Any
            ) -> None:
                for painter in painters:
                    painter(draw, to_px, grow, color)

            self._shade(
                union,
                (min(xs) - rmax, min(ys) - rmax, max(xs) + rmax, max(ys) + rmax),
                shade_of(fill),
                rmax * self.k * 0.2,
            )

    def frame_rect(
        self,
        x0: float,
        y0: float,
        x1: float,
        y1: float,
        radius: float,
        color: Rgb,
        width: float,
    ) -> None:
        """A rounded rectangle drawn as a ring only (nothing inside)."""
        a, b = self.xy(x0, y0), self.xy(x1, y1)
        self.draw.rounded_rectangle(
            [a[0], a[1], b[0], b[1]],
            radius=radius * self.k,
            outline=rgba(color),
            width=max(1, round(width * self.k)),
        )

    def stroke(
        self,
        points: Sequence[Point],
        width: float,
        color: Rgb,
        *,
        round_caps: bool = True,
    ) -> None:
        """A coloured line of ``width`` local units (no outline)."""
        pts = [self.xy(x, y) for x, y in points]
        w = max(1, round(width * self.k))
        self.draw.line(pts, fill=rgba(color), width=w, joint="curve")
        if round_caps:
            r = w / 2
            for p in (pts[0], pts[-1]):
                self.draw.ellipse(
                    [p[0] - r, p[1] - r, p[0] + r, p[1] + r], fill=rgba(color)
                )

    def arc_points(
        self,
        cx: float,
        cy: float,
        rx: float,
        ry: float,
        start_deg: float,
        end_deg: float,
        steps: int = 14,
    ) -> list[Point]:
        return [
            (
                cx
                + rx
                * math.cos(math.radians(start_deg + (end_deg - start_deg) * i / steps)),
                cy
                + ry
                * math.sin(math.radians(start_deg + (end_deg - start_deg) * i / steps)),
            )
            for i in range(steps + 1)
        ]


@dataclass(frozen=True)
class CanvasPlan:
    """A canvas covering part of the frame: where it lands and how it is drawn."""

    left: int
    top: int
    width: int
    height: int
    ss: float
    origin: Point  # canvas pixels (supersampled) of the local origin
    k: float  # supersampled pixels per local unit

    @property
    def canvas_size(self) -> tuple[int, int]:
        return (round(self.width * self.ss), round(self.height * self.ss))


def plan_canvas(
    extent: tuple[float, float, float, float],
    anchor: Point,
    k: float,
    viewport: tuple[int, int],
    *,
    preferred_ss: float = 2.0,
    margin_px: float = 6.0,
) -> CanvasPlan | None:
    """Plan a canvas for ``extent`` (local x0, y0, x1, y1) drawn at ``anchor`` px.

    ``k`` is screen pixels per local unit. The canvas is clipped to the
    viewport, so a close-up never draws what is out of view; ``None`` when
    nothing of the extent is visible.
    """
    x0 = anchor[0] + extent[0] * k - margin_px
    y0 = anchor[1] + extent[1] * k - margin_px
    x1 = anchor[0] + extent[2] * k + margin_px
    y1 = anchor[1] + extent[3] * k + margin_px
    left = max(0, math.floor(x0))
    top = max(0, math.floor(y0))
    right = min(viewport[0], math.ceil(x1))
    bottom = min(viewport[1], math.ceil(y1))
    if right <= left or bottom <= top:
        return None
    width, height = right - left, bottom - top
    ss = choose_supersample(width, height, preferred_ss)
    return CanvasPlan(
        left,
        top,
        width,
        height,
        ss,
        ((anchor[0] - left) * ss, (anchor[1] - top) * ss),
        k * ss,
    )


def new_canvas(plan: CanvasPlan) -> Any:
    return Image.new("RGBA", plan.canvas_size, (0, 0, 0, 0))


def finish_canvas(image: Any, plan: CanvasPlan) -> Any:
    return reduce_canvas(image, plan.ss, (plan.width, plan.height))


def composite_at(target: Any, sprite: Any, x: int, y: int) -> None:
    """Alpha-composite ``sprite`` onto the RGBA ``target`` at ``(x, y)``, clipped."""
    left, top = max(0, x), max(0, y)
    right = min(target.width, x + sprite.width)
    bottom = min(target.height, y + sprite.height)
    if right <= left or bottom <= top:
        return
    if (left, top, right, bottom) != (x, y, x + sprite.width, y + sprite.height):
        sprite = sprite.crop((left - x, top - y, right - x, bottom - y))
    target.alpha_composite(sprite, (left, top))
