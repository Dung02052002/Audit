"""Publish precondition errors.

(Prompt Pack v8, prompts #149-#152), context C12 Publishing.

One typed ``PublishError`` (a ``DomainError``) with a closed ``PublishErrorCode``
is shared by the Shorts and LongForm publishers and by ``PublishContextLoader``.
The internal ``detail`` names ids only and is never shown; every code has a fixed
safe user message. Flags, content type, channel, approval, artifact and storage
problems all refuse the publish *before* a publish job exists, so nothing is
stored and the item stays ``APPROVED``.
"""

from enum import StrEnum
from http import HTTPStatus
from typing import Final

from ai_youtube_agent.core.errors import DomainError


class PublishErrorCode(StrEnum):
    SHORTS_DISABLED = "domain.publish_shorts_disabled"
    PUBLISH_DISABLED = "domain.publish_disabled"
    CONTENT_ITEM_NOT_FOUND = "domain.publish_content_item_not_found"
    WRONG_CONTENT_TYPE = "domain.publish_wrong_content_type"
    CHANNEL_NOT_FOUND = "domain.publish_channel_not_found"
    CHANNEL_NOT_ACTIVE = "domain.publish_channel_not_active"
    NO_APPROVAL = "domain.publish_no_approval"
    NOT_APPROVED = "domain.publish_not_approved"
    ARTIFACT_MISSING = "domain.publish_artifact_missing"
    STORAGE_CORRUPT = "domain.publish_storage_corrupt"
    DURATION_EXCEEDED = "domain.publish_duration_exceeded"
    LONG_UPLOADS_DISALLOWED = "domain.publish_long_uploads_disallowed"
    ITEM_NOT_PUBLISHABLE = "domain.publish_item_not_publishable"


USER_MESSAGES: Final[dict[PublishErrorCode, str]] = {
    PublishErrorCode.SHORTS_DISABLED: "Shorts publishing is currently disabled.",
    PublishErrorCode.PUBLISH_DISABLED: "Publishing to YouTube is currently disabled.",
    PublishErrorCode.CONTENT_ITEM_NOT_FOUND: (
        "The requested content item does not exist."
    ),
    PublishErrorCode.WRONG_CONTENT_TYPE: (
        "This publisher cannot publish that type of content item."
    ),
    PublishErrorCode.CHANNEL_NOT_FOUND: (
        "The channel for this content item does not exist."
    ),
    PublishErrorCode.CHANNEL_NOT_ACTIVE: (
        "The channel must be active before publishing."
    ),
    PublishErrorCode.NO_APPROVAL: "This content item has no approval request.",
    PublishErrorCode.NOT_APPROVED: (
        "The newest approval request for this content item is not approved."
    ),
    PublishErrorCode.ARTIFACT_MISSING: (
        "A required approved artifact for publishing is missing."
    ),
    PublishErrorCode.STORAGE_CORRUPT: (
        "An approved artifact file is missing or its digest does not match."
    ),
    PublishErrorCode.DURATION_EXCEEDED: (
        "The video is longer than this content type allows."
    ),
    PublishErrorCode.LONG_UPLOADS_DISALLOWED: (
        "This YouTube channel is not eligible for uploads longer than 15 minutes."
    ),
    PublishErrorCode.ITEM_NOT_PUBLISHABLE: (
        "This content item is not in a state that can be published."
    ),
}


class PublishError(DomainError):
    default_code = PublishErrorCode.PUBLISH_DISABLED.value
    default_user_message = USER_MESSAGES[PublishErrorCode.PUBLISH_DISABLED]
    default_http_status = HTTPStatus.UNPROCESSABLE_ENTITY

    def __init__(
        self,
        code: PublishErrorCode,
        detail: str,
        *,
        http_status: HTTPStatus = HTTPStatus.UNPROCESSABLE_ENTITY,
    ) -> None:
        if not isinstance(code, PublishErrorCode):
            raise TypeError("code must be a PublishErrorCode")
        self.publish_code = code
        super().__init__(detail, code=code.value, user_message=USER_MESSAGES[code])
        self.default_http_status = http_status
