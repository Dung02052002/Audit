"""LongForm Publisher (Prompt Pack v8, prompt #150), context C12 Publishing.

``LongFormPublisher`` publishes an approved LongForm ``ContentItem`` through the
same shared parts as ``ShortsPublisher`` (``PublishContextLoader``,
``PublishDuplicateGuard``, ``PublishMetadataMapper``, ``PublishExecutor``), with
the LongForm rules on top:

- ``FeatureFlags.longform_enabled`` must be on (``LongFormDisabledError`` otherwise,
  the same refusal as the LongForm production steps) and so must ``publish_enabled``.
- The mapper (#148) requires ``00:00`` chapter timestamps in the description when
  the metadata has chapters.
- A video longer than 15 minutes needs a channel that is allowed long uploads: the
  channel check runs on the verified remote channel before any job exists
  (``LONG_UPLOADS_DISALLOWED``, 409).
- Audit actions are ``longform.published`` and ``longform.publish_failed``.
"""

from datetime import datetime
from http import HTTPStatus
from typing import Final

from ai_youtube_agent.content.longform_script_generator import LongFormDisabledError
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
from ai_youtube_agent.providers.youtube_channel import RemoteChannelInfo
from ai_youtube_agent.providers.youtube_upload import YouTubePrivacyStatus

LONGFORM_PUBLISHED: Final[str] = "longform.published"
LONGFORM_PUBLISH_FAILED: Final[str] = "longform.publish_failed"
LONGFORM_AUDIT_PREFIX: Final[str] = "longform"
LONG_UPLOAD_THRESHOLD_MS: Final[int] = 15 * 60 * 1_000


class LongFormPublisher:
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
            database, storage, content_type=ContentType.LONGFORM
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
            if not self._flags.longform_enabled:
                raise LongFormDisabledError("longform_enabled is False")
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
            content_type=ContentType.LONGFORM,
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
        duration_ms = media.video_duration_ms

        def check_long_uploads(remote: RemoteChannelInfo) -> None:
            if (
                duration_ms > LONG_UPLOAD_THRESHOLD_MS
                and not remote.allows_long_uploads
            ):
                raise PublishError(
                    PublishErrorCode.LONG_UPLOADS_DISALLOWED,
                    f"channel {remote.youtube_channel_id} has long_uploads_status "
                    f"{remote.long_uploads_status!r} and the video lasts "
                    f"{duration_ms} ms",
                    http_status=HTTPStatus.CONFLICT,
                )

        return self._executor.execute(
            PublishRequest(
                context=context,
                media=media,
                payload=payload,
                actor=actor,
                check=check,
                audit_prefix=LONGFORM_AUDIT_PREFIX,
                channel_check=check_long_uploads,
            )
        )
