"""Base Provider Adapter contract and abstract implementation.

Every tool adapter (ComfyUI, EdgeTTS, ElevenLabs, CapCut, FFmpeg, Playwright,
VideoGen) implements this interface to guarantee uniform capabilities,
connectivity checks, resource verification, and health probing.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import UTC, datetime
from typing import Protocol, runtime_checkable

from ai_youtube_agent.core.errors import ProviderError
from ai_youtube_agent.providers.provider_cost import (
    ProviderCostClass,
    ProviderCostDeclaration,
    free_declaration,
    paid_declaration,
)
from ai_youtube_agent.providers.registry.models import (
    ConnectionCheckResult,
    CredentialCheckResult,
    ModelCheckResult,
    ProviderCapability,
    ProviderCategory,
    ProviderDiagnostic,
    ProviderStatus,
    ResourceCheckResult,
)


@runtime_checkable
class ProviderAdapter(Protocol):
    """Protocol satisfied by all video production tool adapters."""

    provider_id: str
    category: ProviderCategory

    def capabilities(self) -> ProviderCapability:
        """Declare supported modalities, models, and resource requirements."""
        ...

    def check_connection(self) -> ConnectionCheckResult:
        """Verify endpoint connectivity, socket reachability, or process existence."""
        ...

    def check_models(self) -> ModelCheckResult:
        """Inspect model weights, checkpoints, voice packs, or codecs."""
        ...

    def check_credentials(self) -> CredentialCheckResult:
        """Validate API keys, tokens, or local filesystem authorization."""
        ...

    def check_resources(self) -> ResourceCheckResult:
        """Assess VRAM, RAM, disk space, and dependent binary executables."""
        ...

    def diagnose(self) -> ProviderDiagnostic:
        """Generate a complete diagnostic assessment combining all checks."""
        ...

    def check(self) -> None:
        """Health check probe. Must raise ProviderError if the tool cannot function."""
        ...


class BaseProviderAdapter(ABC):
    """Abstract base class offering shared diagnostic aggregation and health logic."""

    def __init__(self, provider_id: str, category: ProviderCategory) -> None:
        self.provider_id = provider_id
        self.category = category

    @abstractmethod
    def capabilities(self) -> ProviderCapability:
        """Return provider capability declaration."""

    @abstractmethod
    def check_connection(self) -> ConnectionCheckResult:
        """Check network or process connection."""

    @abstractmethod
    def check_models(self) -> ModelCheckResult:
        """Check model/checkpoint availability."""

    @abstractmethod
    def check_credentials(self) -> CredentialCheckResult:
        """Check API keys/credentials."""

    @abstractmethod
    def check_resources(self) -> ResourceCheckResult:
        """Check hardware and binary resources."""

    def cost_declaration(self) -> ProviderCostDeclaration:
        """The cost class from the static capability declaration (BUDGET-01).

        Free and local is ``local_free``, free and online ``free_service``, and
        anything else ``paid`` without a verified price. No network call.
        """
        capability = self.capabilities()
        name = getattr(self, "name", None) or self.provider_id
        model = (
            getattr(self, "default_model", None)
            or next(iter(capability.supported_models), None)
            or self.provider_id
        )
        if capability.is_free and capability.is_local:
            return free_declaration(
                name,
                model,
                ProviderCostClass.LOCAL_FREE,
                "Miễn phí, chạy trên máy này.",
            )
        if capability.is_free:
            return free_declaration(
                name,
                model,
                ProviderCostClass.FREE_SERVICE,
                "Dịch vụ trực tuyến miễn phí, không cần API key.",
            )
        return paid_declaration(name, model, "API trả phí theo yêu cầu.")

    def diagnose(self) -> ProviderDiagnostic:
        """Run all verification probes and synthesize a unified diagnostic record."""
        conn = self.check_connection()
        models = self.check_models()
        creds = self.check_credentials()
        resources = self.check_resources()

        # Determine overall status
        status: ProviderStatus
        summary_parts: list[str] = []

        if not creds.valid:
            if not creds.has_credentials:
                status = ProviderStatus.NOT_CONFIGURED
                summary_parts.append("Credentials not configured")
            else:
                status = ProviderStatus.MISCONFIGURED
                summary_parts.append(
                    f"Invalid credentials: {creds.error_message or 'rejected'}"
                )
        elif not conn.connected:
            status = ProviderStatus.UNAVAILABLE
            summary_parts.append(
                f"Connection failed: {conn.error_message or 'offline'}"
            )
        elif not models.valid:
            status = ProviderStatus.DEGRADED
            summary_parts.append(
                f"Model missing: {models.error_message or 'no compatible model'}"
            )
        elif not resources.sufficient:
            status = ProviderStatus.DEGRADED
            warning = resources.warning_message or "insufficient resources"
            summary_parts.append(f"Resource constrained: {warning}")
        else:
            status = ProviderStatus.READY
            summary_parts.append("Ready for autonomous production")

        return ProviderDiagnostic(
            provider_id=self.provider_id,
            category=self.category,
            status=status,
            capability=self.capabilities(),
            connection=conn,
            models=models,
            credentials=creds,
            resources=resources,
            checked_at=datetime.now(UTC),
            summary="; ".join(summary_parts),
        )

    def check(self) -> None:
        """Health probe implementation conforming to HealthRegistry convention."""
        diagnostic = self.diagnose()
        if (
            diagnostic.status
            in (
                ProviderStatus.UNAVAILABLE,
                ProviderStatus.NOT_CONFIGURED,
                ProviderStatus.MISCONFIGURED,
            )
            or not diagnostic.models.valid
        ):
            msg = f"Provider '{self.provider_id}' is unavailable: {diagnostic.summary}"
            raise ProviderError(
                diagnostic.summary,
                provider=self.provider_id,
                code=f"{self.provider_id}.unavailable",
                user_message=msg,
                retryable=diagnostic.status == ProviderStatus.UNAVAILABLE,
            )
