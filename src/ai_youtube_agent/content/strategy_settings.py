"""Strategy settings (Prompt Pack v8, prompts #044-#052), context C1.

``StrategySettings`` is the service behind the strategy API. #044 adds the
market, #045 the languages, #046 the audience, #047 the niche, #048 the
brand, #049 the Shorts and LongForm format defaults, #050 the cadence, #051 the
budget and #052 the monetization goals, all through the same ``_save``. The
rules were approved by the user on 2026-10-01 (#049 to #052 on 2026-10-02):

- A channel's strategy profile is created by the first setting a user saves,
  with every other setting left unconfigured (``missing_settings``). #053
  checks for missing or incompatible settings before a run.
- Saving a setting changes that setting and nothing else: no other setting is
  derived, suggested or adjusted from it (no automatic strategy change). Only a
  user may change strategy (B-014, R-09).
- Languages (#045) are stored with tags in canonical case, secondary tags in
  the user's order. Whether they suit the market is checked by #053.
- A change to an existing profile carries the ``expected_version`` the caller
  last read; a stale or missing version is refused (``StrategyConflictError``,
  409). Creating the profile needs no version, and sending one when no profile
  exists is a conflict too.
- An archived channel's strategy cannot change (``ChannelArchivedError``), and
  an unknown channel is 404 (``ChannelNotFoundError``).
- A change that changes nothing stores and records nothing. Otherwise, after
  the transaction commits, ``strategy.created`` (for a new profile) and
  ``strategy.<setting>_changed`` (from, to, version) are audited. ``from`` and
  ``to`` are a short text: the country for the market, the primary tag
  followed by the secondary tags, comma separated, for the languages, and the
  setting as compact JSON for any other setting (#046 audience onwards).
"""

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from http import HTTPStatus

from ai_youtube_agent.content.channel import ChannelArchivedError
from ai_youtube_agent.content.channel_settings import ChannelNotFoundError
from ai_youtube_agent.content.strategy import (
    Audience,
    Brand,
    Budget,
    Cadence,
    FormatSettings,
    LanguageSettings,
    Market,
    Monetization,
    Niche,
    StrategyProfile,
    setting_dict,
)
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
        return self._save(
            channel_id,
            "market",
            Market(country),
            expected_version=expected_version,
            actor=actor,
        )

    def set_languages(
        self,
        channel_id: str,
        primary: str,
        secondary: tuple[str, ...] = (),
        *,
        expected_version: int | None,
        actor: Actor,
    ) -> StrategyChange:
        return self._save(
            channel_id,
            "languages",
            LanguageSettings.canonical(primary, secondary),
            expected_version=expected_version,
            actor=actor,
        )

    def set_audience(
        self,
        channel_id: str,
        audience: Audience,
        *,
        expected_version: int | None,
        actor: Actor,
    ) -> StrategyChange:
        return self._save(
            channel_id,
            "audience",
            audience,
            expected_version=expected_version,
            actor=actor,
        )

    def set_niche(
        self,
        channel_id: str,
        niche: Niche,
        *,
        expected_version: int | None,
        actor: Actor,
    ) -> StrategyChange:
        return self._save(
            channel_id,
            "niche",
            niche,
            expected_version=expected_version,
            actor=actor,
        )

    def set_brand(
        self,
        channel_id: str,
        brand: Brand,
        *,
        expected_version: int | None,
        actor: Actor,
    ) -> StrategyChange:
        return self._save(
            channel_id,
            "brand",
            brand,
            expected_version=expected_version,
            actor=actor,
        )

    def set_format(
        self,
        channel_id: str,
        formats: FormatSettings,
        *,
        expected_version: int | None,
        actor: Actor,
    ) -> StrategyChange:
        return self._save(
            channel_id,
            "format",
            formats,
            expected_version=expected_version,
            actor=actor,
        )

    def set_cadence(
        self,
        channel_id: str,
        cadence: Cadence,
        *,
        expected_version: int | None,
        actor: Actor,
    ) -> StrategyChange:
        return self._save(
            channel_id,
            "cadence",
            cadence,
            expected_version=expected_version,
            actor=actor,
        )

    def set_budget(
        self,
        channel_id: str,
        budget: Budget,
        *,
        expected_version: int | None,
        actor: Actor,
    ) -> StrategyChange:
        return self._save(
            channel_id,
            "budget",
            budget,
            expected_version=expected_version,
            actor=actor,
        )

    def set_monetization(
        self,
        channel_id: str,
        monetization: Monetization,
        *,
        expected_version: int | None,
        actor: Actor,
    ) -> StrategyChange:
        return self._save(
            channel_id,
            "monetization",
            monetization,
            expected_version=expected_version,
            actor=actor,
        )

    def _save(
        self,
        channel_id: str,
        setting: str,
        value: object,
        *,
        expected_version: int | None,
        actor: Actor,
    ) -> StrategyChange:
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
                    channel_id, **{setting: value}, actor=actor, clock=self._clock
                )
                profiles.add(after)
            else:
                if expected_version != before.version:
                    raise StrategyConflictError(
                        f"strategy of channel {channel_id} is at version "
                        f"{before.version}, not {expected_version}"
                    )
                after = before.update(
                    **{setting: value}, actor=actor, clock=self._clock
                )
                if after is before:
                    return StrategyChange(before, created=False)
                try:
                    profiles.update(after, expected_version=before.version)
                except ConcurrencyError as error:
                    raise StrategyConflictError(str(error)) from error
        self._record(setting, before, after, actor)
        return StrategyChange(after, created=before is None)

    def _record(
        self,
        setting: str,
        before: StrategyProfile | None,
        after: StrategyProfile,
        actor: Actor,
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
        self._audit.record(
            f"strategy.{setting}_changed",
            actor,
            entity,
            AuditResult.SUCCESS,
            {
                "channel_id": after.channel_id,
                "from": _summary(getattr(before, setting) if before else None),
                "to": _summary(getattr(after, setting)),
                "version": after.version,
            },
        )


def _summary(value: object) -> str | None:
    """A short audit text for a setting value."""
    if value is None:
        return None
    if isinstance(value, Market):
        return value.country
    if isinstance(value, LanguageSettings):
        return ",".join((value.primary, *value.secondary))
    return json.dumps(
        setting_dict(value), ensure_ascii=False, separators=(",", ":"), sort_keys=True
    )
