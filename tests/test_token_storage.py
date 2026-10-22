"""Tests for S-217 Secure OAuth Token Storage."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import SecretStr

from ai_youtube_agent.core.audit import AuditLog, InMemoryAuditSink
from ai_youtube_agent.core.token_storage import (
    EncryptedFileTokenVault,
    ManagedTokenStore,
    TokenTamperedError,
)
from ai_youtube_agent.providers.mock_youtube_oauth import MockYouTubeOAuthProvider
from ai_youtube_agent.providers.youtube_oauth import OAuthScope, OAuthToken


def test_encrypted_file_token_vault_never_writes_plaintext_and_detects_tampering(
    tmp_path: Path,
) -> None:
    t0 = datetime(2026, 10, 21, 12, 0, tzinfo=UTC)
    vault_file = tmp_path / "oauth.tokens.enc"
    master_key = SecretStr("super-secret-master-vault-key-2026")
    vault = EncryptedFileTokenVault(vault_file, master_key)

    raw_access = "ya29.secret_access_token_value_12345"
    raw_refresh = "1//secret_refresh_token_value_67890"
    token = OAuthToken(
        channel_id="ch-sec-1",
        access_token=SecretStr(raw_access),
        refresh_token=SecretStr(raw_refresh),
        scopes=frozenset({OAuthScope.YOUTUBE_UPLOAD, OAuthScope.YOUTUBE_READONLY}),
        issued_at=t0,
        expires_at=t0 + timedelta(hours=1),
    )

    vault.store(token)
    assert vault_file.is_file()

    disk_content = vault_file.read_text(encoding="utf-8")
    assert raw_access not in disk_content
    assert raw_refresh not in disk_content

    loaded = vault.load("ch-sec-1")
    assert loaded is not None
    assert loaded.access_token.get_secret_value() == raw_access
    assert loaded.refresh_token is not None
    assert loaded.refresh_token.get_secret_value() == raw_refresh
    assert vault.list_channel_ids() == ("ch-sec-1",)

    # Tamper with ciphertext and verify MAC failure raises TokenTamperedError
    envelope = json.loads(disk_content)
    envelope["ct"] = "AAAA" + envelope["ct"][4:]
    vault_file.write_text(json.dumps(envelope), encoding="utf-8")

    with pytest.raises(TokenTamperedError):
        vault.load("ch-sec-1")


def test_managed_token_store_refreshes_expired_tokens_and_audits_safely(
    tmp_path: Path,
) -> None:
    t0 = datetime(2026, 10, 21, 12, 0, tzinfo=UTC)
    vault = EncryptedFileTokenVault(
        tmp_path / "managed.tokens.enc",
        SecretStr("managed-vault-master-key-987654"),
    )
    sink = InMemoryAuditSink()
    audit = AuditLog(sink, clock=lambda: t0)
    oauth = MockYouTubeOAuthProvider(clock=lambda: t0)

    # Seed a token that expires in 10 minutes
    seeded = oauth.seed_token(
        "ch-1",
        access_token="access-initial-secret",
        refresh_token="refresh-initial-secret",
        scopes=frozenset({OAuthScope.YOUTUBE_UPLOAD}),
        expires_at=t0 + timedelta(minutes=10),
    )
    store = ManagedTokenStore(
        vault,
        oauth,
        audit,
        clock=lambda: t0 + timedelta(minutes=15),
    )
    store.save_token(seeded)

    # Because clock is at t0 + 15m, get_valid_token refreshes and updates the vault
    refreshed = store.get_valid_token(
        "ch-1",
        required_scopes=(OAuthScope.YOUTUBE_UPLOAD,),
    )
    assert (
        refreshed.access_token.get_secret_value()
        != seeded.access_token.get_secret_value()
    )
    persisted = vault.load("ch-1")
    assert persisted is not None
    assert (
        persisted.access_token.get_secret_value()
        == refreshed.access_token.get_secret_value()
    )

    # Revoke and delete
    assert store.revoke_and_delete("ch-1") is True
    assert vault.load("ch-1") is None

    events_dump = str([e.as_dict() for e in sink.events()])
    assert "access-initial-secret" not in events_dump
    assert "refresh-initial-secret" not in events_dump
    assert [e.action for e in sink.events()] == [
        "oauth.token_stored",
        "oauth.token_refreshed",
        "oauth.token_deleted",
    ]
