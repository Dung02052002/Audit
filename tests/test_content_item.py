import dataclasses
from datetime import UTC, datetime, timedelta, timezone

import pytest

from ai_youtube_agent.core.content_item import (
    ContentItem,
    ContentStatus,
    ContentType,
)

T0 = datetime(2026, 9, 29, 10, 0, tzinfo=UTC)
T1 = T0 + timedelta(minutes=5)


def at(moment: datetime):
    return lambda: moment


def new_item(content_type: ContentType = ContentType.SHORTS) -> ContentItem:
    return ContentItem.create(
        "channel-1", "strategy-1", 3, content_type, "  First video  ", clock=at(T0)
    )


# Content types and statuses


def test_there_are_exactly_two_content_types() -> None:
    assert [t.value for t in ContentType] == ["shorts", "longform"]


def test_statuses_match_prompt_031() -> None:
    assert [s.value for s in ContentStatus] == [
        "draft",
        "generating",
        "testing",
        "preview_ready",
        "awaiting_approval",
        "approved",
        "publishing",
        "published",
        "rejected",
        "failed",
    ]


# Creating an item


@pytest.mark.parametrize("content_type", list(ContentType))
def test_create_builds_a_draft_for_each_content_type(
    content_type: ContentType,
) -> None:
    item = new_item(content_type)

    assert len(item.id) == 32
    assert item.channel_id == "channel-1"
    assert item.strategy_profile_id == "strategy-1"
    assert item.strategy_version == 3
    assert item.content_type is content_type
    assert item.title == "First video"
    assert item.status is ContentStatus.DRAFT
    assert item.created_at == item.updated_at == T0


def test_create_gives_each_item_its_own_id() -> None:
    assert new_item().id != new_item().id


def test_create_defaults_to_the_current_utc_time() -> None:
    before = datetime.now(UTC)
    item = ContentItem.create("c", "s", 1, ContentType.SHORTS, "Title")
    assert before <= item.created_at <= datetime.now(UTC)
    assert item.created_at.utcoffset() == timedelta(0)


@pytest.mark.parametrize(
    ("channel_id", "strategy_id", "version", "title"),
    [
        ("", "s", 1, "Title"),
        ("c", "", 1, "Title"),
        ("c", "s", 0, "Title"),
        ("c", "s", -1, "Title"),
        ("c", "s", True, "Title"),
        ("c", "s", 1.0, "Title"),
        ("c", "s", 1, "   "),
    ],
)
def test_create_rejects_missing_links_and_titles(
    channel_id, strategy_id, version, title
) -> None:
    with pytest.raises(ValueError):
        ContentItem.create(channel_id, strategy_id, version, ContentType.SHORTS, title)


@pytest.mark.parametrize("content_type", ["shorts", "SHORTS", "podcast", None])
def test_content_type_must_be_a_content_type(content_type) -> None:
    with pytest.raises(TypeError):
        ContentItem.create("c", "s", 1, content_type, "Title")


@pytest.mark.parametrize(
    "changes",
    [
        {"id": ""},
        {"status": "draft"},
        {"created_at": datetime(2026, 9, 29, 10, 0)},
        {"updated_at": T0.astimezone(timezone(timedelta(hours=7)))},
        {"updated_at": T0 - timedelta(seconds=1)},
    ],
)
def test_item_rejects_invalid_state(changes) -> None:
    values = {
        f.name: getattr(new_item(), f.name) for f in dataclasses.fields(ContentItem)
    }
    with pytest.raises((ValueError, TypeError)):
        ContentItem(**{**values, **changes})


def test_item_is_frozen() -> None:
    with pytest.raises(dataclasses.FrozenInstanceError):
        new_item().status = ContentStatus.PUBLISHED  # type: ignore[misc]


# Changing status and title


@pytest.mark.parametrize("status", list(ContentStatus)[1:])
def test_with_status_returns_a_new_item(status: ContentStatus) -> None:
    item = new_item()

    changed = item.with_status(status, clock=at(T1))

    assert changed.status is status
    assert changed.updated_at == T1
    assert (changed.id, changed.created_at, changed.content_type) == (
        item.id,
        item.created_at,
        item.content_type,
    )
    assert item.status is ContentStatus.DRAFT


def test_with_the_same_status_returns_the_same_item() -> None:
    item = new_item()
    assert item.with_status(ContentStatus.DRAFT, clock=at(T1)) is item


def test_with_status_rejects_unknown_values() -> None:
    with pytest.raises(TypeError):
        new_item().with_status("done", clock=at(T1))  # type: ignore[arg-type]


def test_rename_trims_and_updates_the_title() -> None:
    renamed = new_item().rename("  Second title ", clock=at(T1))
    assert renamed.title == "Second title"
    assert renamed.updated_at == T1


def test_rename_to_the_same_title_returns_the_same_item() -> None:
    item = new_item()
    assert item.rename(" First video ", clock=at(T1)) is item


def test_rename_rejects_an_empty_title() -> None:
    with pytest.raises(ValueError):
        new_item().rename("  ", clock=at(T1))


def test_strategy_link_is_kept_across_changes() -> None:
    item = new_item().with_status(ContentStatus.GENERATING, clock=at(T1))
    item = item.rename("New", clock=at(T1))
    assert (item.strategy_profile_id, item.strategy_version) == ("strategy-1", 3)


# Serialisation


def test_as_dict_is_json_friendly() -> None:
    item = new_item(ContentType.LONGFORM)

    assert item.as_dict() == {
        "id": item.id,
        "channel_id": "channel-1",
        "strategy_profile_id": "strategy-1",
        "strategy_version": 3,
        "content_type": "longform",
        "title": "First video",
        "status": "draft",
        "created_at": "2026-09-29T10:00:00+00:00",
        "updated_at": "2026-09-29T10:00:00+00:00",
    }
