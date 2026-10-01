"""Deterministic job keys (Prompt Pack v8, prompt #041), contexts C16 and C12.

The idempotency gate (#041) and whoever creates a job must derive the same
key for the same action. The recipes were approved by the user on 2026-10-01:

- ``generation_key(item, kind)``: ``gen:`` plus the sha256 of the job kind, the
  item id, the item's current status and its ``updated_at``. A repeated
  request from the same item state gives the same key; a real regeneration,
  after the item has moved on, gives a new one. The move into generating uses
  ``GENERATION_KIND`` (``content.generate``).
- ``publish_key(item_id, approval_request_id)``: ``pub:`` plus the sha256 of
  ``publish``, the item id and the approved request id, so a new approval
  gives a new key.

The parts are hashed as a JSON array, so no separator inside a part can make
two different actions collide. #151 builds on ``publish_key``.
"""

import hashlib
import json

from ai_youtube_agent.core.content_item import ContentItem

GENERATION_KIND = "content.generate"
GENERATION_PREFIX = "gen:"
PUBLISH_PREFIX = "pub:"


def generation_key(item: ContentItem, kind: str = GENERATION_KIND) -> str:
    return GENERATION_PREFIX + _digest(
        kind, item.id, item.status.value, item.updated_at.isoformat()
    )


def publish_key(content_item_id: str, approval_request_id: str) -> str:
    return PUBLISH_PREFIX + _digest("publish", content_item_id, approval_request_id)


def _digest(*parts: str) -> str:
    for part in parts:
        if not part.strip():
            raise ValueError("key parts must not be empty")
    canonical = json.dumps(list(parts), ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
