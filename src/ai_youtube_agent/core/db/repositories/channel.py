"""Repositories for context C1 Channel & Strategy and the voice profile (C7)."""

import sqlite3
from datetime import datetime

from ai_youtube_agent.content.channel import Channel, ChannelStatus, YouTubeIdentifiers
from ai_youtube_agent.content.strategy import (
    Audience,
    Brand,
    Budget,
    Cadence,
    LanguageSettings,
    Market,
    Monetization,
    Niche,
    StrategyProfile,
)
from ai_youtube_agent.content.voice import VoiceProfile
from ai_youtube_agent.core.db.codec import format_decimal, parse_decimal
from ai_youtube_agent.core.db.repositories.base import (
    Repository,
    actor_columns,
    actor_from,
    dt,
    from_json,
    parse_dt,
    to_json,
)


class ChannelRepository(Repository):
    table = "channels"

    def add(self, channel: Channel) -> None:
        self._insert(self.table, _channel_row(channel))

    def get(self, channel_id: str) -> Channel | None:
        row = self._one("SELECT * FROM channels WHERE id = ?", (channel_id,))
        return _channel(row) if row else None

    def list_all(self) -> list[Channel]:
        rows = self._all("SELECT * FROM channels ORDER BY created_at, id")
        return [_channel(row) for row in rows]

    def update(self, channel: Channel, *, expected_updated_at: datetime) -> None:
        values = _channel_row(channel)
        mutable = ("title", "youtube_channel_id", "youtube_handle", "status")
        self._update(
            channel.id,
            {column: values[column] for column in (*mutable, "updated_at")},
            guard_column="updated_at",
            guard_value=dt(expected_updated_at),
        )


class StrategyProfileRepository(Repository):
    table = "strategy_profiles"

    def add(self, profile: StrategyProfile) -> None:
        self._insert(self.table, _strategy_row(profile))

    def get(self, profile_id: str) -> StrategyProfile | None:
        row = self._one("SELECT * FROM strategy_profiles WHERE id = ?", (profile_id,))
        return _strategy(row) if row else None

    def get_by_channel(self, channel_id: str) -> StrategyProfile | None:
        row = self._one(
            "SELECT * FROM strategy_profiles WHERE channel_id = ?", (channel_id,)
        )
        return _strategy(row) if row else None

    def update(self, profile: StrategyProfile, *, expected_version: int) -> None:
        values = _strategy_row(profile)
        for column in ("id", "channel_id", "created_at"):
            del values[column]
        self._update(
            profile.id,
            values,
            guard_column="version",
            guard_value=expected_version,
        )


class VoiceProfileRepository(Repository):
    table = "voice_profiles"

    def add(self, voice: VoiceProfile) -> None:
        self._insert(self.table, _voice_row(voice))

    def get(self, voice_id: str) -> VoiceProfile | None:
        row = self._one("SELECT * FROM voice_profiles WHERE id = ?", (voice_id,))
        return _voice(row) if row else None

    def list_by_channel(self, channel_id: str) -> list[VoiceProfile]:
        rows = self._all(
            "SELECT * FROM voice_profiles WHERE channel_id = ? ORDER BY created_at, id",
            (channel_id,),
        )
        return [_voice(row) for row in rows]

    def update(self, voice: VoiceProfile, *, expected_version: int) -> None:
        values = _voice_row(voice)
        for column in ("id", "channel_id", "created_at"):
            del values[column]
        self._update(
            voice.id, values, guard_column="version", guard_value=expected_version
        )


def _channel_row(channel: Channel) -> dict:
    return {
        "id": channel.id,
        "title": channel.title,
        "youtube_channel_id": channel.youtube.channel_id,
        "youtube_handle": channel.youtube.handle,
        "status": channel.status.value,
        "created_at": dt(channel.created_at),
        "updated_at": dt(channel.updated_at),
    }


def _channel(row: sqlite3.Row) -> Channel:
    return Channel(
        id=row["id"],
        title=row["title"],
        youtube=YouTubeIdentifiers(row["youtube_channel_id"], row["youtube_handle"]),
        status=ChannelStatus(row["status"]),
        created_at=parse_dt(row["created_at"]),
        updated_at=parse_dt(row["updated_at"]),
    )


def _strategy_row(profile: StrategyProfile) -> dict:
    return {
        "id": profile.id,
        "channel_id": profile.channel_id,
        "market_country": profile.market.country,
        "primary_language": profile.languages.primary,
        "secondary_languages_json": to_json(list(profile.languages.secondary)),
        "audience_json": to_json({"description": profile.audience.description}),
        "niche_json": to_json(
            {"name": profile.niche.name, "pillars": list(profile.niche.pillars)}
        ),
        "brand_json": to_json({"name": profile.brand.name, "tone": profile.brand.tone}),
        "cadence_shorts_per_day": profile.cadence.shorts_per_day,
        "cadence_longform_per_day": profile.cadence.longform_per_day,
        "budget_currency": profile.budget.currency,
        "budget_daily_limit": format_decimal(profile.budget.daily_limit),
        "budget_monthly_limit": format_decimal(profile.budget.monthly_limit),
        "monetization_json": to_json(
            {"tracked_sources": list(profile.monetization.tracked_sources)}
        ),
        "version": profile.version,
        **actor_columns("updated_by", profile.updated_by),
        "created_at": dt(profile.created_at),
        "updated_at": dt(profile.updated_at),
    }


def _strategy(row: sqlite3.Row) -> StrategyProfile:
    niche = from_json(row["niche_json"])
    brand = from_json(row["brand_json"])
    return StrategyProfile(
        id=row["id"],
        channel_id=row["channel_id"],
        market=Market(row["market_country"]),
        languages=LanguageSettings(
            row["primary_language"], tuple(from_json(row["secondary_languages_json"]))
        ),
        audience=Audience(from_json(row["audience_json"])["description"]),
        niche=Niche(niche["name"], tuple(niche["pillars"])),
        brand=Brand(brand["name"], brand["tone"]),
        cadence=Cadence(
            shorts_per_day=row["cadence_shorts_per_day"],
            longform_per_day=row["cadence_longform_per_day"],
        ),
        budget=Budget(
            row["budget_currency"],
            parse_decimal(row["budget_daily_limit"]),
            parse_decimal(row["budget_monthly_limit"]),
        ),
        monetization=Monetization(
            tuple(from_json(row["monetization_json"])["tracked_sources"])
        ),
        version=row["version"],
        updated_by=actor_from(row, "updated_by"),
        created_at=parse_dt(row["created_at"]),
        updated_at=parse_dt(row["updated_at"]),
    )


def _voice_row(voice: VoiceProfile) -> dict:
    return {
        "id": voice.id,
        "channel_id": voice.channel_id,
        "provider": voice.provider,
        "voice_id": voice.voice_id,
        "language": voice.language,
        "speaking_style": voice.speaking_style,
        "version": voice.version,
        **actor_columns("updated_by", voice.updated_by),
        "created_at": dt(voice.created_at),
        "updated_at": dt(voice.updated_at),
    }


def _voice(row: sqlite3.Row) -> VoiceProfile:
    return VoiceProfile(
        id=row["id"],
        channel_id=row["channel_id"],
        provider=row["provider"],
        voice_id=row["voice_id"],
        language=row["language"],
        speaking_style=row["speaking_style"],
        version=row["version"],
        updated_by=actor_from(row, "updated_by"),
        created_at=parse_dt(row["created_at"]),
        updated_at=parse_dt(row["updated_at"]),
    )
