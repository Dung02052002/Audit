"""Failure Injection Harness (Prompt Pack v8, prompt #222), area X4 Security & Recovery.

Provides deterministic fault injection across network, provider, video render,
and database boundaries to verify resilience, transaction rollback, and
fail-closed behavior.
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from ai_youtube_agent.core.db.database import Database, PersistenceError
from ai_youtube_agent.core.errors import ProviderError


class FaultTarget(StrEnum):
    """Subsystem categories targeted by failure injection."""

    NETWORK = "network"
    PROVIDER = "provider"
    RENDER = "render"
    DATABASE = "database"


@dataclass
class InjectedFault:
    """Mutable fault rule armed for a given target and optional operation name."""

    target: FaultTarget
    operation: str | None
    exception_factory: Callable[[], BaseException]
    remaining_triggers: int = 1
    triggered_count: int = 0


class FailureInjector:
    """Thread-safe deterministic fault injector for testing failure modes (#222)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._faults: list[InjectedFault] = []
        self._history: list[tuple[FaultTarget, str]] = []

    def arm(
        self,
        target: FaultTarget,
        exception_factory: Callable[[], BaseException],
        *,
        operation: str | None = None,
        times: int = 1,
    ) -> InjectedFault:
        if times < 1:
            raise ValueError("times must be >= 1")
        fault = InjectedFault(
            target=target,
            operation=operation,
            exception_factory=exception_factory,
            remaining_triggers=times,
        )
        with self._lock:
            self._faults.append(fault)
        return fault

    def arm_network_timeout(
        self,
        *,
        provider: str = "network",
        operation: str | None = None,
        times: int = 1,
    ) -> InjectedFault:
        return self.arm(
            FaultTarget.NETWORK,
            lambda: ProviderError(
                "Simulated network socket timeout",
                provider=provider,
                code=f"{provider}.timeout",
                retryable=True,
            ),
            operation=operation,
            times=times,
        )

    def arm_provider_failure(
        self,
        *,
        provider: str = "external_provider",
        code: str = "provider.unavailable",
        retryable: bool = True,
        operation: str | None = None,
        times: int = 1,
    ) -> InjectedFault:
        return self.arm(
            FaultTarget.PROVIDER,
            lambda: ProviderError(
                f"Simulated provider failure ({code})",
                provider=provider,
                code=code,
                retryable=retryable,
            ),
            operation=operation,
            times=times,
        )

    def arm_render_failure(
        self,
        *,
        code: str = "render.ffmpeg_failed",
        retryable: bool = False,
        operation: str | None = None,
        times: int = 1,
    ) -> InjectedFault:
        return self.arm(
            FaultTarget.RENDER,
            lambda: ProviderError(
                f"Simulated video render failure ({code})",
                provider="video_renderer",
                code=code,
                retryable=retryable,
            ),
            operation=operation,
            times=times,
        )

    def arm_database_failure(
        self,
        *,
        detail: str = "Simulated SQLite I/O or lock failure",
        operation: str | None = None,
        times: int = 1,
    ) -> InjectedFault:
        return self.arm(
            FaultTarget.DATABASE,
            lambda: PersistenceError(detail),
            operation=operation,
            times=times,
        )

    def maybe_trip(self, target: FaultTarget, operation: str = "default") -> None:
        """Raise the next matching armed exception if one is active."""
        to_raise: BaseException | None = None
        with self._lock:
            for fault in self._faults:
                if fault.remaining_triggers <= 0:
                    continue
                if fault.target is not target:
                    continue
                if fault.operation is not None and fault.operation != operation:
                    continue
                fault.remaining_triggers -= 1
                fault.triggered_count += 1
                self._history.append((target, operation))
                to_raise = fault.exception_factory()
                break
        if to_raise is not None:
            raise to_raise

    def clear(self) -> None:
        with self._lock:
            self._faults.clear()
            self._history.clear()

    @property
    def history(self) -> tuple[tuple[FaultTarget, str], ...]:
        with self._lock:
            return tuple(self._history)

    @contextmanager
    def guarded_transaction(
        self,
        database: Database,
        *,
        operation: str = "transaction",
        fail_before_commit: bool = False,
    ) -> Iterator[sqlite3.Connection]:
        """Run a ``Database.transaction()`` that injects faults.

        Faults are injected before or after SQL work.
        """
        if not fail_before_commit:
            self.maybe_trip(FaultTarget.DATABASE, operation)
        with database.transaction() as conn:
            yield conn
            if fail_before_commit:
                self.maybe_trip(FaultTarget.DATABASE, operation)

    def execute_with_injection(
        self,
        target: FaultTarget,
        operation: str,
        fn: Callable[[], Any],
    ) -> Any:
        self.maybe_trip(target, operation)
        return fn()
