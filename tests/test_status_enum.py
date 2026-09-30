"""C-031 Status Enum (Prompt Pack v8, prompt #031).

``ContentStatus`` in ``core/content_item.py`` is the single lifecycle status
enum. These tests check that it holds exactly the ten statuses #031 names, that
it accepts no other value, and that every status survives the database.
Transition rules belong to #032 and are not tested here.
"""

import json
import re
import sqlite3

import pytest

from ai_youtube_agent.core.content_item import ContentStatus
from ai_youtube_agent.core.db.database import Database
from ai_youtube_agent.core.db.repositories.channel import (
    ChannelRepository,
    StrategyProfileRepository,
)
from ai_youtube_agent.core.db.repositories.content import ContentItemRepository
from factories import make_channel, make_content_item, make_strategy_profile

# The statuses exactly as prompt #031 writes them, in the same order.
PROMPT_031 = [
    "Draft",
    "Generating",
    "Testing",
    "PreviewReady",
    "AwaitingApproval",
    "Approved",
    "Publishing",
    "Published",
    "Rejected",
    "Failed",
]


def snake_case(name: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()


def test_every_prompt_031_status_has_one_member_in_order() -> None:
    assert [s.value for s in ContentStatus] == [snake_case(n) for n in PROMPT_031]
    assert [s.name for s in ContentStatus] == [
        snake_case(n).upper() for n in PROMPT_031
    ]


def test_values_are_unique() -> None:
    assert len({s.value for s in ContentStatus}) == len(ContentStatus) == 10


@pytest.mark.parametrize("status", list(ContentStatus))
def test_each_status_is_its_own_stored_text(status: ContentStatus) -> None:
    assert str(status) == status.value
    assert ContentStatus(status.value) is status
    assert json.loads(json.dumps(status)) == status.value


@pytest.mark.parametrize(
    "value",
    [
        "PreviewReady",  # the display name is not the stored value
        "DRAFT",
        "Draft",
        " draft",
        "archived",  # a channel status, not a content status
        "pending",  # an approval status, not a content status
        "",
    ],
)
def test_values_outside_the_enum_are_refused(value: str) -> None:
    with pytest.raises(ValueError):
        ContentStatus(value)


@pytest.mark.parametrize("status", list(ContentStatus))
def test_every_status_round_trips_through_the_database(
    database: Database, status: ContentStatus
) -> None:
    channel = make_channel()
    strategy = make_strategy_profile(channel)
    item = make_content_item(channel, strategy).with_status(status)
    with database.transaction() as connection:
        ChannelRepository(connection).add(channel)
        StrategyProfileRepository(connection).add(strategy)
        ContentItemRepository(connection).add(item)

    with database.transaction() as connection:
        stored = ContentItemRepository(connection).get(item.id)
        (raw,) = connection.execute(
            "SELECT status FROM content_items WHERE id = ?", (item.id,)
        ).fetchone()

    assert stored == item
    assert stored.status is status
    assert raw == status.value


def test_the_database_refuses_a_status_outside_the_enum(database: Database) -> None:
    channel = make_channel()
    strategy = make_strategy_profile(channel)
    item = make_content_item(channel, strategy)
    with database.transaction() as connection:
        ChannelRepository(connection).add(channel)
        StrategyProfileRepository(connection).add(strategy)
        ContentItemRepository(connection).add(item)

    with pytest.raises(sqlite3.IntegrityError), database.transaction() as connection:
        connection.execute(
            "UPDATE content_items SET status = 'PreviewReady' WHERE id = ?",
            (item.id,),
        )
