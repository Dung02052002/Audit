"""B-roll Manager (Prompt Pack v8, prompt #108), context C9 LongForm Production.

``BrollManager`` assigns approved/attached visual and audio assets (backgrounds,
overlays, SFX) to the scenes of a ``LongFormProject`` timeline.

Rules:
- While ``LONGFORM_ENABLED`` is off (``FeatureFlags.longform_enabled`` is
  ``False``), every call is refused with ``LongFormDisabledError`` before reading
  or writing.
- The target ``LongFormProject`` must have planned scenes
  (``timeline.scenes`` non-empty) and match ``expected_version``.
- Every assigned asset is checked against decision D5 (exists, belongs to the
  item's channel, is attached via ``AssetUsage``, has a declared
  ``AssetCategory`` other than ``unknown``, and matches ``ROLE_KINDS[role]``)
  and its current provenance is snapshotted into ``SceneAsset.provenance_id``.
- In addition, any asset that has an unresolved ``RightsRecord`` on the content
  item with a blocking risk level (``high`` or ``unknown``) is refused with
  ``domain.broll_asset_rights_blocked`` (and skipped during automatic
  selection). The rights gate, rights QC and publish gate remain authoritative
  at publish time.
- ``assign`` applies explicit ``BrollAssignment`` entries to target scenes:
  a ``background`` assignment replaces any existing ``background`` on that scene
  (keeping overlays and SFX), while ``overlay`` and ``sfx`` assignments append
  to the scene's assets (or replace existing assets of that role when
  ``replace_role=True``).
- ``auto_assign_backgrounds`` selects eligible attached visual assets
  (``AssetKind.VIDEO`` or ``AssetKind.IMAGE``) in deterministic attachment order
  and assigns them round-robin to scenes without a background (or all scenes
  when ``replace_existing=True``).
- Updating the timeline recomputes ``LongFormProjectStatus`` via
  ``longform_readiness`` and audits ``longform.broll_assigned`` with scalar IDs
  and counts only.
"""

import sqlite3
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum
from http import HTTPStatus

from ai_youtube_agent.content.asset import Asset, AssetCategory, AssetKind
from ai_youtube_agent.content.longform_captions import check_longform_captions
from ai_youtube_agent.content.longform_project import LongFormProject
from ai_youtube_agent.content.longform_script_generator import LongFormDisabledError
from ai_youtube_agent.content.longform_service import (
    LongFormProjectError,
    LongFormProjectErrorCode,
    longform_readiness,
)
from ai_youtube_agent.content.rights import RiskLevel, RiskResolution
from ai_youtube_agent.content.shorts_timeline import (
    ROLE_KINDS,
    AssetFit,
    AssetRole,
    Box,
    Scene,
    SceneAsset,
)

# Backwards-compatible alias for callers referring to CropMode.
CropMode = AssetFit
from ai_youtube_agent.core.audit import (
    Actor,
    AuditLog,
    AuditResult,
    EntityRef,
)
from ai_youtube_agent.core.content_item import ContentItem
from ai_youtube_agent.core.db.database import ConcurrencyError, Database
from ai_youtube_agent.core.db.repositories.asset import (
    AssetRepository,
    AssetUsageRepository,
)
from ai_youtube_agent.core.db.repositories.content import ContentItemRepository
from ai_youtube_agent.core.db.repositories.longform import LongFormProjectRepository
from ai_youtube_agent.core.db.repositories.provenance import ProvenanceRepository
from ai_youtube_agent.core.db.repositories.review import RightsRecordRepository
from ai_youtube_agent.core.errors import DomainError
from ai_youtube_agent.core.flags import FeatureFlags

Clock = Callable[[], datetime]

BROLL_ASSIGNED = "longform.broll_assigned"
BLOCKING_RISK_LEVELS = frozenset({RiskLevel.HIGH, RiskLevel.UNKNOWN})


class BrollErrorCode(StrEnum):
    BROLL = "domain.broll_invalid"
    PROJECT_NOT_FOUND = "domain.broll_project_not_found"
    PROJECT_CONFLICT = "domain.broll_project_conflict"
    NO_SCENES = "domain.broll_no_scenes"
    SCENE_OUT_OF_RANGE = "domain.broll_scene_out_of_range"
    EMPTY_ASSIGNMENTS = "domain.broll_empty_assignments"
    NO_ELIGIBLE_ASSETS = "domain.broll_no_eligible_assets"
    ASSET_RIGHTS_BLOCKED = "domain.broll_asset_rights_blocked"


USER_MESSAGES = {
    BrollErrorCode.BROLL: "The B-roll assignment could not be saved.",
    BrollErrorCode.PROJECT_NOT_FOUND: "This LongForm project does not exist.",
    BrollErrorCode.PROJECT_CONFLICT: (
        "The LongForm project changed in the meantime. Reload it and try again."
    ),
    BrollErrorCode.NO_SCENES: (
        "Plan the scenes of the LongForm project before assigning B-roll."
    ),
    BrollErrorCode.SCENE_OUT_OF_RANGE: (
        "A B-roll assignment references a scene that does not exist."
    ),
    BrollErrorCode.EMPTY_ASSIGNMENTS: "Provide at least one B-roll assignment.",
    BrollErrorCode.NO_ELIGIBLE_ASSETS: (
        "No eligible visual assets are attached to this content item for B-roll."
    ),
    BrollErrorCode.ASSET_RIGHTS_BLOCKED: (
        "An asset has an unresolved rights risk and cannot be assigned as B-roll."
    ),
}

HTTP_STATUS = {
    BrollErrorCode.PROJECT_NOT_FOUND: HTTPStatus.NOT_FOUND,
    BrollErrorCode.PROJECT_CONFLICT: HTTPStatus.CONFLICT,
    BrollErrorCode.NO_SCENES: HTTPStatus.CONFLICT,
    BrollErrorCode.NO_ELIGIBLE_ASSETS: HTTPStatus.CONFLICT,
    BrollErrorCode.ASSET_RIGHTS_BLOCKED: HTTPStatus.CONFLICT,
}


class BrollError(DomainError):
    """A B-roll operation was refused; ``code`` is a ``BrollErrorCode``."""

    default_code = BrollErrorCode.BROLL.value
    default_user_message = USER_MESSAGES[BrollErrorCode.BROLL]

    def __init__(
        self,
        detail: str = "",
        *,
        code: BrollErrorCode = BrollErrorCode.BROLL,
    ) -> None:
        super().__init__(detail, code=code.value, user_message=USER_MESSAGES[code])
        if code in HTTP_STATUS:
            self.default_http_status = HTTP_STATUS[code]


@dataclass(frozen=True)
class BrollAssignment:
    """One B-roll placement to apply to a scene of a LongForm timeline."""

    scene_index: int
    asset_id: str
    role: AssetRole = AssetRole.BACKGROUND
    crop: CropMode = CropMode.COVER
    box: Box | None = None
    gain_db: float | None = None
    provenance_id: str | None = None

    def __post_init__(self) -> None:
        if isinstance(self.scene_index, bool) or not isinstance(
            self.scene_index, int
        ):
            raise TypeError("scene_index must be an int")
        if self.scene_index < 0:
            raise ValueError("scene_index must be >= 0")
        if not isinstance(self.asset_id, str) or not self.asset_id.strip():
            raise ValueError("asset_id must not be empty")
        if not isinstance(self.role, AssetRole) or self.role is AssetRole.MUSIC:
            raise ValueError(
                "role must be a scene asset role (background, b_roll, overlay or sfx)"
            )

    def to_scene_asset(self, provenance_id: str | None) -> SceneAsset:
        effective_gain = (
            (0.0 if self.gain_db is None else self.gain_db)
            if self.role is AssetRole.SFX
            else None
        )
        return SceneAsset(
            asset_id=self.asset_id,
            role=self.role,
            fit=self.crop,
            box=self.box,
            gain_db=effective_gain,
            provenance_id=provenance_id,
        )


class BrollManager:
    def __init__(
        self,
        database: Database,
        audit: AuditLog,
        flags: FeatureFlags | None = None,
        *,
        clock: Clock | None = None,
    ) -> None:
        self._database = database
        self._audit = audit
        self._flags = (
            flags if flags is not None else FeatureFlags(longform_enabled=True)
        )
        self._clock = clock

    def _require_enabled(self) -> None:
        if not self._flags.longform_enabled:
            raise LongFormDisabledError("LONGFORM_ENABLED is off")

    def assign(
        self,
        project_id: str,
        assignments: Sequence[BrollAssignment],
        *,
        expected_version: int,
        actor: Actor,
        replace_role: bool = False,
    ) -> LongFormProject:
        """Apply ``assignments`` to ``project_id`` and save the updated timeline."""
        self._require_enabled()
        if not assignments:
            raise BrollError(
                "assignments must not be empty",
                code=BrollErrorCode.EMPTY_ASSIGNMENTS,
            )
        for item_assign in assignments:
            if not isinstance(item_assign, BrollAssignment):
                raise TypeError("each assignment must be a BrollAssignment")

        with self._database.transaction() as connection:
            projects = LongFormProjectRepository(connection)
            stored = _load_project(projects, project_id, expected_version)
            if not stored.timeline.scenes:
                raise BrollError(
                    f"LongForm project {project_id} has no planned scenes",
                    code=BrollErrorCode.NO_SCENES,
                )
            item = _load_item(connection, stored.content_item_id)
            blocked_ids = _blocked_asset_ids(connection, item.id)

            scene_count = len(stored.timeline.scenes)
            scenes = list(stored.timeline.scenes)
            for assignment in assignments:
                if assignment.scene_index >= scene_count:
                    raise BrollError(
                        f"scene_index {assignment.scene_index} is out of range "
                        f"for {scene_count} scenes",
                        code=BrollErrorCode.SCENE_OUT_OF_RANGE,
                    )
                provenance_id = _validate_and_snapshot(
                    connection, item, assignment, blocked_ids
                )
                placed = assignment.to_scene_asset(provenance_id)
                scene = scenes[assignment.scene_index]
                scenes[assignment.scene_index] = _apply_to_scene(
                    scene, placed, replace_role=replace_role
                )

            project = _save_scenes(
                projects,
                stored,
                tuple(scenes),
                expected_version=expected_version,
                actor=actor,
                clock=self._clock,
            )

        self._record_audit(
            project,
            actor,
            assigned_count=len(assignments),
            mode="manual",
        )
        return project

    def auto_assign_backgrounds(
        self,
        project_id: str,
        *,
        expected_version: int,
        actor: Actor,
        crop: CropMode = CropMode.COVER,
        replace_existing: bool = False,
    ) -> LongFormProject:
        """Assign eligible attached visual assets round-robin as scene backgrounds."""
        self._require_enabled()
        with self._database.transaction() as connection:
            projects = LongFormProjectRepository(connection)
            stored = _load_project(projects, project_id, expected_version)
            if not stored.timeline.scenes:
                raise BrollError(
                    f"LongForm project {project_id} has no planned scenes",
                    code=BrollErrorCode.NO_SCENES,
                )
            item = _load_item(connection, stored.content_item_id)
            blocked_ids = _blocked_asset_ids(connection, item.id)
            candidates = _eligible_visual_assets(connection, item, blocked_ids)
            if not candidates:
                raise BrollError(
                    f"content item {item.id} has no eligible visual B-roll assets",
                    code=BrollErrorCode.NO_ELIGIBLE_ASSETS,
                )
            provenance_repo = ProvenanceRepository(connection)
            provenance_by_asset = {
                asset.id: (
                    latest.id
                    if (latest := provenance_repo.latest(asset.id)) is not None
                    else None
                )
                for asset in candidates
            }

            scenes: list[Scene] = []
            assigned_count = 0
            cursor = 0
            for scene in stored.timeline.scenes:
                has_bg = any(
                    a.role is AssetRole.BACKGROUND for a in scene.assets
                )
                if has_bg and not replace_existing:
                    scenes.append(scene)
                    continue
                chosen = candidates[cursor % len(candidates)]
                cursor += 1
                assigned_count += 1
                placed = SceneAsset(
                    asset_id=chosen.id,
                    role=AssetRole.BACKGROUND,
                    fit=crop,
                    provenance_id=provenance_by_asset[chosen.id],
                )
                scenes.append(_apply_to_scene(scene, placed, replace_role=True))

            project = _save_scenes(
                projects,
                stored,
                tuple(scenes),
                expected_version=expected_version,
                actor=actor,
                clock=self._clock,
            )

        self._record_audit(
            project,
            actor,
            assigned_count=assigned_count,
            mode="auto",
        )
        return project

    def _record_audit(
        self,
        project: LongFormProject,
        actor: Actor,
        *,
        assigned_count: int,
        mode: str,
    ) -> None:
        background_count = sum(
            sum(1 for a in scene.assets if a.role is AssetRole.BACKGROUND)
            for scene in project.timeline.scenes
        )
        overlay_count = sum(
            sum(1 for a in scene.assets if a.role is AssetRole.OVERLAY)
            for scene in project.timeline.scenes
        )
        sfx_count = sum(
            sum(1 for a in scene.assets if a.role is AssetRole.SFX)
            for scene in project.timeline.scenes
        )
        self._audit.record(
            BROLL_ASSIGNED,
            actor,
            EntityRef("longform_project", project.id),
            AuditResult.SUCCESS,
            {
                "content_item_id": project.content_item_id,
                "version": project.version,
                "status": project.status.value,
                "timeline_sha256": project.timeline.sha256,
                "mode": mode,
                "assigned_count": assigned_count,
                "background_count": background_count,
                "overlay_count": overlay_count,
                "sfx_count": sfx_count,
            },
        )


def _load_project(
    projects: LongFormProjectRepository,
    project_id: str,
    expected_version: int,
) -> LongFormProject:
    stored = projects.get(project_id)
    if stored is None:
        raise BrollError(
            f"LongForm project {project_id} does not exist",
            code=BrollErrorCode.PROJECT_NOT_FOUND,
        )
    if stored.version != expected_version:
        raise BrollError(
            f"LongForm project {project_id} is version {stored.version}, "
            f"not {expected_version}",
            code=BrollErrorCode.PROJECT_CONFLICT,
        )
    return stored


def _load_item(
    connection: sqlite3.Connection, content_item_id: str
) -> ContentItem:
    item = ContentItemRepository(connection).get(content_item_id)
    if item is None:
        raise LongFormProjectError(
            f"content item {content_item_id} does not exist",
            code=LongFormProjectErrorCode.ITEM_NOT_FOUND,
        )
    return item


def _blocked_asset_ids(
    connection: sqlite3.Connection, content_item_id: str
) -> set[str]:
    records = RightsRecordRepository(connection).list_by_content_item(
        content_item_id
    )
    blocked: set[str] = set()
    for record in records:
        if (
            record.resolution is RiskResolution.UNRESOLVED
            and record.risk_level in BLOCKING_RISK_LEVELS
        ):
            blocked.add(record.asset_ref)
    return blocked


def _validate_and_snapshot(
    connection: sqlite3.Connection,
    item: ContentItem,
    assignment: BrollAssignment,
    blocked_ids: set[str],
) -> str | None:
    asset = AssetRepository(connection).get(assignment.asset_id)
    if asset is None:
        raise LongFormProjectError(
            f"asset {assignment.asset_id} does not exist",
            code=LongFormProjectErrorCode.ASSET_NOT_FOUND,
        )
    if asset.channel_id != item.channel_id:
        raise LongFormProjectError(
            f"asset {asset.id} belongs to another channel",
            code=LongFormProjectErrorCode.ASSET_OTHER_CHANNEL,
        )
    if AssetUsageRepository(connection).get_by_pair(asset.id, item.id) is None:
        raise LongFormProjectError(
            f"asset {asset.id} is not attached to content item {item.id}",
            code=LongFormProjectErrorCode.ASSET_NOT_ATTACHED,
        )
    if asset.category is AssetCategory.UNKNOWN:
        raise LongFormProjectError(
            f"asset {asset.id} has the rights category unknown",
            code=LongFormProjectErrorCode.ASSET_RIGHTS_UNKNOWN,
        )
    if asset.kind not in ROLE_KINDS[assignment.role]:
        raise LongFormProjectError(
            f"asset {asset.id} is {asset.kind.value}, which a "
            f"{assignment.role.value} cannot use",
            code=LongFormProjectErrorCode.ASSET_KIND_MISMATCH,
        )
    if asset.id in blocked_ids:
        raise BrollError(
            f"asset {asset.id} has an unresolved blocking rights record",
            code=BrollErrorCode.ASSET_RIGHTS_BLOCKED,
        )
    latest = ProvenanceRepository(connection).latest(asset.id)
    current = latest.id if latest is not None else None
    if (
        assignment.provenance_id is not None
        and assignment.provenance_id != current
    ):
        raise LongFormProjectError(
            f"asset {asset.id} has provenance {current}, not "
            f"{assignment.provenance_id}",
            code=LongFormProjectErrorCode.ASSET_PROVENANCE_MISMATCH,
        )
    return current


def _eligible_visual_assets(
    connection: sqlite3.Connection,
    item: ContentItem,
    blocked_ids: set[str],
) -> list[Asset]:
    attached = AssetRepository(connection).list_by_content_item(item.id)
    visual_kinds = ROLE_KINDS[AssetRole.BACKGROUND]
    return [
        asset
        for asset in attached
        if asset.channel_id == item.channel_id
        and asset.kind in visual_kinds
        and asset.category is not AssetCategory.UNKNOWN
        and asset.id not in blocked_ids
    ]


def _apply_to_scene(
    scene: Scene, placed: SceneAsset, *, replace_role: bool
) -> Scene:
    if placed.role is AssetRole.BACKGROUND or replace_role:
        kept = [a for a in scene.assets if a.role is not placed.role]
    else:
        kept = list(scene.assets)
    kept.append(placed)
    return replace(scene, assets=tuple(kept))


def _save_scenes(
    projects: LongFormProjectRepository,
    stored: LongFormProject,
    scenes: tuple[Scene, ...],
    *,
    expected_version: int,
    actor: Actor,
    clock: Clock | None,
) -> LongFormProject:
    try:
        timeline = replace(stored.timeline, scenes=scenes)
    except ValueError as error:
        raise LongFormProjectError(
            str(error), code=LongFormProjectErrorCode.TIMELINE_INVALID
        ) from None
    check = check_longform_captions(stored.config, timeline)
    if not check.ok:
        raise LongFormProjectError(
            "; ".join(issue.message for issue in check.errors),
            code=LongFormProjectErrorCode.CAPTION_UNSAFE,
        )
    status = longform_readiness(stored.config, timeline)
    project = stored.with_timeline(timeline, status, actor=actor, clock=clock)
    try:
        projects.update(project, expected_version=expected_version)
    except ConcurrencyError as error:
        raise BrollError(
            str(error), code=BrollErrorCode.PROJECT_CONFLICT
        ) from None
    return project

