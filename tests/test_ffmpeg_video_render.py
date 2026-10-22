"""I-100 ffmpeg video renderer (Prompt Pack v8, prompt #100).

``FfmpegVideoRenderer`` runs the ffmpeg command line program; these tests use
a fake runner, so ffmpeg is not needed except for the last ones, which are
skipped when ffmpeg (or a font file) is missing:

- the command: the binary first, no shell, run in the temporary folder with no
  standard input, the graph from ``graph.txt``, H.264 and AAC for exactly the
  request's duration into ``out.mp4``;
- the files: every input as ``<key>.<ext>``, the audio as ``audio.wav``, one
  UTF-8 text file per line, the configured font as ``font0.<ext>``;
- the graph: cover and contain layers, ``between`` windows from whole
  milliseconds, ``drawtext`` from a text file with ``expansion=none``, the
  three alignments;
- the errors: a missing program, a timeout, a non-zero exit (ffmpeg's error
  output never reaches the error) and a missing or invalid output; the folder
  is removed whatever happens;
- ``check`` runs ``ffmpeg -hide_banner -version``.
"""

import io
import logging
import shutil
import struct
import subprocess
import unicodedata
import wave
import zlib
from pathlib import Path

import pytest

from ai_youtube_agent.providers.ffmpeg_video_render import (
    CHECK_TIMEOUT_S,
    FfmpegVideoRenderer,
    build_command,
    build_filter_graph,
    plan_files,
    seconds,
)
from ai_youtube_agent.providers.mock_video_render import stub_mp4
from ai_youtube_agent.providers.video_render import (
    AUDIO_KEY,
    LayerFit,
    PixelRect,
    RenderErrorCode,
    RenderInput,
    TextAlign,
    VideoRenderer,
    VideoRenderError,
    probe_mp4,
)
from test_video_render import (
    SECRET,
    audio,
    image,
    layer,
    line,
    rect,
    request,
)

VIET = unicodedata.normalize("NFC", "Tiếng Việt")


class FakeRunner:
    """Records each call and plays ffmpeg: writes ``out.mp4`` into ``cwd``."""

    def __init__(
        self,
        *,
        returncode: int = 0,
        output: bytes | None = None,
        stderr: bytes = b"",
        raises: BaseException | None = None,
    ) -> None:
        self.returncode = returncode
        self.output = output
        self.stderr = stderr
        self.raises = raises
        self.calls: list[tuple[list[str], dict]] = []
        self.seen: dict[str, bytes] = {}

    def __call__(self, argv, **kwargs):
        self.calls.append((list(argv), kwargs))
        cwd = kwargs.get("cwd")
        if cwd is not None:
            folder = Path(cwd)
            self.seen = {p.name: p.read_bytes() for p in folder.iterdir()}
        if self.raises is not None:
            raise self.raises
        if cwd is not None and self.output is not None:
            (Path(cwd) / "out.mp4").write_bytes(self.output)
        return subprocess.CompletedProcess(
            argv, self.returncode, stdout=b"", stderr=self.stderr
        )

    @property
    def argv(self) -> list[str]:
        return self.calls[-1][0]

    @property
    def kwargs(self) -> dict:
        return self.calls[-1][1]


def renderer(runner: FakeRunner, **kwargs) -> FfmpegVideoRenderer:
    return FfmpegVideoRenderer(runner=runner, **kwargs)


def ok_runner(duration_ms: int = 1000) -> FakeRunner:
    return FakeRunner(output=stub_mp4(duration_ms, b"ffmpeg output"))


def value_after(argv: list[str], flag: str) -> str:
    return argv[argv.index(flag) + 1]


# Pure helpers


@pytest.mark.parametrize(
    "ms, text",
    [(0, "0.000"), (7, "0.007"), (1500, "1.500"), (61001, "61.001")],
)
def test_seconds_come_from_whole_milliseconds(ms, text) -> None:
    assert seconds(ms) == text


def test_the_file_names_are_generated_plain_names() -> None:
    font = RenderInput.of("f1", b"font", "font/otf")
    built = request(inputs=(image(), font), texts=(line(), line(text="Two")))

    files = plan_files(built, Path("C:/Fonts/My Font.TTF"))

    assert dict(files.inputs) == {"v1": "v1.png", "f1": "f1.otf"}
    assert files.texts == ("t0.txt", "t1.txt")
    assert files.font == "font0.ttf"
    assert (files.audio, files.graph, files.output) == (
        "audio.wav",
        "graph.txt",
        "out.mp4",
    )
    assert plan_files(built).font is None
    assert plan_files(built, Path("odd.font file")).font == "font0.ttf"


def test_the_graph_of_a_full_canvas_image_and_one_line() -> None:
    built = request()

    graph = build_filter_graph(built, plan_files(built))

    assert graph == (
        "color=c=0x000000:s=720x1280:r=30:d=1.000[base];"
        "[0:v]fps=30,scale=720:1280:force_original_aspect_ratio=increase,"
        "crop=720:1280,setpts=PTS-STARTPTS+0.000/TB[l0];"
        "[base][l0]overlay=x=0:y=0:enable='between(t,0.000,1.000)':"
        "eof_action=repeat[v0];"
        "[v0]drawtext=textfile=t0.txt:expansion=none:fontsize=64:"
        "fontcolor=0xFFFFFF:borderw=4:bordercolor=0x000000:"
        "x=0+(720-text_w)/2:y=900:enable='between(t,0.000,1.000)',"
        "format=yuv420p[vout]"
    )


def test_a_contained_layer_is_scaled_down_and_centred_in_its_rect() -> None:
    contained = layer(
        fit=LayerFit.CONTAIN,
        rect=PixelRect(100, 200, 400, 300),
        start_ms=250,
        end_ms=750,
    )
    built = request(layers=(contained,), texts=())

    graph = build_filter_graph(built, plan_files(built))

    assert (
        "[0:v]fps=30,scale=400:300:force_original_aspect_ratio=decrease,"
        "setpts=PTS-STARTPTS+0.250/TB[l0]"
    ) in graph
    assert (
        "overlay=x=100+(400-w)/2:y=200+(300-h)/2:"
        "enable='between(t,0.250,0.750)':eof_action=repeat[v0]"
    ) in graph
    assert graph.endswith("[v0]format=yuv420p[vout]")


def test_layers_are_chained_bottom_first() -> None:
    second = RenderInput.of("v2", b"second image", "image/jpeg")
    built = request(
        inputs=(image(), second),
        layers=(layer(), layer(input_key="v2", start_ms=500)),
        texts=(),
    )

    graph = build_filter_graph(built, plan_files(built))

    assert "[base][l0]overlay" in graph
    assert "[v0][l1]overlay" in graph
    assert "[1:v]fps=30" in graph and "+0.500/TB[l1]" in graph
    assert graph.endswith("[v1]format=yuv420p[vout]")


def test_a_request_without_layers_or_texts_is_the_background() -> None:
    built = request(inputs=(), layers=(), texts=(), background_color="#112233")

    graph = build_filter_graph(built, plan_files(built))

    assert graph == (
        "color=c=0x112233:s=720x1280:r=30:d=1.000[base];[base]format=yuv420p[vout]"
    )


@pytest.mark.parametrize(
    "align, x",
    [
        (TextAlign.LEFT, "x=40:"),
        (TextAlign.CENTER, "x=40+(600-text_w)/2:"),
        (TextAlign.RIGHT, "x=640-text_w:"),
    ],
)
def test_text_lines_are_aligned_in_their_rect(align, x) -> None:
    drawn = line(align=align, rect=PixelRect(40, 100, 600, 77))
    built = request(texts=(drawn,))

    assert x in build_filter_graph(built, plan_files(built))


@pytest.mark.parametrize(
    "background, expected",
    [("#00000080", "box=1:boxcolor=0x00000080"), ("#112233", "boxcolor=0x112233FF")],
)
def test_a_text_background_is_a_box(background, expected) -> None:
    built = request(texts=(line(background=background),))

    assert expected in build_filter_graph(built, plan_files(built))


def test_text_fonts_come_from_the_font_input_or_the_configured_font() -> None:
    font = RenderInput.of("f1", b"font", "font/ttf")
    built = request(
        inputs=(image(), font),
        texts=(line(font_key="f1"), line(text="Default font")),
    )

    graph = build_filter_graph(built, plan_files(built, Path("captions.otf")))

    assert "textfile=t0.txt:expansion=none:fontfile=f1.ttf:" in graph
    assert "textfile=t1.txt:expansion=none:fontfile=font0.otf:" in graph


def test_without_any_font_drawtext_uses_its_default() -> None:
    built = request()

    assert "fontfile" not in build_filter_graph(built, plan_files(built))


def test_the_text_never_appears_in_the_graph_or_the_command() -> None:
    built = request(texts=(line(text=SECRET),))
    files = plan_files(built)

    assert SECRET not in build_filter_graph(built, files)
    assert SECRET not in " ".join(build_command(built, files))


def test_the_command_of_an_image_layer() -> None:
    built = request()

    argv = build_command(built, plan_files(built), binary="/opt/ffmpeg")

    assert argv[:6] == [
        "/opt/ffmpeg",
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
    ]
    assert argv[6:14] == [
        "-loop",
        "1",
        "-framerate",
        "30",
        "-t",
        "1.000",
        "-i",
        "v1.png",
    ]
    assert argv[14:16] == ["-i", "audio.wav"]
    assert value_after(argv, "-/filter_complex") == "graph.txt"
    assert argv[argv.index("-map") : argv.index("-map") + 4] == [
        "-map",
        "[vout]",
        "-map",
        "1:a",
    ]
    assert value_after(argv, "-c:v") == "libx264"
    assert value_after(argv, "-preset") == "medium"
    assert value_after(argv, "-crf") == "20"
    assert value_after(argv, "-pix_fmt") == "yuv420p"
    assert value_after(argv, "-r") == "30"
    assert value_after(argv, "-c:a") == "aac"
    assert value_after(argv, "-b:a") == "192k"
    assert value_after(argv, "-ar") == "48000"
    assert argv[-9:] == [
        "-t",
        "1.000",
        "-movflags",
        "+faststart",
        "-map_metadata",
        "-1",
        "-fflags",
        "+bitexact",
        "out.mp4",
    ]


def test_the_command_of_a_video_layer_uses_its_source_window() -> None:
    clip = RenderInput.of("v1", b"clip bytes", "video/mp4")
    built = request(
        inputs=(clip,),
        layers=(layer(source_in_ms=2000, source_out_ms=2600),),
        texts=(),
    )

    argv = build_command(built, plan_files(built))

    assert argv[6:12] == ["-ss", "2.000", "-t", "0.600", "-i", "v1.mp4"]
    assert "-loop" not in argv


def test_a_video_layer_without_a_window_plays_for_its_span() -> None:
    clip = RenderInput.of("v1", b"clip bytes", "video/webm")
    built = request(inputs=(clip,), layers=(layer(end_ms=800),), texts=())

    argv = build_command(built, plan_files(built))

    assert argv[6:10] == ["-t", "0.800", "-i", "v1.webm"]
    assert "-ss" not in argv


# Rendering with a fake runner


def test_a_render_runs_ffmpeg_in_its_folder_without_a_shell() -> None:
    runner = ok_runner()

    result = renderer(runner, binary="ffmpeg-7", timeout_s=42).render(request())

    argv, kwargs = runner.calls[0]
    assert argv[0] == "ffmpeg-7"
    assert "shell" not in kwargs
    assert kwargs["capture_output"] is True
    assert kwargs["stdin"] is subprocess.DEVNULL
    assert kwargs["timeout"] == 42
    assert kwargs["check"] is False
    assert Path(kwargs["cwd"]).name.startswith("render-")
    assert result.data == stub_mp4(1000, b"ffmpeg output")
    assert result.duration_ms == probe_mp4(result.data).duration_ms == 1000
    assert (result.width, result.height, result.fps) == (720, 1280, 30)
    assert result.provider == "ffmpeg"


def test_every_file_is_written_before_ffmpeg_runs() -> None:
    runner = ok_runner()
    built = request(texts=(line(text=VIET),))

    renderer(runner).render(built)

    seen = runner.seen
    assert seen["v1.png"] == built.input("v1").data
    assert seen["audio.wav"] == built.input(AUDIO_KEY).data
    assert seen["t0.txt"] == VIET.encode("utf-8")
    assert not seen["t0.txt"].startswith(b"\xef\xbb\xbf")
    assert seen["graph.txt"].decode("utf-8") == build_filter_graph(
        built, plan_files(built)
    )
    assert "out.mp4" not in seen


def test_the_configured_font_is_copied_into_the_folder(tmp_path: Path) -> None:
    font_file = tmp_path / "captions.ttf"
    font_file.write_bytes(b"my font")
    runner = ok_runner()

    renderer(runner, font_file=font_file).render(request())

    assert runner.seen["font0.ttf"] == b"my font"
    assert b"fontfile=font0.ttf" in runner.seen["graph.txt"]


def test_a_missing_font_file_is_unavailable(tmp_path: Path) -> None:
    runner = ok_runner()

    with pytest.raises(VideoRenderError) as info:
        renderer(runner, font_file=tmp_path / "gone.ttf").render(request())

    assert info.value.render_code is RenderErrorCode.UNAVAILABLE
    assert runner.calls == []


def test_the_folder_is_removed_after_a_render() -> None:
    runner = ok_runner()

    renderer(runner).render(request())

    assert not Path(runner.kwargs["cwd"]).exists()


def test_a_failing_exit_hides_the_error_output(
    caplog: pytest.LogCaptureFixture,
) -> None:
    runner = FakeRunner(returncode=1, stderr=f"boom {SECRET}".encode())

    with caplog.at_level(logging.WARNING), pytest.raises(VideoRenderError) as info:
        renderer(runner).render(request())

    error = info.value
    assert error.render_code is RenderErrorCode.FAILED
    assert error.detail == "ffmpeg exited with code 1"
    assert error.retryable is False
    for shown in (str(error), error.user_message, str(error.to_public().as_dict())):
        assert SECRET not in shown
    (record,) = [r for r in caplog.records if r.getMessage() == "ffmpeg render failed"]
    assert SECRET in record.fields["stderr_tail"]
    assert record.fields["returncode"] == 1
    assert not Path(runner.kwargs["cwd"]).exists()


def test_only_the_end_of_a_long_error_output_is_logged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    runner = FakeRunner(returncode=2, stderr=b"a" * 5000 + b"THE END")

    with caplog.at_level(logging.WARNING), pytest.raises(VideoRenderError):
        renderer(runner).render(request())

    (record,) = [r for r in caplog.records if r.getMessage() == "ffmpeg render failed"]
    assert len(record.fields["stderr_tail"]) == 2000
    assert record.fields["stderr_tail"].endswith("THE END")


@pytest.mark.parametrize(
    "raised", [FileNotFoundError("ffmpeg"), PermissionError("denied")]
)
def test_a_program_that_cannot_start_is_unavailable(raised) -> None:
    runner = FakeRunner(raises=raised)

    with pytest.raises(VideoRenderError) as info:
        renderer(runner).render(request())

    assert info.value.render_code is RenderErrorCode.UNAVAILABLE
    assert info.value.retryable is True
    assert not Path(runner.kwargs["cwd"]).exists()


def test_a_render_that_runs_too_long_is_a_timeout() -> None:
    runner = FakeRunner(raises=subprocess.TimeoutExpired(["ffmpeg"], 5))

    with pytest.raises(VideoRenderError) as info:
        renderer(runner, timeout_s=5).render(request())

    assert info.value.render_code is RenderErrorCode.TIMEOUT
    assert info.value.retryable is True


@pytest.mark.parametrize("output", [None, b"", b"not an mp4"])
def test_a_missing_or_invalid_output_is_refused(output) -> None:
    runner = FakeRunner(output=output)

    with pytest.raises(VideoRenderError) as info:
        renderer(runner).render(request())

    assert info.value.render_code is RenderErrorCode.OUTPUT_INVALID
    assert not Path(runner.kwargs["cwd"]).exists()


@pytest.mark.parametrize(
    "kwargs", [{"binary": ""}, {"binary": "  "}, {"timeout_s": 0}, {"timeout_s": True}]
)
def test_the_renderer_refuses_invalid_settings(kwargs) -> None:
    with pytest.raises(ValueError):
        FfmpegVideoRenderer(**kwargs)


def test_the_renderer_implements_the_protocol() -> None:
    assert isinstance(FfmpegVideoRenderer(), VideoRenderer)
    assert FfmpegVideoRenderer().name == "ffmpeg"


# Health check


def test_check_asks_ffmpeg_for_its_version() -> None:
    runner = FakeRunner()

    renderer(runner, binary="ffmpeg-7").check()

    assert runner.argv == ["ffmpeg-7", "-hide_banner", "-version"]
    assert runner.kwargs["timeout"] == CHECK_TIMEOUT_S == 10
    assert runner.kwargs["stdin"] is subprocess.DEVNULL
    assert "shell" not in runner.kwargs


@pytest.mark.parametrize(
    "runner",
    [
        FakeRunner(returncode=1, stderr=SECRET.encode()),
        FakeRunner(raises=FileNotFoundError("ffmpeg")),
        FakeRunner(raises=subprocess.TimeoutExpired(["ffmpeg"], 10)),
    ],
)
def test_a_failing_check_is_unavailable(runner) -> None:
    with pytest.raises(VideoRenderError) as info:
        renderer(runner).check()

    assert info.value.render_code is RenderErrorCode.UNAVAILABLE
    assert SECRET not in str(info.value)


# The real program (skipped without ffmpeg)

needs_ffmpeg = pytest.mark.skipif(
    shutil.which("ffmpeg") is None, reason="ffmpeg is not installed"
)
FONT_CANDIDATES = (
    Path("C:/Windows/Fonts/arial.ttf"),
    Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
    Path("/System/Library/Fonts/Supplemental/Arial.ttf"),
)


def png(width: int, height: int, rgb: tuple[int, int, int]) -> bytes:
    """A plain RGB PNG built with the standard library."""

    def chunk(kind: bytes, data: bytes) -> bytes:
        crc = zlib.crc32(kind + data) & 0xFFFFFFFF
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", crc)

    rows = b"".join(b"\x00" + bytes(rgb) * width for _ in range(height))
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(rows))
        + chunk(b"IEND", b"")
    )


def wav(duration_ms: int, rate: int = 48000) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(rate)
        writer.writeframes(b"\x00\x00" * (rate * duration_ms // 1000))
    return buffer.getvalue()


def real_request(**changes):
    picture = RenderInput.of("v1", png(64, 64, (200, 40, 40)), "image/png")
    values = {
        "audio": audio(wav(1000)),
        "inputs": (picture,),
        "layers": (layer(),),
        "texts": (),
    }
    return request(**{**values, **changes})


@needs_ffmpeg
def test_ffmpeg_renders_an_image_and_audio_into_an_mp4() -> None:
    result = FfmpegVideoRenderer(timeout_s=120).render(real_request())

    info = probe_mp4(result.data)
    assert abs(info.duration_ms - 1000) <= 250
    assert "isom" in info.brands


@needs_ffmpeg
def test_ffmpeg_burns_in_a_caption_with_a_font_file() -> None:
    font = next((path for path in FONT_CANDIDATES if path.is_file()), None)
    if font is None:
        pytest.skip("no font file found")
    caption = line(text=VIET, rect=rect(y=900, height=77))

    result = FfmpegVideoRenderer(timeout_s=120, font_file=font).render(
        real_request(texts=(caption,))
    )

    assert abs(probe_mp4(result.data).duration_ms - 1000) <= 250
