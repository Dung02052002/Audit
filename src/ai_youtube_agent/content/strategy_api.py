"""Strategy settings API (Prompt Pack v8, prompts #044-#046), context C1.

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

Errors use the envelope of ``core/http.py``.
"""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Response, status
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationInfo,
    field_validator,
    model_validator,
)

from ai_youtube_agent.content.strategy import (
    COUNTRY_PATTERN,
    LANGUAGE_TAG_PATTERN,
    MAX_AUDIENCE_AGE,
    MAX_AUDIENCE_DESCRIPTION,
    MAX_INTEREST_LENGTH,
    MAX_INTERESTS,
    MAX_SECONDARY_LANGUAGES,
    MIN_AUDIENCE_AGE,
    AgeRange,
    Audience,
    AudienceLevel,
    StrategyProfile,
    canonical_language_tag,
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
