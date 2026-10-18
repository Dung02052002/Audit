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
- ``audio_key(script_id, voice_profile_id, voice_profile_version)``: ``tts:``
  plus the sha256 of ``AUDIO_KIND`` (``audio.generate``), the script version id,
  the voice profile id and its version (#088). The same script voiced with the
  same profile version resumes the same TTS job; a new script version or a
  changed voice gives a new one.
- ``render_key(content_item_id, input_sha256, version)``: ``rnd:`` plus the
  sha256 of ``RENDER_KIND`` (``video.render``), the item id, the sha256 of
  everything the render reads (#100) and the VIDEO artifact version the render
  would create. The same inputs on top of the same latest video resume the
  same render job; new inputs or a newer latest video give a new one.

The parts are hashed as a JSON array, so no separator inside a part can make
two different actions collide. #151 builds on ``publish_key``.
"""

import hashlib
import json

from ai_youtube_agent.core.content_item import ContentItem

GENERATION_KIND = "content.generate"
GENERATION_PREFIX = "gen:"
PUBLISH_PREFIX = "pub:"
AUDIO_KIND = "audio.generate"
AUDIO_PREFIX = "tts:"
RENDER_KIND = "video.render"
RENDER_PREFIX = "rnd:"


def generation_key(item: ContentItem, kind: str = GENERATION_KIND) -> str:
    return GENERATION_PREFIX + _digest(
        kind, item.id, item.status.value, item.updated_at.isoformat()
    )


def publish_key(content_item_id: str, approval_request_id: str) -> str:
    return PUBLISH_PREFIX + _digest("publish", content_item_id, approval_request_id)


def audio_key(script_id: str, voice_profile_id: str, voice_profile_version: int) -> str:
    return AUDIO_PREFIX + _digest(
        AUDIO_KIND, script_id, voice_profile_id, str(voice_profile_version)
    )


def render_key(content_item_id: str, input_sha256: str, version: int) -> str:
    return RENDER_PREFIX + _digest(
        RENDER_KIND, content_item_id, input_sha256, str(version)
    )


def _digest(*parts: str) -> str:
    for part in parts:
        if not part.strip():
            raise ValueError("key parts must not be empty")
    canonical = json.dumps(list(parts), ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
