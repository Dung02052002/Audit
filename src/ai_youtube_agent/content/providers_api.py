"""HTTP API routes for Provider Registry and Tool Diagnostics.

Exposes:
- ``GET /providers``: Summary of all registered providers and active choices.
- ``GET /providers/diagnostics``: Comprehensive diagnostics across all tools.
- ``GET /providers/{provider_id}``: Provider capability detail.
- ``GET /providers/{provider_id}/diagnose``: Diagnostic probe for a single provider.
- ``POST /providers/{provider_id}/test``: Instant health check test for a provider.
- ``POST /providers/capcut/export``: Export project timeline and assets to CapCut draft.
- ``POST /providers/playwright/smoke-test``: Execute automated browser smoke test.
"""

from __future__ import annotations

from http import HTTPStatus
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from ai_youtube_agent.core.http import provide
from ai_youtube_agent.providers.registry import (
    CapCutAdapter,
    PlaywrightAdapter,
    ProviderRegistry,
)

router = APIRouter(prefix="/providers", tags=["providers"])

ProviderRegistryDep = Annotated[ProviderRegistry, provide(ProviderRegistry)]


class CapCutExportPayload(BaseModel):
    model_config = ConfigDict(extra="ignore")

    title: str = Field(min_length=1)
    scenes: list[dict[str, Any]] = Field(default_factory=list)
    audio_file_path: str | None = None
    duration_ms: int = Field(default=15000, ge=1000)
    is_vertical_9_16: bool = True


class PlaywrightTestPayload(BaseModel):
    model_config = ConfigDict(extra="ignore")

    base_url: str = "http://127.0.0.1:8000/app"
    headless: bool = True


@router.get("")
def list_providers(registry: ProviderRegistryDep) -> dict[str, Any]:
    """Return JSON summary of all registered tools and active engines."""
    return registry.summary()


@router.get("/diagnostics")
def run_all_diagnostics(registry: ProviderRegistryDep) -> dict[str, Any]:
    """Run connection, model, credential, and resource checks for all tools."""
    report = registry.diagnose_all()
    return report.as_dict()


@router.get("/{provider_id}")
def get_provider(provider_id: str, registry: ProviderRegistryDep) -> dict[str, Any]:
    """Get capability declaration of a specific provider."""
    provider = registry.get(provider_id)
    if not provider:
        raise HTTPException(
            status_code=HTTPStatus.NOT_FOUND,
            detail=f"Provider '{provider_id}' is not registered.",
        )
    return provider.capabilities().as_dict()


@router.get("/{provider_id}/diagnose")
def diagnose_provider(
    provider_id: str, registry: ProviderRegistryDep
) -> dict[str, Any]:
    """Run diagnostic checks on a specific provider."""
    provider = registry.get(provider_id)
    if not provider:
        raise HTTPException(
            status_code=HTTPStatus.NOT_FOUND,
            detail=f"Provider '{provider_id}' is not registered.",
        )
    return provider.diagnose().as_dict()


@router.post("/{provider_id}/test")
def test_provider(provider_id: str, registry: ProviderRegistryDep) -> dict[str, Any]:
    """Trigger an immediate connection and health probe for a provider."""
    provider = registry.get(provider_id)
    if not provider:
        raise HTTPException(
            status_code=HTTPStatus.NOT_FOUND,
            detail=f"Provider '{provider_id}' is not registered.",
        )

    conn = provider.check_connection()
    models = provider.check_models()
    creds = provider.check_credentials()
    resources = provider.check_resources()

    healthy = conn.connected and creds.valid
    return {
        "provider_id": provider_id,
        "healthy": healthy,
        "connection": conn.as_dict(),
        "models": models.as_dict(),
        "credentials": creds.as_dict(),
        "resources": resources.as_dict(),
    }


@router.post("/capcut/export")
def export_to_capcut(
    payload: CapCutExportPayload,
    registry: ProviderRegistryDep,
) -> dict[str, Any]:
    """Export project timeline and assets into a CapCut Desktop project folder."""
    capcut = registry.get("capcut")
    if not isinstance(capcut, CapCutAdapter):
        raise HTTPException(
            status_code=HTTPStatus.SERVICE_UNAVAILABLE,
            detail="CapCut adapter is not registered.",
        )

    try:
        result = capcut.export_draft(
            title=payload.title,
            scenes=payload.scenes,
            audio_file_path=payload.audio_file_path,
            duration_ms=payload.duration_ms,
            is_vertical_9_16=payload.is_vertical_9_16,
        )
        return result.as_dict()
    except Exception as exc:
        raise HTTPException(
            status_code=HTTPStatus.INTERNAL_SERVER_ERROR,
            detail=f"Failed to export CapCut draft: {exc}",
        ) from exc


@router.post("/playwright/smoke-test")
def run_playwright_smoke_test(
    payload: PlaywrightTestPayload,
    registry: ProviderRegistryDep,
) -> dict[str, Any]:
    """Run automated browser testing against the local web UI."""
    playwright = registry.get("playwright")
    if not isinstance(playwright, PlaywrightAdapter):
        raise HTTPException(
            status_code=HTTPStatus.SERVICE_UNAVAILABLE,
            detail="Playwright adapter is not registered.",
        )

    report = playwright.test_web_ui(
        base_url=payload.base_url,
        headless=payload.headless,
    )
    return report.as_dict()
