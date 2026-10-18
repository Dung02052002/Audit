"""ShortsProject entity (Prompt Pack v8, prompt #094), context C8 Shorts Production.

A ``ShortsProject`` is the production plan of one Shorts content item. The rules
were settled with the user's defaults on 2026-10-17:

- One project per content item (``content_item_id``), one mutable row: every
  change returns a new project with ``version`` + 1, and the store checks the
  version it last read (``expected_version``), so no change is lost.
- It is bound to the exact inputs it is produced from: the script version
  (``script_id``, ``script_version``) and the audio artifact version
  (``audio_artifact_id``, ``audio_version``, ``audio_sha256``,
  ``audio_duration_ms``). ``strategy_version`` and ``config`` record the channel
  strategy and the Shorts configuration (``ShortsConfig``, #049) read when the
  project was made or last re-bound.
- ``timeline`` is the plan of scenes, captions and music (``Timeline``). It is
  always as long as the audio, and ``status`` follows it: ``draft`` while it has
  no scenes, ``planned`` once it has scenes that cover the audio (I-095) and
  ``ready`` when it also has a background per scene (I-096) and its captions
  (I-097). The service decides the status (``readiness``); the entity only
  enforces that ``draft`` means no scenes.
- ``create`` makes a draft with an empty timeline. ``rebind`` takes new inputs,
  and as the old timeline described other audio it starts again as a draft with
  an empty timeline. ``with_timeline`` replaces the timeline of the same audio.
- The audio length must not exceed ``config.max_seconds``. Being shorter than
  ``config.min_seconds`` is only reported (``audio_within_target``).
- Who made and who last changed the project is an ``Actor`` of any kind, and the
  times are UTC. A project never changes the status of its content item.
"""

import uuid
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any

from ai_youtube_agent.content.shorts_config import ShortsConfig
from ai_youtube_agent.content.shorts_timeline import Timeline
from ai_youtube_agent.core.artifact import SHA256_PATTERN
from ai_youtube_agent.core.audit import Actor, ActorKind

Clock = Callable[[], datetime]


class ShortsProjectStatus(StrEnum):
    DRAFT = "draft"
    PLANNED = "planned"
    READY = "ready"


@dataclass(frozen=True)
class ShortsProject:
    id: str
    content_item_id: str
    script_id: str
    script_version: int
    audio_artifact_id: str
    audio_version: int
    audio_sha256: str
    audio_duration_ms: int
    strategy_version: int
    config: ShortsConfig
    timeline: Timeline
    status: ShortsProjectStatus
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
        if not isinstance(self.config, ShortsConfig):
            raise TypeError("config must be a ShortsConfig")
        if not isinstance(self.timeline, Timeline):
            raise TypeError("timeline must be a Timeline")
        if not isinstance(self.status, ShortsProjectStatus):
            raise TypeError("status must be a ShortsProjectStatus")
        _require_actor("created_by", self.created_by)
        _require_actor("updated_by", self.updated_by)
        _require_utc("created_at", self.created_at)
        _require_utc("updated_at", self.updated_at)
        if self.updated_at < self.created_at:
            raise ValueError("updated_at must not be earlier than created_at")
        if self.audio_duration_ms > self.config.max_seconds * 1000:
            raise ValueError(
                f"the audio is {self.audio_duration_ms} ms, longer than the "
                f"{self.config.max_seconds} seconds a Short may take"
            )
        if self.timeline.duration_ms != self.audio_duration_ms:
            raise ValueError("the timeline must be as long as the audio")
        if (self.status is ShortsProjectStatus.DRAFT) != (not self.timeline.scenes):
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
        config: ShortsConfig,
        actor: Actor,
        clock: Clock | None = None,
    ) -> "ShortsProject":
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
            timeline=Timeline.empty(audio_duration_ms),
            status=ShortsProjectStatus.DRAFT,
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
        config: ShortsConfig,
        actor: Actor,
        clock: Clock | None = None,
    ) -> "ShortsProject":
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
            timeline=Timeline.empty(audio_duration_ms),
            status=ShortsProjectStatus.DRAFT,
            version=self.version + 1,
            updated_by=actor,
            updated_at=_now(clock),
        )

    def with_timeline(
        self,
        timeline: Timeline,
        status: ShortsProjectStatus,
        *,
        actor: Actor,
        clock: Clock | None = None,
    ) -> "ShortsProject":
        """The project with another timeline of the same audio and its status."""
        if not isinstance(timeline, Timeline):
            raise TypeError("timeline must be a Timeline")
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


def _require_utc(name: str, moment: object) -> None:
    if not isinstance(moment, datetime) or moment.utcoffset() != timedelta(0):
        raise ValueError(f"{name} must be timezone-aware UTC")


def _now(clock: Clock | None) -> datetime:
    return clock() if clock else datetime.now(UTC)
