import dataclasses
import io
import json
import logging
from collections.abc import Iterator

import pytest

from ai_youtube_agent.core.errors import (
    AppError,
    ApplicationError,
    DomainError,
    ErrorCategory,
    ProviderError,
    PublicError,
    to_public,
)
from ai_youtube_agent.core.log import Severity, configure_logging, get_logger

SECRET_DETAIL = "db password=hunter2 at 10.0.0.5"


@pytest.fixture(autouse=True)
def restore_root_logger() -> Iterator[None]:
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    yield
    root.handlers[:] = handlers
    root.setLevel(level)


@pytest.mark.parametrize(
    ("error", "category", "status"),
    [
        (ApplicationError(), ErrorCategory.APPLICATION, 500),
        (DomainError(), ErrorCategory.DOMAIN, 422),
        (ProviderError(provider="youtube"), ErrorCategory.PROVIDER, 502),
    ],
)
def test_categories_and_status(
    error: AppError, category: ErrorCategory, status: int
) -> None:
    public = error.to_public()

    assert isinstance(error, AppError)
    assert error.category is category
    assert public.category is category
    assert public.http_status == status
    assert public.message


def test_errors_can_be_caught_by_category() -> None:
    class InvalidTransitionError(DomainError):
        default_code = "domain.invalid_transition"

    with pytest.raises(DomainError) as caught:
        raise InvalidTransitionError("Draft -> Published is not allowed")

    assert caught.value.code == "domain.invalid_transition"
    assert caught.value.category is ErrorCategory.DOMAIN


@pytest.mark.parametrize(
    "error",
    [
        ApplicationError(SECRET_DETAIL),
        DomainError(SECRET_DETAIL),
        ProviderError(SECRET_DETAIL, provider="tts"),
    ],
)
def test_public_view_never_contains_detail(error: AppError) -> None:
    public = error.to_public()

    assert SECRET_DETAIL not in json.dumps(public.as_dict())
    assert "hunter2" not in public.message


def test_custom_code_and_safe_message() -> None:
    error = DomainError(
        "budget 12.50 > limit 10.00",
        code="domain.budget_exceeded",
        user_message="The daily budget has been reached.",
    )

    assert error.to_public().as_dict() == {
        "code": "domain.budget_exceeded",
        "category": "domain",
        "message": "The daily budget has been reached.",
        "retryable": False,
    }


def test_provider_error_carries_provider_and_retryable() -> None:
    error = ProviderError("timeout after 30s", provider="tts", retryable=True)

    assert error.provider == "tts"
    assert error.to_public().retryable is True
    assert error.log_fields()["provider"] == "tts"


def test_provider_name_is_not_public() -> None:
    public = ProviderError("boom", provider="internal-render-farm").to_public()

    assert "internal-render-farm" not in json.dumps(public.as_dict())


def test_provider_name_is_required() -> None:
    with pytest.raises(TypeError):
        ProviderError("boom")  # type: ignore[call-arg]


def test_empty_code_is_rejected() -> None:
    class BrokenError(ApplicationError):
        default_code = ""

    with pytest.raises(ValueError):
        BrokenError()


def test_unknown_exception_becomes_generic_application_error() -> None:
    public = to_public(KeyError(SECRET_DETAIL))

    assert public.category is ErrorCategory.APPLICATION
    assert public.code == "application.internal"
    assert public.http_status == 500
    assert SECRET_DETAIL not in public.message


def test_to_public_passes_app_errors_through() -> None:
    error = DomainError(code="domain.not_found", user_message="Not found.")

    assert to_public(error) == error.to_public()


def test_public_error_is_immutable() -> None:
    public = ApplicationError().to_public()

    with pytest.raises(dataclasses.FrozenInstanceError):
        public.message = "changed"  # type: ignore[misc]

    assert isinstance(public, PublicError)


def test_log_fields_keep_internal_detail_for_logs() -> None:
    stream = io.StringIO()
    configure_logging(Severity.INFO, stream)
    error = ProviderError("HTTP 503 from upload API", provider="youtube")

    get_logger("test.errors").error(
        "upload failed", extra={"fields": error.log_fields()}
    )

    fields = json.loads(stream.getvalue())["fields"]
    assert fields == {
        "error_code": "provider.failure",
        "error_category": "provider",
        "error_detail": "HTTP 503 from upload API",
        "retryable": False,
        "provider": "youtube",
    }
