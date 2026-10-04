"""Voice (TTS) Provider (Prompt Pack v8, prompt #086), context C7 Voice & Audio.

The interface every text-to-speech provider implements, mirroring the text
generation provider (#065).

- ``SpeechSynthesizer`` is a synchronous ``Protocol`` with ``synthesize`` and
  ``check`` (the health probe); ``name`` identifies the provider.
- ``synthesize(SpeechRequest)`` takes the ``text`` to speak (at most 5,000
  characters), the provider's ``voice_id`` (no whitespace, at most 200
  characters), a BCP-47 ``language`` and an optional ``speaking_style`` (at
  most 200 characters). It returns ``SynthesizedSpeech``: the ``audio`` bytes,
  their ``audio/*`` media type, the duration in whole milliseconds, the
  provider name, the voice id used, the number of ``characters`` sent and a
  UTC ``generated_at``. ``characters`` is ``len(text)`` as sent, with no
  normalisation; billing normalisation is #092.
- Failures raise ``SpeechSynthesisError`` (a ``ProviderError``) with a
  ``VoiceErrorCode``: ``voice.unavailable``, ``voice.rate_limited`` and
  ``voice.timeout`` are retryable; ``voice.invalid_request``,
  ``voice.voice_not_found`` and ``voice.refused`` are not. Error details never
  contain the request text.

A timeout is the provider's infrastructure (only its code is defined here);
retries are #091 and audio storage is #089. The mock is
``providers/mock_speech_synthesis.py``. No real provider yet.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Protocol, runtime_checkable

from ai_youtube_agent.content.strategy import LANGUAGE_TAG_PATTERN
from ai_youtube_agent.core.artifact import MEDIA_TYPE_PATTERN
from ai_youtube_agent.core.errors import ProviderError

MAX_TEXT = 5_000
MAX_VOICE_ID = 200
MAX_STYLE = 200
MAX_DURATION_MS = 86_400_000


def _whole(name: str, value: object, low: int, high: int | None = None) -> None:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < low
        or (high is not None and value > high)
    ):
        bound = f"from {low} to {high}" if high is not None else f"of at least {low}"
        raise ValueError(f"{name} must be a whole number {bound}")


@dataclass(frozen=True)
class SpeechRequest:
    text: str
    voice_id: str
    language: str
    speaking_style: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.text, str) or not self.text.strip():
            raise ValueError("text must not be empty")
        if len(self.text) > MAX_TEXT:
            raise ValueError(f"text must be at most {MAX_TEXT} characters")
        if (
            not isinstance(self.voice_id, str)
            or not self.voice_id
            or len(self.voice_id) > MAX_VOICE_ID
            or any(character.isspace() for character in self.voice_id)
        ):
            raise ValueError(
                f"voice_id must be 1 to {MAX_VOICE_ID} characters without spaces"
            )
        if not isinstance(self.language, str) or not LANGUAGE_TAG_PATTERN.fullmatch(
            self.language
        ):
            raise ValueError("language must be a BCP-47 tag")
        if self.speaking_style is not None and (
            not isinstance(self.speaking_style, str)
            or not self.speaking_style.strip()
            or len(self.speaking_style) > MAX_STYLE
        ):
            raise ValueError(f"speaking_style must be 1 to {MAX_STYLE} characters")


@dataclass(frozen=True)
class SynthesizedSpeech:
    audio: bytes
    media_type: str
    duration_ms: int
    provider: str
    voice_id: str
    characters: int
    generated_at: datetime

    def __post_init__(self) -> None:
        if not isinstance(self.audio, bytes) or not self.audio:
            raise ValueError("audio must be non-empty bytes")
        if (
            not isinstance(self.media_type, str)
            or not self.media_type.startswith("audio/")
            or not MEDIA_TYPE_PATTERN.fullmatch(self.media_type)
        ):
            raise ValueError("media_type must be an audio media type")
        _whole("duration_ms", self.duration_ms, 1, MAX_DURATION_MS)
        _whole("characters", self.characters, 0)
        if not all(
            isinstance(value, str) and value for value in (self.provider, self.voice_id)
        ):
            raise ValueError("provider and voice_id must not be empty")
        if not isinstance(
            self.generated_at, datetime
        ) or self.generated_at.utcoffset() != timedelta(0):
            raise ValueError("generated_at must be timezone-aware UTC")


class VoiceErrorCode(StrEnum):
    UNAVAILABLE = "voice.unavailable"
    RATE_LIMITED = "voice.rate_limited"
    TIMEOUT = "voice.timeout"
    INVALID_REQUEST = "voice.invalid_request"
    VOICE_NOT_FOUND = "voice.voice_not_found"
    REFUSED = "voice.refused"

    @property
    def retryable(self) -> bool:
        return self in RETRYABLE_CODES


RETRYABLE_CODES = frozenset(
    {VoiceErrorCode.UNAVAILABLE, VoiceErrorCode.RATE_LIMITED, VoiceErrorCode.TIMEOUT}
)

USER_MESSAGES = {
    VoiceErrorCode.UNAVAILABLE: "The voice service is not available. "
    "Please try again later.",
    VoiceErrorCode.RATE_LIMITED: "The voice service is busy. Please try again later.",
    VoiceErrorCode.TIMEOUT: "The voice service took too long to answer. "
    "Please try again later.",
    VoiceErrorCode.INVALID_REQUEST: "The voice request was not accepted.",
    VoiceErrorCode.VOICE_NOT_FOUND: "The chosen voice is not available.",
    VoiceErrorCode.REFUSED: "The voice service declined this request.",
}


class SpeechSynthesisError(ProviderError):
    """A voice provider failed; ``retryable`` follows the code."""

    default_code = VoiceErrorCode.UNAVAILABLE.value

    def __init__(
        self, code: VoiceErrorCode, detail: str = "", *, provider: str
    ) -> None:
        super().__init__(
            detail or code.value,
            provider=provider,
            code=code.value,
            user_message=USER_MESSAGES[code],
            retryable=code.retryable,
        )
        self.voice_code = code


@runtime_checkable
class SpeechSynthesizer(Protocol):
    name: str

    def synthesize(self, request: SpeechRequest) -> SynthesizedSpeech: ...

    def check(self) -> None:
        """Raise when the provider cannot be used (health probe)."""
        ...
