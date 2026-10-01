"""Strategy settings API (Prompt Pack v8, prompt #044), context C1.

The HTTP API for a channel's strategy, as the user approved on 2026-10-01.
#045-#052 add one ``PUT`` per setting next to the market.

- ``GET /channels/{id}/strategy``: the profile, with ``missing_settings``
  listing what is not configured yet.
- ``PUT /channels/{id}/strategy/market``: set the market from ``country`` (an
  ISO 3166-1 alpha-2 code; lower case is accepted and upper-cased) and
  ``expected_version``. Answers 201 when this creates the profile, else 200.
  The body accepts only these two fields, so nothing else can change with it.

Errors use the envelope of ``core/http.py``.
"""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Response, status
from pydantic import BaseModel, ConfigDict, Field, field_validator

from ai_youtube_agent.content.strategy import COUNTRY_PATTERN, StrategyProfile
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
