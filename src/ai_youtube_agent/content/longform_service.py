"""LongForm Project service (Prompt Pack v8, prompt #105), context C9 LongForm Production.

``LongFormProjectService`` makes and keeps the ``LongFormProject`` of a LongForm
content item (``content/longform_project.py``, table ``longform_projects`` of
migration 0028).

Rules:
- While ``LONGFORM_ENABLED`` is off (``FeatureFlags.longform_enabled`` is
  ``False``), every service call is refused with ``LongFormDisabledError``
  before reading or mutating the database.
- ``create`` requires a ``ContentType.LONGFORM`` content item and binds the
  project to the latest script version, the latest audio artifact version (which
  must voice that latest script and not exceed ``config.max_seconds``), and a
  ``LongFormConfig`` copied from ``strategy.format.longform``.
- ``rebind`` reads the latest inputs again and resets the project to a ``draft``
  with an empty ``LongFormTimeline``.
- ``save_timeline`` validates sections against the bound script, chapter
  settings against ``config.chapters``, captions via ``check_longform_captions``
  (J-109), and all referenced scene/music/font assets via D5 checks with
  provenance snapshotting, recomputing ``LongFormProjectStatus`` via
  ``longform_readiness``.
- ``plan`` builds one scene per script section (and chapter markers when
  ``config.chapters`` is enabled) via ``plan_longform_timeline`` (J-106/J-107).
- ``generate_chapters`` computes and saves chapter markers for the stored scenes
  via ``generate_chapters`` (J-107) and records ``longform.chapters_generated``.
- ``caption`` plans captions for the stored scenes via ``plan_longform_captions``
  (J-109) and saves the updated timeline.
- Audits ``longform.project_created``, ``longform.project_rebound``,
  ``longform.timeline_saved`` and ``longform.chapters_generated`` after commit
  with scalar IDs and counts only.
"""

import sqlite3
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum
from http import HTTPStatus

from ai_youtube_agent.content.asset import Asset, AssetCategory, AssetKind
from ai_youtube_agent.content.longform_captions import (
     check_longform_captions,
    covers_every_longform_scene,
    plan_longform_captions,
)
from ai_youtube_agent.content.longform_chapters import (
    generate_chapters as build_chapters,
)
from ai_youtube_agent.content.longform_config import LongFormConfig
from ai_youtube_agent.content.longform_planner import plan_longform_timeline
from ai_youtube_agent.content.longform_project import (
    LongFormProject,
    LongFormProjectStatus,
)
from ai_youtube_agent.content.longform_script_generator import LongFormDisabledError
from ai_youtube_agent.content.longform_timeline import LongFormTimeline
from ai_youtube_agent.content.script import Script
from ai_youtube_agent.content.shorts_captions import IssueKind
from ai_youtube_agent.content.shorts_timeline import (
    ROLE_KINDS,
    AssetPlacement,
    AssetRole,
    SceneAsset,
)
from ai_youtube_agent.content.tts_job import chunk_durations
from ai_youtube_agent.core.artifact import ArtifactKind
from ai_youtube_agent.core.audit import (
    Actor,
    AuditLog,
    AuditResult,
    EntityRef,
    MetadataValue,
)
from ai_youtube_agent.core.content_item import ContentItem, ContentType
from ai_youtube_agent.core.db.database import ConcurrencyError, Database
from ai_youtube_agent.core.db.repositories.asset import (
    AssetRepository,
    AssetUsageRepository,
)
from ai_youtube_agent.core.db.repositories.channel import StrategyProfileRepository
from ai_youtube_agent.core.db.repositories.content import (
    ArtifactRepository,
    AudioMetadataRepository,
    ContentItemRepository,
    ScriptRepository,
)
from ai_youtube_agent.core.db.repositories.jobs import AIJobRepository
from ai_youtube_agent.core.db.repositories.longform import LongFormProjectRepository
from ai_youtube_agent.core.db.repositories.provenance import ProvenanceRepository
from ai_youtube_agent.core.errors import DomainError
from ai_youtube_agent.core.flags import FeatureFlags
from ai_youtube_agent.pipeline.idempotency import audio_key

Clock = Callable[[], datetime]

CREATED = "longform.project_created"
REBOUND = "longform.project_rebound"
TIMELINE_SAVED = "longform.timeline_saved"
CHAPTERS_GENERATED = "longform.chapters_generated"


class LongFormProjectErrorCode(StrEnum):
    PROJECT = "domain.longform_project"
    ITEM_NOT_FOUND = "domain.longform_item_not_found"
    NOT_LONGFORM = "domain.longform_not_longform"
    STRATEGY_MISSING = "domain.longform_strategy_missing"
    SCRIPT_MISSING = "domain.longform_script_missing"
    AUDIO_MISSING = "domain.longform_audio_missing"
    STALE_INPUT = "domain.longform_stale_input"
    AUDIO_TOO_LONG = "domain.longform_audio_too_long"
    EXISTS = "domain.longform_project_exists"
    NOT_FOUND = "domain.longform_project_not_found"
    CONFLICT = "domain.longform_project_conflict"
    TIMELINE_INVALID = "domain.longform_timeline_invalid"
    CHAPTER_INVALID = "domain.longform_chapter_invalid"
    ASSET_NOT_FOUND = "domain.longform_asset_not_found"
    ASSET_OTHER_CHANNEL = "domain.longform_asset_other_channel"
    ASSET_NOT_ATTACHED = "domain.longform_asset_not_attached"
    ASSET_RIGHTS_UNKNOWN = "domain.longform_asset_rights_unknown"
    ASSET_KIND_MISMATCH = "domain.longform_asset_kind_mismatch"
    ASSET_PROVENANCE_MISMATCH = "domain.longform_asset_provenance_mismatch"
    CAPTION_INVALID = "domain.longform_caption_invalid"
    CAPTION_UNSAFE = "domain.longform_caption_unsafe"


USER_MESSAGES = {
    LongFormProjectErrorCode.PROJECT: "The LongForm project could not be saved.",
    LongFormProjectErrorCode.ITEM_NOT_FOUND: "This content item does not exist.",
    LongFormProjectErrorCode.NOT_LONGFORM: (
        "Only a LongForm content item has a LongForm project."
    ),
    LongFormProjectErrorCode.STRATEGY_MISSING: (
        "The channel has no LongForm format in its strategy yet."
    ),
    LongFormProjectErrorCode.SCRIPT_MISSING: "This content item has no script yet.",
    LongFormProjectErrorCode.AUDIO_MISSING: "This content item has no audio yet.",
    LongFormProjectErrorCode.STALE_INPUT: (
        "The audio was made from an older script. Voice the latest script first."
    ),
    LongFormProjectErrorCode.AUDIO_TOO_LONG: (
        "The audio is longer than a LongForm video may be."
    ),
    LongFormProjectErrorCode.EXISTS: (
        "This content item already has a LongForm project."
    ),
    LongFormProjectErrorCode.NOT_FOUND: "This LongForm project does not exist.",
    LongFormProjectErrorCode.CONFLICT: (
        "The LongForm project changed in the meantime. Reload it and try again."
    ),
    LongFormProjectErrorCode.TIMELINE_INVALID: "This timeline cannot be saved.",
    LongFormProjectErrorCode.CHAPTER_INVALID: (
        "The chapters of this timeline break a rule and cannot be saved."
    ),
    LongFormProjectErrorCode.ASSET_NOT_FOUND: (
        "A file used in this timeline does not exist."
    ),
    LongFormProjectErrorCode.ASSET_OTHER_CHANNEL: (
        "A file used in this timeline belongs to another channel."
    ),
    LongFormProjectErrorCode.ASSET_NOT_ATTACHED: (
        "A file used in this timeline is not attached to this content item yet."
    ),
    LongFormProjectErrorCode.ASSET_RIGHTS_UNKNOWN: (
        "A file used in this timeline has no declared rights category yet."
    ),
    LongFormProjectErrorCode.ASSET_KIND_MISMATCH: (
        "A file used in this timeline is of the wrong type for its role."
    ),
    LongFormProjectErrorCode.ASSET_PROVENANCE_MISMATCH: (
        "The source record of a file used in this timeline changed. Reload it "
        "and try again."
    ),
    LongFormProjectErrorCode.CAPTION_INVALID: (
        "The captions of this timeline break a rule and cannot be saved."
    ),
    LongFormProjectErrorCode.CAPTION_UNSAFE: (
        "A caption or overlay of this timeline leaves the safe area of the screen."
    ),
}

HTTP_STATUS = {
    LongFormProjectErrorCode.ITEM_NOT_FOUND: HTTPStatus.NOT_FOUND,
    LongFormProjectErrorCode.NOT_FOUND: HTTPStatus.NOT_FOUND,
    LongFormProjectErrorCode.EXISTS: HTTPStatus.CONFLICT,
    LongFormProjectErrorCode.CONFLICT: HTTPStatus.CONFLICT,
    LongFormProjectErrorCode.STALE_INPUT: HTTPStatus.CONFLICT,
    LongFormProjectErrorCode.ASSET_PROVENANCE_MISMATCH: HTTPStatus.CONFLICT,
}


class LongFormProjectError(DomainError):
    """A LongForm project was refused; ``code`` is a ``LongFormProjectErrorCode``."""

    default_code = LongFormProjectErrorCode.PROJECT.value
    default_user_message = USER_MESSAGES[LongFormProjectErrorCode.PROJECT]

    def __init__(
        self,
        detail: str = "",
        *,
        code: LongFormProjectErrorCode = LongFormProjectErrorCode.PROJECT,
    ) -> None:
        super().__init__(detail, code=code.value, user_message=USER_MESSAGES[code])
        if code in HTTP_STATUS:
            self.default_http_status = HTTP_STATUS[code]


class LongFormProjectNotFoundError(LongFormProjectError):
    default_code = LongFormProjectErrorCode.NOT_FOUND.value
    default_user_message = USER_MESSAGES[LongFormProjectErrorCode.NOT_FOUND]
    default_http_status = HTTPStatus.NOT_FOUND

    def __init__(self, detail: str = "") -> None:
        super().__init__(detail, code=LongFormProjectErrorCode.NOT_FOUND)


@dataclass(frozen=True)
class _Inputs:
    """The latest inputs of a LongForm item and the configuration they give."""

    script_id: str
    script_version: int
    audio_artifact_id: str
    audio_version: int
    audio_sha256: str
    audio_duration_ms: int
    strategy_version: int
    config: LongFormConfig

    def bind(self) -> dict[str, object]:
        return {
            "script_id": self.script_id,
            "script_version": self.script_version,
            "audio_artifact_id": self.audio_artifact_id,
            "audio_version": self.audio_version,
            "audio_sha256": self.audio_sha256,
            "audio_duration_ms": self.audio_duration_ms,
            "strategy_version": self.strategy_version,
            "config": self.config,
        }


def longform_readiness(
    config: LongFormConfig, timeline: LongFormTimeline
) -> LongFormProjectStatus:
    """The status a LongForm timeline earns.

    - Without scenes: ``draft``.
    - With scenes: ``ready`` when every scene has a ``background`` asset,
      chapters match ``config.chapters`` (present when enabled, absent when
      disabled), and captions pass ``check_longform_captions`` and cover every
      scene when ``config.captions`` is enabled; otherwise ``planned``.
    """
    if not timeline.scenes:
        return LongFormProjectStatus.DRAFT
    backgrounds = all(
        sum(asset.role is AssetRole.BACKGROUND for asset in scene.assets) == 1
        for scene in timeline.scenes
    )
    chapters = (
        bool(timeline.chapters) if config.chapters else not timeline.chapters
    )
    captions = check_longform_captions(config, timeline).ok and (
        not config.captions or covers_every_longform_scene(timeline)
    )
    if backgrounds and chapters and captions:
        return LongFormProjectStatus.READY
    return LongFormProjectStatus.PLANNED


class LongFormProjectService:
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

    def create(self, content_item_id: str, *, actor: Actor) -> LongFormProject:
        self._require_enabled()
        try:
            with self._database.transaction() as connection:
                item = _longform_item(connection, content_item_id)
                projects = LongFormProjectRepository(connection)
                if projects.get_by_content_item(item.id) is not None:
                    raise _exists(item.id)
                inputs = _latest_inputs(connection, item)
                project = LongFormProject.create(
                    item.id, **inputs.bind(), actor=actor, clock=self._clock
                )
                projects.add(project)
        except sqlite3.IntegrityError:
            with self._database.transaction() as connection:
                stored = LongFormProjectRepository(connection).get_by_content_item(
                    content_item_id
                )
            if stored is None:
                raise
            raise _exists(content_item_id) from None
        self._audit.record(
            CREATED,
            actor,
            EntityRef("longform_project", project.id),
            AuditResult.SUCCESS,
            _event_metadata(project),
        )
        return project

    def rebind(
        self, project_id: str, *, expected_version: int, actor: Actor
    ) -> LongFormProject:
        self._require_enabled()
        with self._database.transaction() as connection:
            projects = LongFormProjectRepository(connection)
            stored = _stored(projects, project_id)
            _check_version(stored, expected_version)
            item = _longform_item(connection, stored.content_item_id)
            inputs = _latest_inputs(connection, item)
            project = stored.rebind(
                **inputs.bind(), actor=actor, clock=self._clock
            )
            _update(projects, project, expected_version)
        self._audit.record(
            REBOUND,
            actor,
            EntityRef("longform_project", project.id),
            AuditResult.SUCCESS,
            {**_event_metadata(project), "previous_version": stored.version},
        )
        return project

    def save_timeline(
        self,
        project_id: str,
        timeline: LongFormTimeline,
        *,
        expected_version: int,
        actor: Actor,
    ) -> LongFormProject:
        self._require_enabled()
        with self._database.transaction() as connection:
            projects = LongFormProjectRepository(connection)
            stored = _stored(projects, project_id)
            _check_version(stored, expected_version)
            project = self._with_timeline(
                connection, stored, timeline, actor, projects, expected_version
            )
        self._record_timeline(project, actor)
        return project

    def plan(
        self, project_id: str, *, expected_version: int, actor: Actor
    ) -> LongFormProject:
        """Plan one scene per script section (and chapters when enabled) and save."""
        self._require_enabled()
        with self._database.transaction() as connection:
            projects = LongFormProjectRepository(connection)
            stored = _stored(projects, project_id)
            _check_version(stored, expected_version)
            script = _bound_script(connection, stored)
            try:
                timeline = plan_longform_timeline(
                    stored, script, _chunk_durations(connection, stored)
                )
            except ValueError as error:
                raise LongFormProjectError(
                    str(error), code=LongFormProjectErrorCode.TIMELINE_INVALID
                ) from None
            project = self._with_timeline(
                connection, stored, timeline, actor, projects, expected_version
            )
        self._record_timeline(project, actor)
        return project

    def generate_chapters(
        self, project_id: str, *, expected_version: int, actor: Actor
    ) -> LongFormProject:
        """Generate YouTube chapter markers for the stored scenes and save them."""
        self._require_enabled()
        with self._database.transaction() as connection:
            projects = LongFormProjectRepository(connection)
            stored = _stored(projects, project_id)
            _check_version(stored, expected_version)
            script = _bound_script(connection, stored)
            try:
                chapters = build_chapters(stored, script, stored.timeline)
                timeline = replace(stored.timeline, chapters=chapters)
            except ValueError as error:
                raise LongFormProjectError(
                    str(error), code=LongFormProjectErrorCode.CHAPTER_INVALID
                ) from None
            project = self._with_timeline(
                connection, stored, timeline, actor, projects, expected_version
            )
        self._audit.record(
            CHAPTERS_GENERATED,
            actor,
            EntityRef("longform_project", project.id),
            AuditResult.SUCCESS,
            {
                **_event_metadata(project),
                "timeline_sha256": project.timeline.sha256,
                "chapter_count": len(project.timeline.chapters),
            },
        )
        return project

    def caption(
        self, project_id: str, *, expected_version: int, actor: Actor
    ) -> LongFormProject:
        """Plan the captions of the stored scenes and save them."""
        self._require_enabled()
        with self._database.transaction() as connection:
            projects = LongFormProjectRepository(connection)
            stored = _stored(projects, project_id)
            _check_version(stored, expected_version)
            script = _bound_script(connection, stored)
            try:
                captions = plan_longform_captions(stored, script, stored.timeline)
                timeline = replace(stored.timeline, captions=captions)
            except ValueError as error:
                raise LongFormProjectError(
                    str(error), code=LongFormProjectErrorCode.CAPTION_INVALID
                ) from None
            project = self._with_timeline(
                connection, stored, timeline, actor, projects, expected_version
            )
        self._record_timeline(project, actor)
        return project

    def _with_timeline(
        self,
        connection: sqlite3.Connection,
        stored: LongFormProject,
        timeline: LongFormTimeline,
        actor: Actor,
        projects: LongFormProjectRepository,
        expected_version: int,
    ) -> LongFormProject:
        script = _bound_script(connection, stored)
        status = _status_of(stored, timeline, script)
        _check_sections(stored, timeline, script)
        timeline = _check_assets(connection, stored, timeline)
        project = stored.with_timeline(
            timeline, status, actor=actor, clock=self._clock
        )
        _update(projects, project, expected_version)
        return project

    def _record_timeline(self, project: LongFormProject, actor: Actor) -> None:
        self._audit.record(
            TIMELINE_SAVED,
            actor,
            EntityRef("longform_project", project.id),
            AuditResult.SUCCESS,
            {
                **_event_metadata(project),
                "timeline_sha256": project.timeline.sha256,
                "scene_count": len(project.timeline.scenes),
                "chapter_count": len(project.timeline.chapters),
                "caption_count": len(project.timeline.captions),
                "caption_warning_count": len(
                    check_longform_captions(
                        project.config, project.timeline
                    ).warnings
                ),
                "music_count": len(project.timeline.music),
                "timing_source": project.timeline.timing_source.value,
            },
        )

    def get(self, project_id: str) -> LongFormProject:
        self._require_enabled()
        with self._database.transaction() as connection:
            return _stored(LongFormProjectRepository(connection), project_id)

    def get_by_content_item(
        self, content_item_id: str
    ) -> LongFormProject | None:
        self._require_enabled()
        with self._database.transaction() as connection:
            if ContentItemRepository(connection).get(content_item_id) is None:
                raise LongFormProjectError(
                    f"content item {content_item_id} does not exist",
                    code=LongFormProjectErrorCode.ITEM_NOT_FOUND,
                )
            return LongFormProjectRepository(connection).get_by_content_item(
                content_item_id
            )

    def list_by_channel(self, channel_id: str) -> list[LongFormProject]:
        self._require_enabled()
        with self._database.transaction() as connection:
            return LongFormProjectRepository(connection).list_by_channel(channel_id)

    def is_current(self, project_id: str) -> bool:
        """True when the bound script and audio are still the item's latest."""
        self._require_enabled()
        with self._database.transaction() as connection:
            project = _stored(LongFormProjectRepository(connection), project_id)
            return longform_project_is_current(connection, project)


def longform_project_is_current(
    connection: sqlite3.Connection, project: LongFormProject
) -> bool:
    """True when the bound script and audio are still the item's latest."""
    scripts = ScriptRepository(connection).list_by_content_item(
        project.content_item_id
    )
    audio = ArtifactRepository(connection).latest(
        project.content_item_id, ArtifactKind.AUDIO
    )
    if not scripts or audio is None:
        return False
    script = scripts[-1]
    return (
        script.id == project.script_id
        and script.version == project.script_version
        and audio.id == project.audio_artifact_id
        and audio.version == project.audio_version
        and audio.sha256 == project.audio_sha256
    )


def check_longform_audio_assets(
    connection: sqlite3.Connection,
    item: ContentItem,
    placements: Sequence[SceneAsset | AssetPlacement],
) -> dict[str, Asset]:
    """D5 checks for audio assets referenced in LongForm scene/music placements."""
    checker = _AssetChecker(connection, item)
    return {
        placed.asset_id: checker.check(
            placed.asset_id, ROLE_KINDS[placed.role], placed.role.value
        )
        for placed in placements
    }


def check_longform_timeline_assets(
    connection: sqlite3.Connection,
    item: ContentItem,
    placements: Sequence[SceneAsset | AssetPlacement],
    font_ids: Sequence[str] = (),
) -> dict[str, Asset]:
    """D5 checks for LongForm timeline placements and caption fonts."""
    checker = _AssetChecker(connection, item)
    assets = {
        placed.asset_id: checker.check(
            placed.asset_id, ROLE_KINDS[placed.role], placed.role.value
        )
        for placed in placements
    }
    for font_id in sorted(set(font_ids)):
        assets[font_id] = checker.check(
            font_id, frozenset({AssetKind.FONT}), "caption font"
        )
    return assets


def _exists(content_item_id: str) -> LongFormProjectError:
    return LongFormProjectError(
        f"content item {content_item_id} already has a LongForm project",
        code=LongFormProjectErrorCode.EXISTS,
    )


def _stored(
    projects: LongFormProjectRepository, project_id: str
) -> LongFormProject:
    project = projects.get(project_id)
    if project is None:
        raise LongFormProjectError(
            f"LongForm project {project_id} does not exist",
            code=LongFormProjectErrorCode.NOT_FOUND,
        )
    return project


def _check_version(project: LongFormProject, expected_version: int) -> None:
    if project.version != expected_version:
        raise LongFormProjectError(
            f"LongForm project {project.id} is version {project.version}, "
            f"not {expected_version}",
            code=LongFormProjectErrorCode.CONFLICT,
        )


def _update(
    projects: LongFormProjectRepository,
    project: LongFormProject,
    expected_version: int,
) -> None:
    try:
        projects.update(project, expected_version=expected_version)
    except ConcurrencyError as error:
        raise LongFormProjectError(
            str(error), code=LongFormProjectErrorCode.CONFLICT
        ) from None


def _status_of(
    project: LongFormProject, timeline: LongFormTimeline, script: Script
) -> LongFormProjectStatus:
    if not isinstance(timeline, LongFormTimeline):
        raise LongFormProjectError(
            "the timeline is not a LongFormTimeline",
            code=LongFormProjectErrorCode.TIMELINE_INVALID,
        )
    if timeline.duration_ms != project.audio_duration_ms:
        raise LongFormProjectError(
            f"the timeline is {timeline.duration_ms} ms but the audio is "
            f"{project.audio_duration_ms} ms",
            code=LongFormProjectErrorCode.TIMELINE_INVALID,
        )
    _check_chapters(project, timeline, script)
    _refuse_caption_errors(project, timeline)
    try:
        return longform_readiness(project.config, timeline)
    except ValueError as error:
        raise LongFormProjectError(
            str(error), code=LongFormProjectErrorCode.TIMELINE_INVALID
        ) from None


def _check_chapters(
    project: LongFormProject, timeline: LongFormTimeline, script: Script
) -> None:
    if not project.config.chapters and timeline.chapters:
        raise LongFormProjectError(
            "chapters are switched off in the project configuration",
            code=LongFormProjectErrorCode.CHAPTER_INVALID,
        )
    sections = len(script.sections)
    for marker in timeline.chapters:
        if marker.section_index >= sections:
            raise LongFormProjectError(
                f"chapter marker {marker.index} references section "
                f"{marker.section_index}, but script version "
                f"{project.script_version} has {sections} sections",
                code=LongFormProjectErrorCode.CHAPTER_INVALID,
            )


def _refuse_caption_errors(
    project: LongFormProject, timeline: LongFormTimeline
) -> None:
    check = check_longform_captions(project.config, timeline)
    for kind, code in (
        (IssueKind.INVALID, LongFormProjectErrorCode.CAPTION_INVALID),
        (IssueKind.UNSAFE, LongFormProjectErrorCode.CAPTION_UNSAFE),
    ):
        issues = check.errors_of(kind)
        if issues:
            raise LongFormProjectError(
                "; ".join(issue.message for issue in issues), code=code
            )


def _bound_script(
    connection: sqlite3.Connection, project: LongFormProject
) -> Script:
    script = ScriptRepository(connection).get(project.script_id)
    if script is None:
        raise LongFormProjectError(
            f"script {project.script_id} of the project does not exist",
            code=LongFormProjectErrorCode.SCRIPT_MISSING,
        )
    return script


def _check_sections(
    project: LongFormProject, timeline: LongFormTimeline, script: Script
) -> None:
    if not timeline.scenes:
        return
    sections = len(script.sections)
    used = {scene.section_index for scene in timeline.scenes}
    outside = sorted(index for index in used if index >= sections)
    if outside:
        raise LongFormProjectError(
            f"scenes use section {outside[0]}, but script version "
            f"{project.script_version} has {sections} sections",
            code=LongFormProjectErrorCode.TIMELINE_INVALID,
        )
    missing = sorted(set(range(sections)) - used)
    if missing:
        raise LongFormProjectError(
            f"no scene shows section {missing[0]} of script version "
            f"{project.script_version}",
            code=LongFormProjectErrorCode.TIMELINE_INVALID,
        )


def _check_assets(
    connection: sqlite3.Connection,
    project: LongFormProject,
    timeline: LongFormTimeline,
) -> LongFormTimeline:
    fonts = {caption.style.font_asset_id for caption in timeline.captions} - {None}
    if (
        not any(scene.assets for scene in timeline.scenes)
        and not timeline.music
        and not fonts
    ):
        return timeline
    item = ContentItemRepository(connection).get(project.content_item_id)
    if item is None:
        raise LongFormProjectError(
            f"content item {project.content_item_id} does not exist",
            code=LongFormProjectErrorCode.ITEM_NOT_FOUND,
        )
    checker = _AssetChecker(connection, item)
    scenes = tuple(
        replace(scene, assets=tuple(checker.snapshot(a) for a in scene.assets))
        for scene in timeline.scenes
    )
    music = tuple(checker.snapshot(placement) for placement in timeline.music)
    for font_id in sorted(fonts):
        checker.check(font_id, frozenset({AssetKind.FONT}), "caption font")
    return replace(timeline, scenes=scenes, music=music)


class _AssetChecker:
    def __init__(self, connection: sqlite3.Connection, item: ContentItem) -> None:
        self._item = item
        self._assets = AssetRepository(connection)
        self._usages = AssetUsageRepository(connection)
        self._provenance = ProvenanceRepository(connection)

    def check(self, asset_id: str, kinds: frozenset[AssetKind], use: str) -> Asset:
        asset = self._assets.get(asset_id)
        if asset is None:
            raise _asset_error(
                f"asset {asset_id} does not exist",
                LongFormProjectErrorCode.ASSET_NOT_FOUND,
            )
        if asset.channel_id != self._item.channel_id:
            raise _asset_error(
                f"asset {asset.id} belongs to another channel",
                LongFormProjectErrorCode.ASSET_OTHER_CHANNEL,
            )
        if self._usages.get_by_pair(asset.id, self._item.id) is None:
            raise _asset_error(
                f"asset {asset.id} is not attached to content item {self._item.id}",
                LongFormProjectErrorCode.ASSET_NOT_ATTACHED,
            )
        if asset.category is AssetCategory.UNKNOWN:
            raise _asset_error(
                f"asset {asset.id} has the rights category unknown",
                LongFormProjectErrorCode.ASSET_RIGHTS_UNKNOWN,
            )
        if asset.kind not in kinds:
            raise _asset_error(
                f"asset {asset.id} is {asset.kind.value}, which a {use} cannot use",
                LongFormProjectErrorCode.ASSET_KIND_MISMATCH,
            )
        return asset

    def snapshot(
        self, placed: SceneAsset | AssetPlacement
    ) -> SceneAsset | AssetPlacement:
        asset = self.check(placed.asset_id, ROLE_KINDS[placed.role], placed.role.value)
        latest = self._provenance.latest(asset.id)
        current = latest.id if latest is not None else None
        if placed.provenance_id is not None and placed.provenance_id != current:
            raise _asset_error(
                f"asset {asset.id} has provenance {current}, not "
                f"{placed.provenance_id}",
                LongFormProjectErrorCode.ASSET_PROVENANCE_MISMATCH,
            )
        return replace(placed, provenance_id=current)


def _asset_error(
    detail: str, code: LongFormProjectErrorCode
) -> LongFormProjectError:
    return LongFormProjectError(detail, code=code)


def _chunk_durations(
    connection: sqlite3.Connection, project: LongFormProject
) -> tuple[int, ...] | None:
    audio = AudioMetadataRepository(connection).get_by_artifact(
        project.audio_artifact_id
    )
    if audio is None:
        return None
    job = AIJobRepository(connection).get_by_idempotency_key(
        audio_key(audio.script_id, audio.voice_profile_id, audio.voice_profile_version)
    )
    return chunk_durations(job) if job is not None else None


def _longform_item(
    connection: sqlite3.Connection, content_item_id: str
) -> ContentItem:
    item = ContentItemRepository(connection).get(content_item_id)
    if item is None:
        raise LongFormProjectError(
            f"content item {content_item_id} does not exist",
            code=LongFormProjectErrorCode.ITEM_NOT_FOUND,
        )
    if item.content_type is not ContentType.LONGFORM:
        raise LongFormProjectError(
            f"content item {item.id} is {item.content_type.value}, not longform",
            code=LongFormProjectErrorCode.NOT_LONGFORM,
        )
    return item


def _latest_inputs(connection: sqlite3.Connection, item: ContentItem) -> _Inputs:
    strategy = StrategyProfileRepository(connection).get_by_channel(item.channel_id)
    if strategy is None or strategy.format is None:
        raise LongFormProjectError(
            f"channel {item.channel_id} has no format in its strategy",
            code=LongFormProjectErrorCode.STRATEGY_MISSING,
        )
    config = LongFormConfig.from_format(strategy.format.longform)
    scripts = ScriptRepository(connection).list_by_content_item(item.id)
    if not scripts:
        raise LongFormProjectError(
            f"content item {item.id} has no script",
            code=LongFormProjectErrorCode.SCRIPT_MISSING,
        )
    script = scripts[-1]
    artifact = ArtifactRepository(connection).latest(item.id, ArtifactKind.AUDIO)
    audio = (
        AudioMetadataRepository(connection).get_by_artifact(artifact.id)
        if artifact is not None
        else None
    )
    if artifact is None or audio is None:
        raise LongFormProjectError(
            f"content item {item.id} has no audio with its details",
            code=LongFormProjectErrorCode.AUDIO_MISSING,
        )
    if audio.script_id != script.id:
        raise LongFormProjectError(
            f"audio version {artifact.version} of content item {item.id} voiced "
            f"script {audio.script_id}, not the latest script {script.id} "
            f"(version {script.version})",
            code=LongFormProjectErrorCode.STALE_INPUT,
        )
    if audio.duration_ms > config.max_seconds * 1000:
        raise LongFormProjectError(
            f"the audio is {audio.duration_ms} ms, longer than the "
            f"{config.max_seconds} seconds a LongForm video may take",
            code=LongFormProjectErrorCode.AUDIO_TOO_LONG,
        )
    return _Inputs(
        script_id=script.id,
        script_version=script.version,
        audio_artifact_id=artifact.id,
        audio_version=artifact.version,
        audio_sha256=artifact.sha256,
        audio_duration_ms=audio.duration_ms,
        strategy_version=strategy.version,
        config=config,
    )


def _event_metadata(project: LongFormProject) -> Mapping[str, MetadataValue]:
    return {
        "content_item_id": project.content_item_id,
        "script_id": project.script_id,
        "script_version": project.script_version,
        "audio_artifact_id": project.audio_artifact_id,
        "audio_version": project.audio_version,
        "audio_duration_ms": project.audio_duration_ms,
        "strategy_version": project.strategy_version,
        "status": project.status.value,
        "version": project.version,
    }

