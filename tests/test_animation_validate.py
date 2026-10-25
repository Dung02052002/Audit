"""Film validation: container facts, motion, scene variety, composition (no GPU).

Pure checks use hand-made numbers; the ffmpeg-backed ones run on tiny videos that
ffmpeg itself generates (skipped without ffmpeg).
"""

from __future__ import annotations

import copy
import shutil
import struct
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from ai_youtube_agent.animation import validate
from ai_youtube_agent.animation.errors import AnimationError

needs_ffmpeg = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg/ffprobe are not installed",
)

EXPECTED = validate.ExpectedMedia(
    width=1080, height=1920, fps=30, frames=849, min_s=20.0, max_s=30.0
)
GOOD_PROBE = {
    "streams": [
        {
            "codec_type": "video",
            "codec_name": "h264",
            "profile": "High",
            "level": 42,
            "width": 1080,
            "height": 1920,
            "pix_fmt": "yuv420p",
            "sample_aspect_ratio": "1:1",
            "r_frame_rate": "30/1",
            "avg_frame_rate": "30/1",
            "nb_frames": "849",
            "duration": "28.300000",
        },
        {
            "codec_type": "audio",
            "codec_name": "aac",
            "sample_rate": "48000",
            "channels": 2,
            "duration": "28.330000",
        },
    ],
    "format": {"duration": "28.330000"},
}


def probe_with(**changes) -> dict:
    probe = copy.deepcopy(GOOD_PROBE)
    for key, value in changes.items():
        stream, field = key.split("__", 1)
        if stream == "format":
            probe["format"][field] = value
        else:
            kind = "video" if stream == "video" else "audio"
            next(s for s in probe["streams"] if s["codec_type"] == kind)[field] = value
    return probe


# --- container ----------------------------------------------------------------------


def test_a_good_probe_passes_and_reports_facts() -> None:
    problems, facts = validate.check_container(GOOD_PROBE, EXPECTED, faststart=True)

    assert problems == []
    assert facts["video"]["nb_frames"] == "849" and facts["faststart"] is True
    assert facts["av_difference_s"] == pytest.approx(0.03)


@pytest.mark.parametrize(
    ("changes", "fragment"),
    [
        ({"video__codec_name": "hevc"}, "not h264"),
        ({"video__profile": "Main"}, "not High"),
        ({"video__width": 720}, "720x1920"),
        ({"video__sample_aspect_ratio": "4:3"}, "aspect ratio"),
        ({"video__r_frame_rate": "24/1"}, "frame rate"),
        ({"video__nb_frames": "800"}, "800 frames, expected 849"),
        ({"video__pix_fmt": "yuv444p"}, "pixel format"),
        ({"audio__codec_name": "mp3"}, "not aac"),
        ({"audio__sample_rate": "44100"}, "44100"),
        ({"audio__channels": 1}, "1 channels"),
        ({"audio__duration": "28.6"}, "differ by"),
        ({"format__duration": "31.0"}, "outside 20-30"),
        ({"format__duration": "65.0"}, "60 s cap"),
    ],
)
def test_each_container_fact_is_checked(changes, fragment) -> None:
    problems, _ = validate.check_container(
        probe_with(**changes), EXPECTED, faststart=True
    )

    assert any(fragment in problem for problem in problems), problems


def test_missing_streams_and_no_faststart_are_problems() -> None:
    problems, _ = validate.check_container(
        {"streams": [], "format": {}}, EXPECTED, faststart=False
    )

    assert "no video stream" in problems and "no audio stream" in problems
    assert any("faststart" in p for p in problems)


def box(kind: bytes, payload: bytes = b"") -> bytes:
    return struct.pack(">I4s", 8 + len(payload), kind) + payload


def test_faststart_needs_moov_before_mdat(tmp_path: Path) -> None:
    good = tmp_path / "good.mp4"
    good.write_bytes(
        box(b"ftyp", b"isom") + box(b"moov", b"x" * 20) + box(b"mdat", b"y" * 50)
    )
    bad = tmp_path / "bad.mp4"
    bad.write_bytes(
        box(b"ftyp", b"isom") + box(b"mdat", b"y" * 50) + box(b"moov", b"x" * 20)
    )

    assert [k for k, _ in validate.mp4_boxes(good)] == ["ftyp", "moov", "mdat"]
    assert validate.faststart_ok(good) is True
    assert validate.faststart_ok(bad) is False
    empty = tmp_path / "e.mp4"
    empty.write_bytes(b"")
    assert validate.faststart_ok(empty) is False


def test_decode_errors_are_the_lines_ffmpeg_prints() -> None:
    def run(command, **_kw):
        return SimpleNamespace(returncode=0, stderr="[h264] error while decoding\n\n")

    assert validate.decode_errors(Path("x.mp4"), run=run) == [
        "[h264] error while decoding"
    ]
    clean = SimpleNamespace(returncode=0, stderr="")
    assert validate.decode_errors(Path("x.mp4"), run=lambda *a, **k: clean) == []
    failed = SimpleNamespace(returncode=1, stderr="")
    assert validate.decode_errors(Path("x.mp4"), run=lambda *a, **k: failed) == [
        "ffmpeg exited with 1"
    ]


def test_probe_media_parses_json_or_raises() -> None:
    ok = SimpleNamespace(returncode=0, stdout='{"streams": []}', stderr="")
    assert validate.probe_media(Path("x"), run=lambda *a, **k: ok) == {"streams": []}
    bad = SimpleNamespace(returncode=1, stdout="", stderr="no such file")
    with pytest.raises(AnimationError, match="ffprobe failed"):
        validate.probe_media(Path("x"), run=lambda *a, **k: bad)


# --- motion -------------------------------------------------------------------------

MOTION_TEXT = """frame:0    pts:0       pts_time:0
lavfi.signalstats.YAVG=0.000000
frame:1    pts:1       pts_time:0.0333333
lavfi.signalstats.YAVG=1.250000
frame:2    pts:2       pts_time:0.0666667
lavfi.signalstats.YAVG=0.500000
"""


def test_parse_motion_pairs_each_frame_time_with_its_value() -> None:
    assert validate.parse_motion(MOTION_TEXT) == [
        (0.0, 0.0),
        (pytest.approx(0.0333333), 1.25),
        (pytest.approx(0.0666667), 0.5),
    ]
    assert validate.parse_motion("nothing") == []


def test_the_motion_command_uses_a_relative_file_name() -> None:
    command = validate.motion_command(Path("D:/runs/a/film.mp4"))
    graph = command[command.index("-vf") + 1]

    assert "tblend=all_mode=difference" in graph and "signalstats" in graph
    assert f"file={validate.MOTION_FILE}" in graph
    assert "D:" not in graph and "/" not in graph.replace("scale=108:192", "")


def series(values: list[float], fps: int = 30) -> list[tuple[float, float]]:
    return [(i / fps, v) for i, v in enumerate(values)]


def test_a_frozen_stretch_longer_than_half_a_second_is_found() -> None:
    moving, still = [2.0] * 30, [0.0] * 20  # 0.67 s of stillness
    data = series(moving + still + moving)

    stretches = validate.frozen_stretches(data)

    assert len(stretches) == 1
    assert stretches[0][0] == pytest.approx(1.0) and stretches[0][1] == pytest.approx(
        1.63, abs=0.05
    )
    assert validate.frozen_stretches(series(moving + [0.0] * 10 + moving)) == []
    assert (
        validate.frozen_stretches(data, holds=[(0.9, 1.8)]) == []
    )  # a deliberate hold


def test_scenes_must_move_in_most_of_their_seconds() -> None:
    lively = series([1.0] * 90)  # 3 s of motion
    scenes = [("a", 0.0, 3.0)]

    problems, facts = validate.analyze_motion(lively, scenes)
    assert problems == [] and facts["scenes"]["a"]["moving_share"] == 1.0

    dull = series([1.0] * 30 + [0.2] * 60)  # 1 of 3 seconds moves
    problems, facts = validate.analyze_motion(dull, scenes)
    assert any("scene 'a' moves in only 33%" in p for p in problems)
    assert facts["scenes"]["a"]["moving_seconds"] == 1


def test_second_means_use_whole_seconds_only() -> None:
    means = validate.second_means(
        series([1.0] * 30 + [3.0] * 30 + [9.0] * 10), 0.0, 2.3
    )

    assert means == [1.0, 3.0]


# --- scene variety ------------------------------------------------------------------


def flat(rgb: tuple[int, int, int]) -> bytes:
    return bytes(rgb) * (validate.THUMB_SIZE[0] * validate.THUMB_SIZE[1])


def test_histogram_distance_is_zero_for_equal_pictures_and_one_for_disjoint() -> None:
    red, red2 = (
        validate.colour_histogram(flat((200, 10, 10))),
        validate.colour_histogram(flat((210, 20, 20))),
    )
    blue = validate.colour_histogram(flat((10, 10, 200)))

    assert sum(red) == pytest.approx(1.0)
    assert validate.histogram_distance(red, red2) == 0.0  # same 4-bin cell
    assert validate.histogram_distance(red, blue) == pytest.approx(1.0)
    half = validate.colour_histogram(
        flat((200, 10, 10))[: -3 * 3648] + flat((10, 10, 200))[: 3 * 3648]
    )
    assert validate.histogram_distance(red, half) == pytest.approx(0.5, abs=0.01)


def test_adjacent_scenes_that_look_alike_are_a_problem() -> None:
    red = validate.colour_histogram(flat((200, 10, 10)))
    blue = validate.colour_histogram(flat((10, 10, 200)))
    green = validate.colour_histogram(flat((10, 200, 10)))

    problems, facts = validate.analyze_scene_distinctness(
        {"s1": red, "s2": blue, "s3": blue, "s4": green}, ["s1", "s2", "s3", "s4"]
    )

    assert problems and "'s2' and 's3' look alike" in problems[0] and len(problems) == 1
    assert facts["distances"]["s1|s4"] == 1.0 and facts["distances"]["s2|s3"] == 0.0


def test_thumbnail_reads_one_raw_frame_or_raises() -> None:
    size = validate.THUMB_SIZE[0] * validate.THUMB_SIZE[1] * 3
    ok = SimpleNamespace(returncode=0, stdout=bytes(size))
    assert len(validate.thumbnail(Path("x"), 1.5, run=lambda *a, **k: ok)) == size
    short = SimpleNamespace(returncode=0, stdout=b"abc")
    with pytest.raises(AnimationError, match="thumbnail at 1.50 s"):
        validate.thumbnail(Path("x"), 1.5, run=lambda *a, **k: short)
    command = validate.thumbnail_command(Path("x.mp4"), 2.0)
    assert "-ss" in command and "rgb24" in command and command[-1] == "-"


# --- composition --------------------------------------------------------------------

SAFE = (64, 230, 950, 1536)
SIZE = (1080, 1920)


def named(name: str, box: tuple[int, int, int, int]):
    return SimpleNamespace(name=name, x0=box[0], y0=box[1], x1=box[2], y1=box[3])


def meta(
    frame: int,
    *,
    caption=(64, 1260, 950, 1500),
    faces=((300, 500, 500, 700),),
    characters=((200, 400, 700, 1500),),
    text=(),
):
    return SimpleNamespace(
        frame=frame,
        caption=caption,
        faces=tuple(named("mai", f) for f in faces),
        characters=tuple(named("mai", c) for c in characters),
        text_boxes=tuple(named("sign", t) for t in text),
    )


def test_faces_clear_of_the_caption_and_a_safe_caption_pass() -> None:
    tally = validate.CompositionTally(size=SIZE, safe_area=SAFE)
    for frame in range(100):
        tally.add(meta(frame))

    problems, facts = tally.report()

    assert problems == []
    assert facts["face_clear_share"] == 1.0 and facts["caption_frames"] == 100


def test_a_face_under_the_caption_in_more_than_two_percent_of_frames_fails() -> None:
    tally = validate.CompositionTally(size=SIZE, safe_area=SAFE)
    for frame in range(100):
        faces = ((300, 1300, 500, 1450),) if frame < 5 else ((300, 500, 500, 700),)
        tally.add(meta(frame, faces=faces))

    problems, facts = tally.report()

    assert any("a face is under the caption in 5.0%" in p for p in problems)
    assert facts["face_clear_share"] == 0.95
    assert any("face ['mai'] under the caption" in e for e in facts["examples"])
    # 2 of 100 frames is within the limit
    tally = validate.CompositionTally(size=SIZE, safe_area=SAFE)
    for frame in range(100):
        faces = ((300, 1300, 500, 1450),) if frame < 2 else ((300, 500, 500, 700),)
        tally.add(meta(frame, faces=faces))
    assert tally.report()[0] == []


def test_a_caption_outside_the_safe_area_is_a_problem() -> None:
    tally = validate.CompositionTally(size=SIZE, safe_area=SAFE)
    tally.add(meta(0, caption=(64, 1500, 1000, 1560)))

    problems, _ = tally.report()

    assert any("leaves the safe area in 1 frames" in p for p in problems)


def test_in_world_text_is_small_and_the_characters_dwarf_it() -> None:
    tally = validate.CompositionTally(size=SIZE, safe_area=SAFE)
    tally.add(meta(0, text=((340, 100, 740, 200),)))  # 40 000 px: 1.9% of the frame

    problems, facts = tally.report()

    assert problems == []
    assert facts["max_text_share"] == pytest.approx(40000 / (1080 * 1920), abs=1e-4)
    assert facts["min_character_to_text_area"] == pytest.approx(
        500 * 1100 / 40000, abs=0.1
    )

    big = validate.CompositionTally(size=SIZE, safe_area=SAFE)
    big.add(meta(0, text=((100, 100, 1000, 400),), characters=((300, 600, 500, 800),)))
    problems, _ = big.report()
    assert any("covers 13.0%" in p or "covers 13" in p for p in problems)
    assert any("less than 5x" in p for p in problems)


def test_frames_without_caption_or_text_are_not_counted_against_the_rules() -> None:
    tally = validate.CompositionTally(size=SIZE, safe_area=SAFE)
    tally.add(meta(0, caption=None))

    problems, facts = tally.report()

    assert problems == [] and facts["caption_frames"] == 0 and facts["text_frames"] == 0
    assert facts["min_character_to_text_area"] is None


def test_the_report_fails_only_the_hard_checks_unless_strict() -> None:
    report = validate.ValidationReport()
    report.add("container", [], {"facts": 1})
    report.add("decode", [], {})
    report.add("motion", ["the picture is frozen from 1.00 s to 1.70 s"], {})
    report.add("audio", ["1 clipped samples"], {})

    relaxed, strict = report.as_dict(False), report.as_dict(True)

    assert relaxed["passed"] is True and relaxed["all_checks_passed"] is False
    assert relaxed["warnings"] == [
        "motion: the picture is frozen from 1.00 s to 1.70 s",
        "audio: 1 clipped samples",
    ]
    assert strict["passed"] is False and len(strict["failed"]) == 2
    report.add("decode", ["[h264] error"], {})
    assert report.as_dict(False)["passed"] is False
    assert report.as_dict(False)["failed"] == ["decode: [h264] error"]


# --- real ffmpeg on tiny generated videos -------------------------------------------


def make_video(path: Path, source: str, seconds: float = 2.0) -> Path:
    command = [
        "ffmpeg",
        "-y",
        "-v",
        "error",
        "-f",
        "lavfi",
        "-i",
        f"{source}size=108x192:rate=30:duration={seconds}",
        "-f",
        "lavfi",
        "-i",
        f"sine=frequency=330:sample_rate=48000:duration={seconds}",
        "-c:v",
        "libx264",
        "-profile:v",
        "high",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-ac",
        "2",
        "-shortest",
        "-movflags",
        "+faststart",
        str(path),
    ]
    subprocess.run(command, check=True, capture_output=True)
    return path


@needs_ffmpeg
def test_real_container_checks_find_the_wrong_size(tmp_path: Path) -> None:
    video = make_video(tmp_path / "t.mp4", "testsrc2=")
    probe = validate.probe_media(video, "ffprobe")
    expected = validate.ExpectedMedia(108, 192, 30, 60, 1.0, 5.0)

    problems, facts = validate.check_container(
        probe, expected, faststart=validate.faststart_ok(video)
    )

    assert problems == [], problems
    assert facts["video"]["profile"] == "High" and facts["audio"]["channels"] == 2
    wrong = validate.ExpectedMedia(1080, 1920, 30, 849, 20.0, 30.0)
    problems, _ = validate.check_container(probe, wrong, faststart=True)
    assert any("108x192" in p for p in problems) and any(
        "outside 20-30" in p for p in problems
    )
    assert validate.decode_errors(video) == []


@needs_ffmpeg
def test_real_motion_analysis_tells_a_moving_picture_from_a_still_one(
    tmp_path: Path,
) -> None:
    moving = make_video(tmp_path / "moving.mp4", "testsrc2=")
    still = make_video(tmp_path / "still.mp4", "color=c=0x336699:")

    moving_series = validate.motion_series(moving, tmp_path)
    still_series = validate.motion_series(still, tmp_path)

    assert len(moving_series) >= 55 and len(still_series) >= 55
    assert validate.frozen_stretches(moving_series) == []
    assert validate.frozen_stretches(still_series)
    problems, _ = validate.analyze_motion(still_series, [("s", 0.0, 2.0)])
    assert any("frozen" in p for p in problems) and any(
        "scene 's' moves" in p for p in problems
    )
    problems, _ = validate.analyze_motion(moving_series, [("s", 0.0, 2.0)])
    assert problems == []
    thumb = validate.thumbnail(moving, 1.0)
    assert len(thumb) == validate.THUMB_SIZE[0] * validate.THUMB_SIZE[1] * 3
    different = validate.colour_histogram(validate.thumbnail(still, 1.0))
    assert (
        validate.histogram_distance(validate.colour_histogram(thumb), different) > 0.3
    )
