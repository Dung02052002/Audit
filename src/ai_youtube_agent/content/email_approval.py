"""Email Approval service (Prompt Pack v8, prompt #138), context C11 Review.

``ApprovalEmailService`` composes and dispatches an approval notification email
for a pending ``ApprovalRequest`` through ``ApprovalEmailProvider``:

- Verifies the ``ApprovalRequest`` exists, is in ``PENDING`` status, binds a
  ``VIDEO`` artifact, and still matches the latest artifacts of the content item
  (no stale artifact kinds).
- Builds a safe preview reference (``/previews/{content_item_id}?version={v}`` or
  prefixed with a safe ``base_url``) that never leaks filesystem paths, storage
  URIs or secret tokens.
- Includes the exact ``approval_request_id``, ``content_item_id``,
  ``video_artifact_id``, ``video_artifact_version`` and ``video_artifact_sha256``
  in both plain-text and HTML bodies, plus the full list of bound artifact IDs
  and versions.
- Records an immutable ``approval.email_sent`` audit event with the provider
  receipt metadata.
"""

import html
from typing import Any

from ai_youtube_agent.content.approval import (
    ApprovalNotFoundError,
    ApprovalStateError,
    ApprovalStatus,
    stale_kinds,
)
from ai_youtube_agent.core.artifact import ArtifactKind
from ai_youtube_agent.core.audit import (
    Actor,
    ActorKind,
    AuditLog,
    AuditResult,
    EntityRef,
)
from ai_youtube_agent.core.db.database import Database
from ai_youtube_agent.core.db.repositories.content import (
    ArtifactRepository,
    ContentItemRepository,
)
from ai_youtube_agent.core.db.repositories.review import (
    ApprovalRequestRepository,
    QCResultRepository,
)
from ai_youtube_agent.providers.approval_email import (
    ApprovalEmailMessage,
    ApprovalEmailProvider,
    ApprovalEmailReceipt,
    _validate_safe_preview_url,
)

DEFAULT_ACTOR = Actor(ActorKind.SYSTEM, "email_approval")


class ApprovalEmailService:
    def __init__(
        self,
        database: Database,
        provider: ApprovalEmailProvider,
        audit_log: AuditLog,
        *,
        default_base_url: str = "",
    ) -> None:
        self._database = database
        self._provider = provider
        self._audit = audit_log
        self._default_base_url = default_base_url.strip().rstrip("/")

    def build_preview_url(
        self,
        content_item_id: str,
        *,
        video_version: int,
        approval_request_id: str,
        base_url: str | None = None,
    ) -> str:
        prefix = (
            base_url.strip().rstrip("/")
            if base_url is not None
            else self._default_base_url
        )
        path = (
            f"/previews/{content_item_id}"
            f"?version={video_version}&approval_request_id={approval_request_id}"
        )
        url = f"{prefix}{path}" if prefix else path
        _validate_safe_preview_url(url)
        return url

    def compose_message(
        self,
        approval_request_id: str,
        recipient: str,
        *,
        base_url: str | None = None,
    ) -> ApprovalEmailMessage:
        with self._database.transaction() as conn:
            approvals_repo = ApprovalRequestRepository(conn)
            request = approvals_repo.get(approval_request_id)
            if request is None:
                raise ApprovalNotFoundError(
                    f"approval request {approval_request_id} was not found"
                )
            item = ContentItemRepository(conn).get(request.content_item_id)
            if item is None:
                raise ApprovalNotFoundError(
                    f"content item {request.content_item_id} was not found"
                )
            artifacts = ArtifactRepository(conn).list_by_content_item(
                request.content_item_id
            )
            qc = (
                QCResultRepository(conn).get(request.qc_result_id)
                if request.qc_result_id
                else None
            )

        if request.status is not ApprovalStatus.PENDING:
            raise ApprovalStateError(
                f"approval {request.id} is {request.status.value}; "
                "approval email can only be sent for a pending request"
            )
        stale = stale_kinds(request, artifacts)
        if stale:
            kinds_str = ", ".join(k.value for k in stale)
            raise ApprovalStateError(
                f"approval {request.id} is stale for artifact kinds: {kinds_str}"
            )
        video_binding = request.binding_for(ArtifactKind.VIDEO)
        if video_binding is None:
            raise ApprovalStateError(
                f"approval {request.id} does not bind a video artifact"
            )

        preview_url = self.build_preview_url(
            request.content_item_id,
            video_version=video_binding.version,
            approval_request_id=request.id,
            base_url=base_url,
        )
        qc_status = qc.status.value if qc is not None else "unrecorded"
        subject = (
            f"[Approval Required] {item.title} "
            f"(v{video_binding.version} | {video_binding.artifact_id})"
        )

        artifact_lines = [
            f"- {b.kind.value}: id={b.artifact_id} version={b.version} sha256={b.sha256}"
            for b in request.artifacts
        ]
        text_body = "\n".join(
            [
                f"Approval is requested for content item: {item.title}",
                f"Content Item ID: {request.content_item_id}",
                f"Content Type: {item.content_type.value}",
                f"Approval Request ID: {request.id}",
                f"Video Artifact ID: {video_binding.artifact_id}",
                f"Video Artifact Version: {video_binding.version}",
                f"Video Artifact SHA-256: {video_binding.sha256}",
                f"QC Status: {qc_status}",
                f"Safe Preview Reference: {preview_url}",
                "",
                "Bound Artifacts:",
                *artifact_lines,
            ]
        )

        html_rows = "".join(
            f"<li><code>{html.escape(b.kind.value)}</code>: "
            f"id=<code>{html.escape(b.artifact_id)}</code> "
            f"v{b.version} "
            f"sha256=<code>{html.escape(b.sha256)}</code></li>"
            for b in request.artifacts
        )
        html_body = (
            "<div class=\"approval-email\">"
            f"<h2>Approval Required: {html.escape(item.title)}</h2>"
            f"<p><strong>Content Item ID:</strong> <code>{html.escape(request.content_item_id)}</code></p>"
            f"<p><strong>Approval Request ID:</strong> <code>{html.escape(request.id)}</code></p>"
            f"<p><strong>Video Artifact ID:</strong> <code>{html.escape(video_binding.artifact_id)}</code> "
            f"(v{video_binding.version})</p>"
            f"<p><strong>Video Artifact SHA-256:</strong> <code>{html.escape(video_binding.sha256)}</code></p>"
            f"<p><strong>QC Status:</strong> <code>{html.escape(qc_status)}</code></p>"
            f"<p><a href=\"{html.escape(preview_url, quote=True)}\">Open Safe Preview ({html.escape(preview_url)})</a></p>"
            f"<ul>{html_rows}</ul>"
            "</div>"
        )

        return ApprovalEmailMessage(
            recipient=recipient.strip(),
            subject=subject,
            approval_request_id=request.id,
            content_item_id=request.content_item_id,
            video_artifact_id=video_binding.artifact_id,
            video_artifact_version=video_binding.version,
            video_artifact_sha256=video_binding.sha256,
            preview_url=preview_url,
            text_body=text_body,
            html_body=html_body,
            artifacts=tuple(b.as_dict() for b in request.artifacts),
        )

    def send_approval_email(
        self,
        approval_request_id: str,
        recipient: str,
        *,
        base_url: str | None = None,
        actor: Actor = DEFAULT_ACTOR,
    ) -> dict[str, Any]:
        message = self.compose_message(
            approval_request_id, recipient, base_url=base_url
        )
        receipt: ApprovalEmailReceipt = self._provider.send(message)
        self._audit.record(
            "approval.email_sent",
            actor,
            EntityRef("approval_request", message.approval_request_id),
            AuditResult.SUCCESS,
            metadata={
                "content_item_id": message.content_item_id,
                "recipient": message.recipient,
                "message_id": receipt.message_id,
                "provider": receipt.provider,
                "video_artifact_id": message.video_artifact_id,
                "video_artifact_version": message.video_artifact_version,
                "video_artifact_sha256": message.video_artifact_sha256,
                "preview_url": message.preview_url,
            },
        )
        return {
            "message": message.as_dict(),
            "receipt": receipt.as_dict(),
        }
