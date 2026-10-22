"""Tests for S-219 Sensitive Logging Redaction."""

from __future__ import annotations

import io
import json
import logging

from pydantic import SecretStr

from ai_youtube_agent.core.log import Severity, configure_logging, get_logger
from ai_youtube_agent.core.log_redaction import (
    REDACTED_MASK,
    redact_mapping,
    redact_string,
)


def test_redact_string_scrubs_tokens_keys_urls_and_emails() -> None:
    raw = (
        "Authorization: Bearer ya29.super_secret_oauth_token_12345 "
        "refresh=1//refresh_token_secret_98765 "
        "key=AIzaSyA12345678901234567890123456789012 "
        "url=https://admin:hunter2pass@api.example.com/v1?access_token=tok_xyz "
        "owner=creator.pii@example.org"
    )
    scrubbed = redact_string(raw)
    assert "ya29.super_secret" not in scrubbed
    assert "1//refresh_token" not in scrubbed
    assert "AIzaSyA12345" not in scrubbed
    assert "hunter2pass" not in scrubbed
    assert "tok_xyz" not in scrubbed
    assert "creator.pii@example.org" not in scrubbed
    assert REDACTED_MASK in scrubbed


def test_json_formatter_redacts_message_fields_and_exception_traceback() -> None:
    root = logging.getLogger()
    old_handlers, old_level = list(root.handlers), root.level
    try:
        buf = io.StringIO()
        configure_logging(Severity.DEBUG, buf)
        logger = get_logger("test.redaction")

        try:
            raise RuntimeError(
                "OAuth failed for user@example.com with "
                "client_secret=GOCSPX-1234567890abcdef1234"
            )
        except RuntimeError:
            logger.exception(
                "Failed request with Bearer ya29.secret_token_abcdef123456",
                extra={
                    "fields": {
                        "access_token": "ya29.field_token_secret",
                        "refresh_token": SecretStr("1//field_refresh_secret"),
                        "reviewer_email": "alice@example.com",
                        "has_refresh_token": True,
                        "prompt_tokens": 250,
                    }
                },
            )

        line = buf.getvalue().strip()
        record = json.loads(line)
        assert "ya29.secret_token" not in line
        assert "ya29.field_token_secret" not in line
        assert "1//field_refresh_secret" not in line
        assert "GOCSPX-1234567890abcdef1234" not in line
        assert "alice@example.com" not in line
        assert "user@example.com" not in line

        assert record["fields"]["access_token"] == REDACTED_MASK
        assert record["fields"]["refresh_token"] == REDACTED_MASK
        assert record["fields"]["reviewer_email"] == REDACTED_MASK
        # Non-sensitive boolean/count fields remain intact
        assert record["fields"]["has_refresh_token"] is True
        assert record["fields"]["prompt_tokens"] == 250
    finally:
        root.handlers[:] = old_handlers
        root.setLevel(old_level)


def test_nested_mapping_redaction() -> None:
    nested = {
        "channel_id": "ch-1",
        "oauth": {
            "client_secret": "top-secret-val",
            "token_type": "Bearer",
        },
        "notes": ["Contact support@domain.com for help"],
    }
    cleaned = redact_mapping(nested)
    assert cleaned["channel_id"] == "ch-1"
    assert cleaned["oauth"]["client_secret"] == REDACTED_MASK
    assert cleaned["oauth"]["token_type"] == "Bearer"
    assert "support@domain.com" not in cleaned["notes"][0]
