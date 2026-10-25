"""Encode rendered frames (and the mixed audio) to an H.264/AAC MP4 with ffmpeg.

Frames are written to ffmpeg's stdin one at a time as raw RGB, never kept in
memory. The video is High profile level 4.2, yuv420p with BT.709 tags, a
keyframe every two seconds at 30 fps and ``+faststart``; the audio is AAC at
48 kHz stereo. No filter takes a file path, so there is nothing to escape on
Windows.
"""

from __future__ import annotations

import contextlib
import subprocess
import time
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from ai_youtube_agent.animation.errors import AnimationError

CRF: Final[int] = 18
PRESET: Final[str] = "medium"
GOP: Final[int] = 60
AUDIO_BITRATE: Final[str] = "192k"
AUDIO_RATE: Final[int] = 48000
BT709_FILTER: Final[str] = "scale=out_color_matrix=bt709:out_range=tv,format=yuv420p"


@dataclass(frozen=True)
class EncodeResult:
    path: Path
    frames: int
    seconds: float
    command: tuple[str, ...]


def build_ffmpeg_command(
    out: Path,
    *,
    size: tuple[int, int],
    fps: int,
    audio: Path | None = None,
    ffmpeg_binary: str = "ffmpeg",
    crf: int = CRF,
    preset: str = PRESET,
    gop: int = GOP,
    pad_audio: bool = False,
) -> list[str]:
    """The ffmpeg command for raw RGB frames on stdin and an optional audio file."""
    width, height = size
    command = [
        ffmpeg_binary,
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "rawvideo",
        "-pix_fmt",
        "rgb24",
        "-s",
        f"{width}x{height}",
        "-framerate",
        str(fps),
        "-i",
        "-",
    ]
    if audio is not None:
        command += ["-i", str(audio)]
    command += [
        "-vf",
        BT709_FILTER,
        "-c:v",
        "libx264",
        "-profile:v",
        "high",
        "-level:v",
        "4.2",
        "-preset",
        preset,
        "-crf",
        str(crf),
        "-g",
        str(gop),
        "-r",
        str(fps),
        "-colorspace",
        "bt709",
        "-color_primaries",
        "bt709",
        "-color_trc",
        "bt709",
        "-color_range",
        "tv",
    ]
    if audio is not None:
        if pad_audio:
            command += ["-af", "apad"]
        command += [
            "-c:a",
            "aac",
            "-b:a",
            AUDIO_BITRATE,
            "-ar",
            str(AUDIO_RATE),
            "-ac",
            "2",
            "-shortest",
        ]
    else:
        command += ["-an"]
    command += ["-movflags", "+faststart", str(out)]
    return command


def _frame_bytes(frame: Any, size: tuple[int, int]) -> bytes:
    if isinstance(frame, bytes | bytearray):
        expected = size[0] * size[1] * 3
        if len(frame) != expected:
            raise AnimationError(
                f"a raw frame has {len(frame)} bytes, expected {expected}"
            )
        return bytes(frame)
    if tuple(frame.size) != tuple(size):
        raise AnimationError(f"a frame is {frame.size}, not {size}")
    return frame.convert("RGB").tobytes()


def encode_frames(
    frames: Iterable[Any],
    out: Path,
    *,
    size: tuple[int, int],
    fps: int,
    audio: Path | None = None,
    ffmpeg_binary: str = "ffmpeg",
    log_path: Path | None = None,
    crf: int = CRF,
    preset: str = PRESET,
    pad_audio: bool = False,
) -> EncodeResult:
    """Stream ``frames`` (PIL images or raw RGB bytes) into an MP4 at ``out``.

    A failing ffmpeg raises ``AnimationError`` with the tail of its log; an
    exception while rendering a frame kills ffmpeg and propagates.
    """
    command = build_ffmpeg_command(
        out,
        size=size,
        fps=fps,
        audio=audio,
        ffmpeg_binary=ffmpeg_binary,
        crf=crf,
        preset=preset,
        pad_audio=pad_audio,
    )
    started = time.monotonic()
    log_file = open(log_path, "ab") if log_path is not None else subprocess.DEVNULL  # noqa: SIM115
    written = 0
    try:
        try:
            proc = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=log_file,
            )
        except OSError as error:
            raise AnimationError(f"ffmpeg could not be started: {error}") from error
        try:
            for frame in frames:
                assert proc.stdin is not None
                proc.stdin.write(_frame_bytes(frame, size))
                written += 1
        except BrokenPipeError:
            pass
        except BaseException:
            proc.kill()
            proc.wait()
            raise
        finally:
            if proc.stdin and not proc.stdin.closed:
                with contextlib.suppress(BrokenPipeError):
                    proc.stdin.close()
        code = proc.wait()
    finally:
        if log_path is not None:
            log_file.close()
    if code != 0 or not out.exists():
        tail = ""
        if log_path is not None and log_path.exists():
            tail = log_path.read_text(encoding="utf-8", errors="replace")[-600:]
        raise AnimationError(
            f"ffmpeg exited with {code} after {written} frames: {tail}"
        )
    if written == 0:
        raise AnimationError("no frames were rendered")
    return EncodeResult(out, written, time.monotonic() - started, tuple(command))
