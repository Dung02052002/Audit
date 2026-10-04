"""D-045 Language Settings (Prompt Pack v8, prompt #045).

Rules the user approved on 2026-10-01:

- tags are accepted in any case and stored in canonical BCP-47 case;
- secondary languages keep the user's order, at most 5, and no tag repeats
  (including the primary), case-insensitive;
- ``PUT /channels/{id}/strategy/languages`` works like the market: the first
  save creates the profile, later saves need ``expected_version``, only the
  languages change, and ``strategy.languages_changed`` is audited after commit.
"""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ai_youtube_agent.bootstrap import build_container
from ai_youtube_agent.content.strategy import (
    MAX_SECONDARY_LANGUAGES,
    SETTING_TYPES,
    LanguageSettings,
    Market,
    canonical_language_tag,
)
from ai_youtube_agent.content.strategy_settings import (
    StrategyConflictError,
    StrategySettings,
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
from ai_youtube_agent.core.db.repositories.channel import (
    ChannelRepository,
    StrategyProfileRepository,
)
from ai_youtube_agent.main import create_app
from factories import make_channel, make_strategy_profile

T0 = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
USER = Actor(ActorKind.USER, "owner")
SIX = ("en", "fr", "de", "es", "ja", "ko")


# Canonical case


@pytest.mark.parametrize(
    "tag, canonical",
    [
        ("VI", "vi"),
        ("en-us", "en-US"),
        ("EN-US", "en-US"),
        ("zh-hant-tw", "zh-Hant-TW"),
        ("ES-419", "es-419"),
        ("sr-LATN", "sr-Latn"),
        ("de-CH-1996", "de-CH-1996"),
        (" fil ", "fil"),
    ],
)
def test_tags_get_their_canonical_case(tag: str, canonical: str) -> None:
    assert canonical_language_tag(tag) == canonical


def test_canonical_settings_keep_the_order() -> None:
    settings = LanguageSettings.canonical("VI", ("fr", "EN-us", "ja"))

    assert settings == LanguageSettings("vi", ("fr", "en-US", "ja"))


# Entity rules


def test_at_most_five_secondary_languages() -> None:
    assert MAX_SECONDARY_LANGUAGES == 5
    assert len(LanguageSettings("vi", SIX[:5]).secondary) == 5
    with pytest.raises(ValueError, match="at most 5"):
        LanguageSettings("vi", SIX)


@pytest.mark.parametrize("secondary", [("VI",), ("en", "EN")])
def test_no_language_repeats_in_any_case(secondary: tuple[str, ...]) -> None:
    with pytest.raises(ValueError, match="repeat"):
        LanguageSettings.canonical("vi", secondary)


# Service


class Clock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> datetime:
        self.now += timedelta(seconds=1)
        return self.now


@pytest.fixture
def sink() -> InMemoryAuditSink:
    return InMemoryAuditSink()


@pytest.fixture
def service(database: Database, sink: InMemoryAuditSink) -> StrategySettings:
    return StrategySettings(database, AuditLog(sink), clock=Clock())


def stored_channel(database: Database):
    channel = make_channel()
    with database.transaction() as connection:
        ChannelRepository(connection).add(channel)
    return channel


def test_languages_can_create_the_profile(
    service: StrategySettings, database: Database, sink: InMemoryAuditSink
) -> None:
    channel = stored_channel(database)

    change = service.set_languages(
        channel.id, "VI", ("en-us",), expected_version=None, actor=USER
    )

    assert change.created
    assert change.profile.languages == LanguageSettings("vi", ("en-US",))
    assert change.profile.market is None
    assert service.get(channel.id) == change.profile
    events = sink.events()
    assert [e.action for e in events] == [
        "strategy.created",
        "strategy.languages_changed",
    ]
    assert dict(events[1].metadata) == {
        "channel_id": channel.id,
        "from": None,
        "to": "vi,en-US",
        "version": 1,
    }


def test_changing_languages_leaves_every_other_setting(
    service: StrategySettings, database: Database, sink: InMemoryAuditSink
) -> None:
    channel = stored_channel(database)
    full = make_strategy_profile(channel)
    with database.transaction() as connection:
        StrategyProfileRepository(connection).add(full)

    change = service.set_languages(
        channel.id, "en", ("vi", "fr"), expected_version=full.version, actor=USER
    )

    stored = service.get(channel.id)
    assert stored == change.profile
    assert stored.languages == LanguageSettings("en", ("vi", "fr"))
    assert stored.version == full.version + 1
    for name in SETTING_TYPES:
        if name != "languages":
            assert getattr(stored, name) == getattr(full, name)
    [event] = sink.events()
    assert dict(event.metadata)["from"] == "vi,en-US"
    assert dict(event.metadata)["to"] == "en,vi,fr"


def test_market_and_languages_build_one_profile(
    service: StrategySettings, database: Database
) -> None:
    channel = stored_channel(database)
    service.set_market(channel.id, "VN", expected_version=None, actor=USER)

    change = service.set_languages(channel.id, "vi", expected_version=1, actor=USER)

    assert not change.created
    assert change.profile.market == Market("VN")
    assert change.profile.languages == LanguageSettings("vi")
    assert change.profile.version == 2
    assert "market" not in change.profile.missing_settings
    assert "languages" not in change.profile.missing_settings


def test_the_same_languages_in_another_case_change_nothing(
    service: StrategySettings, database: Database, sink: InMemoryAuditSink
) -> None:
    channel = stored_channel(database)
    first = service.set_languages(
        channel.id, "vi", ("en-US",), expected_version=None, actor=USER
    )

    again = service.set_languages(
        channel.id, "VI", ("EN-us",), expected_version=1, actor=USER
    )

    assert again.profile == first.profile
    assert len(sink.events()) == 2


def test_reordering_secondary_languages_is_a_change(
    service: StrategySettings, database: Database
) -> None:
    channel = stored_channel(database)
    service.set_languages(
        channel.id, "vi", ("en", "fr"), expected_version=None, actor=USER
    )

    change = service.set_languages(
        channel.id, "vi", ("fr", "en"), expected_version=1, actor=USER
    )

    assert change.profile.languages.secondary == ("fr", "en")
    assert change.profile.version == 2


def test_a_stale_version_is_a_conflict(
    service: StrategySettings, database: Database
) -> None:
    channel = stored_channel(database)
    service.set_languages(channel.id, "vi", expected_version=None, actor=USER)

    with pytest.raises(StrategyConflictError):
        service.set_languages(channel.id, "en", expected_version=None, actor=USER)


# HTTP API


@pytest.fixture
def client(tmp_path: Path, database_copy):
    path = database_copy(tmp_path / "a.db")
    settings = Settings(environment=Environment.TEST, database_path=path)
    container = build_container(settings)
    with TestClient(create_app(container)) as test_client:
        test_client.sink = container.resolve(AuditSink)
        yield test_client


def new_channel(client: TestClient) -> str:
    response = client.post(
        "/channels", json={"title": "Money", "youtube_channel_id": "UC" + "y" * 22}
    )
    return response.json()["id"]


def test_put_languages_creates_then_updates(client: TestClient) -> None:
    url = f"/channels/{new_channel(client)}/strategy/languages"

    created = client.put(url, json={"primary": "VI", "secondary": ["en-us", "FR"]})
    updated = client.put(
        url, json={"primary": "vi", "secondary": [], "expected_version": 1}
    )

    assert created.status_code == 201
    assert created.json()["languages"] == {
        "primary": "vi",
        "secondary": ["en-US", "fr"],
    }
    assert "languages" not in created.json()["missing_settings"]
    assert updated.status_code == 200
    assert updated.json()["languages"] == {"primary": "vi", "secondary": []}
    assert updated.json()["version"] == 2


def test_secondary_is_optional(client: TestClient) -> None:
    url = f"/channels/{new_channel(client)}/strategy/languages"

    response = client.put(url, json={"primary": "ja"})

    assert response.status_code == 201
    assert response.json()["languages"] == {"primary": "ja", "secondary": []}


@pytest.mark.parametrize(
    "body, field",
    [
        ({}, "primary"),
        ({"primary": "english"}, "primary"),
        ({"primary": "e"}, "primary"),
        ({"primary": "vi", "secondary": ["en_US"]}, "secondary"),
        ({"primary": "vi", "secondary": list(SIX)}, "secondary"),
        ({"primary": "vi", "secondary": ["VI"]}, "secondary"),
        ({"primary": "vi", "secondary": ["en", "EN"]}, "secondary"),
        ({"primary": "vi", "secondary": "en"}, "secondary"),
        ({"primary": "vi", "expected_version": 0}, "expected_version"),
        ({"primary": "vi", "market": {"country": "VN"}}, "market"),
    ],
)
def test_put_languages_validation(client: TestClient, body: dict, field: str) -> None:
    url = f"/channels/{new_channel(client)}/strategy/languages"

    response = client.put(url, json=body)

    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "validation.invalid_request"
    assert field in [f["field"].split(".")[0] for f in error["fields"]]


def test_put_languages_conflict_and_audit(client: TestClient) -> None:
    url = f"/channels/{new_channel(client)}/strategy/languages"
    client.put(url, json={"primary": "vi"})

    stale = client.put(url, json={"primary": "en"})

    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "domain.strategy_conflict"
    assert [e.action for e in client.sink.events()][-2:] == [
        "strategy.created",
        "strategy.languages_changed",
    ]
