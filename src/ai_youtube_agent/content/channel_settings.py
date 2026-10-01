"""Channel settings (Prompt Pack v8, prompt #043), context C1 Channel & Strategy.

``ChannelSettings`` is the service behind the channel API. The rules were
approved by the user on 2026-10-01:

- A channel is created with a title, its YouTube channel id and an optional
  ``@handle``. The YouTube channel id must not belong to another channel
  (``ChannelExistsError``, 409) and can never change.
- An update may change the title, set or clear the handle, and set a status a
  user may choose: ``active``, ``paused`` or ``archived``. ``pending`` and
  ``disconnected`` belong to connecting the account (#145)
  (``ChannelStatusNotAllowedError``), although sending the current status
  again is allowed and changes nothing. An archived channel cannot change
  (``ChannelArchivedError``, B-013).
- Every update carries the ``updated_at`` the caller last read. If the channel
  changed since, the update is refused (``ChannelConflictError``, 409).
- An update that changes nothing stores nothing and records nothing.
- Each change is audited after its transaction commits (B-030):
  ``channel.created``, ``channel.updated`` (the changed fields) and
  ``channel.status_changed`` (from and to).
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from http import HTTPStatus

from ai_youtube_agent.content.channel import (
    Channel,
    ChannelStatus,
    YouTubeIdentifiers,
)
from ai_youtube_agent.core.audit import Actor, AuditLog, AuditResult, EntityRef
from ai_youtube_agent.core.db.database import ConcurrencyError, Database
from ai_youtube_agent.core.db.repositories.channel import ChannelRepository
from ai_youtube_agent.core.errors import DomainError

Clock = Callable[[], datetime]

USER_STATUSES = frozenset(
    {ChannelStatus.ACTIVE, ChannelStatus.PAUSED, ChannelStatus.ARCHIVED}
)


class ChannelNotFoundError(DomainError):
    default_code = "domain.channel_not_found"
    default_user_message = "This channel does not exist."
    default_http_status = HTTPStatus.NOT_FOUND


class ChannelExistsError(DomainError):
    default_code = "domain.channel_exists"
    default_user_message = "Another channel already uses this YouTube channel id."
    default_http_status = HTTPStatus.CONFLICT


class ChannelConflictError(DomainError):
    default_code = "domain.channel_conflict"
    default_user_message = (
        "This channel was changed by someone else. Reload it and try again."
    )
    default_http_status = HTTPStatus.CONFLICT


class ChannelStatusNotAllowedError(DomainError):
    default_code = "domain.channel_status_not_allowed"
    default_user_message = (
        "A user can only set a channel to active, paused or archived."
    )


@dataclass(frozen=True)
class ChannelChanges:
    """What an update asks for. ``None`` leaves a field as it is."""

    title: str | None = None
    handle: str | None = None
    clear_handle: bool = False
    status: ChannelStatus | None = None


class ChannelSettings:
    def __init__(
        self, database: Database, audit: AuditLog, *, clock: Clock | None = None
    ) -> None:
        self._database = database
        self._audit = audit
        self._clock = clock

    def list(self) -> list[Channel]:
        with self._database.transaction() as connection:
            return ChannelRepository(connection).list_all()

    def get(self, channel_id: str) -> Channel:
        with self._database.transaction() as connection:
            return _require(ChannelRepository(connection), channel_id)

    def create(
        self,
        title: str,
        youtube_channel_id: str,
        handle: str | None,
        *,
        actor: Actor,
    ) -> Channel:
        channel = Channel.create(
            title, YouTubeIdentifiers(youtube_channel_id, handle), clock=self._clock
        )
        with self._database.transaction() as connection:
            channels = ChannelRepository(connection)
            if any(
                c.youtube.channel_id == youtube_channel_id for c in channels.list_all()
            ):
                raise ChannelExistsError(
                    f"youtube channel {youtube_channel_id} is already stored"
                )
            channels.add(channel)
        self._audit.record(
            "channel.created",
            actor,
            EntityRef("channel", channel.id),
            AuditResult.SUCCESS,
            {"youtube_channel_id": youtube_channel_id, "title": channel.title},
        )
        return channel

    def update(
        self,
        channel_id: str,
        changes: ChannelChanges,
        *,
        expected_updated_at: datetime,
        actor: Actor,
    ) -> Channel:
        with self._database.transaction() as connection:
            channels = ChannelRepository(connection)
            before = _require(channels, channel_id)
            if (
                changes.status is not None
                and changes.status is not before.status
                and changes.status not in USER_STATUSES
            ):
                raise ChannelStatusNotAllowedError(
                    f"status {changes.status.value} is not set by a user"
                )
            if before.updated_at != expected_updated_at:
                raise ChannelConflictError(
                    f"channel {channel_id} is at {before.updated_at.isoformat()}, "
                    f"not {expected_updated_at.isoformat()}"
                )
            after = self._apply(before, changes)
            if after == before:
                return before
            try:
                channels.update(after, expected_updated_at=before.updated_at)
            except ConcurrencyError as error:
                raise ChannelConflictError(str(error)) from error
        self._record(before, after, actor)
        return after

    def _apply(self, channel: Channel, changes: ChannelChanges) -> Channel:
        if changes.title is not None:
            channel = channel.rename(changes.title, clock=self._clock)
        if changes.clear_handle:
            channel = channel.with_handle(None, clock=self._clock)
        elif changes.handle is not None:
            channel = channel.with_handle(changes.handle, clock=self._clock)
        if changes.status is not None:
            channel = channel.with_status(changes.status, clock=self._clock)
        return channel

    def _record(self, before: Channel, after: Channel, actor: Actor) -> None:
        entity = EntityRef("channel", after.id)
        changed = [
            name
            for name, old, new in (
                ("title", before.title, after.title),
                ("handle", before.youtube.handle, after.youtube.handle),
            )
            if old != new
        ]
        if changed:
            self._audit.record(
                "channel.updated",
                actor,
                entity,
                AuditResult.SUCCESS,
                {"fields": ",".join(changed)},
            )
        if before.status is not after.status:
            self._audit.record(
                "channel.status_changed",
                actor,
                entity,
                AuditResult.SUCCESS,
                {"from": before.status.value, "to": after.status.value},
            )


def _require(channels: ChannelRepository, channel_id: str) -> Channel:
    channel = channels.get(channel_id)
    if channel is None:
        raise ChannelNotFoundError(f"channel {channel_id} does not exist")
    return channel
