"""Tests for comment sync and pagination service (P-183, prompt #183)."""

from datetime import UTC, datetime, timedelta

import pytest

from ai_youtube_agent.content.channel import Channel, ChannelStatus
from ai_youtube_agent.content.comment_sync import (
    COMMENTS_SYNCED,
    CommentSync,
    CommentSyncError,
    CommentSyncErrorCode,
)
from ai_youtube_agent.content.strategy import StrategyProfile
from ai_youtube_agent.core.artifact import ArtifactKind
from ai_youtube_agent.core.audit import AuditLog, InMemoryAuditSink
from ai_youtube_agent.core.content_item import ContentStatus, ContentType
from ai_youtube_agent.core.db.database import Database
from ai_youtube_agent.core.db.repositories.channel import (
    ChannelRepository,
    StrategyProfileRepository,
)
from ai_youtube_agent.core.db.repositories.content import (
    ArtifactRepository,
    ContentItemRepository,
)
from ai_youtube_agent.core.db.repositories.publish import PublishJobRepository
from ai_youtube_agent.core.db.repositories.review import ApprovalRequestRepository
from ai_youtube_agent.pipeline.publish import PublishJob
from ai_youtube_agent.pipeline.publish_result import PublishResultService
from ai_youtube_agent.pipeline.upload_retry import UploadRetryPolicy
from ai_youtube_agent.providers.comments import CommentErrorCode, RemoteComment
from ai_youtube_agent.providers.mock_comments import MockCommentProvider
from ai_youtube_agent.providers.mock_youtube_oauth import MockYouTubeOAuthProvider
from ai_youtube_agent.providers.youtube_oauth import OAuthScope
from factories import (
    USER,
    make_approval_request,
    make_artifact,
    make_channel,
    make_content_item,
    make_strategy_profile,
)

NOW = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)
PUBLISHED = datetime(2026, 10, 5, 9, 0, tzinfo=UTC)


def _publish_video(
    database: Database,
    channel: Channel,
    profile: StrategyProfile,
    youtube_video_id: str,
) -> None:
    item = make_content_item(channel, profile, status=ContentStatus.PUBLISHED)
    video = make_artifact(item, ArtifactKind.VIDEO)
    approval = make_approval_request(item, [video]).approve(USER)
    job = (
        PublishJob.create(
            approval,
            ContentType.SHORTS,
            f"pub:{youtube_video_id}",
            clock=lambda: PUBLISHED,
        )
        .start(clock=lambda: PUBLISHED)
        .succeed(youtube_video_id, clock=lambda: PUBLISHED)
    )
    with database.transaction() as connection:
        ContentItemRepository(connection).add(item)
        ArtifactRepository(connection).add(video)
        ApprovalRequestRepository(connection).add(approval)
        PublishJobRepository(connection).add(job)


class TestCommentSync:
    def test_syncs_across_pages_idempotently_and_skips_disabled_videos(
        self, database: Database
    ) -> None:
        channel = make_channel(status=ChannelStatus.ACTIVE)
        profile = make_strategy_profile(channel)
        with database.transaction() as connection:
            ChannelRepository(connection).add(channel)
            StrategyProfileRepository(connection).add(profile)

        v1 = "vid000001cs"
        v2 = "vid000002cs"
        _publish_video(database, channel, profile, v1)
        _publish_video(database, channel, profile, v2)

        provider = MockCommentProvider(clock=lambda: NOW)
        oauth = MockYouTubeOAuthProvider(clock=lambda: NOW)
        oauth.seed_token(channel.id, scopes=frozenset({OAuthScope.YOUTUBE_FORCE_SSL}))
        sink = InMemoryAuditSink()
        audit = AuditLog(sink, clock=lambda: NOW)
        sync = CommentSync(
            database,
            provider,
            oauth,
            audit,
            results=PublishResultService(database, sink),
            retry_policy=UploadRetryPolicy(max_attempts=3, base_delay_seconds=1),
            page_size=2,
            sleep=lambda _: None,
        )

        c1 = RemoteComment(
            youtube_comment_id="Ugx-c1",
            youtube_video_id=v1,
            author_display_name="Viewer A",
            text="First comment",
            published_at=NOW - timedelta(minutes=30),
        )
        c2 = RemoteComment(
            youtube_comment_id="Ugx-c2",
            youtube_video_id=v1,
            parent_comment_id="Ugx-c1",
            author_display_name="Viewer B",
            text="Reply to first",
            published_at=NOW - timedelta(minutes=20),
        )
        c3 = RemoteComment(
            youtube_comment_id="Ugx-c3",
            youtube_video_id=v1,
            author_display_name="Viewer C",
            text="Third comment",
            published_at=NOW - timedelta(minutes=10),
        )
        provider.seed_comments(channel.youtube.channel_id, v1, (c1, c2, c3))
        provider.disable_comments(channel.youtube.channel_id, v2, disabled=True)
        provider.fail_next(CommentErrorCode.TIMEOUT, times=1)

        result = sync.sync(channel.id, actor=USER)
        assert result.created_count == 3
        assert result.unchanged_count == 0
        assert result.disabled_video_ids == (v2,)
        assert result.pages == 2
        assert sink.entries[-1].action == COMMENTS_SYNCED
        assert "First comment" not in str(sink.entries[-1].metadata)

        # Second sync is idempotent
        repeat = sync.sync(channel.id, actor=USER)
        assert repeat.created_count == 0
        assert repeat.unchanged_count == 3

        assert len(sync.list_for_video(channel.id, v1)) == 3
        assert len(sync.list_for_video(channel.id, v1, include_replies=False)) == 2

    def test_refuses_unpublished_video_and_excessive_pages(
        self, database: Database
    ) -> None:
        channel = make_channel(status=ChannelStatus.ACTIVE)
        profile = make_strategy_profile(channel)
        with database.transaction() as connection:
            ChannelRepository(connection).add(channel)
            StrategyProfileRepository(connection).add(profile)

        v1 = "vid000003cs"
        _publish_video(database, channel, profile, v1)

        provider = MockCommentProvider(clock=lambda: NOW)
        oauth = MockYouTubeOAuthProvider(clock=lambda: NOW)
        oauth.seed_token(channel.id, scopes=frozenset({OAuthScope.YOUTUBE_READONLY}))
        sync = CommentSync(
            database,
            provider,
            oauth,
            AuditLog(InMemoryAuditSink()),
            page_size=1,
            max_pages_per_video=2,
            sleep=lambda _: None,
        )

        with pytest.raises(CommentSyncError) as exc_unpub:
            sync.sync(channel.id, actor=USER, video_ids=("unpub123456",))
        assert exc_unpub.value.sync_code is CommentSyncErrorCode.VIDEO_NOT_PUBLISHED

        # Seed 3 comments with page_size=1 and max_pages_per_video=2 -> TOO_MANY_PAGES
        provider.seed_comments(
            channel.youtube.channel_id,
            v1,
            tuple(
                RemoteComment(
                    youtube_comment_id=f"Ugx-p{i}",
                    youtube_video_id=v1,
                    author_display_name="V",
                    text=f"C{i}",
                    published_at=NOW - timedelta(minutes=10 - i),
                )
                for i in range(3)
            ),
        )
        with pytest.raises(CommentSyncError) as exc_pages:
            sync.sync(channel.id, actor=USER, video_ids=(v1,))
        assert exc_pages.value.sync_code is CommentSyncErrorCode.TOO_MANY_PAGES
