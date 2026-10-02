"""Source entity (Prompt Pack v8, prompt #055), context C4 Research.

A ``Source`` is one web page used as research evidence. The rules were
approved by the user on 2026-10-02:

- ``url`` is the URL that was requested and ``final_url`` the one reached after
  redirects, both as the provider gave them. ``normalized_url`` is
  ``normalize_url(final_url)`` and identifies the page: one source per
  normalised URL (persistence enforces it, #029).
- ``normalize_url`` lower-cases the scheme and host, drops credentials, the
  default port (80 for http, 443 for https) and the ``#fragment``, removes
  tracking parameters (``utm_*``, ``fbclid``, ``gclid``, ``mc_cid``,
  ``mc_eid``), sorts the remaining query parameters and removes a trailing
  slash except on the root path. http and https are kept as given.
- ``title`` is whitespace-collapsed text of at most 300 characters,
  ``provider`` the research provider's name, ``published_at`` the optional
  publication time and ``retrieved_at`` when the page was read, both UTC.
- ``evidence_notes`` are 0 to 20 ordered ``EvidenceNote`` values: a note (at
  most 500 characters) and an optional verbatim ``quote`` (at most 1,000).
  ``Source.from_fetch`` checks that each quote occurs in the fetched text.
- ``id`` is a random hex id. ``Evidence.source_ref`` (B-017) holds it.
- ``content_fingerprint`` (#057) is the simhash of the fetched text
  (``content/similarity.py``), or None when unknown; the text is not kept.

A source is an immutable record.
"""

import re
import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from ai_youtube_agent.content.similarity import (
    FINGERPRINT_PATTERN,
    content_fingerprint,
)
from ai_youtube_agent.providers.research import (
    FetchedDocument,
    SearchHit,
    check_url,
)

MAX_TITLE = 300
MAX_NOTES = 20
MAX_NOTE = 500
MAX_QUOTE = 1000
TRACKING_PARAMETERS = frozenset({"fbclid", "gclid", "mc_cid", "mc_eid"})
DEFAULT_PORTS = {"http": 80, "https": 443}
PROVIDER_PATTERN = re.compile(r"^[a-z][a-z0-9_.-]*$")
Clock = Callable[[], datetime]


def _tracking(name: str) -> bool:
    name = name.lower()
    return name.startswith("utm_") or name in TRACKING_PARAMETERS


def normalize_url(url: str) -> str:
    """The canonical form of an absolute http(s) URL (see the module docstring)."""
    check_url("url", url)
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    host = (parts.hostname or "").lower()
    if ":" in host:  # IPv6 literal
        host = f"[{host}]"
    port = parts.port
    netloc = host if port in (None, DEFAULT_PORTS[scheme]) else f"{host}:{port}"
    path = parts.path or "/"
    if path != "/":
        path = path.rstrip("/") or "/"
    query = sorted(
        (name, value)
        for name, value in parse_qsl(parts.query, keep_blank_values=True)
        if not _tracking(name)
    )
    return urlunsplit((scheme, netloc, path, urlencode(query), ""))


def normalize_title(title: str) -> str:
    """Collapse whitespace and cut to 300 characters."""
    return " ".join(title.split())[:MAX_TITLE].rstrip()


def _require_text(name: str, value: object, limit: int) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must not be empty")
    if len(value) > limit:
        raise ValueError(f"{name} must be at most {limit} characters")


def _require_utc(name: str, value: datetime) -> None:
    if not isinstance(value, datetime) or value.utcoffset() != timedelta(0):
        raise ValueError(f"{name} must be timezone-aware UTC")


@dataclass(frozen=True)
class EvidenceNote:
    """Why a source matters: a note and an optional verbatim quote."""

    note: str
    quote: str | None = None

    def __post_init__(self) -> None:
        _require_text("evidence note", self.note, MAX_NOTE)
        if self.quote is not None:
            _require_text("evidence quote", self.quote, MAX_QUOTE)

    def as_dict(self) -> dict[str, str | None]:
        return {"note": self.note, "quote": self.quote}


@dataclass(frozen=True)
class Source:
    id: str
    url: str
    final_url: str
    normalized_url: str
    title: str
    provider: str
    published_at: datetime | None
    retrieved_at: datetime
    evidence_notes: tuple[EvidenceNote, ...] = ()
    content_fingerprint: str | None = None

    def __post_init__(self) -> None:
        if not self.id:
            raise ValueError("source id must not be empty")
        check_url("url", self.url)
        check_url("final_url", self.final_url)
        if self.normalized_url != normalize_url(self.final_url):
            raise ValueError("normalized_url must be normalize_url(final_url)")
        _require_text("source title", self.title, MAX_TITLE)
        if self.title != normalize_title(self.title):
            raise ValueError("source title must have collapsed whitespace")
        if not isinstance(self.provider, str) or not PROVIDER_PATTERN.match(
            self.provider
        ):
            raise ValueError(f"provider {self.provider!r} must be a lowercase name")
        if self.published_at is not None:
            _require_utc("published_at", self.published_at)
        _require_utc("retrieved_at", self.retrieved_at)
        if not isinstance(self.evidence_notes, tuple) or not all(
            isinstance(note, EvidenceNote) for note in self.evidence_notes
        ):
            raise TypeError("evidence_notes must be a tuple of EvidenceNote values")
        if len(self.evidence_notes) > MAX_NOTES:
            raise ValueError(f"a source has at most {MAX_NOTES} evidence notes")
        if self.content_fingerprint is not None and not FINGERPRINT_PATTERN.match(
            self.content_fingerprint
        ):
            raise ValueError("content fingerprint must be 16 lower-case hex digits")

    @classmethod
    def create(
        cls,
        url: str,
        title: str,
        provider: str,
        retrieved_at: datetime,
        *,
        final_url: str | None = None,
        published_at: datetime | None = None,
        evidence_notes: Iterable[EvidenceNote] = (),
        content_fingerprint: str | None = None,
    ) -> "Source":
        final = final_url or url
        return cls(
            id=uuid.uuid4().hex,
            url=url,
            final_url=final,
            normalized_url=normalize_url(final),
            title=normalize_title(title),
            provider=provider,
            published_at=published_at,
            retrieved_at=retrieved_at,
            evidence_notes=tuple(evidence_notes),
            content_fingerprint=content_fingerprint,
        )

    @classmethod
    def from_fetch(
        cls,
        document: FetchedDocument,
        *,
        hit: SearchHit | None = None,
        evidence_notes: Iterable[EvidenceNote] = (),
    ) -> "Source":
        """A source from a fetched page and, if known, the hit that found it.

        The title is the page title, else the hit title, else the normalised
        URL. The publication time comes from the hit. Every quote must occur in
        the fetched text. The content fingerprint is taken from the text.
        """
        notes = tuple(evidence_notes)
        for note in notes:
            if note.quote is not None and note.quote not in document.text:
                raise ValueError("an evidence quote must occur in the fetched text")
        title = next(
            (
                text
                for text in (document.title, hit.title if hit else None)
                if text and text.strip()
            ),
            normalize_url(document.final_url),
        )
        return cls.create(
            document.url,
            title,
            document.provider,
            document.fetched_at,
            final_url=document.final_url,
            published_at=hit.published_at if hit else None,
            evidence_notes=notes,
            content_fingerprint=content_fingerprint(document.text),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "url": self.url,
            "final_url": self.final_url,
            "normalized_url": self.normalized_url,
            "title": self.title,
            "provider": self.provider,
            "published_at": (
                self.published_at.isoformat() if self.published_at else None
            ),
            "retrieved_at": self.retrieved_at.isoformat(),
            "evidence_notes": [note.as_dict() for note in self.evidence_notes],
            "content_fingerprint": self.content_fingerprint,
        }


class DuplicateReason(StrEnum):
    """Why one source is a near-duplicate of another (#057)."""

    FINGERPRINT = "fingerprint"
    TITLE = "title"


@dataclass(frozen=True)
class SourceDuplicate:
    source_id: str
    duplicate_of: str
    reason: DuplicateReason
    similarity: float

    def __post_init__(self) -> None:
        if self.source_id == self.duplicate_of:
            raise ValueError("a source cannot duplicate itself")
        if not isinstance(self.reason, DuplicateReason):
            raise TypeError("reason must be a DuplicateReason")
        if not 0 <= self.similarity <= 1:
            raise ValueError("similarity must be from 0 to 1")
