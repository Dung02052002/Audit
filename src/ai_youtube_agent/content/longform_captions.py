"""LongForm captions (Prompt Pack v8, prompt J-109), context C8 Video Production.

Reuses the caption wrapping, pixel box estimation and phrase timing logic of
``content/shorts_captions.py`` over ``LongFormConfig`` (16:9 horizontal canvas
and ``DEFAULT_LONGFORM_CAPTION_RULES``) and ``LongFormTimeline``.
"""

from ai_youtube_agent.content.longform_config import LongFormConfig
from ai_youtube_agent.content.longform_project import LongFormProject
from ai_youtube_agent.content.longform_timeline import LongFormTimeline
from ai_youtube_agent.content.script import Script
from ai_youtube_agent.content.shorts_captions import (
    DEFAULT_FONT_SIZE,
    MAX_CHARS_PER_SECOND,
    CaptionCheck,
    CaptionIssue,
    IssueKind,
    PixelBox,
    caption_box,
    wrap,
)
from ai_youtube_agent.content.shorts_captions import (
    check_captions as _check_captions,
)
from ai_youtube_agent.content.shorts_captions import (
    covers_every_scene as _covers_every_scene,
)
from ai_youtube_agent.content.shorts_captions import (
    plan_captions as _plan_captions,
)
from ai_youtube_agent.content.shorts_timeline import CaptionOverlay


def check_longform_captions(
    config: LongFormConfig, timeline: LongFormTimeline
) -> CaptionCheck:
    """Validate ``timeline.captions`` and overlay boxes against ``config``."""
    return _check_captions(config, timeline)  # type: ignore[arg-type]


def covers_every_longform_scene(timeline: LongFormTimeline) -> bool:
    """True when every scene of ``timeline`` has at least one caption."""
    return _covers_every_scene(timeline)  # type: ignore[arg-type]


def plan_longform_captions(
    project: LongFormProject,
    script: Script,
    timeline: LongFormTimeline,
) -> tuple[CaptionOverlay, ...]:
    """Plan timed captions for every scene of ``timeline`` from ``script``."""
    return _plan_captions(project, script, timeline)  # type: ignore[arg-type]


__all__ = [
    "CaptionCheck",
    "CaptionIssue",
    "DEFAULT_FONT_SIZE",
    "IssueKind",
    "MAX_CHARS_PER_SECOND",
    "PixelBox",
    "caption_box",
    "check_longform_captions",
    "covers_every_longform_scene",
    "plan_longform_captions",
    "wrap",
]
