"""Shorts Project service (Prompt Pack v8, prompt #094), context C8 Shorts Production.

``ShortsProjectService`` makes and keeps the ``ShortsProject`` of a Shorts
content item (``content/shorts_project.py``, table ``shorts_projects`` of
migration 0027). The rules were settled with the user's defaults on 2026-10-17:

- ``create`` needs a Shorts content item and binds the project to the exact
  inputs it is produced from: the latest script version and the latest audio
  artifact version of the item (with the duration of its ``AudioMetadata``),
  and the channel strategy's Shorts format copied into a ``ShortsConfig``
  (``strategy_version`` is the version of the strategy that was read). Both
  inputs must be the latest ones and belong together: when the latest audio
  voiced an older script than the latest script, the input is stale
  (``domain.shorts_stale_input``). A content item has one project; a second
  ``create`` is ``domain.shorts_project_exists``. Audio longer than the
  format's ``max_seconds`` is refused (``domain.shorts_audio_too_long``); audio
  shorter than ``min_seconds`` is accepted and reported by
  ``ShortsProject.audio_within_target``.
- ``rebind`` reads the latest inputs again and makes the project from them:
  a draft with an empty timeline, because the old timeline described other
  audio. ``save_timeline`` replaces the timeline of the same audio. Both take
  the ``version`` the caller last read (``expected_version``) and a change on
  top of a newer version is ``domain.shorts_project_conflict``.
- ``save_timeline`` recomputes the status with ``readiness`` and never trusts
  the caller: no scenes is a ``draft``, valid scenes (I-095) are ``planned`` and
  a background per scene (I-096) plus, when the format has captions, a valid
  caption list that covers every scene (I-097) is ``ready``.
  ``readiness`` is the only place that decides the status. Scenes must also
  match the bound script: every section index of that script version appears
  and none is out of range.
- Assets (I-096, decision D5). Every asset a timeline references (scene assets
  and music) must exist (``asset_not_found``), belong to the channel of the
  item (``asset_other_channel``), be attached to the item through an
  ``AssetUsage`` (``asset_not_attached``), have a category other than
  ``unknown`` (``asset_rights_unknown``) and be of a kind its role accepts
  (``asset_kind_mismatch``, see ``ROLE_KINDS``). The check only reads; the
  rights gate, the rights QC and the publish gate stay authoritative and are
  neither replaced nor bypassed. The saved timeline snapshots the current
  provenance id of each asset in ``provenance_id`` (null when the asset has no
  provenance record). A caller who sends a ``provenance_id`` that is not the
  current one is refused (``asset_provenance_mismatch``) instead of silently
  overwritten: leave it null to take the current one.
- Captions (I-097). ``save_timeline`` runs ``check_captions`` (see
  ``content/shorts_captions.py``): a caption that breaks a rule (captions on a
  format that has them off, display time, wrapping) is
  ``domain.shorts_caption_invalid`` and one that leaves the safe area, or an
  overlay box that does, is ``domain.shorts_caption_unsafe``; speed (more than
  20 characters per second) is only a warning, counted in the audit event. A
  ``font_asset_id`` must be an asset of kind ``font`` that passes the same
  checks as the other assets (except the provenance snapshot). ``caption``
  plans the captions of the stored scenes with ``plan_captions`` and saves
  them, keeping the scenes and their assets.
- ``plan`` builds the timeline with ``plan_timeline`` (one scene per script
  section, timed from the chunk durations of the succeeded TTS job of the bound
  audio when they fit, else estimated) and saves it like ``save_timeline``.
- ``get``, ``get_by_content_item``, ``list_by_channel`` and ``is_current`` only
  read. ``is_current`` tells whether the bound script and audio are still the
  latest ones of the item. A project never changes the content item's status
  and nothing here approves, renders or publishes anything.
- After each commit one event is audited with ids and counts only, never any
  text: ``shorts.project_created``, ``shorts.project_rebound`` and
  ``shorts.timeline_saved`` (with ``caption_count`` and
  ``caption_warning_count``).
"""

import sqlite3
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum
from http import HTTPStatus

from ai_youtube_agent.content.asset import Asset, AssetCategory, AssetKind
from ai_youtube_agent.content.script import Script
from ai_youtube_agent.content.shorts_captions import (
    IssueKind,
    check_captions,
    covers_every_scene,
    plan_captions,
)
from ai_youtube_agent.content.shorts_config import ShortsConfig
from ai_youtube_agent.content.shorts_planner import plan_timeline
from ai_youtube_agent.content.shorts_project import ShortsProject, ShortsProjectStatus
from ai_youtube_agent.content.shorts_timeline import (
    ROLE_KINDS,
    AssetPlacement,
    AssetRole,
    SceneAsset,
    Timeline,
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
from ai_youtube_agent.core.db.repositories.provenance import ProvenanceRepository
from ai_youtube_agent.core.db.repositories.shorts import ShortsProjectRepository
from ai_youtube_agent.core.errors import DomainError
from ai_youtube_agent.pipeline.idempotency import audio_key

Clock = Callable[[], datetime]

CREATED = "shorts.project_created"
REBOUND = "shorts.project_rebound"
TIMELINE_SAVED = "shorts.timeline_saved"


class ShortsProjectErrorCode(StrEnum):
    PROJECT = "domain.shorts_project"
    ITEM_NOT_FOUND = "domain.shorts_item_not_found"
    NOT_SHORTS = "domain.shorts_not_shorts"
    STRATEGY_MISSING = "domain.shorts_strategy_missing"
    SCRIPT_MISSING = "domain.shorts_script_missing"
    AUDIO_MISSING = "domain.shorts_audio_missing"
    STALE_INPUT = "domain.shorts_stale_input"
    AUDIO_TOO_LONG = "domain.shorts_audio_too_long"
    EXISTS = "domain.shorts_project_exists"
    NOT_FOUND = "domain.shorts_project_not_found"
    CONFLICT = "domain.shorts_project_conflict"
    TIMELINE_INVALID = "domain.shorts_timeline_invalid"
    ASSET_NOT_FOUND = "domain.shorts_asset_not_found"
    ASSET_OTHER_CHANNEL = "domain.shorts_asset_other_channel"
    ASSET_NOT_ATTACHED = "domain.shorts_asset_not_attached"
    ASSET_RIGHTS_UNKNOWN = "domain.shorts_asset_rights_unknown"
    ASSET_KIND_MISMATCH = "domain.shorts_asset_kind_mismatch"
    ASSET_PROVENANCE_MISMATCH = "domain.shorts_asset_provenance_mismatch"
    CAPTION_INVALID = "domain.shorts_caption_invalid"
    CAPTION_UNSAFE = "domain.shorts_caption_unsafe"


USER_MESSAGES = {
    ShortsProjectErrorCode.PROJECT: "The Shorts project could not be saved.",
    ShortsProjectErrorCode.ITEM_NOT_FOUND: "This content item does not exist.",
    ShortsProjectErrorCode.NOT_SHORTS: "Only a Shorts content item has a Shorts "
    "project.",
    ShortsProjectErrorCode.STRATEGY_MISSING: "The channel has no Shorts format in "
    "its strategy yet.",
    ShortsProjectErrorCode.SCRIPT_MISSING: "This content item has no script yet.",
    ShortsProjectErrorCode.AUDIO_MISSING: "This content item has no audio yet.",
    ShortsProjectErrorCode.STALE_INPUT: "The audio was made from an older script. "
    "Voice the latest script first.",
    ShortsProjectErrorCode.AUDIO_TOO_LONG: "The audio is longer than a Short may be.",
    ShortsProjectErrorCode.EXISTS: "This content item already has a Shorts project.",
    ShortsProjectErrorCode.NOT_FOUND: "This Shorts project does not exist.",
    ShortsProjectErrorCode.CONFLICT: "The Shorts project changed in the meantime. "
    "Reload it and try again.",
    ShortsProjectErrorCode.TIMELINE_INVALID: "This timeline cannot be saved.",
    ShortsProjectErrorCode.ASSET_NOT_FOUND: "A file used in this timeline does not "
    "exist.",
    ShortsProjectErrorCode.ASSET_OTHER_CHANNEL: "A file used in this timeline "
    "belongs to another channel.",
    ShortsProjectErrorCode.ASSET_NOT_ATTACHED: "A file used in this timeline is "
    "not attached to this content item yet.",
    ShortsProjectErrorCode.ASSET_RIGHTS_UNKNOWN: "A file used in this timeline has "
    "no declared rights category yet.",
    ShortsProjectErrorCode.ASSET_KIND_MISMATCH: "A file used in this timeline is "
    "of the wrong type for its role.",
    ShortsProjectErrorCode.ASSET_PROVENANCE_MISMATCH: "The source record of a file "
    "used in this timeline changed. Reload it and try again.",
    ShortsProjectErrorCode.CAPTION_INVALID: "The captions of this timeline break a "
    "rule and cannot be saved.",
    ShortsProjectErrorCode.CAPTION_UNSAFE: "A caption or overlay of this timeline "
    "leaves the safe area of the screen.",
}
HTTP_STATUS = {
    ShortsProjectErrorCode.ITEM_NOT_FOUND: HTTPStatus.NOT_FOUND,
    ShortsProjectErrorCode.NOT_FOUND: HTTPStatus.NOT_FOUND,
    ShortsProjectErrorCode.EXISTS: HTTPStatus.CONFLICT,
    ShortsProjectErrorCode.CONFLICT: HTTPStatus.CONFLICT,
    ShortsProjectErrorCode.STALE_INPUT: HTTPStatus.CONFLICT,
    ShortsProjectErrorCode.ASSET_PROVENANCE_MISMATCH: HTTPStatus.CONFLICT,
}


class ShortsProjectError(DomainError):
    """A Shorts project was refused; ``code`` is a ``ShortsProjectErrorCode``."""

    default_code = ShortsProjectErrorCode.PROJECT.value
    default_user_message = USER_MESSAGES[ShortsProjectErrorCode.PROJECT]

    def __init__(
        self,
        detail: str = "",
        *,
        code: ShortsProjectErrorCode = ShortsProjectErrorCode.PROJECT,
    ) -> None:
        super().__init__(detail, code=code.value, user_message=USER_MESSAGES[code])
        if code in HTTP_STATUS:
            self.default_http_status = HTTP_STATUS[code]


class ShortsProjectNotFoundError(ShortsProjectError):
    default_code = ShortsProjectErrorCode.NOT_FOUND.value
    default_user_message = USER_MESSAGES[ShortsProjectErrorCode.NOT_FOUND]
    default_http_status = HTTPStatus.NOT_FOUND

    def __init__(self, detail: str = "") -> None:
        super().__init__(detail, code=ShortsProjectErrorCode.NOT_FOUND)


@dataclass(frozen=True)
class _Inputs:
    """The latest inputs of a Shorts item and the configuration they give."""

    script_id: str
    script_version: int
    audio_artifact_id: str
    audio_version: int
    audio_sha256: str
    audio_duration_ms: int
    strategy_version: int
    config: ShortsConfig

    def bind(self) -> dict[str, object]:
        """The keyword arguments of ``ShortsProject.create`` and ``rebind``."""
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


def readiness(config: ShortsConfig, timeline: Timeline) -> ShortsProjectStatus:
    """The status a timeline earns; ValueError when it cannot be judged yet.

    The one place that decides the status of a project. A timeline without
    scenes is a ``draft`` and one with scenes (valid by construction, they cover
    the audio) is ``planned``. It is ``ready`` when it is planned, every scene
    has exactly one background (I-096) and the captions are in order (I-097):
    ``check_captions`` finds no error and, when ``config.captions`` is true, the
    captions cover every scene (each voices its script section). With
    ``config.captions`` false the caption list must be empty. Music never
    changes the status. It does not raise; the service refuses caption errors
    before it asks for the status.
    """
    if not timeline.scenes:
        return ShortsProjectStatus.DRAFT
    backgrounds = all(
        sum(asset.role is AssetRole.BACKGROUND for asset in scene.assets) == 1
        for scene in timeline.scenes
    )
    captions = check_captions(config, timeline).ok and (
        not config.captions or covers_every_scene(timeline)
    )
    if backgrounds and captions:
        return ShortsProjectStatus.READY
    return ShortsProjectStatus.PLANNED


class ShortsProjectService:
    def __init__(
        self, database: Database, audit: AuditLog, *, clock: Clock | None = None
    ) -> None:
        self._database = database
        self._audit = audit
        self._clock = clock

    def create(self, content_item_id: str, *, actor: Actor) -> ShortsProject:
        try:
            with self._database.transaction() as connection:
                item = _shorts_item(connection, content_item_id)
                projects = ShortsProjectRepository(connection)
                if projects.get_by_content_item(item.id) is not None:
                    raise _exists(item.id)
                inputs = _latest_inputs(connection, item)
                project = ShortsProject.create(
                    item.id, **inputs.bind(), actor=actor, clock=self._clock
                )
                projects.add(project)
        except sqlite3.IntegrityError:
            # Expected when a concurrent call stored a project of this item
            # first (unique content_item_id); any other integrity error stays.
            with self._database.transaction() as connection:
                stored = ShortsProjectRepository(connection).get_by_content_item(
                    content_item_id
                )
            if stored is None:
                raise
            raise _exists(content_item_id) from None
        self._audit.record(
            CREATED,
            actor,
            EntityRef("shorts_project", project.id),
            AuditResult.SUCCESS,
            _event_metadata(project),
        )
        return project

    def rebind(
        self, project_id: str, *, expected_version: int, actor: Actor
    ) -> ShortsProject:
        with self._database.transaction() as connection:
            projects = ShortsProjectRepository(connection)
            stored = _stored(projects, project_id)
            _check_version(stored, expected_version)
            item = _shorts_item(connection, stored.content_item_id)
            inputs = _latest_inputs(connection, item)
            project = stored.rebind(**inputs.bind(), actor=actor, clock=self._clock)
            _update(projects, project, expected_version)
        self._audit.record(
            REBOUND,
            actor,
            EntityRef("shorts_project", project.id),
            AuditResult.SUCCESS,
            {**_event_metadata(project), "previous_version": stored.version},
        )
        return project

    def save_timeline(
        self,
        project_id: str,
        timeline: Timeline,
        *,
        expected_version: int,
        actor: Actor,
    ) -> ShortsProject:
        with self._database.transaction() as connection:
            projects = ShortsProjectRepository(connection)
            stored = _stored(projects, project_id)
            _check_version(stored, expected_version)
            project = self._with_timeline(
                connection, stored, timeline, actor, projects, expected_version
            )
        self._record_timeline(project, actor)
        return project

    def plan(
        self, project_id: str, *, expected_version: int, actor: Actor
    ) -> ShortsProject:
        """Plan one scene per script section and save it (status ``planned``)."""
        with self._database.transaction() as connection:
            projects = ShortsProjectRepository(connection)
            stored = _stored(projects, project_id)
            _check_version(stored, expected_version)
            script = _bound_script(connection, stored)
            try:
                timeline = plan_timeline(
                    stored, script, _chunk_durations(connection, stored)
                )
            except ValueError as error:
                raise ShortsProjectError(
                    str(error), code=ShortsProjectErrorCode.TIMELINE_INVALID
                ) from None
            project = self._with_timeline(
                connection, stored, timeline, actor, projects, expected_version
            )
        self._record_timeline(project, actor)
        return project

    def caption(
        self, project_id: str, *, expected_version: int, actor: Actor
    ) -> ShortsProject:
        """Plan the captions of the stored scenes and save them."""
        with self._database.transaction() as connection:
            projects = ShortsProjectRepository(connection)
            stored = _stored(projects, project_id)
            _check_version(stored, expected_version)
            script = _bound_script(connection, stored)
            try:
                captions = plan_captions(stored, script, stored.timeline)
                timeline = replace(stored.timeline, captions=captions)
            except ValueError as error:
                raise ShortsProjectError(
                    str(error), code=ShortsProjectErrorCode.CAPTION_INVALID
                ) from None
            project = self._with_timeline(
                connection, stored, timeline, actor, projects, expected_version
            )
        self._record_timeline(project, actor)
        return project

    def _with_timeline(
        self,
        connection: sqlite3.Connection,
        stored: ShortsProject,
        timeline: Timeline,
        actor: Actor,
        projects: ShortsProjectRepository,
        expected_version: int,
    ) -> ShortsProject:
        status = _status_of(stored, timeline)
        _check_sections(stored, timeline, _bound_script(connection, stored))
        timeline = _check_assets(connection, stored, timeline)
        project = stored.with_timeline(timeline, status, actor=actor, clock=self._clock)
        _update(projects, project, expected_version)
        return project

    def _record_timeline(self, project: ShortsProject, actor: Actor) -> None:
        self._audit.record(
            TIMELINE_SAVED,
            actor,
            EntityRef("shorts_project", project.id),
            AuditResult.SUCCESS,
            {
                **_event_metadata(project),
                "timeline_sha256": project.timeline.sha256,
                "scene_count": len(project.timeline.scenes),
                "caption_count": len(project.timeline.captions),
                "caption_warning_count": len(
                    check_captions(project.config, project.timeline).warnings
                ),
                "music_count": len(project.timeline.music),
                "timing_source": project.timeline.timing_source.value,
            },
        )

    def get(self, project_id: str) -> ShortsProject:
        with self._database.transaction() as connection:
            return _stored(ShortsProjectRepository(connection), project_id)

    def get_by_content_item(self, content_item_id: str) -> ShortsProject | None:
        with self._database.transaction() as connection:
            if ContentItemRepository(connection).get(content_item_id) is None:
                raise ShortsProjectError(
                    f"content item {content_item_id} does not exist",
                    code=ShortsProjectErrorCode.ITEM_NOT_FOUND,
                )
            return ShortsProjectRepository(connection).get_by_content_item(
                content_item_id
            )

    def list_by_channel(self, channel_id: str) -> list[ShortsProject]:
        with self._database.transaction() as connection:
            return ShortsProjectRepository(connection).list_by_channel(channel_id)

    def is_current(self, project_id: str) -> bool:
        """True when the bound script and audio are still the item's latest."""
        with self._database.transaction() as connection:
            project = _stored(ShortsProjectRepository(connection), project_id)
            return project_is_current(connection, project)


def project_is_current(connection: sqlite3.Connection, project: ShortsProject) -> bool:
    """True when the bound script and audio are still the item's latest."""
    scripts = ScriptRepository(connection).list_by_content_item(project.content_item_id)
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


def check_audio_assets(
    connection: sqlite3.Connection,
    item: ContentItem,
    placements: Sequence[SceneAsset | AssetPlacement],
) -> dict[str, Asset]:
    """The D5 checks (I-096) of the assets of ``placements``, by asset id.

    Read only and without the provenance snapshot: the same ``_AssetChecker``
    as ``save_timeline`` raises a ``ShortsProjectError`` for the first asset that
    is missing, of another channel, not attached, of unknown rights or of a kind
    its role does not accept.
    """
    checker = _AssetChecker(connection, item)
    return {
        placed.asset_id: checker.check(
            placed.asset_id, ROLE_KINDS[placed.role], placed.role.value
        )
        for placed in placements
    }


def check_timeline_assets(
    connection: sqlite3.Connection,
    item: ContentItem,
    placements: Sequence[SceneAsset | AssetPlacement],
    font_ids: Sequence[str] = (),
) -> dict[str, Asset]:
    """The D5 checks (I-096) of the assets of ``placements`` and of caption fonts.

    Read only, for the render (#100): the same ``_AssetChecker`` as
    ``save_timeline`` raises a ``ShortsProjectError`` for the first asset that
    is missing, of another channel, not attached, of unknown rights or of a kind
    its role (or ``font`` for ``font_ids``) does not accept. The provenance
    snapshot is not compared here; the caller does that.
    """
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


def _exists(content_item_id: str) -> ShortsProjectError:
    return ShortsProjectError(
        f"content item {content_item_id} already has a Shorts project",
        code=ShortsProjectErrorCode.EXISTS,
    )


def _stored(projects: ShortsProjectRepository, project_id: str) -> ShortsProject:
    project = projects.get(project_id)
    if project is None:
        raise ShortsProjectError(
            f"Shorts project {project_id} does not exist",
            code=ShortsProjectErrorCode.NOT_FOUND,
        )
    return project


def _check_version(project: ShortsProject, expected_version: int) -> None:
    if project.version != expected_version:
        raise ShortsProjectError(
            f"Shorts project {project.id} is version {project.version}, "
            f"not {expected_version}",
            code=ShortsProjectErrorCode.CONFLICT,
        )


def _update(
    projects: ShortsProjectRepository, project: ShortsProject, expected_version: int
) -> None:
    try:
        projects.update(project, expected_version=expected_version)
    except ConcurrencyError as error:
        raise ShortsProjectError(
            str(error), code=ShortsProjectErrorCode.CONFLICT
        ) from None


def _status_of(project: ShortsProject, timeline: Timeline) -> ShortsProjectStatus:
    if not isinstance(timeline, Timeline):
        raise ShortsProjectError(
            "the timeline is not a Timeline",
            code=ShortsProjectErrorCode.TIMELINE_INVALID,
        )
    if timeline.duration_ms != project.audio_duration_ms:
        raise ShortsProjectError(
            f"the timeline is {timeline.duration_ms} ms but the audio is "
            f"{project.audio_duration_ms} ms",
            code=ShortsProjectErrorCode.TIMELINE_INVALID,
        )
    _refuse_caption_errors(project, timeline)
    try:
        return readiness(project.config, timeline)
    except ValueError as error:
        raise ShortsProjectError(
            str(error), code=ShortsProjectErrorCode.TIMELINE_INVALID
        ) from None


def _refuse_caption_errors(project: ShortsProject, timeline: Timeline) -> None:
    """Refuse a timeline whose captions break a rule or leave the safe area."""
    check = check_captions(project.config, timeline)
    for kind, code in (
        (IssueKind.INVALID, ShortsProjectErrorCode.CAPTION_INVALID),
        (IssueKind.UNSAFE, ShortsProjectErrorCode.CAPTION_UNSAFE),
    ):
        issues = check.errors_of(kind)
        if issues:
            raise ShortsProjectError(
                "; ".join(issue.message for issue in issues), code=code
            )


def _bound_script(connection: sqlite3.Connection, project: ShortsProject) -> Script:
    script = ScriptRepository(connection).get(project.script_id)
    if script is None:
        raise ShortsProjectError(
            f"script {project.script_id} of the project does not exist",
            code=ShortsProjectErrorCode.SCRIPT_MISSING,
        )
    return script


def _check_sections(project: ShortsProject, timeline: Timeline, script: Script) -> None:
    """Every section of the bound script appears in the scenes, none is extra."""
    if not timeline.scenes:
        return
    sections = len(script.sections)
    used = {scene.section_index for scene in timeline.scenes}
    outside = sorted(index for index in used if index >= sections)
    if outside:
        raise ShortsProjectError(
            f"scenes use section {outside[0]}, but script version "
            f"{project.script_version} has {sections} sections",
            code=ShortsProjectErrorCode.TIMELINE_INVALID,
        )
    missing = sorted(set(range(sections)) - used)
    if missing:
        raise ShortsProjectError(
            f"no scene shows section {missing[0]} of script version "
            f"{project.script_version}",
            code=ShortsProjectErrorCode.TIMELINE_INVALID,
        )


def _check_assets(
    connection: sqlite3.Connection, project: ShortsProject, timeline: Timeline
) -> Timeline:
    """Check every referenced asset (D5) and snapshot its current provenance.

    Read only: the rights gate, rights QC and publish gate decide on their own.
    Returns the timeline with ``provenance_id`` filled in.
    """
    fonts = {caption.style.font_asset_id for caption in timeline.captions} - {None}
    if (
        not any(scene.assets for scene in timeline.scenes)
        and not timeline.music
        and not fonts
    ):
        return timeline
    item = ContentItemRepository(connection).get(project.content_item_id)
    if item is None:
        raise ShortsProjectError(
            f"content item {project.content_item_id} does not exist",
            code=ShortsProjectErrorCode.ITEM_NOT_FOUND,
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

    def check(self, asset_id: str, kinds: frozenset[AssetKind], use: str):
        """The D5 checks of one asset used as ``use``; returns the asset."""
        asset = self._assets.get(asset_id)
        if asset is None:
            raise _asset_error(
                f"asset {asset_id} does not exist",
                ShortsProjectErrorCode.ASSET_NOT_FOUND,
            )
        if asset.channel_id != self._item.channel_id:
            raise _asset_error(
                f"asset {asset.id} belongs to another channel",
                ShortsProjectErrorCode.ASSET_OTHER_CHANNEL,
            )
        if self._usages.get_by_pair(asset.id, self._item.id) is None:
            raise _asset_error(
                f"asset {asset.id} is not attached to content item {self._item.id}",
                ShortsProjectErrorCode.ASSET_NOT_ATTACHED,
            )
        if asset.category is AssetCategory.UNKNOWN:
            raise _asset_error(
                f"asset {asset.id} has the rights category unknown",
                ShortsProjectErrorCode.ASSET_RIGHTS_UNKNOWN,
            )
        if asset.kind not in kinds:
            raise _asset_error(
                f"asset {asset.id} is {asset.kind.value}, which a {use} cannot use",
                ShortsProjectErrorCode.ASSET_KIND_MISMATCH,
            )
        return asset

    def snapshot(self, placed: SceneAsset | AssetPlacement):
        asset = self.check(placed.asset_id, ROLE_KINDS[placed.role], placed.role.value)
        latest = self._provenance.latest(asset.id)
        current = latest.id if latest is not None else None
        if placed.provenance_id is not None and placed.provenance_id != current:
            raise _asset_error(
                f"asset {asset.id} has provenance {current}, not "
                f"{placed.provenance_id}",
                ShortsProjectErrorCode.ASSET_PROVENANCE_MISMATCH,
            )
        return replace(placed, provenance_id=current)


def _asset_error(detail: str, code: ShortsProjectErrorCode) -> ShortsProjectError:
    return ShortsProjectError(detail, code=code)


def _chunk_durations(
    connection: sqlite3.Connection, project: ShortsProject
) -> tuple[int, ...] | None:
    """The chunk durations of the TTS job that voiced the bound audio, if any."""
    audio = AudioMetadataRepository(connection).get_by_artifact(
        project.audio_artifact_id
    )
    if audio is None:
        return None
    job = AIJobRepository(connection).get_by_idempotency_key(
        audio_key(audio.script_id, audio.voice_profile_id, audio.voice_profile_version)
    )
    return chunk_durations(job) if job is not None else None


def _shorts_item(connection: sqlite3.Connection, content_item_id: str) -> ContentItem:
    item = ContentItemRepository(connection).get(content_item_id)
    if item is None:
        raise ShortsProjectError(
            f"content item {content_item_id} does not exist",
            code=ShortsProjectErrorCode.ITEM_NOT_FOUND,
        )
    if item.content_type is not ContentType.SHORTS:
        raise ShortsProjectError(
            f"content item {item.id} is {item.content_type.value}, not shorts",
            code=ShortsProjectErrorCode.NOT_SHORTS,
        )
    return item


def _latest_inputs(connection: sqlite3.Connection, item: ContentItem) -> _Inputs:
    strategy = StrategyProfileRepository(connection).get_by_channel(item.channel_id)
    if strategy is None or strategy.format is None:
        raise ShortsProjectError(
            f"channel {item.channel_id} has no format in its strategy",
            code=ShortsProjectErrorCode.STRATEGY_MISSING,
        )
    config = ShortsConfig.from_format(strategy.format.shorts)
    scripts = ScriptRepository(connection).list_by_content_item(item.id)
    if not scripts:
        raise ShortsProjectError(
            f"content item {item.id} has no script",
            code=ShortsProjectErrorCode.SCRIPT_MISSING,
        )
    script = scripts[-1]
    artifact = ArtifactRepository(connection).latest(item.id, ArtifactKind.AUDIO)
    audio = (
        AudioMetadataRepository(connection).get_by_artifact(artifact.id)
        if artifact is not None
        else None
    )
    if artifact is None or audio is None:
        raise ShortsProjectError(
            f"content item {item.id} has no audio with its details",
            code=ShortsProjectErrorCode.AUDIO_MISSING,
        )
    if audio.script_id != script.id:
        raise ShortsProjectError(
            f"audio version {artifact.version} of content item {item.id} voiced "
            f"script {audio.script_id}, not the latest script {script.id} "
            f"(version {script.version})",
            code=ShortsProjectErrorCode.STALE_INPUT,
        )
    if audio.duration_ms > config.max_seconds * 1000:
        raise ShortsProjectError(
            f"the audio is {audio.duration_ms} ms, longer than the "
            f"{config.max_seconds} seconds a Short may take",
            code=ShortsProjectErrorCode.AUDIO_TOO_LONG,
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


def _event_metadata(project: ShortsProject) -> Mapping[str, MetadataValue]:
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
