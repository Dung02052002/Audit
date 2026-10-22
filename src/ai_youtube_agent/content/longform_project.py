"""LongFormProject entity (Prompt Pack v8, prompt J-105), context C8 Video Production.

A ``LongFormProject`` is the production plan of one LongForm content item:

- One project per content item (``content_item_id``), one mutable row guarded by
  ``version`` (optimistic lock).
- Bound to the exact inputs it is produced from: the script version
  (``script_id``, ``script_version``) and the audio artifact version
  (``audio_artifact_id``, ``audio_version``, ``audio_sha256``,
  ``audio_duration_ms``). ``strategy_version`` and ``config`` record the channel
  strategy and the LongForm configuration (``LongFormConfig``, #105) read when
  the project was made or last re-bound.
- ``timeline`` is the plan of scenes, chapters, captions and music
  (``LongFormTimeline``, #106). It is always as long as the audio, and ``status``
  follows it: ``draft`` while it has no scenes, ``planned`` once it has scenes
  that cover the audio, and ``ready`` when it also has a background per scene
  (#108), chapters when ``config.chapters`` is on (#107), and valid captions
  when ``config.captions`` is on (#109).
- The audio length must not exceed ``config.max_seconds``. Being shorter than
  ``config.min_seconds`` is reported via ``audio_within_target``.
"""

import uuid
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any

from ai_youtube_agent.content.longform_config import LongFormConfig
from ai_youtube_agent.content.longform_timeline import LongFormTimeline
from ai_youtube_agent.core.artifact import SHA256_PATTERN
from ai_youtube_agent.core.audit import Actor, ActorKind

Clock = Callable[[], datetime]


class LongFormProjectStatus(StrEnum):
    DRAFT = "draft"
    PLANNED = "planned"
    READY = "ready"


@dataclass(frozen=True)
class LongFormProject:
    id: str
    content_item_id: str
    script_id: str
    script_version: int
    audio_artifact_id: str
    audio_version: int
    audio_sha256: str
    audio_duration_ms: int
    strategy_version: int
    config: LongFormConfig
    timeline: LongFormTimeline
    status: LongFormProjectStatus
    version: int
    created_by: Actor
    created_at: datetime
    updated_by: Actor
    updated_at: datetime

    def __post_init__(self) -> None:
        for name in ("id", "content_item_id", "script_id", "audio_artifact_id"):
            _require_text(name, getattr(self, name))
        for name in (
            "script_version",
            "audio_version",
            "audio_duration_ms",
            "strategy_version",
            "version",
        ):
            _require_whole(name, getattr(self, name))
        if not isinstance(self.audio_sha256, str) or not SHA256_PATTERN.fullmatch(
            self.audio_sha256
        ):
            raise ValueError("audio_sha256 must be 64 lowercase hexadecimal characters")
        if not isinstance(self.config, LongFormConfig):
            raise TypeError("config must be a LongFormConfig")
        if not isinstance(self.timeline, LongFormTimeline):
            raise TypeError("timeline must be a LongFormTimeline")
        if not isinstance(self.status, LongFormProjectStatus):
            raise TypeError("status must be a LongFormProjectStatus")
        _require_actor("created_by", self.created_by)
        _require_actor("updated_by", self.updated_by)
        _require_utc("created_at", self.created_at)
        _require_utc("updated_at", self.updated_at)
        if self.updated_at < self.created_at:
            raise ValueError("updated_at must not be earlier than created_at")
        if self.audio_duration_ms > self.config.max_seconds * 1000:
            raise ValueError(
                f"the audio is {self.audio_duration_ms} ms, longer than the "
                f"{self.config.max_seconds} seconds a LongForm video may take"
            )
        if self.timeline.duration_ms != self.audio_duration_ms:
            raise ValueError("the timeline must be as long as the audio")
        if (self.status is LongFormProjectStatus.DRAFT) != (not self.timeline.scenes):
            raise ValueError("a project is a draft exactly when its timeline is empty")

    @classmethod
    def create(
        cls,
        content_item_id: str,
        *,
        script_id: str,
        script_version: int,
        audio_artifact_id: str,
        audio_version: int,
        audio_sha256: str,
        audio_duration_ms: int,
        strategy_version: int,
        config: LongFormConfig,
        actor: Actor,
        clock: Clock | None = None,
    ) -> "LongFormProject":
        now = _now(clock)
        return cls(
            id=uuid.uuid4().hex,
            content_item_id=content_item_id,
            script_id=script_id,
            script_version=script_version,
            audio_artifact_id=audio_artifact_id,
            audio_version=audio_version,
            audio_sha256=audio_sha256,
            audio_duration_ms=audio_duration_ms,
            strategy_version=strategy_version,
            config=config,
            timeline=LongFormTimeline.empty(audio_duration_ms),
            status=LongFormProjectStatus.DRAFT,
            version=1,
            created_by=actor,
            created_at=now,
            updated_by=actor,
            updated_at=now,
        )

    def rebind(
        self,
        *,
        script_id: str,
        script_version: int,
        audio_artifact_id: str,
        audio_version: int,
        audio_sha256: str,
        audio_duration_ms: int,
        strategy_version: int,
        config: LongFormConfig,
        actor: Actor,
        clock: Clock | None = None,
    ) -> "LongFormProject":
        """The project made again from new inputs: a draft with an empty timeline."""
        return replace(
            self,
            script_id=script_id,
            script_version=script_version,
            audio_artifact_id=audio_artifact_id,
            audio_version=audio_version,
            audio_sha256=audio_sha256,
            audio_duration_ms=audio_duration_ms,
            strategy_version=strategy_version,
            config=config,
            timeline=LongFormTimeline.empty(audio_duration_ms),
            status=LongFormProjectStatus.DRAFT,
            version=self.version + 1,
            updated_by=actor,
            updated_at=_now(clock),
        )

    def with_timeline(
        self,
        timeline: LongFormTimeline,
        status: LongFormProjectStatus,
        *,
        actor: Actor,
        clock: Clock | None = None,
    ) -> "LongFormProject":
        """The project with another timeline of the same audio and its status."""
        if not isinstance(timeline, LongFormTimeline):
            raise TypeError("timeline must be a LongFormTimeline")
        if timeline.duration_ms != self.audio_duration_ms:
            raise ValueError(
                f"the timeline is {timeline.duration_ms} ms but the audio is "
                f"{self.audio_duration_ms} ms"
            )
        return replace(
            self,
            timeline=timeline,
            status=status,
            version=self.version + 1,
            updated_by=actor,
            updated_at=_now(clock),
        )

    @property
    def audio_within_target(self) -> bool:
        """True when the audio is within the format's seconds. Reports only."""
        low = self.config.min_seconds * 1000
        high = self.config.max_seconds * 1000
        return low <= self.audio_duration_ms <= high

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "content_item_id": self.content_item_id,
            "script_id": self.script_id,
            "script_version": self.script_version,
            "audio_artifact_id": self.audio_artifact_id,
            "audio_version": self.audio_version,
            "audio_sha256": self.audio_sha256,
            "audio_duration_ms": self.audio_duration_ms,
            "audio_within_target": self.audio_within_target,
            "strategy_version": self.strategy_version,
            "config": self.config.to_dict(),
            "timeline": self.timeline.to_dict(),
            "timeline_sha256": self.timeline.sha256,
            "status": self.status.value,
            "version": self.version,
            "created_by": _actor_dict(self.created_by),
            "created_at": self.created_at.isoformat(),
            "updated_by": _actor_dict(self.updated_by),
            "updated_at": self.updated_at.isoformat(),
        }


def _actor_dict(actor: Actor) -> dict[str, str]:
    return {"kind": actor.kind.value, "id": actor.id}


def _require_text(name: str, value: object) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must not be empty")


def _require_whole(name: str, value: object) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{name} must be a whole number of 1 or more")


def _require_actor(name: str, actor: object) -> None:
    if not isinstance(actor, Actor) or not isinstance(actor.kind, ActorKind):
        raise TypeError(f"{name} must be an Actor")
    _require_text(f"{name} id", actor.id)


def _require_utc(name: str, value: object) -> None:
    if not isinstance(value, datetime) or value.utcoffset() != timedelta(0):
        raise ValueError(f"{name} must be timezone-aware UTC")


def _now(clock: Clock | None) -> datetime:
    return clock() if clock else datetime.now(UTC)
