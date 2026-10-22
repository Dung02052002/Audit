"""LongForm subtitles (Prompt Pack v8, prompt #109), context C9 LongForm Production.

``LongFormSubtitleRecorder`` stores the timed subtitles of a ``LongFormProject``
as a new version of the item's ``SUBTITLES`` ``Artifact`` (SRT by default or
WebVTT when ``subtitle_format=SubtitleFormat.VTT``).

Rules:
- While ``LONGFORM_ENABLED`` is off (``FeatureFlags.longform_enabled`` is
  ``False``), every service call is refused with ``LongFormDisabledError``
  before reading or writing.
- ``render_longform_srt(timeline, rules)`` and
  ``render_longform_vtt(timeline, rules)`` are pure: one cue per
  ``CaptionOverlay`` in order, wrapped by ``wrap``, NFC normalized, ``\\n`` line
  endings, trailing newline, UTF-8 without BOM.
- ``record(project_id, expected_version=..., actor=..., subtitle_format=...)``
  refuses unless ``config.captions`` is true, the timeline captions cover every
  scene, ``check_longform_captions`` finds no error, ``expected_version``
  matches, and the bound script and audio are still the item's latest.
- Idempotent replay: when the latest ``SUBTITLES`` artifact has the same
  ``sha256``, returns ``SubtitleResult(latest, created=False)`` without writing
  or auditing. Otherwise writes through ``ArtifactStorage``, stores the new
  ``Artifact`` version, invalidates stale approvals in the same transaction,
  and audits ``longform.subtitles_recorded`` with scalar metadata only.
"""

import hashlib
import sqlite3
import unicodedata
from collections.abc import Callable, Mapping
from datetime import datetime
from enum import StrEnum
from http import HTTPStatus

from ai_youtube_agent.content.longform_captions import (
    check_longform_captions,
    covers_every_longform_scene,
)
from ai_youtube_agent.content.longform_project import LongFormProject
from ai_youtube_agent.content.longform_script_generator import LongFormDisabledError
from ai_youtube_agent.content.longform_service import longform_project_is_current
from ai_youtube_agent.content.longform_timeline import LongFormTimeline
from ai_youtube_agent.content.shorts_captions import wrap
from ai_youtube_agent.content.shorts_config import CaptionRules
from ai_youtube_agent.content.shorts_subtitles import (
    SRT_MEDIA_TYPE,
    SubtitleResult,
)
from ai_youtube_agent.core.artifact import Artifact, ArtifactKind
from ai_youtube_agent.core.artifact_storage import ArtifactStorage, StoredFile
from ai_youtube_agent.core.audit import (
    Actor,
    AuditLog,
    AuditResult,
    EntityRef,
    MetadataValue,
)
from ai_youtube_agent.core.db.database import Database
from ai_youtube_agent.core.db.repositories.content import ArtifactRepository
from ai_youtube_agent.core.db.repositories.longform import LongFormProjectRepository
from ai_youtube_agent.core.errors import DomainError
from ai_youtube_agent.core.flags import FeatureFlags
from ai_youtube_agent.core.log import get_logger
from ai_youtube_agent.core.version_invalidation import (
    invalidate_stale_approvals,
    record_invalidations,
)

logger = get_logger(__name__)

Clock = Callable[[], datetime]

RECORDED = "longform.subtitles_recorded"
VTT_MEDIA_TYPE = "text/vtt"
_MS_PER_SECOND = 1000
_MS_PER_MINUTE = 60 * _MS_PER_SECOND
_MS_PER_HOUR = 60 * _MS_PER_MINUTE


class SubtitleFormat(StrEnum):
    SRT = "srt"
    VTT = "vtt"


class LongFormSubtitleErrorCode(StrEnum):
    SUBTITLES = "domain.longform_subtitles"
    PROJECT_NOT_FOUND = "domain.longform_subtitles_project_not_found"
    CONFLICT = "domain.longform_subtitles_conflict"
    CAPTIONS_OFF = "domain.longform_subtitles_captions_off"
    NO_CAPTIONS = "domain.longform_subtitles_no_captions"
    CAPTIONS_INVALID = "domain.longform_subtitles_captions_invalid"
    STALE_INPUT = "domain.longform_subtitles_stale_input"
    STORAGE_FAILED = "domain.longform_subtitles_storage_failed"


USER_MESSAGES = {
    LongFormSubtitleErrorCode.SUBTITLES: "The subtitles could not be saved.",
    LongFormSubtitleErrorCode.PROJECT_NOT_FOUND: (
        "This LongForm project does not exist."
    ),
    LongFormSubtitleErrorCode.CONFLICT: (
        "The LongForm project changed in the meantime. Reload it and try again."
    ),
    LongFormSubtitleErrorCode.CAPTIONS_OFF: (
        "The format of this LongForm video has captions off, so it has no subtitles."
    ),
    LongFormSubtitleErrorCode.NO_CAPTIONS: (
        "Add captions that cover every scene before saving the subtitles."
    ),
    LongFormSubtitleErrorCode.CAPTIONS_INVALID: (
        "The captions break a rule, so the subtitles cannot be saved."
    ),
    LongFormSubtitleErrorCode.STALE_INPUT: (
        "The script or audio of this project is out of date. Rebind the project first."
    ),
    LongFormSubtitleErrorCode.STORAGE_FAILED: (
        "The subtitle file could not be stored."
    ),
}

HTTP_STATUS = {
    LongFormSubtitleErrorCode.PROJECT_NOT_FOUND: HTTPStatus.NOT_FOUND,
    LongFormSubtitleErrorCode.CONFLICT: HTTPStatus.CONFLICT,
    LongFormSubtitleErrorCode.STALE_INPUT: HTTPStatus.CONFLICT,
}


class LongFormSubtitleError(DomainError):
    """LongForm subtitles were refused; ``code`` is a ``LongFormSubtitleErrorCode``."""

    default_code = LongFormSubtitleErrorCode.SUBTITLES.value
    default_user_message = USER_MESSAGES[LongFormSubtitleErrorCode.SUBTITLES]

    def __init__(
        self,
        detail: str = "",
        *,
        code: LongFormSubtitleErrorCode = LongFormSubtitleErrorCode.SUBTITLES,
    ) -> None:
        super().__init__(detail, code=code.value, user_message=USER_MESSAGES[code])
        if code in HTTP_STATUS:
            self.default_http_status = HTTP_STATUS[code]


def _stamp(ms: int, separator: str) -> str:
    hours, rest = divmod(ms, _MS_PER_HOUR)
    minutes, rest = divmod(rest, _MS_PER_MINUTE)
    seconds, millis = divmod(rest, _MS_PER_SECOND)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}{separator}{millis:03d}"


def _cues(timeline: LongFormTimeline, rules: CaptionRules, separator: str) -> list[str]:
    cues = []
    for caption in timeline.captions:
        lines = wrap(caption.text, rules)
        times = (
            f"{_stamp(caption.start_ms, separator)} --> "
            f"{_stamp(caption.end_ms, separator)}"
        )
        cues.append("\n".join((times, *lines)))
    return cues


def render_longform_srt(timeline: LongFormTimeline, rules: CaptionRules) -> str:
    """The SRT text of the captions of ``timeline``; ValueError if one cannot wrap."""
    cues = _cues(timeline, rules, ",")
    blocks = [f"{number}\n{cue}" for number, cue in enumerate(cues, 1)]
    return unicodedata.normalize("NFC", "\n\n".join(blocks) + "\n") if blocks else ""


def render_longform_vtt(timeline: LongFormTimeline, rules: CaptionRules) -> str:
    """The WebVTT text of the captions of ``timeline``.

    ValueError if one cannot wrap.
    """
    cues = _cues(timeline, rules, ".")
    return unicodedata.normalize("NFC", "\n\n".join(["WEBVTT", *cues]) + "\n")


class LongFormSubtitleRecorder:
    def __init__(
        self,
        database: Database,
        storage: ArtifactStorage,
        audit: AuditLog,
        flags: FeatureFlags | None = None,
        *,
        clock: Clock | None = None,
    ) -> None:
        self._database = database
        self._storage = storage
        self._audit = audit
        self._flags = (
            flags if flags is not None else FeatureFlags(longform_enabled=True)
        )
        self._clock = clock

    def record(
        self,
        project_id: str,
        *,
        expected_version: int,
        actor: Actor,
        subtitle_format: SubtitleFormat = SubtitleFormat.SRT,
    ) -> SubtitleResult:
        if not self._flags.longform_enabled:
            raise LongFormDisabledError("LONGFORM_ENABLED is off")
        if not isinstance(subtitle_format, SubtitleFormat):
            raise TypeError("subtitle_format must be a SubtitleFormat")
        media_type = (
            SRT_MEDIA_TYPE if subtitle_format is SubtitleFormat.SRT else VTT_MEDIA_TYPE
        )
        written: StoredFile | None = None
        try:
            with self._database.transaction() as connection:
                project = _stored(connection, project_id)
                if project.version != expected_version:
                    raise LongFormSubtitleError(
                        f"LongForm project {project.id} is version "
                        f"{project.version}, not {expected_version}",
                        code=LongFormSubtitleErrorCode.CONFLICT,
                    )
                data = _subtitle_bytes(project, subtitle_format)
                if not longform_project_is_current(connection, project):
                    raise LongFormSubtitleError(
                        f"the script or audio bound to project {project.id} is "
                        "no longer the latest of its content item",
                        code=LongFormSubtitleErrorCode.STALE_INPUT,
                    )
                artifacts = ArtifactRepository(connection)
                item_id = project.content_item_id
                latest = artifacts.latest(item_id, ArtifactKind.SUBTITLES)
                checksum = hashlib.sha256(data).hexdigest()
                if latest is not None and latest.sha256 == checksum:
                    return SubtitleResult(latest, False)
                version = latest.version + 1 if latest else 1
                try:
                    written = self._storage.write(
                        item_id,
                        ArtifactKind.SUBTITLES,
                        version,
                        data,
                        media_type=media_type,
                    )
                except (OSError, ValueError) as error:
                    raise LongFormSubtitleError(
                        f"the subtitles of project {project.id} could not be "
                        f"stored: {error}",
                        code=LongFormSubtitleErrorCode.STORAGE_FAILED,
                    ) from error
                artifact = _artifact(latest, item_id, written, media_type, self._clock)
                artifacts.add(artifact)
                invalidation = invalidate_stale_approvals(
                    connection, item_id, clock=self._clock
                )
        except BaseException:
            if written is not None:
                self._discard(written.uri)
            raise
        record_invalidations(self._audit, invalidation)
        self._audit.record(
            RECORDED,
            actor,
            EntityRef("artifact", artifact.id),
            AuditResult.SUCCESS,
            _event_metadata(project, artifact, subtitle_format),
        )
        return SubtitleResult(artifact, True)

    def _discard(self, uri: str) -> None:
        try:
            self._storage.delete(uri)
        except Exception:
            logger.warning(
                "subtitle file could not be removed after a failed record",
                extra={"fields": {"uri": uri}},
            )


def _stored(connection: sqlite3.Connection, project_id: str) -> LongFormProject:
    project = LongFormProjectRepository(connection).get(project_id)
    if project is None:
        raise LongFormSubtitleError(
            f"LongForm project {project_id} does not exist",
            code=LongFormSubtitleErrorCode.PROJECT_NOT_FOUND,
        )
    return project


def _subtitle_bytes(project: LongFormProject, subtitle_format: SubtitleFormat) -> bytes:
    config = project.config
    timeline = project.timeline
    if not config.captions:
        raise LongFormSubtitleError(
            f"the format of project {project.id} has captions off",
            code=LongFormSubtitleErrorCode.CAPTIONS_OFF,
        )
    if not timeline.captions or not covers_every_longform_scene(timeline):
        raise LongFormSubtitleError(
            f"project {project.id} has no captions that cover every scene",
            code=LongFormSubtitleErrorCode.NO_CAPTIONS,
        )
    check = check_longform_captions(config, timeline)
    if not check.ok:
        raise LongFormSubtitleError(
            "; ".join(issue.message for issue in check.errors),
            code=LongFormSubtitleErrorCode.CAPTIONS_INVALID,
        )
    try:
        if subtitle_format is SubtitleFormat.SRT:
            text = render_longform_srt(timeline, config.caption_rules)
        else:
            text = render_longform_vtt(timeline, config.caption_rules)
        return text.encode("utf-8")
    except ValueError as error:
        raise LongFormSubtitleError(
            str(error), code=LongFormSubtitleErrorCode.CAPTIONS_INVALID
        ) from None


def _artifact(
    latest: Artifact | None,
    content_item_id: str,
    written: StoredFile,
    media_type: str,
    clock: Clock | None,
) -> Artifact:
    kwargs: dict[str, object] = {
        "uri": written.uri,
        "sha256": written.sha256,
        "size_bytes": written.size_bytes,
        "media_type": media_type,
        "clock": clock,
    }
    if latest is None:
        return Artifact.create(content_item_id, ArtifactKind.SUBTITLES, **kwargs)
    return latest.next_version(**kwargs)


def _event_metadata(
    project: LongFormProject,
    artifact: Artifact,
    subtitle_format: SubtitleFormat,
) -> Mapping[str, MetadataValue]:
    return {
        "content_item_id": project.content_item_id,
        "longform_project_id": project.id,
        "longform_project_version": project.version,
        "script_id": project.script_id,
        "script_version": project.script_version,
        "audio_artifact_id": project.audio_artifact_id,
        "audio_version": project.audio_version,
        "timeline_sha256": project.timeline.sha256,
        "subtitle_format": subtitle_format.value,
        "version": artifact.version,
        "sha256": artifact.sha256,
        "size_bytes": artifact.size_bytes,
        "cue_count": len(project.timeline.captions),
        "warning_count": len(
            check_longform_captions(project.config, project.timeline).warnings
        ),
    }
