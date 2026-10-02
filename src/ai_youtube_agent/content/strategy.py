"""StrategyProfile entity (Prompt Pack v8, prompt #014), context C1 Channel & Strategy.

A ``StrategyProfile`` is the user-controlled strategy of one channel:

- ``id``: a stable internal id. ``channel_id`` is the ``Channel.id`` it belongs
  to. One profile per channel is enforced by persistence (#029, #030).
- ``market``: an ISO 3166-1 alpha-2 country code.
- ``languages``: a primary BCP-47 tag and optional secondary tags.
- ``audience``, ``niche``, ``brand``: short descriptions of who the channel is
  for, what it covers and how it presents itself.
- ``format``: production defaults for Shorts and LongForm (#049).
- ``cadence``: daily limits per content type (``SHORTS`` and ``LONGFORM``),
  the channel time zone and publish preferences (#050).
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
from dataclasses import dataclass, fields, is_dataclass, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from functools import cache
from typing import Any
from zoneinfo import available_timezones

from ai_youtube_agent.core.audit import Actor, ActorKind
from ai_youtube_agent.core.errors import DomainError

COUNTRY_PATTERN = re.compile(r"^[A-Z]{2}$")
LANGUAGE_TAG_PATTERN = re.compile(r"^[a-z]{2,3}(-[A-Za-z0-9]{2,8})*$")
CURRENCY_PATTERN = re.compile(r"^[A-Z]{3}$")
MAX_SECONDARY_LANGUAGES = 5  # #045, user decision 2026-10-01
# #046 audience limits, user decision 2026-10-01.
MAX_AUDIENCE_DESCRIPTION = 500
MIN_AUDIENCE_AGE = 13  # YouTube's minimum account age; no under-13 targeting
MAX_AUDIENCE_AGE = 100
MAX_INTERESTS = 10
MAX_INTEREST_LENGTH = 50
# #047 niche limits, user decision 2026-10-01.
MAX_NICHE_NAME = 100
MAX_PILLARS = 10
MAX_PILLAR_NAME = 60
MAX_PILLAR_DESCRIPTION = 300
# #048 brand limits, user decision 2026-10-01.
MAX_BRAND_NAME = 100
MAX_TONE = 200
MAX_TONE_KEYWORDS = 5
MAX_TONE_KEYWORD = 30
MAX_VOICE_RULES = 10
MAX_VOICE_RULE = 200
MAX_BANNED_PHRASES = 30
MAX_BANNED_PHRASE = 50
MAX_ACCENT_COLORS = 5
MAX_FONT_FAMILY = 100
MAX_VISUAL_NOTES = 500
HEX_COLOR_PATTERN = re.compile(r"^#[0-9A-F]{6}$")
# #049 format limits in seconds, user decision 2026-10-02. YouTube Shorts are
# at most 3 minutes, so LongForm starts above that and ends at 4 hours.
SHORTS_MIN_SECONDS = 1
SHORTS_MAX_SECONDS = 180
LONGFORM_MIN_SECONDS = 181
LONGFORM_MAX_SECONDS = 4 * 60 * 60
# #050 cadence limits, user decision 2026-10-02.
MAX_SHORTS_PER_DAY = 20
MAX_LONGFORM_PER_DAY = 5
MAX_PUBLISH_TIMES = 5
MAX_PUBLISH_GAP_MINUTES = 24 * 60
PUBLISH_TIME_PATTERN = re.compile(r"^([01][0-9]|2[0-3]):[0-5][0-9]$")
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


class AudienceLevel(StrEnum):
    BEGINNER = "beginner"
    INTERMEDIATE = "intermediate"
    ADVANCED = "advanced"
    MIXED = "mixed"


@dataclass(frozen=True)
class AgeRange:
    """An inclusive age range, from 13 to 100 (#046)."""

    min: int
    max: int

    def __post_init__(self) -> None:
        for name in ("min", "max"):
            value = getattr(self, name)
            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or not MIN_AUDIENCE_AGE <= value <= MAX_AUDIENCE_AGE
            ):
                raise ValueError(
                    f"age {name} must be a whole number from {MIN_AUDIENCE_AGE} "
                    f"to {MAX_AUDIENCE_AGE}"
                )
        if self.min > self.max:
            raise ValueError("age min must not be greater than age max")


@dataclass(frozen=True)
class Audience:
    """Who the channel is for (#046, user decision 2026-10-01).

    ``description`` is required; ``age_range``, ordered ``interests`` and
    ``level`` are optional. There are deliberately no fields for sensitive
    traits (ethnicity, religion, health, sexuality, politics), and no age
    below 13 can be targeted.
    """

    description: str
    age_range: AgeRange | None = None
    interests: tuple[str, ...] = ()
    level: AudienceLevel | None = None

    def __post_init__(self) -> None:
        _require_text("audience description", self.description)
        if len(self.description) > MAX_AUDIENCE_DESCRIPTION:
            raise ValueError(
                f"audience description must be at most {MAX_AUDIENCE_DESCRIPTION} "
                "characters"
            )
        if self.age_range is not None and not isinstance(self.age_range, AgeRange):
            raise TypeError("age_range must be an AgeRange or None")
        if not isinstance(self.interests, tuple):
            raise TypeError("interests must be a tuple")
        if len(self.interests) > MAX_INTERESTS:
            raise ValueError(f"at most {MAX_INTERESTS} interests are allowed")
        for interest in self.interests:
            _require_text("interest", interest)
            if len(interest) > MAX_INTEREST_LENGTH:
                raise ValueError(
                    f"an interest must be at most {MAX_INTEREST_LENGTH} characters"
                )
        if len({i.casefold() for i in self.interests}) != len(self.interests):
            raise ValueError("interests must not repeat")
        if self.level is not None and not isinstance(self.level, AudienceLevel):
            raise TypeError("level must be an AudienceLevel or None")


@dataclass(frozen=True)
class Pillar:
    """One content pillar: a name and an optional description (#047)."""

    name: str
    description: str | None = None

    def __post_init__(self) -> None:
        _require_text("content pillar name", self.name)
        if len(self.name) > MAX_PILLAR_NAME:
            raise ValueError(
                f"a content pillar name must be at most {MAX_PILLAR_NAME} characters"
            )
        if self.description is not None:
            _require_text("content pillar description", self.description)
            if len(self.description) > MAX_PILLAR_DESCRIPTION:
                raise ValueError(
                    "a content pillar description must be at most "
                    f"{MAX_PILLAR_DESCRIPTION} characters"
                )


@dataclass(frozen=True)
class Niche:
    """The channel's niche and its ordered content pillars (#047).

    A niche has a name (at most 100 characters) and 1 to 10 pillars in the
    user's order, with no two pillar names equal ignoring case.
    """

    name: str
    pillars: tuple[Pillar, ...]

    def __post_init__(self) -> None:
        _require_text("niche name", self.name)
        if len(self.name) > MAX_NICHE_NAME:
            raise ValueError(
                f"the niche name must be at most {MAX_NICHE_NAME} characters"
            )
        if not isinstance(self.pillars, tuple) or not all(
            isinstance(pillar, Pillar) for pillar in self.pillars
        ):
            raise TypeError("pillars must be a tuple of Pillar values")
        if not 1 <= len(self.pillars) <= MAX_PILLARS:
            raise ValueError(f"a niche needs 1 to {MAX_PILLARS} content pillars")
        names = {pillar.name.casefold() for pillar in self.pillars}
        if len(names) != len(self.pillars):
            raise ValueError("content pillars must not repeat")


def _check_length(name: str, value: str, limit: int) -> None:
    if len(value) > limit:
        raise ValueError(f"{name} must be at most {limit} characters")


def _check_texts(name: str, values: object, count: int, length: int) -> None:
    """An ordered tuple of 0..count non-empty, unique (ignoring case) texts."""
    if not isinstance(values, tuple):
        raise TypeError(f"{name} must be a tuple")
    if len(values) > count:
        raise ValueError(f"at most {count} {name} are allowed")
    for value in values:
        _require_text(name, value)
        _check_length(name, value, length)
    if len({value.casefold() for value in values}) != len(values):
        raise ValueError(f"{name} must not repeat")


def _check_color(name: str, value: str) -> None:
    if not HEX_COLOR_PATTERN.match(value):
        raise ValueError(f"{name} {value!r} must be a colour like '#1A2B3C'")


@dataclass(frozen=True)
class BrandVisual:
    """Visual rules (#048): colours, a font name and notes. No files."""

    primary_color: str | None = None
    accent_colors: tuple[str, ...] = ()
    font_family: str | None = None
    notes: str | None = None

    def __post_init__(self) -> None:
        if self.primary_color is not None:
            _check_color("primary colour", self.primary_color)
        if not isinstance(self.accent_colors, tuple):
            raise TypeError("accent_colors must be a tuple")
        if len(self.accent_colors) > MAX_ACCENT_COLORS:
            raise ValueError(f"at most {MAX_ACCENT_COLORS} accent colours are allowed")
        for color in self.accent_colors:
            _check_color("accent colour", color)
        colors = [c for c in (self.primary_color, *self.accent_colors) if c]
        if len(set(colors)) != len(colors):
            raise ValueError("colours must not repeat, the primary colour included")
        if self.font_family is not None:
            _require_text("font family", self.font_family)
            _check_length("font family", self.font_family, MAX_FONT_FAMILY)
        if self.notes is not None:
            _require_text("visual notes", self.notes)
            _check_length("visual notes", self.notes, MAX_VISUAL_NOTES)


@dataclass(frozen=True)
class Brand:
    """How the channel presents itself (#048, user decision 2026-10-01).

    Tone and written voice: ``tone`` (free text), ``tone_keywords``,
    ``voice_dos``, ``voice_donts`` and ``banned_phrases``. Visual rules live in
    ``visual``. The spoken TTS voice is not here: it is ``VoiceProfile``
    (B-018, #087).
    """

    name: str
    tone: str | None = None
    tone_keywords: tuple[str, ...] = ()
    voice_dos: tuple[str, ...] = ()
    voice_donts: tuple[str, ...] = ()
    banned_phrases: tuple[str, ...] = ()
    visual: BrandVisual | None = None

    def __post_init__(self) -> None:
        _require_text("brand name", self.name)
        _check_length("brand name", self.name, MAX_BRAND_NAME)
        if self.tone is not None:
            _require_text("brand tone", self.tone)
            _check_length("brand tone", self.tone, MAX_TONE)
        _check_texts(
            "tone keywords", self.tone_keywords, MAX_TONE_KEYWORDS, MAX_TONE_KEYWORD
        )
        _check_texts("voice dos", self.voice_dos, MAX_VOICE_RULES, MAX_VOICE_RULE)
        _check_texts("voice donts", self.voice_donts, MAX_VOICE_RULES, MAX_VOICE_RULE)
        _check_texts(
            "banned phrases", self.banned_phrases, MAX_BANNED_PHRASES, MAX_BANNED_PHRASE
        )
        if self.visual is not None and not isinstance(self.visual, BrandVisual):
            raise TypeError("visual must be a BrandVisual or None")


class AspectRatio(StrEnum):
    VERTICAL = "9:16"
    HORIZONTAL = "16:9"


class Resolution(StrEnum):
    HD_720 = "720p"
    FULL_HD_1080 = "1080p"
    UHD_2160 = "2160p"


def _check_duration(name: str, minimum: int, maximum: int, low: int, high: int) -> None:
    for bound, value in (("min_seconds", minimum), ("max_seconds", maximum)):
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or not low <= value <= high
        ):
            raise ValueError(
                f"{name} {bound} must be a whole number from {low} to {high}"
            )
    if minimum > maximum:
        raise ValueError(f"{name} min_seconds must not be greater than max_seconds")


def _check_production(
    name: str, aspect: object, expected: AspectRatio, resolution: object, flags: dict
) -> None:
    if aspect is not expected:
        raise ValueError(f"{name} aspect ratio must be {expected.value}")
    if not isinstance(resolution, Resolution):
        raise TypeError(f"{name} resolution must be a Resolution")
    for flag, value in flags.items():
        if not isinstance(value, bool):
            raise TypeError(f"{name} {flag} must be true or false")


@dataclass(frozen=True)
class ShortsFormat:
    """Shorts production defaults (#049): vertical, 1 to 180 seconds."""

    min_seconds: int
    max_seconds: int
    resolution: Resolution = Resolution.FULL_HD_1080
    captions: bool = True
    aspect_ratio: AspectRatio = AspectRatio.VERTICAL

    def __post_init__(self) -> None:
        _check_duration(
            "Shorts",
            self.min_seconds,
            self.max_seconds,
            SHORTS_MIN_SECONDS,
            SHORTS_MAX_SECONDS,
        )
        _check_production(
            "Shorts",
            self.aspect_ratio,
            AspectRatio.VERTICAL,
            self.resolution,
            {"captions": self.captions},
        )


@dataclass(frozen=True)
class LongFormFormat:
    """LongForm production defaults (#049): horizontal, 181 seconds to 4 hours."""

    min_seconds: int
    max_seconds: int
    resolution: Resolution = Resolution.FULL_HD_1080
    captions: bool = True
    chapters: bool = True
    aspect_ratio: AspectRatio = AspectRatio.HORIZONTAL

    def __post_init__(self) -> None:
        _check_duration(
            "LongForm",
            self.min_seconds,
            self.max_seconds,
            LONGFORM_MIN_SECONDS,
            LONGFORM_MAX_SECONDS,
        )
        _check_production(
            "LongForm",
            self.aspect_ratio,
            AspectRatio.HORIZONTAL,
            self.resolution,
            {"captions": self.captions, "chapters": self.chapters},
        )


@dataclass(frozen=True)
class FormatSettings:
    """Production defaults for both content types (#049, user decision 2026-10-02).

    Both formats are always configured together. LongForm defaults may be
    saved while ``LONGFORM_ENABLED`` is off: the flag controls production
    (#237), not this setting. Aspect ratios are fixed per type (Shorts 9:16,
    LongForm 16:9). Project models (#094, #105) read these defaults.
    """

    shorts: ShortsFormat
    longform: LongFormFormat

    def __post_init__(self) -> None:
        if not isinstance(self.shorts, ShortsFormat):
            raise TypeError("shorts must be a ShortsFormat")
        if not isinstance(self.longform, LongFormFormat):
            raise TypeError("longform must be a LongFormFormat")


@cache
def time_zones() -> frozenset[str]:
    """The IANA time zone names (from the ``tzdata`` package on Windows)."""
    return frozenset(available_timezones())


class Weekday(StrEnum):
    MONDAY = "monday"
    TUESDAY = "tuesday"
    WEDNESDAY = "wednesday"
    THURSDAY = "thursday"
    FRIDAY = "friday"
    SATURDAY = "saturday"
    SUNDAY = "sunday"


ALL_WEEKDAYS: tuple[Weekday, ...] = tuple(Weekday)


def _check_whole(name: str, value: object, low: int, high: int) -> None:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not low <= value <= high
    ):
        raise ValueError(f"{name} must be a whole number from {low} to {high}")


@dataclass(frozen=True)
class PublishSchedule:
    """When one content type should be published (#050).

    ``weekdays`` (1 to 7, Monday first, no repeats) and up to 5 preferred
    ``times`` (``HH:MM`` in the cadence time zone, ascending, no repeats) are
    preferences for the job scheduler (#205) and publishers (#149, #150);
    ``min_gap_minutes`` (0 to 1440) is the least time between two publishes of
    this type. No gate reads them yet.
    """

    weekdays: tuple[Weekday, ...] = ALL_WEEKDAYS
    times: tuple[str, ...] = ()
    min_gap_minutes: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.weekdays, tuple) or not all(
            isinstance(day, Weekday) for day in self.weekdays
        ):
            raise TypeError("weekdays must be a tuple of Weekday values")
        if not self.weekdays:
            raise ValueError("at least one publish weekday is needed")
        order = [ALL_WEEKDAYS.index(day) for day in self.weekdays]
        if order != sorted(set(order)):
            raise ValueError("weekdays must not repeat and must start from Monday")
        if not isinstance(self.times, tuple):
            raise TypeError("times must be a tuple")
        if len(self.times) > MAX_PUBLISH_TIMES:
            raise ValueError(f"at most {MAX_PUBLISH_TIMES} publish times are allowed")
        for value in self.times:
            if not isinstance(value, str) or not PUBLISH_TIME_PATTERN.match(value):
                raise ValueError(f"publish time {value!r} must look like '18:30'")
        if list(self.times) != sorted(set(self.times)):
            raise ValueError("publish times must not repeat and must be ascending")
        _check_whole(
            "min_gap_minutes", self.min_gap_minutes, 0, MAX_PUBLISH_GAP_MINUTES
        )


@dataclass(frozen=True)
class Cadence:
    """Daily limits and scheduling preferences (#050, user decision 2026-10-02).

    ``shorts_per_day`` (0 to 20) and ``longform_per_day`` (0 to 5) are the
    daily limits ``DailyLimitGate`` enforces, per calendar day in
    ``time_zone`` (an IANA name such as ``Asia/Ho_Chi_Minh``; ``UTC`` by
    default). A LongForm limit above 0 may be saved while ``LONGFORM_ENABLED``
    is off; the flag still controls production. Each type has its own
    ``PublishSchedule``.
    """

    shorts_per_day: int
    longform_per_day: int
    time_zone: str = "UTC"
    shorts_schedule: PublishSchedule = PublishSchedule()
    longform_schedule: PublishSchedule = PublishSchedule()

    def __post_init__(self) -> None:
        _check_whole("shorts_per_day", self.shorts_per_day, 0, MAX_SHORTS_PER_DAY)
        _check_whole("longform_per_day", self.longform_per_day, 0, MAX_LONGFORM_PER_DAY)
        if self.time_zone not in time_zones():
            raise ValueError(
                f"time zone {self.time_zone!r} must be an IANA name such as "
                "'Asia/Ho_Chi_Minh' or 'UTC'"
            )
        for name in ("shorts_schedule", "longform_schedule"):
            if not isinstance(getattr(self, name), PublishSchedule):
                raise TypeError(f"{name} must be a PublishSchedule")


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
    "format": FormatSettings,
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
    format: FormatSettings | None
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
        format: FormatSettings | None = None,
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
            format=format,
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
        settings = {name: setting_dict(getattr(self, name)) for name in SETTING_TYPES}
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


def setting_dict(setting: Any) -> dict[str, Any] | None:
    """A JSON-friendly dict of one setting value, or None when unset."""
    if setting is None:
        return None
    result: dict[str, Any] = {}
    for item in fields(setting):
        value = getattr(setting, item.name)
        if isinstance(value, Decimal):
            value = str(value)
        elif isinstance(value, tuple):
            value = [setting_dict(v) if is_dataclass(v) else v for v in value]
        elif isinstance(value, StrEnum):
            value = value.value
        elif is_dataclass(value):
            value = setting_dict(value)
        result[item.name] = value
    return result


def _ensure_user(actor: Actor) -> None:
    if actor.kind is not ActorKind.USER:
        raise StrategyChangeNotAllowedError(
            f"actor {actor.kind.value}:{actor.id} may not change strategy"
        )


def _now(clock: Clock | None) -> datetime:
    return clock() if clock else datetime.now(UTC)
