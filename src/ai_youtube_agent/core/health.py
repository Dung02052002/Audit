"""Health checks and status model (Prompt Pack v8, prompt #010).

A ``HealthCheck`` is a named probe. The probe returns normally when the thing
it checks is healthy and raises when it is not.

- An ``APPLICATION`` check covers this service itself. If it fails, the
  service is ``DOWN`` and ``GET /health`` answers 503.
- A ``PROVIDER`` check covers an external system. If it fails, the service is
  only ``DEGRADED`` and ``GET /health`` still answers 200, so one broken
  provider does not take the whole service out of rotation.

Checks are registered on the ``HealthRegistry`` singleton in the container.
Providers added by later prompts register their own checks in the
composition root (``ai_youtube_agent.bootstrap``).

Every probe runs with a timeout. A failure is reported with the safe message
from ``to_public`` and never with the exception text, which goes only to logs.
"""

import threading
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import dataclass
from enum import StrEnum
from http import HTTPStatus
from typing import Any

from ai_youtube_agent.core.errors import AppError, ApplicationError, to_public
from ai_youtube_agent.core.log import get_logger

logger = get_logger(__name__)

DEFAULT_TIMEOUT_SECONDS = 2.0
TIMEOUT_MESSAGE = "The health check did not answer in time."


class HealthStatus(StrEnum):
    OK = "ok"
    DEGRADED = "degraded"
    DOWN = "down"


class CheckKind(StrEnum):
    APPLICATION = "application"
    PROVIDER = "provider"


class HealthRegistryError(ApplicationError):
    default_code = "application.health_registry"


@dataclass(frozen=True)
class HealthCheck:
    name: str
    kind: CheckKind
    probe: Callable[[], object]
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("health check name must not be empty")
        if self.timeout_seconds <= 0:
            raise ValueError("health check timeout must be positive")

    @property
    def failure_status(self) -> HealthStatus:
        if self.kind is CheckKind.APPLICATION:
            return HealthStatus.DOWN
        return HealthStatus.DEGRADED


@dataclass(frozen=True)
class CheckResult:
    name: str
    kind: CheckKind
    status: HealthStatus
    detail: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "kind": self.kind.value,
            "status": self.status.value,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class HealthReport:
    status: HealthStatus
    version: str
    checks: tuple[CheckResult, ...]

    @property
    def http_status(self) -> int:
        if self.status is HealthStatus.DOWN:
            return int(HTTPStatus.SERVICE_UNAVAILABLE)
        return int(HTTPStatus.OK)

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "version": self.version,
            "checks": [check.as_dict() for check in self.checks],
        }


def overall_status(results: tuple[CheckResult, ...]) -> HealthStatus:
    statuses = {result.status for result in results}
    if HealthStatus.DOWN in statuses:
        return HealthStatus.DOWN
    if HealthStatus.DEGRADED in statuses:
        return HealthStatus.DEGRADED
    return HealthStatus.OK


class HealthRegistry:
    def __init__(self) -> None:
        self._checks: dict[str, HealthCheck] = {}
        self._lock = threading.Lock()

    def register(self, check: HealthCheck) -> None:
        with self._lock:
            if check.name in self._checks:
                raise HealthRegistryError(
                    f"health check {check.name!r} is already registered"
                )
            self._checks[check.name] = check

    def names(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(self._checks)

    def run(self, version: str) -> HealthReport:
        with self._lock:
            checks = tuple(self._checks.values())
        if not checks:
            return HealthReport(HealthStatus.OK, version, ())

        # A probe that hangs keeps its worker thread, so the pool is not
        # waited on. The report is returned as soon as every check has an
        # answer or has timed out.
        pool = ThreadPoolExecutor(
            max_workers=len(checks), thread_name_prefix="health-check"
        )
        try:
            futures = [(check, pool.submit(check.probe)) for check in checks]
            results = tuple(_result(check, future) for check, future in futures)
        finally:
            pool.shutdown(wait=False, cancel_futures=True)
        return HealthReport(overall_status(results), version, results)


def _result(check: HealthCheck, future: Future[object]) -> CheckResult:
    try:
        future.result(timeout=check.timeout_seconds)
    except FutureTimeout:
        logger.warning(
            "health check timed out",
            extra={"fields": _fields(check, timeout_seconds=check.timeout_seconds)},
        )
        return CheckResult(
            check.name, check.kind, check.failure_status, TIMEOUT_MESSAGE
        )
    except Exception as exc:
        extra = exc.log_fields() if isinstance(exc, AppError) else {}
        logger.warning(
            "health check failed",
            exc_info=exc,
            extra={"fields": _fields(check, **extra)},
        )
        return CheckResult(
            check.name, check.kind, check.failure_status, to_public(exc).message
        )
    return CheckResult(check.name, check.kind, HealthStatus.OK)


def _fields(check: HealthCheck, **extra: Any) -> dict[str, Any]:
    return {"check": check.name, "kind": check.kind.value, **extra}
