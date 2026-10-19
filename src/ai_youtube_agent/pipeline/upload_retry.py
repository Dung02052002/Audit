"""Upload retry (Prompt Pack v8, prompt #153), context C12 Publishing.

``UploadRetryPolicy`` and ``call_with_retry`` give the publish executor a bounded,
deterministic retry of one provider call:

- Only a provider error that says it is retryable is retried (for uploads:
  unavailable, timeout, rate limited). Any other error is raised at once.
- At most ``max_attempts`` calls are made (1 means no retry). The wait before the
  next call doubles each time: ``base_delay_seconds * 2 ** (failed_attempt - 1)``,
  capped at ``max_delay_seconds``. A wait of 0 does not sleep. The sleep function
  is injected, so tests never wait.
- The last error is raised when the attempts run out; nothing is swallowed.

Resumable uploads: a retry after an ambiguous failure (a timeout where the video may
already have reached YouTube) is safe because every call carries the same upload
key (#151, ``YouTubeUploadPayload.idempotency_key``): a provider that has stored
that key returns the existing receipt instead of creating a second video.

``RESUMABLE_UPLOAD_CODES`` lists the failures after which the content is still
good and the publish may be resumed later (the retryable ones plus quota and
authorisation problems): the executor leaves the item ``PUBLISHING`` and the job
``FAILED``, and a later publish call restarts that same job (duplicate guard
decision ``RESUME``). Any other failure (invalid payload, invalid media, an upload
the provider says is a duplicate) fails the item.
"""

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final, TypeVar

from ai_youtube_agent.core.errors import ProviderError
from ai_youtube_agent.providers.youtube_upload import (
    RETRYABLE_UPLOAD_CODES,
    UploadErrorCode,
)

T = TypeVar("T")

RESUMABLE_UPLOAD_CODES: Final[frozenset[str]] = frozenset(
    code.value
    for code in (
        *RETRYABLE_UPLOAD_CODES,
        UploadErrorCode.QUOTA_EXCEEDED,
        UploadErrorCode.UNAUTHORIZED,
    )
)


@dataclass(frozen=True)
class UploadRetryPolicy:
    max_attempts: int = 3
    base_delay_seconds: float = 2.0
    max_delay_seconds: float = 60.0

    def __post_init__(self) -> None:
        if (
            isinstance(self.max_attempts, bool)
            or not isinstance(self.max_attempts, int)
            or not 1 <= self.max_attempts <= 10
        ):
            raise ValueError("max_attempts must be a whole number from 1 to 10")
        for name in ("base_delay_seconds", "max_delay_seconds"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int | float) or value < 0:
                raise ValueError(f"{name} must be a number of 0 or more")
        if self.max_delay_seconds < self.base_delay_seconds:
            raise ValueError("max_delay_seconds must not be below base_delay_seconds")

    def delay_after(self, failed_attempt: int) -> float:
        """Seconds to wait after attempt number ``failed_attempt`` (1 is the first)."""
        if isinstance(failed_attempt, bool) or failed_attempt < 1:
            raise ValueError("failed_attempt must be a whole number of 1 or more")
        return float(
            min(self.max_delay_seconds, self.base_delay_seconds * 2 ** (failed_attempt - 1))
        )


def is_resumable_error_code(code: str) -> bool:
    return code in RESUMABLE_UPLOAD_CODES


def call_with_retry(
    operation: Callable[[], T],
    policy: UploadRetryPolicy,
    *,
    sleep: Callable[[float], None] = time.sleep,
    on_retry: Callable[[int, ProviderError, float], None] | None = None,
) -> T:
    """Call ``operation`` up to ``policy.max_attempts`` times.

    ``on_retry(failed_attempt, error, delay)`` is called before each wait, so the
    caller can audit the retry. A non-retryable error, or the error of the last
    attempt, is raised unchanged."""
    attempt = 1
    while True:
        try:
            return operation()
        except ProviderError as error:
            if not error.retryable or attempt >= policy.max_attempts:
                raise
            delay = policy.delay_after(attempt)
            if on_retry is not None:
                on_retry(attempt, error, delay)
            if delay > 0:
                sleep(delay)
            attempt += 1
