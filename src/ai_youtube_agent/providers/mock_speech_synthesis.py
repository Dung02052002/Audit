"""Mock Speech Synthesizer (Prompt Pack v8, prompt #086), context C7.

``MockSpeechSynthesizer`` is the deterministic, in-memory ``SpeechSynthesizer``
used in tests and, until a real provider exists, by the application
(``Settings.voice_provider = "mock"``). It never touches the network.

- Without a queued answer, ``synthesize`` returns a mono, 16-bit, 16 kHz WAV
  file holding a sine tone. Its frequency is ``200 + n % 601`` Hz, where ``n``
  is the first 4 bytes (big-endian) of the SHA-256 of the canonical request:
  ``text``, ``voice_id``, ``language``, a style marker (``"1"`` with a
  ``speaking_style``, ``"0"`` without) and the style (``""`` without), joined
  with ``"\\x00"`` and encoded as UTF-8. The same request always gets
  identical bytes; different requests usually, but not always, get different
  tones (there are only 601). The duration is 60 ms per character of text, at
  least 500 ms (5,000 characters give 300 seconds).
- ``queue(audio, duration_ms=..., media_type="audio/wav")`` sets the answer of
  the next ``synthesize`` call; queued answers are used in order. The values
  are checked when queued, but the bytes are returned as they are, so a test
  can queue corrupt audio.
- ``fail_next(code, times=1)`` makes the next calls raise
  ``SpeechSynthesisError`` with that code.
- ``set_healthy(False)`` makes ``check`` fail with ``voice.unavailable``.
- ``calls`` records every request.
"""

import hashlib
import io
import math
import sys
import threading
import wave
from array import array
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from ai_youtube_agent.providers.speech_synthesis import (
    SpeechRequest,
    SpeechSynthesisError,
    SynthesizedSpeech,
    VoiceErrorCode,
)

Clock = Callable[[], datetime]
SAMPLE_RATE = 16_000
AMPLITUDE = 8_000
MS_PER_CHAR = 60
MIN_MS = 500
MEDIA_TYPE = "audio/wav"
_EPOCH = datetime(2000, 1, 1, tzinfo=UTC)


@dataclass(frozen=True)
class _QueuedSpeech:
    audio: bytes
    duration_ms: int
    media_type: str


class MockSpeechSynthesizer:
    def __init__(self, *, name: str = "mock", clock: Clock | None = None) -> None:
        self.name = name
        self._clock = clock
        self._lock = threading.Lock()
        self._answers: list[_QueuedSpeech] = []
        self._failures: list[VoiceErrorCode] = []
        self._healthy = True
        self.calls: list[SpeechRequest] = []

    # Setup

    def queue(
        self, audio: bytes, *, duration_ms: int, media_type: str = MEDIA_TYPE
    ) -> None:
        # Checked now, with the same rules as a real result.
        SynthesizedSpeech(
            audio=audio,
            media_type=media_type,
            duration_ms=duration_ms,
            provider=self.name,
            voice_id="queued",
            characters=0,
            generated_at=_EPOCH,
        )
        with self._lock:
            self._answers.append(_QueuedSpeech(audio, duration_ms, media_type))

    def fail_next(self, code: VoiceErrorCode, *, times: int = 1) -> None:
        if times < 1:
            raise ValueError("times must be 1 or more")
        with self._lock:
            self._failures.extend([code] * times)

    def set_healthy(self, healthy: bool) -> None:
        with self._lock:
            self._healthy = healthy

    # SpeechSynthesizer

    def synthesize(self, request: SpeechRequest) -> SynthesizedSpeech:
        with self._lock:
            self.calls.append(request)
            if self._failures:
                code = self._failures.pop(0)
                raise SpeechSynthesisError(
                    code, "scripted synthesize failure", provider=self.name
                )
            answer = self._answers.pop(0) if self._answers else None
        if answer is None:
            duration_ms = max(MIN_MS, MS_PER_CHAR * len(request.text))
            answer = _QueuedSpeech(_tone(request, duration_ms), duration_ms, MEDIA_TYPE)
        return SynthesizedSpeech(
            audio=answer.audio,
            media_type=answer.media_type,
            duration_ms=answer.duration_ms,
            provider=self.name,
            voice_id=request.voice_id,
            characters=len(request.text),
            generated_at=self._clock() if self._clock else datetime.now(UTC),
        )

    def check(self) -> None:
        with self._lock:
            healthy = self._healthy
        if not healthy:
            raise SpeechSynthesisError(
                VoiceErrorCode.UNAVAILABLE, "mock set unhealthy", provider=self.name
            )


def _canonical(request: SpeechRequest) -> bytes:
    """The request as stable bytes, independent of ``repr`` and Python version."""
    style = request.speaking_style
    fields = (
        request.text,
        request.voice_id,
        request.language,
        "0" if style is None else "1",
        "" if style is None else style,
    )
    return "\x00".join(fields).encode("utf-8")


def _tone(request: SpeechRequest, duration_ms: int) -> bytes:
    """A WAV file of ``duration_ms`` holding a sine tone chosen by the request."""
    digest = int.from_bytes(hashlib.sha256(_canonical(request)).digest()[:4], "big")
    frequency = 200 + digest % 601
    # A whole number of hertz makes one second repeat without a seam.
    second = array(
        "h",
        [
            round(AMPLITUDE * math.sin(2 * math.pi * frequency * n / SAMPLE_RATE))
            for n in range(SAMPLE_RATE)
        ],
    )
    if sys.byteorder == "big":
        second.byteswap()
    size = duration_ms * SAMPLE_RATE // 1000 * 2
    one_second = second.tobytes()
    frames = (one_second * (size // len(one_second) + 1))[:size]
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(SAMPLE_RATE)
        writer.writeframes(frames)
    return buffer.getvalue()
