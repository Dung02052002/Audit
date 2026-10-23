"""Tests for the BUDGET-01 HTTP API under /budget."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ai_youtube_agent.bootstrap import build_container
from ai_youtube_agent.content.paid_api_budget import (
    PaidBudgetErrorCode,
    PaidRequestContext,
    PaidRequestSpec,
    PaidTaskType,
)
from ai_youtube_agent.content.paid_api_budget_service import PaidApiBudgetService
from ai_youtube_agent.core.config import Environment, Settings
from ai_youtube_agent.core.db.database import Database
from ai_youtube_agent.core.di import Container
from ai_youtube_agent.main import create_app
from ai_youtube_agent.providers.provider_cost import ProviderPricing

SENTINEL_KEY = "sk-sentinel-elevenlabs-0123456789"
PRICING = ProviderPricing(
    unit="second",
    unit_price=Decimal("0.05"),
    currency="USD",
    source="test price sheet",
    verified_at=date(2026, 10, 1),
)


@pytest.fixture
def container(
    database: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Container:
    monkeypatch.setenv("ELEVENLABS_API_KEY", SENTINEL_KEY)
    return build_container(
        Settings(
            environment=Environment.TEST,
            database_path=database.path,
            artifact_dir=tmp_path / "artifacts",
        )
    )


@pytest.fixture
def client(container: Container) -> TestClient:
    return TestClient(create_app(container))


def open_quote(container: Container, fingerprint: str = "a" * 64):
    return container.resolve(PaidApiBudgetService).open_quote(
        PaidRequestSpec(
            provider="priced_video",
            model="clip-v1",
            task_type=PaidTaskType.VIDEO_CLIP_GENERATION,
            unit="second",
            unit_count=Decimal("4"),
            fingerprint=fingerprint,
            duration_ms=4000,
        ),
        PRICING,
        PaidRequestContext(),
    )


def paid_mode(client: TestClient) -> None:
    response = client.put(
        "/budget/paid-api-policy",
        json={"mode": "paid_with_per_request_approval", "expected_version": 0},
    )
    assert response.status_code == 200


def test_the_default_policy_and_provider_classes(client: TestClient) -> None:
    response = client.get("/budget/paid-api-policy")

    assert response.status_code == 200
    body = response.json()
    assert body["mode"] == "free_only"
    assert body["is_default"] is True
    assert body["version"] == 0
    assert body["updated_at"] is None
    assert body["updated_by_id"] is None
    assert body["modes"] == ["free_only", "paid_with_per_request_approval"]
    classes = {(p["provider"], p["task_type"]): p for p in body["providers"]}
    assert classes[("mock", "speech_synthesis")]["cost_class"] == "mock"
    assert (
        classes[("local_deterministic_png", "image_generation")]["cost_class"]
        == "local_free"
    )
    assert classes[("comfyui", "image_generation")]["cost_class"] == "local_free"
    assert classes[("edge_tts", "speech_synthesis")]["cost_class"] == "free_service"
    assert classes[("elevenlabs", "speech_synthesis")] == {
        "provider": "elevenlabs",
        "task_type": "speech_synthesis",
        "cost_class": "paid",
        "price_known": False,
    }
    assert classes[("video_api", "video_clip_generation")]["cost_class"] == "paid"
    assert SENTINEL_KEY not in response.text


def test_saving_the_policy(client: TestClient) -> None:
    saved = client.put(
        "/budget/paid-api-policy",
        json={"mode": "paid_with_per_request_approval", "expected_version": 0},
    )

    assert saved.status_code == 200
    assert saved.json()["mode"] == "paid_with_per_request_approval"
    assert saved.json()["version"] == 1
    assert saved.json()["is_default"] is False
    assert saved.json()["updated_by_id"] == "local-user"
    assert client.get("/budget/paid-api-policy").json()["version"] == 1


def test_a_stale_version_is_a_conflict(client: TestClient) -> None:
    paid_mode(client)

    response = client.put(
        "/budget/paid-api-policy", json={"mode": "free_only", "expected_version": 0}
    )

    assert response.status_code == 409
    assert (
        response.json()["error"]["code"]
        == PaidBudgetErrorCode.POLICY_VERSION_CONFLICT.value
    )


@pytest.mark.parametrize(
    "body",
    [
        {"mode": "spend_freely"},
        {"mode": "free_only", "budget_usd": 10},
        {"mode": "free_only", "expected_version": -1},
        {"mode": "free_only", "expected_version": "1"},
        {},
    ],
)
def test_an_invalid_policy_body_is_refused(client: TestClient, body: dict) -> None:
    response = client.put("/budget/paid-api-policy", json=body)

    assert response.status_code == 422
    assert client.get("/budget/paid-api-policy").json()["version"] == 0


def test_quotes_are_listed_read_and_filtered(
    client: TestClient, container: Container
) -> None:
    assert client.get("/budget/paid-api-quotes").json() == {"quotes": []}
    quote = open_quote(container)

    listed = client.get("/budget/paid-api-quotes").json()["quotes"]
    one = client.get(f"/budget/paid-api-quotes/{quote.id}")

    assert [entry["id"] for entry in listed] == [quote.id]
    assert one.status_code == 200
    assert one.json()["estimated_amount"] == "0.200000"
    assert one.json()["price_known"] is True
    assert one.json()["status"] == "pending"
    assert client.get(
        "/budget/paid-api-quotes", params={"status": "approved"}
    ).json() == {"quotes": []}
    assert client.get(
        "/budget/paid-api-quotes", params={"job_id": "missing"}
    ).json() == {"quotes": []}


@pytest.mark.parametrize("params", [{"limit": 0}, {"limit": 201}, {"status": "maybe"}])
def test_invalid_quote_filters_are_refused(client: TestClient, params: dict) -> None:
    assert client.get("/budget/paid-api-quotes", params=params).status_code == 422


def test_an_unknown_quote_is_not_found(client: TestClient) -> None:
    response = client.get("/budget/paid-api-quotes/pq_missing")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "budget.quote_not_found"


def test_approve_and_reject(client: TestClient, container: Container) -> None:
    paid_mode(client)
    quote = open_quote(container)
    other = open_quote(container, "b" * 64)

    approved = client.post(
        f"/budget/paid-api-quotes/{quote.id}/approve",
        json={
            "request_hash": quote.request_hash,
            "estimated_amount": "0.2",
            "currency": "USD",
        },
    )
    rejected = client.post(f"/budget/paid-api-quotes/{other.id}/reject")

    assert approved.status_code == 200
    assert approved.json()["status"] == "approved"
    assert approved.json()["decided_by_id"] == "local-user"
    assert rejected.status_code == 200
    assert rejected.json()["status"] == "rejected"


def test_a_mismatched_approval_is_a_conflict(
    client: TestClient, container: Container
) -> None:
    paid_mode(client)
    quote = open_quote(container)

    response = client.post(
        f"/budget/paid-api-quotes/{quote.id}/approve",
        json={
            "request_hash": quote.request_hash,
            "estimated_amount": "0.19",
            "currency": "USD",
        },
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "budget.quote_mismatch"


def test_approving_in_free_only_is_a_conflict(
    client: TestClient, container: Container
) -> None:
    quote = open_quote(container)

    response = client.post(
        f"/budget/paid-api-quotes/{quote.id}/approve",
        json={
            "request_hash": quote.request_hash,
            "estimated_amount": "0.200000",
            "currency": "USD",
        },
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "budget.paid_mode_disabled"


@pytest.mark.parametrize(
    "body",
    [
        {"request_hash": "x", "estimated_amount": "0.2", "currency": "USD"},
        {"request_hash": "a" * 64, "estimated_amount": "1e3", "currency": "USD"},
        {"request_hash": "a" * 64, "estimated_amount": "0.2", "currency": "usd"},
        {"request_hash": "a" * 64, "estimated_amount": 0.2, "currency": "USD"},
        {"request_hash": "a" * 64, "estimated_amount": "0.2"},
    ],
)
def test_an_invalid_approval_body_is_refused(
    client: TestClient, container: Container, body: dict
) -> None:
    paid_mode(client)
    quote = open_quote(container)

    response = client.post(f"/budget/paid-api-quotes/{quote.id}/approve", json=body)

    assert response.status_code == 422


def test_no_response_carries_a_key(client: TestClient, container: Container) -> None:
    paid_mode(client)
    quote = open_quote(container)
    bodies = [
        client.get("/budget/paid-api-policy").text,
        client.get("/budget/paid-api-quotes").text,
        client.get(f"/budget/paid-api-quotes/{quote.id}").text,
        client.post(f"/budget/paid-api-quotes/{quote.id}/reject").text,
    ]

    for body in bodies:
        assert SENTINEL_KEY not in body
        assert "api_key" not in body
