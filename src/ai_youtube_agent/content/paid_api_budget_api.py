"""HTTP API of the paid AI API budget (BUDGET-01).

As the user decided for BUDGET-01 (routes live under ``/budget``):

- ``GET /budget/paid-api-policy``: the mode, whether it is the default (no
  user saved one yet), its version, who saved it and when, the modes there
  are, and the cost class of every AI provider the app can call (``provider``,
  ``task_type``, ``cost_class``, ``price_known``). Never a key, a token or a
  credentialed endpoint.
- ``PUT /budget/paid-api-policy``: ``mode`` and an optional
  ``expected_version`` (nothing else is accepted). Saved for the current user
  only; 200 with the policy, 422 for an unknown mode, 409
  ``budget.policy_version_conflict`` when the policy changed since it was read.
- ``GET /budget/paid-api-quotes``: quotes newest first, filtered by
  ``status``, ``content_item_id`` and ``job_id``, at most ``limit`` (1 to 200,
  default 50).
- ``GET /budget/paid-api-quotes/{quote_id}``: one quote (404
  ``budget.quote_not_found``).
- ``POST /budget/paid-api-quotes/{quote_id}/approve``: the user's approval of
  exactly this quote, echoing its ``request_hash``, ``estimated_amount`` (a
  decimal string) and ``currency``; 409 when the paid mode is off, the quote
  is not pending, expired or does not match.
- ``POST /budget/paid-api-quotes/{quote_id}/reject``: reject a pending quote
  or withdraw an approval not used yet.

Approving a quote does not run anything: the run is started again with
``POST /script-to-video/background-runs/{run_id}/retry``.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict, Field

from ai_youtube_agent.content.paid_api_budget import (
    PaidApiMode,
    PaidApiPolicy,
    PaidQuoteStatus,
    PaidTaskType,
)
from ai_youtube_agent.content.paid_api_budget_service import (
    MAX_LIST,
    PaidApiBudgetService,
)
from ai_youtube_agent.core.audit import Actor
from ai_youtube_agent.core.http import current_actor, provide
from ai_youtube_agent.providers.image_generation import ImageGenerator
from ai_youtube_agent.providers.provider_cost import classify_provider, task_type_of
from ai_youtube_agent.providers.registry import ProviderRegistry
from ai_youtube_agent.providers.research import ResearchProvider
from ai_youtube_agent.providers.speech_synthesis import SpeechSynthesizer
from ai_youtube_agent.providers.text_generation import TextGenerator
from ai_youtube_agent.providers.video_generation import VideoClipGenerator

router = APIRouter(prefix="/budget", tags=["budget"])

BudgetDep = Annotated[PaidApiBudgetService, provide(PaidApiBudgetService)]
CurrentActor = Annotated[Actor, Depends(current_actor)]
DECIMAL_TEXT = r"^\d{1,12}(\.\d{1,6})?$"


class PolicyPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: PaidApiMode
    expected_version: int | None = Field(default=None, ge=0, strict=True)


class ApprovalPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    estimated_amount: str = Field(pattern=DECIMAL_TEXT)
    currency: str = Field(pattern=r"^[A-Z]{3}$")


def _policy_body(policy: PaidApiPolicy) -> dict[str, Any]:
    return {
        "mode": policy.mode.value,
        "is_default": policy.is_default,
        "version": policy.version,
        "updated_at": (
            policy.updated_at.isoformat() if policy.updated_at is not None else None
        ),
        "updated_by_id": policy.updated_by.id if policy.updated_by else None,
        "modes": [mode.value for mode in PaidApiMode],
    }


def provider_inventory(
    providers: list[tuple[PaidTaskType, object]],
) -> list[dict[str, Any]]:
    """One entry per provider and task: names and cost classes only."""
    seen: set[tuple[str, str]] = set()
    entries: list[dict[str, Any]] = []
    for task_type, provider in providers:
        declaration = classify_provider(provider)
        key = (declaration.provider, task_type.value)
        if key in seen:
            continue
        seen.add(key)
        entries.append(
            {
                "provider": declaration.provider,
                "task_type": task_type.value,
                "cost_class": declaration.cost_class.value,
                "price_known": declaration.price_known,
            }
        )
    return entries


def _ai_providers(
    speech: SpeechSynthesizer,
    image: ImageGenerator,
    clip: VideoClipGenerator,
    text: TextGenerator,
    research: ResearchProvider,
    registry: ProviderRegistry,
) -> list[tuple[PaidTaskType, object]]:
    providers: list[tuple[PaidTaskType, object]] = [
        (PaidTaskType.SPEECH_SYNTHESIS, speech),
        (PaidTaskType.IMAGE_GENERATION, image),
        (PaidTaskType.VIDEO_CLIP_GENERATION, clip),
        (PaidTaskType.TEXT_GENERATION, text),
        (PaidTaskType.RESEARCH, research),
    ]
    # The registry tools that make AI calls (not the renderer, the editor or
    # the browser automation).
    ai_categories = {
        PaidTaskType.IMAGE_GENERATION.value,
        PaidTaskType.SPEECH_SYNTHESIS.value,
        "video_generation",
    }
    for adapter in registry.list_all():
        if str(adapter.category) in ai_categories:
            providers.append((task_type_of(adapter), adapter))
    return providers


@router.get("/paid-api-policy")
def get_paid_api_policy(
    budget: BudgetDep,
    speech: Annotated[SpeechSynthesizer, provide(SpeechSynthesizer)],
    image: Annotated[ImageGenerator, provide(ImageGenerator)],
    clip: Annotated[VideoClipGenerator, provide(VideoClipGenerator)],
    text: Annotated[TextGenerator, provide(TextGenerator)],
    research: Annotated[ResearchProvider, provide(ResearchProvider)],
    registry: Annotated[ProviderRegistry, provide(ProviderRegistry)],
) -> dict[str, Any]:
    return {
        **_policy_body(budget.get_policy()),
        "providers": provider_inventory(
            _ai_providers(speech, image, clip, text, research, registry)
        ),
    }


@router.put("/paid-api-policy")
def put_paid_api_policy(
    payload: PolicyPayload, budget: BudgetDep, actor: CurrentActor
) -> dict[str, Any]:
    policy = budget.set_policy(
        payload.mode, actor=actor, expected_version=payload.expected_version
    )
    return _policy_body(policy)


@router.get("/paid-api-quotes")
def list_paid_api_quotes(
    budget: BudgetDep,
    status: PaidQuoteStatus | None = None,
    content_item_id: str | None = None,
    job_id: str | None = None,
    limit: Annotated[int, Query(ge=1, le=MAX_LIST)] = 50,
) -> dict[str, Any]:
    quotes = budget.list_quotes(
        status=status, content_item_id=content_item_id, job_id=job_id, limit=limit
    )
    return {"quotes": [quote.as_dict() for quote in quotes]}


@router.get("/paid-api-quotes/{quote_id}")
def get_paid_api_quote(quote_id: str, budget: BudgetDep) -> dict[str, Any]:
    return budget.get_quote(quote_id).as_dict()


@router.post("/paid-api-quotes/{quote_id}/approve")
def approve_paid_api_quote(
    quote_id: str, payload: ApprovalPayload, budget: BudgetDep, actor: CurrentActor
) -> dict[str, Any]:
    quote = budget.approve(
        quote_id,
        actor=actor,
        request_hash=payload.request_hash,
        estimated_amount=Decimal(payload.estimated_amount),
        currency=payload.currency,
    )
    return quote.as_dict()


@router.post("/paid-api-quotes/{quote_id}/reject")
def reject_paid_api_quote(
    quote_id: str, budget: BudgetDep, actor: CurrentActor
) -> dict[str, Any]:
    return budget.reject(quote_id, actor=actor).as_dict()
