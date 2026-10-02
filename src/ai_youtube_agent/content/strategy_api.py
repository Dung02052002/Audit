"""Strategy settings API (Prompt Pack v8, prompts #044-#050), context C1.

The HTTP API for a channel's strategy, as the user approved on 2026-10-01.
#045-#052 add one ``PUT`` per setting next to the market.

- ``GET /channels/{id}/strategy``: the profile, with ``missing_settings``
  listing what is not configured yet.
- ``PUT /channels/{id}/strategy/market``: set the market from ``country`` (an
  ISO 3166-1 alpha-2 code; lower case is accepted and upper-cased) and
  ``expected_version``. Answers 201 when this creates the profile, else 200.
  The body accepts only these two fields, so nothing else can change with it.
- ``PUT /channels/{id}/strategy/languages`` (#045): ``primary``, ordered
  ``secondary`` (at most 5) and ``expected_version``. Tags may be sent in any
  case and are stored in canonical BCP-47 case (``en-us`` -> ``en-US``). No tag
  may repeat, including the primary. Same create, version and audit rules as
  the market.
- ``PUT /channels/{id}/strategy/audience`` (#046): ``description`` (required,
  at most 500 characters), optional ``age_range`` ``{min, max}`` from 13 to
  100, ordered ``interests`` (at most 10, each at most 50 characters, no
  repeats) and ``level`` (beginner, intermediate, advanced or mixed). Text is
  trimmed. Sending the body replaces the whole audience; a field left out is
  cleared. Same create, version and audit rules as the market.
- ``PUT /channels/{id}/strategy/niche`` (#047): ``name`` (at most 100
  characters) and 1 to 10 ordered ``pillars``, each a ``name`` (at most 60
  characters, unique ignoring case) and optional ``description`` (at most 300).
  The body replaces the whole niche; adding, removing or reordering pillars is
  sending the new list. Same create, version and audit rules as the market.
- ``PUT /channels/{id}/strategy/brand`` (#048): ``name`` (at most 100), ``tone``
  (at most 200), ``tone_keywords`` (at most 5 x 30), ``voice_dos`` and
  ``voice_donts`` (each at most 10 x 200), ``banned_phrases`` (at most 30 x
  50) and ``visual`` (``primary_color``, up to 5 ``accent_colors`` as
  ``#RRGGBB``, ``font_family`` at most 100, ``notes`` at most 500). Lists keep
  their order and do not repeat ignoring case; colours are upper-cased and
  may not repeat. The body replaces the whole brand. The spoken voice is
  ``VoiceProfile`` (#087), not this.
- ``PUT /channels/{id}/strategy/format`` (#049, user decision 2026-10-02):
  ``shorts`` and ``longform`` production defaults, both required. Each has
  ``min_seconds`` and ``max_seconds`` (Shorts 1 to 180, LongForm 181 to
  14400, min <= max), ``resolution`` (720p, 1080p or 2160p, default 1080p) and
  ``captions`` (default true); LongForm adds ``chapters`` (default true).
  ``aspect_ratio`` is fixed (Shorts ``9:16``, LongForm ``16:9``) and may be
  left out. LongForm defaults can be saved while ``LONGFORM_ENABLED`` is off.
  The body replaces the whole format setting.
- ``PUT /channels/{id}/strategy/cadence`` (#050, user decision 2026-10-02):
  ``shorts_per_day`` (0 to 20) and ``longform_per_day`` (0 to 5), the daily
  limits counted per calendar day in ``time_zone`` (an IANA name, any case,
  stored in its canonical case; default ``UTC``), and ``shorts_schedule`` and
  ``longform_schedule``: ``weekdays`` (1 to 7, stored Monday first; default
  every day), up to 5 ``times`` as ``HH:MM`` (stored ascending) and
  ``min_gap_minutes`` (0 to 1440, default 0). Nothing may repeat. A LongForm
  limit above 0 may be saved while ``LONGFORM_ENABLED`` is off. The body
  replaces the whole cadence.

Errors use the envelope of ``core/http.py``.
"""

from functools import cache
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Response, status
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    ValidationInfo,
    field_validator,
    model_validator,
)

from ai_youtube_agent.content.strategy import (
    COUNTRY_PATTERN,
    HEX_COLOR_PATTERN,
    LANGUAGE_TAG_PATTERN,
    LONGFORM_MAX_SECONDS,
    LONGFORM_MIN_SECONDS,
    MAX_ACCENT_COLORS,
    MAX_AUDIENCE_AGE,
    MAX_AUDIENCE_DESCRIPTION,
    MAX_BANNED_PHRASE,
    MAX_BANNED_PHRASES,
    MAX_BRAND_NAME,
    MAX_FONT_FAMILY,
    MAX_INTEREST_LENGTH,
    MAX_INTERESTS,
    MAX_LONGFORM_PER_DAY,
    MAX_NICHE_NAME,
    MAX_PILLAR_DESCRIPTION,
    MAX_PILLAR_NAME,
    MAX_PILLARS,
    MAX_PUBLISH_GAP_MINUTES,
    MAX_PUBLISH_TIMES,
    MAX_SECONDARY_LANGUAGES,
    MAX_SHORTS_PER_DAY,
    MAX_TONE,
    MAX_TONE_KEYWORD,
    MAX_TONE_KEYWORDS,
    MAX_VISUAL_NOTES,
    MAX_VOICE_RULE,
    MAX_VOICE_RULES,
    MIN_AUDIENCE_AGE,
    PUBLISH_TIME_PATTERN,
    SHORTS_MAX_SECONDS,
    SHORTS_MIN_SECONDS,
    AgeRange,
    AspectRatio,
    Audience,
    AudienceLevel,
    Brand,
    BrandVisual,
    Cadence,
    FormatSettings,
    LongFormFormat,
    Niche,
    Pillar,
    PublishSchedule,
    Resolution,
    ShortsFormat,
    StrategyProfile,
    Weekday,
    canonical_language_tag,
    time_zones,
)
from ai_youtube_agent.content.strategy_settings import StrategySettings
from ai_youtube_agent.core.audit import Actor
from ai_youtube_agent.core.http import current_actor, provide

router = APIRouter(prefix="/channels/{channel_id}/strategy", tags=["strategy"])

Settings = Annotated[StrategySettings, provide(StrategySettings)]
CurrentActor = Annotated[Actor, Depends(current_actor)]


class MarketUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    country: str
    expected_version: int | None = Field(default=None, ge=1)

    @field_validator("country")
    @classmethod
    def _check_country(cls, value: str) -> str:
        value = value.strip().upper()
        if not COUNTRY_PATTERN.match(value):
            raise ValueError(
                "country must be an ISO 3166-1 alpha-2 code such as 'VN' or 'US'"
            )
        return value


def _tag(value: str) -> str:
    tag = canonical_language_tag(value)
    if not LANGUAGE_TAG_PATTERN.match(tag):
        raise ValueError(
            f"language {value!r} must be a BCP-47 tag such as 'vi' or 'en-US'"
        )
    return tag


class LanguagesUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    primary: str
    secondary: list[str] = Field(default_factory=list)
    expected_version: int | None = Field(default=None, ge=1)

    @field_validator("primary")
    @classmethod
    def _check_primary(cls, value: str) -> str:
        return _tag(value)

    @field_validator("secondary")
    @classmethod
    def _check_secondary(cls, value: list[str], info: ValidationInfo) -> list[str]:
        if len(value) > MAX_SECONDARY_LANGUAGES:
            raise ValueError(
                f"at most {MAX_SECONDARY_LANGUAGES} secondary languages are allowed"
            )
        tags = [_tag(tag) for tag in value]
        primary = info.data.get("primary")
        seen = {primary.lower()} if primary else set()
        for tag in tags:
            if tag.lower() in seen:
                raise ValueError(
                    f"language {tag!r} repeats; each language may appear once, "
                    "and not as both primary and secondary"
                )
            seen.add(tag.lower())
        return tags


class AgeRangeBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    min: int = Field(ge=MIN_AUDIENCE_AGE, le=MAX_AUDIENCE_AGE)
    max: int = Field(ge=MIN_AUDIENCE_AGE, le=MAX_AUDIENCE_AGE)

    @model_validator(mode="after")
    def _check_order(self) -> "AgeRangeBody":
        if self.min > self.max:
            raise ValueError("age min must not be greater than age max")
        return self


class AudienceUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    description: str
    age_range: AgeRangeBody | None = None
    interests: list[str] = Field(default_factory=list)
    level: AudienceLevel | None = None
    expected_version: int | None = Field(default=None, ge=1)

    @field_validator("description")
    @classmethod
    def _check_description(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("description must not be empty")
        if len(value) > MAX_AUDIENCE_DESCRIPTION:
            raise ValueError(
                f"description must be at most {MAX_AUDIENCE_DESCRIPTION} characters"
            )
        return value

    @field_validator("interests")
    @classmethod
    def _check_interests(cls, value: list[str]) -> list[str]:
        if len(value) > MAX_INTERESTS:
            raise ValueError(f"at most {MAX_INTERESTS} interests are allowed")
        interests = [interest.strip() for interest in value]
        seen: set[str] = set()
        for interest in interests:
            if not interest:
                raise ValueError("an interest must not be empty")
            if len(interest) > MAX_INTEREST_LENGTH:
                raise ValueError(
                    f"an interest must be at most {MAX_INTEREST_LENGTH} characters"
                )
            if interest.casefold() in seen:
                raise ValueError(f"interest {interest!r} repeats")
            seen.add(interest.casefold())
        return interests

    def audience(self) -> Audience:
        age = self.age_range
        return Audience(
            self.description,
            AgeRange(age.min, age.max) if age else None,
            tuple(self.interests),
            self.level,
        )


def _text(value: str, what: str, limit: int) -> str:
    value = value.strip()
    if not value:
        raise ValueError(f"{what} must not be empty")
    if len(value) > limit:
        raise ValueError(f"{what} must be at most {limit} characters")
    return value


class PillarBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    description: str | None = None

    @field_validator("name")
    @classmethod
    def _check_name(cls, value: str) -> str:
        return _text(value, "pillar name", MAX_PILLAR_NAME)

    @field_validator("description")
    @classmethod
    def _check_description(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _text(value, "pillar description", MAX_PILLAR_DESCRIPTION)


class NicheUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    pillars: list[PillarBody]
    expected_version: int | None = Field(default=None, ge=1)

    @field_validator("name")
    @classmethod
    def _check_name(cls, value: str) -> str:
        return _text(value, "niche name", MAX_NICHE_NAME)

    @field_validator("pillars")
    @classmethod
    def _check_pillars(cls, value: list[PillarBody]) -> list[PillarBody]:
        if not 1 <= len(value) <= MAX_PILLARS:
            raise ValueError(f"a niche needs 1 to {MAX_PILLARS} content pillars")
        seen: set[str] = set()
        for pillar in value:
            if pillar.name.casefold() in seen:
                raise ValueError(f"content pillar {pillar.name!r} repeats")
            seen.add(pillar.name.casefold())
        return value

    def niche(self) -> Niche:
        return Niche(
            self.name,
            tuple(Pillar(p.name, p.description) for p in self.pillars),
        )


def _optional_text(value: str | None, what: str, limit: int) -> str | None:
    return None if value is None else _text(value, what, limit)


def _texts(values: list[str], what: str, count: int, limit: int) -> list[str]:
    if len(values) > count:
        raise ValueError(f"at most {count} {what} are allowed")
    cleaned = [_text(value, what, limit) for value in values]
    seen: set[str] = set()
    for value in cleaned:
        if value.casefold() in seen:
            raise ValueError(f"{what} {value!r} repeats")
        seen.add(value.casefold())
    return cleaned


def _color(value: str) -> str:
    value = value.strip().upper()
    if not HEX_COLOR_PATTERN.match(value):
        raise ValueError(f"colour {value!r} must look like '#1A2B3C'")
    return value


class VisualBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    primary_color: str | None = None
    accent_colors: list[str] = Field(default_factory=list)
    font_family: str | None = None
    notes: str | None = None

    @field_validator("primary_color")
    @classmethod
    def _check_primary(cls, value: str | None) -> str | None:
        return None if value is None else _color(value)

    @field_validator("accent_colors")
    @classmethod
    def _check_accents(cls, value: list[str], info: ValidationInfo) -> list[str]:
        if len(value) > MAX_ACCENT_COLORS:
            raise ValueError(f"at most {MAX_ACCENT_COLORS} accent colours are allowed")
        colors = [_color(color) for color in value]
        primary = info.data.get("primary_color")
        seen = {primary} if primary else set()
        for color in colors:
            if color in seen:
                raise ValueError(f"colour {color} repeats, the primary included")
            seen.add(color)
        return colors

    @field_validator("font_family")
    @classmethod
    def _check_font(cls, value: str | None) -> str | None:
        return _optional_text(value, "font family", MAX_FONT_FAMILY)

    @field_validator("notes")
    @classmethod
    def _check_notes(cls, value: str | None) -> str | None:
        return _optional_text(value, "visual notes", MAX_VISUAL_NOTES)


class BrandUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    tone: str | None = None
    tone_keywords: list[str] = Field(default_factory=list)
    voice_dos: list[str] = Field(default_factory=list)
    voice_donts: list[str] = Field(default_factory=list)
    banned_phrases: list[str] = Field(default_factory=list)
    visual: VisualBody | None = None
    expected_version: int | None = Field(default=None, ge=1)

    @field_validator("name")
    @classmethod
    def _check_name(cls, value: str) -> str:
        return _text(value, "brand name", MAX_BRAND_NAME)

    @field_validator("tone")
    @classmethod
    def _check_tone(cls, value: str | None) -> str | None:
        return _optional_text(value, "tone", MAX_TONE)

    @field_validator("tone_keywords")
    @classmethod
    def _check_keywords(cls, value: list[str]) -> list[str]:
        return _texts(value, "tone keywords", MAX_TONE_KEYWORDS, MAX_TONE_KEYWORD)

    @field_validator("voice_dos", "voice_donts")
    @classmethod
    def _check_rules(cls, value: list[str], info: ValidationInfo) -> list[str]:
        what = (info.field_name or "voice rules").replace("_", " ")
        return _texts(value, what, MAX_VOICE_RULES, MAX_VOICE_RULE)

    @field_validator("banned_phrases")
    @classmethod
    def _check_banned(cls, value: list[str]) -> list[str]:
        return _texts(value, "banned phrases", MAX_BANNED_PHRASES, MAX_BANNED_PHRASE)

    def brand(self) -> Brand:
        visual = self.visual
        return Brand(
            self.name,
            self.tone,
            tuple(self.tone_keywords),
            tuple(self.voice_dos),
            tuple(self.voice_donts),
            tuple(self.banned_phrases),
            (
                BrandVisual(
                    visual.primary_color,
                    tuple(visual.accent_colors),
                    visual.font_family,
                    visual.notes,
                )
                if visual
                else None
            ),
        )


def _check_order(body: "ShortsBody | LongFormBody") -> None:
    if body.min_seconds > body.max_seconds:
        raise ValueError("min_seconds must not be greater than max_seconds")


class ShortsBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    min_seconds: int = Field(ge=SHORTS_MIN_SECONDS, le=SHORTS_MAX_SECONDS, strict=True)
    max_seconds: int = Field(ge=SHORTS_MIN_SECONDS, le=SHORTS_MAX_SECONDS, strict=True)
    resolution: Resolution = Resolution.FULL_HD_1080
    captions: StrictBool = True
    aspect_ratio: Literal["9:16"] = "9:16"

    @model_validator(mode="after")
    def _check(self) -> "ShortsBody":
        _check_order(self)
        return self


class LongFormBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    min_seconds: int = Field(
        ge=LONGFORM_MIN_SECONDS, le=LONGFORM_MAX_SECONDS, strict=True
    )
    max_seconds: int = Field(
        ge=LONGFORM_MIN_SECONDS, le=LONGFORM_MAX_SECONDS, strict=True
    )
    resolution: Resolution = Resolution.FULL_HD_1080
    captions: StrictBool = True
    chapters: StrictBool = True
    aspect_ratio: Literal["16:9"] = "16:9"

    @model_validator(mode="after")
    def _check(self) -> "LongFormBody":
        _check_order(self)
        return self


class FormatUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    shorts: ShortsBody
    longform: LongFormBody
    expected_version: int | None = Field(default=None, ge=1)

    def formats(self) -> FormatSettings:
        shorts, longform = self.shorts, self.longform
        return FormatSettings(
            ShortsFormat(
                shorts.min_seconds,
                shorts.max_seconds,
                shorts.resolution,
                shorts.captions,
                AspectRatio(shorts.aspect_ratio),
            ),
            LongFormFormat(
                longform.min_seconds,
                longform.max_seconds,
                longform.resolution,
                longform.captions,
                longform.chapters,
                AspectRatio(longform.aspect_ratio),
            ),
        )


class ScheduleBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    weekdays: list[Weekday] = Field(default_factory=lambda: list(Weekday))
    times: list[str] = Field(default_factory=list)
    min_gap_minutes: int = Field(
        default=0, ge=0, le=MAX_PUBLISH_GAP_MINUTES, strict=True
    )

    @field_validator("weekdays")
    @classmethod
    def _check_weekdays(cls, value: list[Weekday]) -> list[Weekday]:
        if not value:
            raise ValueError("at least one publish weekday is needed")
        if len(set(value)) != len(value):
            raise ValueError("weekdays must not repeat")
        return sorted(value, key=list(Weekday).index)

    @field_validator("times")
    @classmethod
    def _check_times(cls, value: list[str]) -> list[str]:
        if len(value) > MAX_PUBLISH_TIMES:
            raise ValueError(f"at most {MAX_PUBLISH_TIMES} publish times are allowed")
        times = [time.strip() for time in value]
        for time in times:
            if not PUBLISH_TIME_PATTERN.match(time):
                raise ValueError(f"publish time {time!r} must look like '18:30'")
        if len(set(times)) != len(times):
            raise ValueError("publish times must not repeat")
        return sorted(times)

    def schedule(self) -> PublishSchedule:
        return PublishSchedule(
            tuple(self.weekdays), tuple(self.times), self.min_gap_minutes
        )


@cache
def _zones_by_folded_name() -> dict[str, str]:
    return {name.casefold(): name for name in time_zones()}


class CadenceUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    shorts_per_day: int = Field(ge=0, le=MAX_SHORTS_PER_DAY, strict=True)
    longform_per_day: int = Field(ge=0, le=MAX_LONGFORM_PER_DAY, strict=True)
    time_zone: str = "UTC"
    shorts_schedule: ScheduleBody = Field(default_factory=ScheduleBody)
    longform_schedule: ScheduleBody = Field(default_factory=ScheduleBody)
    expected_version: int | None = Field(default=None, ge=1)

    @field_validator("time_zone")
    @classmethod
    def _check_time_zone(cls, value: str) -> str:
        name = _zones_by_folded_name().get(value.strip().casefold())
        if name is None:
            raise ValueError(
                f"time zone {value!r} must be an IANA name such as "
                "'Asia/Ho_Chi_Minh' or 'UTC'"
            )
        return name

    def cadence(self) -> Cadence:
        return Cadence(
            self.shorts_per_day,
            self.longform_per_day,
            self.time_zone,
            self.shorts_schedule.schedule(),
            self.longform_schedule.schedule(),
        )


def _body(profile: StrategyProfile) -> dict[str, Any]:
    return profile.as_dict() | {"missing_settings": list(profile.missing_settings)}


@router.get("")
def get_strategy(channel_id: str, settings: Settings) -> dict[str, Any]:
    return _body(settings.get(channel_id))


@router.put("/market")
def put_market(
    channel_id: str,
    body: MarketUpdate,
    settings: Settings,
    actor: CurrentActor,
    response: Response,
) -> dict[str, Any]:
    change = settings.set_market(
        channel_id,
        body.country,
        expected_version=body.expected_version,
        actor=actor,
    )
    if change.created:
        response.status_code = status.HTTP_201_CREATED
    return _body(change.profile)


@router.put("/languages")
def put_languages(
    channel_id: str,
    body: LanguagesUpdate,
    settings: Settings,
    actor: CurrentActor,
    response: Response,
) -> dict[str, Any]:
    change = settings.set_languages(
        channel_id,
        body.primary,
        tuple(body.secondary),
        expected_version=body.expected_version,
        actor=actor,
    )
    if change.created:
        response.status_code = status.HTTP_201_CREATED
    return _body(change.profile)


@router.put("/audience")
def put_audience(
    channel_id: str,
    body: AudienceUpdate,
    settings: Settings,
    actor: CurrentActor,
    response: Response,
) -> dict[str, Any]:
    change = settings.set_audience(
        channel_id,
        body.audience(),
        expected_version=body.expected_version,
        actor=actor,
    )
    if change.created:
        response.status_code = status.HTTP_201_CREATED
    return _body(change.profile)


@router.put("/niche")
def put_niche(
    channel_id: str,
    body: NicheUpdate,
    settings: Settings,
    actor: CurrentActor,
    response: Response,
) -> dict[str, Any]:
    change = settings.set_niche(
        channel_id,
        body.niche(),
        expected_version=body.expected_version,
        actor=actor,
    )
    if change.created:
        response.status_code = status.HTTP_201_CREATED
    return _body(change.profile)


@router.put("/brand")
def put_brand(
    channel_id: str,
    body: BrandUpdate,
    settings: Settings,
    actor: CurrentActor,
    response: Response,
) -> dict[str, Any]:
    change = settings.set_brand(
        channel_id,
        body.brand(),
        expected_version=body.expected_version,
        actor=actor,
    )
    if change.created:
        response.status_code = status.HTTP_201_CREATED
    return _body(change.profile)


@router.put("/format")
def put_format(
    channel_id: str,
    body: FormatUpdate,
    settings: Settings,
    actor: CurrentActor,
    response: Response,
) -> dict[str, Any]:
    change = settings.set_format(
        channel_id,
        body.formats(),
        expected_version=body.expected_version,
        actor=actor,
    )
    if change.created:
        response.status_code = status.HTTP_201_CREATED
    return _body(change.profile)


@router.put("/cadence")
def put_cadence(
    channel_id: str,
    body: CadenceUpdate,
    settings: Settings,
    actor: CurrentActor,
    response: Response,
) -> dict[str, Any]:
    change = settings.set_cadence(
        channel_id,
        body.cadence(),
        expected_version=body.expected_version,
        actor=actor,
    )
    if change.created:
        response.status_code = status.HTTP_201_CREATED
    return _body(change.profile)
