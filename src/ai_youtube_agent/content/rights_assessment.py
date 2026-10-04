"""Rights assessment (Prompt Pack v8, prompt #078), context C6 Rights & Policy.

A ``RightsAssessment`` is one entry of the history of the risk engine's
judgement of a ``RightsRecord``. ``classify`` is the pure rule table that gives
the level. The rules were approved by the user on 2026-10-04:

- ``RightsAssessment``: ``id``; ``rights_record_id`` and ``content_item_id``
  (what is judged); ``asset_id`` (the registered ``Asset`` of the record, or
  ``None``); ``level`` (``RiskLevel`` low, medium or high, never unknown);
  ``rule_codes`` (stable codes, at least one); ``rules_version``;
  ``provenance_id`` (the current ``Provenance`` of the asset when it was judged,
  or ``None``; it needs an ``asset_id``); ``assessed_by`` (an ``Actor``);
  ``created_at`` (UTC). The record is frozen and the history is append only.
- ``classify(asset, item_channel_id, provenance)`` is deterministic, with no AI
  model and no clock. One rule code per outcome:

  1. asset missing or in another channel: high, ``asset.not_registered``
  2. category unknown: high, ``asset.category_unknown``
  3. licensed, no provenance: high, ``licensed.no_provenance``
  4. licensed, no licence name and no licence reference: high,
     ``licensed.no_licence``
  5. licensed, a licence only: medium, ``licensed.licence_without_evidence``
  6. licensed, a licence and a licence URL or a proof: low,
     ``licensed.documented``
  7. public_domain, no provenance: medium, ``public_domain.no_provenance``
  8. public_domain, no source URL: medium, ``public_domain.no_source_url``
  9. public_domain, a source URL only: medium, ``public_domain.source_only``
  10. public_domain, a source URL and a proof or a licence URL: low,
      ``public_domain.documented``
  11. user_owned, no provenance: medium, ``user_owned.no_provenance``
  12. user_owned, an owner and a proof or a checksum: low,
      ``user_owned.documented``
  13. user_owned, otherwise: medium, ``user_owned.unproven``
  14. generated: low, ``generated.declared``

- ``outcome_key`` is what the history compares: level, codes, provenance id and
  rules version. When the asset is registered the outcome carries the id of the
  current provenance, so a new provenance record gives a new assessment.
- A code, an id or an error message never holds a URL, a licence or a text.
"""

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from ai_youtube_agent.content.asset import Asset, AssetCategory
from ai_youtube_agent.content.provenance import Provenance
from ai_youtube_agent.content.rights import RiskLevel
from ai_youtube_agent.core.audit import Actor

RULES_VERSION = "rights-rules-v1"
NOT_REGISTERED = "asset.not_registered"
CATEGORY_UNKNOWN = "asset.category_unknown"
LICENSED_NO_PROVENANCE = "licensed.no_provenance"
LICENSED_NO_LICENCE = "licensed.no_licence"
LICENSED_WITHOUT_EVIDENCE = "licensed.licence_without_evidence"
LICENSED_DOCUMENTED = "licensed.documented"
PUBLIC_DOMAIN_NO_PROVENANCE = "public_domain.no_provenance"
PUBLIC_DOMAIN_NO_SOURCE_URL = "public_domain.no_source_url"
PUBLIC_DOMAIN_SOURCE_ONLY = "public_domain.source_only"
PUBLIC_DOMAIN_DOCUMENTED = "public_domain.documented"
USER_OWNED_NO_PROVENANCE = "user_owned.no_provenance"
USER_OWNED_DOCUMENTED = "user_owned.documented"
USER_OWNED_UNPROVEN = "user_owned.unproven"
GENERATED_DECLARED = "generated.declared"
Clock = Callable[[], datetime]


@dataclass(frozen=True)
class Outcome:
    """What the rules say about one record: no id, no actor, no time."""

    level: RiskLevel
    rule_codes: tuple[str, ...]
    asset_id: str | None
    provenance_id: str | None


@dataclass(frozen=True)
class RightsAssessment:
    id: str
    rights_record_id: str
    content_item_id: str
    asset_id: str | None
    level: RiskLevel
    rule_codes: tuple[str, ...]
    rules_version: str
    provenance_id: str | None
    assessed_by: Actor
    created_at: datetime

    def __post_init__(self) -> None:
        for name in ("id", "rights_record_id", "content_item_id", "rules_version"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must not be empty")
        for name in ("asset_id", "provenance_id"):
            value = getattr(self, name)
            if value is not None and (not isinstance(value, str) or not value.strip()):
                raise ValueError(f"{name} must not be empty")
        if not isinstance(self.level, RiskLevel):
            raise TypeError("level must be a RiskLevel")
        if self.level is RiskLevel.UNKNOWN:
            raise ValueError("an assessment gives a level other than unknown")
        if not isinstance(self.rule_codes, tuple) or not self.rule_codes:
            raise ValueError("rule_codes needs at least one code")
        for code in self.rule_codes:
            if not isinstance(code, str) or not code or code != code.strip():
                raise ValueError("a rule code must be text without outer whitespace")
        if self.provenance_id is not None and self.asset_id is None:
            raise ValueError("provenance_id needs an asset_id")
        if not isinstance(self.assessed_by, Actor):
            raise TypeError("assessed_by must be an Actor")
        if not isinstance(self.created_at, datetime):
            raise TypeError("created_at must be a datetime")
        if self.created_at.utcoffset() != timedelta(0):
            raise ValueError("created_at must be timezone-aware UTC")

    @classmethod
    def create(
        cls,
        rights_record_id: str,
        content_item_id: str,
        outcome: Outcome,
        *,
        assessed_by: Actor,
        clock: Clock | None = None,
    ) -> "RightsAssessment":
        return cls(
            id=uuid.uuid4().hex,
            rights_record_id=rights_record_id,
            content_item_id=content_item_id,
            asset_id=outcome.asset_id,
            level=outcome.level,
            rule_codes=outcome.rule_codes,
            rules_version=RULES_VERSION,
            provenance_id=outcome.provenance_id,
            assessed_by=assessed_by,
            created_at=clock() if clock else datetime.now(UTC),
        )

    def outcome_key(self) -> tuple[Any, ...]:
        """What the assessment says: equal keys are the same outcome."""
        return (self.level, self.rule_codes, self.provenance_id, self.rules_version)

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "rights_record_id": self.rights_record_id,
            "content_item_id": self.content_item_id,
            "asset_id": self.asset_id,
            "level": self.level.value,
            "rule_codes": list(self.rule_codes),
            "rules_version": self.rules_version,
            "provenance_id": self.provenance_id,
            "assessed_by": {
                "kind": self.assessed_by.kind.value,
                "id": self.assessed_by.id,
            },
            "created_at": self.created_at.isoformat(),
        }


class _Assets(Protocol):
    def get(self, asset_id: str) -> Asset | None: ...


class _Provenances(Protocol):
    def latest(self, asset_id: str) -> Provenance | None: ...


def current_facts(
    assets: _Assets, provenances: _Provenances, asset_ref: str, channel_id: str
) -> tuple[Asset | None, Provenance | None]:
    """The facts a record is judged on: the asset of the ``asset_ref``, only when it
    exists and is registered in the channel, and its latest provenance. The risk
    engine and the publish gate's freshness check both read through this function,
    so that they never disagree about what is current."""
    asset = assets.get(asset_ref)
    if asset is None or asset.channel_id != channel_id:
        return None, None
    return asset, provenances.latest(asset.id)


def classify(
    asset: Asset | None, item_channel_id: str, provenance: Provenance | None
) -> Outcome:
    """The level of one record, from its asset and the current provenance."""
    if asset is None or asset.channel_id != item_channel_id:
        return Outcome(RiskLevel.HIGH, (NOT_REGISTERED,), None, None)
    provenance_id = provenance.id if provenance is not None else None
    level, code = _rule(asset.category, provenance)
    return Outcome(level, (code,), asset.id, provenance_id)


def _rule(
    category: AssetCategory, provenance: Provenance | None
) -> tuple[RiskLevel, str]:
    if category is AssetCategory.GENERATED:
        return RiskLevel.LOW, GENERATED_DECLARED
    if category is AssetCategory.LICENSED:
        return _licensed(provenance)
    if category is AssetCategory.PUBLIC_DOMAIN:
        return _public_domain(provenance)
    if category is AssetCategory.USER_OWNED:
        return _user_owned(provenance)
    return RiskLevel.HIGH, CATEGORY_UNKNOWN


def _licensed(provenance: Provenance | None) -> tuple[RiskLevel, str]:
    if provenance is None:
        return RiskLevel.HIGH, LICENSED_NO_PROVENANCE
    if provenance.license_name is None and provenance.license_ref is None:
        return RiskLevel.HIGH, LICENSED_NO_LICENCE
    if provenance.license_url is not None or provenance.proof is not None:
        return RiskLevel.LOW, LICENSED_DOCUMENTED
    return RiskLevel.MEDIUM, LICENSED_WITHOUT_EVIDENCE


def _public_domain(provenance: Provenance | None) -> tuple[RiskLevel, str]:
    if provenance is None:
        return RiskLevel.MEDIUM, PUBLIC_DOMAIN_NO_PROVENANCE
    if provenance.source_url is None:
        return RiskLevel.MEDIUM, PUBLIC_DOMAIN_NO_SOURCE_URL
    if provenance.proof is not None or provenance.license_url is not None:
        return RiskLevel.LOW, PUBLIC_DOMAIN_DOCUMENTED
    return RiskLevel.MEDIUM, PUBLIC_DOMAIN_SOURCE_ONLY


def _user_owned(provenance: Provenance | None) -> tuple[RiskLevel, str]:
    if provenance is None:
        return RiskLevel.MEDIUM, USER_OWNED_NO_PROVENANCE
    if provenance.owner is not None and (
        provenance.proof is not None or provenance.file_sha256 is not None
    ):
        return RiskLevel.LOW, USER_OWNED_DOCUMENTED
    return RiskLevel.MEDIUM, USER_OWNED_UNPROVEN
