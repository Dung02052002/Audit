"""Tests for the confirmed publish result (M-155, prompt #155)."""

from pathlib import Path

import pytest

from ai_youtube_agent.core.artifact import ArtifactKind
from ai_youtube_agent.core.audit import (
    AuditLog,
    AuditResult,
    EntityRef,
    InMemoryAuditSink,
)
from ai_youtube_agent.core.content_item import ContentStatus, ContentType
from ai_youtube_agent.core.db.database import Database
from ai_youtube_agent.core.db.repositories.content import ContentItemRepository
from ai_youtube_agent.core.db.repositories.publish import PublishJobRepository
from ai_youtube_agent.pipeline.idempotency import derive_publish_keys
from ai_youtube_agent.pipeline.publish import PublishJob
from ai_youtube_agent.pipeline.publish_result import (
    PublishReceiptError,
    PublishResultInconsistentError,
    PublishResultNotFoundError,
    PublishResultService,
    confirm_receipt,
)
from ai_youtube_agent.providers.youtube_upload import (
    YouTubePrivacyStatus,
    YouTubeUploadPayload,
    YouTubeUploadReceipt,
    YouTubeVideoSnippet,
    YouTubeVideoStatus,
)
from factories import USER
from publish_helpers import T_PUBLISH, Seed, clock, seed_approved_item

VIDEO_ID = "abcdefghijk"
YT_CHANNEL = "UC" + "A" * 22
SHA = "c" * 64


def _payload(**overrides) -> YouTubeUploadPayload:
    values = dict(
        channel_id="channel-1",
        youtube_channel_id=YT_CHANNEL,
        content_item_id="item-1",
        content_type=ContentType.SHORTS,
        approval_request_id="request-1",
        idempotency_key="upl:" + "d" * 64,
        video_artifact_id="video-1",
        video_version=1,
        video_sha256=SHA,
        video_size_bytes=100,
        video_media_type="video/mp4",
        snippet=YouTubeVideoSnippet(
            title="A title #Shorts",
            description="A description",
            tags=("Shorts",),
            category_id="22",
            default_language="en",
            default_audio_language="en",
        ),
        status=YouTubeVideoStatus(
            privacy_status=YouTubePrivacyStatus.PRIVATE,
            self_declared_made_for_kids=False,
            contains_synthetic_media=True,
        ),
        metadata_artifact_id="metadata-1",
        metadata_version=1,
        metadata_sha256="e" * 64,
    )
    values.update(overrides)
    return YouTubeUploadPayload(**values)


def _receipt(**overrides) -> YouTubeUploadReceipt:
    values = dict(
        youtube_video_id=VIDEO_ID,
        channel_id="channel-1",
        youtube_channel_id=YT_CHANNEL,
        idempotency_key="upl:" + "d" * 64,
        video_sha256=SHA,
        privacy_status=YouTubePrivacyStatus.PRIVATE,
        contains_synthetic_media=True,
        thumbnail_uploaded=False,
        captions_uploaded=False,
        uploaded_at=T_PUBLISH,
        provider="mock",
    )
    values.update(overrides)
    return YouTubeUploadReceipt(**values)


def test_a_matching_receipt_is_confirmed() -> None:
    confirm_receipt(_receipt(), _payload())


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("channel_id", "channel-2"),
        ("youtube_channel_id", "UC" + "B" * 22),
        ("idempotency_key", "upl:" + "f" * 64),
        ("video_sha256", "9" * 64),
        ("privacy_status", YouTubePrivacyStatus.PUBLIC),
        ("contains_synthetic_media", False),
    ],
)
def test_a_receipt_for_another_publish_is_refused(field: str, value: object) -> None:
    with pytest.raises(PublishReceiptError) as caught:
        confirm_receipt(_receipt(**{field: value}), _payload())

    assert caught.value.mismatches == (field,)
    assert caught.value.to_public().http_status == 409


@pytest.fixture
def seed(database: Database, tmp_path: Path) -> Seed:
    return seed_approved_item(database, tmp_path)


def _publish(
    seed: Seed,
    *,
    content_type: ContentType = ContentType.SHORTS,
    move_item: bool = True,
) -> PublishJob:
    keys = derive_publish_keys(
        seed.item.id,
        seed.approval.id,
        seed.channel.youtube.channel_id,
        seed.artifacts[ArtifactKind.VIDEO].sha256,
    )
    job = (
        PublishJob.create(seed.approval, content_type, keys.job_key, clock=clock)
        .start(clock=clock)
        .succeed(VIDEO_ID, clock=clock)
    )
    with seed.database.transaction() as connection:
        PublishJobRepository(connection).add(job)
        if move_item:
            published = seed.item.with_status(
                ContentStatus.PUBLISHING, clock=clock
            ).with_status(ContentStatus.PUBLISHED, clock=clock)
            ContentItemRepository(connection).update(
                published, expected_updated_at=seed.item.updated_at
            )
    return job


def test_the_result_of_a_published_item_is_read_back(seed: Seed) -> None:
    job = _publish(seed)
    sink = InMemoryAuditSink()
    AuditLog(sink).record(
        "shorts.published",
        USER,
        EntityRef("publish_job", job.id),
        AuditResult.SUCCESS,
        metadata={"privacy_status": "private", "contains_synthetic_media": True},
    )

    record = PublishResultService(seed.database, sink).for_content_item(seed.item.id)

    assert record.youtube_video_id == VIDEO_ID
    assert record.watch_url == f"https://www.youtube.com/shorts/{VIDEO_ID}"
    assert record.job_id == job.id
    assert record.channel_id == seed.channel.id
    assert record.youtube_channel_id == seed.channel.youtube.channel_id
    assert record.published_at == T_PUBLISH
    assert record.privacy_status == "private"
    assert record.contains_synthetic_media is True
    assert record.as_dict()["content_type"] == "shorts"


def test_without_an_audit_sink_the_receipt_details_are_unknown(seed: Seed) -> None:
    _publish(seed)

    record = PublishResultService(seed.database).for_content_item(seed.item.id)

    assert record.privacy_status is None
    assert record.contains_synthetic_media is None


def test_an_unpublished_item_has_no_result(seed: Seed) -> None:
    service = PublishResultService(seed.database)

    with pytest.raises(PublishResultNotFoundError):
        service.for_content_item(seed.item.id)
    with pytest.raises(PublishResultNotFoundError):
        service.for_content_item("unknown-item")


def test_a_succeeded_job_on_an_unpublished_item_is_reported_as_inconsistent(
    seed: Seed,
) -> None:
    _publish(seed, move_item=False)

    with pytest.raises(PublishResultInconsistentError) as caught:
        PublishResultService(seed.database).for_content_item(seed.item.id)

    assert caught.value.to_public().http_status == 409


def test_the_results_of_a_channel_are_listed_oldest_first(seed: Seed) -> None:
    job = _publish(seed)

    records = PublishResultService(seed.database).list_for_channel(seed.channel.id)

    assert [record.job_id for record in records] == [job.id]
    assert records[0].youtube_video_id == VIDEO_ID
    with pytest.raises(PublishResultNotFoundError):
        PublishResultService(seed.database).list_for_channel("unknown-channel")


def test_a_longform_result_links_to_the_watch_page(
    database: Database, tmp_path: Path
) -> None:
    long_seed = seed_approved_item(
        database, tmp_path, content_type=ContentType.LONGFORM, duration_ms=600_000
    )
    _publish(long_seed, content_type=ContentType.LONGFORM)

    record = PublishResultService(database).for_content_item(long_seed.item.id)

    assert record.watch_url == f"https://www.youtube.com/watch?v={VIDEO_ID}"
