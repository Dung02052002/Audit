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

- ``upload_key(job_key, youtube_channel_id, video_sha256)`` (#151): ``upl:`` plus
  the sha256 of ``UPLOAD_KIND`` (``youtube.upload``), the publish job key, the
  YouTube channel id and the sha256 of the video bytes. It is the key sent to the
  upload provider with the payload, so a retry of the same job, to the same
  channel, with the same bytes, can be recognised by the provider (and so
  resumed instead of uploaded twice), while another channel or other bytes never
  share it. ``publish_key`` stays the key of the stored ``PublishJob``, which the
  idempotency gate reads.
- ``derive_publish_keys`` returns both keys together (``PublishKeys``);
  ``is_publish_key`` and ``is_upload_key`` check the shape of a stored or received
  key (prefix plus 64 lower-case hex digits).

The parts are hashed as a JSON array, so no separator inside a part can make
two different actions collide. #151 adds ``upload_key``; #152 uses both keys.
"""

import hashlib
import json
import re
from dataclasses import dataclass

from ai_youtube_agent.core.content_item import ContentItem

GENERATION_KIND = "content.generate"
GENERATION_PREFIX = "gen:"
PUBLISH_PREFIX = "pub:"
UPLOAD_KIND = "youtube.upload"
UPLOAD_PREFIX = "upl:"
AUDIO_KIND = "audio.generate"
AUDIO_PREFIX = "tts:"
RENDER_KIND = "video.render"
RENDER_PREFIX = "rnd:"

_DIGEST_PATTERN = re.compile(r"[0-9a-f]{64}")


def generation_key(item: ContentItem, kind: str = GENERATION_KIND) -> str:
    return GENERATION_PREFIX + _digest(
        kind, item.id, item.status.value, item.updated_at.isoformat()
    )


def publish_key(content_item_id: str, approval_request_id: str) -> str:
    return PUBLISH_PREFIX + _digest("publish", content_item_id, approval_request_id)


def upload_key(job_key: str, youtube_channel_id: str, video_sha256: str) -> str:
    if not is_publish_key(job_key):
        raise ValueError("job_key must be a publish key")
    if not _DIGEST_PATTERN.fullmatch(video_sha256):
        raise ValueError("video_sha256 must be 64 lower-case hex digits")
    return UPLOAD_PREFIX + _digest(
        UPLOAD_KIND, job_key, youtube_channel_id, video_sha256
    )


@dataclass(frozen=True)
class PublishKeys:
    """The two deterministic keys of one publish: the job key and the upload key."""

    job_key: str
    upload_key: str


def derive_publish_keys(
    content_item_id: str,
    approval_request_id: str,
    youtube_channel_id: str,
    video_sha256: str,
) -> PublishKeys:
    job_key = publish_key(content_item_id, approval_request_id)
    return PublishKeys(job_key, upload_key(job_key, youtube_channel_id, video_sha256))


def is_publish_key(value: object) -> bool:
    return _has_shape(value, PUBLISH_PREFIX)


def is_upload_key(value: object) -> bool:
    return _has_shape(value, UPLOAD_PREFIX)


def audio_key(script_id: str, voice_profile_id: str, voice_profile_version: int) -> str:
    return AUDIO_PREFIX + _digest(
        AUDIO_KIND, script_id, voice_profile_id, str(voice_profile_version)
    )


def render_key(content_item_id: str, input_sha256: str, version: int) -> str:
    return RENDER_PREFIX + _digest(
        RENDER_KIND, content_item_id, input_sha256, str(version)
    )


def _has_shape(value: object, prefix: str) -> bool:
    return (
        isinstance(value, str)
        and value.startswith(prefix)
        and bool(_DIGEST_PATTERN.fullmatch(value[len(prefix) :]))
    )


def _digest(*parts: str) -> str:
    for part in parts:
        if not part.strip():
            raise ValueError("key parts must not be empty")
    canonical = json.dumps(list(parts), ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
