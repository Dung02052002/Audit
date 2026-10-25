"""MP4 encoding: the ffmpeg command and a small real encode."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from ai_youtube_agent.animation.encode import build_ffmpeg_command, encode_frames
from ai_youtube_agent.animation.errors import AnimationError

FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")


def test_the_command_has_the_delivery_settings() -> None:
    command = build_ffmpeg_command(
        Path("out.mp4"), size=(1080, 1920), fps=30, audio=Path("mix.wav")
    )
    text = " ".join(command)

    assert "-s 1080x1920" in text and "-f rawvideo" in text and "-pix_fmt rgb24" in text
    assert (
        "-c:v libx264" in text and "-profile:v high" in text and "-level:v 4.2" in text
    )
    assert "-crf 18" in text and "-g 60" in text and "-preset medium" in text
    assert "scale=out_color_matrix=bt709" in text and "-colorspace bt709" in text
    assert "-c:a aac" in text and "-b:a 192k" in text and "-ar 48000" in text
    assert "-ac 2" in text and "-shortest" in text and "-movflags +faststart" in text
    assert command[-1] == "out.mp4"


def test_without_audio_the_command_drops_the_audio_track() -> None:
    command = build_ffmpeg_command(Path("out.mp4"), size=(64, 112), fps=30)

    assert "-an" in command and "-c:a" not in command and "mix.wav" not in command


@pytest.mark.skipif(FFMPEG is None, reason="ffmpeg is not installed")
def test_a_frame_of_the_wrong_size_is_refused(tmp_path) -> None:
    with pytest.raises(AnimationError, match="raw frame"):
        encode_frames(
            [b"\x00" * 10],
            tmp_path / "x.mp4",
            size=(64, 112),
            fps=30,
            ffmpeg_binary=FFMPEG or "ffmpeg",
        )


def test_a_missing_ffmpeg_is_an_animation_error(tmp_path) -> None:
    with pytest.raises(AnimationError, match="could not be started"):
        encode_frames(
            [bytes(64 * 112 * 3)],
            tmp_path / "x.mp4",
            size=(64, 112),
            fps=30,
            ffmpeg_binary="ffmpeg-missing",
        )


@pytest.mark.skipif(FFMPEG is None, reason="ffmpeg is not installed")
def test_a_small_real_encode_has_the_expected_frames(tmp_path) -> None:
    width, height, count = 64, 112, 12
    frames = (bytes([(i * 20) % 256, 80, 160]) * (width * height) for i in range(count))
    out = tmp_path / "small.mp4"

    result = encode_frames(
        frames,
        out,
        size=(width, height),
        fps=30,
        ffmpeg_binary=FFMPEG or "ffmpeg",
        log_path=tmp_path / "log.txt",
    )

    assert result.frames == count and out.stat().st_size > 500
    if FFPROBE:
        probe = subprocess.run(
            [
                FFPROBE,
                "-v",
                "error",
                "-count_frames",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=codec_name,profile,width,height,nb_read_frames,pix_fmt,color_space",
                "-of",
                "json",
                str(out),
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        stream = json.loads(probe.stdout)["streams"][0]
        assert stream["codec_name"] == "h264" and stream["profile"] == "High"
        assert (stream["width"], stream["height"]) == (width, height)
        assert stream["nb_read_frames"] == str(count) and stream["pix_fmt"] == "yuv420p"


@pytest.mark.skipif(FFMPEG is None, reason="ffmpeg is not installed")
def test_a_failing_frame_source_kills_ffmpeg_and_propagates(tmp_path) -> None:
    def frames():
        yield bytes(64 * 112 * 3)
        raise RuntimeError("render failed")

    with pytest.raises(RuntimeError, match="render failed"):
        encode_frames(
            frames(),
            tmp_path / "x.mp4",
            size=(64, 112),
            fps=30,
            ffmpeg_binary=FFMPEG or "ffmpeg",
        )


@pytest.mark.skipif(FFMPEG is None, reason="ffmpeg is not installed")
def test_no_frames_is_an_error(tmp_path) -> None:
    with pytest.raises(AnimationError):
        encode_frames(
            [],
            tmp_path / "x.mp4",
            size=(64, 112),
            fps=30,
            ffmpeg_binary=FFMPEG or "ffmpeg",
        )
