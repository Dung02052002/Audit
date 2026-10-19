"""Tests for YouTube OAuth (#145), Channel (#146) and Upload (#147) providers."""

import hashlib
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import SecretStr

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
    OAuthToken,
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


def test_oauth_token_masks_secrets_and_validates_scopes_and_expiry() -> None:
    provider = MockYouTubeOAuthProvider(clock=lambda: T0)
    assert isinstance(provider, YouTubeOAuthProvider)

    token = provider.authorize_channel(
        "ch-1",
        access_token="secret-access-token-xyz",
        refresh_token="secret-refresh-token-xyz",
        scopes=(OAuthScope.YOUTUBE_UPLOAD, OAuthScope.YOUTUBE_READONLY),
        ttl=timedelta(hours=1),
    )

    assert "secret-access-token-xyz" not in repr(token)
    assert "secret-refresh-token-xyz" not in repr(token)
    redacted = token.redacted_dict()
    assert "access_token" not in redacted
    assert "refresh_token" not in redacted
    assert redacted["has_refresh_token"] is True

    fetched = provider.get_token(
        "ch-1", required_scopes=(OAuthScope.YOUTUBE_UPLOAD,)
    )
    assert fetched == token

    with pytest.raises(YouTubeOAuthError) as exc_info:
        provider.get_token(
            "ch-1", required_scopes=(OAuthScope.YT_ANALYTICS_READONLY,)
        )
    assert exc_info.value.oauth_code is OAuthErrorCode.INSUFFICIENT_SCOPE

    refreshed = provider.refresh_token("ch-1")
    assert (
        refreshed.access_token.get_secret_value()
        != token.access_token.get_secret_value()
    )

    provider.revoke_token("ch-1")
    with pytest.raises(YouTubeOAuthError) as exc_revoked:
        provider.get_token("ch-1")
    assert exc_revoked.value.oauth_code is OAuthErrorCode.TOKEN_REVOKED


def test_channel_provider_fetches_and_verifies_channel_access() -> None:
    oauth = MockYouTubeOAuthProvider(clock=lambda: T0)
    token = oauth.authorize_channel("ch-1")
    channels = MockYouTubeChannelProvider(oauth_provider=oauth, clock=lambda: T0)
    assert isinstance(channels, YouTubeChannelProvider)

    info = RemoteChannelInfo(
        youtube_channel_id=YT_CHANNEL_ID,
        title="Audit Channel",
        fetched_at=T0,
        handle="@auditchannel",
        long_uploads_status="allowed",
    )
    channels.register_channel(info)

    fetched = channels.verify_channel_access("ch-1", YT_CHANNEL_ID, token=token)
    assert fetched == info
    assert fetched.allows_long_uploads is True

    wrong_token = OAuthToken(
        channel_id="ch-other",
        access_token=SecretStr("tok-other"),
        refresh_token=None,
        scopes=frozenset({OAuthScope.YOUTUBE_READONLY}),
        issued_at=T0,
        expires_at=T0 + timedelta(hours=1),
    )
    with pytest.raises(YouTubeChannelProviderError) as exc_info:
        channels.verify_channel_access("ch-1", YT_CHANNEL_ID, token=wrong_token)
    assert exc_info.value.channel_code is ChannelProviderErrorCode.UNAUTHORIZED


def test_upload_provider_validates_digests_and_returns_deterministic_receipt() -> None:
    oauth = MockYouTubeOAuthProvider(clock=lambda: T0)
    token = oauth.authorize_channel("ch-1")
    uploader = MockYouTubeUploadProvider(
        reject_duplicates=True, clock=lambda: T0
    )
    assert isinstance(uploader, YouTubeUploadProvider)

    video_bytes = b"fake-mp4-video-content"
    video_sha = hashlib.sha256(video_bytes).hexdigest()
    meta_sha = "b" * 64
    thumb_bytes = b"fake-png-thumb"
    thumb_sha = hashlib.sha256(thumb_bytes).hexdigest()

    payload = YouTubeUploadPayload(
        channel_id="ch-1",
        youtube_channel_id=YT_CHANNEL_ID,
        content_item_id="item-1",
        content_type=ContentType.SHORTS,
        approval_request_id="req-1",
        idempotency_key="publish:item-1:req-1",
        video_artifact_id="art-v1",
        video_version=1,
        video_sha256=video_sha,
        video_size_bytes=len(video_bytes),
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
        metadata_sha256=meta_sha,
        thumbnail_artifact_id="art-t1",
        thumbnail_sha256=thumb_sha,
        thumbnail_media_type="image/png",
    )

    receipt = uploader.upload(
        payload,
        video_bytes=video_bytes,
        thumbnail_bytes=thumb_bytes,
        token=token,
    )
    assert receipt.youtube_video_id == deterministic_youtube_video_id(
        payload.idempotency_key, video_sha
    )
    assert receipt.thumbnail_uploaded is True
    assert receipt.captions_uploaded is False
    assert receipt.contains_synthetic_media is True

    with pytest.raises(YouTubeUploadError) as dup_exc:
        uploader.upload(
            payload,
            video_bytes=video_bytes,
            thumbnail_bytes=thumb_bytes,
            token=token,
        )
    assert dup_exc.value.upload_code is UploadErrorCode.DUPLICATE_UPLOAD
