"""Text Generation Provider (Prompt Pack v8, prompt #065), contexts C5 and C14.

The interface every text (LLM) provider implements, as the user approved on
2026-10-03 for the Hook Generator (#065). It answers ARCHITECTURE Q1: scripts
(#065-#067) are generated through this provider, not through a model SDK.

- ``TextGenerator`` is a synchronous ``Protocol`` with ``generate`` and
  ``check`` (the health probe); ``name`` identifies the provider.
- ``generate(TextRequest)`` takes a ``prompt`` (at most 20,000 characters), an
  optional ``system`` text (at most 10,000), an optional BCP-47 ``language``
  for the answer, the number of ``candidates`` wanted (1 to 5) and
  ``max_tokens`` per candidate (1 to 8,000). It returns ``GeneratedText``:
  1 to ``candidates`` non-empty texts, the provider and model names, a UTC
  ``generated_at`` and optional token counts (for LLM cost, #175).
- Failures raise ``TextGenerationError`` (a ``ProviderError``) with a
  ``TextErrorCode``: ``text.unavailable``, ``text.rate_limited`` and
  ``text.timeout`` are retryable; ``text.refused`` (the model declined) and
  ``text.invalid_request`` are not.

The mock is ``providers/mock_text_generation.py``. No real provider yet.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Protocol, runtime_checkable

from ai_youtube_agent.content.strategy import LANGUAGE_TAG_PATTERN
from ai_youtube_agent.core.errors import ProviderError

MAX_PROMPT = 20_000
MAX_SYSTEM = 10_000
MAX_CANDIDATES = 5
MAX_TOKENS = 8_000


def _whole(name: str, value: object, low: int, high: int) -> None:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not low <= value <= high
    ):
        raise ValueError(f"{name} must be a whole number from {low} to {high}")


@dataclass(frozen=True)
class TextRequest:
    prompt: str
    system: str | None = None
    language: str | None = None
    candidates: int = 1
    max_tokens: int = 256

    def __post_init__(self) -> None:
        if not isinstance(self.prompt, str) or not self.prompt.strip():
            raise ValueError("prompt must not be empty")
        if len(self.prompt) > MAX_PROMPT:
            raise ValueError(f"prompt must be at most {MAX_PROMPT} characters")
        if self.system is not None and (
            not self.system.strip() or len(self.system) > MAX_SYSTEM
        ):
            raise ValueError(f"system must be 1 to {MAX_SYSTEM} characters")
        if self.language is not None and not LANGUAGE_TAG_PATTERN.fullmatch(
            self.language
        ):
            raise ValueError(f"language {self.language!r} must be a BCP-47 tag")
        _whole("candidates", self.candidates, 1, MAX_CANDIDATES)
        _whole("max_tokens", self.max_tokens, 1, MAX_TOKENS)


@dataclass(frozen=True)
class GeneratedText:
    texts: tuple[str, ...]
    provider: str
    model: str
    generated_at: datetime
    input_tokens: int | None = None
    output_tokens: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.texts, tuple) or not (
            1 <= len(self.texts) <= MAX_CANDIDATES
        ):
            raise ValueError(f"a result has 1 to {MAX_CANDIDATES} texts")
        if not all(isinstance(t, str) and t.strip() for t in self.texts):
            raise ValueError("generated texts must not be empty")
        if not self.provider or not self.model:
            raise ValueError("provider and model must not be empty")
        if not isinstance(
            self.generated_at, datetime
        ) or self.generated_at.utcoffset() != timedelta(0):
            raise ValueError("generated_at must be timezone-aware UTC")
        for name in ("input_tokens", "output_tokens"):
            if getattr(self, name) is not None:
                _whole(name, getattr(self, name), 0, 10_000_000)


class TextErrorCode(StrEnum):
    UNAVAILABLE = "text.unavailable"
    RATE_LIMITED = "text.rate_limited"
    TIMEOUT = "text.timeout"
    REFUSED = "text.refused"
    INVALID_REQUEST = "text.invalid_request"

    @property
    def retryable(self) -> bool:
        return self in RETRYABLE_CODES


RETRYABLE_CODES = frozenset(
    {TextErrorCode.UNAVAILABLE, TextErrorCode.RATE_LIMITED, TextErrorCode.TIMEOUT}
)

USER_MESSAGES = {
    TextErrorCode.UNAVAILABLE: "The writing service is not available. "
    "Please try again later.",
    TextErrorCode.RATE_LIMITED: "The writing service is busy. Please try again later.",
    TextErrorCode.TIMEOUT: "The writing service took too long to answer. "
    "Please try again later.",
    TextErrorCode.REFUSED: "The writing service declined this request.",
    TextErrorCode.INVALID_REQUEST: "The writing request was not accepted.",
}


class TextGenerationError(ProviderError):
    """A text provider failed; ``retryable`` follows the code."""

    default_code = TextErrorCode.UNAVAILABLE.value

    def __init__(self, code: TextErrorCode, detail: str = "", *, provider: str) -> None:
        super().__init__(
            detail or code.value,
            provider=provider,
            code=code.value,
            user_message=USER_MESSAGES[code],
            retryable=code.retryable,
        )
        self.text_code = code


@runtime_checkable
class TextGenerator(Protocol):
    name: str

    def generate(self, request: TextRequest) -> GeneratedText: ...

    def check(self) -> None:
        """Raise when the provider cannot be used (health probe)."""
        ...
