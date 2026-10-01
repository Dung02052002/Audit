"""StrategyProfile entity (Prompt Pack v8, prompt #014), context C1 Channel & Strategy.

A ``StrategyProfile`` is the user-controlled strategy of one channel:

- ``id``: a stable internal id. ``channel_id`` is the ``Channel.id`` it belongs
  to. One profile per channel is enforced by persistence (#029, #030).
- ``market``: an ISO 3166-1 alpha-2 country code.
- ``languages``: a primary BCP-47 tag and optional secondary tags.
- ``audience``, ``niche``, ``brand``: short descriptions of who the channel is
  for, what it covers and how it presents itself.
- ``cadence``: daily limits per content type (``SHORTS`` and ``LONGFORM``).
- ``budget``: daily and monthly spend limits as ``Decimal`` in an ISO 4217
  currency.
- ``monetization``: the revenue sources the user wants tracked. These are
  tracking goals, not guaranteed outcomes.
- ``version``, ``updated_by``, ``created_at`` and ``updated_at``: the version
  starts at 1 and grows by one on every change. Timestamps are UTC.

Since #044 (user decision, 2026-10-01) a profile is configured one section
at a time: every setting may be ``None`` (not configured yet), ``create``
takes any of them, and ``missing_settings`` lists the ones still unset. Once
set, a setting can be changed but not removed. Readers must treat a missing
setting as a reason to stop, as the daily limit and budget gates do.

Validation here only checks that each value is well formed. The detailed rules
for each setting come with #044–#052, and cross-field validation with #053.

The AI never changes strategy on its own (R-09). ``create`` and ``update`` take
the ``Actor`` making the change, and any actor that is not a user is refused
with ``StrategyChangeNotAllowedError``. A profile is frozen: ``update`` returns
a new profile.
"""

import re
import uuid
from collections.abc import Callable
from dataclasses import dataclass, fields, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from ai_youtube_agent.core.audit import Actor, ActorKind
from ai_youtube_agent.core.errors import DomainError

COUNTRY_PATTERN = re.compile(r"^[A-Z]{2}$")
LANGUAGE_TAG_PATTERN = re.compile(r"^[a-z]{2,3}(-[A-Za-z0-9]{2,8})*$")
CURRENCY_PATTERN = re.compile(r"^[A-Z]{3}$")
MAX_SECONDARY_LANGUAGES = 5  # #045, user decision 2026-10-01
SOURCE_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")
Clock = Callable[[], datetime]


class StrategyChangeNotAllowedError(DomainError):
    default_code = "domain.strategy_change_not_allowed"
    default_user_message = "Only a user can change the channel strategy."


def _require_text(name: str, value: str) -> None:
    if not value.strip():
        raise ValueError(f"{name} must not be empty")


@dataclass(frozen=True)
class Market:
    country: str

    def __post_init__(self) -> None:
        if not COUNTRY_PATTERN.match(self.country):
            raise ValueError(
                f"country {self.country!r} must be an ISO 3166-1 alpha-2 code "
                "such as 'US'"
            )


def canonical_language_tag(tag: str) -> str:
    """The BCP-47 tag in its canonical case (#045).

    The language is lower case, a four-letter script is title case and a region
    (two letters or three digits) is upper case; other subtags are lower case:
    ``EN-us`` becomes ``en-US`` and ``zh-hant-tw`` becomes ``zh-Hant-TW``.
    """
    language, *rest = tag.strip().split("-")
    parts = [language.lower()]
    for subtag in rest:
        if len(subtag) == 4 and subtag.isalpha():
            parts.append(subtag.title())
        elif (len(subtag) == 2 and subtag.isalpha()) or (
            len(subtag) == 3 and subtag.isdigit()
        ):
            parts.append(subtag.upper())
        else:
            parts.append(subtag.lower())
    return "-".join(parts)


@dataclass(frozen=True)
class LanguageSettings:
    """A primary language and up to five ordered secondary languages (#045)."""

    primary: str
    secondary: tuple[str, ...] = ()

    @classmethod
    def canonical(
        cls, primary: str, secondary: tuple[str, ...] = ()
    ) -> "LanguageSettings":
        """Build settings with every tag in canonical case, order kept."""
        return cls(
            canonical_language_tag(primary),
            tuple(canonical_language_tag(tag) for tag in secondary),
        )

    def __post_init__(self) -> None:
        tags = (self.primary, *self.secondary)
        for tag in tags:
            if not LANGUAGE_TAG_PATTERN.match(tag):
                raise ValueError(
                    f"language {tag!r} must be a BCP-47 tag such as 'en' or 'en-US'"
                )
        if len({tag.lower() for tag in tags}) != len(tags):
            raise ValueError("languages must not repeat")
        if len(self.secondary) > MAX_SECONDARY_LANGUAGES:
            raise ValueError(
                f"at most {MAX_SECONDARY_LANGUAGES} secondary languages are allowed"
            )


@dataclass(frozen=True)
class Audience:
    description: str

    def __post_init__(self) -> None:
        _require_text("audience description", self.description)


@dataclass(frozen=True)
class Niche:
    name: str
    pillars: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _require_text("niche name", self.name)
        for pillar in self.pillars:
            _require_text("content pillar", pillar)
        if len(set(self.pillars)) != len(self.pillars):
            raise ValueError("content pillars must not repeat")


@dataclass(frozen=True)
class Brand:
    name: str
    tone: str | None = None

    def __post_init__(self) -> None:
        _require_text("brand name", self.name)
        if self.tone is not None:
            _require_text("brand tone", self.tone)


@dataclass(frozen=True)
class Cadence:
    shorts_per_day: int
    longform_per_day: int

    def __post_init__(self) -> None:
        for name in ("shorts_per_day", "longform_per_day"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a whole number of 0 or more")


@dataclass(frozen=True)
class Budget:
    currency: str
    daily_limit: Decimal
    monthly_limit: Decimal

    def __post_init__(self) -> None:
        if not CURRENCY_PATTERN.match(self.currency):
            raise ValueError(
                f"currency {self.currency!r} must be an ISO 4217 code such as 'USD'"
            )
        for name in ("daily_limit", "monthly_limit"):
            value = getattr(self, name)
            if not isinstance(value, Decimal) or not value.is_finite() or value < 0:
                raise ValueError(f"{name} must be a finite Decimal of 0 or more")
        if self.daily_limit > self.monthly_limit:
            raise ValueError("daily_limit must not exceed monthly_limit")


@dataclass(frozen=True)
class Monetization:
    tracked_sources: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for source in self.tracked_sources:
            if not SOURCE_PATTERN.match(source):
                raise ValueError(
                    f"revenue source {source!r} must be a lowercase name such as 'ads'"
                )
        if len(set(self.tracked_sources)) != len(self.tracked_sources):
            raise ValueError("revenue sources must not repeat")


SETTING_TYPES: dict[str, type] = {
    "market": Market,
    "languages": LanguageSettings,
    "audience": Audience,
    "niche": Niche,
    "brand": Brand,
    "cadence": Cadence,
    "budget": Budget,
    "monetization": Monetization,
}


@dataclass(frozen=True)
class StrategyProfile:
    id: str
    channel_id: str
    market: Market | None
    languages: LanguageSettings | None
    audience: Audience | None
    niche: Niche | None
    brand: Brand | None
    cadence: Cadence | None
    budget: Budget | None
    monetization: Monetization | None
    version: int
    updated_by: Actor
    created_at: datetime
    updated_at: datetime

    def __post_init__(self) -> None:
        if not self.id:
            raise ValueError("strategy profile id must not be empty")
        if not self.channel_id:
            raise ValueError("channel id must not be empty")
        if self.version < 1:
            raise ValueError("version must be 1 or more")
        for name, setting_type in SETTING_TYPES.items():
            value = getattr(self, name)
            if value is not None and not isinstance(value, setting_type):
                raise TypeError(f"{name} must be a {setting_type.__name__} or None")
        _ensure_user(self.updated_by)
        for name in ("created_at", "updated_at"):
            if getattr(self, name).utcoffset() != timedelta(0):
                raise ValueError(f"{name} must be timezone-aware UTC")
        if self.updated_at < self.created_at:
            raise ValueError("updated_at must not be earlier than created_at")

    @classmethod
    def create(
        cls,
        channel_id: str,
        *,
        market: Market | None = None,
        languages: LanguageSettings | None = None,
        audience: Audience | None = None,
        niche: Niche | None = None,
        brand: Brand | None = None,
        cadence: Cadence | None = None,
        budget: Budget | None = None,
        monetization: Monetization | None = None,
        actor: Actor,
        clock: Clock | None = None,
    ) -> "StrategyProfile":
        _ensure_user(actor)
        now = _now(clock)
        return cls(
            id=uuid.uuid4().hex,
            channel_id=channel_id,
            market=market,
            languages=languages,
            audience=audience,
            niche=niche,
            brand=brand,
            cadence=cadence,
            budget=budget,
            monetization=monetization,
            version=1,
            updated_by=actor,
            created_at=now,
            updated_at=now,
        )

    def update(
        self, *, actor: Actor, clock: Clock | None = None, **changes: Any
    ) -> "StrategyProfile":
        unknown = set(changes) - SETTING_TYPES.keys()
        if unknown:
            raise ValueError(f"not a strategy setting: {', '.join(sorted(unknown))}")
        removed = sorted(name for name, value in changes.items() if value is None)
        if removed:
            raise ValueError(f"a setting cannot be removed: {', '.join(removed)}")
        _ensure_user(actor)
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

    @property
    def missing_settings(self) -> tuple[str, ...]:
        """The settings not configured yet, in ``SETTING_TYPES`` order."""
        return tuple(name for name in SETTING_TYPES if getattr(self, name) is None)

    @property
    def is_complete(self) -> bool:
        return not self.missing_settings

    def as_dict(self) -> dict[str, Any]:
        settings = {name: _setting_dict(getattr(self, name)) for name in SETTING_TYPES}
        return {
            "id": self.id,
            "channel_id": self.channel_id,
            **settings,
            "version": self.version,
            "updated_by": {
                "kind": self.updated_by.kind.value,
                "id": self.updated_by.id,
            },
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }


def _setting_dict(setting: Any) -> dict[str, Any] | None:
    if setting is None:
        return None
    result: dict[str, Any] = {}
    for item in fields(setting):
        value = getattr(setting, item.name)
        if isinstance(value, Decimal):
            value = str(value)
        elif isinstance(value, tuple):
            value = list(value)
        result[item.name] = value
    return result


def _ensure_user(actor: Actor) -> None:
    if actor.kind is not ActorKind.USER:
        raise StrategyChangeNotAllowedError(
            f"actor {actor.kind.value}:{actor.id} may not change strategy"
        )


def _now(clock: Clock | None) -> datetime:
    return clock() if clock else datetime.now(UTC)
