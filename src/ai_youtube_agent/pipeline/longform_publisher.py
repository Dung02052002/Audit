"""LongForm Publisher (Prompt Pack v8, prompt #150), context C12 Publishing.

``LongFormPublisher`` orchestrates publishing an approved LongForm ``ContentItem``:

- Refuses immediately with ``LongFormDisabledError`` when
  ``FeatureFlags.longform_enabled`` is ``False``, and with ``LongFormPublishError``
  (``PUBLISH_DISABLED``) when ``FeatureFlags.publish_enabled`` is ``False``.
- Verifies ``ContentItem`` is ``ContentType.LONGFORM``, its ``Channel`` is
  ``ChannelStatus.ACTIVE``, and its newest ``ApprovalRequest`` is
  ``ApprovalStatus.APPROVED``.
- Reads and verifies on-disk bytes and SHA-256 digests of the bound ``VIDEO``,
  ``METADATA`` (including embedded ``00:00`` chapters), and optional
  ``THUMBNAIL`` / ``SUBTITLES`` artifacts, probes MP4 duration, and maps
  metadata via ``PublishMetadataMapper`` (#148).
- Returns an idempotent replay outcome immediately when a ``PublishJob`` for
  ``publish_key(item.id, approval.id)`` has already ``SUCCEEDED``.
- Evaluates ``PublishGate.ensure_can_publish`` (G-078b) before creating/starting
  the ``PublishJob`` and transitioning ``ContentItem`` to ``PUBLISHING``.
- Acquires a scoped ``OAuthToken`` (#145), verifies channel access (#146) and
  long-upload eligibility when video duration exceeds 15 minutes, and uploads
  via ``YouTubeUploadProvider`` (#147).
- Records ``PublishResult`` and transitions ``ContentItem`` to ``PUBLISHED`` on
  success (auditing ``longform.published``), or records failure on ``PublishJob``
  and transitions ``ContentItem`` to ``FAILED`` on provider error (auditing
  ``longform.publish_failed``).
"""

import hashlib
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from http import HTTPStatus
from typing import Any, Final

from ai_youtube_agent.content.approval import ApprovalRequest, ApprovalStatus
from ai_youtube_agent.content.channel import Channel, ChannelStatus
from ai_youtube_agent.content.disclosure import DisclosureRecord
from ai_youtube_agent.content.longform_script_generator import (
    LongFormDisabledError,
)
from ai_youtube_agent.core.artifact import Artifact, ArtifactKind
from ai_youtube_agent.core.artifact_storage import (
    ArtifactNotFoundError,
    ArtifactStorage,
)
from ai_youtube_agent.core.audit import (
    Actor,
    AuditLog,
    AuditResult,
    EntityRef,
    MetadataValue,
)
from ai_youtube_agent.core.content_item import (
    ContentItem,
    ContentStatus,
    ContentType,
)
from ai_youtube_agent.core.db.database import Database
from ai_youtube_agent.core.db.repositories.channel import ChannelRepository
from ai_youtube_agent.core.db.repositories.content import (
    ArtifactRepository,
    ContentItemRepository,
)
from ai_youtube_agent.core.db.repositories.disclosure import DisclosureRepository
from ai_youtube_agent.core.db.repositories.publish import PublishJobRepository
from ai_youtube_agent.core.db.repositories.review import ApprovalRequestRepository
from ai_youtube_agent.core.errors import DomainError, ProviderError
from ai_youtube_agent.core.flags import FeatureFlags
from ai_youtube_agent.core.publish_gate import PublishGate
from ai_youtube_agent.pipeline.idempotency import publish_key
from ai_youtube_agent.pipeline.publish import PublishJob, PublishStatus
from ai_youtube_agent.pipeline.publish_metadata_mapper import (
    DEFAULT_YOUTUBE_CATEGORY_ID,
    PublishMetadataMapper,
)
from ai_youtube_agent.pipeline.shorts_publisher import REQUIRED_UPLOAD_SCOPES
from ai_youtube_agent.providers.video_render import Mp4ProbeError, probe_mp4
from ai_youtube_agent.providers.youtube_channel import YouTubeChannelProvider
from ai_youtube_agent.providers.youtube_oauth import YouTubeOAuthProvider
from ai_youtube_agent.providers.youtube_upload import (
    YouTubePrivacyStatus,
    YouTubeUploadPayload,
    YouTubeUploadProvider,
    YouTubeUploadReceipt,
)

Clock = Callable[[], datetime]

LONGFORM_PUBLISHED: Final[str] = "longform.published"
LONGFORM_PUBLISH_FAILED: Final[str] = "longform.publish_failed"
LONG_UPLOAD_THRESHOLD_MS: Final[int] = 15 * 60 * 1_000


class LongFormPublishErrorCode(StrEnum):
    PUBLISH_DISABLED = "domain.longform_publish_disabled"
    CONTENT_ITEM_NOT_FOUND = "domain.longform_publish_content_item_not_found"
    WRONG_CONTENT_TYPE = "domain.longform_publish_wrong_content_type"
    CHANNEL_NOT_FOUND = "domain.longform_publish_channel_not_found"
    CHANNEL_NOT_ACTIVE = "domain.longform_publish_channel_not_active"
    LONG_UPLOADS_DISALLOWED = "domain.longform_publish_long_uploads_disallowed"
    NO_APPROVAL = "domain.longform_publish_no_approval"
    NOT_APPROVED = "domain.longform_publish_not_approved"
    ARTIFACT_MISSING = "domain.longform_publish_artifact_missing"
    STORAGE_CORRUPT = "domain.longform_publish_storage_corrupt"


USER_MESSAGES: Final[dict[LongFormPublishErrorCode, str]] = {
    LongFormPublishErrorCode.PUBLISH_DISABLED: (
        "Publishing to YouTube is currently disabled."
    ),
    LongFormPublishErrorCode.CONTENT_ITEM_NOT_FOUND: (
        "The requested content item does not exist."
    ),
    LongFormPublishErrorCode.WRONG_CONTENT_TYPE: (
        "The LongForm publisher can only publish LongForm content items."
    ),
    LongFormPublishErrorCode.CHANNEL_NOT_FOUND: (
        "The channel for this content item does not exist."
    ),
    LongFormPublishErrorCode.CHANNEL_NOT_ACTIVE: (
        "The channel must be active before publishing."
    ),
    LongFormPublishErrorCode.LONG_UPLOADS_DISALLOWED: (
        "This YouTube channel is not eligible for uploads longer than 15 minutes."
    ),
    LongFormPublishErrorCode.NO_APPROVAL: (
        "This content item has no approval request."
    ),
    LongFormPublishErrorCode.NOT_APPROVED: (
        "The newest approval request for this content item is not approved."
    ),
    LongFormPublishErrorCode.ARTIFACT_MISSING: (
        "A required approved artifact for LongForm publishing is missing."
    ),
    LongFormPublishErrorCode.STORAGE_CORRUPT: (
        "An approved artifact file is missing or its digest does not match."
    ),
}


class LongFormPublishError(DomainError):
    default_code = LongFormPublishErrorCode.PUBLISH_DISABLED.value
    default_user_message = USER_MESSAGES[LongFormPublishErrorCode.PUBLISH_DISABLED]
    default_http_status = HTTPStatus.UNPROCESSABLE_ENTITY

    def __init__(
        self,
        code: LongFormPublishErrorCode,
        detail: str,
        *,
        http_status: HTTPStatus = HTTPStatus.UNPROCESSABLE_ENTITY,
    ) -> None:
        if not isinstance(code, LongFormPublishErrorCode):
            raise TypeError("code must be a LongFormPublishErrorCode")
        self.publish_code = code
        super().__init__(
            detail,
            code=code.value,
            user_message=USER_MESSAGES[code],
        )
        self.default_http_status = http_status


@dataclass(frozen=True)
class LongFormPublishOutcome:
    job: PublishJob
    receipt: YouTubeUploadReceipt | None
    payload: YouTubeUploadPayload
    replayed: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "job": self.job.as_dict(),
            "receipt": self.receipt.as_dict() if self.receipt else None,
            "payload": self.payload.as_dict(),
            "replayed": self.replayed,
        }


def _read_verified_bytes(storage: ArtifactStorage, artifact: Artifact) -> bytes:
    try:
        raw = storage.read_bytes(artifact.storage_uri)
    except ArtifactNotFoundError as exc:
        raise LongFormPublishError(
            LongFormPublishErrorCode.STORAGE_CORRUPT,
            f"stored file for {artifact.kind.value} artifact {artifact.id} is missing",
            http_status=HTTPStatus.CONFLICT,
        ) from exc
    digest = hashlib.sha256(raw).hexdigest()
    if len(raw) != artifact.size_bytes or digest != artifact.sha256:
        raise LongFormPublishError(
            LongFormPublishErrorCode.STORAGE_CORRUPT,
            f"stored file for {artifact.kind.value} artifact {artifact.id} failed sha256/size verification",
            http_status=HTTPStatus.CONFLICT,
        )
    return raw


class LongFormPublisher:
    """Publishes an approved LongForm ``ContentItem`` to YouTube (#150)."""

    def __init__(
        self,
        database: Database,
        storage: ArtifactStorage,
        audit_log: AuditLog,
        publish_gate: PublishGate,
        oauth_provider: YouTubeOAuthProvider,
        channel_provider: YouTubeChannelProvider,
        upload_provider: YouTubeUploadProvider,
        *,
        mapper: PublishMetadataMapper | None = None,
        flags: FeatureFlags | None = None,
        clock: Clock | None = None,
    ) -> None:
        self._database = database
        self._storage = storage
        self._audit = audit_log
        self._publish_gate = publish_gate
        self._oauth = oauth_provider
        self._channel_provider = channel_provider
        self._upload = upload_provider
        self._mapper = mapper or PublishMetadataMapper()
        self._flags = flags
        self._clock = clock

    def publish(
        self,
        content_item_id: str,
        *,
        actor: Actor,
        privacy_status: YouTubePrivacyStatus = YouTubePrivacyStatus.PRIVATE,
        category_id: str = DEFAULT_YOUTUBE_CATEGORY_ID,
        publish_at: datetime | None = None,
    ) -> LongFormPublishOutcome:
        if self._flags is not None:
            if not self._flags.longform_enabled:
                raise LongFormDisabledError(
                    "LongForm publishing is disabled because longform_enabled is False"
                )
            if not self._flags.publish_enabled:
                raise LongFormPublishError(
                    LongFormPublishErrorCode.PUBLISH_DISABLED,
                    "LongForm publishing is disabled because publish_enabled is False",
                    http_status=HTTPStatus.FORBIDDEN,
                )

        (
            item,
            channel,
            approval,
            video_artifact,
            metadata_artifact,
            thumbnail_artifact,
            subtitles_artifact,
            disclosure_record,
            existing_job,
        ) = self._load_context(content_item_id)

        video_bytes = _read_verified_bytes(self._storage, video_artifact)
        metadata_bytes = _read_verified_bytes(self._storage, metadata_artifact)
        thumbnail_bytes = (
            _read_verified_bytes(self._storage, thumbnail_artifact)
            if thumbnail_artifact is not None
            else None
        )
        captions_bytes = (
            _read_verified_bytes(self._storage, subtitles_artifact)
            if subtitles_artifact is not None
            else None
        )

        try:
            mp4_info = probe_mp4(video_bytes)
        except Mp4ProbeError as exc:
            raise LongFormPublishError(
                LongFormPublishErrorCode.STORAGE_CORRUPT,
                f"video artifact {video_artifact.id} is not a valid MP4: {exc}",
                http_status=HTTPStatus.UNPROCESSABLE_ENTITY,
            ) from exc

        key = publish_key(item.id, approval.id)
        payload = self._mapper.map_to_payload(
            channel=channel,
            content_type=ContentType.LONGFORM,
            approval=approval,
            video_artifact=video_artifact,
            metadata_artifact=metadata_artifact,
            metadata_source=metadata_bytes,
            thumbnail_artifact=thumbnail_artifact,
            subtitles_artifact=subtitles_artifact,
            disclosure_record=disclosure_record,
            privacy_status=privacy_status,
            category_id=category_id,
            publish_at=publish_at,
            idempotency_key=key,
        )

        if (
            existing_job is not None
            and existing_job.status is PublishStatus.SUCCEEDED
        ):
            return LongFormPublishOutcome(
                job=existing_job,
                receipt=None,
                payload=payload,
                replayed=True,
            )

        now = self._now()
        self._publish_gate.ensure_can_publish(item, actor=actor, at=now)

        token = self._oauth.get_token(
            channel.id, required_scopes=REQUIRED_UPLOAD_SCOPES
        )
        remote_channel = self._channel_provider.verify_channel_access(
            channel.id, channel.youtube.channel_id, token=token
        )
        if (
            mp4_info.duration_ms > LONG_UPLOAD_THRESHOLD_MS
            and not remote_channel.allows_long_uploads
        ):
            raise LongFormPublishError(
                LongFormPublishErrorCode.LONG_UPLOADS_DISALLOWED,
                f"channel {channel.youtube.channel_id} long_uploads_status is "
                f"{remote_channel.long_uploads_status!r}, cannot upload {mp4_info.duration_ms}ms video",
                http_status=HTTPStatus.CONFLICT,
            )

        with self._database.transaction() as conn:
            jobs = PublishJobRepository(conn)
            items = ContentItemRepository(conn)
            if existing_job is None:
                created_job = PublishJob.create(
                    approval,
                    ContentType.LONGFORM,
                    key,
                    clock=self._clock,
                )
                in_progress_job = created_job.start(clock=self._clock)
                jobs.add(in_progress_job)
            else:
                in_progress_job = existing_job.start(clock=self._clock)
                jobs.update(
                    in_progress_job,
                    expected_updated_at=existing_job.updated_at,
                )
            publishing_item = item.with_status(
                ContentStatus.PUBLISHING, clock=self._clock
            )
            items.update(publishing_item, expected_updated_at=item.updated_at)

        try:
            receipt = self._upload.upload(
                payload,
                video_bytes=video_bytes,
                thumbnail_bytes=thumbnail_bytes,
                captions_bytes=captions_bytes,
                token=token,
            )
        except ProviderError as err:
            with self._database.transaction() as conn:
                jobs = PublishJobRepository(conn)
                items = ContentItemRepository(conn)
                failed_job = in_progress_job.fail(err.code, clock=self._clock)
                jobs.update(
                    failed_job,
                    expected_updated_at=in_progress_job.updated_at,
                )
                failed_item = publishing_item.with_status(
                    ContentStatus.FAILED, clock=self._clock
                )
                items.update(
                    failed_item,
                    expected_updated_at=publishing_item.updated_at,
                )
            self._audit_event(
                action=LONGFORM_PUBLISH_FAILED,
                result=AuditResult.FAILURE,
                actor=actor,
                job=failed_job,
                item=failed_item,
                approval=approval,
                reason=err.code,
                extra={"retryable": err.retryable},
            )
            raise

        with self._database.transaction() as conn:
            jobs = PublishJobRepository(conn)
            items = ContentItemRepository(conn)
            succeeded_job = in_progress_job.succeed(
                receipt.youtube_video_id, clock=self._clock
            )
            jobs.update(
                succeeded_job,
                expected_updated_at=in_progress_job.updated_at,
            )
            published_item = publishing_item.with_status(
                ContentStatus.PUBLISHED, clock=self._clock
            )
            items.update(
                published_item,
                expected_updated_at=publishing_item.updated_at,
            )

        self._audit_event(
            action=LONGFORM_PUBLISHED,
            result=AuditResult.SUCCESS,
            actor=actor,
            job=succeeded_job,
            item=published_item,
            approval=approval,
            extra={
                "youtube_video_id": receipt.youtube_video_id,
                "privacy_status": receipt.privacy_status.value,
                "contains_synthetic_media": receipt.contains_synthetic_media,
                "video_sha256": receipt.video_sha256,
            },
        )
        return LongFormPublishOutcome(
            job=succeeded_job,
            receipt=receipt,
            payload=payload,
            replayed=False,
        )

    def _load_context(
        self, content_item_id: str
    ) -> tuple[
        ContentItem,
        Channel,
        ApprovalRequest,
        Artifact,
        Artifact,
        Artifact | None,
        Artifact | None,
        DisclosureRecord | None,
        PublishJob | None,
    ]:
        conn = self._database.connect()
        try:
            items = ContentItemRepository(conn)
            channels = ChannelRepository(conn)
            approvals = ApprovalRequestRepository(conn)
            artifacts = ArtifactRepository(conn)
            disclosures = DisclosureRepository(conn)
            jobs = PublishJobRepository(conn)

            item = items.get(content_item_id)
            if item is None:
                raise LongFormPublishError(
                    LongFormPublishErrorCode.CONTENT_ITEM_NOT_FOUND,
                    f"content item {content_item_id} was not found",
                    http_status=HTTPStatus.NOT_FOUND,
                )
            if item.content_type is not ContentType.LONGFORM:
                raise LongFormPublishError(
                    LongFormPublishErrorCode.WRONG_CONTENT_TYPE,
                    f"content item {content_item_id} has type {item.content_type.value}, expected longform",
                    http_status=HTTPStatus.UNPROCESSABLE_ENTITY,
                )

            channel = channels.get(item.channel_id)
            if channel is None:
                raise LongFormPublishError(
                    LongFormPublishErrorCode.CHANNEL_NOT_FOUND,
                    f"channel {item.channel_id} was not found",
                    http_status=HTTPStatus.NOT_FOUND,
                )
            if channel.status is not ChannelStatus.ACTIVE:
                raise LongFormPublishError(
                    LongFormPublishErrorCode.CHANNEL_NOT_ACTIVE,
                    f"channel {channel.id} is {channel.status.value}, not active",
                    http_status=HTTPStatus.CONFLICT,
                )

            requests = approvals.list_by_content_item(content_item_id)
            if not requests:
                raise LongFormPublishError(
                    LongFormPublishErrorCode.NO_APPROVAL,
                    f"content item {content_item_id} has no approval requests",
                    http_status=HTTPStatus.CONFLICT,
                )
            newest = max(requests, key=lambda r: (r.created_at, r.id))
            if newest.status is not ApprovalStatus.APPROVED:
                raise LongFormPublishError(
                    LongFormPublishErrorCode.NOT_APPROVED,
                    f"newest approval request {newest.id} is {newest.status.value}, not approved",
                    http_status=HTTPStatus.CONFLICT,
                )

            video_binding = newest.binding_for(ArtifactKind.VIDEO)
            if video_binding is None:
                raise LongFormPublishError(
                    LongFormPublishErrorCode.ARTIFACT_MISSING,
                    f"approval {newest.id} does not bind a VIDEO artifact",
                    http_status=HTTPStatus.CONFLICT,
                )
            video_artifact = artifacts.get(video_binding.artifact_id)
            if video_artifact is None:
                raise LongFormPublishError(
                    LongFormPublishErrorCode.ARTIFACT_MISSING,
                    f"bound VIDEO artifact {video_binding.artifact_id} was not found",
                    http_status=HTTPStatus.CONFLICT,
                )

            metadata_binding = newest.binding_for(ArtifactKind.METADATA)
            if metadata_binding is None:
                raise LongFormPublishError(
                    LongFormPublishErrorCode.ARTIFACT_MISSING,
                    f"approval {newest.id} does not bind a METADATA artifact",
                    http_status=HTTPStatus.CONFLICT,
                )
            metadata_artifact = artifacts.get(metadata_binding.artifact_id)
            if metadata_artifact is None:
                raise LongFormPublishError(
                    LongFormPublishErrorCode.ARTIFACT_MISSING,
                    f"bound METADATA artifact {metadata_binding.artifact_id} was not found",
                    http_status=HTTPStatus.CONFLICT,
                )

            thumbnail_artifact: Artifact | None = None
            thumb_binding = newest.binding_for(ArtifactKind.THUMBNAIL)
            if thumb_binding is not None:
                thumbnail_artifact = artifacts.get(thumb_binding.artifact_id)
                if thumbnail_artifact is None:
                    raise LongFormPublishError(
                        LongFormPublishErrorCode.ARTIFACT_MISSING,
                        f"bound THUMBNAIL artifact {thumb_binding.artifact_id} was not found",
                        http_status=HTTPStatus.CONFLICT,
                    )

            subtitles_artifact: Artifact | None = None
            sub_binding = newest.binding_for(ArtifactKind.SUBTITLES)
            if sub_binding is not None:
                subtitles_artifact = artifacts.get(sub_binding.artifact_id)
                if subtitles_artifact is None:
                    raise LongFormPublishError(
                        LongFormPublishErrorCode.ARTIFACT_MISSING,
                        f"bound SUBTITLES artifact {sub_binding.artifact_id} was not found",
                        http_status=HTTPStatus.CONFLICT,
                    )

            disclosure_record = disclosures.latest(content_item_id)
            existing_job = jobs.get_by_idempotency_key(
                publish_key(item.id, newest.id)
            )
            return (
                item,
                channel,
                newest,
                video_artifact,
                metadata_artifact,
                thumbnail_artifact,
                subtitles_artifact,
                disclosure_record,
                existing_job,
            )
        finally:
            conn.close()

    def _audit_event(
        self,
        *,
        action: str,
        result: AuditResult,
        actor: Actor,
        job: PublishJob,
        item: ContentItem,
        approval: ApprovalRequest,
        reason: str | None = None,
        extra: dict[str, MetadataValue] | None = None,
    ) -> None:
        metadata: dict[str, MetadataValue] = {
            "content_item_id": item.id,
            "channel_id": item.channel_id,
            "content_type": item.content_type.value,
            "approval_request_id": approval.id,
            "idempotency_key": job.idempotency_key,
            "attempts": job.attempts,
            "status": job.status.value,
        }
        if extra:
            metadata.update(extra)
        self._audit.record(
            action=action,
            result=result,
            actor=actor,
            entity=EntityRef(kind="publish_job", id=job.id),
            reason=reason,
            metadata=metadata,
        )

    def _now(self) -> datetime:
        return self._clock() if self._clock else datetime.now(UTC)
