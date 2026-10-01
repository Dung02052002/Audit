"""Channel settings API (Prompt Pack v8, prompt #043), context C1.

The HTTP API the Command Center (#191-#203) uses to configure channels, as the
user approved on 2026-10-01. There is no UI in this task.

- ``GET /channels``: every channel, as ``{"channels": [...]}``.
- ``GET /channels/{id}``: one channel.
- ``POST /channels``: create from ``title``, ``youtube_channel_id`` and an
  optional ``handle``. Answers 201.
- ``PATCH /channels/{id}``: change ``title``, ``handle`` (``null`` clears it)
  and ``status``, with the ``expected_updated_at`` last read. Fields left out
  stay as they are.

The schemas validate with the same rules as the entity (B-013), so a bad
request is refused as 422 ``validation.invalid_request`` with the failing
fields. Business refusals come from ``ChannelSettings``. Errors use the
envelope of ``core/http.py``.
"""

from datetime import datetime, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Depends, status
from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    ValidationInfo,
    field_validator,
)

from ai_youtube_agent.content.channel import (
    CHANNEL_ID_PATTERN,
    HANDLE_PATTERN,
    ChannelStatus,
)
from ai_youtube_agent.content.channel_settings import ChannelChanges, ChannelSettings
from ai_youtube_agent.core.audit import Actor
from ai_youtube_agent.core.http import current_actor, provide

router = APIRouter(prefix="/channels", tags=["channels"])

Settings = Annotated[ChannelSettings, provide(ChannelSettings)]
CurrentActor = Annotated[Actor, Depends(current_actor)]


def _title(value: str) -> str:
    value = value.strip()
    if not value:
        raise ValueError("title must not be empty")
    return value


def _handle(value: str | None) -> str | None:
    if value is not None and not HANDLE_PATTERN.match(value):
        raise ValueError(
            "handle must be '@' followed by 3 to 30 letters, digits, '.', '_' or '-'"
        )
    return value


Title = Annotated[str, AfterValidator(_title)]
Handle = Annotated[str | None, AfterValidator(_handle)]


class ChannelCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: Title
    youtube_channel_id: str
    handle: Handle = None

    @field_validator("youtube_channel_id")
    @classmethod
    def _check_channel_id(cls, value: str) -> str:
        if not CHANNEL_ID_PATTERN.match(value):
            raise ValueError(
                "youtube_channel_id must be 'UC' followed by 22 letters, "
                "digits, '_' or '-'"
            )
        return value


class ChannelUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_updated_at: datetime
    title: Title | None = None
    handle: Handle = None
    status: ChannelStatus | None = None

    @field_validator("expected_updated_at")
    @classmethod
    def _check_utc(cls, value: datetime) -> datetime:
        if value.utcoffset() != timedelta(0):
            raise ValueError("expected_updated_at must be a UTC time")
        return value

    @field_validator("title", "status")
    @classmethod
    def _not_null(cls, value: Any, info: ValidationInfo) -> Any:
        if value is None:
            raise ValueError(f"{info.field_name} cannot be null")
        return value

    def changes(self) -> ChannelChanges:
        sent = self.model_fields_set
        return ChannelChanges(
            title=self.title,
            handle=self.handle,
            clear_handle="handle" in sent and self.handle is None,
            status=self.status,
        )


@router.get("")
def list_channels(settings: Settings) -> dict[str, Any]:
    return {"channels": [channel.as_dict() for channel in settings.list()]}


@router.get("/{channel_id}")
def get_channel(channel_id: str, settings: Settings) -> dict[str, Any]:
    return settings.get(channel_id).as_dict()


@router.post("", status_code=status.HTTP_201_CREATED)
def create_channel(
    body: ChannelCreate, settings: Settings, actor: CurrentActor
) -> dict[str, Any]:
    channel = settings.create(
        body.title, body.youtube_channel_id, body.handle, actor=actor
    )
    return channel.as_dict()


@router.patch("/{channel_id}")
def update_channel(
    channel_id: str, body: ChannelUpdate, settings: Settings, actor: CurrentActor
) -> dict[str, Any]:
    channel = settings.update(
        channel_id,
        body.changes(),
        expected_updated_at=body.expected_updated_at,
        actor=actor,
    )
    return channel.as_dict()
