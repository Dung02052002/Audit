from fastapi.testclient import TestClient

from ai_youtube_agent import __version__
from ai_youtube_agent.main import app

client = TestClient(app)


def test_health_returns_ok() -> None:
    response = client.get("/health")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["version"] == __version__
    assert all(check["status"] == "ok" for check in body["checks"])
