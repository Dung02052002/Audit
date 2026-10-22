"""Secure OAuth Token Storage (Prompt Pack v8, prompt #217), area X4 Security.

Provides authenticated encrypted storage and automatic refresh coordination for
channel ``OAuthToken`` credentials without ever persisting plaintext tokens on
disk or exposing secrets in logs or audit events.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import hmac
import json
import os
import secrets
import threading
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final, Protocol, runtime_checkable

from pydantic import SecretStr

from ai_youtube_agent.core.audit import (
    Actor,
    ActorKind,
    AuditLog,
    AuditResult,
    EntityRef,
)
from ai_youtube_agent.core.errors import DomainError
from ai_youtube_agent.providers.youtube_oauth import (
    OAuthScope,
    OAuthToken,
    YouTubeOAuthProvider,
)

Clock = Callable[[], datetime]
PBKDF2_ITERATIONS: Final[int] = 100_000
VAULT_VERSION: Final[int] = 1
SYSTEM_TOKEN_ACTOR: Final[Actor] = Actor(ActorKind.SYSTEM, "token_storage")


class TokenStorageError(DomainError):
    """Raised when secure token storage or retrieval fails."""

    def __init__(
        self,
        detail: str,
        *,
        code: str = "domain.token_storage_error",
        user_message: str = "Secure OAuth token storage operation failed.",
    ) -> None:
        super().__init__(detail, code=code, user_message=user_message)


class TokenTamperedError(TokenStorageError):
    """Raised when ciphertext integrity / MAC verification fails."""

    def __init__(
        self, detail: str = "Encrypted token payload failed integrity check."
    ) -> None:
        super().__init__(
            detail,
            code="domain.token_storage_tampered",
            user_message=(
                "Stored authorization credentials could not be verified and were "
                "rejected."
            ),
        )


@runtime_checkable
class TokenVault(Protocol):
    """Platform-appropriate secure storage backend for ``OAuthToken`` records."""

    def store(self, token: OAuthToken) -> None: ...

    def load(self, channel_id: str) -> OAuthToken | None: ...

    def delete(self, channel_id: str) -> bool: ...

    def list_channel_ids(self) -> tuple[str, ...]: ...


def _derive_keys(master_key: bytes, salt: bytes) -> tuple[bytes, bytes]:
    derived = hashlib.pbkdf2_hmac(
        "sha256",
        master_key,
        salt,
        PBKDF2_ITERATIONS,
        dklen=64,
    )
    return derived[:32], derived[32:]


def _keystream_xor(data: bytes, enc_key: bytes, nonce: bytes) -> bytes:
    out = bytearray(len(data))
    block_count = (len(data) + 31) // 32
    for counter in range(block_count):
        counter_bytes = counter.to_bytes(8, "big")
        keystream = hmac.new(enc_key, nonce + counter_bytes, hashlib.sha256).digest()
        start = counter * 32
        end = min(start + 32, len(data))
        for idx in range(start, end):
            out[idx] = data[idx] ^ keystream[idx - start]
    return bytes(out)


def encrypt_payload(plaintext: bytes, master_secret: SecretStr) -> dict[str, Any]:
    """Encrypt ``plaintext`` with authenticated encryption (PBKDF2-SHA256 +
    CTR-HMAC-SHA256)."""
    raw_secret = master_secret.get_secret_value().encode("utf-8")
    if len(raw_secret) < 16:
        raise TokenStorageError("Master encryption key must be at least 16 characters.")
    salt = secrets.token_bytes(16)
    nonce = secrets.token_bytes(16)
    enc_key, mac_key = _derive_keys(raw_secret, salt)
    ciphertext = _keystream_xor(plaintext, enc_key, nonce)
    mac = hmac.new(
        mac_key,
        b"v1|" + salt + nonce + ciphertext,
        hashlib.sha256,
    ).digest()
    return {
        "v": VAULT_VERSION,
        "salt": base64.b64encode(salt).decode("ascii"),
        "nonce": base64.b64encode(nonce).decode("ascii"),
        "ct": base64.b64encode(ciphertext).decode("ascii"),
        "mac": base64.b64encode(mac).decode("ascii"),
    }


def decrypt_payload(envelope: dict[str, Any], master_secret: SecretStr) -> bytes:
    """Verify MAC and decrypt ``envelope``."""
    try:
        if envelope.get("v") != VAULT_VERSION:
            raise TokenTamperedError("Unsupported vault envelope version.")
        salt = base64.b64decode(envelope["salt"])
        nonce = base64.b64decode(envelope["nonce"])
        ciphertext = base64.b64decode(envelope["ct"])
        expected_mac = base64.b64decode(envelope["mac"])
    except (KeyError, ValueError, TypeError) as exc:
        raise TokenTamperedError("Malformed encrypted token envelope.") from exc

    raw_secret = master_secret.get_secret_value().encode("utf-8")
    enc_key, mac_key = _derive_keys(raw_secret, salt)
    actual_mac = hmac.new(
        mac_key,
        b"v1|" + salt + nonce + ciphertext,
        hashlib.sha256,
    ).digest()
    if not hmac.compare_digest(actual_mac, expected_mac):
        raise TokenTamperedError("HMAC verification failed for encrypted token store.")
    return _keystream_xor(ciphertext, enc_key, nonce)


def _serialize_token(token: OAuthToken) -> dict[str, Any]:
    return {
        "channel_id": token.channel_id,
        "access_token": token.access_token.get_secret_value(),
        "refresh_token": (
            token.refresh_token.get_secret_value()
            if token.refresh_token is not None
            else None
        ),
        "scopes": sorted(s.value for s in token.scopes),
        "issued_at": token.issued_at.isoformat(),
        "expires_at": token.expires_at.isoformat(),
        "token_type": token.token_type,
    }


def _deserialize_token(raw: dict[str, Any]) -> OAuthToken:
    return OAuthToken(
        channel_id=raw["channel_id"],
        access_token=SecretStr(raw["access_token"]),
        refresh_token=(
            SecretStr(raw["refresh_token"])
            if raw.get("refresh_token") is not None
            else None
        ),
        scopes=frozenset(OAuthScope(s) for s in raw["scopes"]),
        issued_at=datetime.fromisoformat(raw["issued_at"]),
        expires_at=datetime.fromisoformat(raw["expires_at"]),
        token_type=raw.get("token_type", "Bearer"),
    )


class InMemoryTokenVault:
    """Thread-safe in-memory token vault."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._tokens: dict[str, OAuthToken] = {}

    def store(self, token: OAuthToken) -> None:
        if not isinstance(token, OAuthToken):
            raise TokenStorageError("token must be an OAuthToken.")
        with self._lock:
            self._tokens[token.channel_id] = token

    def load(self, channel_id: str) -> OAuthToken | None:
        with self._lock:
            return self._tokens.get(channel_id)

    def delete(self, channel_id: str) -> bool:
        with self._lock:
            return self._tokens.pop(channel_id, None) is not None

    def list_channel_ids(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(sorted(self._tokens))


class EncryptedFileTokenVault:
    """Encrypted on-disk token vault with atomic writes and HMAC tamper protection."""

    def __init__(
        self,
        vault_path: Path,
        master_secret: SecretStr,
    ) -> None:
        if not isinstance(master_secret, SecretStr):
            raise TokenStorageError("master_secret must be a SecretStr.")
        if len(master_secret.get_secret_value()) < 16:
            raise TokenStorageError("master_secret must be at least 16 characters.")
        self._path = Path(vault_path)
        self._secret = master_secret
        self._lock = threading.Lock()

    @property
    def path(self) -> Path:
        return self._path

    def store(self, token: OAuthToken) -> None:
        if not isinstance(token, OAuthToken):
            raise TokenStorageError("token must be an OAuthToken.")
        with self._lock:
            records = self._read_all_unlocked()
            records[token.channel_id] = _serialize_token(token)
            self._write_all_unlocked(records)

    def load(self, channel_id: str) -> OAuthToken | None:
        with self._lock:
            records = self._read_all_unlocked()
            raw = records.get(channel_id)
            if raw is None:
                return None
            return _deserialize_token(raw)

    def delete(self, channel_id: str) -> bool:
        with self._lock:
            records = self._read_all_unlocked()
            if channel_id not in records:
                return False
            del records[channel_id]
            self._write_all_unlocked(records)
            return True

    def list_channel_ids(self) -> tuple[str, ...]:
        with self._lock:
            records = self._read_all_unlocked()
            return tuple(sorted(records))

    def _read_all_unlocked(self) -> dict[str, dict[str, Any]]:
        if not self._path.is_file():
            return {}
        try:
            envelope = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise TokenTamperedError(
                "Vault file is unreadable or corrupt JSON."
            ) from exc
        plaintext = decrypt_payload(envelope, self._secret)
        try:
            loaded = json.loads(plaintext.decode("utf-8"))
            if not isinstance(loaded, dict):
                raise TokenTamperedError("Decrypted vault content is not a mapping.")
            return loaded
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise TokenTamperedError("Decrypted vault payload is invalid.") from exc

    def _write_all_unlocked(self, records: dict[str, dict[str, Any]]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        plaintext = json.dumps(records, sort_keys=True).encode("utf-8")
        envelope = encrypt_payload(plaintext, self._secret)
        tmp_path = self._path.with_suffix(self._path.suffix + ".tmp")
        tmp_path.write_text(json.dumps(envelope, indent=2), encoding="utf-8")
        with contextlib.suppress(OSError):
            os.chmod(tmp_path, 0o600)
        tmp_path.replace(self._path)


class ManagedTokenStore:
    """Coordinates secure storage, automatic refresh, and revocation of OAuth tokens."""

    def __init__(
        self,
        vault: TokenVault,
        oauth_provider: YouTubeOAuthProvider,
        audit: AuditLog,
        *,
        clock: Clock | None = None,
    ) -> None:
        self._vault = vault
        self._oauth = oauth_provider
        self._audit = audit
        self._clock: Clock = clock or (lambda: datetime.now(UTC))

    @property
    def vault(self) -> TokenVault:
        return self._vault

    def save_token(
        self,
        token: OAuthToken,
        *,
        actor: Actor = SYSTEM_TOKEN_ACTOR,
    ) -> OAuthToken:
        self._vault.store(token)
        self._audit.record(
            action="oauth.token_stored",
            actor=actor,
            entity=EntityRef("channel", token.channel_id),
            result=AuditResult.SUCCESS,
            metadata={
                "channel_id": token.channel_id,
                "scope_count": len(token.scopes),
                "has_refresh_token": token.refresh_token is not None,
                "expires_at": token.expires_at.isoformat(),
            },
        )
        return token

    def get_valid_token(
        self,
        channel_id: str,
        *,
        required_scopes: Iterable[OAuthScope] = (),
        actor: Actor = SYSTEM_TOKEN_ACTOR,
    ) -> OAuthToken:
        """Load token from vault (or provider), refreshing and persisting if expired."""
        now = self._clock()
        req_scopes = tuple(required_scopes)
        token = self._vault.load(channel_id)
        if token is None:
            token = self._oauth.get_token(channel_id, required_scopes=req_scopes)
            self._vault.store(token)

        if token.is_expired(now):
            refreshed = self._oauth.refresh_token(channel_id)
            refreshed.require_valid(now, required_scopes=req_scopes)
            self._vault.store(refreshed)
            self._audit.record(
                action="oauth.token_refreshed",
                actor=actor,
                entity=EntityRef("channel", channel_id),
                result=AuditResult.SUCCESS,
                metadata={
                    "channel_id": channel_id,
                    "expires_at": refreshed.expires_at.isoformat(),
                },
            )
            return refreshed

        token.require_valid(now, required_scopes=req_scopes)
        return token

    def revoke_and_delete(
        self,
        channel_id: str,
        *,
        actor: Actor = SYSTEM_TOKEN_ACTOR,
    ) -> bool:
        self._oauth.revoke_token(channel_id)
        removed = self._vault.delete(channel_id)
        self._audit.record(
            action="oauth.token_deleted",
            actor=actor,
            entity=EntityRef("channel", channel_id),
            result=AuditResult.SUCCESS,
            metadata={
                "channel_id": channel_id,
                "removed_from_vault": removed,
            },
        )
        return removed
