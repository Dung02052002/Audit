"""Tests for YouTube OAuth (#145), Channel (#146) and Upload (#147) providers."""

import hashlib
from datetime import UTC, datetime

import pytest

from ai_youtube_agent.core.content_item import ContentType
from ai_youtube_agent.providers.mock_youtube_channel import (
    MockYouTubeChannelProvider,
)
from ai_youtube_agent.providers.mock_youtube_oauth import MockYouTubeOAuthProvider
from ai_youtube_agent.providers.mock_youtube_upload import (
    MockYouTubeUploadProvider,
    deterministic_youtube_video_id,
)
from ai_youtube_agent.providers.youtube_channel import (
    ChannelProviderErrorCode,
    RemoteChannelInfo,
    YouTubeChannelProvider,
    YouTubeChannelProviderError,
)
from ai_youtube_agent.providers.youtube_oauth import (
    OAuthErrorCode,
    OAuthScope,
    YouTubeOAuthError,
    YouTubeOAuthProvider,
)
from ai_youtube_agent.providers.youtube_upload import (
    UploadErrorCode,
    YouTubePrivacyStatus,
    YouTubeUploadError,
    YouTubeUploadPayload,
    YouTubeUploadProvider,
    YouTubeVideoSnippet,
    YouTubeVideoStatus,
)

T0 = datetime(2026, 10, 19, 12, 0, tzinfo=UTC)
YT_CHANNEL_ID = "UC" + "A" * 22


def _clock() -> datetime:
    return T0


def test_oauth_token_masks_secrets_and_checks_scopes_refresh_and_revocation() -> None:
    provider = MockYouTubeOAuthProvider(clock=_clock)
    assert isinstance(provider, YouTubeOAuthProvider)

    token = provider.get_token("ch-1", required_scopes=(OAuthScope.YOUTUBE_UPLOAD,))

    secret = token.access_token.get_secret_value()
    assert secret not in repr(token)
    assert secret not in str(token)
    redacted = token.redacted_dict()
    assert "access_token" not in redacted
    assert "refresh_token" not in redacted
    assert redacted["has_refresh_token"] is True

    refreshed = provider.refresh_token("ch-1")
    assert refreshed.access_token.get_secret_value() != secret

    provider.revoke_token("ch-1")
    with pytest.raises(YouTubeOAuthError) as revoked:
        provider.get_token("ch-1")
    assert revoked.value.oauth_code is OAuthErrorCode.TOKEN_REVOKED


def test_oauth_refuses_a_token_without_the_required_scope() -> None:
    provider = MockYouTubeOAuthProvider(
        clock=_clock, default_scopes=[OAuthScope.YOUTUBE_READONLY]
    )

    with pytest.raises(YouTubeOAuthError) as caught:
        provider.get_token("ch-1", required_scopes=(OAuthScope.YOUTUBE_UPLOAD,))

    assert caught.value.oauth_code is OAuthErrorCode.INSUFFICIENT_SCOPE


def test_channel_provider_fetches_and_verifies_channel_access() -> None:
    oauth = MockYouTubeOAuthProvider(clock=_clock)
    channels = MockYouTubeChannelProvider(oauth_provider=oauth, clock=_clock)
    assert isinstance(channels, YouTubeChannelProvider)
    info = RemoteChannelInfo(
        youtube_channel_id=YT_CHANNEL_ID,
        title="Audit Channel",
        fetched_at=T0,
        handle="@auditchannel",
        long_uploads_status="allowed",
    )
    channels.register_channel(info)

    fetched = channels.verify_channel_access(
        "ch-1", YT_CHANNEL_ID, token=oauth.get_token("ch-1")
    )

    assert fetched == info
    assert fetched.allows_long_uploads is True

    with pytest.raises(YouTubeChannelProviderError) as caught:
        channels.verify_channel_access(
            "ch-1", YT_CHANNEL_ID, token=oauth.get_token("ch-other")
        )
    assert caught.value.channel_code is ChannelProviderErrorCode.UNAUTHORIZED


def test_channel_provider_reports_a_disallowed_long_upload_status() -> None:
    channels = MockYouTubeChannelProvider(clock=_clock)
    channels.register_channel(
        RemoteChannelInfo(
            youtube_channel_id=YT_CHANNEL_ID,
            title="No long uploads",
            fetched_at=T0,
            long_uploads_status="disallowed",
        )
    )

    assert channels.fetch_channel(YT_CHANNEL_ID).allows_long_uploads is False


def _payload(video: bytes, thumbnail: bytes) -> YouTubeUploadPayload:
    return YouTubeUploadPayload(
        channel_id="ch-1",
        youtube_channel_id=YT_CHANNEL_ID,
        content_item_id="item-1",
        content_type=ContentType.SHORTS,
        approval_request_id="req-1",
        idempotency_key="upl:" + "a" * 64,
        video_artifact_id="art-v1",
        video_version=1,
        video_sha256=hashlib.sha256(video).hexdigest(),
        video_size_bytes=len(video),
        video_media_type="video/mp4",
        snippet=YouTubeVideoSnippet(
            title="Test Short #Shorts",
            description="Short description #Shorts",
            tags=("Shorts", "AI"),
            category_id="22",
            default_language="en",
            default_audio_language="en",
        ),
        status=YouTubeVideoStatus(
            privacy_status=YouTubePrivacyStatus.PRIVATE,
            self_declared_made_for_kids=False,
            contains_synthetic_media=True,
        ),
        metadata_artifact_id="art-m1",
        metadata_version=1,
        metadata_sha256="b" * 64,
        thumbnail_artifact_id="art-t1",
        thumbnail_sha256=hashlib.sha256(thumbnail).hexdigest(),
        thumbnail_media_type="image/png",
    )


def test_upload_provider_checks_digests_and_gives_a_deterministic_receipt() -> None:
    oauth = MockYouTubeOAuthProvider(clock=_clock)
    token = oauth.get_token("ch-1")
    uploader = MockYouTubeUploadProvider(reject_duplicates=True, clock=_clock)
    assert isinstance(uploader, YouTubeUploadProvider)
    video, thumbnail = b"fake-mp4-video-content", b"fake-png-thumb"
    payload = _payload(video, thumbnail)

    receipt = uploader.upload(
        payload, video_bytes=video, thumbnail_bytes=thumbnail, token=token
    )

    assert receipt.youtube_video_id == deterministic_youtube_video_id(
        payload.idempotency_key, payload.video_sha256
    )
    assert receipt.thumbnail_uploaded is True
    assert receipt.captions_uploaded is False
    assert receipt.contains_synthetic_media is True
    assert uploader.calls == 1

    with pytest.raises(YouTubeUploadError) as duplicate:
        uploader.upload(
            payload, video_bytes=video, thumbnail_bytes=thumbnail, token=token
        )
    assert duplicate.value.upload_code is UploadErrorCode.DUPLICATE_UPLOAD

    with pytest.raises(YouTubeUploadError) as corrupt:
        uploader.upload(
            payload, video_bytes=b"other bytes", thumbnail_bytes=thumbnail, token=token
        )
    assert corrupt.value.upload_code is UploadErrorCode.INVALID_MEDIA


def test_upload_provider_without_duplicate_rejection_returns_the_stored_receipt() -> (
    None
):
    oauth = MockYouTubeOAuthProvider(clock=_clock)
    token = oauth.get_token("ch-1")
    uploader = MockYouTubeUploadProvider(clock=_clock)
    video, thumbnail = b"fake-mp4-video-content", b"fake-png-thumb"
    payload = _payload(video, thumbnail)

    first = uploader.upload(
        payload, video_bytes=video, thumbnail_bytes=thumbnail, token=token
    )
    again = uploader.upload(
        payload, video_bytes=video, thumbnail_bytes=thumbnail, token=token
    )

    assert again == first
    assert len(uploader.uploads) == 1


def test_upload_provider_can_fail_several_times_in_a_row() -> None:
    oauth = MockYouTubeOAuthProvider(clock=_clock)
    token = oauth.get_token("ch-1")
    uploader = MockYouTubeUploadProvider(clock=_clock)
    video, thumbnail = b"fake-mp4-video-content", b"fake-png-thumb"
    payload = _payload(video, thumbnail)
    uploader.fail_next(UploadErrorCode.TIMEOUT, times=2)

    for _ in range(2):
        with pytest.raises(YouTubeUploadError) as caught:
            uploader.upload(
                payload, video_bytes=video, thumbnail_bytes=thumbnail, token=token
            )
        assert caught.value.upload_code is UploadErrorCode.TIMEOUT
        assert caught.value.retryable is True

    assert uploader.upload(
        payload, video_bytes=video, thumbnail_bytes=thumbnail, token=token
    )
    assert uploader.calls == 3
