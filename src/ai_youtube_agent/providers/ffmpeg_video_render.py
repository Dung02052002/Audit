"""ffmpeg video renderer (Prompt Pack v8, prompt #100), context C8.

``FfmpegVideoRenderer`` renders a ``RenderRequest`` with the ``ffmpeg``
command line program (``Settings.render_provider = "ffmpeg"``). There is no
Python dependency: the program is started with ``subprocess``. The rules were
decided by the user on 2026-10-18:

- ``render`` works in a new ``tempfile.TemporaryDirectory`` (prefix
  ``render-``, removed afterwards whatever happens). It writes every input as
  ``<key>.<ext>``, the audio as ``audio.wav``, each text line as ``t<n>.txt``
  (UTF-8 without a BOM), the configured font (``font_file``) as
  ``font0.<ext>`` and the filter graph as ``graph.txt``, then runs ffmpeg in
  that folder with ``-/filter_complex graph.txt`` and reads ``out.mp4``.
  Only these generated plain names appear in the graph and the command line:
  no user text and no path, so nothing needs escaping. The text is read by
  ``drawtext`` from its file with ``expansion=none``. The program is never run
  through a shell and gets no standard input.
- The graph (``build_filter_graph``) starts from a canvas of the background
  colour, overlays every layer in order (``cover`` scales up and crops,
  ``contain`` scales down and centres in the rect; each is shifted to its start
  and enabled between its start and end; a video shorter than its window
  keeps its last frame) and draws every text line (left, centred or right in
  its rect), and ends in ``yuv420p``. Times are written as seconds with three
  decimals from whole milliseconds, never from a float.
- The command (``build_command``) encodes H.264 (``libx264``, preset medium,
  CRF 20, ``yuv420p``) at the request's frame rate and AAC (192 kbit/s,
  48 kHz) for exactly the request's duration, with ``+faststart``, without
  metadata and with ``+bitexact``. The audio of a video input is not used.
- Failures: ffmpeg missing or not startable is ``render.unavailable``, running
  longer than ``timeout_s`` is ``render.timeout``, a non-zero exit is
  ``render.failed`` ("ffmpeg exited with code N"), and a missing, empty or
  unreadable ``out.mp4`` is ``render.output_invalid``. The end of ffmpeg's
  error output (at most 2000 characters) is only logged as a warning; it is
  never put in an error detail or message.
- ``check`` runs ``ffmpeg -hide_banner -version`` (10 s timeout); any failure
  is ``render.unavailable``.
"""

import re
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory

from ai_youtube_agent.core.log import get_logger
from ai_youtube_agent.providers.video_render import (
    IMAGE_MEDIA_TYPES,
    INPUT_EXTENSIONS,
    MP4_MEDIA_TYPE,
    LayerFit,
    RenderedVideo,
    RenderErrorCode,
    RenderRequest,
    TextAlign,
    TextLine,
    VideoRenderError,
    probe_mp4,
)

logger = get_logger(__name__)

Clock = Callable[[], datetime]
Runner = Callable[..., subprocess.CompletedProcess]
CHECK_TIMEOUT_S = 10
STDERR_TAIL = 2000
_FONT_SUFFIX = re.compile(r"\.[a-z0-9]{1,8}")


@dataclass(frozen=True)
class RenderFiles:
    """The plain file names of one render inside its temporary folder."""

    inputs: Mapping[str, str]
    texts: tuple[str, ...]
    font: str | None = None
    audio: str = "audio.wav"
    graph: str = "graph.txt"
    output: str = "out.mp4"


def plan_files(request: RenderRequest, font_file: Path | None = None) -> RenderFiles:
    """The file names of ``request``; ``font_file`` only gives the font extension."""
    font = None
    if font_file is not None:
        suffix = font_file.suffix.lower()
        font = f"font0{suffix}" if _FONT_SUFFIX.fullmatch(suffix) else "font0.ttf"
    return RenderFiles(
        inputs={
            item.key: f"{item.key}.{INPUT_EXTENSIONS[item.media_type]}"
            for item in request.inputs
        },
        texts=tuple(f"t{number}.txt" for number in range(len(request.texts))),
        font=font,
    )


def seconds(ms: int) -> str:
    """Whole milliseconds as seconds with three decimals (``1500`` -> ``1.500``)."""
    return f"{ms // 1000}.{ms % 1000:03d}"


def _hex(colour: str) -> str:
    return "0x" + colour[1:]


def _enable(start_ms: int, end_ms: int) -> str:
    return f"enable='between(t,{seconds(start_ms)},{seconds(end_ms)})'"


def _text_x(line: TextLine) -> str:
    rect = line.rect
    if line.align is TextAlign.LEFT:
        return str(rect.x)
    if line.align is TextAlign.RIGHT:
        return f"{rect.x + rect.width}-text_w"
    return f"{rect.x}+({rect.width}-text_w)/2"


def _drawtext(line: TextLine, file: str, font: str | None) -> str:
    options = [f"textfile={file}", "expansion=none"]
    if font is not None:
        options.append(f"fontfile={font}")
    options += [
        f"fontsize={line.size_px}",
        f"fontcolor={_hex(line.color)}",
        f"borderw={line.stroke_px}",
        f"bordercolor={_hex(line.stroke_color)}",
    ]
    if line.background is not None:
        alpha = "" if len(line.background) == 9 else "FF"
        options += ["box=1", f"boxcolor={_hex(line.background)}{alpha}"]
    options += [
        f"x={_text_x(line)}",
        f"y={line.rect.y}",
        _enable(line.start_ms, line.end_ms),
    ]
    return "drawtext=" + ":".join(options)


def build_filter_graph(request: RenderRequest, files: RenderFiles) -> str:
    """The ``-filter_complex`` graph of ``request``; the output pad is ``vout``."""
    fps = request.fps
    chains = [
        f"color=c={_hex(request.background_color)}:s={request.width}x"
        f"{request.height}:r={fps}:d={seconds(request.duration_ms)}[base]"
    ]
    current = "base"
    for index, layer in enumerate(request.layers):
        rect = layer.rect
        size = f"{rect.width}:{rect.height}"
        if layer.fit is LayerFit.COVER:
            fit = f"scale={size}:force_original_aspect_ratio=increase,crop={size}"
            x, y = str(rect.x), str(rect.y)
        else:
            fit = f"scale={size}:force_original_aspect_ratio=decrease"
            x = f"{rect.x}+({rect.width}-w)/2"
            y = f"{rect.y}+({rect.height}-h)/2"
        chains.append(
            f"[{index}:v]fps={fps},{fit},"
            f"setpts=PTS-STARTPTS+{seconds(layer.start_ms)}/TB[l{index}]"
        )
        chains.append(
            f"[{current}][l{index}]overlay=x={x}:y={y}:"
            f"{_enable(layer.start_ms, layer.end_ms)}:eof_action=repeat[v{index}]"
        )
        current = f"v{index}"
    filters = []
    for line, file in zip(request.texts, files.texts, strict=True):
        font = files.inputs[line.font_key] if line.font_key else files.font
        filters.append(_drawtext(line, file, font))
    filters.append("format=yuv420p")
    chains.append(f"[{current}]" + ",".join(filters) + "[vout]")
    return ";".join(chains)


def build_command(
    request: RenderRequest, files: RenderFiles, *, binary: str = "ffmpeg"
) -> list[str]:
    """The ffmpeg arguments of ``request``; input ``n`` is layer ``n``, then audio."""
    command = [binary, "-nostdin", "-hide_banner", "-loglevel", "error", "-y"]
    for layer in request.layers:
        source = request.input(layer.input_key)
        file = files.inputs[layer.input_key]
        span = layer.end_ms - layer.start_ms
        if source.media_type in IMAGE_MEDIA_TYPES:
            command += ["-loop", "1", "-framerate", str(request.fps)]
            command += ["-t", seconds(span), "-i", file]
            continue
        start = layer.source_in_ms or 0
        if layer.source_out_ms is not None:
            span = min(span, layer.source_out_ms - start)
        if layer.source_in_ms is not None:
            command += ["-ss", seconds(layer.source_in_ms)]
        command += ["-t", seconds(span), "-i", file]
    command += ["-i", files.audio]
    # ``-/opt file`` reads the option value from a file (ffmpeg 7.0 or later;
    # ffmpeg 8 removed ``-filter_complex_script``).
    command += ["-/filter_complex", files.graph]
    command += ["-map", "[vout]", "-map", f"{len(request.layers)}:a"]
    command += ["-c:v", "libx264", "-preset", "medium", "-crf", "20"]
    command += ["-pix_fmt", "yuv420p", "-r", str(request.fps)]
    command += ["-c:a", "aac", "-b:a", "192k", "-ar", "48000"]
    command += ["-t", seconds(request.duration_ms), "-movflags", "+faststart"]
    command += ["-map_metadata", "-1", "-fflags", "+bitexact", files.output]
    return command


class FfmpegVideoRenderer:
    def __init__(
        self,
        *,
        binary: str = "ffmpeg",
        timeout_s: int = 600,
        font_file: Path | None = None,
        name: str = "ffmpeg",
        clock: Clock | None = None,
        runner: Runner = subprocess.run,
    ) -> None:
        if not isinstance(binary, str) or not binary.strip():
            raise ValueError("binary must not be empty")
        if isinstance(timeout_s, bool) or not isinstance(timeout_s, int | float):
            raise ValueError("timeout_s must be a number of seconds")
        if timeout_s <= 0:
            raise ValueError("timeout_s must be more than 0")
        self.name = name
        self._binary = binary
        self._timeout_s = timeout_s
        self._font_file = Path(font_file) if font_file is not None else None
        self._clock = clock
        self._runner = runner

    def render(self, request: RenderRequest) -> RenderedVideo:
        files = plan_files(request, self._font_file)
        with TemporaryDirectory(prefix="render-") as folder:
            root = Path(folder)
            self._prepare(root, request, files)
            completed = self._run(
                build_command(request, files, binary=self._binary), root
            )
            if completed.returncode != 0:
                self._log_failure(completed)
                raise VideoRenderError(
                    RenderErrorCode.FAILED,
                    f"ffmpeg exited with code {completed.returncode}",
                    provider=self.name,
                )
            data = self._output(root / files.output)
        try:
            info = probe_mp4(data)
        except ValueError:
            raise VideoRenderError(
                RenderErrorCode.OUTPUT_INVALID,
                "ffmpeg wrote a file that is not an MP4",
                provider=self.name,
            ) from None
        return RenderedVideo(
            data=data,
            media_type=MP4_MEDIA_TYPE,
            duration_ms=max(1, info.duration_ms),
            width=request.width,
            height=request.height,
            fps=request.fps,
            provider=self.name,
            generated_at=self._clock() if self._clock else datetime.now(UTC),
        )

    def check(self) -> None:
        try:
            completed = self._runner(
                [self._binary, "-hide_banner", "-version"],
                capture_output=True,
                stdin=subprocess.DEVNULL,
                timeout=CHECK_TIMEOUT_S,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            completed = None
        if completed is None or completed.returncode != 0:
            raise VideoRenderError(
                RenderErrorCode.UNAVAILABLE,
                "ffmpeg -version did not succeed",
                provider=self.name,
            )

    def _prepare(self, root: Path, request: RenderRequest, files: RenderFiles) -> None:
        """Write every file of the render into ``root``."""
        try:
            for item in request.inputs:
                (root / files.inputs[item.key]).write_bytes(item.data)
            (root / files.audio).write_bytes(request.audio.data)
            for line, file in zip(request.texts, files.texts, strict=True):
                (root / file).write_bytes(line.text.encode("utf-8"))
            if files.font is not None and self._font_file is not None:
                (root / files.font).write_bytes(self._font_file.read_bytes())
            graph = build_filter_graph(request, files)
            (root / files.graph).write_bytes(graph.encode("utf-8"))
        except OSError:
            raise VideoRenderError(
                RenderErrorCode.UNAVAILABLE,
                "the render folder could not be prepared",
                provider=self.name,
            ) from None

    def _run(self, command: list[str], root: Path) -> subprocess.CompletedProcess:
        try:
            return self._runner(
                command,
                cwd=root,
                capture_output=True,
                stdin=subprocess.DEVNULL,
                timeout=self._timeout_s,
                check=False,
            )
        except (FileNotFoundError, PermissionError):
            raise VideoRenderError(
                RenderErrorCode.UNAVAILABLE,
                "ffmpeg could not be started",
                provider=self.name,
            ) from None
        except subprocess.TimeoutExpired:
            raise VideoRenderError(
                RenderErrorCode.TIMEOUT,
                f"ffmpeg ran longer than {self._timeout_s} s",
                provider=self.name,
            ) from None

    def _output(self, path: Path) -> bytes:
        try:
            data = path.read_bytes() if path.is_file() else b""
        except OSError:
            data = b""
        if not data:
            raise VideoRenderError(
                RenderErrorCode.OUTPUT_INVALID,
                "ffmpeg wrote no output file",
                provider=self.name,
            )
        return data

    def _log_failure(self, completed: subprocess.CompletedProcess) -> None:
        error = completed.stderr or b""
        if isinstance(error, bytes):
            error = error.decode("utf-8", errors="replace")
        logger.warning(
            "ffmpeg render failed",
            extra={
                "fields": {
                    "provider": self.name,
                    "returncode": completed.returncode,
                    "stderr_tail": error[-STDERR_TAIL:],
                }
            },
        )
