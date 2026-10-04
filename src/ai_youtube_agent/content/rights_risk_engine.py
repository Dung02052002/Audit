"""Rights Risk Engine (Prompt Pack v8, prompt #078), context C6 Rights & Policy.

``RightsRiskEngine`` classifies the unresolved ``RightsRecord`` rows of a content
item as low, medium or high risk, from the registered ``Asset`` of the record
and its current ``Provenance``. The rules were approved by the user on
2026-10-04:

- Scope: a service (this module), the pure rules and the entity
  (``content/rights_assessment.py``), a migration (0025) and a repository
  (``core/db/repositories/rights_assessment.py``), registered in the bootstrap.
  No HTTP route. ``RightsRecord``, ``RightsRecordRepository``, the assets and
  provenance code and migrations 0001-0024 are unchanged, and the gate is not
  wired into the shared publish gate (#084).
- The rules are deterministic, with no AI model. ``classify`` gives one level
  and one stable rule code per record:

  * the ``asset_ref`` is not a registered asset of the channel of the item:
    high (``asset.not_registered``);
  * category ``unknown``: high (``asset.category_unknown``);
  * ``licensed``: no provenance or no licence name and reference: high; a
    licence only: medium; a licence and a licence URL or a proof: low;
  * ``public_domain``: no provenance, no source URL, or a source URL only:
    medium; a source URL and a proof or a licence URL: low;
  * ``user_owned``: no provenance, or no owner with a proof or a checksum:
    medium; an owner and a proof or a checksum: low;
  * ``generated``: low.

  The full table of the 14 outcomes is in ``content/rights_assessment.py``.
- ``assess`` runs on demand, in one ``BEGIN IMMEDIATE`` transaction, for every
  rights record of the item. A record a user resolved is skipped: it is not
  read, assessed or updated. For an unresolved record the asset and the current
  provenance are read and classified. When the newest assessment of the record
  has the same outcome (level, codes, provenance id and rules version) and the
  record already holds that level, nothing is written. Otherwise a new
  ``RightsAssessment`` row is appended (the history is never edited or deleted)
  and, if the level of the record differs, it is applied with
  ``RightsRecord.with_risk_level``, which keeps the record unresolved. A
  provenance record that is newer than the last assessment gives a new row. A
  concurrent change of a record (``ConcurrencyError``) rolls everything back.
- ``changed`` is the number of new assessment rows. The time of assessment is
  read from the clock inside the transaction; a clock that returns a naive or
  non-UTC time is a fault of the application (a plain ``ValueError``).
- ``rights.assessed`` is audited after commit, only when ``changed`` is more
  than zero, with counts only (assessed, changed, skipped_resolved and the
  number of records per level), never a URL, a licence or a text. An error
  message never holds one either.
- ``latest`` and ``history`` read and write nothing. A missing rights record is
  ``RightsRecordNotFoundError`` (404).
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from http import HTTPStatus

from ai_youtube_agent.content.hook_generator import ContentItemNotFoundError
from ai_youtube_agent.content.rights import RiskLevel
from ai_youtube_agent.content.rights_assessment import RightsAssessment, classify
from ai_youtube_agent.core.audit import Actor, AuditLog, AuditResult, EntityRef
from ai_youtube_agent.core.db.database import Database
from ai_youtube_agent.core.db.repositories.asset import AssetRepository
from ai_youtube_agent.core.db.repositories.content import ContentItemRepository
from ai_youtube_agent.core.db.repositories.provenance import ProvenanceRepository
from ai_youtube_agent.core.db.repositories.review import RightsRecordRepository
from ai_youtube_agent.core.db.repositories.rights_assessment import (
    RightsAssessmentRepository,
)
from ai_youtube_agent.core.errors import DomainError

Clock = Callable[[], datetime]
ASSESSED_LEVELS = (RiskLevel.LOW, RiskLevel.MEDIUM, RiskLevel.HIGH)


class RightsRecordNotFoundError(DomainError):
    default_code = "domain.rights_record_not_found"
    default_user_message = "This rights record does not exist."
    default_http_status = HTTPStatus.NOT_FOUND


@dataclass(frozen=True)
class RightsAssessmentRun:
    """The result of one ``assess`` call.

    ``assessments`` holds the current assessment of every record that was
    assessed (a new row or the unchanged newest one), in record order.
    ``changed`` is the number of new rows and ``skipped_resolved`` holds the ids
    of the records a user resolved, which are not assessed.
    """

    content_item_id: str
    assessments: tuple[RightsAssessment, ...]
    changed: int
    skipped_resolved: tuple[str, ...]

    @property
    def assessed(self) -> int:
        return len(self.assessments)

    @property
    def level_counts(self) -> dict[RiskLevel, int]:
        return {
            level: sum(1 for a in self.assessments if a.level is level)
            for level in ASSESSED_LEVELS
        }


class RightsRiskEngine:
    def __init__(
        self, database: Database, audit: AuditLog, *, clock: Clock | None = None
    ) -> None:
        self._database = database
        self._audit = audit
        self._clock = clock

    def assess(self, content_item_id: str, *, actor: Actor) -> RightsAssessmentRun:
        with self._database.transaction() as connection:
            now = self._now()
            item = ContentItemRepository(connection).get(content_item_id)
            if item is None:
                raise ContentItemNotFoundError(
                    f"content item {content_item_id} does not exist"
                )
            rights = RightsRecordRepository(connection)
            assets = AssetRepository(connection)
            provenances = ProvenanceRepository(connection)
            history = RightsAssessmentRepository(connection)
            assessments: list[RightsAssessment] = []
            skipped: list[str] = []
            changed = 0
            for record in rights.list_by_content_item(content_item_id):
                if record.is_resolved:
                    skipped.append(record.id)
                    continue
                asset = assets.get(record.asset_ref)
                in_channel = asset is not None and asset.channel_id == item.channel_id
                provenance = provenances.latest(asset.id) if in_channel else None
                outcome = classify(asset, item.channel_id, provenance)
                newest = history.latest(record.id)
                assessment = RightsAssessment.create(
                    record.id,
                    content_item_id,
                    outcome,
                    assessed_by=actor,
                    clock=lambda: now,
                )
                if (
                    newest is not None
                    and newest.outcome_key() == assessment.outcome_key()
                    and record.risk_level is outcome.level
                ):
                    assessments.append(newest)
                    continue
                history.add(assessment)
                changed += 1
                assessments.append(assessment)
                if record.risk_level is not outcome.level:
                    rights.update(
                        record.with_risk_level(outcome.level, clock=lambda: now),
                        expected_updated_at=record.updated_at,
                    )
        run = RightsAssessmentRun(
            content_item_id=content_item_id,
            assessments=tuple(assessments),
            changed=changed,
            skipped_resolved=tuple(skipped),
        )
        if changed:
            counts = run.level_counts
            self._audit.record(
                "rights.assessed",
                actor,
                EntityRef("content_item", content_item_id),
                AuditResult.SUCCESS,
                {
                    "assessed": run.assessed,
                    "changed": changed,
                    "skipped_resolved": len(skipped),
                    "low": counts[RiskLevel.LOW],
                    "medium": counts[RiskLevel.MEDIUM],
                    "high": counts[RiskLevel.HIGH],
                },
            )
        return run

    def latest(self, rights_record_id: str) -> RightsAssessment | None:
        with self._database.transaction() as connection:
            _require_record(connection, rights_record_id)
            return RightsAssessmentRepository(connection).latest(rights_record_id)

    def history(self, rights_record_id: str) -> list[RightsAssessment]:
        with self._database.transaction() as connection:
            _require_record(connection, rights_record_id)
            return RightsAssessmentRepository(connection).list_by_rights_record(
                rights_record_id
            )

    def _now(self) -> datetime:
        """The time of assessment, read inside the transaction, so that a later
        insert never carries an earlier time. A clock that returns a naive or
        non-UTC time is a fault of the application, not an input error."""
        now = self._clock() if self._clock else datetime.now(UTC)
        if not isinstance(now, datetime) or now.utcoffset() != timedelta(0):
            raise ValueError("the clock must return a timezone-aware UTC time")
        return now


def _require_record(connection, rights_record_id: str) -> None:
    if RightsRecordRepository(connection).get(rights_record_id) is None:
        raise RightsRecordNotFoundError(
            f"rights record {rights_record_id} does not exist"
        )
