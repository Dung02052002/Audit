"""Publish context loader (Prompt Pack v8, prompts #149, #150 and #152), context C12.

``PublishContextLoader`` gathers and verifies everything one publish needs, for
either content type, so the Shorts and LongForm publishers share one tested path:

- ``load`` reads the database only (one connection, nothing written): the content
  item (right ``ContentType``), its channel (``ACTIVE``), the newest approval request
  (must be ``APPROVED``), the artifacts that approval binds (``VIDEO`` and ``METADATA``
  are required, ``THUMBNAIL`` and ``SUBTITLES`` optional), the newest AI disclosure
  decision, the job already stored under the publish job key and the two
  deterministic keys (#151).
- ``read_media`` reads the bound files through ``ArtifactStorage``, checks size and
  sha256 against the artifact record, probes the MP4 and enforces the optional
  duration limit of the content type. A missing or changed file is
  ``STORAGE_CORRUPT``; nothing is uploaded from a file that is not the approved one.

Every refusal is a typed ``PublishError`` raised before any publish job exists.
"""

import hashlib
from dataclasses import dataclass
from http import HTTPStatus

from ai_youtube_agent.content.approval import ApprovalRequest, ApprovalStatus
from ai_youtube_agent.content.channel import Channel, ChannelStatus
from ai_youtube_agent.content.disclosure import DisclosureRecord
from ai_youtube_agent.core.artifact import Artifact, ArtifactKind
from ai_youtube_agent.core.artifact_storage import ArtifactStorage
from ai_youtube_agent.core.content_item import ContentItem, ContentType
from ai_youtube_agent.core.db.database import Database
from ai_youtube_agent.core.db.repositories.channel import ChannelRepository
from ai_youtube_agent.core.db.repositories.content import (
    ArtifactRepository,
    ContentItemRepository,
)
from ai_youtube_agent.core.db.repositories.disclosure import DisclosureRepository
from ai_youtube_agent.core.db.repositories.publish import PublishJobRepository
from ai_youtube_agent.core.db.repositories.review import ApprovalRequestRepository
from ai_youtube_agent.pipeline.idempotency import PublishKeys, derive_publish_keys
from ai_youtube_agent.pipeline.publish import PublishJob
from ai_youtube_agent.pipeline.publish_errors import PublishError, PublishErrorCode
from ai_youtube_agent.providers.video_render import probe_mp4
from ai_youtube_agent.providers.youtube_upload import (
    YouTubeUploadPayload,
    YouTubeUploadReceipt,
)


@dataclass(frozen=True)
class PublishContext:
    item: ContentItem
    channel: Channel
    approval: ApprovalRequest
    video_artifact: Artifact
    metadata_artifact: Artifact
    thumbnail_artifact: Artifact | None
    subtitles_artifact: Artifact | None
    disclosure_record: DisclosureRecord | None
    existing_job: PublishJob | None
    keys: PublishKeys


@dataclass(frozen=True)
class PublishMedia:
    video_bytes: bytes
    metadata_bytes: bytes
    thumbnail_bytes: bytes | None
    captions_bytes: bytes | None
    video_duration_ms: int


@dataclass(frozen=True)
class PublishOutcome:
    """Result of one publish call.

    ``replayed`` is true when the video was already published and nothing was
    uploaded: ``receipt`` and ``payload`` are then ``None`` and ``job`` carries the
    stored result."""

    job: PublishJob
    receipt: YouTubeUploadReceipt | None
    payload: YouTubeUploadPayload | None
    replayed: bool = False

    def as_dict(self) -> dict[str, object]:
        return {
            "job": self.job.as_dict(),
            "receipt": self.receipt.as_dict() if self.receipt else None,
            "payload": self.payload.as_dict() if self.payload else None,
            "replayed": self.replayed,
        }


class PublishContextLoader:
    def __init__(
        self,
        database: Database,
        storage: ArtifactStorage,
        *,
        content_type: ContentType,
        max_duration_ms: int | None = None,
    ) -> None:
        if not isinstance(content_type, ContentType):
            raise TypeError("content_type must be a ContentType")
        if max_duration_ms is not None and max_duration_ms < 1:
            raise ValueError("max_duration_ms must be positive")
        self._database = database
        self._storage = storage
        self._content_type = content_type
        self._max_duration_ms = max_duration_ms

    def load(self, content_item_id: str) -> PublishContext:
        connection = self._database.connect()
        try:
            items = ContentItemRepository(connection)
            artifacts = ArtifactRepository(connection)
            item = items.get(content_item_id)
            if item is None:
                raise PublishError(
                    PublishErrorCode.CONTENT_ITEM_NOT_FOUND,
                    f"content item {content_item_id} was not found",
                    http_status=HTTPStatus.NOT_FOUND,
                )
            if item.content_type is not self._content_type:
                raise PublishError(
                    PublishErrorCode.WRONG_CONTENT_TYPE,
                    f"content item {item.id} has type {item.content_type.value}, "
                    f"expected {self._content_type.value}",
                )
            channel = ChannelRepository(connection).get(item.channel_id)
            if channel is None:
                raise PublishError(
                    PublishErrorCode.CHANNEL_NOT_FOUND,
                    f"channel {item.channel_id} was not found",
                    http_status=HTTPStatus.NOT_FOUND,
                )
            if channel.status is not ChannelStatus.ACTIVE:
                raise PublishError(
                    PublishErrorCode.CHANNEL_NOT_ACTIVE,
                    f"channel {channel.id} is {channel.status.value}, not active",
                    http_status=HTTPStatus.CONFLICT,
                )
            requests = ApprovalRequestRepository(connection).list_by_content_item(
                item.id
            )
            if not requests:
                raise PublishError(
                    PublishErrorCode.NO_APPROVAL,
                    f"content item {item.id} has no approval requests",
                    http_status=HTTPStatus.CONFLICT,
                )
            approval = max(requests, key=lambda r: (r.created_at, r.id))
            if approval.status is not ApprovalStatus.APPROVED:
                raise PublishError(
                    PublishErrorCode.NOT_APPROVED,
                    f"newest approval request {approval.id} is "
                    f"{approval.status.value}, not approved",
                    http_status=HTTPStatus.CONFLICT,
                )
            video = _bound(artifacts, approval, ArtifactKind.VIDEO, required=True)
            metadata = _bound(artifacts, approval, ArtifactKind.METADATA, required=True)
            thumbnail = _bound(artifacts, approval, ArtifactKind.THUMBNAIL)
            subtitles = _bound(artifacts, approval, ArtifactKind.SUBTITLES)
            assert video is not None and metadata is not None
            keys = derive_publish_keys(
                item.id, approval.id, channel.youtube.channel_id, video.sha256
            )
            return PublishContext(
                item=item,
                channel=channel,
                approval=approval,
                video_artifact=video,
                metadata_artifact=metadata,
                thumbnail_artifact=thumbnail,
                subtitles_artifact=subtitles,
                disclosure_record=DisclosureRepository(connection).latest(item.id),
                existing_job=PublishJobRepository(connection).get_by_idempotency_key(
                    keys.job_key
                ),
                keys=keys,
            )
        finally:
            connection.close()

    def read_media(self, context: PublishContext) -> PublishMedia:
        video = self._read(context.video_artifact)
        metadata = self._read(context.metadata_artifact)
        thumbnail = (
            self._read(context.thumbnail_artifact)
            if context.thumbnail_artifact is not None
            else None
        )
        captions = (
            self._read(context.subtitles_artifact)
            if context.subtitles_artifact is not None
            else None
        )
        try:
            duration_ms = probe_mp4(video).duration_ms
        except ValueError as error:
            raise PublishError(
                PublishErrorCode.STORAGE_CORRUPT,
                f"video artifact {context.video_artifact.id} is not a valid MP4: "
                f"{error}",
            ) from error
        if self._max_duration_ms is not None and duration_ms > self._max_duration_ms:
            raise PublishError(
                PublishErrorCode.DURATION_EXCEEDED,
                f"video duration {duration_ms} ms is over the limit of "
                f"{self._max_duration_ms} ms for {self._content_type.value}",
            )
        return PublishMedia(video, metadata, thumbnail, captions, duration_ms)

    def _read(self, artifact: Artifact) -> bytes:
        try:
            data = self._storage.read(artifact.uri)
        except (OSError, ValueError) as error:
            raise PublishError(
                PublishErrorCode.STORAGE_CORRUPT,
                f"the stored file of {artifact.kind.value} artifact {artifact.id} "
                "cannot be read",
                http_status=HTTPStatus.CONFLICT,
            ) from error
        if (
            len(data) != artifact.size_bytes
            or hashlib.sha256(data).hexdigest() != artifact.sha256
        ):
            raise PublishError(
                PublishErrorCode.STORAGE_CORRUPT,
                f"the stored file of {artifact.kind.value} artifact {artifact.id} "
                "does not match its recorded size and sha256",
                http_status=HTTPStatus.CONFLICT,
            )
        return data


def _bound(
    artifacts: ArtifactRepository,
    approval: ApprovalRequest,
    kind: ArtifactKind,
    *,
    required: bool = False,
) -> Artifact | None:
    binding = approval.binding_for(kind)
    if binding is None:
        if required:
            raise PublishError(
                PublishErrorCode.ARTIFACT_MISSING,
                f"approval {approval.id} does not bind a {kind.value} artifact",
                http_status=HTTPStatus.CONFLICT,
            )
        return None
    artifact = artifacts.get(binding.artifact_id)
    if artifact is None:
        raise PublishError(
            PublishErrorCode.ARTIFACT_MISSING,
            f"bound {kind.value} artifact {binding.artifact_id} was not found",
            http_status=HTTPStatus.CONFLICT,
        )
    return artifact
