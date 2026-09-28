import dataclasses
from datetime import UTC, datetime, timedelta, timezone

import pytest

from ai_youtube_agent.content.channel import (
    Channel,
    ChannelArchivedError,
    ChannelStatus,
    YouTubeIdentifiers,
)
from ai_youtube_agent.core.errors import DomainError

CHANNEL_ID = "UC" + "a1B2_c3D-" * 2 + "e4F5"
T0 = datetime(2026, 9, 28, 10, 0, tzinfo=UTC)
T1 = T0 + timedelta(minutes=5)
YOUTUBE = YouTubeIdentifiers(CHANNEL_ID, "@my.channel")


def at(moment: datetime):
    return lambda: moment


def new_channel() -> Channel:
    return Channel.create("  My Channel  ", YOUTUBE, clock=at(T0))


# YouTube identifiers


def test_channel_id_fixture_has_the_youtube_length() -> None:
    assert len(CHANNEL_ID) == 24


@pytest.mark.parametrize(
    "channel_id",
    [
        "",
        "UC" + "a" * 21,
        "UC" + "a" * 23,
        "UX" + "a" * 22,
        "uc" + "a" * 22,
        "UC" + "a" * 21 + "!",
    ],
)
def test_channel_id_must_match_the_youtube_format(channel_id: str) -> None:
    with pytest.raises(ValueError):
        YouTubeIdentifiers(channel_id)


def test_handle_is_optional() -> None:
    assert YouTubeIdentifiers(CHANNEL_ID).handle is None


@pytest.mark.parametrize("handle", ["mychannel", "@ab", "@" + "a" * 31, "@my channel"])
def test_handle_must_match_the_youtube_format(handle: str) -> None:
    with pytest.raises(ValueError):
        YouTubeIdentifiers(CHANNEL_ID, handle)


# Creation


def test_create_sets_id_status_and_timestamps() -> None:
    channel = new_channel()

    assert channel.id
    assert channel.title == "My Channel"
    assert channel.youtube == YOUTUBE
    assert channel.status is ChannelStatus.PENDING
    assert channel.created_at == channel.updated_at == T0


def test_create_uses_utc_now_by_default() -> None:
    channel = Channel.create("My Channel", YOUTUBE)

    assert channel.created_at.tzinfo is UTC
    assert abs(datetime.now(UTC) - channel.created_at) < timedelta(seconds=5)


def test_every_channel_gets_its_own_id() -> None:
    assert new_channel().id != new_channel().id


def test_status_values() -> None:
    assert [status.value for status in ChannelStatus] == [
        "pending",
        "active",
        "paused",
        "disconnected",
        "archived",
    ]


# Validation


@pytest.mark.parametrize("title", ["", "   "])
def test_title_must_not_be_empty(title: str) -> None:
    with pytest.raises(ValueError):
        Channel.create(title, YOUTUBE, clock=at(T0))


@pytest.mark.parametrize(
    "moment",
    [
        datetime(2026, 9, 28, 10, 0),
        datetime(2026, 9, 28, 17, 0, tzinfo=timezone(timedelta(hours=7))),
    ],
)
def test_timestamps_must_be_utc(moment: datetime) -> None:
    with pytest.raises(ValueError):
        Channel.create("My Channel", YOUTUBE, clock=at(moment))


def test_updated_at_cannot_be_before_created_at() -> None:
    channel = new_channel()

    with pytest.raises(ValueError):
        channel.with_status(ChannelStatus.ACTIVE, clock=at(T0 - timedelta(seconds=1)))


def test_empty_id_is_rejected() -> None:
    with pytest.raises(ValueError):
        dataclasses.replace(new_channel(), id="")


# Changes


def test_channel_is_frozen() -> None:
    channel = new_channel()

    with pytest.raises(dataclasses.FrozenInstanceError):
        channel.status = ChannelStatus.ACTIVE  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        channel.youtube.channel_id = "UC" + "b" * 22  # type: ignore[misc]


def test_with_status_returns_a_new_channel_with_the_same_identity() -> None:
    channel = new_channel()

    active = channel.with_status(ChannelStatus.ACTIVE, clock=at(T1))

    assert active is not channel
    assert channel.status is ChannelStatus.PENDING
    assert active.status is ChannelStatus.ACTIVE
    assert active.id == channel.id
    assert active.youtube == channel.youtube
    assert active.created_at == T0
    assert active.updated_at == T1


@pytest.mark.parametrize("status", list(ChannelStatus))
def test_any_status_can_be_reached_from_a_live_channel(status: ChannelStatus) -> None:
    channel = new_channel().with_status(ChannelStatus.PAUSED, clock=at(T1))

    assert channel.with_status(status, clock=at(T1)).status is status


def test_same_status_is_a_no_op() -> None:
    channel = new_channel()

    assert channel.with_status(ChannelStatus.PENDING, clock=at(T1)) is channel


def test_rename_updates_title_and_updated_at() -> None:
    channel = new_channel()

    renamed = channel.rename("  New Name ", clock=at(T1))

    assert renamed.title == "New Name"
    assert renamed.id == channel.id
    assert renamed.updated_at == T1
    assert channel.rename("My Channel", clock=at(T1)) is channel


def test_rename_rejects_an_empty_title() -> None:
    with pytest.raises(ValueError):
        new_channel().rename("  ", clock=at(T1))


def test_archived_channel_is_read_only() -> None:
    archived = new_channel().with_status(ChannelStatus.ARCHIVED, clock=at(T1))

    assert archived.is_archived
    with pytest.raises(ChannelArchivedError) as raised:
        archived.with_status(ChannelStatus.ACTIVE, clock=at(T1))
    with pytest.raises(ChannelArchivedError):
        archived.rename("Other", clock=at(T1))
    assert archived.with_status(ChannelStatus.ARCHIVED) is archived
    assert isinstance(raised.value, DomainError)
    assert raised.value.to_public().http_status == 422
    assert archived.id not in raised.value.to_public().message


def test_as_dict() -> None:
    channel = new_channel()

    assert channel.as_dict() == {
        "id": channel.id,
        "title": "My Channel",
        "youtube": {"channel_id": CHANNEL_ID, "handle": "@my.channel"},
        "status": "pending",
        "created_at": "2026-09-28T10:00:00+00:00",
        "updated_at": "2026-09-28T10:00:00+00:00",
    }
