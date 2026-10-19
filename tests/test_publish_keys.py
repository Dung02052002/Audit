"""Tests for the deterministic publish keys (M-151, prompt #151)."""

import pytest

from ai_youtube_agent.pipeline.idempotency import (
    PUBLISH_PREFIX,
    UPLOAD_PREFIX,
    PublishKeys,
    derive_publish_keys,
    is_publish_key,
    is_upload_key,
    publish_key,
    upload_key,
)

SHA_A = "a" * 64
SHA_B = "b" * 64
CHANNEL_A = "UC" + "A" * 22
CHANNEL_B = "UC" + "B" * 22


def test_upload_key_is_deterministic_and_prefixed() -> None:
    job_key = publish_key("item-1", "req-1")

    first = upload_key(job_key, CHANNEL_A, SHA_A)
    second = upload_key(job_key, CHANNEL_A, SHA_A)

    assert first == second
    assert first.startswith(UPLOAD_PREFIX)
    assert is_upload_key(first)
    assert not is_publish_key(first)
    assert " " not in first


@pytest.mark.parametrize(
    "other",
    [
        (publish_key("item-2", "req-1"), CHANNEL_A, SHA_A),
        (publish_key("item-1", "req-2"), CHANNEL_A, SHA_A),
        (publish_key("item-1", "req-1"), CHANNEL_B, SHA_A),
        (publish_key("item-1", "req-1"), CHANNEL_A, SHA_B),
    ],
)
def test_upload_key_changes_with_every_input(other: tuple[str, str, str]) -> None:
    base = upload_key(publish_key("item-1", "req-1"), CHANNEL_A, SHA_A)

    assert upload_key(*other) != base


def test_upload_key_refuses_bad_inputs() -> None:
    job_key = publish_key("item-1", "req-1")

    with pytest.raises(ValueError):
        upload_key("not-a-publish-key", CHANNEL_A, SHA_A)
    with pytest.raises(ValueError):
        upload_key(job_key, CHANNEL_A, "ABC")
    with pytest.raises(ValueError):
        upload_key(job_key, CHANNEL_A, SHA_A.upper())
    with pytest.raises(ValueError):
        upload_key(job_key, " ", SHA_A)


def test_derive_publish_keys_returns_both_keys() -> None:
    keys = derive_publish_keys("item-1", "req-1", CHANNEL_A, SHA_A)

    assert isinstance(keys, PublishKeys)
    assert keys.job_key == publish_key("item-1", "req-1")
    assert keys.job_key.startswith(PUBLISH_PREFIX)
    assert keys.upload_key == upload_key(keys.job_key, CHANNEL_A, SHA_A)
    assert keys.job_key != keys.upload_key


@pytest.mark.parametrize(
    "value",
    [None, 5, "", "pub:", "pub:xyz", "pub:" + "A" * 64, "upl:" + "a" * 64, "pub:" + "a" * 63],
)
def test_is_publish_key_rejects_wrong_shapes(value: object) -> None:
    assert not is_publish_key(value)


def test_is_upload_key_rejects_a_trailing_newline() -> None:
    assert is_upload_key("upl:" + SHA_A)
    assert not is_upload_key("upl:" + SHA_A + "\n")
