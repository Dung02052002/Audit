"""Strategy settings (Prompt Pack v8, prompt #044), context C1 Channel & Strategy.

``StrategySettings`` is the service behind the strategy API. #044 adds the
market; #045-#052 add one setting each. The rules were approved by the user on
2026-10-01:

- A channel's strategy profile is created by the first setting a user saves,
  with every other setting left unconfigured (``missing_settings``). #053
  checks for missing or incompatible settings before a run.
- Setting the market changes the market and nothing else: no other setting is
  derived, suggested or adjusted from it (no automatic strategy change). Only a
  user may change strategy (B-014, R-09).
- A change to an existing profile carries the ``expected_version`` the caller
  last read; a stale or missing version is refused (``StrategyConflictError``,
  409). Creating the profile needs no version, and sending one when no profile
  exists is a conflict too.
- An archived channel's strategy cannot change (``ChannelArchivedError``), and
  an unknown channel is 404 (``ChannelNotFoundError``).
- A change that changes nothing stores and records nothing. Otherwise, after
  the transaction commits, ``strategy.created`` (for a new profile) and
  ``strategy.market_changed`` (from, to, version) are audited.
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from http import HTTPStatus

from ai_youtube_agent.content.channel import ChannelArchivedError
from ai_youtube_agent.content.channel_settings import ChannelNotFoundError
from ai_youtube_agent.content.strategy import Market, StrategyProfile
from ai_youtube_agent.core.audit import Actor, AuditLog, AuditResult, EntityRef
from ai_youtube_agent.core.db.database import ConcurrencyError, Database
from ai_youtube_agent.core.db.repositories.channel import (
    ChannelRepository,
    StrategyProfileRepository,
)
from ai_youtube_agent.core.errors import DomainError

Clock = Callable[[], datetime]


class StrategyNotFoundError(DomainError):
    default_code = "domain.strategy_not_found"
    default_user_message = "This channel has no strategy yet."
    default_http_status = HTTPStatus.NOT_FOUND


class StrategyConflictError(DomainError):
    default_code = "domain.strategy_conflict"
    default_user_message = (
        "This strategy was changed by someone else. Reload it and try again."
    )
    default_http_status = HTTPStatus.CONFLICT


@dataclass(frozen=True)
class StrategyChange:
    profile: StrategyProfile
    created: bool


class StrategySettings:
    def __init__(
        self, database: Database, audit: AuditLog, *, clock: Clock | None = None
    ) -> None:
        self._database = database
        self._audit = audit
        self._clock = clock

    def get(self, channel_id: str) -> StrategyProfile:
        with self._database.transaction() as connection:
            if ChannelRepository(connection).get(channel_id) is None:
                raise ChannelNotFoundError(f"channel {channel_id} does not exist")
            profile = StrategyProfileRepository(connection).get_by_channel(channel_id)
        if profile is None:
            raise StrategyNotFoundError(f"channel {channel_id} has no strategy")
        return profile

    def set_market(
        self,
        channel_id: str,
        country: str,
        *,
        expected_version: int | None,
        actor: Actor,
    ) -> StrategyChange:
        market = Market(country)
        with self._database.transaction() as connection:
            channel = ChannelRepository(connection).get(channel_id)
            if channel is None:
                raise ChannelNotFoundError(f"channel {channel_id} does not exist")
            if channel.is_archived:
                raise ChannelArchivedError(f"channel {channel_id} is archived")
            profiles = StrategyProfileRepository(connection)
            before = profiles.get_by_channel(channel_id)
            if before is None:
                if expected_version is not None:
                    raise StrategyConflictError(
                        f"channel {channel_id} has no strategy, "
                        f"not version {expected_version}"
                    )
                after = StrategyProfile.create(
                    channel_id, market=market, actor=actor, clock=self._clock
                )
                profiles.add(after)
            else:
                if expected_version != before.version:
                    raise StrategyConflictError(
                        f"strategy of channel {channel_id} is at version "
                        f"{before.version}, not {expected_version}"
                    )
                after = before.update(market=market, actor=actor, clock=self._clock)
                if after is before:
                    return StrategyChange(before, created=False)
                try:
                    profiles.update(after, expected_version=before.version)
                except ConcurrencyError as error:
                    raise StrategyConflictError(str(error)) from error
        self._record(before, after, actor)
        return StrategyChange(after, created=before is None)

    def _record(
        self, before: StrategyProfile | None, after: StrategyProfile, actor: Actor
    ) -> None:
        entity = EntityRef("strategy_profile", after.id)
        if before is None:
            self._audit.record(
                "strategy.created",
                actor,
                entity,
                AuditResult.SUCCESS,
                {"channel_id": after.channel_id, "version": after.version},
            )
        old = before.market.country if before and before.market else None
        self._audit.record(
            "strategy.market_changed",
            actor,
            entity,
            AuditResult.SUCCESS,
            {
                "channel_id": after.channel_id,
                "from": old,
                "to": after.market.country if after.market else None,
                "version": after.version,
            },
        )
