"""D-043 Channel Settings (Prompt Pack v8, prompt #043).

Rules the user approved on 2026-10-01:

- HTTP API only: list, get, create and update channels under ``/channels``;
- create takes title, YouTube channel id and an optional handle; update may
  change the title, set or clear the handle, and set active, paused or
  archived; the YouTube channel id never changes;
- every error uses one envelope ``{"error": {...}}``;
- requests act as the local user, updates need ``expected_updated_at``, and
  changes are audited after commit.
"""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ai_youtube_agent.bootstrap import build_container
from ai_youtube_agent.content.channel import (
    Channel,
    ChannelArchivedError,
    ChannelStatus,
    YouTubeIdentifiers,
)
from ai_youtube_agent.content.channel_settings import (
    ChannelChanges,
    ChannelConflictError,
    ChannelExistsError,
    ChannelNotFoundError,
    ChannelSettings,
    ChannelStatusNotAllowedError,
)
from ai_youtube_agent.core.audit import (
    Actor,
    ActorKind,
    AuditLog,
    AuditSink,
    InMemoryAuditSink,
)
from ai_youtube_agent.core.config import Environment, Settings
from ai_youtube_agent.core.db.database import Database
from ai_youtube_agent.core.db.repositories.channel import ChannelRepository
from ai_youtube_agent.core.http import LOCAL_USER, current_actor
from ai_youtube_agent.main import create_app

T0 = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
USER = Actor(ActorKind.USER, "owner")
YT_ID = "UC" + "a" * 22
OTHER_YT_ID = "UC" + "b" * 22


class Clock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> datetime:
        self.now += timedelta(seconds=1)
        return self.now


# Entity: with_handle


def channel() -> Channel:
    return Channel.create(
        "Money Minute", YouTubeIdentifiers(YT_ID, "@money"), clock=lambda: T0
    )


def test_with_handle_sets_and_clears_the_handle_only() -> None:
    original = channel()
    later = T0 + timedelta(minutes=1)

    changed = original.with_handle("@money.vn", clock=lambda: later)
    cleared = changed.with_handle(None, clock=lambda: later)

    assert changed.youtube == YouTubeIdentifiers(YT_ID, "@money.vn")
    assert changed.updated_at == later
    assert (changed.id, changed.created_at) == (original.id, original.created_at)
    assert cleared.youtube == YouTubeIdentifiers(YT_ID, None)


def test_with_handle_validates_and_keeps_an_unchanged_channel() -> None:
    original = channel()

    assert original.with_handle("@money") is original
    with pytest.raises(ValueError):
        original.with_handle("money")


def test_an_archived_channel_keeps_its_handle() -> None:
    archived = channel().with_status(ChannelStatus.ARCHIVED)

    with pytest.raises(ChannelArchivedError):
        archived.with_handle("@other")


# Service


@pytest.fixture
def sink() -> InMemoryAuditSink:
    return InMemoryAuditSink()


@pytest.fixture
def service(database: Database, sink: InMemoryAuditSink) -> ChannelSettings:
    return ChannelSettings(database, AuditLog(sink), clock=Clock())


def actions(sink: InMemoryAuditSink) -> list[str]:
    return [e.action for e in sink.events()]


def test_create_stores_a_pending_channel_and_audits_it(
    service: ChannelSettings, database: Database, sink: InMemoryAuditSink
) -> None:
    created = service.create("  Money Minute ", YT_ID, "@money", actor=USER)

    with database.transaction() as connection:
        assert ChannelRepository(connection).get(created.id) == created
    assert created.title == "Money Minute"
    assert created.status is ChannelStatus.PENDING
    [event] = sink.events()
    assert event.action == "channel.created"
    assert event.actor == USER
    assert (event.entity.type, event.entity.id) == ("channel", created.id)
    assert event.metadata["youtube_channel_id"] == YT_ID


def test_create_refuses_a_youtube_id_that_is_already_stored(
    service: ChannelSettings, sink: InMemoryAuditSink
) -> None:
    service.create("First", YT_ID, None, actor=USER)

    with pytest.raises(ChannelExistsError):
        service.create("Second", YT_ID, None, actor=USER)
    assert len(service.list()) == 1
    assert actions(sink) == ["channel.created"]


def test_get_and_list(service: ChannelSettings) -> None:
    a = service.create("A", YT_ID, None, actor=USER)
    b = service.create("B", OTHER_YT_ID, None, actor=USER)

    assert service.get(a.id) == a
    assert {c.id for c in service.list()} == {a.id, b.id}
    with pytest.raises(ChannelNotFoundError):
        service.get("missing")


def test_update_changes_title_handle_and_status(
    service: ChannelSettings, sink: InMemoryAuditSink
) -> None:
    created = service.create("Old", YT_ID, "@old", actor=USER)

    updated = service.update(
        created.id,
        ChannelChanges(title="New", handle="@new", status=ChannelStatus.ACTIVE),
        expected_updated_at=created.updated_at,
        actor=USER,
    )

    assert (updated.title, updated.youtube.handle) == ("New", "@new")
    assert updated.youtube.channel_id == YT_ID
    assert updated.status is ChannelStatus.ACTIVE
    assert service.get(created.id) == updated
    events = sink.events()[1:]
    assert [e.action for e in events] == ["channel.updated", "channel.status_changed"]
    assert events[0].metadata["fields"] == "title,handle"
    assert dict(events[1].metadata) == {"from": "pending", "to": "active"}


def test_update_can_clear_the_handle(service: ChannelSettings) -> None:
    created = service.create("A", YT_ID, "@money", actor=USER)

    updated = service.update(
        created.id,
        ChannelChanges(clear_handle=True),
        expected_updated_at=created.updated_at,
        actor=USER,
    )

    assert updated.youtube.handle is None


def test_an_update_that_changes_nothing_stores_and_audits_nothing(
    service: ChannelSettings, sink: InMemoryAuditSink
) -> None:
    created = service.create("A", YT_ID, "@money", actor=USER)

    same = service.update(
        created.id,
        ChannelChanges(title="A", handle="@money", status=ChannelStatus.PENDING),
        expected_updated_at=created.updated_at,
        actor=USER,
    )

    assert same == created
    assert actions(sink) == ["channel.created"]


@pytest.mark.parametrize("status", [ChannelStatus.PENDING, ChannelStatus.DISCONNECTED])
def test_a_user_cannot_set_system_statuses(
    service: ChannelSettings, status: ChannelStatus
) -> None:
    created = service.create("A", YT_ID, None, actor=USER)
    active = service.update(
        created.id,
        ChannelChanges(status=ChannelStatus.ACTIVE),
        expected_updated_at=created.updated_at,
        actor=USER,
    )

    with pytest.raises(ChannelStatusNotAllowedError):
        service.update(
            created.id,
            ChannelChanges(status=status),
            expected_updated_at=active.updated_at,
            actor=USER,
        )
    assert service.get(created.id) == active


def test_a_stale_update_is_refused_and_stores_nothing(
    service: ChannelSettings, sink: InMemoryAuditSink
) -> None:
    created = service.create("A", YT_ID, None, actor=USER)
    first = service.update(
        created.id,
        ChannelChanges(title="B"),
        expected_updated_at=created.updated_at,
        actor=USER,
    )

    with pytest.raises(ChannelConflictError):
        service.update(
            created.id,
            ChannelChanges(title="C"),
            expected_updated_at=created.updated_at,
            actor=USER,
        )
    assert service.get(created.id) == first
    assert actions(sink) == ["channel.created", "channel.updated"]


def test_an_archived_channel_cannot_change(service: ChannelSettings) -> None:
    created = service.create("A", YT_ID, None, actor=USER)
    archived = service.update(
        created.id,
        ChannelChanges(status=ChannelStatus.ARCHIVED),
        expected_updated_at=created.updated_at,
        actor=USER,
    )

    with pytest.raises(ChannelArchivedError):
        service.update(
            created.id,
            ChannelChanges(title="B"),
            expected_updated_at=archived.updated_at,
            actor=USER,
        )
    assert service.get(created.id) == archived


def test_updating_a_missing_channel_is_not_found(service: ChannelSettings) -> None:
    with pytest.raises(ChannelNotFoundError):
        service.update(
            "missing", ChannelChanges(title="A"), expected_updated_at=T0, actor=USER
        )


# HTTP API


@pytest.fixture
def client(tmp_path: Path, database_copy):
    path = database_copy(tmp_path / "api.db")
    settings = Settings(environment=Environment.TEST, database_path=path)
    container = build_container(settings)
    app = create_app(container)
    with TestClient(app, raise_server_exceptions=False) as test_client:
        test_client.sink = container.resolve(AuditSink)
        yield test_client


def create(client: TestClient, **overrides) -> dict:
    body = {"title": "Money Minute", "youtube_channel_id": YT_ID, "handle": "@money"}
    response = client.post("/channels", json=body | overrides)
    assert response.status_code == 201, response.json()
    return response.json()


def error(response) -> dict:
    return response.json()["error"]


def test_post_creates_a_channel(client: TestClient) -> None:
    body = create(client)

    assert body["title"] == "Money Minute"
    assert body["youtube"] == {"channel_id": YT_ID, "handle": "@money"}
    assert body["status"] == "pending"
    assert client.get(f"/channels/{body['id']}").json() == body
    assert client.get("/channels").json() == {"channels": [body]}


def test_requests_act_as_the_local_user(client: TestClient) -> None:
    body = create(client)

    [event] = client.sink.events()
    assert event.actor == LOCAL_USER
    assert event.entity.id == body["id"]
    assert current_actor() == LOCAL_USER


def test_patch_changes_fields_and_leaves_the_rest(client: TestClient) -> None:
    body = create(client)

    response = client.patch(
        f"/channels/{body['id']}",
        json={"expected_updated_at": body["updated_at"], "status": "active"},
    )

    assert response.status_code == 200
    updated = response.json()
    assert updated["status"] == "active"
    assert updated["title"] == body["title"]
    assert updated["youtube"] == body["youtube"]


def test_patch_with_null_handle_clears_it(client: TestClient) -> None:
    body = create(client)

    response = client.patch(
        f"/channels/{body['id']}",
        json={"expected_updated_at": body["updated_at"], "handle": None},
    )

    assert response.json()["youtube"]["handle"] is None


@pytest.mark.parametrize(
    "overrides, field",
    [
        ({"title": "   "}, "title"),
        ({"youtube_channel_id": "UCshort"}, "youtube_channel_id"),
        ({"handle": "no-at-sign"}, "handle"),
        ({"extra": 1}, "extra"),
    ],
)
def test_post_validation_errors_use_the_envelope(
    client: TestClient, overrides: dict, field: str
) -> None:
    body = {"title": "A", "youtube_channel_id": YT_ID} | overrides

    response = client.post("/channels", json=body)

    assert response.status_code == 422
    err = error(response)
    assert err["code"] == "validation.invalid_request"
    assert err["category"] == "domain"
    assert err["retryable"] is False
    assert [f["field"] for f in err["fields"]] == [field]
    assert all(f["message"] for f in err["fields"])


def test_post_reports_every_missing_field(client: TestClient) -> None:
    response = client.post("/channels", json={})

    fields = {f["field"] for f in error(response)["fields"]}
    assert fields == {"title", "youtube_channel_id"}


@pytest.mark.parametrize(
    "patch, field",
    [
        ({}, "expected_updated_at"),
        ({"expected_updated_at": "2026-10-01T12:00:00+07:00"}, "expected_updated_at"),
        ({"title": None}, "title"),
        ({"status": None}, "status"),
        ({"status": "sleeping"}, "status"),
        ({"youtube_channel_id": OTHER_YT_ID}, "youtube_channel_id"),
    ],
)
def test_patch_validation_errors(client: TestClient, patch: dict, field: str) -> None:
    body = create(client)
    if "expected_updated_at" not in patch and field != "expected_updated_at":
        patch = {"expected_updated_at": body["updated_at"]} | patch

    response = client.patch(f"/channels/{body['id']}", json=patch)

    assert response.status_code == 422
    assert field in [f["field"] for f in error(response)["fields"]]


def test_business_errors_use_their_own_status(client: TestClient) -> None:
    body = create(client)
    url = f"/channels/{body['id']}"

    duplicate = client.post(
        "/channels", json={"title": "B", "youtube_channel_id": YT_ID}
    )
    missing = client.get("/channels/missing")
    system_status = client.patch(
        url, json={"expected_updated_at": body["updated_at"], "status": "disconnected"}
    )
    client.patch(url, json={"expected_updated_at": body["updated_at"], "title": "B"})
    stale = client.patch(
        url, json={"expected_updated_at": body["updated_at"], "title": "C"}
    )

    assert (duplicate.status_code, error(duplicate)["code"]) == (
        409,
        "domain.channel_exists",
    )
    assert (missing.status_code, error(missing)["code"]) == (
        404,
        "domain.channel_not_found",
    )
    assert (system_status.status_code, error(system_status)["code"]) == (
        422,
        "domain.channel_status_not_allowed",
    )
    assert (stale.status_code, error(stale)["code"]) == (
        409,
        "domain.channel_conflict",
    )
    assert "fields" not in error(stale)


def test_an_archived_channel_is_refused(client: TestClient) -> None:
    body = create(client)
    url = f"/channels/{body['id']}"
    archived = client.patch(
        url, json={"expected_updated_at": body["updated_at"], "status": "archived"}
    ).json()

    response = client.patch(
        url, json={"expected_updated_at": archived["updated_at"], "title": "B"}
    )

    assert response.status_code == 422
    assert error(response)["code"] == "domain.channel_archived"


def test_unknown_routes_and_methods_use_the_envelope(client: TestClient) -> None:
    not_found = client.get("/nothing-here")
    wrong_method = client.delete("/channels")

    assert not_found.status_code == 404
    assert error(not_found)["code"] == "request.not_found"
    assert wrong_method.status_code == 405
    assert error(wrong_method)["code"] == "request.method_not_allowed"


def test_an_unexpected_error_is_a_safe_500(client: TestClient) -> None:
    class Exploding(ChannelSettings):
        def __init__(self) -> None:
            pass

        def list(self):
            raise RuntimeError("secret database path /var/db")

    with client.app.state.container.override(ChannelSettings, lambda _: Exploding()):
        response = client.get("/channels")

    assert response.status_code == 500
    err = error(response)
    assert err["code"] == "application.internal"
    assert "secret" not in response.text
