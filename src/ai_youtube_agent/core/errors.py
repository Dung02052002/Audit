"""Typed error model (Prompt Pack v8, prompt #008).

Every error the application raises on purpose is an ``AppError`` in one of
three categories:

- ``ApplicationError``: a fault inside this service, such as a bug or a bad setup.
- ``DomainError``: a business rule refused the request. Later prompts subclass
  it, for example the typed transition errors of #032.
- ``ProviderError``: an external system failed. ``retryable`` tells the retry
  logic (#062, #091, #153, #208) whether trying again can help.

``detail`` is internal. It may go to logs, and it never goes to users.
``user_message`` is the only text a user ever sees, and ``to_public`` turns any
exception into a safe ``PublicError``, including exceptions it does not know.
"""

from dataclasses import dataclass
from enum import StrEnum
from http import HTTPStatus
from typing import Any, ClassVar


class ErrorCategory(StrEnum):
    APPLICATION = "application"
    DOMAIN = "domain"
    PROVIDER = "provider"


@dataclass(frozen=True)
class PublicError:
    """The part of an error that is safe to show to a user."""

    code: str
    category: ErrorCategory
    message: str
    retryable: bool
    http_status: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "category": self.category.value,
            "message": self.message,
            "retryable": self.retryable,
        }


class AppError(Exception):
    category: ClassVar[ErrorCategory]
    default_code: ClassVar[str]
    default_user_message: ClassVar[str]
    default_http_status: ClassVar[HTTPStatus]

    def __init__(
        self,
        detail: str = "",
        *,
        code: str | None = None,
        user_message: str | None = None,
        retryable: bool = False,
    ) -> None:
        super().__init__(detail or self.default_code)
        self.detail = detail
        self.code = code or self.default_code
        self.user_message = user_message or self.default_user_message
        self.retryable = retryable
        if not self.code:
            raise ValueError("error code must not be empty")

    def to_public(self) -> PublicError:
        return PublicError(
            code=self.code,
            category=self.category,
            message=self.user_message,
            retryable=self.retryable,
            http_status=int(self.default_http_status),
        )

    def log_fields(self) -> dict[str, Any]:
        """Internal fields for structured logs. May include ``detail``."""
        return {
            "error_code": self.code,
            "error_category": self.category.value,
            "error_detail": self.detail,
            "retryable": self.retryable,
        }


class ApplicationError(AppError):
    category = ErrorCategory.APPLICATION
    default_code = "application.internal"
    default_user_message = "Something went wrong on our side. Please try again later."
    default_http_status = HTTPStatus.INTERNAL_SERVER_ERROR


class DomainError(AppError):
    category = ErrorCategory.DOMAIN
    default_code = "domain.rule_violation"
    default_user_message = "The request cannot be completed in the current state."
    default_http_status = HTTPStatus.UNPROCESSABLE_ENTITY


class ProviderError(AppError):
    category = ErrorCategory.PROVIDER
    default_code = "provider.failure"
    default_user_message = (
        "An external service is not available. Please try again later."
    )
    default_http_status = HTTPStatus.BAD_GATEWAY

    def __init__(
        self,
        detail: str = "",
        *,
        provider: str,
        code: str | None = None,
        user_message: str | None = None,
        retryable: bool = False,
    ) -> None:
        super().__init__(
            detail, code=code, user_message=user_message, retryable=retryable
        )
        self.provider = provider

    def log_fields(self) -> dict[str, Any]:
        return {**super().log_fields(), "provider": self.provider}


def to_public(exc: BaseException) -> PublicError:
    """Return the safe view of any exception.

    An exception that is not an ``AppError`` is unexpected, so it is reported
    as a generic application error and its message is never exposed.
    """
    if isinstance(exc, AppError):
        return exc.to_public()
    return ApplicationError().to_public()
