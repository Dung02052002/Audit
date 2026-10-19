"""Tests for the upload retry policy (M-153, prompt #153)."""

import pytest
from pydantic import ValidationError

from ai_youtube_agent.core.config import Environment, Settings
from ai_youtube_agent.pipeline.upload_retry import (
    RESUMABLE_UPLOAD_CODES,
    UploadRetryPolicy,
    call_with_retry,
    is_resumable_error_code,
)
from ai_youtube_agent.providers.youtube_upload import UploadErrorCode, YouTubeUploadError


def _error(code: UploadErrorCode) -> YouTubeUploadError:
    return YouTubeUploadError(code, f"injected {code.value}")


class Flaky:
    """Raises the given errors in order, then returns ``"ok"``."""

    def __init__(self, *errors: Exception) -> None:
        self._errors = list(errors)
        self.calls = 0

    def __call__(self) -> str:
        self.calls += 1
        if self._errors:
            raise self._errors.pop(0)
        return "ok"


def test_delays_double_and_are_capped() -> None:
    policy = UploadRetryPolicy(max_attempts=5, base_delay_seconds=2, max_delay_seconds=10)

    assert [policy.delay_after(n) for n in (1, 2, 3, 4)] == [2.0, 4.0, 8.0, 10.0]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_attempts": 0},
        {"max_attempts": 11},
        {"max_attempts": True},
        {"base_delay_seconds": -1},
        {"base_delay_seconds": 5, "max_delay_seconds": 1},
    ],
)
def test_the_policy_refuses_bad_values(kwargs: dict) -> None:
    with pytest.raises(ValueError):
        UploadRetryPolicy(**kwargs)


def test_a_first_try_success_neither_waits_nor_retries() -> None:
    waits: list[float] = []
    operation = Flaky()

    assert call_with_retry(operation, UploadRetryPolicy(), sleep=waits.append) == "ok"
    assert operation.calls == 1
    assert waits == []


def test_retryable_errors_are_retried_with_growing_waits() -> None:
    waits: list[float] = []
    seen: list[tuple[int, str, float]] = []
    operation = Flaky(
        _error(UploadErrorCode.UNAVAILABLE), _error(UploadErrorCode.RATE_LIMITED)
    )

    result = call_with_retry(
        operation,
        UploadRetryPolicy(max_attempts=3, base_delay_seconds=2),
        sleep=waits.append,
        on_retry=lambda attempt, error, delay: seen.append((attempt, error.code, delay)),
    )

    assert result == "ok"
    assert operation.calls == 3
    assert waits == [2.0, 4.0]
    assert seen == [
        (1, UploadErrorCode.UNAVAILABLE.value, 2.0),
        (2, UploadErrorCode.RATE_LIMITED.value, 4.0),
    ]


def test_the_last_error_is_raised_when_the_attempts_run_out() -> None:
    operation = Flaky(
        _error(UploadErrorCode.TIMEOUT),
        _error(UploadErrorCode.TIMEOUT),
        _error(UploadErrorCode.UNAVAILABLE),
    )

    with pytest.raises(YouTubeUploadError) as caught:
        call_with_retry(
            operation, UploadRetryPolicy(max_attempts=3), sleep=lambda _: None
        )

    assert caught.value.upload_code is UploadErrorCode.UNAVAILABLE
    assert operation.calls == 3


@pytest.mark.parametrize(
    "code",
    [
        UploadErrorCode.INVALID_PAYLOAD,
        UploadErrorCode.INVALID_MEDIA,
        UploadErrorCode.QUOTA_EXCEEDED,
        UploadErrorCode.UNAUTHORIZED,
        UploadErrorCode.DUPLICATE_UPLOAD,
    ],
)
def test_a_non_retryable_error_is_raised_at_once(code: UploadErrorCode) -> None:
    operation = Flaky(_error(code))

    with pytest.raises(YouTubeUploadError):
        call_with_retry(operation, UploadRetryPolicy(), sleep=lambda _: None)

    assert operation.calls == 1


def test_an_unexpected_exception_is_never_retried() -> None:
    operation = Flaky(RuntimeError("bug"))

    with pytest.raises(RuntimeError):
        call_with_retry(operation, UploadRetryPolicy(), sleep=lambda _: None)

    assert operation.calls == 1


def test_a_zero_delay_does_not_sleep() -> None:
    waits: list[float] = []
    operation = Flaky(_error(UploadErrorCode.TIMEOUT))

    call_with_retry(
        operation,
        UploadRetryPolicy(max_attempts=2, base_delay_seconds=0),
        sleep=waits.append,
    )

    assert operation.calls == 2
    assert waits == []


def test_one_attempt_means_no_retry() -> None:
    operation = Flaky(_error(UploadErrorCode.TIMEOUT))

    with pytest.raises(YouTubeUploadError):
        call_with_retry(
            operation, UploadRetryPolicy(max_attempts=1), sleep=lambda _: None
        )

    assert operation.calls == 1


def test_resumable_codes_are_the_retryable_ones_plus_quota_and_authorisation() -> None:
    assert RESUMABLE_UPLOAD_CODES == {
        UploadErrorCode.UNAVAILABLE.value,
        UploadErrorCode.TIMEOUT.value,
        UploadErrorCode.RATE_LIMITED.value,
        UploadErrorCode.QUOTA_EXCEEDED.value,
        UploadErrorCode.UNAUTHORIZED.value,
    }
    assert is_resumable_error_code(UploadErrorCode.TIMEOUT.value)
    assert not is_resumable_error_code(UploadErrorCode.INVALID_PAYLOAD.value)
    assert not is_resumable_error_code(UploadErrorCode.DUPLICATE_UPLOAD.value)
    assert not is_resumable_error_code("something.else")


def test_the_settings_bound_the_retry_values() -> None:
    settings = Settings(environment=Environment.TEST)
    assert (settings.upload_max_attempts, settings.upload_retry_base_delay_seconds) == (
        3,
        2,
    )

    for field, value in (
        ("upload_max_attempts", 0),
        ("upload_max_attempts", 6),
        ("upload_max_attempts", True),
        ("upload_retry_base_delay_seconds", -1),
        ("upload_retry_base_delay_seconds", 61),
        ("upload_retry_base_delay_seconds", True),
    ):
        with pytest.raises(ValidationError):
            Settings(environment=Environment.TEST, **{field: value})
