"""Provenance Recorder (Prompt Pack v8, prompt #077), context C6 Rights & Policy.

``ProvenanceRecorder`` stores the source and licence metadata of an ``Asset`` as
an append-only history of ``Provenance`` records. The rules were approved by the
user on 2026-10-04:

- Scope: a service over the ``Asset`` entity (#075, #076) with a migration
  (0024) and a repository (``core/db/repositories/provenance.py``), registered in
  the bootstrap, and no HTTP route and no risk computation (#078 does that).
- ``record`` builds the record with ``Provenance.create``: a rule of the entity
  that fails (``ValueError``, ``TypeError``) is ``ProvenanceInputError`` (422).
  The asset must exist (``AssetNotFoundError``, 404). Any actor may record and
  the actor is stored.
- Category rules, by the category of the asset and on the record itself: a
  ``licensed`` asset needs a ``license_name`` or a ``license_ref``, a
  ``user_owned`` asset needs an ``owner``; ``generated``, ``public_domain`` and
  ``unknown`` need nothing more than the entity does. The ``Asset`` fields are
  not used to satisfy a rule.
- The history is never edited or deleted: the newest record (by ``created_at``,
  then insertion order) is the current one. Recording a statement equal to the
  current one (``Provenance.content_key``: every detail, the five text fields
  after NFC without case folding, URLs, checksum and retrieval time exactly)
  returns the current record and writes and audits nothing. A statement that
  equals an older record but not the current one is a new row (A, B, A gives
  three rows).
- Recording changes neither the ``Asset`` nor any ``RightsRecord``: the rights
  record keeps its own state until a later task decides otherwise.
- The time of recording is read from the clock inside the ``BEGIN IMMEDIATE``
  transaction that also holds the asset lookup, the current record and the
  insert, so a later insert never carries an earlier time, and there is no unique
  key to retry on. A clock that returns a naive or non-UTC time is a fault of the
  application (a plain ``ValueError``), not a 422. An error message never holds a
  value (a URL or a text), only the name of a field.
- ``asset.provenance_recorded`` is audited after commit, only for a new row,
  with ids, the category and flags only, never a URL, a licence or a proof text.
- ``current`` and ``history`` read and write nothing.
"""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from ai_youtube_agent.content.asset import AssetCategory
from ai_youtube_agent.content.asset_registry import AssetNotFoundError
from ai_youtube_agent.content.provenance import Provenance
from ai_youtube_agent.core.audit import Actor, AuditLog, AuditResult, EntityRef
from ai_youtube_agent.core.db.database import Database
from ai_youtube_agent.core.db.repositories.asset import AssetRepository
from ai_youtube_agent.core.db.repositories.provenance import ProvenanceRepository
from ai_youtube_agent.core.errors import DomainError

Clock = Callable[[], datetime]


class ProvenanceInputError(DomainError):
    default_code = "domain.provenance_input"
    default_user_message = "This provenance record cannot be stored as given."


class ProvenanceRecorder:
    def __init__(
        self, database: Database, audit: AuditLog, *, clock: Clock | None = None
    ) -> None:
        self._database = database
        self._audit = audit
        self._clock = clock

    def record(
        self,
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
        actor: Actor,
    ) -> Provenance:
        with self._database.transaction() as connection:
            created_at = self._now()
            try:
                provenance = Provenance.create(
                    asset_id,
                    source_url=source_url,
                    retrieved_at=retrieved_at,
                    license_name=license_name,
                    license_url=license_url,
                    license_ref=license_ref,
                    attribution=attribution,
                    owner=owner,
                    file_sha256=file_sha256,
                    proof=proof,
                    recorded_by=actor,
                    clock=lambda: created_at,
                )
            except (ValueError, TypeError) as error:
                # The entity messages name a field and never hold a value, and
                # the cause is dropped, so no URL or text reaches a log.
                raise ProvenanceInputError(str(error)) from None
            asset = AssetRepository(connection).get(asset_id)
            if asset is None:
                raise AssetNotFoundError(f"asset {asset_id} does not exist")
            _check_category(asset.category, provenance)
            records = ProvenanceRepository(connection)
            current = records.latest(asset_id)
            if (
                current is not None
                and current.content_key() == provenance.content_key()
            ):
                return current
            records.add(provenance)
        self._audit.record(
            "asset.provenance_recorded",
            actor,
            EntityRef("asset", asset.id),
            AuditResult.SUCCESS,
            {
                "provenance_id": provenance.id,
                "channel_id": asset.channel_id,
                "category": asset.category.value,
                "has_source_url": provenance.source_url is not None,
                "has_license_url": provenance.license_url is not None,
                "has_file_sha256": provenance.file_sha256 is not None,
                "has_proof": provenance.proof is not None,
            },
        )
        return provenance

    def _now(self) -> datetime:
        """The time of recording, read inside the transaction, so that a later
        insert never carries an earlier time. A clock that returns a naive or
        non-UTC time is a fault of the application, not an input error."""
        now = self._clock() if self._clock else datetime.now(UTC)
        if not isinstance(now, datetime) or now.utcoffset() != timedelta(0):
            raise ValueError("the clock must return a timezone-aware UTC time")
        return now

    def current(self, asset_id: str) -> Provenance | None:
        with self._database.transaction() as connection:
            _require_asset(connection, asset_id)
            return ProvenanceRepository(connection).latest(asset_id)

    def history(self, asset_id: str) -> list[Provenance]:
        with self._database.transaction() as connection:
            _require_asset(connection, asset_id)
            return ProvenanceRepository(connection).list_by_asset(asset_id)


def _require_asset(connection, asset_id: str) -> None:
    if AssetRepository(connection).get(asset_id) is None:
        raise AssetNotFoundError(f"asset {asset_id} does not exist")


def _check_category(category: AssetCategory, provenance: Provenance) -> None:
    if (
        category is AssetCategory.LICENSED
        and provenance.license_name is None
        and provenance.license_ref is None
    ):
        raise ProvenanceInputError(
            "a licensed asset needs a license_name or a license_ref in the record"
        )
    if category is AssetCategory.USER_OWNED and provenance.owner is None:
        raise ProvenanceInputError("a user_owned asset needs an owner in the record")
