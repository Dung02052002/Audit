"""Approval HTTP API (Prompt Pack v8, prompts #137–#140), context C11 Review.

Exposes the HTTP endpoints for creating version-bound approval requests,
dispatching safe approval emails, and recording explicit human approve/reject
decisions:

- ``POST /approvals`` (#137): create an approval request bound to the exact
  artifact versions of a content item, optionally sending the approval email (#138).
- ``GET /approvals/{approval_request_id}``: inspect one approval request.
- ``GET /approvals/by-item/{content_item_id}``: list approval requests for an item.
- ``POST /approvals/{approval_request_id}/email`` (#138): send an approval email
  containing a safe preview reference and exact artifact IDs/hashes.
- ``POST /approvals/{approval_request_id}/approve`` (#139): explicit human
  approval action.
- ``POST /approvals/{approval_request_id}/reject`` (#140): explicit human
  reject action with reason.
"""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, status
from pydantic import BaseModel, ConfigDict, field_validator

from ai_youtube_agent.content.approval_service import ApprovalService
from ai_youtube_agent.content.email_approval import ApprovalEmailService
from ai_youtube_agent.core.audit import Actor
from ai_youtube_agent.core.http import current_actor, provide

router = APIRouter(prefix="/approvals", tags=["approvals"])

ApprovalServiceDep = Annotated[ApprovalService, provide(ApprovalService)]
EmailServiceDep = Annotated[ApprovalEmailService, provide(ApprovalEmailService)]
CurrentActor = Annotated[Actor, Depends(current_actor)]


class ApprovalCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    content_item_id: str
    expected_video_version: int | None = None
    expected_video_artifact_id: str | None = None
    recipient_email: str | None = None
    base_url: str | None = None

    @field_validator("content_item_id")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("content_item_id must not be empty")
        return value


class ApprovalEmailSendRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    recipient_email: str
    base_url: str | None = None

    @field_validator("recipient_email")
    @classmethod
    def _check_email(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("recipient_email must not be empty")
        return value


class ApproveActionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_video_artifact_id: str | None = None
    expected_video_version: int | None = None
    note: str | None = None


class RejectActionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str

    @field_validator("reason")
    @classmethod
    def _check_reason(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("reason must not be empty")
        return value


@router.post("", status_code=status.HTTP_201_CREATED)
def create_approval_request(
    body: ApprovalCreateRequest,
    service: ApprovalServiceDep,
    actor: CurrentActor,
) -> dict[str, Any]:
    result = service.request_approval(
        body.content_item_id,
        requested_by=actor,
        expected_video_version=body.expected_video_version,
        expected_video_artifact_id=body.expected_video_artifact_id,
        recipient_email=body.recipient_email,
        base_url=body.base_url,
    )
    return result.as_dict()


@router.get("/{approval_request_id}")
def get_approval_request(
    approval_request_id: str,
    service: ApprovalServiceDep,
) -> dict[str, Any]:
    return service.get(approval_request_id).as_dict()


@router.get("/by-item/{content_item_id}")
def list_approvals_by_item(
    content_item_id: str,
    service: ApprovalServiceDep,
) -> dict[str, Any]:
    requests = service.list_by_content_item(content_item_id)
    return {"approvals": [r.as_dict() for r in requests]}


@router.post("/{approval_request_id}/email")
def send_approval_email(
    approval_request_id: str,
    body: ApprovalEmailSendRequest,
    email_service: EmailServiceDep,
    actor: CurrentActor,
) -> dict[str, Any]:
    return email_service.send_approval_email(
        approval_request_id,
        body.recipient_email,
        base_url=body.base_url,
        actor=actor,
    )


@router.post("/{approval_request_id}/approve")
def approve_request(
    approval_request_id: str,
    service: ApprovalServiceDep,
    actor: CurrentActor,
    body: ApproveActionRequest | None = None,
) -> dict[str, Any]:
    req_body = body or ApproveActionRequest()
    return service.approve(
        approval_request_id,
        actor=actor,
        expected_video_artifact_id=req_body.expected_video_artifact_id,
        expected_video_version=req_body.expected_video_version,
        note=req_body.note,
    ).as_dict()


@router.post("/{approval_request_id}/reject")
def reject_request(
    approval_request_id: str,
    body: RejectActionRequest,
    service: ApprovalServiceDep,
    actor: CurrentActor,
) -> dict[str, Any]:
    return service.reject(
        approval_request_id,
        actor=actor,
        reason=body.reason,
    ).as_dict()
