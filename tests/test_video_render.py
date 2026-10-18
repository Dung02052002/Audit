"""I-100 Render provider (Prompt Pack v8, prompt #100).

The video renderer interface (``VideoRenderer``) and its deterministic mock:

- ``RenderInput``, ``PixelRect``, ``VisualLayer``, ``TextLine``,
  ``RenderRequest`` and ``RenderedVideo`` refuse invalid values;
- ``describe()`` is stable and holds no bytes;
- ``RenderErrorCode`` says which failures are retryable and the user messages
  never hold a detail;
- ``probe_mp4`` reads the brands and duration of an MP4 container and refuses
  anything else;
- the mock answers a probe-valid stub MP4 chosen by the request, can be
  scripted (queued answers, failures, health) and records its calls.
"""

import dataclasses
import hashlib
import struct
import unicodedata
from datetime import UTC, datetime, timedelta, timezone

import pytest

from ai_youtube_agent.core.errors import ProviderError, to_public
from ai_youtube_agent.providers.mock_video_render import (
    MDAT_SIZE,
    MockVideoRenderer,
    request_digest,
    stub_mp4,
)
from ai_youtube_agent.providers.video_render import (
    AUDIO_KEY,
    MAX_DURATION_MS,
    MP4_MEDIA_TYPE,
    RETRYABLE_CODES,
    USER_MESSAGES,
    LayerFit,
    Mp4Info,
    PixelRect,
    RenderedVideo,
    RenderErrorCode,
    RenderInput,
    RenderRequest,
    TextAlign,
    TextLine,
    VideoRenderer,
    VideoRenderError,
    VisualLayer,
    probe_mp4,
)

T0 = datetime(2026, 10, 18, 12, 0, tzinfo=UTC)
SECRET = "Planted caption 5d1e that must stay private."
WAV = b"RIFF fake wav bytes"
PNG = b"\x89PNG fake image"
TTF = b"fake font bytes"


def audio(data: bytes = WAV) -> RenderInput:
    return RenderInput.of(AUDIO_KEY, data, "audio/wav")


def image(key: str = "v1", data: bytes = PNG) -> RenderInput:
    return RenderInput.of(key, data, "image/png")


def rect(**changes) -> PixelRect:
    return PixelRect(**{"x": 0, "y": 0, "width": 720, "height": 1280, **changes})


def layer(**changes) -> VisualLayer:
    values = {"input_key": "v1", "start_ms": 0, "end_ms": 1000, "rect": rect()}
    return VisualLayer(**{**values, **changes})


def line(**changes) -> TextLine:
    values = {
        "text": "Hello there",
        "start_ms": 0,
        "end_ms": 1000,
        "rect": rect(y=900, height=77),
    }
    return TextLine(**{**values, **changes})


def request(**changes) -> RenderRequest:
    values = {
        "width": 720,
        "height": 1280,
        "fps": 30,
        "duration_ms": 1000,
        "audio": audio(),
        "inputs": (image(),),
        "layers": (layer(),),
        "texts": (line(),),
    }
    return RenderRequest(**{**values, **changes})


def video(**changes) -> RenderedVideo:
    values = {
        "data": stub_mp4(1000, b"x"),
        "media_type": MP4_MEDIA_TYPE,
        "duration_ms": 1000,
        "width": 720,
        "height": 1280,
        "fps": 30,
        "provider": "mock",
        "generated_at": T0,
    }
    return RenderedVideo(**{**values, **changes})


def box(kind: bytes, payload: bytes) -> bytes:
    return struct.pack(">I4s", 8 + len(payload), kind) + payload


FTYP = box(b"ftyp", b"isom" + struct.pack(">I", 0x200) + b"isommp41")


def mvhd_v0(timescale: int, duration: int) -> bytes:
    return box(b"mvhd", struct.pack(">B3xIIII", 0, 0, 0, timescale, duration))


def mvhd_v1(timescale: int, duration: int) -> bytes:
    return box(b"mvhd", struct.pack(">B3xQQIQ", 1, 0, 0, timescale, duration))


# Values


def test_a_valid_request_keeps_its_values() -> None:
    built = request()

    assert (built.width, built.height, built.fps, built.duration_ms) == (
        720,
        1280,
        30,
        1000,
    )
    assert built.background_color == "#000000"
    assert built.input("v1") == image()
    assert built.input(AUDIO_KEY) == audio()
    with pytest.raises(KeyError):
        built.input("v9")


def test_an_input_needs_the_sha256_of_its_data() -> None:
    good = image()
    assert good.sha256 == hashlib.sha256(PNG).hexdigest()

    with pytest.raises(ValueError, match="sha256"):
        RenderInput("v1", PNG, "image/png", hashlib.sha256(b"other").hexdigest())


@pytest.mark.parametrize(
    "key, data, media_type",
    [
        ("", PNG, "image/png"),
        ("V1", PNG, "image/png"),
        ("1v", PNG, "image/png"),
        ("v" * 17, PNG, "image/png"),
        ("v-1", PNG, "image/png"),
        ("v1", b"", "image/png"),
        ("v1", PNG, "image/gif"),
        ("v1", PNG, "text/plain"),
    ],
)
def test_inputs_reject_invalid_values(key, data, media_type) -> None:
    with pytest.raises(ValueError):
        RenderInput.of(key, data, media_type)


def test_the_bytes_of_an_input_are_not_in_its_repr() -> None:
    assert repr(PNG) not in repr(image())


@pytest.mark.parametrize(
    "changes",
    [
        {"x": -1},
        {"y": -1},
        {"width": 0},
        {"height": 0},
        {"x": True},
        {"width": 1.5},
        {"width": 5000},
    ],
)
def test_pixel_rects_reject_invalid_values(changes) -> None:
    with pytest.raises(ValueError):
        rect(**changes)


@pytest.mark.parametrize(
    "changes",
    [
        {"input_key": "Bad"},
        {"start_ms": -1},
        {"end_ms": 0},
        {"start_ms": 500, "end_ms": 500},
        {"end_ms": MAX_DURATION_MS + 1},
        {"source_in_ms": -1},
        {"source_in_ms": 500, "source_out_ms": 500},
        {"source_out_ms": True},
    ],
)
def test_layers_reject_invalid_values(changes) -> None:
    with pytest.raises(ValueError):
        layer(**changes)


def test_a_layer_needs_a_rect_and_a_fit() -> None:
    with pytest.raises(TypeError):
        layer(rect=(0, 0, 10, 10))
    with pytest.raises(TypeError):
        layer(fit="cover")
    assert layer(fit=LayerFit.CONTAIN).fit is LayerFit.CONTAIN


@pytest.mark.parametrize(
    "changes",
    [
        {"text": ""},
        {"text": "   "},
        {"text": "two\nlines"},
        {"text": "tab\there"},
        {"text": "x" * 501},
        {"text": "Viêt"},  # decomposed, not NFC
        {"size_px": 0},
        {"color": "#ffffff"},
        {"color": "white"},
        {"stroke_color": "#FFF"},
        {"stroke_px": -1},
        {"background": "#FFFFFF0"},
        {"font_key": "Font"},
        {"start_ms": 1000, "end_ms": 1000},
    ],
)
def test_text_lines_reject_invalid_values(changes) -> None:
    with pytest.raises(ValueError):
        line(**changes)


def test_a_text_line_takes_an_alpha_background_and_an_alignment() -> None:
    drawn = line(background="#00000080", align=TextAlign.LEFT, font_key="f1")

    assert drawn.background == "#00000080"
    assert drawn.align is TextAlign.LEFT
    with pytest.raises(TypeError):
        line(align="left")


@pytest.mark.parametrize(
    "changes",
    [
        {"width": 721},
        {"height": 14},
        {"width": 4098},
        {"fps": 0},
        {"fps": 121},
        {"duration_ms": 0},
        {"duration_ms": MAX_DURATION_MS + 1},
        {"audio": RenderInput.of(AUDIO_KEY, PNG, "image/png")},
        {"audio": RenderInput.of("sound", WAV, "audio/wav")},
        {"inputs": (image(), image())},
        {"inputs": (RenderInput.of(AUDIO_KEY, WAV, "audio/wav"), image())},
        {"layers": (layer(input_key="v2"),)},
        {"inputs": (RenderInput.of("v1", TTF, "font/ttf"),)},
        {"layers": (layer(end_ms=1001),)},
        {"texts": (line(end_ms=1001),)},
        {"texts": (line(font_key="f1"),)},
        {"texts": (line(font_key="v1"),)},
        {"background_color": "#00000000"},
    ],
)
def test_requests_reject_invalid_values(changes) -> None:
    with pytest.raises(ValueError):
        request(**changes)


@pytest.mark.parametrize("name", ["inputs", "layers", "texts"])
def test_request_collections_must_be_tuples(name) -> None:
    with pytest.raises(TypeError):
        request(**{name: list(getattr(request(), name))})


def test_a_request_with_a_font_input_accepts_its_font_key() -> None:
    font = RenderInput.of("f1", TTF, "font/ttf")

    built = request(inputs=(image(), font), texts=(line(font_key="f1"),))

    assert built.texts[0].font_key == "f1"


def test_a_request_without_layers_or_texts_is_valid() -> None:
    assert request(inputs=(), layers=(), texts=()).layers == ()


def test_describe_holds_no_bytes_and_is_stable() -> None:
    built = request()

    described = built.describe()

    assert described == request().describe()
    assert described["audio"] == {
        "key": AUDIO_KEY,
        "media_type": "audio/wav",
        "sha256": hashlib.sha256(WAV).hexdigest(),
    }
    assert described["inputs"] == [
        {
            "key": "v1",
            "media_type": "image/png",
            "sha256": hashlib.sha256(PNG).hexdigest(),
        }
    ]
    assert described["layers"][0]["rect"] == {
        "x": 0,
        "y": 0,
        "width": 720,
        "height": 1280,
    }
    assert described["texts"][0]["text"] == "Hello there"
    assert "fake" not in repr(described)
    assert not any(isinstance(value, bytes) for value in described.values())


def test_describe_changes_with_the_bytes_of_an_input() -> None:
    changed = request(inputs=(image(data=b"other image"),))

    assert changed.describe() != request().describe()


def test_a_valid_result_keeps_its_values() -> None:
    result = video()

    assert result.media_type == MP4_MEDIA_TYPE
    assert result.generated_at == T0
    assert "x" * 10 not in repr(result)


@pytest.mark.parametrize(
    "changes",
    [
        {"data": b""},
        {"media_type": "video/webm"},
        {"duration_ms": 0},
        {"width": 0},
        {"fps": 121},
        {"provider": ""},
        {"generated_at": datetime(2026, 10, 18)},
        {"generated_at": T0.astimezone(timezone(timedelta(hours=7)))},
    ],
)
def test_results_reject_invalid_values(changes) -> None:
    with pytest.raises(ValueError):
        video(**changes)


# Errors


def test_only_unavailable_and_timeout_are_retryable() -> None:
    assert RETRYABLE_CODES == {RenderErrorCode.UNAVAILABLE, RenderErrorCode.TIMEOUT}
    assert [code for code in RenderErrorCode if code.retryable] == [
        RenderErrorCode.UNAVAILABLE,
        RenderErrorCode.TIMEOUT,
    ]


def test_every_code_has_a_safe_user_message() -> None:
    assert set(USER_MESSAGES) == set(RenderErrorCode)
    for code, message in USER_MESSAGES.items():
        assert message and code.value not in message


@pytest.mark.parametrize("code", list(RenderErrorCode))
def test_errors_are_provider_errors_that_follow_their_code(code) -> None:
    error = VideoRenderError(code, f"engine said {SECRET}", provider="ffmpeg")

    assert isinstance(error, ProviderError)
    assert error.code == code.value
    assert error.render_code is code
    assert error.retryable is code.retryable
    assert error.provider == "ffmpeg"
    assert error.user_message == USER_MESSAGES[code]
    assert SECRET not in str(to_public(error).as_dict())


def test_an_error_without_detail_uses_its_code() -> None:
    error = VideoRenderError(RenderErrorCode.FAILED, provider="mock")

    assert str(error) == "render.failed"


# MP4 probe


def test_the_probe_reads_a_stub() -> None:
    info = probe_mp4(stub_mp4(30250, b"payload"))

    assert info == Mp4Info(("isom", "isom", "iso2", "avc1", "mp41"), 30250)


def test_the_probe_reads_a_version_1_movie_header() -> None:
    data = FTYP + box(b"moov", mvhd_v1(90000, 90000 * 3))

    assert probe_mp4(data) == Mp4Info(("isom", "isom", "mp41"), 3000)


def test_the_probe_finds_moov_after_mdat_and_rounds_the_duration() -> None:
    data = FTYP + box(b"mdat", b"\x00" * 16) + box(b"moov", mvhd_v0(600, 601))

    assert probe_mp4(data).duration_ms == 1002


def test_the_probe_reads_a_64_bit_box_size() -> None:
    payload = b"\x00" * 8
    large = struct.pack(">I4sQ", 1, b"free", 16 + len(payload)) + payload
    data = FTYP + large + box(b"moov", mvhd_v0(1000, 500))

    assert probe_mp4(data).duration_ms == 500


@pytest.mark.parametrize(
    "data",
    [
        b"",
        b"not an mp4 at all",
        box(b"moov", mvhd_v0(1000, 1000)),  # no ftyp first
        FTYP,  # no moov
        FTYP + box(b"moov", box(b"trak", b"")),  # no mvhd
        FTYP + box(b"moov", mvhd_v0(0, 1000)),  # timescale 0
        FTYP + box(b"moov", box(b"mvhd", b"\x02\x00\x00\x00" + bytes(16))),
        FTYP + box(b"moov", box(b"mvhd", b"\x00\x00\x00\x00")),  # cut off
        stub_mp4(1000, b"x")[:-10],  # truncated mdat
        FTYP + struct.pack(">I4s", 4, b"free"),  # size below the header
        box(b"ftyp", b"iso"),  # malformed ftyp
    ],
)
def test_the_probe_refuses_what_is_not_an_mp4(data) -> None:
    with pytest.raises(ValueError):
        probe_mp4(data)


# The mock


def test_the_mock_implements_the_protocol() -> None:
    mock = MockVideoRenderer()

    assert isinstance(mock, VideoRenderer)
    assert mock.name == "mock"
    mock.check()


def test_the_mock_answers_a_probe_valid_stub_of_the_request() -> None:
    built = request(duration_ms=2500)

    result = MockVideoRenderer(clock=lambda: T0).render(built)

    assert result.media_type == MP4_MEDIA_TYPE
    assert result.data == stub_mp4(2500, request_digest(built))
    assert probe_mp4(result.data).duration_ms == 2500
    assert (result.duration_ms, result.width, result.height, result.fps) == (
        2500,
        720,
        1280,
        30,
    )
    assert result.provider == "mock" and result.generated_at == T0


def test_the_stub_media_data_is_one_kibibyte_of_the_digest() -> None:
    built = request()
    digest = request_digest(built)

    data = MockVideoRenderer().render(built).data

    assert data.endswith(box(b"mdat", (digest * 64)[:MDAT_SIZE]))
    assert len(digest) == 32


def test_the_same_request_gives_the_same_bytes() -> None:
    first = MockVideoRenderer().render(request()).data
    second = MockVideoRenderer().render(dataclasses.replace(request())).data

    assert first == second


@pytest.mark.parametrize(
    "changes",
    [
        {"duration_ms": 900},
        {"fps": 25},
        {"audio": audio(b"other wav")},
        {"inputs": (image(data=b"other image"),)},
        {"texts": (line(text="Another line"),)},
        {"background_color": "#FFFFFF"},
    ],
)
def test_changed_requests_give_different_bytes(changes) -> None:
    mock = MockVideoRenderer()

    assert mock.render(request()).data != mock.render(request(**changes)).data


def test_the_digest_is_the_canonical_description() -> None:
    built = request(texts=(line(text=unicodedata.normalize("NFC", "Tiếng Việt")),))

    assert request_digest(built) == request_digest(request(texts=built.texts))
    assert len(request_digest(built)) == 32


def test_queued_answers_are_used_in_order() -> None:
    mock = MockVideoRenderer()
    mock.queue(b"first file", duration_ms=700)
    mock.queue(b"second file")

    first = mock.render(request())
    second = mock.render(request())
    third = mock.render(request())

    assert (first.data, first.duration_ms) == (b"first file", 700)
    assert (second.data, second.duration_ms) == (b"second file", 1000)
    assert probe_mp4(third.data).duration_ms == 1000


@pytest.mark.parametrize(
    "data, duration_ms",
    [
        (b"", None),
        ("text", None),
        (b"x", 0),
        (b"x", True),
        (b"x", MAX_DURATION_MS + 1),
    ],
)
def test_queue_checks_its_values_at_once(data, duration_ms) -> None:
    with pytest.raises(ValueError):
        MockVideoRenderer().queue(data, duration_ms=duration_ms)


def test_scripted_failures_come_first_then_normal_answers() -> None:
    mock = MockVideoRenderer()
    mock.fail_next(RenderErrorCode.TIMEOUT, times=2)

    for _ in range(2):
        with pytest.raises(VideoRenderError) as info:
            mock.render(request())
        assert info.value.render_code is RenderErrorCode.TIMEOUT
        assert info.value.retryable is True

    assert probe_mp4(mock.render(request()).data).duration_ms == 1000
    assert len(mock.calls) == 3


def test_fail_next_needs_at_least_one_time() -> None:
    with pytest.raises(ValueError):
        MockVideoRenderer().fail_next(RenderErrorCode.FAILED, times=0)


def test_check_follows_the_health_switch() -> None:
    mock = MockVideoRenderer()
    mock.set_healthy(False)

    with pytest.raises(VideoRenderError) as info:
        mock.check()
    assert info.value.render_code is RenderErrorCode.UNAVAILABLE

    mock.set_healthy(True)
    mock.check()


def test_calls_record_every_request() -> None:
    mock = MockVideoRenderer()
    first, second = request(), request(duration_ms=800)

    mock.render(first)
    mock.render(second)

    assert mock.calls == [first, second]


def test_the_clock_defaults_to_now_in_utc() -> None:
    before = datetime.now(UTC)

    result = MockVideoRenderer().render(request())

    assert before <= result.generated_at <= datetime.now(UTC)


@pytest.mark.parametrize(
    "duration_ms, payload", [(0, b"x"), (True, b"x"), (1000, b""), (1000, "x")]
)
def test_the_stub_refuses_invalid_values(duration_ms, payload) -> None:
    with pytest.raises(ValueError):
        stub_mp4(duration_ms, payload)
