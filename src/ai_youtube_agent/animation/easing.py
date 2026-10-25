"""Easing functions and keyframe tracks (pure, deterministic)."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Final

Easing = Callable[[float], float]


def clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return low if value < low else high if value > high else value


def lerp(a: float, b: float, t: float) -> float:
    return a + (b - a) * t


def smoothstep(edge0: float, edge1: float, x: float) -> float:
    """0 below ``edge0``, 1 above ``edge1``, a smooth S-curve between."""
    if edge1 == edge0:
        return 1.0 if x >= edge1 else 0.0
    t = clamp((x - edge0) / (edge1 - edge0))
    return t * t * (3 - 2 * t)


def linear(t: float) -> float:
    return clamp(t)


def ease_in_out_cubic(t: float) -> float:
    t = clamp(t)
    return 4 * t * t * t if t < 0.5 else 1 - ((-2 * t + 2) ** 3) / 2


def ease_out_cubic(t: float) -> float:
    t = clamp(t)
    return 1 - (1 - t) ** 3


def ease_in_cubic(t: float) -> float:
    t = clamp(t)
    return t * t * t


def ease_out_back(t: float, overshoot: float = 1.70158) -> float:
    """Ends at 1 after a small overshoot (a settling pop)."""
    t = clamp(t)
    c3 = overshoot + 1
    return 1 + c3 * (t - 1) ** 3 + overshoot * (t - 1) ** 2


EASINGS: Final[dict[str, Easing]] = {
    "linear": linear,
    "ease_in": ease_in_cubic,
    "ease_out": ease_out_cubic,
    "ease_in_out": ease_in_out_cubic,
    "ease_out_back": ease_out_back,
}


def get_easing(name: str | None) -> Easing:
    """The easing called ``name`` (``None`` means ``ease_in_out``)."""
    if name is None:
        return ease_in_out_cubic
    try:
        return EASINGS[name]
    except KeyError:
        raise ValueError(f"unknown easing '{name}'") from None


def critically_damped(
    position: float,
    velocity: float,
    target: float,
    smooth_time: float,
    dt: float,
) -> tuple[float, float]:
    """One step of a critically damped spring toward ``target``.

    The same closed form as Unity's ``SmoothDamp``: stable for any ``dt``, no
    overshoot, and ``smooth_time`` is roughly the time to reach the target.
    """
    if smooth_time <= 1e-6:
        return target, 0.0
    omega = 2.0 / smooth_time
    x = omega * dt
    exp = 1.0 / (1.0 + x + 0.48 * x * x + 0.235 * x * x * x)
    change = position - target
    temp = (velocity + omega * change) * dt
    new_velocity = (velocity - omega * temp) * exp
    new_position = target + (change + temp) * exp
    return new_position, new_velocity


@dataclass(frozen=True)
class Track:
    """Keyframes ``(time, value)`` sampled with an easing between neighbours.

    Times must be strictly increasing; outside the range the first or last
    value holds.
    """

    keys: tuple[tuple[float, float], ...]
    easing: Easing = ease_in_out_cubic

    def __post_init__(self) -> None:
        if not self.keys:
            raise ValueError("a track needs at least one keyframe")
        times = [time for time, _ in self.keys]
        if any(b <= a for a, b in zip(times, times[1:], strict=False)):
            raise ValueError("keyframe times must be strictly increasing")

    @classmethod
    def from_pairs(
        cls, pairs: Sequence[tuple[float, float]], easing: Easing = ease_in_out_cubic
    ) -> Track:
        return cls(tuple(pairs), easing)

    def value_at(self, time: float) -> float:
        keys = self.keys
        if time <= keys[0][0]:
            return keys[0][1]
        if time >= keys[-1][0]:
            return keys[-1][1]
        for (t0, v0), (t1, v1) in zip(keys, keys[1:], strict=False):
            if t0 <= time <= t1:
                return lerp(v0, v1, self.easing((time - t0) / (t1 - t0)))
        raise AssertionError("unreachable: time is inside the key range")
