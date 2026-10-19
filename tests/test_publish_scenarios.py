"""Publishing scenarios with mock providers (M-157, prompt #157).

Success, failure, retry, resume, token recovery, duplicate and gate cases for the
Shorts and LongForm publishers, run end to end on a real (temporary) database and
real stored files. The providers are the mocks: nothing reaches YouTube.
"""

import dataclasses
from datetime import timedelta
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import url2pathname

import pytest
from pydantic import SecretStr

from ai_youtube_agent.content.longform_script_generator import LongFormDisabledError
from ai_youtube_agent.core.artifact import ArtifactKind
from ai_youtube_agent.core.content_item import ContentStatus, ContentType
from ai_youtube_agent.core.db.database import Database
from ai_youtube_agent.core.db.repositories.content import (
    ArtifactRepository,
    ContentItemRepository,
)
from ai_youtube_agent.core.db.repositories.publish import PublishJobRepository
from ai_youtube_agent.core.db.repositories.review import (
    ApprovalRequestRepository,
    RightsRecordRepository,
)
from ai_youtube_agent.core.flags import FeatureFlags
from ai_youtube_agent.core.gates import GateBlockedError
from ai_youtube_agent.pipeline.duplicate_guard import (
    DuplicateErrorCode,
    DuplicatePublishError,
)
from ai_youtube_agent.pipeline.idempotency import derive_publish_keys
from ai_youtube_agent.pipeline.longform_publisher import LongFormPublisher
from ai_youtube_agent.pipeline.publish import PublishJob, PublishStatus
from ai_youtube_agent.pipeline.publish_audit import (
    PUBLISH_BLOCKED,
    PUBLISH_DUPLICATE_BLOCKED,
    PUBLISH_REPLAYED,
    PUBLISH_REQUESTED,
    PUBLISH_TOKEN_RENEWED,
    PUBLISH_UPLOAD_RETRY,
    PUBLISH_AUTHORIZATION_FAILED,
)
from ai_youtube_agent.pipeline.publish_errors import PublishError, PublishErrorCode
from ai_youtube_agent.pipeline.publish_result import (
    PublishReceiptError,
    PublishResultService,
)
from ai_youtube_agent.pipeline.shorts_publisher import ShortsPublisher
from ai_youtube_agent.pipeline.token_recovery import PublishAuthorizationError
from ai_youtube_agent.providers.mock_youtube_oauth import DEFAULT_MOCK_SCOPES
from ai_youtube_agent.providers.mock_youtube_upload import MockYouTubeUploadProvider
from ai_youtube_agent.providers.youtube_channel import RemoteChannelInfo
from ai_youtube_agent.providers.youtube_oauth import OAuthToken
from ai_youtube_agent.providers.youtube_upload import (
    UploadErrorCode,
    YouTubePrivacyStatus,
    YouTubeUploadError,
)
from factories import (
    USER,
    at,
    make_approval_request,
    make_artifact,
    make_content_item,
    make_rights_record,
)
from publish_helpers import (
    T_PUBLISH,
    Publishing,
    Seed,
    actions,
    build_publishing,
    clock,
    seed_approved_item,
)


@pytest.fixture
def seed(database: Database, tmp_path: Path) -> Seed:
    return seed_approved_item(database, tmp_path)


@pytest.fixture
def pub(seed: Seed) -> Publishing:
    return build_publishing(seed)


def _item(seed: Seed):
    with seed.database.transaction() as connection:
        return ContentItemRepository(connection).get(seed.item.id)


def _jobs(seed: Seed) -> list[PublishJob]:
    with seed.database.transaction() as connection:
        return PublishJobRepository(connection).list_by_content_item(seed.item.id)


def _keys(seed: Seed):
    return derive_publish_keys(
        seed.item.id,
        seed.approval.id,
        seed.channel.youtube.channel_id,
        seed.artifacts[ArtifactKind.VIDEO].sha256,
    )


def _event(pub: Publishing, action: str):
    return next(event for event in pub.sink.events() if event.action == action)


# --- success ---------------------------------------------------------------------


def test_an_approved_short_is_published_privately_by_default(
    seed: Seed, pub: Publishing
) -> None:
    outcome = pub.shorts.publish(seed.item.id, actor=USER)

    assert outcome.replayed is False
    assert outcome.receipt is not None and outcome.payload is not None
    assert outcome.receipt.privacy_status is YouTubePrivacyStatus.PRIVATE
    assert outcome.job.status is PublishStatus.SUCCEEDED
    assert outcome.job.result is not None
    assert outcome.job.result.youtube_video_id == outcome.receipt.youtube_video_id
    assert outcome.payload.idempotency_key == _keys(seed).upload_key
    assert outcome.job.idempotency_key == _keys(seed).job_key
    assert len(pub.uploader.uploads) == 1
    assert _item(seed).status is ContentStatus.PUBLISHED
    assert [job.status for job in _jobs(seed)] == [PublishStatus.SUCCEEDED]
    assert actions(pub.sink) == [PUBLISH_REQUESTED, "shorts.published"]

    stored = PublishResultService(seed.database, pub.sink).for_content_item(
        seed.item.id
    )
    assert stored.youtube_video_id == outcome.receipt.youtube_video_id
    assert stored.privacy_status == "private"


def test_a_second_publish_replays_the_stored_result(
    seed: Seed, pub: Publishing
) -> None:
    first = pub.shorts.publish(seed.item.id, actor=USER)

    second = pub.shorts.publish(seed.item.id, actor=USER)

    assert second.replayed is True
    assert second.receipt is None
    assert second.job.id == first.job.id
    assert second.job.result == first.job.result
    assert pub.uploader.calls == 1
    assert actions(pub.sink)[-2:] == [PUBLISH_REQUESTED, PUBLISH_REPLAYED]


def test_a_longform_video_with_chapters_is_published(
    database: Database, tmp_path: Path
) -> None:
    seed = seed_approved_item(
        database,
        tmp_path,
        content_type=ContentType.LONGFORM,
        duration_ms=20 * 60 * 1_000,
        with_thumbnail=True,
    )
    pub = build_publishing(seed)

    outcome = pub.longform.publish(
        seed.item.id, actor=USER, privacy_status=YouTubePrivacyStatus.UNLISTED
    )

    assert outcome.job.status is PublishStatus.SUCCEEDED
    assert outcome.payload is not None
    assert "00:00 Intro" in outcome.payload.snippet.description
    assert outcome.payload.thumbnail_artifact_id is not None
    assert outcome.receipt is not None and outcome.receipt.thumbnail_uploaded
    assert outcome.receipt.privacy_status is YouTubePrivacyStatus.UNLISTED
    assert "longform.published" in actions(pub.sink)


# --- failure ---------------------------------------------------------------------


def test_an_invalid_payload_fails_the_item_without_a_retry(
    seed: Seed, pub: Publishing
) -> None:
    pub.uploader.fail_next(UploadErrorCode.INVALID_PAYLOAD)

    with pytest.raises(YouTubeUploadError):
        pub.shorts.publish(seed.item.id, actor=USER)

    assert pub.uploader.calls == 1
    assert _item(seed).status is ContentStatus.FAILED
    [job] = _jobs(seed)
    assert job.status is PublishStatus.FAILED
    assert job.last_error == UploadErrorCode.INVALID_PAYLOAD.value
    event = _event(pub, "shorts.publish_failed")
    assert event.metadata["resumable"] is False
    assert event.metadata["upload_attempts"] == 1
    assert "shorts.published" not in actions(pub.sink)


def test_a_receipt_that_does_not_match_is_never_recorded_as_published(
    seed: Seed,
) -> None:
    class WrongReceiptUploader(MockYouTubeUploadProvider):
        def upload(self, payload, **kwargs):
            receipt = super().upload(payload, **kwargs)
            return dataclasses.replace(
                receipt, contains_synthetic_media=not receipt.contains_synthetic_media
            )

    pub = build_publishing(seed, uploader=WrongReceiptUploader(clock=clock))

    with pytest.raises(PublishReceiptError):
        pub.shorts.publish(seed.item.id, actor=USER)

    assert _item(seed).status is ContentStatus.FAILED
    [job] = _jobs(seed)
    assert job.status is PublishStatus.FAILED
    assert job.result is None
    assert "shorts.published" not in actions(pub.sink)
    assert _event(pub, "shorts.publish_failed").metadata["resumable"] is False


# --- retry and resume -------------------------------------------------------------


def test_retryable_errors_are_retried_with_growing_waits(seed: Seed) -> None:
    waits: list[float] = []
    pub = build_publishing(seed, sleep=waits.append)
    pub.uploader.fail_next(UploadErrorCode.UNAVAILABLE, times=2)

    outcome = pub.shorts.publish(seed.item.id, actor=USER)

    assert outcome.job.status is PublishStatus.SUCCEEDED
    assert pub.uploader.calls == 3
    assert waits == [2.0, 4.0]
    assert actions(pub.sink).count(PUBLISH_UPLOAD_RETRY) == 2
    assert _event(pub, "shorts.published").metadata["upload_attempts"] == 3
    assert _item(seed).status is ContentStatus.PUBLISHED


def test_exhausted_retries_leave_the_item_publishing_and_a_later_call_resumes(
    seed: Seed, pub: Publishing
) -> None:
    pub.uploader.fail_next(UploadErrorCode.TIMEOUT, times=3)

    with pytest.raises(YouTubeUploadError):
        pub.shorts.publish(seed.item.id, actor=USER)

    assert pub.uploader.calls == 3
    assert _item(seed).status is ContentStatus.PUBLISHING
    [failed] = _jobs(seed)
    assert failed.status is PublishStatus.FAILED
    assert failed.last_error == UploadErrorCode.TIMEOUT.value
    failure = _event(pub, "shorts.publish_failed")
    assert failure.metadata["resumable"] is True
    assert failure.metadata["upload_attempts"] == 3

    resumed = pub.shorts.publish(seed.item.id, actor=USER)

    assert resumed.replayed is False
    assert resumed.job.id == failed.id
    assert resumed.job.attempts == 2
    assert resumed.job.status is PublishStatus.SUCCEEDED
    assert _item(seed).status is ContentStatus.PUBLISHED
    assert len(pub.uploader.uploads) == 1
    assert len(_jobs(seed)) == 1


def test_a_quota_error_is_resumable_not_final(seed: Seed, pub: Publishing) -> None:
    pub.uploader.fail_next(UploadErrorCode.QUOTA_EXCEEDED)

    with pytest.raises(YouTubeUploadError):
        pub.shorts.publish(seed.item.id, actor=USER)

    assert pub.uploader.calls == 1
    assert _item(seed).status is ContentStatus.PUBLISHING

    assert pub.shorts.publish(seed.item.id, actor=USER).job.status is (
        PublishStatus.SUCCEEDED
    )


def test_a_resume_is_checked_by_the_gates_again(seed: Seed, pub: Publishing) -> None:
    pub.uploader.fail_next(UploadErrorCode.QUOTA_EXCEEDED)
    with pytest.raises(YouTubeUploadError):
        pub.shorts.publish(seed.item.id, actor=USER)
    with seed.database.transaction() as connection:
        RightsRecordRepository(connection).add(make_rights_record(seed.item))

    with pytest.raises(GateBlockedError):
        pub.shorts.publish(seed.item.id, actor=USER)

    assert _item(seed).status is ContentStatus.PUBLISHING
    assert PUBLISH_BLOCKED in actions(pub.sink)
    assert len(pub.uploader.uploads) == 0


# --- token recovery ----------------------------------------------------------------


def test_an_expired_token_is_renewed_before_the_upload(
    seed: Seed, pub: Publishing
) -> None:
    pub.oauth.expire_channel_token(seed.channel.id)

    outcome = pub.shorts.publish(seed.item.id, actor=USER)

    assert outcome.job.status is PublishStatus.SUCCEEDED
    renewed = _event(pub, PUBLISH_TOKEN_RENEWED)
    assert renewed.metadata["stage"] == "token"
    assert actions(pub.sink).index(PUBLISH_TOKEN_RENEWED) < actions(pub.sink).index(
        "shorts.published"
    )


def test_an_upload_refused_as_unauthorised_renews_the_token_once(
    seed: Seed, pub: Publishing
) -> None:
    pub.uploader.fail_next(UploadErrorCode.UNAUTHORIZED)

    outcome = pub.shorts.publish(seed.item.id, actor=USER)

    assert outcome.job.status is PublishStatus.SUCCEEDED
    assert pub.uploader.calls == 2
    assert _event(pub, PUBLISH_TOKEN_RENEWED).metadata["stage"] == "upload"


def test_a_revoked_authorization_changes_nothing_until_the_channel_is_authorised(
    seed: Seed, pub: Publishing
) -> None:
    pub.oauth.revoke_token(seed.channel.id)

    with pytest.raises(PublishAuthorizationError):
        pub.shorts.publish(seed.item.id, actor=USER)

    assert _item(seed).status is ContentStatus.APPROVED
    assert _jobs(seed) == []
    assert pub.uploader.calls == 0
    assert actions(pub.sink) == [PUBLISH_REQUESTED, PUBLISH_AUTHORIZATION_FAILED]

    pub.oauth.seed_token(
        OAuthToken(
            channel_id=seed.channel.id,
            access_token=SecretStr("new-access-token"),
            refresh_token=SecretStr("new-refresh-token"),
            scopes=frozenset(DEFAULT_MOCK_SCOPES),
            issued_at=T_PUBLISH,
            expires_at=T_PUBLISH + timedelta(hours=1),
        )
    )

    assert pub.shorts.publish(seed.item.id, actor=USER).job.status is (
        PublishStatus.SUCCEEDED
    )


# --- duplicates --------------------------------------------------------------------


def test_a_publish_that_is_already_running_is_refused(
    seed: Seed, pub: Publishing
) -> None:
    running = PublishJob.create(
        seed.approval, ContentType.SHORTS, _keys(seed).job_key, clock=clock
    ).start(clock=clock)
    with seed.database.transaction() as connection:
        PublishJobRepository(connection).add(running)

    with pytest.raises(DuplicatePublishError) as caught:
        pub.shorts.publish(seed.item.id, actor=USER)

    assert caught.value.duplicate_code is DuplicateErrorCode.IN_FLIGHT
    assert pub.uploader.calls == 0
    assert actions(pub.sink) == [PUBLISH_REQUESTED, PUBLISH_DUPLICATE_BLOCKED]


def test_the_same_video_is_not_published_twice_on_a_channel(
    seed: Seed, pub: Publishing
) -> None:
    video = seed.artifacts[ArtifactKind.VIDEO]
    twin = make_content_item(seed.channel, seed.profile, status=ContentStatus.APPROVED)
    twin_video = make_artifact(
        twin, ArtifactKind.VIDEO, sha256=video.sha256, size_bytes=video.size_bytes
    )
    twin_approval = make_approval_request(twin, [twin_video]).approve(USER)
    with seed.database.transaction() as connection:
        ContentItemRepository(connection).add(twin)
        ArtifactRepository(connection).add(twin_video)
        ApprovalRequestRepository(connection).add(twin_approval)
        PublishJobRepository(connection).add(
            PublishJob.create(
                twin_approval,
                ContentType.SHORTS,
                derive_publish_keys(
                    twin.id, twin_approval.id, seed.channel.youtube.channel_id, video.sha256
                ).job_key,
                clock=at(),
            )
            .start(clock=at())
            .succeed("zzzzzzzzzzz", clock=at())
        )

    with pytest.raises(DuplicatePublishError) as caught:
        pub.shorts.publish(seed.item.id, actor=USER)

    assert caught.value.duplicate_code is DuplicateErrorCode.SAME_VIDEO_PUBLISHED
    assert pub.uploader.calls == 0
    assert _item(seed).status is ContentStatus.APPROVED


# --- preconditions and gates --------------------------------------------------------


def test_publishing_is_off_unless_the_flag_is_on(seed: Seed, pub: Publishing) -> None:
    off = ShortsPublisher(seed.database, seed.storage, pub.executor, flags=FeatureFlags())

    with pytest.raises(PublishError) as caught:
        off.publish(seed.item.id, actor=USER)

    assert caught.value.publish_code is PublishErrorCode.PUBLISH_DISABLED
    assert caught.value.to_public().http_status == 403
    assert list(pub.sink.events()) == []
    assert _jobs(seed) == []


def test_longform_is_off_unless_its_flag_is_on(database: Database, tmp_path: Path) -> None:
    seed = seed_approved_item(
        database, tmp_path, content_type=ContentType.LONGFORM, duration_ms=600_000
    )
    pub = build_publishing(seed)
    locked = LongFormPublisher(
        seed.database,
        seed.storage,
        pub.executor,
        flags=FeatureFlags(publish_enabled=True, longform_enabled=False),
    )

    with pytest.raises(LongFormDisabledError):
        locked.publish(seed.item.id, actor=USER)

    assert list(pub.sink.events()) == []


def test_a_short_cannot_be_published_by_the_longform_publisher(
    seed: Seed, pub: Publishing
) -> None:
    with pytest.raises(PublishError) as caught:
        pub.longform.publish(seed.item.id, actor=USER)

    assert caught.value.publish_code is PublishErrorCode.WRONG_CONTENT_TYPE


def test_an_item_whose_approval_was_invalidated_is_not_published(
    seed: Seed, pub: Publishing
) -> None:
    with seed.database.transaction() as connection:
        ApprovalRequestRepository(connection).update(
            seed.approval.invalidate(), expected_status=seed.approval.status
        )

    with pytest.raises(PublishError) as caught:
        pub.shorts.publish(seed.item.id, actor=USER)

    assert caught.value.publish_code is PublishErrorCode.NOT_APPROVED
    assert pub.uploader.calls == 0


def test_a_changed_file_is_never_uploaded(seed: Seed, pub: Publishing) -> None:
    video = seed.artifacts[ArtifactKind.VIDEO]
    path = Path(url2pathname(urlsplit(video.uri).path))
    path.write_bytes(b"not the approved video")

    with pytest.raises(PublishError) as caught:
        pub.shorts.publish(seed.item.id, actor=USER)

    assert caught.value.publish_code is PublishErrorCode.STORAGE_CORRUPT
    assert pub.uploader.calls == 0
    assert _jobs(seed) == []
    assert _item(seed).status is ContentStatus.APPROVED


def test_a_short_longer_than_the_format_allows_is_refused(
    database: Database, tmp_path: Path
) -> None:
    seed = seed_approved_item(database, tmp_path, duration_ms=181_000)
    pub = build_publishing(seed)

    with pytest.raises(PublishError) as caught:
        pub.shorts.publish(seed.item.id, actor=USER)

    assert caught.value.publish_code is PublishErrorCode.DURATION_EXCEEDED
    assert pub.uploader.calls == 0


def test_an_unresolved_rights_risk_blocks_the_publish(
    seed: Seed, pub: Publishing
) -> None:
    with seed.database.transaction() as connection:
        RightsRecordRepository(connection).add(make_rights_record(seed.item))

    with pytest.raises(GateBlockedError):
        pub.shorts.publish(seed.item.id, actor=USER)

    blocked = _event(pub, PUBLISH_BLOCKED)
    assert "rights" in str(blocked.metadata["blocked_gates"])
    assert _item(seed).status is ContentStatus.APPROVED
    assert _jobs(seed) == []
    assert pub.uploader.calls == 0


def test_a_channel_not_allowed_long_uploads_cannot_publish_a_long_video(
    database: Database, tmp_path: Path
) -> None:
    seed = seed_approved_item(
        database,
        tmp_path,
        content_type=ContentType.LONGFORM,
        duration_ms=20 * 60 * 1_000,
    )
    pub = build_publishing(seed)
    pub.channels.register_channel(
        RemoteChannelInfo(
            youtube_channel_id=seed.channel.youtube.channel_id,
            title=seed.channel.title,
            fetched_at=T_PUBLISH,
            long_uploads_status="disallowed",
        )
    )

    with pytest.raises(PublishError) as caught:
        pub.longform.publish(seed.item.id, actor=USER)

    assert caught.value.publish_code is PublishErrorCode.LONG_UPLOADS_DISALLOWED
    assert _item(seed).status is ContentStatus.APPROVED
    assert _jobs(seed) == []
    assert pub.uploader.calls == 0
