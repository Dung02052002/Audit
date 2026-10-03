"""Mock Text Generator (Prompt Pack v8, prompt #065), contexts C5 and C14.

``MockTextGenerator`` is the deterministic, in-memory ``TextGenerator`` used
in tests and, until a real provider exists, by the application
(``Settings.text_provider = "mock"``). It never touches the network.

- ``queue(*texts)`` sets the texts of the next ``generate`` call (at most the
  request's ``candidates`` are returned); queued answers are used in order.
- Without a queued answer, ``generate`` returns ``candidates`` texts
  ``"Mock text <n> <hash>"``, where the hash is taken from the request, so the
  same request always gets the same texts.
- ``fail_next(code, times=1)`` makes the next calls raise
  ``TextGenerationError`` with that code.
- ``set_healthy(False)`` makes ``check`` fail with ``text.unavailable``.
- ``calls`` records every request.
"""

import hashlib
import threading
from collections.abc import Callable
from datetime import UTC, datetime

from ai_youtube_agent.providers.text_generation import (
    GeneratedText,
    TextErrorCode,
    TextGenerationError,
    TextRequest,
)

Clock = Callable[[], datetime]
MODEL = "mock-1"


class MockTextGenerator:
    def __init__(self, *, name: str = "mock", clock: Clock | None = None) -> None:
        self.name = name
        self._clock = clock
        self._lock = threading.Lock()
        self._answers: list[tuple[str, ...]] = []
        self._failures: list[TextErrorCode] = []
        self._healthy = True
        self.calls: list[TextRequest] = []

    # Setup

    def queue(self, *texts: str) -> None:
        if not texts:
            raise ValueError("queue at least one text")
        with self._lock:
            self._answers.append(tuple(texts))

    def fail_next(self, code: TextErrorCode, *, times: int = 1) -> None:
        if times < 1:
            raise ValueError("times must be 1 or more")
        with self._lock:
            self._failures.extend([code] * times)

    def set_healthy(self, healthy: bool) -> None:
        with self._lock:
            self._healthy = healthy

    # TextGenerator

    def generate(self, request: TextRequest) -> GeneratedText:
        with self._lock:
            self.calls.append(request)
            if self._failures:
                code = self._failures.pop(0)
                raise TextGenerationError(
                    code, "scripted generate failure", provider=self.name
                )
            answer = self._answers.pop(0) if self._answers else None
        if answer is None:
            digest = hashlib.sha256(repr(request).encode()).hexdigest()[:8]
            answer = tuple(
                f"Mock text {n} {digest}" for n in range(1, request.candidates + 1)
            )
        texts = answer[: request.candidates]
        return GeneratedText(
            texts=texts,
            provider=self.name,
            model=MODEL,
            generated_at=self._clock() if self._clock else datetime.now(UTC),
            input_tokens=len(request.prompt.split()),
            output_tokens=sum(len(text.split()) for text in texts),
        )

    def check(self) -> None:
        with self._lock:
            healthy = self._healthy
        if not healthy:
            raise TextGenerationError(
                TextErrorCode.UNAVAILABLE, "mock set unhealthy", provider=self.name
            )
