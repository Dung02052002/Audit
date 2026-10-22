"""Tests for PublishMetadataMapper (M-148) and the publish preconditions (M-149, M-150).

The end-to-end publish flows are in ``tests/test_publish_scenarios.py``.
"""

import dataclasses
import json
from pathlib import Path

import pytest

from ai_youtube_agent.core.artifact import ArtifactKind
from ai_youtube_agent.core.content_item import ContentType
from ai_youtube_agent.core.db.database import Database
from ai_youtube_agent.pipeline.idempotency import is_upload_key
from ai_youtube_agent.pipeline.publish_context import PublishContextLoader
from ai_youtube_agent.pipeline.publish_metadata_mapper import (
    PublishMetadataErrorCode,
    PublishMetadataMapper,
    PublishMetadataMappingError,
)
from ai_youtube_agent.providers.youtube_upload import YouTubePrivacyStatus
from publish_helpers import Seed, default_metadata, seed_approved_item

MAPPER = PublishMetadataMapper()


@pytest.fixture
def seed(database: Database, tmp_path: Path) -> Seed:
    return seed_approved_item(database, tmp_path, with_thumbnail=True)


def _map(seed: Seed, content_type: ContentType = ContentType.SHORTS, **overrides):
    video = seed.artifacts[ArtifactKind.VIDEO]
    metadata = seed.artifacts[ArtifactKind.METADATA]
    arguments = dict(
        channel=seed.channel,
        content_type=content_type,
        approval=seed.approval,
        video_artifact=video,
        metadata_artifact=metadata,
        metadata_source=seed.storage.read(metadata.uri),
        thumbnail_artifact=seed.artifacts.get(ArtifactKind.THUMBNAIL),
    )
    arguments.update(overrides)
    return MAPPER.map_to_payload(**arguments)


def test_a_valid_short_maps_to_a_private_payload_bound_to_its_artifacts(
    seed: Seed,
) -> None:
    payload = _map(seed)

    video = seed.artifacts[ArtifactKind.VIDEO]
    assert payload.status.privacy_status is YouTubePrivacyStatus.PRIVATE
    assert payload.channel_id == seed.channel.id
    assert payload.youtube_channel_id == seed.channel.youtube.channel_id
    assert payload.approval_request_id == seed.approval.id
    assert payload.video_artifact_id == video.id
    assert payload.video_sha256 == video.sha256
    assert payload.thumbnail_artifact_id == seed.artifacts[ArtifactKind.THUMBNAIL].id
    assert payload.snippet.title.endswith("#Shorts")
    assert payload.status.contains_synthetic_media is True


def test_the_payload_key_is_the_one_given_or_the_job_key(seed: Seed) -> None:
    keys = (
        PublishContextLoader(
            seed.database, seed.storage, content_type=ContentType.SHORTS
        )
        .load(seed.item.id)
        .keys
    )

    assert (
        _map(seed, idempotency_key=keys.upload_key).idempotency_key == keys.upload_key
    )
    assert _map(seed).idempotency_key == keys.job_key
    assert is_upload_key(keys.upload_key)


def test_a_short_without_the_shorts_marker_is_refused(seed: Seed) -> None:
    source = {
        **default_metadata(ContentType.SHORTS, seed.item, seed.channel),
        "title": "No marker here",
        "description": "Plain description",
        "tags": ["Money"],
    }

    with pytest.raises(PublishMetadataMappingError) as caught:
        _map(seed, metadata_source=json.dumps(source).encode("utf-8"))

    assert caught.value.mapping_code is PublishMetadataErrorCode.SHORTS_MARKER_MISSING


def test_an_artifact_that_is_not_the_approved_one_is_refused(seed: Seed) -> None:
    video = seed.artifacts[ArtifactKind.VIDEO]

    with pytest.raises(PublishMetadataMappingError) as caught:
        _map(seed, video_artifact=dataclasses.replace(video, sha256="0" * 64))

    assert caught.value.mapping_code is PublishMetadataErrorCode.BINDING_MISMATCH
    assert caught.value.to_public().http_status == 409


def test_metadata_of_another_channel_is_refused(seed: Seed) -> None:
    source = {
        **default_metadata(ContentType.SHORTS, seed.item, seed.channel),
        "channel_id": "another-channel",
    }

    with pytest.raises(PublishMetadataMappingError) as caught:
        _map(seed, metadata_source=json.dumps(source).encode("utf-8"))

    assert caught.value.mapping_code is PublishMetadataErrorCode.BINDING_MISMATCH


def test_metadata_that_is_not_json_is_refused(seed: Seed) -> None:
    with pytest.raises(PublishMetadataMappingError) as caught:
        _map(seed, metadata_source=b"{not json")

    assert caught.value.mapping_code is PublishMetadataErrorCode.INVALID_JSON


def test_longform_chapters_need_00_00_in_the_description(
    database: Database, tmp_path: Path
) -> None:
    long_seed = seed_approved_item(
        database, tmp_path, content_type=ContentType.LONGFORM, duration_ms=600_000
    )
    good = default_metadata(ContentType.LONGFORM, long_seed.item, long_seed.channel)
    bad = {**good, "description": "A deep dive without timestamps."}

    assert _map(
        long_seed,
        ContentType.LONGFORM,
        metadata_source=json.dumps(good).encode("utf-8"),
        thumbnail_artifact=None,
    ).snippet.description.startswith("A deep dive")
    with pytest.raises(PublishMetadataMappingError) as caught:
        _map(
            long_seed,
            ContentType.LONGFORM,
            metadata_source=json.dumps(bad).encode("utf-8"),
            thumbnail_artifact=None,
        )

    assert caught.value.mapping_code is PublishMetadataErrorCode.CHAPTERS_MISSING


def test_the_loader_returns_the_bound_artifacts_and_the_two_keys(seed: Seed) -> None:
    context = PublishContextLoader(
        seed.database, seed.storage, content_type=ContentType.SHORTS
    ).load(seed.item.id)

    assert context.approval.id == seed.approval.id
    assert context.video_artifact.id == seed.artifacts[ArtifactKind.VIDEO].id
    assert context.thumbnail_artifact is not None
    assert context.subtitles_artifact is None
    assert context.existing_job is None
    assert context.keys.job_key.startswith("pub:")
    assert context.keys.upload_key.startswith("upl:")
