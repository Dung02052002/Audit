"""Mock video renderer (Prompt Pack v8, prompt #100), context C8.

``MockVideoRenderer`` is the deterministic, in-memory ``VideoRenderer`` used in
tests and by default by the application (``Settings.render_provider =
"mock"``). It never starts a process.

- Without a queued answer, ``render`` returns ``stub_mp4(request.duration_ms,
  digest)``: an ``ftyp`` box (major brand ``isom``, minor version 0x200,
  compatible ``isom``, ``iso2``, ``avc1``, ``mp41``), a ``moov`` box holding a
  version 0 ``mvhd`` with timescale 1000 and the request's duration, and an
  ``mdat`` box of 1 KiB: the SHA-256 of the canonical request
  (``json.dumps(request.describe(), sort_keys=True, separators=(",", ":"),
  ensure_ascii=False)`` in UTF-8) repeated. ``probe_mp4`` reads it, the same
  request always gives the same bytes and different requests give different
  ones. It is not a playable video: it has no track.
- ``queue(data, duration_ms=None)`` sets the answer of the next ``render``
  call; queued answers are used in order. The bytes are returned as they are,
  so a test can queue a file that is not an MP4. ``duration_ms`` (default: the
  request's) is the duration the answer reports.
- ``fail_next(code, times=1)`` makes the next calls raise ``VideoRenderError``
  with that code.
- ``set_healthy(False)`` makes ``check`` fail with ``render.unavailable``.
- ``calls`` records every request.
"""

import hashlib
import json
import struct
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from ai_youtube_agent.providers.video_render import (
    MAX_DURATION_MS,
    MP4_MEDIA_TYPE,
    RenderedVideo,
    RenderErrorCode,
    RenderRequest,
    VideoRenderError,
)

Clock = Callable[[], datetime]
TIMESCALE = 1000
MDAT_SIZE = 1024
_BRANDS = (b"isom", b"iso2", b"avc1", b"mp41")
# The identity matrix of an ISO-BMFF movie header.
_MATRIX = (0x00010000, 0, 0, 0, 0x00010000, 0, 0, 0, 0x40000000)


@dataclass(frozen=True)
class _QueuedVideo:
    data: bytes
    duration_ms: int | None


def _box(kind: bytes, payload: bytes) -> bytes:
    return struct.pack(">I4s", 8 + len(payload), kind) + payload


def stub_mp4(duration_ms: int, payload: bytes) -> bytes:
    """A small MP4 container of ``duration_ms`` whose media data repeats ``payload``.

    ``probe_mp4`` reads it; it has no track, so no player shows anything.
    """
    if (
        isinstance(duration_ms, bool)
        or not isinstance(duration_ms, int)
        or not 1 <= duration_ms <= MAX_DURATION_MS
    ):
        raise ValueError(
            f"duration_ms must be a whole number from 1 to {MAX_DURATION_MS}"
        )
    if not isinstance(payload, bytes) or not payload:
        raise ValueError("payload must be non-empty bytes")
    ftyp = _box(b"ftyp", _BRANDS[0] + struct.pack(">I", 0x200) + b"".join(_BRANDS))
    mvhd = _box(
        b"mvhd",
        struct.pack(">B3xIIII", 0, 0, 0, TIMESCALE, duration_ms)
        + struct.pack(">IH10x", 0x00010000, 0x0100)
        + struct.pack(">9I", *_MATRIX)
        + bytes(24)
        + struct.pack(">I", 2),
    )
    media = (payload * (MDAT_SIZE // len(payload) + 1))[:MDAT_SIZE]
    return ftyp + _box(b"moov", mvhd) + _box(b"mdat", media)


def request_digest(request: RenderRequest) -> bytes:
    """The SHA-256 of the canonical description of ``request``."""
    canonical = json.dumps(
        request.describe(), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return hashlib.sha256(canonical.encode("utf-8")).digest()


class MockVideoRenderer:
    def __init__(self, *, name: str = "mock", clock: Clock | None = None) -> None:
        self.name = name
        self._clock = clock
        self._lock = threading.Lock()
        self._answers: list[_QueuedVideo] = []
        self._failures: list[RenderErrorCode] = []
        self._healthy = True
        self.calls: list[RenderRequest] = []

    # Setup

    def queue(self, data: bytes, *, duration_ms: int | None = None) -> None:
        if not isinstance(data, bytes) or not data:
            raise ValueError("data must be non-empty bytes")
        if duration_ms is not None and (
            isinstance(duration_ms, bool)
            or not isinstance(duration_ms, int)
            or not 1 <= duration_ms <= MAX_DURATION_MS
        ):
            raise ValueError(
                f"duration_ms must be a whole number from 1 to {MAX_DURATION_MS}"
            )
        with self._lock:
            self._answers.append(_QueuedVideo(data, duration_ms))

    def fail_next(self, code: RenderErrorCode, *, times: int = 1) -> None:
        if times < 1:
            raise ValueError("times must be 1 or more")
        with self._lock:
            self._failures.extend([code] * times)

    def set_healthy(self, healthy: bool) -> None:
        with self._lock:
            self._healthy = healthy

    # VideoRenderer

    def render(self, request: RenderRequest) -> RenderedVideo:
        with self._lock:
            self.calls.append(request)
            if self._failures:
                code = self._failures.pop(0)
                raise VideoRenderError(
                    code, "scripted render failure", provider=self.name
                )
            answer = self._answers.pop(0) if self._answers else None
        if answer is None:
            answer = _QueuedVideo(
                stub_mp4(request.duration_ms, request_digest(request)), None
            )
        return RenderedVideo(
            data=answer.data,
            media_type=MP4_MEDIA_TYPE,
            duration_ms=answer.duration_ms or request.duration_ms,
            width=request.width,
            height=request.height,
            fps=request.fps,
            provider=self.name,
            generated_at=self._clock() if self._clock else datetime.now(UTC),
        )

    def check(self) -> None:
        with self._lock:
            healthy = self._healthy
        if not healthy:
            raise VideoRenderError(
                RenderErrorCode.UNAVAILABLE, "mock set unhealthy", provider=self.name
            )
