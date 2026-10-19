"""Shorts Publisher (Prompt Pack v8, prompt #149), context C12 Publishing.

``ShortsPublisher`` publishes an approved Shorts ``ContentItem``. It is a thin
orchestration over shared parts, in this order:

1. ``FeatureFlags``: ``shorts_enabled`` and ``publish_enabled`` must be on
   (``publish_enabled`` is off by default, R-08).
2. ``PublishContextLoader.load``: Shorts item, active channel, newest approval is
   ``APPROVED``, bound ``VIDEO`` and ``METADATA`` (and optional ``THUMBNAIL`` and
   ``SUBTITLES``) artifacts, the two deterministic keys (#151).
3. ``PublishDuplicateGuard`` (#152): a succeeded job replays, a running one and any
   duplicate of the item or of the video bytes are refused.
4. ``PublishContextLoader.read_media``: the files on disk match the approved
   size and sha256, the MP4 probes and is not longer than ``SHORTS_MAX_DURATION_MS``.
5. ``PublishMetadataMapper`` (#148): the validated ``YouTubeUploadPayload``, with the
   upload key of #151 as its idempotency key and ``#Shorts`` enforced.
6. ``PublishExecutor``: gates, authorisation, job and item states, upload, result
   and audit (``shorts.published`` / ``shorts.publish_failed``).

Nothing is uploaded to a real YouTube account unless the composition root wires a
real provider; ``bootstrap`` wires the mocks.
"""

from datetime import datetime
from http import HTTPStatus
from typing import Final

from ai_youtube_agent.core.artifact_storage import ArtifactStorage
from ai_youtube_agent.core.audit import Actor
from ai_youtube_agent.core.content_item import ContentType
from ai_youtube_agent.core.db.database import Database
from ai_youtube_agent.core.flags import FeatureFlags
from ai_youtube_agent.pipeline.duplicate_guard import (
    DuplicateDecision,
    DuplicatePublishError,
    PublishDuplicateGuard,
)
from ai_youtube_agent.pipeline.publish_context import (
    PublishContextLoader,
    PublishOutcome,
)
from ai_youtube_agent.pipeline.publish_errors import PublishError, PublishErrorCode
from ai_youtube_agent.pipeline.publish_executor import PublishExecutor, PublishRequest
from ai_youtube_agent.pipeline.publish_metadata_mapper import (
    DEFAULT_YOUTUBE_CATEGORY_ID,
    PublishMetadataMapper,
)
from ai_youtube_agent.providers.youtube_upload import YouTubePrivacyStatus

SHORTS_PUBLISHED: Final[str] = "shorts.published"
SHORTS_PUBLISH_FAILED: Final[str] = "shorts.publish_failed"
SHORTS_AUDIT_PREFIX: Final[str] = "shorts"
SHORTS_MAX_DURATION_MS: Final[int] = 180_000
"""Shorts format maximum (``ShortsFormat``: 1 to 180 seconds)."""


class ShortsPublisher:
    def __init__(
        self,
        database: Database,
        storage: ArtifactStorage,
        executor: PublishExecutor,
        *,
        mapper: PublishMetadataMapper | None = None,
        guard: PublishDuplicateGuard | None = None,
        flags: FeatureFlags | None = None,
    ) -> None:
        self._loader = PublishContextLoader(
            database,
            storage,
            content_type=ContentType.SHORTS,
            max_duration_ms=SHORTS_MAX_DURATION_MS,
        )
        self._guard = guard or PublishDuplicateGuard(database)
        self._executor = executor
        self._mapper = mapper or PublishMetadataMapper()
        self._flags = flags

    def publish(
        self,
        content_item_id: str,
        *,
        actor: Actor,
        privacy_status: YouTubePrivacyStatus = YouTubePrivacyStatus.PRIVATE,
        category_id: str = DEFAULT_YOUTUBE_CATEGORY_ID,
        publish_at: datetime | None = None,
    ) -> PublishOutcome:
        if self._flags is not None:
            if not self._flags.shorts_enabled:
                raise PublishError(
                    PublishErrorCode.SHORTS_DISABLED,
                    "shorts_enabled is False",
                    http_status=HTTPStatus.FORBIDDEN,
                )
            if not self._flags.publish_enabled:
                raise PublishError(
                    PublishErrorCode.PUBLISH_DISABLED,
                    "publish_enabled is False",
                    http_status=HTTPStatus.FORBIDDEN,
                )

        context = self._loader.load(content_item_id)
        self._executor.begin(context, actor=actor)
        try:
            check = self._guard.check(
                content_item_id=context.item.id,
                channel_id=context.channel.id,
                video_sha256=context.video_artifact.sha256,
                keys=context.keys,
            )
        except DuplicatePublishError as error:
            self._executor.record_duplicate(context, actor=actor, error=error)
            raise
        if check.decision is DuplicateDecision.REPLAY:
            return self._executor.replay(context, check, actor=actor)

        media = self._loader.read_media(context)
        payload = self._mapper.map_to_payload(
            channel=context.channel,
            content_type=ContentType.SHORTS,
            approval=context.approval,
            video_artifact=context.video_artifact,
            metadata_artifact=context.metadata_artifact,
            metadata_source=media.metadata_bytes,
            thumbnail_artifact=context.thumbnail_artifact,
            subtitles_artifact=context.subtitles_artifact,
            disclosure_record=context.disclosure_record,
            privacy_status=privacy_status,
            category_id=category_id,
            publish_at=publish_at,
            idempotency_key=context.keys.upload_key,
        )
        return self._executor.execute(
            PublishRequest(
                context=context,
                media=media,
                payload=payload,
                actor=actor,
                check=check,
                audit_prefix=SHORTS_AUDIT_PREFIX,
            )
        )
