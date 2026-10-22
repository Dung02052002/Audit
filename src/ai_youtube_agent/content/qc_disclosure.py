"""AI Disclosure QC check (Prompt Pack v8, prompt K-128), context C10 Quality
(Test & QC).

Verifies that the configured AI disclosure requirement (#081) is satisfied for a
content item before QC/publish:
- Fails (``QCStatus.FAIL``) when no ``DisclosureRecord`` exists for the content item.
- Fails (``QCStatus.FAIL``) when the ``DisclosureRecord`` was evaluated under an
  outdated rule set version (default ``disclosure-rules-v1``).
- Fails (``QCStatus.FAIL``) when the content item's attached generated visual
  assets have drifted since the ``DisclosureRecord`` was recorded.
- Fails (``QCStatus.FAIL``) when ``DisclosureDecision.REQUIRED`` is in effect
  and no publish metadata artifact satisfies ``contains_synthetic_media=True``
  bound to ``disclosure_decision_id == record.id``.
- Fails (``QCStatus.FAIL``) when a publish metadata artifact is present (or
  ``require_metadata=True``) and its ``disclosure_decision_id`` does not match
  the latest ``DisclosureRecord.id``.
- Warns (``QCStatus.WARN``) when ``DisclosureDecision.NOT_REQUIRED`` is in effect
  but publish metadata over-discloses with ``contains_synthetic_media=True``.
- Passes (``QCStatus.PASS``) when the latest ``DisclosureRecord`` is current and
  satisfied by the publish metadata.
"""

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final

from ai_youtube_agent.content.asset import AssetCategory
from ai_youtube_agent.content.disclosure import (
    FACT_NAMES,
    DisclosureDecision,
    DisclosureRecord,
)
from ai_youtube_agent.content.disclosure_rule import (
    DISCLOSURE_RULES,
    GENERATED_VISUAL_KINDS,
    DisclosureFacts,
    DisclosureRuleCatalog,
    evaluate_disclosure,
)
from ai_youtube_agent.content.hook_generator import ContentItemNotFoundError
from ai_youtube_agent.content.policy_rule import RuleSet
from ai_youtube_agent.content.qc import QCCheck, QCStatus
from ai_youtube_agent.core.artifact import ArtifactKind
from ai_youtube_agent.core.artifact_storage import ArtifactStorage
from ai_youtube_agent.core.db.database import Database
from ai_youtube_agent.core.db.repositories.asset import AssetRepository
from ai_youtube_agent.core.db.repositories.content import (
    ArtifactRepository,
    ContentItemRepository,
)
from ai_youtube_agent.core.db.repositories.disclosure import DisclosureRepository

DISCLOSURE_QC_CHECK_NAME: Final[str] = "qc.ai_disclosure"


@dataclass(frozen=True)
class DisclosureQCEvaluation:
    """Structured outcome of the AI disclosure QC check."""

    content_item_id: str
    decision_id: str | None
    decision: DisclosureDecision | None
    rule_set_version: str | None
    metadata_satisfied: bool
    check: QCCheck

    def __post_init__(self) -> None:
        if not isinstance(self.check, QCCheck):
            raise TypeError("check must be a QCCheck")
        if self.check.name != DISCLOSURE_QC_CHECK_NAME:
            raise ValueError(f"check.name must be {DISCLOSURE_QC_CHECK_NAME!r}")

    @property
    def is_blocking(self) -> bool:
        return self.check.status is QCStatus.FAIL

    def to_dict(self) -> dict[str, Any]:
        return {
            "content_item_id": self.content_item_id,
            "decision_id": self.decision_id,
            "decision": self.decision.value if self.decision is not None else None,
            "rule_set_version": self.rule_set_version,
            "metadata_satisfied": self.metadata_satisfied,
            "is_blocking": self.is_blocking,
            "check": self.check.as_dict(),
        }


def evaluate_disclosure_qc(
    content_item_id: str,
    channel_id: str,
    record: DisclosureRecord | None,
    *,
    generated_visual_asset_ids: tuple[str, ...] = (),
    metadata_payload: Mapping[str, Any] | None = None,
    require_metadata: bool = False,
    expected_rule_set: RuleSet = DISCLOSURE_RULES,
) -> DisclosureQCEvaluation:
    """Pure evaluation of AI disclosure QC compliance."""
    if not isinstance(expected_rule_set, RuleSet):
        raise TypeError("expected_rule_set must be a RuleSet")

    if record is None:
        return DisclosureQCEvaluation(
            content_item_id=content_item_id,
            decision_id=None,
            decision=None,
            rule_set_version=None,
            metadata_satisfied=False,
            check=QCCheck(
                name=DISCLOSURE_QC_CHECK_NAME,
                status=QCStatus.FAIL,
                detail="No AI disclosure decision record exists for content item",
            ),
        )

    if record.content_item_id != content_item_id or record.channel_id != channel_id:
        return DisclosureQCEvaluation(
            content_item_id=content_item_id,
            decision_id=record.id,
            decision=record.decision,
            rule_set_version=record.rule_set_version,
            metadata_satisfied=False,
            check=QCCheck(
                name=DISCLOSURE_QC_CHECK_NAME,
                status=QCStatus.FAIL,
                detail="DisclosureRecord content_item_id/channel_id mismatch",
            ),
        )

    if record.rule_set_version != expected_rule_set.stored_version:
        return DisclosureQCEvaluation(
            content_item_id=content_item_id,
            decision_id=record.id,
            decision=record.decision,
            rule_set_version=record.rule_set_version,
            metadata_satisfied=False,
            check=QCCheck(
                name=DISCLOSURE_QC_CHECK_NAME,
                status=QCStatus.FAIL,
                detail=(
                    f"AI disclosure decision uses outdated rule set "
                    f"{record.rule_set_version!r} "
                    f"(expected {expected_rule_set.stored_version!r})"
                ),
            ),
        )

    canonical_visual_ids = tuple(sorted(set(generated_visual_asset_ids)))
    if record.sources.asset_ids != canonical_visual_ids:
        return DisclosureQCEvaluation(
            content_item_id=content_item_id,
            decision_id=record.id,
            decision=record.decision,
            rule_set_version=record.rule_set_version,
            metadata_satisfied=False,
            check=QCCheck(
                name=DISCLOSURE_QC_CHECK_NAME,
                status=QCStatus.FAIL,
                detail=(
                    f"AI disclosure decision is stale: attached generated visual "
                    f"assets changed from {list(record.sources.asset_ids)} to "
                    f"{list(canonical_visual_ids)}"
                ),
            ),
        )

    if metadata_payload is None:
        if require_metadata:
            return DisclosureQCEvaluation(
                content_item_id=content_item_id,
                decision_id=record.id,
                decision=record.decision,
                rule_set_version=record.rule_set_version,
                metadata_satisfied=False,
                check=QCCheck(
                    name=DISCLOSURE_QC_CHECK_NAME,
                    status=QCStatus.FAIL,
                    detail=(
                        "Missing publish metadata artifact to verify AI disclosure "
                        "satisfaction"
                    ),
                ),
            )
        if record.decision is DisclosureDecision.REQUIRED:
            return DisclosureQCEvaluation(
                content_item_id=content_item_id,
                decision_id=record.id,
                decision=record.decision,
                rule_set_version=record.rule_set_version,
                metadata_satisfied=False,
                check=QCCheck(
                    name=DISCLOSURE_QC_CHECK_NAME,
                    status=QCStatus.FAIL,
                    detail=(
                        "AI disclosure is required but no publish metadata artifact "
                        "exists to satisfy contains_synthetic_media=True"
                    ),
                ),
            )
        return DisclosureQCEvaluation(
            content_item_id=content_item_id,
            decision_id=record.id,
            decision=record.decision,
            rule_set_version=record.rule_set_version,
            metadata_satisfied=True,
            check=QCCheck(
                name=DISCLOSURE_QC_CHECK_NAME,
                status=QCStatus.PASS,
                detail=(
                    f"AI disclosure satisfied: decision={record.decision.value} "
                    f"({record.rule_set_version})"
                ),
            ),
        )

    meta_decision_id = metadata_payload.get("disclosure_decision_id")
    meta_synthetic = metadata_payload.get("contains_synthetic_media")

    if meta_decision_id != record.id:
        return DisclosureQCEvaluation(
            content_item_id=content_item_id,
            decision_id=record.id,
            decision=record.decision,
            rule_set_version=record.rule_set_version,
            metadata_satisfied=False,
            check=QCCheck(
                name=DISCLOSURE_QC_CHECK_NAME,
                status=QCStatus.FAIL,
                detail=(
                    f"Publish metadata disclosure_decision_id {meta_decision_id!r} "
                    f"does not match latest DisclosureRecord {record.id!r}"
                ),
            ),
        )

    if not isinstance(meta_synthetic, bool):
        return DisclosureQCEvaluation(
            content_item_id=content_item_id,
            decision_id=record.id,
            decision=record.decision,
            rule_set_version=record.rule_set_version,
            metadata_satisfied=False,
            check=QCCheck(
                name=DISCLOSURE_QC_CHECK_NAME,
                status=QCStatus.FAIL,
                detail="Publish metadata contains_synthetic_media must be a boolean",
            ),
        )

    if record.decision is DisclosureDecision.REQUIRED and not meta_synthetic:
        return DisclosureQCEvaluation(
            content_item_id=content_item_id,
            decision_id=record.id,
            decision=record.decision,
            rule_set_version=record.rule_set_version,
            metadata_satisfied=False,
            check=QCCheck(
                name=DISCLOSURE_QC_CHECK_NAME,
                status=QCStatus.FAIL,
                detail=(
                    "AI disclosure is required by decision record, but publish "
                    "metadata has contains_synthetic_media=False"
                ),
            ),
        )

    if record.decision is DisclosureDecision.NOT_REQUIRED and meta_synthetic:
        return DisclosureQCEvaluation(
            content_item_id=content_item_id,
            decision_id=record.id,
            decision=record.decision,
            rule_set_version=record.rule_set_version,
            metadata_satisfied=True,
            check=QCCheck(
                name=DISCLOSURE_QC_CHECK_NAME,
                status=QCStatus.WARN,
                detail=(
                    "Publish metadata sets contains_synthetic_media=True although "
                    "disclosure decision is not_required"
                ),
            ),
        )

    return DisclosureQCEvaluation(
        content_item_id=content_item_id,
        decision_id=record.id,
        decision=record.decision,
        rule_set_version=record.rule_set_version,
        metadata_satisfied=True,
        check=QCCheck(
            name=DISCLOSURE_QC_CHECK_NAME,
            status=QCStatus.PASS,
            detail=(
                f"AI disclosure requirement satisfied: "
                f"decision={record.decision.value}, "
                f"contains_synthetic_media={meta_synthetic} "
                f"({record.rule_set_version})"
            ),
        ),
    )


class DisclosureQCChecker:
    """Loads disclosure state and publish metadata from repositories and runs QC."""

    def __init__(
        self,
        database: Database,
        *,
        storage: ArtifactStorage | None = None,
        rule_set: RuleSet = DISCLOSURE_RULES,
        catalog: DisclosureRuleCatalog | None = None,
    ) -> None:
        if not isinstance(rule_set, RuleSet):
            raise TypeError("rule_set must be a RuleSet")
        self._database = database
        self._storage = storage
        self._rule_set = rule_set
        self._catalog = catalog

    def evaluate(
        self,
        content_item_id: str,
        *,
        metadata_payload: Mapping[str, Any] | None = None,
        require_metadata: bool = False,
    ) -> DisclosureQCEvaluation:
        if not isinstance(content_item_id, str) or not content_item_id.strip():
            raise ValueError("content_item_id must be a non-empty string")

        with self._database.transaction() as connection:
            item = ContentItemRepository(connection).get(content_item_id)
            if item is None:
                raise ContentItemNotFoundError(
                    f"content item {content_item_id} does not exist"
                )
            record = DisclosureRepository(connection).latest(item.id)
            attached_assets = AssetRepository(connection).list_by_content_item(item.id)
            meta_artifacts = ArtifactRepository(connection).list_by_content_item(
                item.id, kind=ArtifactKind.METADATA
            )

        generated_visual_ids = tuple(
            sorted(
                {
                    asset.id
                    for asset in attached_assets
                    if asset.category is AssetCategory.GENERATED
                    and asset.kind in GENERATED_VISUAL_KINDS
                }
            )
        )

        # Verify deterministic rule evaluation matches stored decision when rules match
        if (
            record is not None
            and record.rule_set_version == self._rule_set.stored_version
            and record.sources.asset_ids == generated_visual_ids
        ):
            facts = DisclosureFacts(
                **{name: (name in record.sources.facts) for name in FACT_NAMES}
            )
            re_eval = evaluate_disclosure(
                item.id,
                item.channel_id,
                facts,
                attached_assets,
                rule_set=self._rule_set,
                catalog=self._catalog,
            )
            if re_eval.decision != record.decision:
                return DisclosureQCEvaluation(
                    content_item_id=item.id,
                    decision_id=record.id,
                    decision=record.decision,
                    rule_set_version=record.rule_set_version,
                    metadata_satisfied=False,
                    check=QCCheck(
                        name=DISCLOSURE_QC_CHECK_NAME,
                        status=QCStatus.FAIL,
                        detail=(
                            "Stored DisclosureRecord decision "
                            f"{record.decision.value!r} "
                            f"disagrees with rule evaluation {re_eval.decision.value!r}"
                        ),
                    ),
                )

        resolved_metadata = metadata_payload
        effective_require_metadata = require_metadata or bool(meta_artifacts)
        if resolved_metadata is None and meta_artifacts and self._storage is not None:
            latest_meta = meta_artifacts[-1]
            try:  # a missing or unreadable file counts as no metadata
                loaded = json.loads(self._storage.read(latest_meta.uri).decode("utf-8"))
                if isinstance(loaded, dict):
                    resolved_metadata = loaded
            except Exception:
                resolved_metadata = None

        return evaluate_disclosure_qc(
            content_item_id=item.id,
            channel_id=item.channel_id,
            record=record,
            generated_visual_asset_ids=generated_visual_ids,
            metadata_payload=resolved_metadata,
            require_metadata=effective_require_metadata,
            expected_rule_set=self._rule_set,
        )

    def check(
        self,
        content_item_id: str,
        *,
        metadata_payload: Mapping[str, Any] | None = None,
        require_metadata: bool = False,
    ) -> QCCheck:
        return self.evaluate(
            content_item_id,
            metadata_payload=metadata_payload,
            require_metadata=require_metadata,
        ).check
