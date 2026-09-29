import dataclasses
from datetime import UTC, datetime, timedelta, timezone

import pytest

from ai_youtube_agent.content.script import Script
from ai_youtube_agent.content.voice import (
    AudioMetadata,
    VoiceChangeNotAllowedError,
    VoiceProfile,
)
from ai_youtube_agent.core.artifact import Artifact, ArtifactKind
from ai_youtube_agent.core.audit import Actor, ActorKind
from ai_youtube_agent.core.errors import DomainError

T0 = datetime(2026, 9, 29, 10, 0, tzinfo=UTC)
T1 = T0 + timedelta(minutes=5)
USER = Actor(ActorKind.USER, "owner-1")
AI = Actor(ActorKind.AI, "voice-agent")
SYSTEM = Actor(ActorKind.SYSTEM, "scheduler")
VOICE = {"provider": "mock_tts", "voice_id": "vi-female-01", "language": "vi"}
FILE = {
    "uri": "store://items/item-1/audio/v1.mp3",
    "sha256": "b" * 64,
    "size_bytes": 480_000,
    "media_type": "audio/mpeg",
}


def at(moment: datetime):
    return lambda: moment


def new_voice(**overrides) -> VoiceProfile:
    return VoiceProfile.create(
        "channel-1", **{**VOICE, **overrides}, actor=USER, clock=at(T0)
    )


def rebuild(entity, **changes):
    values = {f.name: getattr(entity, f.name) for f in dataclasses.fields(entity)}
    return type(entity)(**{**values, **changes})


def audio_parts(kind: ArtifactKind = ArtifactKind.AUDIO, script_item="item-1"):
    artifact = Artifact.create("item-1", kind, **FILE, clock=at(T0))
    script = Script.create(script_item, "Hello and welcome.", clock=at(T0))
    return artifact, script, new_voice()


# Voice profile


def test_create_builds_a_first_version_owned_by_the_user() -> None:
    voice = new_voice(speaking_style="  warm, calm  ")

    assert len(voice.id) == 32
    assert voice.channel_id == "channel-1"
    assert (voice.provider, voice.voice_id, voice.language) == (
        "mock_tts",
        "vi-female-01",
        "vi",
    )
    assert voice.speaking_style == "warm, calm"
    assert voice.version == 1
    assert voice.updated_by == USER
    assert voice.created_at == voice.updated_at == T0


def test_speaking_style_is_optional() -> None:
    assert new_voice().speaking_style is None


def test_create_defaults_to_the_current_utc_time() -> None:
    before = datetime.now(UTC)
    voice = VoiceProfile.create("channel-1", **VOICE, actor=USER)
    assert before <= voice.created_at <= datetime.now(UTC)


@pytest.mark.parametrize(
    "overrides",
    [
        {"provider": ""},
        {"provider": "Mock TTS"},
        {"provider": "1tts"},
        {"voice_id": ""},
        {"voice_id": "vi female"},
        {"language": ""},
        {"language": "Vietnamese"},
        {"language": "vi_VN"},
        {"speaking_style": "  "},
    ],
)
def test_create_rejects_malformed_settings(overrides) -> None:
    with pytest.raises(ValueError):
        new_voice(**overrides)


def test_create_needs_a_channel() -> None:
    with pytest.raises(ValueError):
        VoiceProfile.create("", **VOICE, actor=USER)


@pytest.mark.parametrize("actor", [AI, SYSTEM])
def test_only_a_user_can_create_a_voice_profile(actor: Actor) -> None:
    with pytest.raises(VoiceChangeNotAllowedError):
        VoiceProfile.create("channel-1", **VOICE, actor=actor)


def test_refusal_is_a_domain_error_with_a_safe_message() -> None:
    with pytest.raises(DomainError) as info:
        VoiceProfile.create("channel-1", **VOICE, actor=AI)
    assert info.value.code == "domain.voice_change_not_allowed"
    assert info.value.user_message == "Only a user can change the voice configuration."


@pytest.mark.parametrize(
    "changes",
    [
        {"id": ""},
        {"version": 0},
        {"updated_by": AI},
        {"created_at": datetime(2026, 9, 29, 10, 0)},
        {"updated_at": T0.astimezone(timezone(timedelta(hours=7)))},
        {"updated_at": T0 - timedelta(seconds=1)},
    ],
)
def test_voice_profile_rejects_invalid_state(changes) -> None:
    with pytest.raises((ValueError, DomainError)):
        rebuild(new_voice(), **changes)


def test_voice_profile_is_frozen() -> None:
    with pytest.raises(dataclasses.FrozenInstanceError):
        new_voice().voice_id = "other"  # type: ignore[misc]


def test_update_returns_a_new_version() -> None:
    voice = new_voice()

    updated = voice.update(
        actor=USER, clock=at(T1), voice_id="vi-male-02", speaking_style=" energetic "
    )

    assert updated.voice_id == "vi-male-02"
    assert updated.speaking_style == "energetic"
    assert updated.version == 2
    assert updated.updated_at == T1
    assert (updated.id, updated.channel_id, updated.created_at) == (
        voice.id,
        voice.channel_id,
        voice.created_at,
    )
    assert (voice.version, voice.voice_id) == (1, "vi-female-01")


def test_update_without_a_change_returns_the_same_profile() -> None:
    voice = new_voice()
    assert voice.update(actor=USER, clock=at(T1)) is voice
    assert voice.update(actor=USER, clock=at(T1), language="vi") is voice


def test_update_can_clear_the_speaking_style() -> None:
    voice = new_voice(speaking_style="calm")
    assert voice.update(actor=USER, speaking_style=None).speaking_style is None


@pytest.mark.parametrize("actor", [AI, SYSTEM])
def test_ai_and_system_cannot_change_the_voice(actor: Actor) -> None:
    with pytest.raises(VoiceChangeNotAllowedError):
        new_voice().update(actor=actor, voice_id="vi-male-02")


def test_ai_is_refused_even_when_nothing_would_change() -> None:
    with pytest.raises(VoiceChangeNotAllowedError):
        new_voice().update(actor=AI, language="vi")


@pytest.mark.parametrize("name", ["id", "channel_id", "version", "created_at"])
def test_update_only_accepts_voice_settings(name: str) -> None:
    with pytest.raises(ValueError, match="not a voice setting"):
        new_voice().update(actor=USER, **{name: "x"})


def test_update_validates_new_values() -> None:
    with pytest.raises(ValueError):
        new_voice().update(actor=USER, clock=at(T1), language="Vietnamese")


# Audio metadata


def test_audio_metadata_links_file_script_and_voice() -> None:
    artifact, script, voice = audio_parts()

    audio = AudioMetadata.create(
        artifact, script, voice, duration_ms=30_250, clock=at(T1)
    )

    assert len(audio.id) == 32
    assert audio.artifact_id == artifact.id
    assert audio.script_id == script.id
    assert audio.voice_profile_id == voice.id
    assert audio.voice_profile_version == 1
    assert audio.provider == "mock_tts"
    assert audio.duration_ms == 30_250
    assert audio.created_at == T1


def test_audio_metadata_keeps_the_voice_version_it_was_made_with() -> None:
    artifact, script, voice = audio_parts()
    voice = voice.update(actor=USER, clock=at(T1), voice_id="vi-male-02")

    audio = AudioMetadata.create(artifact, script, voice, duration_ms=1_000)

    assert audio.voice_profile_version == 2


@pytest.mark.parametrize(
    "kind", [k for k in ArtifactKind if k is not ArtifactKind.AUDIO]
)
def test_audio_metadata_needs_an_audio_artifact(kind: ArtifactKind) -> None:
    artifact, script, voice = audio_parts(kind)
    with pytest.raises(ValueError, match="audio artifact"):
        AudioMetadata.create(artifact, script, voice, duration_ms=1_000)


def test_artifact_and_script_must_belong_to_the_same_item() -> None:
    artifact, script, voice = audio_parts(script_item="item-2")
    with pytest.raises(ValueError, match="different items"):
        AudioMetadata.create(artifact, script, voice, duration_ms=1_000)


@pytest.mark.parametrize("duration_ms", [0, -1, 1.5, True, "1000"])
def test_duration_must_be_whole_positive_milliseconds(duration_ms) -> None:
    artifact, script, voice = audio_parts()
    with pytest.raises(ValueError):
        AudioMetadata.create(artifact, script, voice, duration_ms=duration_ms)


@pytest.mark.parametrize(
    "changes",
    [
        {"id": ""},
        {"artifact_id": ""},
        {"script_id": " "},
        {"voice_profile_id": ""},
        {"voice_profile_version": 0},
        {"provider": "Mock"},
        {"created_at": datetime(2026, 9, 29, 10, 0)},
    ],
)
def test_audio_metadata_rejects_invalid_state(changes) -> None:
    audio = AudioMetadata.create(*audio_parts(), duration_ms=1_000, clock=at(T0))
    with pytest.raises(ValueError):
        rebuild(audio, **changes)


def test_audio_metadata_is_frozen() -> None:
    audio = AudioMetadata.create(*audio_parts(), duration_ms=1_000)
    with pytest.raises(dataclasses.FrozenInstanceError):
        audio.duration_ms = 2  # type: ignore[misc]


# Serialisation


def test_as_dict_is_json_friendly() -> None:
    artifact, script, voice = audio_parts()
    audio = AudioMetadata.create(
        artifact, script, voice, duration_ms=1_500, clock=at(T0)
    )
    stamp = "2026-09-29T10:00:00+00:00"

    assert voice.as_dict() == {
        "id": voice.id,
        "channel_id": "channel-1",
        "provider": "mock_tts",
        "voice_id": "vi-female-01",
        "language": "vi",
        "speaking_style": None,
        "version": 1,
        "updated_by": {"kind": "user", "id": "owner-1"},
        "created_at": stamp,
        "updated_at": stamp,
    }
    assert audio.as_dict() == {
        "id": audio.id,
        "artifact_id": artifact.id,
        "script_id": script.id,
        "voice_profile_id": voice.id,
        "voice_profile_version": 1,
        "provider": "mock_tts",
        "duration_ms": 1_500,
        "created_at": stamp,
    }
