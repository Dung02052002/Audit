"""Repositories for context C1 Channel & Strategy and the voice profile (C7)."""

import sqlite3
from datetime import datetime

from ai_youtube_agent.content.channel import Channel, ChannelStatus, YouTubeIdentifiers
from ai_youtube_agent.content.strategy import (
    AgeRange,
    Audience,
    AudienceLevel,
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
    # A setting that is not configured yet is stored as NULL in all its columns.
    market, languages = profile.market, profile.languages
    audience, niche, brand = profile.audience, profile.niche, profile.brand
    cadence, budget, money = profile.cadence, profile.budget, profile.monetization
    return {
        "id": profile.id,
        "channel_id": profile.channel_id,
        "market_country": market.country if market else None,
        "primary_language": languages.primary if languages else None,
        "secondary_languages_json": (
            to_json(list(languages.secondary)) if languages else None
        ),
        "audience_json": to_json(_audience_json(audience)) if audience else None,
        "niche_json": (
            to_json({"name": niche.name, "pillars": list(niche.pillars)})
            if niche
            else None
        ),
        "brand_json": (
            to_json({"name": brand.name, "tone": brand.tone}) if brand else None
        ),
        "cadence_shorts_per_day": cadence.shorts_per_day if cadence else None,
        "cadence_longform_per_day": cadence.longform_per_day if cadence else None,
        "budget_currency": budget.currency if budget else None,
        "budget_daily_limit": format_decimal(budget.daily_limit) if budget else None,
        "budget_monthly_limit": (
            format_decimal(budget.monthly_limit) if budget else None
        ),
        "monetization_json": (
            to_json({"tracked_sources": list(money.tracked_sources)}) if money else None
        ),
        "version": profile.version,
        **actor_columns("updated_by", profile.updated_by),
        "created_at": dt(profile.created_at),
        "updated_at": dt(profile.updated_at),
    }


def _strategy(row: sqlite3.Row) -> StrategyProfile:
    def present(column: str) -> bool:
        return row[column] is not None

    niche = from_json(row["niche_json"]) if present("niche_json") else None
    brand = from_json(row["brand_json"]) if present("brand_json") else None
    return StrategyProfile(
        id=row["id"],
        channel_id=row["channel_id"],
        market=Market(row["market_country"]) if present("market_country") else None,
        languages=(
            LanguageSettings(
                row["primary_language"],
                tuple(from_json(row["secondary_languages_json"])),
            )
            if present("primary_language")
            else None
        ),
        audience=(
            _audience(from_json(row["audience_json"]))
            if present("audience_json")
            else None
        ),
        niche=Niche(niche["name"], tuple(niche["pillars"])) if niche else None,
        brand=Brand(brand["name"], brand["tone"]) if brand else None,
        cadence=(
            Cadence(
                shorts_per_day=row["cadence_shorts_per_day"],
                longform_per_day=row["cadence_longform_per_day"],
            )
            if present("cadence_shorts_per_day")
            else None
        ),
        budget=(
            Budget(
                row["budget_currency"],
                parse_decimal(row["budget_daily_limit"]),
                parse_decimal(row["budget_monthly_limit"]),
            )
            if present("budget_currency")
            else None
        ),
        monetization=(
            Monetization(tuple(from_json(row["monetization_json"])["tracked_sources"]))
            if present("monetization_json")
            else None
        ),
        version=row["version"],
        updated_by=actor_from(row, "updated_by"),
        created_at=parse_dt(row["created_at"]),
        updated_at=parse_dt(row["updated_at"]),
    )


def _audience_json(audience: Audience) -> dict:
    age = audience.age_range
    return {
        "description": audience.description,
        "age_range": {"min": age.min, "max": age.max} if age else None,
        "interests": list(audience.interests),
        "level": audience.level.value if audience.level else None,
    }


def _audience(data: dict) -> Audience:
    # Rows written before #046 only hold the description.
    age = data.get("age_range")
    level = data.get("level")
    return Audience(
        data["description"],
        AgeRange(age["min"], age["max"]) if age else None,
        tuple(data.get("interests", ())),
        AudienceLevel(level) if level else None,
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
