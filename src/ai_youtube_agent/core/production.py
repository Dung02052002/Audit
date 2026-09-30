"""Starting production (Prompt Pack v8, prompt #036), context C3 Control Gates.

``start_production`` is the writer the pipeline calls when a draft starts
production. Inside the caller's transaction it moves the item to generating
and appends a ``ProductionStart``, so the item and the log never disagree. The
caller runs the gates, including the daily limit gate, first.
"""

import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime

from ai_youtube_agent.core.content_item import ContentItem, ContentStatus
from ai_youtube_agent.core.db.repositories.content import ContentItemRepository
from ai_youtube_agent.core.db.repositories.usage import ProductionStartRepository
from ai_youtube_agent.core.production_start import (
    ProductionStart,
    ProductionStartError,
)

Clock = Callable[[], datetime]


def start_production(
    connection: sqlite3.Connection, item: ContentItem, *, clock: Clock | None = None
) -> tuple[ContentItem, ProductionStart]:
    """Move a draft to generating and log the start."""
    if item.status is not ContentStatus.DRAFT:
        raise ProductionStartError(
            f"content item {item.id} is {item.status.value}, not draft"
        )
    now = clock() if clock else datetime.now(UTC)
    moved = item.with_status(ContentStatus.GENERATING, clock=lambda: now)
    start = ProductionStart.of(item, now)
    ContentItemRepository(connection).update(moved, expected_updated_at=item.updated_at)
    ProductionStartRepository(connection).add(start)
    return moved, start
