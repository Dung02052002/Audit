"""LongForm Artifact Bundle (Prompt Pack v8, prompt J-114), context C9 LongForm Production.

``LongFormBundleBuilder`` packages and cross-validates the complete deliverable
bundle for a ``LongFormProject`` (#105):

- Rendered MP4 ``VIDEO`` artifact (#111): verified against the storage file,
  ``probe_mp4`` (duration within frame tolerance of ``project.audio_duration_ms``)
  and the succeeded ``video.render`` ``AIJob`` whose ``video.rendered``
  checkpoint matches ``artifact.id``, ``artifact.sha256`` and
  ``project.timeline.sha256``.
- Timed ``SUBTITLES`` artifact (#109): required when
  ``project.config.captions`` is ``True`` (and ``None`` when ``False``),
  verified against storage bytes.
- Horizontal 16:9 ``THUMBNAIL`` artifact (#112): verified against storage bytes and
  ``probe_longform_image``.
- Canonical JSON ``METADATA`` artifact (#113): verified against storage bytes,
  parsed into ``LongFormMetadata`` and checked for binding to the current
  ``project.id``, ``project.version``, ``project.script_id`` and
  ``project.script_version``.
- Bound ``AUDIO`` artifact (#089): verified to match ``project.audio_artifact_id``,
  ``project.audio_version`` and ``project.audio_sha256``.
- ``RightsReport`` (#082) and ``PolicyReport`` (#083): evaluated on demand;
  any blocking rights verdict, missing or mismatched AI disclosure decision, or
  blocking policy verdict refuses the bundle.
- Audits ``longform.bundle_built`` with scalar metadata and ``manifest_sha256``.
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

from ai_youtube_agent.content.longform_metadata import (
    METADATA_MEDIA_TYPE,
    LongFormMetadata,
)
from ai_youtube_agent.content.longform_project import (
    LongFormProject,
    LongFormProjectStatus,
)
from ai_youtube_agent.content.longform_render import (
    JOB_KIND as RENDER_JOB_KIND,
    MP4_MEDIA_TYPE,
    RENDERED_STEP,
)
from ai_youtube_agent.content.longform_script_generator import LongFormDisabledError
from ai_youtube_agent.content.longform_service import (
    LongFormProjectNotFoundError,
    longform_project_is_current,
    longform_readiness,
)
from ai_youtube_agent.content.longform_thumbnail import (
    ImageInfo,
    probe_longform_image,
)
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
from ai_youtube_agent.core.config import Settings
from ai_youtube_agent.core.content_item import ContentItem
from ai_youtube_agent.core.db.database import Database
from ai_youtube_agent.core.db.repositories.channel import StrategyProfileRepository
from ai_youtube_agent.core.db.repositories.content import (
    ArtifactRepository,
    ContentItemRepository,
)
from ai_youtube_agent.core.db.repositories.jobs import AIJobRepository
from ai_youtube_agent.core.db.repositories.longform import LongFormProjectRepository
from ai_youtube_agent.core.errors import DomainError
from ai_youtube_agent.core.flags import FeatureFlags
from ai_youtube_agent.pipeline.job import AIJob, AIJobStatus
from ai_youtube_agent.providers.video_render import Mp4Info, probe_mp4

BUNDLE_BUILT: Final[str] = "longform.bundle_built"
BUNDLE_SCHEMA_VERSION: Final[str] = "longform-bundle-v1"

Clock = Callable[[], datetime]


class LongFormBundleErrorCode(StrEnum):
    BUNDLE = "domain.longform_bundle"
    PROJECT_NOT_READY = "domain.longform_bundle_project_not_ready"
    STALE_INPUT = "domain.longform_bundle_stale_input"
    VIDEO_MISSING = "domain.longform_bundle_video_missing"
    VIDEO_STALE = "domain.longform_bundle_video_stale"
    VIDEO_INVALID = "domain.longform_bundle_video_invalid"
    SUBTITLES_MISSING = "domain.longform_bundle_subtitles_missing"
    SUBTITLES_STALE = "domain.longform_bundle_subtitles_stale"
    THUMBNAIL_MISSING = "domain.longform_bundle_thumbnail_missing"
    THUMBNAIL_INVALID = "domain.longform_bundle_thumbnail_invalid"
    METADATA_MISSING = "domain.longform_bundle_metadata_missing"
    METADATA_STALE = "domain.longform_bundle_metadata_stale"
    METADATA_INVALID = "domain.longform_bundle_metadata_invalid"
    ARTIFACT_CORRUPTED = "domain.longform_bundle_artifact_corrupted"
    RIGHTS_BLOCKED = "domain.longform_bundle_rights_blocked"
    DISCLOSURE_MISMATCH = "domain.longform_bundle_disclosure_mismatch"
    POLICY_BLOCKED = "domain.longform_bundle_policy_blocked"


USER_MESSAGES: Final[dict[LongFormBundleErrorCode, str]] = {
    LongFormBundleErrorCode.BUNDLE: (
        "The LongForm artifact bundle could not be assembled."
    ),
    LongFormBundleErrorCode.PROJECT_NOT_READY: (
        "This LongForm project is not ready to be bundled."
    ),
    LongFormBundleErrorCode.STALE_INPUT: (
        "The LongForm project is out of date with its content item, script or audio."
    ),
    LongFormBundleErrorCode.VIDEO_MISSING: (
        "The rendered MP4 video artifact is missing."
    ),
    LongFormBundleErrorCode.VIDEO_STALE: (
        "The rendered MP4 video does not match the current LongForm timeline."
    ),
    LongFormBundleErrorCode.VIDEO_INVALID: (
        "The rendered MP4 video file is invalid."
    ),
    LongFormBundleErrorCode.SUBTITLES_MISSING: (
        "The subtitles artifact is missing."
    ),
    LongFormBundleErrorCode.SUBTITLES_STALE: (
        "The subtitles artifact does not match the current LongForm captions."
    ),
    LongFormBundleErrorCode.THUMBNAIL_MISSING: (
        "The LongForm thumbnail artifact is missing."
    ),
    LongFormBundleErrorCode.THUMBNAIL_INVALID: (
        "The LongForm thumbnail file is invalid."
    ),
    LongFormBundleErrorCode.METADATA_MISSING: (
        "The LongForm metadata artifact is missing."
    ),
    LongFormBundleErrorCode.METADATA_STALE: (
        "The LongForm metadata artifact belongs to an older project version."
    ),
    LongFormBundleErrorCode.METADATA_INVALID: (
        "The LongForm metadata file is invalid."
    ),
    LongFormBundleErrorCode.ARTIFACT_CORRUPTED: (
        "An artifact file on disk is missing or does not match its recorded checksum."
    ),
    LongFormBundleErrorCode.RIGHTS_BLOCKED: (
        "The rights gate blocked this LongForm bundle."
    ),
    LongFormBundleErrorCode.DISCLOSURE_MISMATCH: (
        "The AI disclosure decision is missing or out of date with the metadata."
    ),
    LongFormBundleErrorCode.POLICY_BLOCKED: (
        "The policy check blocked this LongForm bundle."
    ),
}


class LongFormBundleError(DomainError):
    default_code = LongFormBundleErrorCode.BUNDLE.value
    default_user_message = USER_MESSAGES[LongFormBundleErrorCode.BUNDLE]
    default_http_status = HTTPStatus.UNPROCESSABLE_ENTITY

    def __init__(
        self,
        detail: str = "",
        *,
        code: LongFormBundleErrorCode = LongFormBundleErrorCode.BUNDLE,
        http_status: int | None = None,
    ) -> None:
        status = (
            http_status
            if http_status is not None
            else (
                HTTPStatus.CONFLICT
                if code
                in (
                    LongFormBundleErrorCode.PROJECT_NOT_READY,
                    LongFormBundleErrorCode.STALE_INPUT,
                    LongFormBundleErrorCode.VIDEO_MISSING,
                    LongFormBundleErrorCode.VIDEO_STALE,
                    LongFormBundleErrorCode.SUBTITLES_MISSING,
                    LongFormBundleErrorCode.SUBTITLES_STALE,
                    LongFormBundleErrorCode.THUMBNAIL_MISSING,
                    LongFormBundleErrorCode.METADATA_MISSING,
                    LongFormBundleErrorCode.METADATA_STALE,
                    LongFormBundleErrorCode.RIGHTS_BLOCKED,
                    LongFormBundleErrorCode.DISCLOSURE_MISMATCH,
                    LongFormBundleErrorCode.POLICY_BLOCKED,
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


@dataclass(frozen=True)
class LongFormArtifactBundle:
    manifest_sha256: str
    project: LongFormProject
    audio: Artifact
    video: Artifact
    subtitles: Artifact | None
    thumbnail: Artifact
    metadata: Artifact
    rights_report: RightsReport
    policy_report: PolicyReport
    manifest: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return self.manifest


class LongFormBundleBuilder:
    """Assembles and verifies deliverable artifact packages for LongForm projects."""

    def __init__(
        self,
        database: Database,
        storage: ArtifactStorage,
        audit: AuditLog,
        settings: Settings,
        rights_reporter: RightsReporter | None = None,
        policy_reporter: PolicyReporter | None = None,
        flags: FeatureFlags | None = None,
        *,
        clock: Clock | None = None,
    ) -> None:
        self._database = database
        self._storage = storage
        self._audit = audit
        self._settings = settings
        self._rights_reporter = rights_reporter or RightsReporter(
            database, settings, clock=clock
        )
        self._policy_reporter = policy_reporter or PolicyReporter(
            database, clock=clock
        )
        self._flags = (
            flags if flags is not None else FeatureFlags(longform_enabled=True)
        )
        self._clock = clock or (lambda: datetime.now(UTC))

    def _require_enabled(self) -> None:
        if not self._flags.longform_enabled:
            raise LongFormDisabledError("LONGFORM_ENABLED is off")

    def build(
        self,
        project_id: str,
        *,
        expected_version: int,
        actor: Actor,
    ) -> LongFormArtifactBundle:
        """Verify all constituent artifacts and reports, and assemble the bundle."""
        self._require_enabled()

        with self._database.transaction() as connection:
            projects = LongFormProjectRepository(connection)
            project = projects.get(project_id)
            if project is None:
                raise LongFormBundleError(
                    f"project {project_id} not found",
                    code=LongFormBundleErrorCode.PROJECT_NOT_READY,
                    http_status=HTTPStatus.NOT_FOUND,
                )
            if project.version != expected_version:
                raise LongFormBundleError(
                    f"project version {project.version} != {expected_version}",
                    code=LongFormBundleErrorCode.STALE_INPUT,
                )
            if not longform_project_is_current(connection, project):
                raise LongFormBundleError(
                    f"project {project_id} is stale",
                    code=LongFormBundleErrorCode.STALE_INPUT,
                )
            if longform_readiness(project.config, project.timeline) != LongFormProjectStatus.READY:
                raise LongFormBundleError(
                    f"project {project_id} is not in READY status",
                    code=LongFormBundleErrorCode.PROJECT_NOT_READY,
                )

            item = ContentItemRepository(connection).get(project.content_item_id)
            if item is None:
                raise LongFormBundleError(
                    f"item {project.content_item_id} not found",
                    code=LongFormBundleErrorCode.PROJECT_NOT_READY,
                    http_status=HTTPStatus.NOT_FOUND,
                )

            artifacts_repo = ArtifactRepository(connection)
            audio = artifacts_repo.get(project.audio_artifact_id)
            if audio is None:
                raise LongFormBundleError(
                    f"bound audio artifact {project.audio_artifact_id} not found",
                    code=LongFormBundleErrorCode.ARTIFACT_CORRUPTED,
                )

            video = artifacts_repo.latest(item.id, ArtifactKind.VIDEO)
            if video is None:
                raise LongFormBundleError(
                    "video artifact is missing",
                    code=LongFormBundleErrorCode.VIDEO_MISSING,
                )

            subtitles = artifacts_repo.latest(item.id, ArtifactKind.SUBTITLES)
            if project.config.captions and subtitles is None:
                raise LongFormBundleError(
                    "subtitles artifact is missing but captions are enabled",
                    code=LongFormBundleErrorCode.SUBTITLES_MISSING,
                )

            thumbnail = artifacts_repo.latest(item.id, ArtifactKind.THUMBNAIL)
            if thumbnail is None:
                raise LongFormBundleError(
                    "thumbnail artifact is missing",
                    code=LongFormBundleErrorCode.THUMBNAIL_MISSING,
                )

            metadata_art = artifacts_repo.latest(item.id, ArtifactKind.METADATA)
            if metadata_art is None:
                raise LongFormBundleError(
                    "metadata artifact is missing",
                    code=LongFormBundleErrorCode.METADATA_MISSING,
                )

            # Succeeded video.render AIJob verification
            jobs = AIJobRepository(connection)
            all_jobs = jobs.list_by_content_item(item.id)
            render_jobs = [
                j
                for j in all_jobs
                if j.kind == RENDER_JOB_KIND and j.status == AIJobStatus.SUCCEEDED
            ]
            matching_job: AIJob | None = None
            for rj in render_jobs:
                for cp in rj.checkpoints:
                    if (
                        cp.step == RENDERED_STEP
                        and cp.data.get("artifact_id") == video.id
                        and cp.data.get("sha256") == video.sha256
                        and cp.data.get("timeline_sha256") == project.timeline.sha256
                    ):
                        matching_job = rj
                        break
                if matching_job is not None:
                    break

            if matching_job is None:
                raise LongFormBundleError(
                    "latest video artifact does not match a succeeded render job "
                    "for the current timeline",
                    code=LongFormBundleErrorCode.VIDEO_STALE,
                )

        # File integrity checks
        self._verify_file(audio)
        video_bytes = self._verify_file(video)
        try:
            mp4_info = probe_mp4(video_bytes)
        except Exception as error:
            raise LongFormBundleError(
                f"video artifact is not valid MP4: {error}",
                code=LongFormBundleErrorCode.VIDEO_INVALID,
            ) from error

        tolerance_ms = max(250, 2000 // project.config.fps)
        if abs(mp4_info.duration_ms - project.audio_duration_ms) > tolerance_ms:
            raise LongFormBundleError(
                f"video duration {mp4_info.duration_ms} ms differs from audio "
                f"{project.audio_duration_ms} ms",
                code=LongFormBundleErrorCode.VIDEO_INVALID,
            )

        if subtitles is not None:
            self._verify_file(subtitles)

        thumb_bytes = self._verify_file(thumbnail)
        try:
            probe_longform_image(thumb_bytes)
        except Exception as error:
            raise LongFormBundleError(
                f"thumbnail artifact is invalid: {error}",
                code=LongFormBundleErrorCode.THUMBNAIL_INVALID,
            ) from error

        meta_bytes = self._verify_file(metadata_art)
        try:
            parsed_meta = LongFormMetadata.from_bytes(meta_bytes)
        except Exception as error:
            raise LongFormBundleError(
                f"metadata artifact is invalid: {error}",
                code=LongFormBundleErrorCode.METADATA_INVALID,
            ) from error

        if (
            parsed_meta.project_id != project.id
            or parsed_meta.project_version != project.version
            or parsed_meta.script_id != project.script_id
            or parsed_meta.script_version != project.script_version
        ):
            raise LongFormBundleError(
                "metadata artifact bindings do not match the current project",
                code=LongFormBundleErrorCode.METADATA_STALE,
            )

        # Evaluate RightsReport and PolicyReport
        rights_report = self._rights_reporter.report(item.id)
        if rights_report.gate.blocks:
            raise LongFormBundleError(
                "rights gate blocks this LongForm bundle",
                code=LongFormBundleErrorCode.RIGHTS_BLOCKED,
            )

        if (
            rights_report.disclosure is None
            or rights_report.disclosure.decision_id
            != parsed_meta.disclosure_decision_id
        ):
            raise LongFormBundleError(
                "AI disclosure decision drifted from metadata",
                code=LongFormBundleErrorCode.DISCLOSURE_MISMATCH,
            )

        policy_report = self._policy_reporter.report(
            item.id,
            rule_set=RuleSet(
                parsed_meta.policy_rule_set_id,
                parsed_meta.policy_rule_set_version,
            ),
            description=parsed_meta.description,
            tags=parsed_meta.tags,
        )
        if policy_report.blocks:
            raise LongFormBundleError(
                "policy report blocks this LongForm bundle",
                code=LongFormBundleErrorCode.POLICY_BLOCKED,
            )

        # Assemble manifest
        manifest: dict[str, Any] = {
            "schema_version": BUNDLE_SCHEMA_VERSION,
            "project_id": project.id,
            "project_version": project.version,
            "content_item_id": item.id,
            "channel_id": item.channel_id,
            "artifacts": {
                "audio": {"id": audio.id, "version": audio.version, "sha256": audio.sha256},
                "video": {"id": video.id, "version": video.version, "sha256": video.sha256},
                "subtitles": (
                    {"id": subtitles.id, "version": subtitles.version, "sha256": subtitles.sha256}
                    if subtitles
                    else None
                ),
                "thumbnail": {
                    "id": thumbnail.id,
                    "version": thumbnail.version,
                    "sha256": thumbnail.sha256,
                },
                "metadata": {
                    "id": metadata_art.id,
                    "version": metadata_art.version,
                    "sha256": metadata_art.sha256,
                },
            },
            "video_info": {
                "duration_ms": mp4_info.duration_ms,
                "timescale": mp4_info.timescale,
                "major_brand": mp4_info.major_brand,
            },
            "reports": {
                "rights_gate_blocks": rights_report.gate.blocks,
                "policy_blocks": policy_report.blocks,
                "disclosure_decision_id": parsed_meta.disclosure_decision_id,
            },
        }

        manifest_sha256 = hashlib.sha256(
            json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()

        # Audit
        self._audit.record(
            BUNDLE_BUILT,
            actor,
            EntityRef("longform_project", project.id),
            AuditResult.SUCCESS,
            {
                "content_item_id": item.id,
                "project_version": project.version,
                "manifest_sha256": manifest_sha256,
                "video_artifact_id": video.id,
                "thumbnail_artifact_id": thumbnail.id,
                "metadata_artifact_id": metadata_art.id,
                "subtitles_artifact_id": subtitles.id if subtitles else None,
            },
        )

        return LongFormArtifactBundle(
            manifest_sha256=manifest_sha256,
            project=project,
            audio=audio,
            video=video,
            subtitles=subtitles,
            thumbnail=thumbnail,
            metadata=metadata_art,
            rights_report=rights_report,
            policy_report=policy_report,
            manifest=manifest,
        )

    def _verify_file(self, artifact: Artifact) -> bytes:
        try:
            data = self._storage.read(artifact.uri)
        except Exception as error:
            raise LongFormBundleError(
                f"artifact file {artifact.uri} could not be read: {error}",
                code=LongFormBundleErrorCode.ARTIFACT_CORRUPTED,
            ) from error
        if len(data) != artifact.size_bytes or hashlib.sha256(data).hexdigest() != artifact.sha256:
            raise LongFormBundleError(
                f"artifact {artifact.id} data corrupted or size mismatch",
                code=LongFormBundleErrorCode.ARTIFACT_CORRUPTED,
            )
        return data
