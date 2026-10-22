import pytest

from ai_youtube_agent.content.longform_timeline import (
    ChapterMarker,
    LongFormTimeline,
    format_chapter_timestamp,
)
from ai_youtube_agent.content.shorts_timeline import (
    TIMELINE_SCHEMA_VERSION,
    Scene,
    TimingSource,
)


def test_format_chapter_timestamp_renders_mm_ss_and_h_mm_ss() -> None:
    assert format_chapter_timestamp(0) == "00:00"
    assert format_chapter_timestamp(15_000) == "00:15"
    assert format_chapter_timestamp(605_000) == "10:05"
    assert format_chapter_timestamp(3_661_000) == "01:01:01"


def test_longform_timeline_validates_contiguous_scenes_and_chapters() -> None:
    scenes = (
        Scene("s1", 0, 0, 15_000),
        Scene("s2", 1, 15_000, 75_000),
        Scene("s3", 2, 75_000, 135_000),
        Scene("s4", 3, 135_000, 200_000),
    )
    chapters = (
        ChapterMarker("ch1", "Intro", "s1", "s1", 0, 15_000),
        ChapterMarker("ch2", "Part 1", "s2", "s2", 15_000, 75_000),
        ChapterMarker("ch3", "Part 2", "s3", "s4", 75_000, 200_000),
    )
    timeline = LongFormTimeline(
        TIMELINE_SCHEMA_VERSION,
        200_000,
        TimingSource.ESTIMATED,
        scenes=scenes,
        chapters=chapters,
    )

    assert timeline.duration_ms == 200_000
    assert LongFormTimeline.from_dict(timeline.to_dict()) == timeline
    assert LongFormTimeline.from_dict(timeline.to_dict()).sha256 == timeline.sha256


def test_longform_timeline_refuses_too_few_chapters_or_non_zero_first_chapter() -> None:
    scenes = (
        Scene("s1", 0, 0, 100_000),
        Scene("s2", 1, 100_000, 200_000),
    )
    with pytest.raises(ValueError, match="3 to 50"):
        LongFormTimeline(
            TIMELINE_SCHEMA_VERSION,
            200_000,
            TimingSource.ESTIMATED,
            scenes=scenes,
            chapters=(
                ChapterMarker("ch1", "Intro", "s1", "s1", 0, 100_000),
                ChapterMarker("ch2", "Main", "s2", "s2", 100_000, 200_000),
            ),
        )
    with pytest.raises(ValueError, match="must match scene s1 start_ms"):
        LongFormTimeline(
            TIMELINE_SCHEMA_VERSION,
            200_000,
            TimingSource.ESTIMATED,
            scenes=(
                Scene("s1", 0, 0, 60_000),
                Scene("s2", 1, 60_000, 120_000),
                Scene("s3", 2, 120_000, 200_000),
            ),
            chapters=(
                ChapterMarker("ch1", "A", "s1", "s1", 10_000, 60_000),
                ChapterMarker("ch2", "B", "s2", "s2", 60_000, 120_000),
                ChapterMarker("ch3", "C", "s3", "s3", 120_000, 200_000),
            ),
        )


def test_longform_timeline_refuses_chapters_shorter_than_10_seconds() -> None:
    with pytest.raises(ValueError, match="10000 ms"):
        ChapterMarker("ch1", "Short", "s1", "s1", 0, 5_000)
