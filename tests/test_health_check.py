import logging
import threading
import time
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from ai_youtube_agent import __version__
from ai_youtube_agent.bootstrap import build_container
from ai_youtube_agent.core.config import Environment, Settings
from ai_youtube_agent.core.errors import ApplicationError, ProviderError
from ai_youtube_agent.core.flags import FeatureFlags
from ai_youtube_agent.core.health import (
    TIMEOUT_MESSAGE,
    CheckKind,
    CheckResult,
    HealthCheck,
    HealthRegistry,
    HealthRegistryError,
    HealthReport,
    HealthStatus,
    overall_status,
)
from ai_youtube_agent.core.log import Severity
from ai_youtube_agent.main import create_app


@pytest.fixture(autouse=True)
def restore_root_logger() -> Iterator[None]:
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    yield
    root.handlers[:] = handlers
    root.setLevel(level)


@pytest.fixture
def settings() -> Settings:
    return Settings(environment=Environment.TEST, log_level=Severity.WARNING)


def ok() -> None:
    return None


def fail_with(exc: Exception):
    def probe() -> None:
        raise exc

    return probe


# Status model


def test_overall_status_is_ok_when_every_check_is_ok() -> None:
    results = (
        CheckResult("a", CheckKind.APPLICATION, HealthStatus.OK),
        CheckResult("b", CheckKind.PROVIDER, HealthStatus.OK),
    )

    assert overall_status(results) is HealthStatus.OK


def test_overall_status_takes_the_worst_result() -> None:
    degraded = CheckResult("p", CheckKind.PROVIDER, HealthStatus.DEGRADED)
    down = CheckResult("a", CheckKind.APPLICATION, HealthStatus.DOWN)

    assert overall_status((degraded,)) is HealthStatus.DEGRADED
    assert overall_status((degraded, down)) is HealthStatus.DOWN


@pytest.mark.parametrize(
    ("status", "http_status"),
    [
        (HealthStatus.OK, 200),
        (HealthStatus.DEGRADED, 200),
        (HealthStatus.DOWN, 503),
    ],
)
def test_report_http_status(status: HealthStatus, http_status: int) -> None:
    assert HealthReport(status, "1.0", ()).http_status == http_status


def test_report_as_dict() -> None:
    report = HealthReport(
        HealthStatus.DEGRADED,
        "1.0",
        (CheckResult("youtube", CheckKind.PROVIDER, HealthStatus.DEGRADED, "x"),),
    )

    assert report.as_dict() == {
        "status": "degraded",
        "version": "1.0",
        "checks": [
            {
                "name": "youtube",
                "kind": "provider",
                "status": "degraded",
                "detail": "x",
            }
        ],
    }


def test_check_rejects_empty_name_and_bad_timeout() -> None:
    with pytest.raises(ValueError):
        HealthCheck("", CheckKind.APPLICATION, ok)
    with pytest.raises(ValueError):
        HealthCheck("a", CheckKind.APPLICATION, ok, timeout_seconds=0)


# Registry


def test_empty_registry_is_ok() -> None:
    report = HealthRegistry().run("1.0")

    assert report == HealthReport(HealthStatus.OK, "1.0", ())


def test_registry_rejects_duplicate_names() -> None:
    registry = HealthRegistry()
    registry.register(HealthCheck("a", CheckKind.APPLICATION, ok))

    with pytest.raises(HealthRegistryError):
        registry.register(HealthCheck("a", CheckKind.PROVIDER, ok))


def test_results_keep_registration_order() -> None:
    registry = HealthRegistry()
    for name in ("c", "a", "b"):
        registry.register(HealthCheck(name, CheckKind.APPLICATION, ok))

    report = registry.run("1.0")

    assert registry.names() == ("c", "a", "b")
    assert [check.name for check in report.checks] == ["c", "a", "b"]


def test_failing_application_check_means_down() -> None:
    registry = HealthRegistry()
    registry.register(HealthCheck("ok", CheckKind.APPLICATION, ok))
    registry.register(
        HealthCheck("db", CheckKind.APPLICATION, fail_with(RuntimeError("boom")))
    )

    report = registry.run("1.0")

    assert report.status is HealthStatus.DOWN
    assert report.checks[1].status is HealthStatus.DOWN


def test_failing_provider_check_means_degraded() -> None:
    registry = HealthRegistry()
    registry.register(HealthCheck("app", CheckKind.APPLICATION, ok))
    registry.register(
        HealthCheck(
            "youtube",
            CheckKind.PROVIDER,
            fail_with(ProviderError("quota exceeded", provider="youtube")),
        )
    )

    report = registry.run("1.0")

    assert report.status is HealthStatus.DEGRADED
    assert report.checks[0].status is HealthStatus.OK
    assert report.checks[1].status is HealthStatus.DEGRADED


def test_failure_detail_is_the_safe_message_only() -> None:
    registry = HealthRegistry()
    registry.register(
        HealthCheck(
            "secret",
            CheckKind.APPLICATION,
            fail_with(RuntimeError("password=hunter2")),
        )
    )
    registry.register(
        HealthCheck(
            "youtube",
            CheckKind.PROVIDER,
            fail_with(ProviderError("token=abc", provider="youtube")),
        )
    )

    report = registry.run("1.0")

    assert report.checks[0].detail == ApplicationError().user_message
    assert report.checks[1].detail == ProviderError(provider="x").user_message
    assert "hunter2" not in str(report.as_dict())
    assert "token=abc" not in str(report.as_dict())


def test_failure_is_logged_with_internal_detail(
    caplog: pytest.LogCaptureFixture,
) -> None:
    registry = HealthRegistry()
    registry.register(
        HealthCheck(
            "youtube",
            CheckKind.PROVIDER,
            fail_with(ProviderError("quota exceeded", provider="youtube")),
        )
    )

    with caplog.at_level(logging.WARNING, logger="ai_youtube_agent.core.health"):
        registry.run("1.0")

    [record] = caplog.records
    assert record.getMessage() == "health check failed"
    assert record.fields["check"] == "youtube"
    assert record.fields["error_detail"] == "quota exceeded"
    assert record.fields["provider"] == "youtube"


def test_slow_check_times_out_without_blocking_the_report() -> None:
    release = threading.Event()
    registry = HealthRegistry()
    registry.register(HealthCheck("fast", CheckKind.APPLICATION, ok))
    registry.register(
        HealthCheck(
            "slow",
            CheckKind.PROVIDER,
            lambda: release.wait(5),
            timeout_seconds=0.05,
        )
    )

    started = time.monotonic()
    try:
        report = registry.run("1.0")
    finally:
        release.set()

    assert time.monotonic() - started < 2
    assert report.status is HealthStatus.DEGRADED
    assert report.checks[1] == CheckResult(
        "slow", CheckKind.PROVIDER, HealthStatus.DEGRADED, TIMEOUT_MESSAGE
    )


# Wiring


def test_container_registers_application_checks(settings: Settings) -> None:
    registry = build_container(settings).resolve(HealthRegistry)

    assert registry.names() == ("settings", "feature_flags")
    assert build_container(settings).resolve(HealthRegistry) is not registry


def test_endpoint_reports_every_check(settings: Settings) -> None:
    response = TestClient(create_app(build_container(settings))).get("/health")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "version": __version__,
        "checks": [
            {
                "name": "settings",
                "kind": "application",
                "status": "ok",
                "detail": None,
            },
            {
                "name": "feature_flags",
                "kind": "application",
                "status": "ok",
                "detail": None,
            },
        ],
    }


def test_endpoint_returns_503_when_an_application_check_fails(
    settings: Settings,
) -> None:
    container = build_container(settings)
    client = TestClient(create_app(container))

    def broken(_: object) -> FeatureFlags:
        raise RuntimeError("flags unavailable")

    with container.override(FeatureFlags, broken):
        response = client.get("/health")

    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "down"
    assert body["checks"][1]["status"] == "down"
    assert "flags unavailable" not in response.text


def test_endpoint_stays_200_when_a_provider_is_degraded(settings: Settings) -> None:
    container = build_container(settings)
    container.resolve(HealthRegistry).register(
        HealthCheck(
            "youtube",
            CheckKind.PROVIDER,
            fail_with(ProviderError("down", provider="youtube")),
        )
    )

    response = TestClient(create_app(container)).get("/health")

    assert response.status_code == 200
    assert response.json()["status"] == "degraded"
