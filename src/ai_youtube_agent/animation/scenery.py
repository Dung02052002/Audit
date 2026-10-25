"""Scenery: the AI plate in the camera, a drawn floor, light, steam and passers-by.

The ComfyUI plate is only the far background: it is cropped with a parallax
zoom, softened with a depth-of-field blur that grows with the camera zoom and
washed toward a warm haze so the characters stay the hero. The floor, counters
and furniture are drawn by code at the exact scale of the frame, tinted from the
plate's own bottom colour, with contact shadows. Particles are pure functions of
time and a seed, so every frame depends only on its index.
"""

from __future__ import annotations

import math
import random
from collections.abc import Sequence
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Final

from PIL import Image, ImageDraw, ImageFilter

from ai_youtube_agent.animation.camera import (
    PARALLAX_FACTOR,
    STAGE_H,
    STAGE_W,
    CameraState,
    background_blur_px,
    parallax_zoom,
)
from ai_youtube_agent.animation.easing import clamp, lerp, smoothstep
from ai_youtube_agent.animation.rig import Pose, Rgb, solve_skeleton, walk_cycle

Point = tuple[float, float]
GROUND_TOP: Final[float] = 1452.0  # stage y where the drawn floor starts
SKIRTING_H: Final[float] = 22.0
CREAM_HAZE: Final[Rgb] = (255, 244, 224)

# --- colour grade ----------------------------------------
# (gain r, g, b), (lift r, g, b), saturation, contrast
GRADES: Final[
    dict[
        str, tuple[tuple[float, float, float], tuple[float, float, float], float, float]
    ]
] = {
    "dawn": ((0.97, 0.98, 1.04), (6.0, 3.0, 12.0), 0.92, 1.0),
    "morning": ((1.02, 1.0, 0.97), (3.0, 2.0, 0.0), 1.02, 1.03),
    "warm": ((1.06, 1.0, 0.9), (8.0, 3.0, -4.0), 1.06, 1.04),
}


@lru_cache(maxsize=16)
def grade_lut(grade: str) -> tuple[int, ...]:
    gain, lift, _saturation, contrast = GRADES.get(grade, GRADES["morning"])
    lut: list[int] = []
    for channel in range(3):
        for value in range(256):
            v = (value - 128) * contrast + 128
            v = v * gain[channel] + lift[channel]
            lut.append(round(clamp(v, 0, 255)))
    return tuple(lut)


def apply_grade(image: Any, grade: str, amount: float = 1.0) -> Any:
    """The shared colour grade of a scene (a LUT plus saturation)."""
    graded = image.point(list(grade_lut(grade)))
    saturation = GRADES.get(grade, GRADES["morning"])[2]
    if abs(saturation - 1.0) > 0.005:
        from PIL import ImageEnhance

        graded = ImageEnhance.Color(graded).enhance(saturation)
    return graded if amount >= 1.0 else Image.blend(image, graded, amount)


@lru_cache(maxsize=4)
def vignette(size: tuple[int, int], strength: float = 0.16) -> Any:
    """A multiply layer, bright in the middle, a little dark at the edges."""
    w, h = size
    small_w, small_h = max(8, w // 8), max(8, h // 8)
    mask = Image.new("L", (small_w, small_h))
    pixels = mask.load()
    for y in range(small_h):
        for x in range(small_w):
            dx, dy = (x + 0.5) / small_w * 2 - 1, (y + 0.5) / small_h * 2 - 1
            d = min(1.0, math.hypot(dx * 0.92, dy * 0.8))
            falloff = smoothstep(0.55, 1.0, d)
            pixels[x, y] = round(255 * (1 - strength * falloff))
    mask = mask.resize(size, Image.Resampling.BICUBIC)
    return Image.merge("RGB", (mask, mask, mask))


@lru_cache(maxsize=8)
def grain_tiles(
    size: tuple[int, int], count: int = 4, amplitude: int = 5
) -> tuple[Any, ...]:
    """A few seeded noise images (around 128) for a light film grain."""
    rng = random.Random(20261023)
    tiles = []
    for _ in range(count):
        base = Image.new("L", (size[0] // 2, size[1] // 2))
        base.putdata(
            [
                128 + rng.randint(-amplitude, amplitude)
                for _ in range(base.width * base.height)
            ]
        )
        tile = base.resize(size, Image.Resampling.NEAREST)
        tiles.append(Image.merge("RGB", (tile, tile, tile)))
    return tuple(tiles)


# --- plates ----------------------------------------


@dataclass(frozen=True)
class PlateInfo:
    """A prepared plate: the washed image and the colours sampled from it."""

    image: Any  # RGB, 1080x1920
    floor_rgb: Rgb
    wall_rgb: Rgb


def prepare_plate(plate: Any, *, haze: float = 0.12, soften: float = 3.0) -> PlateInfo:
    """Make a plate sit behind the characters: soft, slightly hazy, one palette."""
    image = plate.convert("RGB")
    if image.size != (STAGE_W, STAGE_H):
        image = image.resize((STAGE_W, STAGE_H), Image.Resampling.LANCZOS)
    # a median at half size erases thin strokes (the plates' garbled lettering)
    half = image.resize((STAGE_W // 2, STAGE_H // 2), Image.Resampling.BOX)
    half = half.filter(ImageFilter.MedianFilter(5))
    image = half.resize((STAGE_W, STAGE_H), Image.Resampling.BICUBIC)
    floor = image.crop(
        (
            int(STAGE_W * 0.1),
            int(STAGE_H * 0.9),
            int(STAGE_W * 0.9),
            int(STAGE_H * 0.985),
        )
    )
    wall = image.crop(
        (int(STAGE_W * 0.2), int(STAGE_H * 0.2), int(STAGE_W * 0.8), int(STAGE_H * 0.5))
    )
    floor_rgb = _average(floor)
    wall_rgb = _average(wall)
    image = image.filter(ImageFilter.GaussianBlur(soften))
    veil = Image.new("RGB", image.size, CREAM_HAZE)
    image = Image.blend(image, veil, haze)
    return PlateInfo(image, floor_rgb, wall_rgb)


def _average(image: Any) -> Rgb:
    small = image.resize((1, 1), Image.Resampling.BOX)
    r, g, b = small.getpixel((0, 0))[:3]
    return (int(r), int(g), int(b))


def render_plate(
    info: PlateInfo,
    camera: CameraState,
    size: tuple[int, int],
) -> Any:
    """The plate seen by ``camera``: parallax crop, depth-of-field blur."""
    w, h = size
    fs = w / STAGE_W
    zb = parallax_zoom(camera.zoom)
    cx = STAGE_W / 2 + (camera.cx + camera.shake[0] - STAGE_W / 2) * PARALLAX_FACTOR
    cy = STAGE_H / 2 + (camera.cy + camera.shake[1] - STAGE_H / 2) * PARALLAX_FACTOR
    half_w, half_h = STAGE_W / (2 * zb), STAGE_H / (2 * zb)
    cx = clamp(cx, half_w, STAGE_W - half_w)
    cy = clamp(cy, half_h, STAGE_H - half_h)
    box = (cx - half_w, cy - half_h, cx + half_w, cy + half_h)
    blur = background_blur_px(camera.zoom) * camera.zoom * fs
    if blur < 0.6:
        return info.image.resize(size, Image.Resampling.BICUBIC, box=box)
    small = (max(8, w // 2), max(8, h // 2))
    image = info.image.resize(small, Image.Resampling.BILINEAR, box=box)
    image = image.filter(ImageFilter.GaussianBlur(blur / 2))
    return image.resize(size, Image.Resampling.BICUBIC)


# --- floor ----------------------------------------

FLOOR_TINTS: Final[dict[str, tuple[Rgb, str]]] = {
    # location id: (tint to mix with the plate's own floor colour, pattern)
    "front": ((198, 168, 132), "flags"),
    "kitchen": ((190, 134, 88), "planks"),
}


def floor_colour(info: PlateInfo, location: str) -> Rgb:
    tint, _ = FLOOR_TINTS.get(location, ((196, 156, 116), "planks"))
    base = info.floor_rgb
    mixed = tuple(round(lerp(base[i], tint[i], 0.55)) for i in range(3))
    return (round(mixed[0] * 0.94), round(mixed[1] * 0.92), round(mixed[2] * 0.9))


def _shade(rgb: Rgb, factor: float) -> Rgb:
    return (
        round(clamp(rgb[0] * factor, 0, 255)),
        round(clamp(rgb[1] * factor, 0, 255)),
        round(clamp(rgb[2] * factor, 0, 255)),
    )


def draw_floor(
    frame: Any,
    camera: CameraState,
    location: str,
    base: Rgb,
    *,
    top: float = GROUND_TOP,
) -> None:
    """Draw the code floor (a wall base, boards or flagstones) at the frame's scale."""
    w, h = frame.size
    k = camera.zoom * (w / STAGE_W)
    x_origin = camera.cx + camera.shake[0] - STAGE_W / (2 * camera.zoom)
    y_origin = camera.cy + camera.shake[1] - STAGE_H / (2 * camera.zoom)

    def sy(y: float) -> float:
        return (y - y_origin) * k

    def sx(x: float) -> float:
        return (x - x_origin) * k

    y0 = sy(top)
    if y0 >= h:
        return
    draw = ImageDraw.Draw(frame)
    # skirting board where the wall meets the floor
    draw.rectangle([0, sy(top - SKIRTING_H), w, y0], fill=_shade(base, 0.66))
    draw.rectangle(
        [0, sy(top - SKIRTING_H), w, sy(top - SKIRTING_H) + max(1.0, 3 * k)],
        fill=_shade(base, 0.8),
    )
    # a smooth vertical gradient in bands (light far, a little darker near)
    bottom = sy(STAGE_H + 80)
    bands = 22
    for i in range(bands):
        a = y0 + (bottom - y0) * i / bands
        b = y0 + (bottom - y0) * (i + 1) / bands + 1
        v = i / (bands - 1)
        draw.rectangle([0, a, w, b], fill=_shade(base, 1.06 - 0.2 * v**0.8))
    _tint, pattern = FLOOR_TINTS.get(location, ((0, 0, 0), "planks"))
    seam = _shade(base, 0.74)
    light = _shade(base, 1.12)
    row_top = top
    row_h = 30.0
    index = 0
    while sy(row_top) < h and index < 40:
        row_bottom = row_top + row_h
        line_w = max(1.0, (2.0 + index * 0.18) * k)
        draw.rectangle([0, sy(row_top), w, sy(row_top) + line_w], fill=seam)
        draw.rectangle(
            [0, sy(row_top) + line_w, w, sy(row_top) + line_w * 1.8], fill=light
        )
        pitch = (210.0 if pattern == "flags" else 330.0) * (1 + index * 0.05)
        offset = (index % 2) * pitch / 2 + (index * 37) % 90
        x = -pitch + (offset % pitch)
        while sx(x) < w:
            if sx(x) > -10:
                draw.rectangle(
                    [sx(x), sy(row_top), sx(x) + max(1.0, 2 * k), sy(row_bottom)],
                    fill=seam,
                )
            x += pitch
        row_top = row_bottom
        row_h *= 1.18
        index += 1
    # soft shade right under the skirting
    shade_h = 70 * k
    ramp = (
        Image.linear_gradient("L")
        .resize((1, 64))
        .transpose(Image.Transpose.FLIP_TOP_BOTTOM)
    )
    mask = ramp.resize((w, max(1, round(shade_h)))).point(lambda v: v * 0.42)
    frame.paste((40, 24, 18), (0, round(y0)), mask)


# --- shadows ----------------------------------------


@lru_cache(maxsize=1)
def _soft_ellipse() -> Any:
    size = 128
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).ellipse([14, 14, size - 14, size - 14], fill=255)
    return mask.filter(ImageFilter.GaussianBlur(11))


def draw_shadow(
    frame: Any, centre: Point, rx: float, ry: float, alpha: float = 0.38
) -> None:
    """A soft contact shadow ellipse (screen px)."""
    if rx < 1 or ry < 1 or alpha <= 0:
        return
    box = (round(centre[0] - rx * 1.2), round(centre[1] - ry * 1.2))
    size = (max(2, round(rx * 2.4)), max(2, round(ry * 2.4)))
    mask = (
        _soft_ellipse()
        .resize(size, Image.Resampling.BILINEAR)
        .point(lambda v: round(v * alpha))
    )
    frame.paste((46, 28, 28), box, mask)


# --- soft sprites ----------------------------------------


@lru_cache(maxsize=4)
def _puff(variant: int) -> Any:
    """A soft cloud-like blob (three overlapping discs, blurred)."""
    size = 160
    mask = Image.new("L", (size, size), 0)
    rng = random.Random(1000 + variant)
    draw = ImageDraw.Draw(mask)
    for _ in range(4):
        r = rng.uniform(18, 34)
        cx = size / 2 + rng.uniform(-30, 30)
        cy = size / 2 + rng.uniform(-26, 26)
        draw.ellipse([cx - r, cy - r * rng.uniform(0.7, 1.1), cx + r, cy + r], fill=255)
    return mask.filter(ImageFilter.GaussianBlur(16))


@lru_cache(maxsize=1)
def _glow_mask() -> Any:
    size = 192
    mask = Image.new("L", (size, size), 0)
    pixels = mask.load()
    for y in range(size):
        for x in range(size):
            d = math.hypot((x + 0.5) / size * 2 - 1, (y + 0.5) / size * 2 - 1)
            pixels[x, y] = round(255 * max(0.0, 1 - d) ** 2)
    return mask


def paste_soft(
    frame: Any, mask: Any, centre: Point, radius: float, color: Rgb, alpha: float
) -> None:
    if radius < 1 or alpha <= 0.004:
        return
    d = max(2, round(radius * 2))
    left, top = round(centre[0] - radius), round(centre[1] - radius)
    if left >= frame.width or top >= frame.height or left + d <= 0 or top + d <= 0:
        return
    scaled = mask.resize((d, d), Image.Resampling.BILINEAR).point(
        lambda v: round(v * clamp(alpha))
    )
    frame.paste(color, (left, top), scaled)


# --- steam ----------------------------------------


@dataclass(frozen=True)
class Puff:
    """A steam puff in stage pixels relative to its emitter."""

    dx: float
    dy: float
    radius: float
    alpha: float
    born_ms: float
    variant: int


def steam_puffs(
    t_ms: float,
    start_ms: float,
    end_ms: float,
    *,
    burst: bool,
    seed: int,
    scale: float = 1.0,
    strength: float = 1.0,
) -> list[Puff]:
    """Puffs alive at ``t_ms`` for an emitter running ``start_ms``-``end_ms``."""
    interval = 13.0 if burst else 22.0
    life = 1600.0 if burst else 2400.0
    speed = (190.0 if burst else 78.0) * scale
    radius0 = (7.0 if burst else 5.0) * scale
    radius1 = (34.0 if burst else 22.0) * scale
    peak = (0.52 if burst else 0.38) * strength
    puffs: list[Puff] = []
    first = max(0, math.ceil((t_ms - life - start_ms) / interval))
    n = first
    while True:
        born = start_ms + n * interval
        if born > min(t_ms, end_ms):
            break
        rng = random.Random(seed * 100003 + n)
        life_n = life * rng.uniform(0.8, 1.2)
        age = t_ms - born
        if 0 <= age <= life_n:
            u = age / life_n
            rise = speed * (age / 1000) * (1 - 0.35 * u)
            curl = (
                (30 + 26 * rng.random())
                * scale
                * math.sin(
                    math.tau * (0.7 + 0.4 * rng.random()) * age / 1000
                    + rng.random() * 6.28
                )
            )
            fade_in = smoothstep(0.0, 0.14, u)
            fade_out = 1 - smoothstep(0.5, 1.0, u)
            puffs.append(
                Puff(
                    dx=rng.uniform(-6, 6) * scale + curl * min(1.0, u * 1.6),
                    dy=-rise,
                    radius=lerp(radius0, radius1, u**0.8),
                    alpha=peak * fade_in * fade_out,
                    born_ms=born,
                    variant=rng.randrange(3),
                )
            )
        n += 1
    return puffs


def draw_steam(frame: Any, puffs: Sequence[Puff], origin: Point, k: float) -> None:
    """Soft rising steam: warm white blobs, drawn oldest first."""
    for puff in puffs:
        paste_soft(
            frame,
            _puff(puff.variant),
            (origin[0] + puff.dx * k, origin[1] + puff.dy * k),
            puff.radius * k * 1.25,
            (255, 255, 252),
            puff.alpha,
        )


# --- light ----------------------------------------


def draw_glow(
    frame: Any, centre: Point, radius: float, color: Rgb, alpha: float
) -> None:
    paste_soft(frame, _glow_mask(), centre, radius, color, alpha)


def dust_motes(
    t_ms: float, seed: int, count: int = 22
) -> list[tuple[float, float, float, float]]:
    """``(x, y, radius, alpha)`` of motes in stage pixels, drifting up and sideways."""
    rng = random.Random(seed)
    motes = []
    for _ in range(count):
        x0 = rng.uniform(80, 1000)
        y0 = rng.uniform(500, 1500)
        speed = rng.uniform(6, 16)
        wobble = rng.uniform(8, 22)
        phase = rng.uniform(0, math.tau)
        radius = rng.uniform(2.2, 5.0)
        t = t_ms / 1000
        y = (y0 - speed * t) % 1000 + 450
        x = x0 + wobble * math.sin(t * 0.5 + phase) + 4 * t % 1
        twinkle = 0.55 + 0.45 * math.sin(t * 1.3 + phase * 2)
        motes.append((x, y, radius, 0.5 * twinkle))
    return motes


def draw_dust(
    frame: Any, motes: Sequence[tuple[float, float, float, float]], camera: CameraState
) -> None:
    w = frame.width
    k = camera.zoom * (w / STAGE_W)
    x_origin = camera.cx + camera.shake[0] - STAGE_W / (2 * camera.zoom)
    y_origin = camera.cy + camera.shake[1] - STAGE_H / (2 * camera.zoom)
    for x, y, radius, alpha in motes:
        sx, sy = (x - x_origin) * k, (y - y_origin) * k
        if -20 < sx < frame.width + 20 and -20 < sy < frame.height + 20:
            paste_soft(
                frame,
                _glow_mask(),
                (sx, sy),
                max(1.5, radius * k * 1.4),
                (255, 236, 190),
                alpha,
            )


# --- passers-by ----------------------------------------


@dataclass(frozen=True)
class Walker:
    x: float
    y: float
    scale: float
    direction: float
    phase: float
    tone: Rgb


def passersby(
    t_ms: float, start_ms: float, end_ms: float, count: int, seed: int
) -> list[Walker]:
    """Silhouettes crossing the street behind the shopfront."""
    rng = random.Random(seed)
    walkers: list[Walker] = []
    span = max(1.0, end_ms - start_ms)
    for i in range(count):
        direction = 1.0 if i % 2 == 0 else -1.0
        scale = rng.uniform(0.46, 0.56)
        delay = i * 0.22
        u = (t_ms - start_ms) / span * 0.9 - delay * 0.4
        if not 0.0 <= u <= 1.0:
            continue
        x = (
            lerp(-90.0, STAGE_W + 90.0, u)
            if direction > 0
            else lerp(STAGE_W + 90.0, -90.0, u)
        )
        walkers.append(
            Walker(
                x=x,
                y=1282 + i * 16,
                scale=scale,
                direction=direction,
                phase=rng.random(),
                tone=(118 - i * 10, 98 - i * 8, 112 - i * 8),
            )
        )
    return walkers


def draw_walker(frame: Any, walker: Walker, camera: CameraState, t_ms: float) -> None:
    """A soft flat silhouette with a walk cycle (legs, arms, head)."""
    from ai_youtube_agent.animation.draw import Pen, composite_at, plan_canvas

    w = frame.width
    k = camera.zoom * (w / STAGE_W)
    x_origin = camera.cx + camera.shake[0] - STAGE_W / (2 * camera.zoom)
    y_origin = camera.cy + camera.shake[1] - STAGE_H / (2 * camera.zoom)
    anchor = ((walker.x - x_origin) * k, (walker.y - y_origin) * k)
    size_k = k * walker.scale
    plan = plan_canvas(
        (-120, -600, 120, 20), anchor, size_k, frame.size, preferred_ss=1.5
    )
    if plan is None:
        return
    phase = walker.phase + t_ms / 1000 * 0.9 * walker.direction
    cycle = walk_cycle(phase, 300.0)
    pose = Pose(
        yaw=0.9 * walker.direction,
        foot_l=(walker.direction * cycle.foot_a[0], -cycle.foot_a[1]),
        foot_r=(walker.direction * cycle.foot_b[0], -cycle.foot_b[1]),
        hand_l=(70 + walker.direction * cycle.swing_a * 30, 40),
        hand_r=(-70 + walker.direction * cycle.swing_b * 30, 40),
        dy=cycle.bob,
    )
    skel = solve_skeleton(pose)
    from ai_youtube_agent.animation.draw import finish_canvas, new_canvas

    image = new_canvas(plan)
    tone = walker.tone
    pen = Pen(image, plan.origin, plan.k, outline=tone, outline_w=0.5)
    far, near = ("left", "right") if skel.near == "right" else ("right", "left")
    for side in (far, near):
        hip, knee, ankle = (
            (skel.hip_l, skel.knee_l, skel.ankle_l)
            if side == "left"
            else (skel.hip_r, skel.knee_r, skel.ankle_r)
        )
        pen.limb([hip, knee, ankle], 40, tone, shade=False)
        pen.ellipse(
            ankle[0] + skel.facing * 14, ankle[1] + 4, 30, 12, tone, outline=False
        )
    pen.taper(
        (skel.pelvis[0], skel.pelvis[1] + 8),
        56,
        (skel.chest[0], skel.chest[1] + 26),
        66,
        tone,
        shade=False,
    )
    for side in (far, near):
        sh = skel.shoulder_l if side == "left" else skel.shoulder_r
        hd = skel.hand_l if side == "left" else skel.hand_r
        el = skel.elbow_l if side == "left" else skel.elbow_r
        pen.limb([sh, el, hd], 34, tone, shade=False)
    pen.ellipse(skel.head[0], skel.head[1], 80, 84, tone, shade=False, outline=False)
    image = finish_canvas(image, plan).filter(
        ImageFilter.GaussianBlur(max(0.6, 1.4 * k * walker.scale))
    )
    composite_at(
        frame, image, plan.left, plan.top
    ) if frame.mode == "RGBA" else frame.paste(image, (plan.left, plan.top), image)


# --- liquids ----------------------------------------


def broth_arc(
    start: Point, end: Point, progress: float, samples: int = 14
) -> list[Point]:
    """Screen points of a poured stream falling from ``start`` to ``end``."""
    drop = abs(end[1] - start[1]) + 20
    pts = []
    reach = clamp(progress)
    for i in range(samples + 1):
        u = i / samples * reach
        x = lerp(start[0], end[0], u)
        y = lerp(start[1], end[1], u * u) + math.sin(math.pi * u) * -drop * 0.08
        pts.append((x, y))
    return pts
