"""Publish executor (Prompt Pack v8, prompts #149, #150, #152 and #153), context C12.

``PublishExecutor`` runs the part of a publish that is the same for Shorts and
LongForm, after a publisher has loaded and verified the approved artifacts and
mapped the metadata (``PublishContextLoader``, ``PublishMetadataMapper``):

1. ``replay`` (the duplicate guard found a succeeded job): return the stored result,
   upload nothing, evaluate nothing.
2. Check that the item can be published (``APPROVED``; or ``PUBLISHING`` when a
   failed job is resumed) and evaluate ``PublishGate`` while the item is
   ``APPROVED`` and before a job row exists (the daily limit and idempotency gates
   would otherwise see the new job). A resumed item is evaluated as ``APPROVED``:
   rights, policy, approval and limits are checked again on every attempt.
3. Get an OAuth token and verify access to the channel (no state is written yet).
4. In one transaction create or restart the ``PublishJob`` (``IN_PROGRESS``) and move
   the item ``APPROVED -> PUBLISHING``. A lost race (unique key or optimistic check)
   becomes ``DuplicatePublishError``.
5. Upload through ``YouTubeUploadProvider`` with the upload key of #151, retrying
   retryable provider errors with ``UploadRetryPolicy`` (#153); every retry is
   audited as ``publish.upload_retry``.
6. On success, in one transaction, store the result on the job and move the item
   ``PUBLISHING -> PUBLISHED``; audit ``<prefix>.published``.
7. On failure the job becomes ``FAILED`` with the error code
   (``<prefix>.publish_failed``
   is audited with the number of attempts made). If the failure is *resumable*
   (``RESUMABLE_UPLOAD_CODES``, or an unexpected error whose outcome is unknown) the
   item stays ``PUBLISHING``, so a later publish call restarts the same job; any
   other failure moves the item to ``FAILED``.

Nothing here publishes without a human approval: the approved request is a
precondition of ``PublishContextLoader`` and the approval gate is part of
``PublishGate``. Providers are chosen by the composition root (mocks by default).
"""

import dataclasses
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final

from ai_youtube_agent.core.audit import Actor, AuditLog, AuditResult, MetadataValue
from ai_youtube_agent.core.content_item import ContentItem, ContentStatus
from ai_youtube_agent.core.db.database import ConcurrencyError, Database
from ai_youtube_agent.core.db.repositories.content import ContentItemRepository
from ai_youtube_agent.core.db.repositories.publish import PublishJobRepository
from ai_youtube_agent.core.errors import ProviderError
from ai_youtube_agent.core.gates import GateBlockedError
from ai_youtube_agent.core.publish_gate import PublishGate
from ai_youtube_agent.pipeline.duplicate_guard import (
    DuplicateCheck,
    DuplicateDecision,
    DuplicatePublishError,
    concurrent_publish,
)
from ai_youtube_agent.pipeline.publish import PublishJob
from ai_youtube_agent.pipeline.publish_audit import (
    PUBLISH_AUTHORIZATION_FAILED as AUTHORIZATION_FAILED,
)
from ai_youtube_agent.pipeline.publish_audit import (
    PUBLISH_BLOCKED,
    PUBLISH_DUPLICATE_BLOCKED,
    PUBLISH_REPLAYED,
    PUBLISH_REQUESTED,
    PublishAuditRecorder,
    publish_failed_action,
    published_action,
)
from ai_youtube_agent.pipeline.publish_audit import (
    PUBLISH_TOKEN_RENEWED as TOKEN_RENEWED,
)
from ai_youtube_agent.pipeline.publish_audit import (
    PUBLISH_UPLOAD_RETRY as UPLOAD_RETRY,
)
from ai_youtube_agent.pipeline.publish_context import (
    PublishContext,
    PublishMedia,
    PublishOutcome,
)
from ai_youtube_agent.pipeline.publish_errors import PublishError, PublishErrorCode
from ai_youtube_agent.pipeline.publish_result import (
    PublishReceiptError,
    confirm_receipt,
)
from ai_youtube_agent.pipeline.token_recovery import (
    PublishAuthorizationError,
    TokenRecovery,
)
from ai_youtube_agent.pipeline.upload_retry import (
    UploadRetryPolicy,
    call_with_retry,
    is_resumable_error_code,
)
from ai_youtube_agent.providers.youtube_channel import (
    ChannelProviderErrorCode,
    RemoteChannelInfo,
    YouTubeChannelProvider,
    YouTubeChannelProviderError,
)
from ai_youtube_agent.providers.youtube_oauth import (
    OAuthScope,
    OAuthToken,
    YouTubeOAuthProvider,
)
from ai_youtube_agent.providers.youtube_upload import (
    UploadErrorCode,
    YouTubeUploadError,
    YouTubeUploadPayload,
    YouTubeUploadProvider,
    YouTubeUploadReceipt,
)

Clock = Callable[[], datetime]

UNEXPECTED_ERROR_CODE: Final[str] = "application.unexpected"
REQUIRED_UPLOAD_SCOPES: Final[tuple[OAuthScope, ...]] = (
    OAuthScope.YOUTUBE_UPLOAD,
    OAuthScope.YOUTUBE_READONLY,
)


@dataclass(frozen=True)
class PublishRequest:
    context: PublishContext
    media: PublishMedia
    payload: YouTubeUploadPayload
    actor: Actor
    check: DuplicateCheck
    audit_prefix: str
    channel_check: Callable[[RemoteChannelInfo], None] | None = None


class PublishExecutor:
    def __init__(
        self,
        database: Database,
        audit_log: AuditLog,
        publish_gate: PublishGate,
        oauth_provider: YouTubeOAuthProvider,
        channel_provider: YouTubeChannelProvider,
        upload_provider: YouTubeUploadProvider,
        *,
        retry_policy: UploadRetryPolicy | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Clock | None = None,
    ) -> None:
        self._database = database
        self._recorder = PublishAuditRecorder(audit_log)
        self._gate = publish_gate
        self._oauth = oauth_provider
        self._channels = channel_provider
        self._uploader = upload_provider
        self._recovery = TokenRecovery(oauth_provider, clock=clock)
        self._retry_policy = retry_policy or UploadRetryPolicy()
        self._sleep = sleep
        self._clock = clock

    def begin(self, context: PublishContext, *, actor: Actor) -> None:
        """Audit that a publish was requested (`publish.requested`, #156)."""
        self._recorder.item_event(
            PUBLISH_REQUESTED, AuditResult.SUCCESS, context, actor
        )

    def record_duplicate(
        self, context: PublishContext, *, actor: Actor, error: DuplicatePublishError
    ) -> None:
        """Audit a request the duplicate guard refused (`publish.duplicate_blocked`)."""
        self._recorder.item_event(
            PUBLISH_DUPLICATE_BLOCKED,
            AuditResult.DENIED,
            context,
            actor,
            {"error_code": error.code},
        )

    def replay(
        self, context: PublishContext, check: DuplicateCheck, *, actor: Actor
    ) -> PublishOutcome:
        """Return the stored result of a job that already succeeded (nothing is
        uploaded).
        """
        if check.decision is not DuplicateDecision.REPLAY or check.job is None:
            raise ValueError("only a REPLAY check carries a stored result")
        job = check.job
        assert job.result is not None
        self._recorder.job_event(
            PUBLISH_REPLAYED,
            AuditResult.SUCCESS,
            context,
            actor,
            job,
            context.item.status.value,
            {"youtube_video_id": job.result.youtube_video_id},
        )
        return PublishOutcome(job, None, None, replayed=True)

    def execute(self, request: PublishRequest) -> PublishOutcome:
        context = request.context
        if request.check.decision is DuplicateDecision.REPLAY:
            raise ValueError("a replay is returned by replay(), not executed")

        item = context.item
        if item.status not in (ContentStatus.APPROVED, ContentStatus.PUBLISHING) or (
            request.check.decision is DuplicateDecision.PROCEED
            and item.status is not ContentStatus.APPROVED
        ):
            raise PublishError(
                PublishErrorCode.ITEM_NOT_PUBLISHABLE,
                f"content item {item.id} is {item.status.value}",
            )

        now = self._now()
        gate_item = (
            item
            if item.status is ContentStatus.APPROVED
            else dataclasses.replace(item, status=ContentStatus.APPROVED)
        )
        try:
            self._gate.ensure_can_publish(gate_item, actor=request.actor, at=now)
        except GateBlockedError as blocked:
            self._audit_item_event(
                PUBLISH_BLOCKED,
                AuditResult.DENIED,
                request,
                {
                    "blocked_gates": ",".join(
                        r.gate.value for r in blocked.report.blocked
                    ),
                    "reason_codes": ",".join(r.code for r in blocked.report.reasons),
                },
            )
            raise

        token, remote = self._authorize(request)
        if request.channel_check is not None:
            request.channel_check(remote)

        running, publishing = self._start(request)
        attempts_made = 0
        renewals = 0

        def upload_once() -> YouTubeUploadReceipt:
            return self._uploader.upload(
                request.payload,
                video_bytes=request.media.video_bytes,
                thumbnail_bytes=request.media.thumbnail_bytes,
                captions_bytes=request.media.captions_bytes,
                token=token,
            )

        def attempt() -> YouTubeUploadReceipt:
            nonlocal attempts_made, renewals, token
            attempts_made += 1
            try:
                return upload_once()
            except YouTubeUploadError as error:
                if error.upload_code is not UploadErrorCode.UNAUTHORIZED or renewals:
                    raise
                # YouTube refused the token: renew it once (#154) and try again.
                renewals += 1
                token = self._recovery.renew(context.channel.id, REQUIRED_UPLOAD_SCOPES)
                self._audit_event(
                    TOKEN_RENEWED,
                    AuditResult.SUCCESS,
                    request,
                    running,
                    publishing,
                    {"stage": "upload"},
                )
                return upload_once()

        def on_retry(failed_attempt: int, error: ProviderError, delay: float) -> None:
            self._audit_event(
                UPLOAD_RETRY,
                AuditResult.FAILURE,
                request,
                running,
                publishing,
                {
                    "failed_attempt": failed_attempt,
                    "error_code": error.code,
                    "delay_seconds": delay,
                },
            )

        try:
            receipt = call_with_retry(
                attempt, self._retry_policy, sleep=self._sleep, on_retry=on_retry
            )
            confirm_receipt(receipt, request.payload)
        except Exception as error:
            code = getattr(error, "code", UNEXPECTED_ERROR_CODE)
            resumable = _is_resumable(error, code)
            self._fail(
                request, running, publishing, code, resumable, attempts_made, error
            )
            raise
        return self._succeed(request, running, publishing, receipt, attempts_made)

    def _authorize(
        self, request: PublishRequest
    ) -> tuple[OAuthToken, RemoteChannelInfo]:
        """Get a token and verify channel access, renewing an expired or refused token
        once (#154). Nothing is stored; a failure that needs a human is audited as
        `publish.authorization_failed` and raised as `PublishAuthorizationError`."""
        channel = request.context.channel
        try:
            acquired = self._recovery.acquire(channel.id, REQUIRED_UPLOAD_SCOPES)
        except PublishAuthorizationError as error:
            self._audit_authorization_failure(request, "token", error)
            raise
        token = acquired.token
        if acquired.renewed:
            self._audit_item_event(
                TOKEN_RENEWED, AuditResult.SUCCESS, request, {"stage": "token"}
            )
        try:
            remote = self._channels.verify_channel_access(
                channel.id, channel.youtube.channel_id, token=token
            )
        except YouTubeChannelProviderError as error:
            if error.channel_code is not ChannelProviderErrorCode.UNAUTHORIZED:
                raise
            try:
                token = self._recovery.renew(channel.id, REQUIRED_UPLOAD_SCOPES)
            except PublishAuthorizationError as auth_error:
                self._audit_authorization_failure(request, "channel", auth_error)
                raise
            self._audit_item_event(
                TOKEN_RENEWED, AuditResult.SUCCESS, request, {"stage": "channel"}
            )
            try:
                remote = self._channels.verify_channel_access(
                    channel.id, channel.youtube.channel_id, token=token
                )
            except YouTubeChannelProviderError as second:
                if second.channel_code is ChannelProviderErrorCode.UNAUTHORIZED:
                    refused = PublishAuthorizationError(channel.id, second.code)
                    self._audit_authorization_failure(request, "channel", refused)
                    raise refused from second
                raise
        return token, remote

    def _start(self, request: PublishRequest) -> tuple[PublishJob, ContentItem]:
        context = request.context
        item = context.item
        existing = context.existing_job
        try:
            with self._database.transaction() as connection:
                jobs = PublishJobRepository(connection)
                if existing is None:
                    created = PublishJob.create(
                        context.approval,
                        item.content_type,
                        context.keys.job_key,
                        clock=self._clock,
                    )
                    running = created.start(clock=self._clock)
                    jobs.add(running)
                else:
                    running = existing.start(clock=self._clock)
                    jobs.update(running, expected_updated_at=existing.updated_at)
                publishing = item
                if item.status is ContentStatus.APPROVED:
                    publishing = item.with_status(
                        ContentStatus.PUBLISHING, clock=self._clock
                    )
                    ContentItemRepository(connection).update(
                        publishing, expected_updated_at=item.updated_at
                    )
        except (sqlite3.IntegrityError, ConcurrencyError) as error:
            raise concurrent_publish(error, item.id) from error
        return running, publishing

    def _fail(
        self,
        request: PublishRequest,
        running: PublishJob,
        publishing: ContentItem,
        code: str,
        resumable: bool,
        attempts_made: int,
        error: BaseException,
    ) -> None:
        with self._database.transaction() as connection:
            failed_job = running.fail(code, clock=self._clock)
            PublishJobRepository(connection).update(
                failed_job, expected_updated_at=running.updated_at
            )
            failed_item = publishing
            if not resumable:
                failed_item = publishing.with_status(
                    ContentStatus.FAILED, clock=self._clock
                )
                ContentItemRepository(connection).update(
                    failed_item, expected_updated_at=publishing.updated_at
                )
        self._audit_event(
            publish_failed_action(request.audit_prefix),
            AuditResult.FAILURE,
            request,
            failed_job,
            failed_item,
            {
                "error_code": code,
                "error_type": type(error).__name__,
                "retryable": bool(getattr(error, "retryable", False)),
                "resumable": resumable,
                "upload_attempts": attempts_made,
            },
        )

    def _succeed(
        self,
        request: PublishRequest,
        running: PublishJob,
        publishing: ContentItem,
        receipt: YouTubeUploadReceipt,
        attempts_made: int,
    ) -> PublishOutcome:
        with self._database.transaction() as connection:
            done_job = running.succeed(receipt.youtube_video_id, clock=self._clock)
            PublishJobRepository(connection).update(
                done_job, expected_updated_at=running.updated_at
            )
            published = publishing.with_status(
                ContentStatus.PUBLISHED, clock=self._clock
            )
            ContentItemRepository(connection).update(
                published, expected_updated_at=publishing.updated_at
            )
        self._audit_event(
            published_action(request.audit_prefix),
            AuditResult.SUCCESS,
            request,
            done_job,
            published,
            {
                "youtube_video_id": receipt.youtube_video_id,
                "privacy_status": receipt.privacy_status.value,
                "contains_synthetic_media": receipt.contains_synthetic_media,
                "video_sha256": receipt.video_sha256,
                "upload_attempts": attempts_made,
            },
        )
        return PublishOutcome(done_job, receipt, request.payload)

    def _audit_authorization_failure(
        self, request: PublishRequest, stage: str, error: PublishAuthorizationError
    ) -> None:
        self._audit_item_event(
            AUTHORIZATION_FAILED,
            AuditResult.DENIED,
            request,
            {"stage": stage, "reason": error.reason},
        )

    def _audit_item_event(
        self,
        action: str,
        result: AuditResult,
        request: PublishRequest,
        extra: dict[str, MetadataValue],
    ) -> None:
        self._recorder.item_event(action, result, request.context, request.actor, extra)

    def _audit_event(
        self,
        action: str,
        result: AuditResult,
        request: PublishRequest,
        job: PublishJob,
        item: ContentItem,
        extra: dict[str, MetadataValue],
    ) -> None:
        self._recorder.job_event(
            action,
            result,
            request.context,
            request.actor,
            job,
            item.status.value,
            extra,
        )

    def _now(self) -> datetime:
        return self._clock() if self._clock else datetime.now(UTC)


def _is_resumable(error: BaseException, code: str) -> bool:
    """Whether a failed publish may be resumed later (the item stays PUBLISHING)."""
    if isinstance(error, ProviderError):
        return is_resumable_error_code(code)
    # A token that needs renewing by a human, or an unexpected error whose outcome is
    # unknown (the video may already be on YouTube): never mark the item failed.
    return not isinstance(error, PublishReceiptError | PublishError)
