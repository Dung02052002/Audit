"""QC Test Harness (Prompt Pack v8, prompt K-116), context C10 Quality (Test & QC).

``VideoArtifactTestHarness`` provides a reusable, extensible inspection harness
for rendered video artifacts:
- Registers and runs discrete ``VideoCheck`` functions against a ``VideoInspectionContext``.
- Integrates the foundational video checks:
  - ``video.container`` (#117)
  - ``video.resolution`` (#118)
  - ``video.fps`` (#119)
  - ``video.duration`` (#120)
- Builds and records an immutable ``QCResult`` entity (with derived worst-of status)
  into ``QCResultRepository`` and audits ``qc.evaluated``.
"""

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from http import HTTPStatus
from typing import Any, Final

from ai_youtube_agent.content.longform_config import LongFormConfig
from ai_youtube_agent.content.qc import QCCheck, QCResult, QCStatus
from ai_youtube_agent.content.qc_video_checks import (
    CONTAINER_CHECK_NAME,
    DURATION_CHECK_NAME,
    FPS_CHECK_NAME,
    RESOLUTION_CHECK_NAME,
    VideoInspectionContext,
    check_container,
    check_duration,
    check_fps,
    check_resolution,
)
from ai_youtube_agent.content.shorts_config import ShortsConfig
from ai_youtube_agent.core.artifact import Artifact, ArtifactKind
from ai_youtube_agent.core.artifact_storage import ArtifactStorage
from ai_youtube_agent.core.audit import (
    Actor,
    AuditLog,
    AuditResult,
    EntityRef,
    MetadataValue,
)
from ai_youtube_agent.core.content_item import ContentItem, ContentType
from ai_youtube_agent.core.db.database import Database
from ai_youtube_agent.core.db.repositories.channel import StrategyProfileRepository
from ai_youtube_agent.core.db.repositories.content import (
    ArtifactRepository,
    AudioMetadataRepository,
    ContentItemRepository,
)
from ai_youtube_agent.core.db.repositories.longform import LongFormProjectRepository
from ai_youtube_agent.core.db.repositories.review import QCResultRepository
from ai_youtube_agent.core.db.repositories.shorts import ShortsProjectRepository
from ai_youtube_agent.core.errors import DomainError
from ai_youtube_agent.core.log import get_logger
from ai_youtube_agent.providers.video_render import probe_mp4

logger = get_logger(__name__)

QC_EVALUATED: Final[str] = "qc.evaluated"

Clock = Callable[[], datetime]
VideoCheck = Callable[[VideoInspectionContext], QCCheck]


class QCHarnessErrorCode(StrEnum):
    HARNESS = "domain.qc_harness"
    ITEM_NOT_FOUND = "domain.qc_item_not_found"
    VIDEO_MISSING = "domain.qc_video_missing"
    VIDEO_CORRUPTED = "domain.qc_video_corrupted"
    STRATEGY_MISSING = "domain.qc_strategy_missing"


USER_MESSAGES: Final[dict[QCHarnessErrorCode, str]] = {
    QCHarnessErrorCode.HARNESS: "The video QC inspection could not be completed.",
    QCHarnessErrorCode.ITEM_NOT_FOUND: "The content item does not exist.",
    QCHarnessErrorCode.VIDEO_MISSING: "No rendered video artifact exists for this item.",
    QCHarnessErrorCode.VIDEO_CORRUPTED: "The video artifact data could not be read.",
    QCHarnessErrorCode.STRATEGY_MISSING: (
        "The channel has no strategy profile format settings."
    ),
}


class QCHarnessError(DomainError):
    default_code = QCHarnessErrorCode.HARNESS.value
    default_user_message = USER_MESSAGES[QCHarnessErrorCode.HARNESS]
    default_http_status = HTTPStatus.UNPROCESSABLE_ENTITY

    def __init__(
        self,
        detail: str = "",
        *,
        code: QCHarnessErrorCode = QCHarnessErrorCode.HARNESS,
        http_status: int | None = None,
    ) -> None:
        status = (
            http_status
            if http_status is not None
            else (
                HTTPStatus.NOT_FOUND
                if code
                in (
                    QCHarnessErrorCode.ITEM_NOT_FOUND,
                    QCHarnessErrorCode.VIDEO_MISSING,
                )
                else HTTPStatus.UNPROCESSABLE_ENTITY
            )
        )
        super().__init__(
            detail,
            code=code.value,
            user_message=USER_MESSAGES.get(code, USER_MESSAGES[QCHarnessErrorCode.HARNESS]),
            http_status=status,
        )


def default_video_checks() -> dict[str, VideoCheck]:
    """Default check suite containing K-117 through K-120."""
    return {
        CONTAINER_CHECK_NAME: check_container,
        RESOLUTION_CHECK_NAME: check_resolution,
        FPS_CHECK_NAME: check_fps,
        DURATION_CHECK_NAME: check_duration,
    }


class VideoArtifactTestHarness:
    """Test harness for inspecting rendered video artifacts and recording QC results."""

    def __init__(
        self,
        database: Database,
        storage: ArtifactStorage,
        audit: AuditLog,
        checks: Mapping[str, VideoCheck] | None = None,
        *,
        clock: Clock | None = None,
    ) -> None:
        self._database = database
        self._storage = storage
        self._audit = audit
        self._checks: dict[str, VideoCheck] = (
            dict(checks) if checks is not None else default_video_checks()
        )
        self._clock = clock or (lambda: datetime.now(UTC))

    def register_check(self, name: str, check_fn: VideoCheck) -> None:
        """Register a new video QC inspection check."""
        self._checks[name] = check_fn

    def run_checks(
        self,
        context: VideoInspectionContext,
        check_names: Sequence[str] | None = None,
    ) -> list[QCCheck]:
        """Execute registered video checks against the inspection context."""
        selected_names = check_names if check_names is not None else list(self._checks.keys())
        results: list[QCCheck] = []
        for name in selected_names:
            if name not in self._checks:
                raise ValueError(f"unknown QC check {name!r}")
            check_fn = self._checks[name]
            results.append(check_fn(context))
        return results

    def inspect_and_record(
        self,
        content_item_id: str,
        *,
        video_artifact_id: str | None = None,
        actor: Actor,
        check_names: Sequence[str] | None = None,
        actual_width: int | None = None,
        actual_height: int | None = None,
        actual_fps: float | None = None,
    ) -> QCResult:
        """Inspect the video artifact, construct QCResult, store it, and audit."""
        with self._database.transaction() as connection:
            items = ContentItemRepository(connection)
            item = items.get(content_item_id)
            if item is None:
                raise QCHarnessError(
                    f"content item {content_item_id} not found",
                    code=QCHarnessErrorCode.ITEM_NOT_FOUND,
                )

            artifacts_repo = ArtifactRepository(connection)
            if video_artifact_id is not None:
                video = artifacts_repo.get(video_artifact_id)
            else:
                video = artifacts_repo.latest(item.id, ArtifactKind.VIDEO)

            if video is None:
                raise QCHarnessError(
                    f"video artifact not found for item {content_item_id}",
                    code=QCHarnessErrorCode.VIDEO_MISSING,
                )

            # Read audio duration for sync validation
            audio = artifacts_repo.latest(item.id, ArtifactKind.AUDIO)
            audio_duration_ms = 0
            if audio is not None:
                audio_meta = AudioMetadataRepository(connection).get_by_artifact(audio.id)
                if audio_meta is not None:
                    audio_duration_ms = audio_meta.duration_ms

            # Format targets from strategy
            strategies = StrategyProfileRepository(connection)
            strategy = strategies.get(item.channel_id)
            if strategy is None or strategy.format is None:
                raise QCHarnessError(
                    f"strategy format missing for channel {item.channel_id}",
                    code=QCHarnessErrorCode.STRATEGY_MISSING,
                )

            format_setting = strategy.format
            if item.content_type == ContentType.SHORTS:
                fmt = format_setting.shorts
                expected_width = 1080
                expected_height = 1920
                if fmt.resolution.value == "720p":
                    expected_width, expected_height = 720, 1280
                elif fmt.resolution.value == "2160p":
                    expected_width, expected_height = 2160, 3840
                min_duration_ms = fmt.min_seconds * 1000
                max_duration_ms = fmt.max_seconds * 1000
                expected_fps = 30
            else:
                fmt = format_setting.longform
                expected_width = 1920
                expected_height = 1080
                if fmt.resolution.value == "720p":
                    expected_width, expected_height = 1280, 720
                elif fmt.resolution.value == "2160p":
                    expected_width, expected_height = 3840, 2160
                min_duration_ms = fmt.min_seconds * 1000
                max_duration_ms = fmt.max_seconds * 1000
                expected_fps = 30

        # Read video file outside transaction
        try:
            video_data = self._storage.read(video.uri)
        except Exception as error:
            raise QCHarnessError(
                f"could not read video artifact {video.id}: {error}",
                code=QCHarnessErrorCode.VIDEO_CORRUPTED,
            ) from error

        context = VideoInspectionContext(
            content_item_id=item.id,
            content_type=item.content_type,
            video_data=video_data,
            expected_width=expected_width,
            expected_height=expected_height,
            expected_fps=expected_fps,
            expected_duration_ms=audio_duration_ms,
            min_duration_ms=min_duration_ms,
            max_duration_ms=max_duration_ms,
            actual_width=actual_width,
            actual_height=actual_height,
            actual_fps=actual_fps,
        )

        checks = self.run_checks(context, check_names=check_names)

        # Assemble and persist QCResult
        result = QCResult.create(
            content_item_id=item.id,
            artifacts=[video],
            checks=checks,
            clock=self._clock,
        )

        with self._database.transaction() as connection:
            QCResultRepository(connection).add(result)

        # Post-commit audit
        self._audit.record(
            QC_EVALUATED,
            actor,
            EntityRef("content_item", item.id),
            AuditResult.SUCCESS,
            {
                "qc_result_id": result.id,
                "overall_status": result.status.value,
                "checks_count": len(result.checks),
                "pass_count": sum(c.status == QCStatus.PASS for c in result.checks),
                "warn_count": sum(c.status == QCStatus.WARN for c in result.checks),
                "fail_count": sum(c.status == QCStatus.FAIL for c in result.checks),
                "video_artifact_id": video.id,
            },
        )

        return result
