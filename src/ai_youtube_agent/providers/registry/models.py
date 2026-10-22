"""Provider Registry Models & Data Structures.

Defines unified data models for capability declaration, connection testing,
model cataloging, credential validation, and resource requirement assessment
across all video production AI engines, desktop apps, and web services.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any


class ProviderCategory(StrEnum):
    """Functional category of a provider in the video production pipeline."""

    IMAGE_GENERATION = "image_generation"
    SPEECH_SYNTHESIS = "speech_synthesis"
    VIDEO_RENDER = "video_render"
    VIDEO_GENERATION = "video_generation"
    DESKTOP_EDITOR = "desktop_editor"
    BROWSER_AUTOMATION = "browser_automation"


class ProviderStatus(StrEnum):
    """Runtime availability and health status of a provider."""

    READY = "ready"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"
    NOT_CONFIGURED = "not_configured"
    MISCONFIGURED = "misconfigured"


@dataclass(frozen=True)
class ResourceRequirement:
    """Hardware and environment requirements for running a provider."""

    cpu_cores_min: int = 1
    ram_mb_min: int = 512
    vram_mb_min: int = 0
    disk_space_mb_min: int = 100
    requires_gpu: bool = False
    requires_internet: bool = False
    executable_name: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "cpu_cores_min": self.cpu_cores_min,
            "ram_mb_min": self.ram_mb_min,
            "vram_mb_min": self.vram_mb_min,
            "disk_space_mb_min": self.disk_space_mb_min,
            "requires_gpu": self.requires_gpu,
            "requires_internet": self.requires_internet,
            "executable_name": self.executable_name,
        }


@dataclass(frozen=True)
class ProviderCapability:
    """Unified capability declaration for any production tool or service."""

    provider_id: str
    name: str
    category: ProviderCategory
    is_local: bool
    is_free: bool
    supported_modalities: tuple[str, ...]
    supported_models: tuple[str, ...]
    resource_requirements: ResourceRequirement = field(
        default_factory=ResourceRequirement
    )
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "provider_id": self.provider_id,
            "name": self.name,
            "category": self.category.value,
            "is_local": self.is_local,
            "is_free": self.is_free,
            "supported_modalities": list(self.supported_modalities),
            "supported_models": list(self.supported_models),
            "resource_requirements": self.resource_requirements.as_dict(),
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True)
class ConnectionCheckResult:
    """Result of verifying connectivity to a provider's endpoint or process."""

    connected: bool
    latency_ms: float | None = None
    endpoint: str | None = None
    error_message: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "connected": self.connected,
            "latency_ms": self.latency_ms,
            "endpoint": self.endpoint,
            "error_message": self.error_message,
        }


@dataclass(frozen=True)
class ModelCheckResult:
    """Result of checking required AI models, checkpoints, or voice profiles."""

    valid: bool
    available_models: tuple[str, ...] = ()
    default_model: str | None = None
    missing_models: tuple[str, ...] = ()
    error_message: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "valid": self.valid,
            "available_models": list(self.available_models),
            "default_model": self.default_model,
            "missing_models": list(self.missing_models),
            "error_message": self.error_message,
        }


@dataclass(frozen=True)
class CredentialCheckResult:
    """Result of verifying API keys, tokens, or local authorization."""

    valid: bool
    has_credentials: bool
    auth_type: str = "none"
    quota_remaining: str | None = None
    error_message: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "valid": self.valid,
            "has_credentials": self.has_credentials,
            "auth_type": self.auth_type,
            "quota_remaining": self.quota_remaining,
            "error_message": self.error_message,
        }


@dataclass(frozen=True)
class ResourceCheckResult:
    """Result of verifying system resources (RAM, VRAM, disk space, binaries)."""

    sufficient: bool
    vram_available_mb: int | None = None
    disk_free_mb: int | None = None
    executable_found: bool = True
    details: Mapping[str, Any] = field(default_factory=dict)
    warning_message: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "sufficient": self.sufficient,
            "vram_available_mb": self.vram_available_mb,
            "disk_free_mb": self.disk_free_mb,
            "executable_found": self.executable_found,
            "details": dict(self.details),
            "warning_message": self.warning_message,
        }


@dataclass(frozen=True)
class ProviderDiagnostic:
    """Comprehensive diagnostic snapshot of a single provider."""

    provider_id: str
    category: ProviderCategory
    status: ProviderStatus
    capability: ProviderCapability
    connection: ConnectionCheckResult
    models: ModelCheckResult
    credentials: CredentialCheckResult
    resources: ResourceCheckResult
    checked_at: datetime
    summary: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "provider_id": self.provider_id,
            "category": self.category.value,
            "status": self.status.value,
            "capability": self.capability.as_dict(),
            "connection": self.connection.as_dict(),
            "models": self.models.as_dict(),
            "credentials": self.credentials.as_dict(),
            "resources": self.resources.as_dict(),
            "checked_at": self.checked_at.isoformat(),
            "summary": self.summary,
        }


@dataclass(frozen=True)
class RegistryDiagnosticReport:
    """Aggregated health and diagnostic report across all registered providers."""

    status: ProviderStatus
    total_providers: int
    ready_count: int
    diagnostics: tuple[ProviderDiagnostic, ...]
    checked_at: datetime

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "total_providers": self.total_providers,
            "ready_count": self.ready_count,
            "diagnostics": [d.as_dict() for d in self.diagnostics],
            "checked_at": self.checked_at.isoformat(),
        }
