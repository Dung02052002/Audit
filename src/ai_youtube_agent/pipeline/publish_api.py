"""HTTP API of the confirmed publish results (Prompt Pack v8, prompt #155), context C12.

Read-only: it shows where a published video is. There is deliberately no endpoint
here that starts a publish; publishing stays behind the approval, the gates and
``PUBLISH_ENABLED``.

- ``GET /publish-results/by-item/{content_item_id}``: the confirmed result of one item
  (404 when it was not published, 409 when the stored result and the item status
  disagree).
- ``GET /publish-results/by-channel/{channel_id}``: every confirmed result of a
  channel, oldest first.
- ``GET /publish-audit/by-item/{content_item_id}`` (#156): the audit trail of an item,
  its approval decisions and its publish events together, oldest first.
"""

from typing import Annotated, Any

from fastapi import APIRouter

from ai_youtube_agent.core.http import provide
from ai_youtube_agent.pipeline.publish_audit import PublishAuditService
from ai_youtube_agent.pipeline.publish_result import PublishResultService

router = APIRouter(prefix="/publish-results", tags=["publish-results"])
audit_router = APIRouter(prefix="/publish-audit", tags=["publish-audit"])

ResultServiceDep = Annotated[PublishResultService, provide(PublishResultService)]
AuditServiceDep = Annotated[PublishAuditService, provide(PublishAuditService)]


@router.get("/by-item/{content_item_id}")
def get_publish_result_of_item(
    content_item_id: str, service: ResultServiceDep
) -> dict[str, Any]:
    return service.for_content_item(content_item_id).as_dict()


@router.get("/by-channel/{channel_id}")
def list_publish_results_of_channel(
    channel_id: str, service: ResultServiceDep
) -> dict[str, Any]:
    records = service.list_for_channel(channel_id)
    return {"results": [record.as_dict() for record in records]}


@audit_router.get("/by-item/{content_item_id}")
def get_publish_audit_of_item(
    content_item_id: str, service: AuditServiceDep
) -> dict[str, Any]:
    return service.trail_for_content_item(content_item_id).as_dict()
