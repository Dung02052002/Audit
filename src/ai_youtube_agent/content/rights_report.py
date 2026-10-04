"""Rights Report (Prompt Pack v8, prompt G-082), context C6 Rights & Policy.

``RightsReporter`` builds a machine-readable and dashboard-friendly rights report
of one content item. The design was approved by the user on 2026-10-04:

- One report per content item, at any item status. An unknown item is
  ``ContentItemNotFoundError`` (404) and a wrong argument type is a ``TypeError``.
- The report is computed on demand and never stored: no table, no migration, no
  HTTP route and no audit event (reads are not audited). It changes nothing.
- Everything is read inside one transaction: the time, the item, the rights
  records, the newest assessment of each, the assets attached to the item (the
  same list the disclosure decider reads), the newest provenance of each asset,
  the newest disclosure decision and the gate evaluation. A clock that returns a
  naive or non-UTC time is a fault of the application (a plain ``ValueError``).
- ``RightsReport.to_dict`` is JSON-safe with a fixed key order, deterministic
  list order and ISO datetimes. The schema is named by ``REPORT_SCHEMA_VERSION``.
- ``records`` holds every rights record, ordered by ``created_at`` then ``id``.
  ``level`` is ``RightsRecord.risk_level``, the level the gate reads. The licence
  and the source of a record are only flags (``has_license``, ``has_source``).
- ``worst_level`` is the worst level of the unresolved records, ordered low,
  medium, high, unknown (unknown is the worst, as the gate fails closed). A
  resolved record does not block, so it does not count; it is ``None`` when no
  record is unresolved.
- ``assets`` lists each attached asset (ordered by ``created_at``, then ``id``)
  with flags of what its newest provenance holds and the sorted names of what is
  missing. No URL, licence, owner, attribution, proof, checksum or title is output.
- ``disclosure`` is the newest disclosure decision (its triggered rule codes in
  rationale order) or ``None``.
- ``gate`` is the verdict of the real ``RightsGate`` for a move to ``PUBLISHING``
  with the blocking levels of ``Settings.rights_block_levels`` and
  ``RepositoryFreshness``, so stale and outdated assessments block as they do
  at publish time. The gate is evaluated for the item as if it were approved,
  whatever its status, because only the rights rules are asked. The reason of
  each record comes from the same gate run over that single record; the output
  holds codes only, never the messages.
"""

import dataclasses
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from ai_youtube_agent.content.asset import Asset
from ai_youtube_agent.content.disclosure import DisclosureRecord
from ai_youtube_agent.content.hook_generator import ContentItemNotFoundError
from ai_youtube_agent.content.provenance import Provenance
from ai_youtube_agent.content.rights import RightsRecord, RiskLevel, RiskResolution
from ai_youtube_agent.content.rights_assessment import RightsAssessment
from ai_youtube_agent.core.audit import Actor, ActorKind
from ai_youtube_agent.core.config import Settings
from ai_youtube_agent.core.content_item import ContentStatus
from ai_youtube_agent.core.db.database import Database
from ai_youtube_agent.core.db.repositories.asset import AssetRepository
from ai_youtube_agent.core.db.repositories.content import ContentItemRepository
from ai_youtube_agent.core.db.repositories.disclosure import DisclosureRepository
from ai_youtube_agent.core.db.repositories.provenance import ProvenanceRepository
from ai_youtube_agent.core.db.repositories.review import RightsRecordRepository
from ai_youtube_agent.core.db.repositories.rights_assessment import (
    RightsAssessmentRepository,
)
from ai_youtube_agent.core.gates import GateContext
from ai_youtube_agent.core.publish_gate import RepositoryFreshness
from ai_youtube_agent.core.rights_gate import (
    FreshnessSource,
    RightsGate,
    blocking_levels_for,
)

REPORT_SCHEMA_VERSION = "rights-report-v1"
LEVEL_ORDER = (RiskLevel.LOW, RiskLevel.MEDIUM, RiskLevel.HIGH, RiskLevel.UNKNOWN)
REPORT_ACTOR = Actor(ActorKind.SYSTEM, "rights-report")
Clock = Callable[[], datetime]


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


@dataclass(frozen=True)
class AssessmentSummary:
    id: str
    level: str
    rule_codes: tuple[str, ...]
    rules_version: str
    asset_id: str | None
    provenance_id: str | None
    created_at: datetime

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "level": self.level,
            "rule_codes": list(self.rule_codes),
            "rules_version": self.rules_version,
            "asset_id": self.asset_id,
            "provenance_id": self.provenance_id,
            "created_at": _iso(self.created_at),
        }


@dataclass(frozen=True)
class RecordReport:
    id: str
    asset_ref: str
    status: str
    level: str
    resolved_at: datetime | None
    has_license: bool
    has_source: bool
    assessment: AssessmentSummary | None
    blocks: bool
    gate_code: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "asset_ref": self.asset_ref,
            "status": self.status,
            "level": self.level,
            "resolved_at": _iso(self.resolved_at),
            "has_license": self.has_license,
            "has_source": self.has_source,
            "assessment": self.assessment.to_dict() if self.assessment else None,
            "blocks": self.blocks,
            "gate_code": self.gate_code,
        }


@dataclass(frozen=True)
class AssetReport:
    asset_id: str
    kind: str
    category: str
    provenance_id: str | None
    has_source_url: bool
    has_license: bool
    has_license_url: bool
    has_proof: bool
    has_owner: bool
    has_file_sha256: bool
    has_attribution: bool
    missing: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "asset_id": self.asset_id,
            "kind": self.kind,
            "category": self.category,
            "provenance_id": self.provenance_id,
            "has_source_url": self.has_source_url,
            "has_license": self.has_license,
            "has_license_url": self.has_license_url,
            "has_proof": self.has_proof,
            "has_owner": self.has_owner,
            "has_file_sha256": self.has_file_sha256,
            "has_attribution": self.has_attribution,
            "missing": list(self.missing),
        }


@dataclass(frozen=True)
class DisclosureSummary:
    decision_id: str
    decision: str
    rule_set_version: str
    rule_codes: tuple[str, ...]
    created_at: datetime

    def to_dict(self) -> dict[str, Any]:
        return {
            "decision_id": self.decision_id,
            "decision": self.decision,
            "rule_set_version": self.rule_set_version,
            "rule_codes": list(self.rule_codes),
            "created_at": _iso(self.created_at),
        }


@dataclass(frozen=True)
class BlockingRecord:
    record_id: str
    asset_ref: str
    code: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "record_id": self.record_id,
            "asset_ref": self.asset_ref,
            "code": self.code,
        }


@dataclass(frozen=True)
class GateSummary:
    blocks: bool
    blocking_levels: tuple[str, ...]
    blocking_records: tuple[BlockingRecord, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "blocks": self.blocks,
            "blocking_levels": list(self.blocking_levels),
            "blocking_records": [r.to_dict() for r in self.blocking_records],
        }


@dataclass(frozen=True)
class RightsReport:
    content_item_id: str
    channel_id: str
    generated_at: datetime
    worst_level: str | None
    records: tuple[RecordReport, ...]
    assets: tuple[AssetReport, ...]
    disclosure: DisclosureSummary | None
    gate: GateSummary
    schema_version: str = REPORT_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "content_item_id": self.content_item_id,
            "channel_id": self.channel_id,
            "generated_at": _iso(self.generated_at),
            "worst_level": self.worst_level,
            "records": [r.to_dict() for r in self.records],
            "assets": [a.to_dict() for a in self.assets],
            "disclosure": self.disclosure.to_dict() if self.disclosure else None,
            "gate": self.gate.to_dict(),
        }


class _OneRecord:
    """A ``RightsSource`` that holds one record, to ask the gate about it alone."""

    def __init__(self, record: RightsRecord) -> None:
        self._record = record

    def list_by_content_item(self, content_item_id: str) -> Sequence[RightsRecord]:
        return [self._record]


class RightsReporter:
    def __init__(
        self, database: Database, settings: Settings, *, clock: Clock | None = None
    ) -> None:
        self._database = database
        self._blocking_levels = blocking_levels_for(settings.rights_block_levels)
        self._clock = clock

    def report(self, content_item_id: str) -> RightsReport:
        if not isinstance(content_item_id, str):
            raise TypeError("content_item_id must be text")
        with self._database.transaction() as connection:
            now = self._now()
            item = ContentItemRepository(connection).get(content_item_id)
            if item is None:
                raise ContentItemNotFoundError(
                    f"content item {content_item_id} does not exist"
                )
            rights = RightsRecordRepository(connection)
            records = sorted(
                rights.list_by_content_item(item.id),
                key=lambda r: (r.created_at, r.id),
            )
            history = RightsAssessmentRepository(connection)
            assets_repo = AssetRepository(connection)
            provenances = ProvenanceRepository(connection)
            freshness = RepositoryFreshness(history, assets_repo, provenances)
            context = GateContext(
                dataclasses.replace(item, status=ContentStatus.APPROVED),
                ContentStatus.PUBLISHING,
                REPORT_ACTOR,
                now,
            )
            record_reports = tuple(
                self._record_report(r, history.latest(r.id), freshness, context)
                for r in records
            )
            overall = RightsGate(
                rights, blocking_levels=self._blocking_levels, freshness=freshness
            ).evaluate(context)
            assets = sorted(
                assets_repo.list_by_content_item(item.id),
                key=lambda a: (a.created_at, a.id),
            )
            asset_reports = tuple(
                _asset_report(a, provenances.latest(a.id)) for a in assets
            )
            disclosure = DisclosureRepository(connection).latest(item.id)
        levels = sorted(level.value for level in self._blocking_levels)
        return RightsReport(
            content_item_id=item.id,
            channel_id=item.channel_id,
            generated_at=now,
            worst_level=_worst_level(records),
            records=record_reports,
            assets=asset_reports,
            disclosure=_disclosure_summary(disclosure) if disclosure else None,
            gate=GateSummary(
                blocks=not overall.is_passed,
                blocking_levels=tuple(levels),
                blocking_records=tuple(
                    BlockingRecord(r.id, r.asset_ref, r.gate_code)
                    for r in record_reports
                    if r.gate_code is not None
                ),
            ),
        )

    def _record_report(
        self,
        record: RightsRecord,
        assessment: RightsAssessment | None,
        freshness: FreshnessSource,
        context: GateContext,
    ) -> RecordReport:
        result = RightsGate(
            _OneRecord(record),
            blocking_levels=self._blocking_levels,
            freshness=freshness,
        ).evaluate(context)
        code = result.reasons[0].code if result.reasons else None
        return RecordReport(
            id=record.id,
            asset_ref=record.asset_ref,
            status=record.resolution.value,
            level=record.risk_level.value,
            resolved_at=record.resolved_at,
            has_license=record.license is not None,
            has_source=bool(record.source),
            assessment=_assessment_summary(assessment) if assessment else None,
            blocks=code is not None,
            gate_code=code,
        )

    def _now(self) -> datetime:
        """The time of the report, read inside the transaction. A clock that
        returns a naive or non-UTC time is a fault of the application."""
        now = self._clock() if self._clock else datetime.now(UTC)
        if not isinstance(now, datetime) or now.utcoffset() != timedelta(0):
            raise ValueError("the clock must return a timezone-aware UTC time")
        return now


def _worst_level(records: Sequence[RightsRecord]) -> str | None:
    levels = [
        r.risk_level for r in records if r.resolution is RiskResolution.UNRESOLVED
    ]
    if not levels:
        return None
    return max(levels, key=LEVEL_ORDER.index).value


def _assessment_summary(assessment: RightsAssessment) -> AssessmentSummary:
    return AssessmentSummary(
        id=assessment.id,
        level=assessment.level.value,
        rule_codes=assessment.rule_codes,
        rules_version=assessment.rules_version,
        asset_id=assessment.asset_id,
        provenance_id=assessment.provenance_id,
        created_at=assessment.created_at,
    )


def _asset_report(asset: Asset, provenance: Provenance | None) -> AssetReport:
    def has(*names: str) -> bool:
        return provenance is not None and any(
            getattr(provenance, name) is not None for name in names
        )

    flags = {
        "source_url": has("source_url"),
        "license": has("license_name", "license_ref"),
        "license_url": has("license_url"),
        "proof": has("proof"),
        "owner": has("owner"),
        "file_sha256": has("file_sha256"),
        "attribution": has("attribution"),
    }
    return AssetReport(
        asset_id=asset.id,
        kind=asset.kind.value,
        category=asset.category.value,
        provenance_id=provenance.id if provenance else None,
        has_source_url=flags["source_url"],
        has_license=flags["license"],
        has_license_url=flags["license_url"],
        has_proof=flags["proof"],
        has_owner=flags["owner"],
        has_file_sha256=flags["file_sha256"],
        has_attribution=flags["attribution"],
        missing=tuple(sorted(name for name, present in flags.items() if not present)),
    )


def _disclosure_summary(record: DisclosureRecord) -> DisclosureSummary:
    return DisclosureSummary(
        decision_id=record.id,
        decision=record.decision.value,
        rule_set_version=record.rule_set_version,
        rule_codes=tuple(e.code for e in record.rationale if e.triggered),
        created_at=record.created_at,
    )
