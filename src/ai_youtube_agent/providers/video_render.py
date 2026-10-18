"""Render provider (Prompt Pack v8, prompt #100), context C8 Shorts Production.

The interface every video renderer implements, mirroring the voice provider
(#086). It answers open question Q2 for Shorts; the long-form render (#111)
may reuse it. The rules were decided by the user on 2026-10-18:

- ``VideoRenderer`` is a synchronous ``Protocol`` with ``render`` and ``check``
  (the health probe); ``name`` identifies the renderer. The mock is
  ``providers/mock_video_render.py`` and the ffmpeg command line renderer is
  ``providers/ffmpeg_video_render.py`` (``Settings.render_provider``).
- ``render(RenderRequest)`` takes the canvas (even ``width`` and ``height`` of
  16 to 4096 pixels, 1 to 120 ``fps``, ``duration_ms`` up to 4 hours), the
  mixed ``audio`` (a WAV ``RenderInput`` with the key ``audio``), the other
  ``inputs`` (images, videos and fonts, each a ``RenderInput`` with its bytes,
  media type and sha256), the visual ``layers`` from bottom to top and the
  ``texts`` drawn over them, one ``TextLine`` per line. Every time is absolute
  whole milliseconds inside the video. ``describe()`` is the request without
  any bytes (inputs by key, media type and sha256), stable for hashing.
- It returns ``RenderedVideo``: the MP4 ``data``, its duration, size and frame
  rate, the renderer name and a UTC ``generated_at``.
- Failures raise ``VideoRenderError`` (a ``ProviderError``) with a
  ``RenderErrorCode``: ``render.unavailable`` and ``render.timeout`` are
  retryable; ``render.invalid_request``, ``render.unsupported_input``,
  ``render.failed`` and ``render.output_invalid`` are not. The user message is
  generic and never holds engine output.
- ``probe_mp4(data)`` reads the top-level ISO-BMFF boxes of an MP4 file: it
  needs ``ftyp`` first and a ``moov`` with an ``mvhd`` (version 0 or 1, a
  timescale above 0) and gives the brands and the duration. It does not decode
  any frame.
"""

import hashlib
import re
import struct
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from ai_youtube_agent.core.errors import ProviderError

MP4_MEDIA_TYPE = "video/mp4"
AUDIO_INPUT_MEDIA_TYPE = "audio/wav"
IMAGE_MEDIA_TYPES = frozenset({"image/png", "image/jpeg", "image/webp"})
VIDEO_MEDIA_TYPES = frozenset({"video/mp4", "video/quicktime", "video/webm"})
FONT_MEDIA_TYPES = frozenset({"font/ttf", "font/otf"})
INPUT_EXTENSIONS = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/webp": "webp",
    "video/mp4": "mp4",
    "video/quicktime": "mov",
    "video/webm": "webm",
    "font/ttf": "ttf",
    "font/otf": "otf",
    AUDIO_INPUT_MEDIA_TYPE: "wav",
}
AUDIO_KEY = "audio"
MAX_DURATION_MS = 4 * 60 * 60 * 1000
MIN_SIDE = 16
MAX_SIDE = 4096
MAX_FPS = 120
MAX_TEXT_LINE = 500
MAX_FONT_SIZE = 1000
MAX_STROKE_PX = 100
INPUT_KEY_PATTERN = re.compile(r"[a-z][a-z0-9]{0,15}")
_RGB = re.compile(r"#[0-9A-F]{6}")
_RGB_OR_RGBA = re.compile(r"#[0-9A-F]{6}(?:[0-9A-F]{2})?")


def _whole(name: str, value: object, low: int, high: int) -> None:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not low <= value <= high
    ):
        raise ValueError(f"{name} must be a whole number from {low} to {high}")


def _key(name: str, value: object) -> None:
    if not isinstance(value, str) or not INPUT_KEY_PATTERN.fullmatch(value):
        raise ValueError(
            f"{name} must be a lowercase letter and up to 15 letters or digits"
        )


def _colour(name: str, value: object, pattern: re.Pattern[str], shape: str) -> None:
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise ValueError(f"{name} must be {shape} with capital hex digits")


def _window(name: str, start_ms: object, end_ms: object) -> None:
    _whole(f"{name} start_ms", start_ms, 0, MAX_DURATION_MS)
    _whole(f"{name} end_ms", end_ms, 1, MAX_DURATION_MS)
    if end_ms <= start_ms:
        raise ValueError(f"{name} must end after it starts")


@dataclass(frozen=True)
class RenderInput:
    """One file the renderer reads: its bytes, media type and sha256."""

    key: str
    data: bytes = field(repr=False)
    media_type: str
    sha256: str

    def __post_init__(self) -> None:
        _key("key", self.key)
        if not isinstance(self.data, bytes) or not self.data:
            raise ValueError("data must be non-empty bytes")
        if self.media_type not in INPUT_EXTENSIONS:
            allowed = ", ".join(sorted(INPUT_EXTENSIONS))
            raise ValueError(f"media_type must be one of: {allowed}")
        if self.sha256 != hashlib.sha256(self.data).hexdigest():
            raise ValueError(f"the sha256 of input {self.key} does not match its data")

    @classmethod
    def of(cls, key: str, data: bytes, media_type: str) -> "RenderInput":
        """An input whose sha256 is computed from ``data``."""
        if not isinstance(data, bytes):
            raise ValueError("data must be non-empty bytes")
        return cls(key, data, media_type, hashlib.sha256(data).hexdigest())

    def describe(self) -> dict[str, str]:
        return {"key": self.key, "media_type": self.media_type, "sha256": self.sha256}


@dataclass(frozen=True)
class PixelRect:
    """A rectangle in whole pixels: left and top corner, width and height."""

    x: int
    y: int
    width: int
    height: int

    def __post_init__(self) -> None:
        _whole("x", self.x, 0, MAX_SIDE)
        _whole("y", self.y, 0, MAX_SIDE)
        _whole("width", self.width, 1, MAX_SIDE)
        _whole("height", self.height, 1, MAX_SIDE)

    def to_dict(self) -> dict[str, int]:
        return {"x": self.x, "y": self.y, "width": self.width, "height": self.height}


class LayerFit(StrEnum):
    COVER = "cover"
    CONTAIN = "contain"


class TextAlign(StrEnum):
    LEFT = "left"
    CENTER = "center"
    RIGHT = "right"


@dataclass(frozen=True)
class VisualLayer:
    """An image or video input shown in ``rect`` from ``start_ms`` to ``end_ms``.

    ``source_in_ms`` and ``source_out_ms`` choose a window of a video input;
    an image ignores them.
    """

    input_key: str
    start_ms: int
    end_ms: int
    rect: PixelRect
    fit: LayerFit = LayerFit.COVER
    source_in_ms: int | None = None
    source_out_ms: int | None = None

    def __post_init__(self) -> None:
        _key("input_key", self.input_key)
        _window(f"layer {self.input_key}", self.start_ms, self.end_ms)
        if not isinstance(self.rect, PixelRect):
            raise TypeError("rect must be a PixelRect")
        if not isinstance(self.fit, LayerFit):
            raise TypeError("fit must be a LayerFit")
        for name in ("source_in_ms", "source_out_ms"):
            if getattr(self, name) is not None:
                _whole(name, getattr(self, name), 0, MAX_DURATION_MS)
        if (
            self.source_in_ms is not None
            and self.source_out_ms is not None
            and self.source_in_ms >= self.source_out_ms
        ):
            raise ValueError("source_in_ms must be before source_out_ms")

    def to_dict(self) -> dict[str, Any]:
        return {
            "input_key": self.input_key,
            "start_ms": self.start_ms,
            "end_ms": self.end_ms,
            "rect": self.rect.to_dict(),
            "fit": self.fit.value,
            "source_in_ms": self.source_in_ms,
            "source_out_ms": self.source_out_ms,
        }


@dataclass(frozen=True)
class TextLine:
    """One line of text drawn in ``rect``; ``font_key`` names a font input."""

    text: str
    start_ms: int
    end_ms: int
    rect: PixelRect
    align: TextAlign = TextAlign.CENTER
    size_px: int = 64
    color: str = "#FFFFFF"
    stroke_color: str = "#000000"
    stroke_px: int = 4
    background: str | None = None
    font_key: str | None = None

    def __post_init__(self) -> None:
        if (
            not isinstance(self.text, str)
            or not self.text.strip()
            or len(self.text) > MAX_TEXT_LINE
        ):
            raise ValueError(f"text must be 1 to {MAX_TEXT_LINE} characters")
        if self.text != unicodedata.normalize("NFC", self.text):
            raise ValueError("text must be NFC normalised")
        if any(unicodedata.category(character) == "Cc" for character in self.text):
            raise ValueError("text must be one line without control characters")
        _window("text", self.start_ms, self.end_ms)
        if not isinstance(self.rect, PixelRect):
            raise TypeError("rect must be a PixelRect")
        if not isinstance(self.align, TextAlign):
            raise TypeError("align must be a TextAlign")
        _whole("size_px", self.size_px, 1, MAX_FONT_SIZE)
        _colour("color", self.color, _RGB, "#RRGGBB")
        _colour("stroke_color", self.stroke_color, _RGB, "#RRGGBB")
        _whole("stroke_px", self.stroke_px, 0, MAX_STROKE_PX)
        if self.background is not None:
            _colour("background", self.background, _RGB_OR_RGBA, "#RRGGBB[AA]")
        if self.font_key is not None:
            _key("font_key", self.font_key)

    def to_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "start_ms": self.start_ms,
            "end_ms": self.end_ms,
            "rect": self.rect.to_dict(),
            "align": self.align.value,
            "size_px": self.size_px,
            "color": self.color,
            "stroke_color": self.stroke_color,
            "stroke_px": self.stroke_px,
            "background": self.background,
            "font_key": self.font_key,
        }


@dataclass(frozen=True)
class RenderRequest:
    width: int
    height: int
    fps: int
    duration_ms: int
    audio: RenderInput
    inputs: tuple[RenderInput, ...] = ()
    layers: tuple[VisualLayer, ...] = ()
    texts: tuple[TextLine, ...] = ()
    background_color: str = "#000000"

    def __post_init__(self) -> None:
        for name in ("width", "height"):
            value = getattr(self, name)
            _whole(name, value, MIN_SIDE, MAX_SIDE)
            if value % 2:
                raise ValueError(f"{name} must be even")
        _whole("fps", self.fps, 1, MAX_FPS)
        _whole("duration_ms", self.duration_ms, 1, MAX_DURATION_MS)
        if not isinstance(self.audio, RenderInput):
            raise TypeError("audio must be a RenderInput")
        if (
            self.audio.key != AUDIO_KEY
            or self.audio.media_type != AUDIO_INPUT_MEDIA_TYPE
        ):
            raise ValueError(
                f"audio must be the {AUDIO_INPUT_MEDIA_TYPE} input {AUDIO_KEY}"
            )
        for name in ("inputs", "layers", "texts"):
            if not isinstance(getattr(self, name), tuple):
                raise TypeError(f"{name} must be a tuple")
        by_key: dict[str, RenderInput] = {}
        for item in self.inputs:
            if not isinstance(item, RenderInput):
                raise TypeError("inputs must be RenderInput values")
            if item.key == AUDIO_KEY or item.key in by_key:
                raise ValueError(f"input key {item.key} is used twice")
            by_key[item.key] = item
        visual = IMAGE_MEDIA_TYPES | VIDEO_MEDIA_TYPES
        for layer in self.layers:
            if not isinstance(layer, VisualLayer):
                raise TypeError("layers must be VisualLayer values")
            source = by_key.get(layer.input_key)
            if source is None or source.media_type not in visual:
                raise ValueError(
                    f"layer input {layer.input_key} is not an image or video input"
                )
            self._inside("a layer", layer.end_ms)
        for line in self.texts:
            if not isinstance(line, TextLine):
                raise TypeError("texts must be TextLine values")
            if line.font_key is not None and (
                by_key.get(line.font_key) is None
                or by_key[line.font_key].media_type not in FONT_MEDIA_TYPES
            ):
                raise ValueError(f"font {line.font_key} is not a font input")
            self._inside("a text line", line.end_ms)
        _colour("background_color", self.background_color, _RGB, "#RRGGBB")

    def _inside(self, what: str, end_ms: int) -> None:
        if end_ms > self.duration_ms:
            raise ValueError(
                f"{what} ends at {end_ms} ms, after the video ({self.duration_ms} ms)"
            )

    def input(self, key: str) -> RenderInput:
        """The input named ``key`` (``audio`` is the audio); KeyError if none."""
        if key == AUDIO_KEY:
            return self.audio
        for item in self.inputs:
            if item.key == key:
                return item
        raise KeyError(key)

    def describe(self) -> dict[str, Any]:
        """The request without bytes, in a stable order (for hashing)."""
        return {
            "width": self.width,
            "height": self.height,
            "fps": self.fps,
            "duration_ms": self.duration_ms,
            "background_color": self.background_color,
            "audio": self.audio.describe(),
            "inputs": [item.describe() for item in self.inputs],
            "layers": [layer.to_dict() for layer in self.layers],
            "texts": [line.to_dict() for line in self.texts],
        }


@dataclass(frozen=True)
class RenderedVideo:
    data: bytes = field(repr=False)
    media_type: str
    duration_ms: int
    width: int
    height: int
    fps: int
    provider: str
    generated_at: datetime

    def __post_init__(self) -> None:
        if not isinstance(self.data, bytes) or not self.data:
            raise ValueError("data must be non-empty bytes")
        if self.media_type != MP4_MEDIA_TYPE:
            raise ValueError(f"media_type must be {MP4_MEDIA_TYPE}")
        _whole("duration_ms", self.duration_ms, 1, MAX_DURATION_MS)
        _whole("width", self.width, 1, MAX_SIDE)
        _whole("height", self.height, 1, MAX_SIDE)
        _whole("fps", self.fps, 1, MAX_FPS)
        if not isinstance(self.provider, str) or not self.provider:
            raise ValueError("provider must not be empty")
        if not isinstance(
            self.generated_at, datetime
        ) or self.generated_at.utcoffset() != timedelta(0):
            raise ValueError("generated_at must be timezone-aware UTC")


class RenderErrorCode(StrEnum):
    UNAVAILABLE = "render.unavailable"
    TIMEOUT = "render.timeout"
    INVALID_REQUEST = "render.invalid_request"
    UNSUPPORTED_INPUT = "render.unsupported_input"
    FAILED = "render.failed"
    OUTPUT_INVALID = "render.output_invalid"

    @property
    def retryable(self) -> bool:
        return self in RETRYABLE_CODES


RETRYABLE_CODES = frozenset({RenderErrorCode.UNAVAILABLE, RenderErrorCode.TIMEOUT})

USER_MESSAGES = {
    RenderErrorCode.UNAVAILABLE: "The video renderer is not available. "
    "Please try again later.",
    RenderErrorCode.TIMEOUT: "The video took too long to render. "
    "Please try again later.",
    RenderErrorCode.INVALID_REQUEST: "The video renderer did not accept the request.",
    RenderErrorCode.UNSUPPORTED_INPUT: "A file of this video is in a format the "
    "renderer cannot use.",
    RenderErrorCode.FAILED: "The video could not be rendered.",
    RenderErrorCode.OUTPUT_INVALID: "The video renderer gave an unusable file.",
}


class VideoRenderError(ProviderError):
    """A video renderer failed; ``retryable`` follows the code."""

    default_code = RenderErrorCode.UNAVAILABLE.value

    def __init__(
        self, code: RenderErrorCode, detail: str = "", *, provider: str
    ) -> None:
        super().__init__(
            detail or code.value,
            provider=provider,
            code=code.value,
            user_message=USER_MESSAGES[code],
            retryable=code.retryable,
        )
        self.render_code = code


@runtime_checkable
class VideoRenderer(Protocol):
    name: str

    def render(self, request: RenderRequest) -> RenderedVideo: ...

    def check(self) -> None:
        """Raise when the renderer cannot be used (health probe)."""
        ...


# MP4 probe


@dataclass(frozen=True)
class Mp4Info:
    brands: tuple[str, ...]
    duration_ms: int


def _boxes(data: bytes, start: int, end: int):
    """The ``(type, payload start, payload end)`` of the boxes in a range."""
    position = start
    while position < end:
        if end - position < 8:
            raise ValueError("an MP4 box header is cut off")
        size, kind = struct.unpack_from(">I4s", data, position)
        header = 8
        if size == 1:
            if end - position < 16:
                raise ValueError("an MP4 box header is cut off")
            (size,) = struct.unpack_from(">Q", data, position + 8)
            header = 16
        elif size == 0:
            size = end - position
        if size < header or position + size > end:
            raise ValueError(f"the MP4 box {kind!r} has a bad size")
        yield kind, position + header, position + size
        position += size


def _brands(data: bytes, start: int, end: int) -> tuple[str, ...]:
    if end - start < 8 or (end - start - 8) % 4:
        raise ValueError("the MP4 ftyp box is malformed")
    major = data[start : start + 4]
    compatible = [data[at : at + 4] for at in range(start + 8, end, 4)]
    return tuple(brand.decode("latin-1") for brand in (major, *compatible))


def _duration_ms(data: bytes, start: int, end: int) -> int:
    if end - start < 4:
        raise ValueError("the MP4 mvhd box is cut off")
    version = data[start]
    if version == 0:
        layout, offset = ">II", start + 12
    elif version == 1:
        layout, offset = ">IQ", start + 20
    else:
        raise ValueError(f"MP4 mvhd version {version} is not supported")
    if end - offset < struct.calcsize(layout):
        raise ValueError("the MP4 mvhd box is cut off")
    timescale, duration = struct.unpack_from(layout, data, offset)
    if timescale == 0:
        raise ValueError("the MP4 mvhd timescale is 0")
    return (duration * 1000 + timescale // 2) // timescale


def probe_mp4(data: bytes) -> Mp4Info:
    """The brands and duration of an MP4 file; ValueError when it is not one."""
    if not isinstance(data, bytes | bytearray) or not data:
        raise ValueError("an MP4 file needs bytes")
    content = bytes(data)
    boxes = list(_boxes(content, 0, len(content)))
    if not boxes or boxes[0][0] != b"ftyp":
        raise ValueError("an MP4 file starts with an ftyp box")
    brands = _brands(content, boxes[0][1], boxes[0][2])
    for kind, start, end in boxes[1:]:
        if kind != b"moov":
            continue
        for inner, inner_start, inner_end in _boxes(content, start, end):
            if inner == b"mvhd":
                return Mp4Info(brands, _duration_ms(content, inner_start, inner_end))
        raise ValueError("the MP4 moov box has no mvhd box")
    raise ValueError("the MP4 file has no moov box")
