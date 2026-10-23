"""Tests for the APP-01 HTTP API: background runs, content item views, video
download and the settings whitelist. Mock providers only (never ffmpeg)."""

from __future__ import annotations

import hashlib
import shutil
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient
from tests.factories import make_channel, make_content_item, make_strategy_profile
from tests.test_script_to_video import SAMPLE_SHORTS_SCRIPT

from ai_youtube_agent.bootstrap import build_container
from ai_youtube_agent.content.app_views import content_disposition
from ai_youtube_agent.core.config import Environment, RenderProviderKind, Settings
from ai_youtube_agent.core.content_item import ContentType
from ai_youtube_agent.core.db.database import Database
from ai_youtube_agent.core.db.repositories.channel import (
    ChannelRepository,
    StrategyProfileRepository,
)
from ai_youtube_agent.core.db.repositories.content import ContentItemRepository
from ai_youtube_agent.core.di import Container
from ai_youtube_agent.main import create_app
from ai_youtube_agent.pipeline.job import AIJob
from ai_youtube_agent.pipeline.job_queue import SqliteJobQueue
from ai_youtube_agent.pipeline.script_to_video import (
    ScriptToVideoRequest,
    ScriptToVideoService,
)
from ai_youtube_agent.pipeline.script_to_video_runner import (
    ScriptToVideoRunner,
    job_as_dict,
)

WAIT_SECONDS = 60.0
TITLE = "Hương Trà Đêm Mưa"
SETTINGS_KEYS = {
    "app_name",
    "version",
    "environment",
    "debug",
    "log_level",
    "providers",
    "render",
    "flags",
    "approval_ttl_hours",
    "policy_rule_set_version",
    "rights_block_levels",
    "job_max_attempts",
    "upload_max_attempts",
}


@pytest.fixture
def settings(database: Database, tmp_path: Path) -> Settings:
    return Settings(
        environment=Environment.TEST,
        database_path=database.path,
        artifact_dir=tmp_path / "artifacts",
        render_provider=RenderProviderKind.MOCK,
    )


@pytest.fixture
def container(settings: Settings) -> Iterator[Container]:
    built = build_container(settings)
    yield built
    built.resolve(ScriptToVideoRunner).shutdown()


@pytest.fixture
def client(container: Container) -> TestClient:
    return TestClient(create_app(container))


def error(response: Any) -> dict[str, Any]:
    return response.json()["error"]


def _payload(**overrides: Any) -> dict[str, Any]:
    return {
        "script_text": SAMPLE_SHORTS_SCRIPT,
        "title": TITLE,
        "content_type": "shorts",
        "production_mode": "story_video",
        **overrides,
    }


def _run_to_end(client: TestClient, **overrides: Any) -> dict[str, Any]:
    response = client.post(
        "/script-to-video/background-runs", json=_payload(**overrides)
    )
    assert response.status_code == 202
    location = response.headers["Location"]
    assert location == f"/script-to-video/background-runs/{response.json()['run_id']}"
    deadline = time.monotonic() + WAIT_SECONDS
    while True:
        run = client.get(location).json()
        if run["status"] in ("succeeded", "failed"):
            return run
        if time.monotonic() > deadline:
            raise AssertionError("the background run did not finish in time")
        time.sleep(0.05)


def test_lists_are_empty_at_first(client: TestClient) -> None:
    assert client.get("/content-items").json() == {"items": [], "total": 0}
    assert client.get("/script-to-video/background-runs").json() == {"runs": []}


def test_a_background_run_is_accepted_and_succeeds(client: TestClient) -> None:
    run = _run_to_end(client)

    assert run["status"] == "succeeded"
    assert run["error"] is None
    assert run["request"]["title"] == TITLE
    assert run["request"]["script_chars"] == len(SAMPLE_SHORTS_SCRIPT)
    assert run["result"]["video_size_bytes"] > 0
    assert run["result"]["item_status"] == "preview_ready"
    assert run["progress"]["total"] == 14  # 13 stages + budget.checked (BUDGET-01)
    stages = {stage["id"]: stage["state"] for stage in run["progress"]["stages"]}
    assert stages["budget.checked"] == "done"
    listed = client.get("/script-to-video/background-runs").json()["runs"]
    assert [entry["run_id"] for entry in listed] == [run["run_id"]]

    job = client.get(f"/script-to-video/jobs/{run['job_id']}").json()
    assert job["status"] == "succeeded"
    assert job["tracked"] is False
    assert job["job"]["kind"] == "pipeline.script_to_video"
    assert job["progress"]["completed"] == run["progress"]["completed"]


def test_a_blank_script_is_422(client: TestClient) -> None:
    response = client.post(
        "/script-to-video/background-runs", json=_payload(script_text="  \n ")
    )

    assert response.status_code == 422
    assert error(response)["code"] == "pipeline.script_to_video_empty_script"


def test_an_unavailable_mode_is_409(client: TestClient) -> None:
    response = client.post(
        "/script-to-video/background-runs", json=_payload(production_mode="ai_film")
    )

    assert response.status_code == 409
    assert error(response)["code"] == "pipeline.script_to_video_mode_unavailable"


def test_a_script_path_is_refused(client: TestClient, tmp_path: Path) -> None:
    script = tmp_path / "script.txt"
    script.write_text(SAMPLE_SHORTS_SCRIPT, encoding="utf-8")

    response = client.post(
        "/script-to-video/background-runs",
        json=_payload(script_text=None, script_path=str(script)),
    )

    assert response.status_code == 422
    assert error(response)["code"] == "validation.invalid_request"
    assert error(response)["fields"][0]["field"] == "script_path"


def test_an_invalid_content_type_is_422(client: TestClient) -> None:
    response = client.post(
        "/script-to-video/background-runs", json=_payload(content_type="reel")
    )

    assert response.status_code == 422
    assert error(response)["code"] == "pipeline.script_to_video_invalid_input"


def test_an_item_awaiting_approval_cannot_be_regenerated(
    client: TestClient, container: Container
) -> None:
    manifest = container.resolve(ScriptToVideoService).run(
        ScriptToVideoRequest(
            script_text=SAMPLE_SHORTS_SCRIPT, title=TITLE, request_approval=True
        )
    )
    assert manifest.status.value == "awaiting_approval"

    response = client.post(
        "/script-to-video/background-runs",
        json=_payload(content_item_id=manifest.content_item_id, script_text="Mới."),
    )

    assert response.status_code == 409
    assert error(response)["code"] == "domain.content_item_not_editable"


def test_content_items_show_the_latest_job_and_video(client: TestClient) -> None:
    run = _run_to_end(client)
    item_id = run["content_item_id"]

    listed = client.get("/content-items").json()

    assert listed["total"] == 1
    (item,) = listed["items"]
    assert item["id"] == item_id
    assert item["title"] == TITLE
    assert item["status"] == "preview_ready"
    assert item["active_run_id"] is None
    assert item["latest_job"]["id"] == run["job_id"]
    assert item["latest_job"]["status"] == "succeeded"
    assert item["latest_job"]["last_stage"] == "preview.ready"
    assert item["video"]["artifact_id"] == run["result"]["video_artifact_id"]
    assert item["video"]["stream_url"].endswith(
        f"/artifacts/{item['video']['artifact_id']}/stream"
    )
    assert client.get("/content-items?status=preview_ready").json()["total"] == 1
    assert client.get("/content-items?status=draft").json()["total"] == 0
    assert client.get("/content-items?content_type=longform").json()["total"] == 0
    assert client.get("/content-items?status=nope").status_code == 422
    assert client.get("/content-items?limit=0").status_code == 422
    assert client.get("/content-items?limit=201").status_code == 422


def test_item_detail_has_jobs_checkpoints_and_artifacts(client: TestClient) -> None:
    run = _run_to_end(client)

    detail = client.get(f"/content-items/{run['content_item_id']}").json()

    assert detail["item"]["id"] == run["content_item_id"]
    assert detail["active_run_id"] is None
    jobs = {job["kind"]: job for job in detail["jobs"]}
    script_job = jobs["pipeline.script_to_video"]
    assert script_job["id"] == run["job_id"]
    steps = [checkpoint["step"] for checkpoint in script_job["checkpoints"]]
    assert steps[0] == "script.parsed"
    assert "video.rendered" in steps
    assert detail["latest_job"]["id"] == run["job_id"]
    assert detail["progress"]["completed"] == run["progress"]["completed"]
    artifacts = detail["artifacts"]
    assert artifacts["video"]["artifact_id"] == run["result"]["video_artifact_id"]
    assert artifacts["subtitles"] is not None
    assert artifacts["thumbnail"]["media_type"] == "image/png"
    scene_images = {
        scene["image"]["artifact_id"]
        for scene in client.get(
            f"/content-items/{run['content_item_id']}/scenes"
        ).json()["scenes"]
    }
    assert artifacts["thumbnail"]["artifact_id"] not in scene_images


def test_the_stored_script_is_returned(client: TestClient) -> None:
    run = _run_to_end(client)

    script = client.get(f"/content-items/{run['content_item_id']}/script").json()

    assert script["version"] == 1
    assert script["title"] == TITLE
    assert len(script["sections"]) == 3
    assert "Minh" in script["text"]


def test_scenes_come_from_the_run_then_from_persisted_data(
    client: TestClient, settings: Settings
) -> None:
    run = _run_to_end(client)
    item_id = run["content_item_id"]

    from_run = client.get(f"/content-items/{item_id}/scenes").json()

    assert from_run["source"] == "run_manifest"
    assert [scene["scene_id"] for scene in from_run["scenes"]] == [
        "scene_01",
        "scene_02",
        "scene_03",
    ]
    first = from_run["scenes"][0]
    assert first["narration"]
    assert first["visual_prompt"]
    assert first["image"]["media_type"] == "image/png"

    # A fresh container stands for a restarted server: no run is in memory.
    fresh = build_container(settings)
    try:
        restarted = TestClient(create_app(fresh))
        persisted = restarted.get(f"/content-items/{item_id}/scenes").json()
    finally:
        fresh.resolve(ScriptToVideoRunner).shutdown()

    assert persisted["source"] == "persisted"
    assert [
        (scene["scene_id"], scene["image"]["artifact_id"], scene["start_ms"])
        for scene in persisted["scenes"]
    ] == [
        (scene["scene_id"], scene["image"]["artifact_id"], scene["start_ms"])
        for scene in from_run["scenes"]
    ]
    assert all(scene["status"] == "rendered" for scene in persisted["scenes"])
    image = restarted.get(persisted["scenes"][0]["image"]["stream_url"])
    assert image.status_code == 200
    assert image.headers["content-type"] == "image/png"


def test_the_video_downloads_as_a_verified_attachment(client: TestClient) -> None:
    run = _run_to_end(client)

    response = client.get(f"/content-items/{run['content_item_id']}/video/download")

    assert response.status_code == 200
    assert response.headers["content-type"] == "video/mp4"
    assert hashlib.sha256(response.content).hexdigest() == run["result"]["video_sha256"]
    assert response.headers["etag"] == f'"{run["result"]["video_sha256"]}"'
    disposition = response.headers["content-disposition"]
    assert disposition.startswith('attachment; filename="huong-tra-dem-mua-v1.mp4"')
    assert f"filename*=UTF-8''{quote(TITLE + '-v1.mp4', safe='')}" in disposition
    disposition.encode("latin-1")


def test_a_download_needs_a_video_of_the_item(
    client: TestClient, database: Database
) -> None:
    channel = make_channel()
    strategy = make_strategy_profile(channel)
    item = make_content_item(channel, strategy)
    with database.transaction() as connection:
        ChannelRepository(connection).add(channel)
        StrategyProfileRepository(connection).add(strategy)
        ContentItemRepository(connection).add(item)

    no_video = client.get(f"/content-items/{item.id}/video/download")
    run = _run_to_end(client)
    detail = client.get(f"/content-items/{run['content_item_id']}").json()
    thumbnail_id = detail["artifacts"]["thumbnail"]["artifact_id"]
    not_a_video = client.get(
        f"/content-items/{run['content_item_id']}/video/download",
        params={"artifact_id": thumbnail_id},
    )
    other_item = client.get(
        f"/content-items/{item.id}/video/download",
        params={"artifact_id": run["result"]["video_artifact_id"]},
    )

    for response in (no_video, not_a_video, other_item):
        assert response.status_code == 404
        assert error(response)["code"] == "domain.video_not_found"
    assert client.get(f"/content-items/{item.id}/scenes").json() == {
        "source": None,
        "scenes": [],
    }
    assert error(client.get(f"/content-items/{item.id}/script"))["code"] == (
        "domain.script_not_found"
    )


def test_unknown_items_runs_and_jobs_are_404(client: TestClient) -> None:
    for path in (
        "/content-items/missing",
        "/content-items/missing/script",
        "/content-items/missing/scenes",
        "/content-items/missing/video/download",
    ):
        response = client.get(path)
        assert response.status_code == 404
        assert error(response)["code"] == "domain.content_item_not_found"

    run = client.get("/script-to-video/background-runs/run_missing")
    job = client.get("/script-to-video/jobs/job_missing")
    assert run.status_code == 404
    assert error(run)["code"] == "domain.run_not_found"
    assert job.status_code == 404
    assert error(job)["code"] == "domain.job_not_found"


def test_system_settings_is_a_whitelist(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(shutil, "which", lambda _name: None)
    missing = client.get("/system/settings").json()
    monkeypatch.setattr(shutil, "which", lambda name: f"C:/tools/{name}.exe")
    found = client.get("/system/settings").json()

    assert set(missing) == SETTINGS_KEYS
    assert missing["render"]["ffmpeg_found"] is False
    assert found["render"]["ffmpeg_found"] is True
    assert missing["render"]["provider"] == "mock"
    assert missing["render"]["ffmpeg_binary_name"] == "ffmpeg"
    assert set(missing["flags"]) == {
        "shorts_enabled",
        "longform_enabled",
        "publish_enabled",
        "test_required",
        "approval_required",
        "auto_reply_enabled",
    }
    text = client.get("/system/settings").text
    for forbidden in ("database_path", "artifact_dir", "**********", "tools"):
        assert forbidden not in text


def test_content_disposition_is_latin1_safe_for_any_title() -> None:
    header = content_disposition('Đêm "mưa" / 雨の夜', 3)

    header.encode("latin-1")
    assert 'filename="dem-mua-v3.mp4"' in header
    assert header.endswith(quote('Đêm "mưa" / 雨の夜-v3.mp4', safe=""))
    assert 'filename="video-v1.mp4"' in content_disposition("雨", 1)


def test_the_sync_run_endpoint_is_unchanged(client: TestClient) -> None:
    response = client.post("/script-to-video/runs", json=_payload())

    assert response.status_code == 201
    body = response.json()
    assert body["video"]["size_bytes"] > 0
    assert body["content_type"] == ContentType.SHORTS.value


def test_lease_tokens_in_job_checkpoints_are_redacted(
    client: TestClient, database: Database
) -> None:
    channel = make_channel()
    strategy = make_strategy_profile(channel)
    item = make_content_item(channel, strategy)
    with database.transaction() as connection:
        ChannelRepository(connection).add(channel)
        StrategyProfileRepository(connection).add(strategy)
        ContentItemRepository(connection).add(item)
    queue = SqliteJobQueue(database)
    queued = queue.enqueue_new("test.leased", "app01:lease", content_item_id=item.id)
    lease = queue.dequeue("worker-1", kinds=["test.leased"])
    assert lease is not None
    job_id = queued.job.id

    job = client.get(f"/script-to-video/jobs/{job_id}")
    detail = client.get(f"/content-items/{item.id}")

    assert job.status_code == 200
    assert detail.status_code == 200
    for response in (job, detail):
        assert lease.lease_token not in response.text
    (leased,) = job.json()["job"]["checkpoints"]
    assert leased["data"]["token"] == "[redacted]"
    assert leased["data"]["worker_id"] == "worker-1"
    (detail_job,) = detail.json()["jobs"]
    assert detail_job["checkpoints"][0]["data"]["token"] == "[redacted]"


def test_any_token_key_is_redacted_whatever_its_case() -> None:
    job = (
        AIJob.create("test.leased", "app01:token-case")
        .start()
        .checkpoint("lease.acquired", {"Token": "secret", "tokens": 2})
    )

    (saved,) = job_as_dict(job)["checkpoints"]

    assert saved["data"] == {"Token": "[redacted]", "tokens": 2}
