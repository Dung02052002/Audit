"""Sensitive Logging Redaction (Prompt Pack v8, prompt #219), area X4 Security.

Redacts secrets, OAuth tokens, API keys, passwords, URL credentials, and PII
(such as email addresses) from structured log messages, fields, and tracebacks.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from typing import Any, Final

from pydantic import SecretStr

REDACTED_MASK: Final[str] = "[REDACTED]"

SENSITIVE_KEY_SUBSTRINGS: Final[tuple[str, ...]] = (
    "secret",
    "password",
    "passwd",
    "access_token",
    "refresh_token",
    "id_token",
    "api_key",
    "apikey",
    "authorization",
    "Private_key".lower(),
    "cookie",
    "set-cookie",
    "email",
)

SAFE_KEY_ALLOWLIST: Final[frozenset[str]] = frozenset(
    {
        "has_refresh_token",
        "token_type",
        "max_tokens",
        "prompt_tokens",
        "completion_tokens",
        "total_tokens",
        "email_enabled",
    }
)

_INLINE_PATTERNS: Final[tuple[tuple[re.Pattern[str], str], ...]] = (
    (
        re.compile(r"(?i)\b(Bearer\s+)[A-Za-z0-9\-._~+/]+=*"),
        rf"\1{REDACTED_MASK}",
    ),
    (
        re.compile(r"\bya29\.[0-9A-Za-z\-_]{12,}\b"),
        REDACTED_MASK,
    ),
    (
        re.compile(r"\b1//[0-9A-Za-z\-_]{12,}\b"),
        REDACTED_MASK,
    ),
    (
        re.compile(r"\bAIza[0-9A-Za-z\-_]{35}\b"),
        REDACTED_MASK,
    ),
    (
        re.compile(r"\bGOCSPX-[0-9A-Za-z\-_]{16,}\b"),
        REDACTED_MASK,
    ),
    (
        re.compile(r"\bsk-[0-9A-Za-z\-_]{20,}\b"),
        REDACTED_MASK,
    ),
    (
        re.compile(r"(https?://[^/\s:@]+:)([^@\s/]+)(@)"),
        rf"\1{REDACTED_MASK}\3",
    ),
    (
        re.compile(
            r"(?i)([?&](?:access_token|refresh_token|token|api_key|key|secret|password)=)([^&\s\"']+)"
        ),
        rf"\1{REDACTED_MASK}",
    ),
    (
        re.compile(
            r"(?i)\b((?:access_token|refresh_token|client_secret|api_key|password)\s*[=:]\s*['\"]?)([^'\"\s,;]+)"
        ),
        rf"\1{REDACTED_MASK}",
    ),
    (
        re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"),
        REDACTED_MASK,
    ),
)


def is_sensitive_key(key: str) -> bool:
    """Return True if a mapping key name represents a secret, token, or PII field."""
    lowered = key.strip().lower()
    if lowered in SAFE_KEY_ALLOWLIST:
        return False
    if lowered == "token" or lowered.endswith("_token"):
        return True
    return any(sub in lowered for sub in SENSITIVE_KEY_SUBSTRINGS)


def redact_string(text: str) -> str:
    """Redact inline credentials, OAuth tokens, API keys, and email PII from ``text``."""
    if not text:
        return text
    result = text
    for pattern, replacement in _INLINE_PATTERNS:
        result = pattern.sub(replacement, result)
    return result


def redact_value(value: Any, *, key: str | None = None) -> Any:
    """Recursively redact a structured log value."""
    if key is not None and is_sensitive_key(key):
        if isinstance(value, bool | int | float) or value is None:
            return value
        return REDACTED_MASK
    if isinstance(value, SecretStr):
        return REDACTED_MASK
    if isinstance(value, str):
        return redact_string(value)
    if isinstance(value, Mapping):
        return redact_mapping(value)
    if isinstance(value, list):
        return [redact_value(item) for item in value]
    if isinstance(value, tuple):
        return [redact_value(item) for item in value]
    return value


def redact_mapping(data: Mapping[str, Any]) -> dict[str, Any]:
    """Return a copy of ``data`` with all sensitive keys and inline secrets redacted."""
    redacted: dict[str, Any] = {}
    for k, v in data.items():
        key_str = str(k)
        redacted[key_str] = redact_value(v, key=key_str)
    return redacted


class SensitiveLogFilter(logging.Filter):
    """Logging filter that redacts ``record.msg`` and ``record.fields`` in place."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            rendered = record.getMessage()
            record.msg = redact_string(rendered)
            record.args = ()
        except Exception:  # noqa: BLE001
            pass
        fields = getattr(record, "fields", None)
        if isinstance(fields, Mapping):
            record.fields = redact_mapping(fields)
        return True
