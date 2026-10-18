import pytest

from ai_youtube_agent.content.longform_timeline import (
    ChapterMarker,
    LongFormTimeline,
    format_chapter_timestamp,
)
from ai_youtube_agent.content.shorts_timeline import (
    AssetRole,
    CaptionOverlay,
    Scene,
    SceneAsset,
    TimingSource,
)


def test_format_chapter_timestamp_renders_mm_ss_and_h_mm_ss() -> None:
    assert format_chapter_timestamp(0) == "00:00"
    assert format_chapter_timestamp(15_000) == "00:15"
    assert format_chapter_timestamp(605_000) == "10:05"
    assert format_chapter_timestamp(3_661_000) == "1:01:01"


def test_longform_timeline_validates_contiguous_scenes_and_chapters() -> None:
    scenes = (
        Scene(index=0, section_index=0, start_ms=0, duration_ms=15_000),
        Scene(index=1, section_index=1, start_ms=15_000, duration_ms=60_000),
        Scene(index=2, section_index=2, start_ms=75_000, duration_ms=60_000),
        Scene(index=3, section_index=3, start_ms=135_000, duration_ms=65_000),
    )
    chapters = (
        ChapterMarker(index=0, section_index=0, start_ms=0, title="Intro"),
        ChapterMarker(index=1, section_index=1, start_ms=15_000, title="Part 1"),
        ChapterMarker(index=2, section_index=2, start_ms=75_000, title="Part 2"),
    )
    timeline = LongFormTimeline(
        scenes=scenes,
        chapters=chapters,
        timing_source=TimingSource.ESTIMATED,
    )

    assert timeline.duration_ms == 200_000
    assert LongFormTimeline.from_dict(timeline.to_dict()) == timeline
    assert LongFormTimeline.from_dict(timeline.to_dict()).sha256 == timeline.sha256


def test_longform_timeline_refuses_too_few_chapters_or_non_zero_first_chapter() -> None:
    scenes = (
        Scene(index=0, section_index=0, start_ms=0, duration_ms=100_000),
        Scene(index=1, section_index=1, start_ms=100_000, duration_ms=100_000),
    )
    with pytest.raises(ValueError, match="at least 3"):
        LongFormTimeline(
            scenes=scenes,
            chapters=(
                ChapterMarker(index=0, section_index=0, start_ms=0, title="Intro"),
                ChapterMarker(index=1, section_index=1, start_ms=100_000, title="Main"),
            ),
        )
    with pytest.raises(ValueError, match="0 ms"):
        LongFormTimeline(
            scenes=(
                Scene(index=0, section_index=0, start_ms=0, duration_ms=60_000),
                Scene(index=1, section_index=1, start_ms=60_000, duration_ms=60_000),
                Scene(index=2, section_index=2, start_ms=120_000, duration_ms=80_000),
            ),
            chapters=(
                ChapterMarker(index=0, section_index=0, start_ms=10_000, title="A"),
                ChapterMarker(index=1, section_index=1, start_ms=60_000, title="B"),
                ChapterMarker(index=2, section_index=2, start_ms=120_000, title="C"),
            ),
        )


def test_longform_timeline_refuses_chapters_shorter_than_10_seconds() -> None:
    scenes = (
        Scene(index=0, section_index=0, start_ms=0, duration_ms=5_000),
        Scene(index=1, section_index=1, start_ms=5_000, duration_ms=95_000),
        Scene(index=2, section_index=2, start_ms=100_000, duration_ms=100_000),
    )
    with pytest.raises(ValueError, match="10000 ms"):
        LongFormTimeline(
            scenes=scenes,
            chapters=(
                ChapterMarker(index=0, section_index=0, start_ms=0, title="Short"),
                ChapterMarker(index=1, section_index=1, start_ms=5_000, title="One"),
                ChapterMarker(index=2, section_index=2, start_ms=100_000, title="Two"),
            ),
        )

