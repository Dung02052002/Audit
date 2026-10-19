"""Shared seeding for the publishing tests (M-149..M-157).

``seed_approved_item`` stores a channel, a strategy, an ``APPROVED`` content item,
real files in a ``LocalFileStorage`` (a stub MP4 and a metadata JSON, optionally a
thumbnail), the matching ``Artifact`` rows and an ``APPROVED`` ``ApprovalRequest``
bound to them. ``build_publishing`` wires mock providers and the publishers on top.
Nothing here reaches the network.
"""

import dataclasses
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ai_youtube_agent.content.approval import ApprovalRequest
from ai_youtube_agent.content.channel import Channel, ChannelStatus
from ai_youtube_agent.content.qc import QCCheck, QCResult, QCStatus
from ai_youtube_agent.content.strategy import Cadence, StrategyProfile
from ai_youtube_agent.core.artifact import Artifact, ArtifactKind
from ai_youtube_agent.core.artifact_storage import LocalFileStorage
from ai_youtube_agent.core.audit import AuditLog, InMemoryAuditSink
from ai_youtube_agent.core.config import Environment, Settings
from ai_youtube_agent.core.content_item import (
    ContentItem,
    ContentStatus,
    ContentType,
)
from ai_youtube_agent.core.db.database import Database
from ai_youtube_agent.core.db.repositories.channel import (
    ChannelRepository,
    StrategyProfileRepository,
)
from ai_youtube_agent.core.db.repositories.content import (
    ArtifactRepository,
    ContentItemRepository,
)
from ai_youtube_agent.core.db.repositories.review import (
    ApprovalRequestRepository,
    QCResultRepository,
)
from ai_youtube_agent.core.flags import FeatureFlags
from ai_youtube_agent.core.publish_gate import PublishGate
from ai_youtube_agent.pipeline.longform_publisher import LongFormPublisher
from ai_youtube_agent.pipeline.publish_executor import PublishExecutor
from ai_youtube_agent.pipeline.shorts_publisher import ShortsPublisher
from ai_youtube_agent.providers.mock_video_render import stub_mp4
from ai_youtube_agent.providers.mock_youtube_channel import MockYouTubeChannelProvider
from ai_youtube_agent.providers.mock_youtube_oauth import MockYouTubeOAuthProvider
from ai_youtube_agent.providers.mock_youtube_upload import MockYouTubeUploadProvider
from factories import (
    SYSTEM,
    USER,
    at,
    make_channel,
    make_content_item,
    make_strategy_profile,
)

T_PUBLISH = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)
PUBLISH_FLAGS = FeatureFlags(
    shorts_enabled=True,
    longform_enabled=True,
    publish_enabled=True,
    test_required=True,
    approval_required=True,
)


def clock() -> datetime:
    return T_PUBLISH


@dataclass
class Seed:
    database: Database
    storage: LocalFileStorage
    settings: Settings
    channel: Channel
    profile: StrategyProfile
    item: ContentItem
    approval: ApprovalRequest
    artifacts: dict[ArtifactKind, Artifact]
    video_bytes: bytes


def store_artifact(
    storage: LocalFileStorage,
    repository: ArtifactRepository,
    content_item_id: str,
    kind: ArtifactKind,
    media_type: str,
    data: bytes,
    *,
    version: int = 1,
) -> Artifact:
    stored = storage.write(content_item_id, kind, version, data, media_type=media_type)
    artifact = Artifact.create(
        content_item_id,
        kind,
        uri=stored.uri,
        sha256=stored.sha256,
        size_bytes=stored.size_bytes,
        media_type=media_type,
        clock=at(),
    )
    repository.add(artifact)
    return artifact


def default_metadata(
    content_type: ContentType, item: ContentItem, channel: Channel
) -> dict[str, Any]:
    base: dict[str, Any] = {
        "content_item_id": item.id,
        "channel_id": channel.id,
        "language": "en",
        "made_for_kids": False,
        "contains_synthetic_media": True,
        "tags": ["Science", "Explained"],
    }
    if content_type is ContentType.SHORTS:
        return {
            **base,
            "title": "Five bank fees you never noticed #Shorts",
            "description": "Quick money explainer #Shorts",
            "tags": ["Shorts", "Money"],
        }
    return {
        **base,
        "title": "Five bank fees you never noticed: the full guide",
        "description": "A deep dive.\n\n00:00 Intro\n02:00 The fees",
        "chapters": [
            {"title": "Intro", "start_ms": 0, "end_ms": 120_000},
            {"title": "The fees", "start_ms": 120_000, "end_ms": 600_000},
        ],
    }


def seed_approved_item(
    database: Database,
    tmp_path: Path,
    *,
    content_type: ContentType = ContentType.SHORTS,
    duration_ms: int = 30_000,
    payload: bytes = b"video-payload",
    metadata: dict[str, Any] | None = None,
    with_thumbnail: bool = False,
    channel: Channel | None = None,
) -> Seed:
    storage = LocalFileStorage(tmp_path / "artifacts")
    settings = Settings(environment=Environment.TEST)
    channel = channel or make_channel(status=ChannelStatus.ACTIVE)
    profile = make_strategy_profile(
        channel, cadence=Cadence(shorts_per_day=5, longform_per_day=5)
    )
    item = make_content_item(
        channel,
        profile,
        content_type=content_type,
        status=ContentStatus.APPROVED,
    )
    video_bytes = stub_mp4(duration_ms, payload)
    metadata_json = json.dumps(
        metadata or default_metadata(content_type, item, channel)
    ).encode("utf-8")

    with database.transaction() as connection:
        ChannelRepository(connection).add(channel)
        StrategyProfileRepository(connection).add(profile)
        ContentItemRepository(connection).add(item)
        repository = ArtifactRepository(connection)
        artifacts = {
            ArtifactKind.VIDEO: store_artifact(
                storage, repository, item.id, ArtifactKind.VIDEO, "video/mp4", video_bytes
            ),
            ArtifactKind.METADATA: store_artifact(
                storage,
                repository,
                item.id,
                ArtifactKind.METADATA,
                "application/json",
                metadata_json,
            ),
        }
        if with_thumbnail:
            artifacts[ArtifactKind.THUMBNAIL] = store_artifact(
                storage,
                repository,
                item.id,
                ArtifactKind.THUMBNAIL,
                "image/png",
                b"\x89PNG\r\n\x1a\n-thumbnail-bytes",
            )
        bound = list(artifacts.values())
        qc = QCResult.create(
            item.id, bound, [QCCheck("video.fps", QCStatus.PASS)], clock=at()
        )
        QCResultRepository(connection).add(qc)
        approval = dataclasses.replace(
            ApprovalRequest.create(
                item.id, bound, requested_by=SYSTEM, qc_result=qc, clock=at()
            )
        ).approve(USER)
        ApprovalRequestRepository(connection).add(approval)
    return Seed(
        database, storage, settings, channel, profile, item, approval, artifacts, video_bytes
    )


@dataclass
class Publishing:
    seed: Seed
    sink: InMemoryAuditSink
    oauth: MockYouTubeOAuthProvider
    channels: MockYouTubeChannelProvider
    uploader: MockYouTubeUploadProvider
    executor: PublishExecutor
    shorts: ShortsPublisher
    longform: LongFormPublisher


def build_publishing(
    seed: Seed,
    *,
    uploader: MockYouTubeUploadProvider | None = None,
    **executor_options: Any,
) -> Publishing:
    """Wire the mock providers and both publishers. Retry waits never sleep unless a
    test passes its own ``sleep``."""
    executor_options.setdefault("sleep", lambda _seconds: None)
    sink = InMemoryAuditSink()
    gate = PublishGate(seed.database, seed.settings)
    oauth = MockYouTubeOAuthProvider(clock=clock)
    channels = MockYouTubeChannelProvider(oauth_provider=oauth, clock=clock)
    uploader = uploader or MockYouTubeUploadProvider(clock=clock)
    executor = PublishExecutor(
        seed.database,
        AuditLog(sink),
        gate,
        oauth,
        channels,
        uploader,
        clock=clock,
        **executor_options,
    )
    return Publishing(
        seed=seed,
        sink=sink,
        oauth=oauth,
        channels=channels,
        uploader=uploader,
        executor=executor,
        shorts=ShortsPublisher(
            seed.database, seed.storage, executor, flags=PUBLISH_FLAGS
        ),
        longform=LongFormPublisher(
            seed.database, seed.storage, executor, flags=PUBLISH_FLAGS
        ),
    )


def actions(sink: InMemoryAuditSink) -> list[str]:
    return [event.action for event in sink.events()]
