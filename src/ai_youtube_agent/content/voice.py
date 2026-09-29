"""Voice entity (Prompt Pack v8, prompt #018), context C7 Voice & Audio.

Two frozen entities:

- ``VoiceProfile``: the user-controlled voice configuration of one channel. It
  has a stable id, ``channel_id``, the TTS ``provider`` name, the provider's
  ``voice_id``, a BCP-47 ``language`` and an optional ``speaking_style``. Like
  the strategy, only a user may create or change it
  (``VoiceChangeNotAllowedError``), and every real change returns a new
  profile with ``version`` + 1. Pronunciation and style rules come with #087.
- ``AudioMetadata``: what is known about one generated audio file. It points to
  the ``Artifact`` of kind audio that holds the file (uri, checksum, size and
  media type), the exact ``Script`` version that was voiced, and the voice
  profile id and version used, and it records the provider and the duration in
  whole milliseconds.

No cost is stored here: TTS cost is recorded by #092 and the Cost entity
(#025). The TTS provider interface is #086.
"""

import re
import uuid
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any

from ai_youtube_agent.content.script import Script
from ai_youtube_agent.content.strategy import LANGUAGE_TAG_PATTERN
from ai_youtube_agent.core.artifact import Artifact, ArtifactKind
from ai_youtube_agent.core.audit import Actor, ActorKind
from ai_youtube_agent.core.errors import DomainError

PROVIDER_PATTERN = re.compile(r"^[a-z][a-z0-9_-]*$")
Clock = Callable[[], datetime]
VOICE_SETTINGS = frozenset({"provider", "voice_id", "language", "speaking_style"})


class VoiceChangeNotAllowedError(DomainError):
    default_code = "domain.voice_change_not_allowed"
    default_user_message = "Only a user can change the voice configuration."


@dataclass(frozen=True)
class VoiceProfile:
    id: str
    channel_id: str
    provider: str
    voice_id: str
    language: str
    speaking_style: str | None
    version: int
    updated_by: Actor
    created_at: datetime
    updated_at: datetime

    def __post_init__(self) -> None:
        _require_ids(self, "id", "channel_id")
        _require_provider(self.provider)
        if not self.voice_id or any(char.isspace() for char in self.voice_id):
            raise ValueError("voice_id must not be empty or contain whitespace")
        if not LANGUAGE_TAG_PATTERN.match(self.language):
            raise ValueError(
                f"language {self.language!r} must be a BCP-47 tag such as 'en-US'"
            )
        if self.speaking_style is not None and not self.speaking_style.strip():
            raise ValueError("speaking_style must not be empty")
        _require_whole(self.version, "version")
        _ensure_user(self.updated_by)
        _require_utc(self.created_at, "created_at")
        _require_utc(self.updated_at, "updated_at")
        if self.updated_at < self.created_at:
            raise ValueError("updated_at must not be earlier than created_at")

    @classmethod
    def create(
        cls,
        channel_id: str,
        *,
        provider: str,
        voice_id: str,
        language: str,
        speaking_style: str | None = None,
        actor: Actor,
        clock: Clock | None = None,
    ) -> "VoiceProfile":
        _ensure_user(actor)
        now = _now(clock)
        return cls(
            id=uuid.uuid4().hex,
            channel_id=channel_id,
            provider=provider,
            voice_id=voice_id,
            language=language,
            speaking_style=_strip(speaking_style),
            version=1,
            updated_by=actor,
            created_at=now,
            updated_at=now,
        )

    def update(
        self, *, actor: Actor, clock: Clock | None = None, **changes: Any
    ) -> "VoiceProfile":
        unknown = set(changes) - VOICE_SETTINGS
        if unknown:
            raise ValueError(f"not a voice setting: {', '.join(sorted(unknown))}")
        _ensure_user(actor)
        if "speaking_style" in changes:
            changes["speaking_style"] = _strip(changes["speaking_style"])
        changed = {
            name: value
            for name, value in changes.items()
            if value != getattr(self, name)
        }
        if not changed:
            return self
        return replace(
            self,
            **changed,
            version=self.version + 1,
            updated_by=actor,
            updated_at=_now(clock),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "channel_id": self.channel_id,
            "provider": self.provider,
            "voice_id": self.voice_id,
            "language": self.language,
            "speaking_style": self.speaking_style,
            "version": self.version,
            "updated_by": {
                "kind": self.updated_by.kind.value,
                "id": self.updated_by.id,
            },
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }


@dataclass(frozen=True)
class AudioMetadata:
    id: str
    artifact_id: str
    script_id: str
    voice_profile_id: str
    voice_profile_version: int
    provider: str
    duration_ms: int
    created_at: datetime

    def __post_init__(self) -> None:
        _require_ids(self, "id", "artifact_id", "script_id", "voice_profile_id")
        _require_whole(self.voice_profile_version, "voice_profile_version")
        _require_provider(self.provider)
        _require_whole(self.duration_ms, "duration_ms")
        _require_utc(self.created_at, "created_at")

    @classmethod
    def create(
        cls,
        artifact: Artifact,
        script: Script,
        voice: VoiceProfile,
        *,
        duration_ms: int,
        clock: Clock | None = None,
    ) -> "AudioMetadata":
        if artifact.kind is not ArtifactKind.AUDIO:
            raise ValueError(
                f"audio metadata needs an audio artifact, not {artifact.kind.value}"
            )
        if artifact.content_item_id != script.content_item_id:
            raise ValueError("the artifact and the script belong to different items")
        return cls(
            id=uuid.uuid4().hex,
            artifact_id=artifact.id,
            script_id=script.id,
            voice_profile_id=voice.id,
            voice_profile_version=voice.version,
            provider=voice.provider,
            duration_ms=duration_ms,
            created_at=_now(clock),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "artifact_id": self.artifact_id,
            "script_id": self.script_id,
            "voice_profile_id": self.voice_profile_id,
            "voice_profile_version": self.voice_profile_version,
            "provider": self.provider,
            "duration_ms": self.duration_ms,
            "created_at": self.created_at.isoformat(),
        }


def _ensure_user(actor: Actor) -> None:
    if actor.kind is not ActorKind.USER:
        raise VoiceChangeNotAllowedError(
            f"actor {actor.kind.value}:{actor.id} may not change the voice"
        )


def _require_ids(entity: object, *names: str) -> None:
    for name in names:
        value = getattr(entity, name)
        if not value or not value.strip():
            raise ValueError(f"{name} must not be empty")


def _require_provider(provider: str) -> None:
    if not PROVIDER_PATTERN.match(provider):
        raise ValueError(
            f"provider {provider!r} must be a lowercase name such as 'mock_tts'"
        )


def _require_whole(value: object, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{name} must be a whole number of 1 or more")


def _require_utc(moment: datetime, name: str) -> None:
    if moment.utcoffset() != timedelta(0):
        raise ValueError(f"{name} must be timezone-aware UTC")


def _strip(value: str | None) -> str | None:
    return value.strip() if value is not None else None


def _now(clock: Clock | None) -> datetime:
    return clock() if clock else datetime.now(UTC)
