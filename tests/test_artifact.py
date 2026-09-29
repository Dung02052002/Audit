import dataclasses
from datetime import UTC, datetime, timedelta, timezone

import pytest

from ai_youtube_agent.core.artifact import Artifact, ArtifactKind

T0 = datetime(2026, 9, 29, 10, 0, tzinfo=UTC)
T1 = T0 + timedelta(minutes=5)
SHA_A = "a" * 64
SHA_B = "0123456789abcdef" * 4
FILE_A = {
    "uri": "store://items/item-1/video/v1.mp4",
    "sha256": SHA_A,
    "size_bytes": 1_048_576,
    "media_type": "video/mp4",
}
FILE_B = {
    "uri": "store://items/item-1/video/v2.mp4",
    "sha256": SHA_B,
    "size_bytes": 2_097_152,
    "media_type": "video/mp4",
}


def at(moment: datetime):
    return lambda: moment


def new_artifact(kind: ArtifactKind = ArtifactKind.VIDEO, **overrides) -> Artifact:
    return Artifact.create("item-1", kind, **{**FILE_A, **overrides}, clock=at(T0))


# Kinds


def test_kinds_match_prompt_016() -> None:
    assert [k.value for k in ArtifactKind] == [
        "video",
        "audio",
        "subtitles",
        "thumbnail",
        "metadata",
    ]


# Creating the first version


@pytest.mark.parametrize(
    ("kind", "media_type"),
    [
        (ArtifactKind.VIDEO, "video/mp4"),
        (ArtifactKind.AUDIO, "audio/mpeg"),
        (ArtifactKind.SUBTITLES, "text/vtt"),
        (ArtifactKind.THUMBNAIL, "image/png"),
        (ArtifactKind.METADATA, "application/json"),
    ],
)
def test_create_builds_version_1_for_each_kind(
    kind: ArtifactKind, media_type: str
) -> None:
    artifact = new_artifact(kind, media_type=media_type)

    assert len(artifact.id) == 32
    assert artifact.content_item_id == "item-1"
    assert artifact.kind is kind
    assert artifact.version == 1
    assert artifact.uri == FILE_A["uri"]
    assert artifact.sha256 == SHA_A
    assert artifact.size_bytes == 1_048_576
    assert artifact.media_type == media_type
    assert artifact.created_at == T0


def test_create_gives_each_artifact_its_own_id() -> None:
    assert new_artifact().id != new_artifact().id


def test_create_defaults_to_the_current_utc_time() -> None:
    before = datetime.now(UTC)
    artifact = Artifact.create("item-1", ArtifactKind.AUDIO, **FILE_A)
    assert before <= artifact.created_at <= datetime.now(UTC)
    assert artifact.created_at.utcoffset() == timedelta(0)


@pytest.mark.parametrize(
    "overrides",
    [
        {"uri": ""},
        {"uri": "store://my file.mp4"},
        {"sha256": ""},
        {"sha256": "A" * 64},
        {"sha256": "a" * 63},
        {"sha256": "g" * 64},
        {"size_bytes": 0},
        {"size_bytes": -1},
        {"size_bytes": 1.5},
        {"size_bytes": True},
        {"media_type": ""},
        {"media_type": "mp4"},
        {"media_type": "Video/MP4"},
        {"media_type": "video/"},
    ],
)
def test_create_rejects_malformed_file_details(overrides) -> None:
    with pytest.raises(ValueError):
        new_artifact(**overrides)


def test_create_needs_a_content_item() -> None:
    with pytest.raises(ValueError):
        Artifact.create("", ArtifactKind.VIDEO, **FILE_A)


@pytest.mark.parametrize("kind", ["video", "report", None])
def test_kind_must_be_an_artifact_kind(kind) -> None:
    with pytest.raises(TypeError):
        Artifact.create("item-1", kind, **FILE_A)


@pytest.mark.parametrize(
    "changes",
    [
        {"id": ""},
        {"version": 0},
        {"version": True},
        {"created_at": datetime(2026, 9, 29, 10, 0)},
        {"created_at": T0.astimezone(timezone(timedelta(hours=7)))},
    ],
)
def test_artifact_rejects_invalid_state(changes) -> None:
    values = {
        f.name: getattr(new_artifact(), f.name) for f in dataclasses.fields(Artifact)
    }
    with pytest.raises(ValueError):
        Artifact(**{**values, **changes})


def test_artifact_is_frozen() -> None:
    artifact = new_artifact()
    with pytest.raises(dataclasses.FrozenInstanceError):
        artifact.sha256 = SHA_B  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        artifact.version = 2  # type: ignore[misc]


# New versions


def test_next_version_is_a_new_record_and_keeps_the_old_one() -> None:
    first = new_artifact()

    second = first.next_version(**FILE_B, clock=at(T1))

    assert second.id != first.id
    assert second.version == 2
    assert (second.content_item_id, second.kind) == ("item-1", ArtifactKind.VIDEO)
    assert second.uri == FILE_B["uri"]
    assert second.sha256 == SHA_B
    assert second.size_bytes == 2_097_152
    assert second.created_at == T1
    assert first.version == 1
    assert first.sha256 == SHA_A
    assert first.created_at == T0


def test_versions_keep_counting_up() -> None:
    third = (
        new_artifact()
        .next_version(**FILE_B, clock=at(T1))
        .next_version(**{**FILE_A, "sha256": "c" * 64}, clock=at(T1))
    )
    assert third.version == 3


def test_next_version_refuses_unchanged_content() -> None:
    with pytest.raises(ValueError, match="different sha256"):
        new_artifact().next_version(**{**FILE_B, "sha256": SHA_A}, clock=at(T1))


def test_next_version_validates_the_new_file() -> None:
    with pytest.raises(ValueError):
        new_artifact().next_version(**{**FILE_B, "size_bytes": 0}, clock=at(T1))


# Serialisation


def test_as_dict_is_json_friendly() -> None:
    artifact = new_artifact(ArtifactKind.METADATA, media_type="application/json")

    assert artifact.as_dict() == {
        "id": artifact.id,
        "content_item_id": "item-1",
        "kind": "metadata",
        "version": 1,
        "uri": "store://items/item-1/video/v1.mp4",
        "sha256": SHA_A,
        "size_bytes": 1_048_576,
        "media_type": "application/json",
        "created_at": "2026-09-29T10:00:00+00:00",
    }
