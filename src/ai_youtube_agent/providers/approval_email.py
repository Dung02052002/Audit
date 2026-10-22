"""Approval Email Provider interface (Prompt Pack v8, prompt #138), context C11 Review.

Defines the synchronous ``ApprovalEmailProvider`` protocol used by ``L-138 Email
Approval`` to deliver approval notifications containing a safe preview reference
and exact artifact bindings (``video_artifact_id``, ``video_artifact_version``,
``video_artifact_sha256``) without exposing raw credentials or storage secrets.
"""

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable
from urllib.parse import parse_qsl, urlsplit

from ai_youtube_agent.core.artifact import SHA256_PATTERN
from ai_youtube_agent.core.errors import ProviderError

EMAIL_PATTERN = re.compile(r"^[^@\s\r\n]+@[^@\s\r\n]+\.[^@\s\r\n]+$")
FORBIDDEN_QUERY_KEYS = frozenset(
    {
        "api_key",
        "apikey",
        "secret",
        "client_secret",
        "password",
        "access_token",
        "refresh_token",
        "private_key",
        "bearer",
    }
)


def _validate_safe_preview_url(url: str) -> None:
    if not isinstance(url, str) or not url.strip():
        raise ValueError("preview_url must not be empty")
    if any(ch in url for ch in ("\r", "\n", "\x00")):
        raise ValueError("preview_url must not contain control characters")
    parsed = urlsplit(url)
    if parsed.scheme not in ("", "http", "https"):
        raise ValueError("preview_url must use http, https or a relative path")
    if parsed.username or parsed.password:
        raise ValueError("preview_url must not embed credentials")
    if not parsed.path.startswith("/"):
        raise ValueError("preview_url path must start with '/'")
    for key, _ in parse_qsl(parsed.query, keep_blank_values=True):
        if key.strip().lower() in FORBIDDEN_QUERY_KEYS:
            raise ValueError(
                f"preview_url must not expose sensitive query parameter {key!r}"
            )


@dataclass(frozen=True)
class ApprovalEmailMessage:
    recipient: str
    subject: str
    approval_request_id: str
    content_item_id: str
    video_artifact_id: str
    video_artifact_version: int
    video_artifact_sha256: str
    preview_url: str
    text_body: str
    html_body: str | None = None
    artifacts: tuple[dict[str, Any], ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.recipient, str) or not EMAIL_PATTERN.fullmatch(
            self.recipient
        ):
            raise ValueError("recipient must be a valid email address")
        if (
            not isinstance(self.subject, str)
            or not self.subject.strip()
            or "\r" in self.subject
            or "\n" in self.subject
        ):
            raise ValueError("subject must be a single non-empty line")
        for field_name in (
            "approval_request_id",
            "content_item_id",
            "video_artifact_id",
        ):
            val = getattr(self, field_name)
            if not isinstance(val, str) or not val.strip():
                raise ValueError(f"{field_name} must not be empty")
        if (
            isinstance(self.video_artifact_version, bool)
            or not isinstance(self.video_artifact_version, int)
            or self.video_artifact_version < 1
        ):
            raise ValueError("video_artifact_version must be an integer >= 1")
        if not isinstance(self.video_artifact_sha256, str) or not SHA256_PATTERN.match(
            self.video_artifact_sha256
        ):
            raise ValueError(
                "video_artifact_sha256 must be 64 lowercase hexadecimal characters"
            )
        _validate_safe_preview_url(self.preview_url)
        if not isinstance(self.text_body, str) or not self.text_body.strip():
            raise ValueError("text_body must not be empty")
        for required_token, label in (
            (self.approval_request_id, "approval_request_id"),
            (self.video_artifact_id, "video_artifact_id"),
            (self.video_artifact_sha256, "video_artifact_sha256"),
            (self.preview_url, "preview_url"),
        ):
            if required_token not in self.text_body:
                raise ValueError(f"text_body must include exact {label}")
        if self.html_body is not None:
            if not isinstance(self.html_body, str) or not self.html_body.strip():
                raise ValueError("html_body must not be empty when provided")
            for required_token, label in (
                (self.approval_request_id, "approval_request_id"),
                (self.video_artifact_id, "video_artifact_id"),
                (self.preview_url, "preview_url"),
            ):
                if required_token not in self.html_body:
                    raise ValueError(f"html_body must include exact {label}")

    def as_dict(self) -> dict[str, Any]:
        return {
            "recipient": self.recipient,
            "subject": self.subject,
            "approval_request_id": self.approval_request_id,
            "content_item_id": self.content_item_id,
            "video_artifact_id": self.video_artifact_id,
            "video_artifact_version": self.video_artifact_version,
            "video_artifact_sha256": self.video_artifact_sha256,
            "preview_url": self.preview_url,
            "text_body": self.text_body,
            "html_body": self.html_body,
            "artifacts": [dict(a) for a in self.artifacts],
        }


@dataclass(frozen=True)
class ApprovalEmailReceipt:
    message_id: str
    provider: str
    recipient: str
    approval_request_id: str
    video_artifact_id: str
    sent_at: datetime

    def __post_init__(self) -> None:
        for field_name in (
            "message_id",
            "provider",
            "recipient",
            "approval_request_id",
            "video_artifact_id",
        ):
            val = getattr(self, field_name)
            if not isinstance(val, str) or not val.strip():
                raise ValueError(f"{field_name} must not be empty")
        if not isinstance(
            self.sent_at, datetime
        ) or self.sent_at.utcoffset() != timedelta(0):
            raise ValueError("sent_at must be timezone-aware UTC")

    def as_dict(self) -> dict[str, Any]:
        return {
            "message_id": self.message_id,
            "provider": self.provider,
            "recipient": self.recipient,
            "approval_request_id": self.approval_request_id,
            "video_artifact_id": self.video_artifact_id,
            "sent_at": self.sent_at.isoformat(),
        }


class EmailErrorCode(StrEnum):
    UNAVAILABLE = "email.unavailable"
    RATE_LIMITED = "email.rate_limited"
    TIMEOUT = "email.timeout"
    INVALID_RECIPIENT = "email.invalid_recipient"
    INVALID_MESSAGE = "email.invalid_message"
    REFUSED = "email.refused"

    @property
    def retryable(self) -> bool:
        return self in RETRYABLE_EMAIL_CODES


RETRYABLE_EMAIL_CODES = frozenset(
    {
        EmailErrorCode.UNAVAILABLE,
        EmailErrorCode.RATE_LIMITED,
        EmailErrorCode.TIMEOUT,
    }
)

USER_MESSAGES: dict[EmailErrorCode, str] = {
    EmailErrorCode.UNAVAILABLE: "The approval email service is unavailable.",
    EmailErrorCode.RATE_LIMITED: "The approval email service is rate limited.",
    EmailErrorCode.TIMEOUT: "The approval email service timed out.",
    EmailErrorCode.INVALID_RECIPIENT: (
        "The approval email recipient address is invalid."
    ),
    EmailErrorCode.INVALID_MESSAGE: (
        "The approval email message was rejected as invalid."
    ),
    EmailErrorCode.REFUSED: "The approval email service refused the message.",
}


class ApprovalEmailProviderError(ProviderError):
    default_code = EmailErrorCode.UNAVAILABLE.value

    def __init__(
        self, code: EmailErrorCode, detail: str = "", *, provider: str
    ) -> None:
        super().__init__(
            detail or code.value,
            provider=provider,
            code=code.value,
            user_message=USER_MESSAGES[code],
            retryable=code.retryable,
        )
        self.email_code = code


@runtime_checkable
class ApprovalEmailProvider(Protocol):
    name: str

    def send(self, message: ApprovalEmailMessage) -> ApprovalEmailReceipt: ...

    def check(self) -> None:
        """Raise when the email provider cannot be used (health probe)."""
        ...
