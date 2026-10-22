"""Shorts Artifact Bundle (Prompt Pack v8, prompt I-103), context C8 Video Production.

``ShortsBundleBuilder`` packages and cross-validates the complete deliverable
bundle for a ``ShortsProject`` (#094):

- Rendered MP4 ``VIDEO`` artifact (#100): verified against the storage file,
  ``probe_mp4`` (duration within frame tolerance of ``project.audio_duration_ms``)
  and the succeeded ``video.render`` ``AIJob`` whose ``video.rendered``
  checkpoint matches ``artifact.id``, ``artifact.sha256`` and
  ``project.timeline.sha256``.
- Timed SRT ``SUBTITLES`` artifact (#098): required when
  ``project.config.captions`` is ``True`` (and ``None`` when ``False``),
  verified against storage bytes and ``render_srt(project.timeline,
  project.config.caption_rules)``.
- Vertical ``THUMBNAIL`` artifact (#101): verified against storage bytes and
  ``probe_image``.
- Canonical JSON ``METADATA`` artifact (#102): verified against storage bytes,
  parsed into ``ShortsMetadata`` and checked for binding to the current
  ``project.id``, ``project.version``, ``project.script_id`` and
  ``project.script_version``.
- Bound ``AUDIO`` artifact (#089): verified to match ``project.audio_artifact_id``,
  ``project.audio_version`` and ``project.audio_sha256``.
- ``RightsReport`` (#082) and ``PolicyReport`` (#083): evaluated on demand;
  any blocking rights verdict (``rights_report.gate.blocks``), missing or
  mismatched AI disclosure decision, or blocking policy verdict
  (``policy_report.blocks``) refuses the bundle.
- Audits ``shorts.bundle_built`` with scalar metadata and ``manifest_sha256``,
  never raw text or bytes.
"""

import hashlib
import json
import sqlite3
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from http import HTTPStatus
from typing import Any, Final

from ai_youtube_agent.content.policy_check import (
    PolicyChecker,
    PolicyInput,
    PolicyStatus,
)
from ai_youtube_agent.content.policy_report import (
    FindingReport,
    PolicyReport,
    PolicyReporter,
    RuleOutcomeReport,
)
from ai_youtube_agent.content.policy_rule import RuleSet
from ai_youtube_agent.content.rights_report import RightsReport, RightsReporter
from ai_youtube_agent.content.shorts_metadata import (
    METADATA_MEDIA_TYPE,
    ShortsMetadata,
)
from ai_youtube_agent.content.shorts_project import ShortsProject, ShortsProjectStatus
from ai_youtube_agent.content.shorts_render import (
    JOB_KIND as RENDER_JOB_KIND,
)
from ai_youtube_agent.content.shorts_render import (
    MP4_MEDIA_TYPE,
    RENDERED_STEP,
)
from ai_youtube_agent.content.shorts_service import (
    ShortsProjectNotFoundError,
    project_is_current,
    readiness,
)
from ai_youtube_agent.content.shorts_subtitles import SRT_MEDIA_TYPE, render_srt
from ai_youtube_agent.content.shorts_thumbnail import ImageInfo, probe_image
from ai_youtube_agent.content.text_prompts import banned_phrases
from ai_youtube_agent.core.artifact import Artifact, ArtifactKind
from ai_youtube_agent.core.artifact_storage import ArtifactStorage
from ai_youtube_agent.core.audit import (
    Actor,
    AuditLog,
    AuditResult,
    EntityRef,
    MetadataValue,
)
from ai_youtube_agent.core.content_item import ContentItem
from ai_youtube_agent.core.db.database import Database
from ai_youtube_agent.core.db.repositories.channel import StrategyProfileRepository
from ai_youtube_agent.core.db.repositories.content import (
    ArtifactRepository,
    ContentItemRepository,
)
from ai_youtube_agent.core.db.repositories.jobs import AIJobRepository
from ai_youtube_agent.core.db.repositories.shorts import ShortsProjectRepository
from ai_youtube_agent.core.errors import DomainError
from ai_youtube_agent.pipeline.job import AIJob, AIJobStatus
from ai_youtube_agent.providers.video_render import Mp4Info, probe_mp4

BUNDLE_BUILT: Final[str] = "shorts.bundle_built"
BUNDLE_SCHEMA_VERSION: Final[str] = "shorts-bundle-v1"

Clock = Callable[[], datetime]


class ShortsBundleErrorCode(StrEnum):
    BUNDLE = "domain.shorts_bundle"
    PROJECT_NOT_READY = "domain.shorts_bundle_project_not_ready"
    STALE_INPUT = "domain.shorts_bundle_stale_input"
    VIDEO_MISSING = "domain.shorts_bundle_video_missing"
    VIDEO_STALE = "domain.shorts_bundle_video_stale"
    VIDEO_INVALID = "domain.shorts_bundle_video_invalid"
    SUBTITLES_MISSING = "domain.shorts_bundle_subtitles_missing"
    SUBTITLES_STALE = "domain.shorts_bundle_subtitles_stale"
    THUMBNAIL_MISSING = "domain.shorts_bundle_thumbnail_missing"
    THUMBNAIL_INVALID = "domain.shorts_bundle_thumbnail_invalid"
    METADATA_MISSING = "domain.shorts_bundle_metadata_missing"
    METADATA_STALE = "domain.shorts_bundle_metadata_stale"
    METADATA_INVALID = "domain.shorts_bundle_metadata_invalid"
    ARTIFACT_CORRUPTED = "domain.shorts_bundle_artifact_corrupted"
    RIGHTS_BLOCKED = "domain.shorts_bundle_rights_blocked"
    DISCLOSURE_MISMATCH = "domain.shorts_bundle_disclosure_mismatch"
    POLICY_BLOCKED = "domain.shorts_bundle_policy_blocked"


USER_MESSAGES: Final[dict[ShortsBundleErrorCode, str]] = {
    ShortsBundleErrorCode.BUNDLE: (
        "The Shorts artifact bundle could not be assembled."
    ),
    ShortsBundleErrorCode.PROJECT_NOT_READY: (
        "This Shorts project is not ready to be bundled."
    ),
    ShortsBundleErrorCode.STALE_INPUT: (
        "The Shorts project is out of date with its content item, script or audio."
    ),
    ShortsBundleErrorCode.VIDEO_MISSING: (
        "The rendered MP4 video artifact is missing."
    ),
    ShortsBundleErrorCode.VIDEO_STALE: (
        "The rendered MP4 video does not match the current Shorts timeline."
    ),
    ShortsBundleErrorCode.VIDEO_INVALID: ("The rendered MP4 video file is invalid."),
    ShortsBundleErrorCode.SUBTITLES_MISSING: ("The SRT subtitles artifact is missing."),
    ShortsBundleErrorCode.SUBTITLES_STALE: (
        "The SRT subtitles artifact does not match the current Shorts captions."
    ),
    ShortsBundleErrorCode.THUMBNAIL_MISSING: (
        "The Shorts thumbnail artifact is missing."
    ),
    ShortsBundleErrorCode.THUMBNAIL_INVALID: ("The Shorts thumbnail file is invalid."),
    ShortsBundleErrorCode.METADATA_MISSING: (
        "The Shorts metadata artifact is missing."
    ),
    ShortsBundleErrorCode.METADATA_STALE: (
        "The Shorts metadata artifact belongs to an older project version."
    ),
    ShortsBundleErrorCode.METADATA_INVALID: ("The Shorts metadata file is invalid."),
    ShortsBundleErrorCode.ARTIFACT_CORRUPTED: (
        "An artifact file on disk is missing or does not match its recorded checksum."
    ),
    ShortsBundleErrorCode.RIGHTS_BLOCKED: (
        "The rights gate blocked this Shorts bundle."
    ),
    ShortsBundleErrorCode.DISCLOSURE_MISMATCH: (
        "The AI disclosure decision is missing or out of date with the metadata."
    ),
    ShortsBundleErrorCode.POLICY_BLOCKED: (
        "The policy check blocked this Shorts bundle."
    ),
}


class ShortsBundleError(DomainError):
    default_code = ShortsBundleErrorCode.BUNDLE.value
    default_user_message = USER_MESSAGES[ShortsBundleErrorCode.BUNDLE]
    default_http_status = HTTPStatus.UNPROCESSABLE_ENTITY

    def __init__(
        self,
        detail: str = "",
        *,
        code: ShortsBundleErrorCode = ShortsBundleErrorCode.BUNDLE,
        http_status: int | None = None,
    ) -> None:
        status = (
            http_status
            if http_status is not None
            else (
                HTTPStatus.CONFLICT
                if code
                in (
                    ShortsBundleErrorCode.PROJECT_NOT_READY,
                    ShortsBundleErrorCode.STALE_INPUT,
                    ShortsBundleErrorCode.VIDEO_MISSING,
                    ShortsBundleErrorCode.VIDEO_STALE,
                    ShortsBundleErrorCode.SUBTITLES_MISSING,
                    ShortsBundleErrorCode.SUBTITLES_STALE,
                    ShortsBundleErrorCode.THUMBNAIL_MISSING,
                    ShortsBundleErrorCode.METADATA_MISSING,
                    ShortsBundleErrorCode.METADATA_STALE,
                    ShortsBundleErrorCode.DISCLOSURE_MISMATCH,
                )
                else HTTPStatus.UNPROCESSABLE_ENTITY
            )
        )
        super().__init__(
            detail,
            code=code.value,
            user_message=USER_MESSAGES[code],
            http_status=status,
        )


class ShortsBundleConflictError(DomainError):
    default_code = "domain.shorts_bundle_conflict"
    default_user_message = (
        "This Shorts project was updated by someone else; reload and try again."
    )
    default_http_status = HTTPStatus.CONFLICT


@dataclass(frozen=True)
class ShortsArtifactBundle:
    project: ShortsProject
    channel_id: str
    audio: Artifact
    video: Artifact
    video_info: Mp4Info
    render_job_id: str
    subtitles: Artifact | None
    thumbnail: Artifact
    thumbnail_info: ImageInfo
    metadata_artifact: Artifact
    metadata: ShortsMetadata
    rights_report: RightsReport
    policy_report: PolicyReport
    built_at: datetime
    schema_version: str = BUNDLE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != BUNDLE_SCHEMA_VERSION:
            raise ValueError(f"schema_version must be {BUNDLE_SCHEMA_VERSION!r}")
        if self.built_at.utcoffset() != timedelta(0):
            raise ValueError("built_at must be timezone-aware UTC")
        if self.rights_report.gate.blocks:
            raise ValueError("a blocked rights_report cannot be in a bundle")
        if self.policy_report.blocks:
            raise ValueError("a blocked policy_report cannot be in a bundle")
        if self.project.config.captions and self.subtitles is None:
            raise ValueError("subtitles artifact is required when captions are enabled")
        if not self.project.config.captions and self.subtitles is not None:
            raise ValueError(
                "subtitles artifact must be None when captions are disabled"
            )

    @property
    def artifacts(self) -> tuple[Artifact, ...]:
        """All deliverable artifacts in canonical order."""
        items: list[Artifact] = [self.video]
        if self.subtitles is not None:
            items.append(self.subtitles)
        items.extend((self.thumbnail, self.metadata_artifact, self.audio))
        return tuple(items)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "content_item_id": self.project.content_item_id,
            "channel_id": self.channel_id,
            "project_id": self.project.id,
            "project_version": self.project.version,
            "timeline_sha256": self.project.timeline.sha256,
            "render_job_id": self.render_job_id,
            "built_at": self.built_at.isoformat(),
            "audio": _artifact_summary(self.audio),
            "video": {
                **_artifact_summary(self.video),
                "duration_ms": self.video_info.duration_ms,
            },
            "subtitles": (
                _artifact_summary(self.subtitles)
                if self.subtitles is not None
                else None
            ),
            "thumbnail": {
                **_artifact_summary(self.thumbnail),
                "width": self.thumbnail_info.width,
                "height": self.thumbnail_info.height,
            },
            "metadata_artifact": _artifact_summary(self.metadata_artifact),
            "metadata": self.metadata.to_dict(),
            "rights_report": self.rights_report.to_dict(),
            "policy_report": self.policy_report.to_dict(),
        }

    @property
    def manifest_sha256(self) -> str:
        canonical = json.dumps(
            self.to_dict(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(canonical).hexdigest()


class ShortsBundleBuilder:
    def __init__(
        self,
        database: Database,
        storage: ArtifactStorage,
        rights_reporter: RightsReporter,
        policy_reporter: PolicyReporter,
        audit: AuditLog,
        *,
        clock: Clock | None = None,
    ) -> None:
        self._database = database
        self._storage = storage
        self._rights_reporter = rights_reporter
        self._policy_reporter = policy_reporter
        self._audit = audit
        self._clock = clock

    def build(
        self,
        project_id: str,
        *,
        actor: Actor,
        expected_version: int | None = None,
    ) -> ShortsArtifactBundle:
        if not isinstance(project_id, str):
            raise TypeError("project_id must be text")
        if not isinstance(actor, Actor):
            raise TypeError("actor must be an Actor")
        if expected_version is not None and (
            isinstance(expected_version, bool)
            or not isinstance(expected_version, int)
            or expected_version < 1
        ):
            raise ValueError("expected_version must be an integer >= 1")

        with self._database.transaction() as connection:
            now = self._now()
            project, item = _load_ready_project(
                connection, project_id, expected_version
            )
            artifacts = ArtifactRepository(connection)
            audio = _verify_audio(artifacts, self._storage, project)
            video, video_info, render_job_id = _verify_video(
                connection, artifacts, self._storage, project
            )
            subtitles = _verify_subtitles(artifacts, self._storage, project)
            thumbnail, thumbnail_info = _verify_thumbnail(
                artifacts, self._storage, project
            )
            metadata_artifact, metadata = _verify_metadata(
                artifacts, self._storage, project
            )
            policy_report = self._evaluate_policy_report(
                connection, item, metadata, now
            )

        rights_report = self._rights_reporter.report(item.id)
        if rights_report.gate.blocks:
            codes = ", ".join(r.code for r in rights_report.gate.blocking_records)
            raise ShortsBundleError(
                f"rights gate blocked content item {item.id} ({codes})",
                code=ShortsBundleErrorCode.RIGHTS_BLOCKED,
            )
        if (
            rights_report.disclosure is None
            or rights_report.disclosure.decision_id != metadata.disclosure_decision_id
        ):
            raise ShortsBundleError(
                f"disclosure decision of content item {item.id} does not match "
                f"metadata disclosure_decision_id {metadata.disclosure_decision_id}",
                code=ShortsBundleErrorCode.DISCLOSURE_MISMATCH,
            )
        if policy_report.blocks:
            codes = ", ".join(f.rule_id for f in policy_report.blocking_findings)
            raise ShortsBundleError(
                f"policy report blocked content item {item.id} ({codes})",
                code=ShortsBundleErrorCode.POLICY_BLOCKED,
            )

        bundle = ShortsArtifactBundle(
            project=project,
            channel_id=item.channel_id,
            audio=audio,
            video=video,
            video_info=video_info,
            render_job_id=render_job_id,
            subtitles=subtitles,
            thumbnail=thumbnail,
            thumbnail_info=thumbnail_info,
            metadata_artifact=metadata_artifact,
            metadata=metadata,
            rights_report=rights_report,
            policy_report=policy_report,
            built_at=now,
        )
        self._audit.record(
            BUNDLE_BUILT,
            actor,
            EntityRef("shorts_project", project.id),
            AuditResult.SUCCESS,
            _event_metadata(bundle),
        )
        return bundle

    def _evaluate_policy_report(
        self,
        connection: sqlite3.Connection,
        item: ContentItem,
        metadata: ShortsMetadata,
        now: datetime,
    ) -> PolicyReport:
        strategy = StrategyProfileRepository(connection).get_by_channel(item.channel_id)
        if strategy is None:
            raise ShortsBundleError(
                f"channel {item.channel_id} has no strategy profile",
                code=ShortsBundleErrorCode.STALE_INPUT,
            )
        rule_set = RuleSet(
            metadata.policy_rule_set_id, metadata.policy_rule_set_version
        )
        checker = PolicyChecker(self._policy_reporter._catalog)
        policy_input = PolicyInput(
            content_item_id=item.id,
            channel_id=item.channel_id,
            title=metadata.title,
            description=metadata.description,
            tags=metadata.tags,
            banned_phrases=banned_phrases(strategy),
        )
        result = checker.check(policy_input, rule_set=rule_set)
        findings = tuple(FindingReport.from_finding(f) for f in result.to_findings())
        return PolicyReport(
            content_item_id=item.id,
            channel_id=item.channel_id,
            generated_at=now,
            rule_set=result.rule_set,
            status=result.status,
            blocks=result.status is PolicyStatus.BLOCK,
            outcomes=tuple(RuleOutcomeReport.from_outcome(o) for o in result.outcomes),
            findings=findings,
            blocking_findings=tuple(f for f in findings if f.blocking),
        )

    def _now(self) -> datetime:
        now = self._clock() if self._clock else datetime.now(UTC)
        if not isinstance(now, datetime) or now.utcoffset() != timedelta(0):
            raise ValueError("the clock must return a timezone-aware UTC time")
        return now


def _load_ready_project(
    connection: sqlite3.Connection,
    project_id: str,
    expected_version: int | None,
) -> tuple[ShortsProject, ContentItem]:
    project = ShortsProjectRepository(connection).get(project_id)
    if project is None:
        raise ShortsProjectNotFoundError(f"Shorts project {project_id} does not exist")
    if expected_version is not None and project.version != expected_version:
        raise ShortsBundleConflictError(
            f"Shorts project {project.id} is at version {project.version}, "
            f"expected {expected_version}"
        )
    item = ContentItemRepository(connection).get(project.content_item_id)
    if item is None or not project_is_current(connection, project):
        raise ShortsBundleError(
            f"Shorts project {project.id} is no longer current for content item "
            f"{project.content_item_id}",
            code=ShortsBundleErrorCode.STALE_INPUT,
        )
    if (
        project.status is not ShortsProjectStatus.READY
        or readiness(project.config, project.timeline) is not ShortsProjectStatus.READY
    ):
        raise ShortsBundleError(
            f"Shorts project {project.id} is in status {project.status.value}, "
            f"not ready",
            code=ShortsBundleErrorCode.PROJECT_NOT_READY,
        )
    return project, item


def _read_verified_bytes(storage: ArtifactStorage, artifact: Artifact) -> bytes:
    try:
        raw = storage.read(artifact.uri)
    except Exception as error:
        raise ShortsBundleError(
            f"artifact {artifact.id} ({artifact.kind.value}) could not be read: "
            f"{error}",
            code=ShortsBundleErrorCode.ARTIFACT_CORRUPTED,
        ) from error
    digest = hashlib.sha256(raw).hexdigest()
    if digest != artifact.sha256 or len(raw) != artifact.size_bytes:
        raise ShortsBundleError(
            f"artifact {artifact.id} ({artifact.kind.value}) on-disk bytes do not "
            f"match recorded sha256/size",
            code=ShortsBundleErrorCode.ARTIFACT_CORRUPTED,
        )
    return raw


def _verify_audio(
    artifacts: ArtifactRepository,
    storage: ArtifactStorage,
    project: ShortsProject,
) -> Artifact:
    audio = artifacts.latest(project.content_item_id, ArtifactKind.AUDIO)
    if (
        audio is None
        or audio.id != project.audio_artifact_id
        or audio.version != project.audio_version
        or audio.sha256 != project.audio_sha256
    ):
        raise ShortsBundleError(
            f"audio artifact of project {project.id} is missing or out of date",
            code=ShortsBundleErrorCode.STALE_INPUT,
        )
    _read_verified_bytes(storage, audio)
    return audio


def _verify_video(
    connection: sqlite3.Connection,
    artifacts: ArtifactRepository,
    storage: ArtifactStorage,
    project: ShortsProject,
) -> tuple[Artifact, Mp4Info, str]:
    video = artifacts.latest(project.content_item_id, ArtifactKind.VIDEO)
    if video is None:
        raise ShortsBundleError(
            f"Shorts project {project.id} has no VIDEO artifact",
            code=ShortsBundleErrorCode.VIDEO_MISSING,
        )
    if video.media_type != MP4_MEDIA_TYPE:
        raise ShortsBundleError(
            f"VIDEO artifact {video.id} has media_type {video.media_type!r}",
            code=ShortsBundleErrorCode.VIDEO_INVALID,
        )
    raw = _read_verified_bytes(storage, video)
    try:
        info = probe_mp4(raw)
    except ValueError as error:
        raise ShortsBundleError(
            f"VIDEO artifact {video.id} is not a valid MP4: {error}",
            code=ShortsBundleErrorCode.VIDEO_INVALID,
        ) from error
    tolerance = max(250, 2000 // project.config.fps)
    if abs(info.duration_ms - project.audio_duration_ms) > tolerance:
        raise ShortsBundleError(
            f"VIDEO artifact {video.id} duration {info.duration_ms} ms differs "
            f"from audio duration {project.audio_duration_ms} ms",
            code=ShortsBundleErrorCode.VIDEO_INVALID,
        )
    jobs = AIJobRepository(connection).list_by_content_item(project.content_item_id)
    matched_job = _find_render_job(jobs, video, project.timeline.sha256)
    if matched_job is None:
        raise ShortsBundleError(
            f"VIDEO artifact {video.id} was not produced by a succeeded render job "
            f"for the current timeline of project {project.id}",
            code=ShortsBundleErrorCode.VIDEO_STALE,
        )
    return video, info, matched_job.id


def _find_render_job(
    jobs: list[AIJob], video: Artifact, timeline_sha256: str
) -> AIJob | None:
    for job in reversed(jobs):
        if job.kind != RENDER_JOB_KIND or job.status is not AIJobStatus.SUCCEEDED:
            continue
        for cp in reversed(job.checkpoints):
            if cp.step == RENDERED_STEP:
                data = cp.data
                if (
                    data.get("artifact_id") == video.id
                    and data.get("sha256") == video.sha256
                    and data.get("timeline_sha256") == timeline_sha256
                ):
                    return job
                break
    return None


def _verify_subtitles(
    artifacts: ArtifactRepository,
    storage: ArtifactStorage,
    project: ShortsProject,
) -> Artifact | None:
    if not project.config.captions:
        return None
    subtitles = artifacts.latest(project.content_item_id, ArtifactKind.SUBTITLES)
    if subtitles is None:
        raise ShortsBundleError(
            f"Shorts project {project.id} has captions enabled "
            "but no SUBTITLES artifact",
            code=ShortsBundleErrorCode.SUBTITLES_MISSING,
        )
    if subtitles.media_type != SRT_MEDIA_TYPE:
        raise ShortsBundleError(
            f"SUBTITLES artifact {subtitles.id} has media_type "
            f"{subtitles.media_type!r}",
            code=ShortsBundleErrorCode.SUBTITLES_STALE,
        )
    raw = _read_verified_bytes(storage, subtitles)
    expected = render_srt(project.timeline, project.config.caption_rules).encode(
        "utf-8"
    )
    if raw != expected:
        raise ShortsBundleError(
            f"SUBTITLES artifact {subtitles.id} does not match the current captions "
            f"of project {project.id}",
            code=ShortsBundleErrorCode.SUBTITLES_STALE,
        )
    return subtitles


def _verify_thumbnail(
    artifacts: ArtifactRepository,
    storage: ArtifactStorage,
    project: ShortsProject,
) -> tuple[Artifact, ImageInfo]:
    thumbnail = artifacts.latest(project.content_item_id, ArtifactKind.THUMBNAIL)
    if thumbnail is None:
        raise ShortsBundleError(
            f"Shorts project {project.id} has no THUMBNAIL artifact",
            code=ShortsBundleErrorCode.THUMBNAIL_MISSING,
        )
    raw = _read_verified_bytes(storage, thumbnail)
    try:
        info = probe_image(raw)
    except Exception as error:
        raise ShortsBundleError(
            f"THUMBNAIL artifact {thumbnail.id} is invalid: {error}",
            code=ShortsBundleErrorCode.THUMBNAIL_INVALID,
        ) from error
    if info.media_type != thumbnail.media_type:
        raise ShortsBundleError(
            f"THUMBNAIL artifact {thumbnail.id} media_type {thumbnail.media_type!r} "
            f"disagrees with probed {info.media_type!r}",
            code=ShortsBundleErrorCode.THUMBNAIL_INVALID,
        )
    return thumbnail, info


def _verify_metadata(
    artifacts: ArtifactRepository,
    storage: ArtifactStorage,
    project: ShortsProject,
) -> tuple[Artifact, ShortsMetadata]:
    meta_artifact = artifacts.latest(project.content_item_id, ArtifactKind.METADATA)
    if meta_artifact is None:
        raise ShortsBundleError(
            f"Shorts project {project.id} has no METADATA artifact",
            code=ShortsBundleErrorCode.METADATA_MISSING,
        )
    if meta_artifact.media_type != METADATA_MEDIA_TYPE:
        raise ShortsBundleError(
            f"METADATA artifact {meta_artifact.id} has media_type "
            f"{meta_artifact.media_type!r}",
            code=ShortsBundleErrorCode.METADATA_INVALID,
        )
    raw = _read_verified_bytes(storage, meta_artifact)
    try:
        metadata = ShortsMetadata.from_json_bytes(raw)
    except (ValueError, TypeError) as error:
        raise ShortsBundleError(
            f"METADATA artifact {meta_artifact.id} is invalid: {error}",
            code=ShortsBundleErrorCode.METADATA_INVALID,
        ) from error
    if (
        metadata.project_id != project.id
        or metadata.project_version != project.version
        or metadata.content_item_id != project.content_item_id
        or metadata.script_id != project.script_id
        or metadata.script_version != project.script_version
    ):
        raise ShortsBundleError(
            f"METADATA artifact {meta_artifact.id} was recorded for project "
            f"{metadata.project_id} v{metadata.project_version}, expected "
            f"{project.id} v{project.version}",
            code=ShortsBundleErrorCode.METADATA_STALE,
        )
    return meta_artifact, metadata


def _artifact_summary(artifact: Artifact) -> dict[str, Any]:
    return {
        "id": artifact.id,
        "kind": artifact.kind.value,
        "version": artifact.version,
        "uri": artifact.uri,
        "sha256": artifact.sha256,
        "size_bytes": artifact.size_bytes,
        "media_type": artifact.media_type,
        "created_at": artifact.created_at.isoformat(),
    }


def _event_metadata(bundle: ShortsArtifactBundle) -> Mapping[str, MetadataValue]:
    event: dict[str, MetadataValue] = {
        "content_item_id": bundle.project.content_item_id,
        "channel_id": bundle.channel_id,
        "project_id": bundle.project.id,
        "project_version": bundle.project.version,
        "timeline_sha256": bundle.project.timeline.sha256,
        "render_job_id": bundle.render_job_id,
        "audio_artifact_id": bundle.audio.id,
        "video_artifact_id": bundle.video.id,
        "video_version": bundle.video.version,
        "thumbnail_artifact_id": bundle.thumbnail.id,
        "thumbnail_version": bundle.thumbnail.version,
        "metadata_artifact_id": bundle.metadata_artifact.id,
        "metadata_version": bundle.metadata_artifact.version,
        "rights_blocks": bundle.rights_report.gate.blocks,
        "policy_status": bundle.policy_report.status.value,
        "manifest_sha256": bundle.manifest_sha256,
    }
    if bundle.subtitles is not None:
        event["subtitles_artifact_id"] = bundle.subtitles.id
        event["subtitles_version"] = bundle.subtitles.version
    return event
