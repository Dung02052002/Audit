"""Shorts subtitles (Prompt Pack v8, prompt #098), context C8 Shorts Production.

``ShortsSubtitleRecorder`` stores the timed subtitles of a Shorts project as a
new version of the item's ``SUBTITLES`` ``Artifact``, in the same way
``AudioArtifactRecorder`` (#089) stores audio. The rules were decided on
2026-10-17:

- ``render_srt(timeline, rules)`` is pure: one cue per ``CaptionOverlay`` in
  order, numbered from 1, with ``HH:MM:SS,mmm --> HH:MM:SS,mmm`` times (whole
  milliseconds, hours may pass 99), the text wrapped by ``wrap`` (the same
  lines as on screen), NFC, ``\\n`` line endings, a trailing newline, UTF-8
  without a BOM when encoded. The same timeline gives the same bytes.
  ``render_vtt`` gives WebVTT (``WEBVTT`` header, ``.`` before the
  milliseconds) for later use; only SRT is stored now.
- ``record(project_id, expected_version=..., actor=...)`` refuses unless the
  format has captions on, the timeline has captions that cover every scene
  and ``check_captions`` finds no error (a ``planned`` or ``ready`` project
  both qualify; the backgrounds do not matter for subtitles), the caller read
  the current project version, and the bound script and audio are still the
  latest of the item. Warnings (speed) do not block.
- The same subtitles twice are not a new version: when the latest SUBTITLES
  artifact has the same sha256 it is returned with ``created=False`` and
  nothing is written or audited. Otherwise the file is written through the
  ``ArtifactStorage`` as ``application/x-subrip`` at the next version, the
  artifact is stored and stale approvals are invalidated (#035) in one
  transaction; the file is deleted again when anything fails.
- After the commit each invalidated approval is audited as
  ``approval.invalidated`` and the new version as ``shorts.subtitles_recorded``
  with ids, version, sha256, size, cue count and the sha256 of the timeline,
  never any caption text.
"""

import hashlib
import sqlite3
import unicodedata
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from http import HTTPStatus

from ai_youtube_agent.content.shorts_captions import (
    check_captions,
    covers_every_scene,
    wrap,
)
from ai_youtube_agent.content.shorts_config import CaptionRules
from ai_youtube_agent.content.shorts_project import ShortsProject
from ai_youtube_agent.content.shorts_timeline import Timeline
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
from ai_youtube_agent.core.db.repositories.content import (
    ArtifactRepository,
    ScriptRepository,
)
from ai_youtube_agent.core.db.repositories.shorts import ShortsProjectRepository
from ai_youtube_agent.core.errors import DomainError
from ai_youtube_agent.core.log import get_logger
from ai_youtube_agent.core.version_invalidation import (
    invalidate_stale_approvals,
    record_invalidations,
)

logger = get_logger(__name__)

Clock = Callable[[], datetime]

RECORDED = "shorts.subtitles_recorded"
SRT_MEDIA_TYPE = "application/x-subrip"
_MS_PER_SECOND = 1000
_MS_PER_MINUTE = 60 * _MS_PER_SECOND
_MS_PER_HOUR = 60 * _MS_PER_MINUTE


class ShortsSubtitleErrorCode(StrEnum):
    SUBTITLES = "domain.shorts_subtitles"
    PROJECT_NOT_FOUND = "domain.shorts_subtitles_project_not_found"
    CONFLICT = "domain.shorts_subtitles_conflict"
    CAPTIONS_OFF = "domain.shorts_subtitles_captions_off"
    NO_CAPTIONS = "domain.shorts_subtitles_no_captions"
    CAPTIONS_INVALID = "domain.shorts_subtitles_captions_invalid"
    STALE_INPUT = "domain.shorts_subtitles_stale_input"
    STORAGE_FAILED = "domain.shorts_subtitles_storage_failed"


USER_MESSAGES = {
    ShortsSubtitleErrorCode.SUBTITLES: "The subtitles could not be saved.",
    ShortsSubtitleErrorCode.PROJECT_NOT_FOUND: "This Shorts project does not exist.",
    ShortsSubtitleErrorCode.CONFLICT: "The Shorts project changed in the meantime. "
    "Reload it and try again.",
    ShortsSubtitleErrorCode.CAPTIONS_OFF: "The format of this Short has captions "
    "off, so it has no subtitles.",
    ShortsSubtitleErrorCode.NO_CAPTIONS: "Add captions that cover every scene "
    "before saving the subtitles.",
    ShortsSubtitleErrorCode.CAPTIONS_INVALID: "The captions break a rule, so the "
    "subtitles cannot be saved.",
    ShortsSubtitleErrorCode.STALE_INPUT: "The script or audio of this project is "
    "out of date. Rebind the project first.",
    ShortsSubtitleErrorCode.STORAGE_FAILED: "The subtitle file could not be stored.",
}
HTTP_STATUS = {
    ShortsSubtitleErrorCode.PROJECT_NOT_FOUND: HTTPStatus.NOT_FOUND,
    ShortsSubtitleErrorCode.CONFLICT: HTTPStatus.CONFLICT,
    ShortsSubtitleErrorCode.STALE_INPUT: HTTPStatus.CONFLICT,
}


class ShortsSubtitleError(DomainError):
    """Subtitles were refused; ``code`` is a ``ShortsSubtitleErrorCode``."""

    default_code = ShortsSubtitleErrorCode.SUBTITLES.value
    default_user_message = USER_MESSAGES[ShortsSubtitleErrorCode.SUBTITLES]

    def __init__(
        self,
        detail: str = "",
        *,
        code: ShortsSubtitleErrorCode = ShortsSubtitleErrorCode.SUBTITLES,
    ) -> None:
        super().__init__(detail, code=code.value, user_message=USER_MESSAGES[code])
        if code in HTTP_STATUS:
            self.default_http_status = HTTP_STATUS[code]


@dataclass(frozen=True)
class SubtitleResult:
    artifact: Artifact
    created: bool


# Rendering


def _stamp(ms: int, separator: str) -> str:
    hours, rest = divmod(ms, _MS_PER_HOUR)
    minutes, rest = divmod(rest, _MS_PER_MINUTE)
    seconds, millis = divmod(rest, _MS_PER_SECOND)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}{separator}{millis:03d}"


def _cues(timeline: Timeline, rules: CaptionRules, separator: str) -> list[str]:
    cues = []
    for caption in timeline.captions:
        lines = wrap(caption.text, rules)
        times = (
            f"{_stamp(caption.start_ms, separator)} --> "
            f"{_stamp(caption.end_ms, separator)}"
        )
        cues.append("\n".join((times, *lines)))
    return cues


def render_srt(timeline: Timeline, rules: CaptionRules) -> str:
    """The SRT text of the captions of ``timeline``; ValueError if one cannot wrap."""
    cues = _cues(timeline, rules, ",")
    blocks = [f"{number}\n{cue}" for number, cue in enumerate(cues, 1)]
    return unicodedata.normalize("NFC", "\n\n".join(blocks) + "\n") if blocks else ""


def render_vtt(timeline: Timeline, rules: CaptionRules) -> str:
    """The WebVTT text of the captions of ``timeline``; ValueError if one cannot
    wrap.
    """
    cues = _cues(timeline, rules, ".")
    return unicodedata.normalize("NFC", "\n\n".join(["WEBVTT", *cues]) + "\n")


# Recording


class ShortsSubtitleRecorder:
    def __init__(
        self,
        database: Database,
        storage: ArtifactStorage,
        audit: AuditLog,
        *,
        clock: Clock | None = None,
    ) -> None:
        self._database = database
        self._storage = storage
        self._audit = audit
        self._clock = clock

    def record(
        self, project_id: str, *, expected_version: int, actor: Actor
    ) -> SubtitleResult:
        written: StoredFile | None = None
        try:
            with self._database.transaction() as connection:
                project = _stored(connection, project_id)
                if project.version != expected_version:
                    raise ShortsSubtitleError(
                        f"Shorts project {project.id} is version {project.version}, "
                        f"not {expected_version}",
                        code=ShortsSubtitleErrorCode.CONFLICT,
                    )
                data = _subtitle_bytes(project)
                _check_current(connection, project)
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
                        media_type=SRT_MEDIA_TYPE,
                    )
                except (OSError, ValueError) as error:
                    raise ShortsSubtitleError(
                        f"the subtitles of project {project.id} could not be "
                        f"stored: {error}",
                        code=ShortsSubtitleErrorCode.STORAGE_FAILED,
                    ) from error
                artifact = _artifact(latest, item_id, written, self._clock)
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
            _event_metadata(project, artifact),
        )
        return SubtitleResult(artifact, True)

    def _discard(self, uri: str) -> None:
        try:
            self._storage.delete(uri)
        except Exception:
            # The first error must win; the cleanup failure is only logged.
            logger.warning(
                "subtitle file could not be removed after a failed record",
                extra={"fields": {"uri": uri}},
            )


def _stored(connection: sqlite3.Connection, project_id: str) -> ShortsProject:
    project = ShortsProjectRepository(connection).get(project_id)
    if project is None:
        raise ShortsSubtitleError(
            f"Shorts project {project_id} does not exist",
            code=ShortsSubtitleErrorCode.PROJECT_NOT_FOUND,
        )
    return project


def _subtitle_bytes(project: ShortsProject) -> bytes:
    """The SRT bytes of a project whose captions are on, complete and valid."""
    config = project.config
    timeline = project.timeline
    if not config.captions:
        raise ShortsSubtitleError(
            f"the format of project {project.id} has captions off",
            code=ShortsSubtitleErrorCode.CAPTIONS_OFF,
        )
    if not timeline.captions or not covers_every_scene(timeline):
        raise ShortsSubtitleError(
            f"project {project.id} has no captions that cover every scene",
            code=ShortsSubtitleErrorCode.NO_CAPTIONS,
        )
    check = check_captions(config, timeline)
    if not check.ok:
        raise ShortsSubtitleError(
            "; ".join(issue.message for issue in check.errors),
            code=ShortsSubtitleErrorCode.CAPTIONS_INVALID,
        )
    try:
        return render_srt(timeline, config.caption_rules).encode("utf-8")
    except ValueError as error:
        raise ShortsSubtitleError(
            str(error), code=ShortsSubtitleErrorCode.CAPTIONS_INVALID
        ) from None


def _check_current(connection: sqlite3.Connection, project: ShortsProject) -> None:
    """The bound script and audio must still be the latest of the item."""
    scripts = ScriptRepository(connection).list_by_content_item(project.content_item_id)
    audio = ArtifactRepository(connection).latest(
        project.content_item_id, ArtifactKind.AUDIO
    )
    current = (
        bool(scripts)
        and audio is not None
        and scripts[-1].id == project.script_id
        and scripts[-1].version == project.script_version
        and audio.id == project.audio_artifact_id
        and audio.version == project.audio_version
        and audio.sha256 == project.audio_sha256
    )
    if not current:
        raise ShortsSubtitleError(
            f"the script or audio bound to project {project.id} is no longer the "
            "latest of its content item",
            code=ShortsSubtitleErrorCode.STALE_INPUT,
        )


def _artifact(
    latest: Artifact | None, item_id: str, stored: StoredFile, clock: Clock | None
) -> Artifact:
    if latest is None:
        return Artifact.create(
            item_id,
            ArtifactKind.SUBTITLES,
            uri=stored.uri,
            sha256=stored.sha256,
            size_bytes=stored.size_bytes,
            media_type=SRT_MEDIA_TYPE,
            clock=clock,
        )
    return latest.next_version(
        uri=stored.uri,
        sha256=stored.sha256,
        size_bytes=stored.size_bytes,
        media_type=SRT_MEDIA_TYPE,
        clock=clock,
    )


def _event_metadata(
    project: ShortsProject, artifact: Artifact
) -> Mapping[str, MetadataValue]:
    return {
        "project_id": project.id,
        "content_item_id": artifact.content_item_id,
        "version": artifact.version,
        "sha256": artifact.sha256,
        "size_bytes": artifact.size_bytes,
        "cue_count": len(project.timeline.captions),
        "timeline_sha256": project.timeline.sha256,
        "project_version": project.version,
    }
