"""Transitions between scenes: cut, crossfade, whip pan, steam dissolve and dip.

The timing is pure (``transition_progress``, ``whip_offset``); ``blend_frames``
mixes two rendered frames with Pillow, imported only when it is called.
"""

from __future__ import annotations

from typing import Any, Final

from ai_youtube_agent.animation.easing import clamp, ease_in_out_cubic, smoothstep
from ai_youtube_agent.animation.storyboard import TRANSITION_KINDS
from ai_youtube_agent.animation.timeline import ResolvedTransition

DIP_RGB: Final[tuple[int, int, int]] = (38, 22, 12)
HAZE_RGB: Final[tuple[int, int, int]] = (255, 246, 230)
WHIP_BLUR_FRACTION: Final[float] = 0.075


def transition_progress(t_ms: float, transition: ResolvedTransition) -> float:
    """0 at the start of the transition and 1 at its end (a cut jumps at the middle)."""
    span = transition.end_ms - transition.start_ms
    if span <= 0:
        return 0.0 if t_ms < transition.boundary_ms else 1.0
    return clamp((t_ms - transition.start_ms) / span)


def whip_offset(progress: float, width: int, direction: str | None) -> int:
    """Pixels the pair of frames has slid by ``progress`` (toward ``direction``)."""
    travelled = round(ease_in_out_cubic(progress) * width)
    return travelled if direction != "left" else -travelled


def haze_mix(progress: float) -> tuple[float, float]:
    """``(to_haze, from_haze)``: A turns to steam, then B clears."""
    return smoothstep(0.0, 0.5, progress), 1.0 - smoothstep(0.5, 1.0, progress)


def blend_frames(
    kind: str,
    a: Any,
    b: Any,
    progress: float,
    *,
    direction: str | None = None,
) -> Any:
    """The frame at ``progress`` of a transition from RGB image ``a`` to ``b``."""
    from PIL import Image, ImageFilter

    if kind not in TRANSITION_KINDS:
        raise ValueError(f"unknown transition '{kind}'")
    progress = clamp(progress)
    if kind == "cut":
        return b if progress >= 0.5 else a
    if kind == "crossfade":
        return Image.blend(a, b, smoothstep(0.0, 1.0, progress))
    if kind == "dip":
        dip = Image.new("RGB", a.size, DIP_RGB)
        if progress < 0.5:
            return Image.blend(a, dip, smoothstep(0.0, 0.5, progress))
        return Image.blend(dip, b, smoothstep(0.5, 1.0, progress))
    if kind == "whip_pan":
        width, _height = a.size
        offset = whip_offset(progress, width, direction)
        sign = 1 if offset >= 0 else -1
        strip = Image.new("RGB", a.size)
        strip.paste(a, (-offset, 0))
        strip.paste(b, (-offset + sign * width, 0))
        blur = int(width * WHIP_BLUR_FRACTION * (1 - abs(2 * progress - 1) ** 2))
        if blur >= 1:
            strip = strip.filter(ImageFilter.BoxBlur((blur, 0))).filter(
                ImageFilter.BoxBlur((max(1, blur // 2), 0))
            )
        return strip
    # steam_dissolve: A -> white haze -> B
    to_haze, from_haze = haze_mix(progress)
    radius = max(2.0, a.width / 40)
    source = a if progress < 0.5 else b
    small = source.resize((a.width // 3, a.height // 3), Image.Resampling.BILINEAR)
    soft = small.filter(ImageFilter.GaussianBlur(radius / 3)).resize(
        a.size, Image.Resampling.BICUBIC
    )
    haze = Image.blend(soft, Image.new("RGB", a.size, HAZE_RGB), 0.62)
    if progress < 0.5:
        return Image.blend(a, haze, to_haze)
    return Image.blend(b, haze, from_haze)
