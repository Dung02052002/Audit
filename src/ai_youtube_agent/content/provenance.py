"""Provenance record (Prompt Pack v8, prompt #077), context C6 Rights & Policy.

A ``Provenance`` is one entry of the source and licence history of an ``Asset``:
where a file came from, under which licence, and the proof the user holds. The
rules were approved by the user on 2026-10-04:

- ``id``: a stable internal id; ``asset_id``: the ``Asset.id`` it describes.
- ``source_url``: optional http(s) URL the file was taken from.
- ``retrieved_at``: optional UTC time the file was taken, never after
  ``created_at``.
- ``license_name`` (1-200), ``license_url`` (a URL), ``license_ref`` (1-500),
  ``attribution`` (1-500), ``owner`` (1-200): optional licence details.
- ``file_sha256``: optional checksum, 64 lowercase hexadecimal characters.
- ``proof``: optional free text (1-1000), for example where an invoice is kept.
- ``recorded_by``: the ``Actor`` who recorded it (any actor).
- ``created_at``: UTC. The record is frozen with no mutators, and the history of
  an asset is append only: the newest record is the current one.

Every content field above is optional, but a record needs at least one. Text is
stripped and runs of whitespace are collapsed, and a text that is empty after
that is an error, not ``None``. No Unicode normalisation is applied to the
stored text (like ``Asset``). ``content_key`` is the idempotency key: NFC of
the collapsed value for the five text fields only (no case folding), while URLs,
the checksum and the retrieval time are compared exactly.

URLs (``source_url``, ``license_url``) are stripped and stored as given. They
must pass ``check_url`` of the research provider (an absolute http(s) URL with a
host, at most 2048 characters), hold no whitespace, no userinfo
(``user:password@``) and a valid port, and neither the query string nor the
fragment (also after a ``?`` inside it) may hold a parameter whose name looks
secret (``SECRET_PARAMETERS`` and the ``SECRET_PARAMETER_PREFIXES``, matched on
the whole name after percent-decoding, case insensitively, even with no value):
such a URL is refused, never rewritten. An error message never holds a value (a
URL or a text), only the name of the field, so a credential cannot reach a log.
The checksum is stripped and lower-cased. ``Provenance.create`` takes the time
from an injected clock, never the wall clock when one is given.
"""

import re
import unicodedata
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qsl, urlsplit

from ai_youtube_agent.core.audit import Actor
from ai_youtube_agent.providers.research import check_url

MAX_URL_LENGTH = 2048
MAX_LICENSE_NAME = 200
MAX_LICENSE_REF = 500
MAX_ATTRIBUTION = 500
MAX_OWNER = 200
MAX_PROOF = 1000
SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")
# The names (compared in lower case, whole name) of a query or fragment
# parameter that is taken to carry a secret, and the prefixes of the signed
# cloud URL parameters (AWS and Google).
SECRET_PARAMETERS = frozenset(
    {
        "token",
        "access_token",
        "refresh_token",
        "id_token",
        "auth",
        "authorization",
        "key",
        "api_key",
        "apikey",
        "api-key",
        "secret",
        "client_secret",
        "password",
        "passwd",
        "pwd",
        "sig",
        "signature",
        "session",
        "sessionid",
        "credential",
        "credentials",
    }
)
SECRET_PARAMETER_PREFIXES = ("x-amz-", "x-goog-")
TEXT_FIELDS = ("license_name", "license_ref", "attribution", "owner", "proof")
Clock = Callable[[], datetime]
CONTENT_FIELDS = (
    "source_url",
    "retrieved_at",
    "license_name",
    "license_url",
    "license_ref",
    "attribution",
    "owner",
    "file_sha256",
    "proof",
)


@dataclass(frozen=True)
class Provenance:
    id: str
    asset_id: str
    source_url: str | None
    retrieved_at: datetime | None
    license_name: str | None
    license_url: str | None
    license_ref: str | None
    attribution: str | None
    owner: str | None
    file_sha256: str | None
    proof: str | None
    recorded_by: Actor
    created_at: datetime

    def __post_init__(self) -> None:
        for name in ("id", "asset_id"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must not be empty")
        if not isinstance(self.recorded_by, Actor):
            raise TypeError("recorded_by must be an Actor")
        _require_utc("created_at", self.created_at)
        _require_url("source_url", self.source_url)
        _require_url("license_url", self.license_url)
        _require_text("license_name", self.license_name, MAX_LICENSE_NAME)
        _require_text("license_ref", self.license_ref, MAX_LICENSE_REF)
        _require_text("attribution", self.attribution, MAX_ATTRIBUTION)
        _require_text("owner", self.owner, MAX_OWNER)
        _require_text("proof", self.proof, MAX_PROOF)
        if self.retrieved_at is not None:
            _require_utc("retrieved_at", self.retrieved_at)
            if self.retrieved_at > self.created_at:
                raise ValueError("retrieved_at must not be after created_at")
        if self.file_sha256 is not None and (
            not isinstance(self.file_sha256, str)
            or not SHA256_PATTERN.fullmatch(self.file_sha256)
        ):
            raise ValueError("file_sha256 must be 64 lowercase hexadecimal characters")
        if all(value is None for value in self._content()):
            raise ValueError("a provenance record needs at least one detail")

    @classmethod
    def create(
        cls,
        asset_id: str,
        *,
        source_url: str | None = None,
        retrieved_at: datetime | None = None,
        license_name: str | None = None,
        license_url: str | None = None,
        license_ref: str | None = None,
        attribution: str | None = None,
        owner: str | None = None,
        file_sha256: str | None = None,
        proof: str | None = None,
        recorded_by: Actor,
        clock: Clock | None = None,
    ) -> "Provenance":
        return cls(
            id=uuid.uuid4().hex,
            asset_id=asset_id,
            source_url=_strip("source_url", source_url),
            retrieved_at=retrieved_at,
            license_name=_collapse("license_name", license_name),
            license_url=_strip("license_url", license_url),
            license_ref=_collapse("license_ref", license_ref),
            attribution=_collapse("attribution", attribution),
            owner=_collapse("owner", owner),
            file_sha256=_strip("file_sha256", file_sha256, lower=True),
            proof=_collapse("proof", proof),
            recorded_by=recorded_by,
            created_at=clock() if clock else datetime.now(UTC),
        )

    def content_key(self) -> tuple[Any, ...]:
        """What the record says: equal keys are the same statement.

        The five text fields are compared after NFC (no case folding). URLs,
        the checksum and the retrieval time are compared exactly. The id, the
        actor and the time of recording are not part of it.
        """
        return tuple(
            unicodedata.normalize("NFC", value)
            if name in TEXT_FIELDS and value is not None
            else value
            for name, value in zip(CONTENT_FIELDS, self._content(), strict=True)
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "asset_id": self.asset_id,
            "source_url": self.source_url,
            "retrieved_at": (
                self.retrieved_at.isoformat() if self.retrieved_at else None
            ),
            "license_name": self.license_name,
            "license_url": self.license_url,
            "license_ref": self.license_ref,
            "attribution": self.attribution,
            "owner": self.owner,
            "file_sha256": self.file_sha256,
            "proof": self.proof,
            "recorded_by": {
                "kind": self.recorded_by.kind.value,
                "id": self.recorded_by.id,
            },
            "created_at": self.created_at.isoformat(),
        }

    def _content(self) -> tuple[Any, ...]:
        return (
            self.source_url,
            self.retrieved_at,
            self.license_name,
            self.license_url,
            self.license_ref,
            self.attribution,
            self.owner,
            self.file_sha256,
            self.proof,
        )


def _strip(name: str, value: str | None, *, lower: bool = False) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError(f"{name} must be text")
    value = value.strip()
    return value.lower() if lower else value


def _collapse(name: str, value: str | None) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError(f"{name} must be text")
    return " ".join(value.split())


def _require_text(name: str, value: str | None, limit: int) -> None:
    if value is None:
        return
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must not be empty")
    if value != " ".join(value.split()):
        raise ValueError(f"{name} must have single spaces and no outer whitespace")
    if len(value) > limit:
        raise ValueError(f"{name} must be at most {limit} characters")


def _require_url(name: str, value: str | None) -> None:
    if value is None:
        return
    # No message below holds the URL: it may carry a credential and an error
    # message reaches the logs. Errors of urlsplit quote the host, so they are
    # dropped too: the error is raised after the ``except`` block, so it keeps
    # no reference (``__cause__`` or ``__context__``) to the original one.
    malformed = False
    try:
        check_url(name, value)
        parts = urlsplit(value)
        parts.port  # noqa: B018 - raises ValueError for a bad port
        has_userinfo = "@" in parts.netloc
        names = [
            parameter.lower()
            for text in (
                parts.query,
                parts.fragment,
                parts.fragment.partition("?")[2],  # "#/route?token=..."
            )
            for parameter, _ in parse_qsl(text, keep_blank_values=True)
        ]
    except ValueError:
        malformed = True
    if malformed:
        raise ValueError(f"{name} must be an absolute http(s) URL without credentials")
    if any(character.isspace() for character in value):
        raise ValueError(f"{name} must not contain whitespace")
    if has_userinfo:
        raise ValueError(f"{name} must not contain a user name or a password")
    if any(_looks_secret(parameter) for parameter in names):
        raise ValueError(
            f"{name} must not carry a secret-looking query or fragment parameter"
        )


def _looks_secret(parameter: str) -> bool:
    return parameter in SECRET_PARAMETERS or parameter.startswith(
        SECRET_PARAMETER_PREFIXES
    )


def _require_utc(name: str, value: datetime) -> None:
    if not isinstance(value, datetime):
        raise TypeError(f"{name} must be a datetime")
    if value.utcoffset() != timedelta(0):
        raise ValueError(f"{name} must be timezone-aware UTC")
