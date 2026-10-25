"""Errors of the animation pipeline."""

from __future__ import annotations

from collections.abc import Iterable


class AnimationError(Exception):
    """A step of the animation pipeline could not deliver its real output."""


class StoryboardError(ValueError):
    """The storyboard is malformed; ``issues`` lists every problem found."""

    def __init__(self, issues: Iterable[str]) -> None:
        self.issues: tuple[str, ...] = tuple(issues)
        shown = "; ".join(self.issues[:8])
        extra = len(self.issues) - 8
        suffix = f" (+{extra} more)" if extra > 0 else ""
        super().__init__(f"{len(self.issues)} storyboard issue(s): {shown}{suffix}")
