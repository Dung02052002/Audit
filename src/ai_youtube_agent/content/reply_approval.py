"""Human reply approval and posting workflow.

(Prompt Pack v8, prompt #186), context C15 Community.

``ReplyApprovalService`` manages the human review, approval, discarding, and
manual posting of ``ReplyDraft`` records (#026, #185):

- ``approve`` requires a human user actor (``actor.kind is ActorKind.USER``):
  AI or system actors cannot approve reply drafts (``USER_ACTOR_REQUIRED``).
- Optionally allows the human reviewer to edit ``edited_text`` during approval,
  re-validating length and channel ``banned_phrases`` before transitioning the
  draft from ``ReplyStatus.DRAFT`` (or ``FAILED``) to ``ReplyStatus.APPROVED``.
- ``discard`` transitions a ``DRAFT``, ``APPROVED``, or ``FAILED`` reply draft to
  ``ReplyStatus.DISCARDED``; a ``POSTED`` reply cannot be discarded.
- ``post_approved`` requires a human user actor and a draft in
  ``ReplyStatus.APPROVED`` (or ``POSTED`` for idempotent replay), verifies the
  channel is ``ACTIVE`` or ``PAUSED``, checks for any already-posted reply on the
  comment thread, calls ``CommentProvider.post_reply`` with an OAuth token
  carrying ``OAuthScope.YOUTUBE_FORCE_SSL``, and transitions the draft to
  ``ReplyStatus.POSTED`` with its ``youtube_reply_id`` (or ``ReplyStatus.FAILED``
  on provider error).
- Audits ``comment.reply_approved``, ``comment.reply_discarded``,
  ``comment.reply_posted``, and ``comment.reply_post_failed`` with scalar
  metadata only.
"""

import dataclasses
import hashlib
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from http import HTTPStatus
from typing import Any, Final

from ai_youtube_agent.content.channel import ChannelStatus
from ai_youtube_agent.content.channel_metrics import DEFAULT_SYNC_RETRY
from ai_youtube_agent.content.comment import Comment, ReplyDraft, ReplyStatus
from ai_youtube_agent.content.reply_draft import (
    REPLY_DRAFT_ENTITY,
    _validate_reply_text,
)
from ai_youtube_agent.core.audit import (
    Actor,
    ActorKind,
    AuditLog,
    AuditResult,
    EntityRef,
)
from ai_youtube_agent.core.db.database import Database
from ai_youtube_agent.core.db.repositories.channel import (
    ChannelRepository,
    StrategyProfileRepository,
)
from ai_youtube_agent.core.db.repositories.community import CommentRepository
from ai_youtube_agent.core.errors import DomainError
from ai_youtube_agent.pipeline.upload_retry import UploadRetryPolicy, call_with_retry
from ai_youtube_agent.providers.comments import (
    CommentProvider,
    CommentProviderError,
    ReplyPostReceipt,
    ReplyPostRequest,
)
from ai_youtube_agent.providers.youtube_oauth import (
    OAuthScope,
    YouTubeOAuthProvider,
    get_or_renew_token,
)

REPLY_APPROVED: Final[str] = "comment.reply_approved"
REPLY_DISCARDED: Final[str] = "comment.reply_discarded"
REPLY_POSTED: Final[str] = "comment.reply_posted"
REPLY_POST_FAILED: Final[str] = "comment.reply_post_failed"


def reply_post_idempotency_key(comment_id: str, text: str) -> str:
    """Derive a deterministic idempotency key for posting a reply to ``comment_id``."""
    normalized = " ".join(text.strip().lower().split())
    digest = hashlib.sha256(f"{comment_id}:{normalized}".encode()).hexdigest()[:24]
    return f"rpl:{comment_id}:{digest}"


class ReplyApprovalErrorCode(StrEnum):
    DRAFT_NOT_FOUND = "domain.reply_approval_draft_not_found"
    COMMENT_NOT_FOUND = "domain.reply_approval_comment_not_found"
    CHANNEL_NOT_FOUND = "domain.reply_approval_channel_not_found"
    CHANNEL_NOT_ACTIVE = "domain.reply_approval_channel_not_active"
    USER_ACTOR_REQUIRED = "domain.reply_approval_user_actor_required"
    INVALID_STATUS_TRANSITION = "domain.reply_approval_invalid_transition"
    NOT_APPROVED = "domain.reply_approval_not_approved"
    ALREADY_REPLIED = "domain.reply_approval_already_replied"


USER_MESSAGES: Final[dict[ReplyApprovalErrorCode, str]] = {
    ReplyApprovalErrorCode.DRAFT_NOT_FOUND: "The reply draft does not exist.",
    ReplyApprovalErrorCode.COMMENT_NOT_FOUND: "The parent comment does not exist.",
    ReplyApprovalErrorCode.CHANNEL_NOT_FOUND: "The channel does not exist.",
    ReplyApprovalErrorCode.CHANNEL_NOT_ACTIVE: (
        "Replies can only be posted for an active or paused channel."
    ),
    ReplyApprovalErrorCode.USER_ACTOR_REQUIRED: (
        "Only a human user can approve or manually post a comment reply."
    ),
    ReplyApprovalErrorCode.INVALID_STATUS_TRANSITION: (
        "The reply draft cannot transition from its current status."
    ),
    ReplyApprovalErrorCode.NOT_APPROVED: (
        "A reply draft must be approved before it can be posted."
    ),
    ReplyApprovalErrorCode.ALREADY_REPLIED: (
        "A reply has already been posted for this comment."
    ),
}

_STATUS: Final[dict[ReplyApprovalErrorCode, HTTPStatus]] = {
    ReplyApprovalErrorCode.DRAFT_NOT_FOUND: HTTPStatus.NOT_FOUND,
    ReplyApprovalErrorCode.COMMENT_NOT_FOUND: HTTPStatus.NOT_FOUND,
    ReplyApprovalErrorCode.CHANNEL_NOT_FOUND: HTTPStatus.NOT_FOUND,
    ReplyApprovalErrorCode.CHANNEL_NOT_ACTIVE: HTTPStatus.CONFLICT,
    ReplyApprovalErrorCode.USER_ACTOR_REQUIRED: HTTPStatus.FORBIDDEN,
    ReplyApprovalErrorCode.INVALID_STATUS_TRANSITION: HTTPStatus.CONFLICT,
    ReplyApprovalErrorCode.NOT_APPROVED: HTTPStatus.CONFLICT,
    ReplyApprovalErrorCode.ALREADY_REPLIED: HTTPStatus.CONFLICT,
}


class ReplyApprovalError(DomainError):
    default_code = ReplyApprovalErrorCode.NOT_APPROVED.value
    default_user_message = USER_MESSAGES[ReplyApprovalErrorCode.NOT_APPROVED]
    default_http_status = HTTPStatus.CONFLICT

    def __init__(self, code: ReplyApprovalErrorCode, detail: str) -> None:
        if not isinstance(code, ReplyApprovalErrorCode):
            raise TypeError("code must be a ReplyApprovalErrorCode")
        self.approval_code = code
        super().__init__(detail, code=code.value, user_message=USER_MESSAGES[code])
        self.default_http_status = _STATUS[code]


@dataclass(frozen=True)
class ReplyApprovalOutcome:
    comment: Comment
    draft: ReplyDraft
    edited: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "comment_id": self.comment.id,
            "draft": self.draft.as_dict(),
            "edited": self.edited,
        }


@dataclass(frozen=True)
class ReplyPostOutcome:
    comment: Comment
    draft: ReplyDraft
    receipt: ReplyPostReceipt | None
    replayed: bool

    @property
    def youtube_reply_id(self) -> str:
        assert self.draft.youtube_reply_id is not None
        return self.draft.youtube_reply_id

    def as_dict(self) -> dict[str, Any]:
        return {
            "comment_id": self.comment.id,
            "draft": self.draft.as_dict(),
            "youtube_reply_id": self.draft.youtube_reply_id,
            "receipt": self.receipt.as_dict() if self.receipt else None,
            "replayed": self.replayed,
        }


class ReplyApprovalService:
    """Human approval, discard, and posting service for comment reply drafts."""

    def __init__(
        self,
        database: Database,
        provider: CommentProvider,
        oauth_provider: YouTubeOAuthProvider,
        audit_log: AuditLog,
        *,
        duplicate_guard: Any | None = None,
        retry_policy: UploadRetryPolicy = DEFAULT_SYNC_RETRY,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._database = database
        self._provider = provider
        self._oauth = oauth_provider
        self._audit = audit_log
        self._duplicate_guard = duplicate_guard
        self._retry = retry_policy
        self._sleep = sleep

    def approve(
        self,
        draft_id: str,
        *,
        actor: Actor,
        edited_text: str | None = None,
    ) -> ReplyApprovalOutcome:
        """Approve a ``ReplyDraft`` as a human user, optionally editing its text."""
        _require_user_actor(actor, action="approve")
        with self._database.transaction() as connection:
            repo = CommentRepository(connection)
            draft, comment = _load_draft_and_comment(repo, draft_id)
            siblings = repo.list_reply_drafts(comment.id)
            if any(
                s.id != draft.id and s.status is ReplyStatus.POSTED for s in siblings
            ):
                raise ReplyApprovalError(
                    ReplyApprovalErrorCode.ALREADY_REPLIED,
                    f"comment {comment.id} already has a posted reply",
                )
            if draft.status is ReplyStatus.APPROVED and (
                edited_text is None or edited_text.strip() == draft.text
            ):
                return ReplyApprovalOutcome(comment, draft, False)
            if draft.status not in (
                ReplyStatus.DRAFT,
                ReplyStatus.FAILED,
                ReplyStatus.APPROVED,
            ):
                raise ReplyApprovalError(
                    ReplyApprovalErrorCode.INVALID_STATUS_TRANSITION,
                    f"cannot approve reply draft {draft.id} from status "
                    f"{draft.status.value}",
                )

            strategy = StrategyProfileRepository(connection).get_by_channel(
                comment.channel_id
            )
            banned_phrases = (
                strategy.brand.banned_phrases if strategy and strategy.brand else ()
            )
            new_text = edited_text.strip() if edited_text is not None else draft.text
            _validate_reply_text(new_text, banned_phrases=banned_phrases)
            edited = new_text != draft.text
            approved = dataclasses.replace(
                draft,
                text=new_text,
                status=ReplyStatus.APPROVED,
            )
            repo.update_reply_draft(approved, expected_status=draft.status)

        self._audit.record(
            REPLY_APPROVED,
            actor,
            EntityRef(REPLY_DRAFT_ENTITY, approved.id),
            AuditResult.SUCCESS,
            metadata={
                "channel_id": comment.channel_id,
                "comment_id": comment.id,
                "youtube_video_id": comment.youtube_video_id,
                "reply_draft_id": approved.id,
                "previous_status": draft.status.value,
                "status": approved.status.value,
                "edited": edited,
            },
        )
        return ReplyApprovalOutcome(comment, approved, edited)

    def discard(
        self,
        draft_id: str,
        *,
        actor: Actor,
        reason: str | None = None,
    ) -> ReplyDraft:
        """Discard a ``DRAFT``, ``APPROVED``, or ``FAILED`` reply draft."""
        if not isinstance(actor, Actor):
            raise TypeError("actor must be an Actor")
        with self._database.transaction() as connection:
            repo = CommentRepository(connection)
            draft, comment = _load_draft_and_comment(repo, draft_id)
            if draft.status is ReplyStatus.DISCARDED:
                return draft
            if draft.status is ReplyStatus.POSTED:
                raise ReplyApprovalError(
                    ReplyApprovalErrorCode.INVALID_STATUS_TRANSITION,
                    f"posted reply draft {draft.id} cannot be discarded",
                )
            discarded = dataclasses.replace(draft, status=ReplyStatus.DISCARDED)
            repo.update_reply_draft(discarded, expected_status=draft.status)

        self._audit.record(
            REPLY_DISCARDED,
            actor,
            EntityRef(REPLY_DRAFT_ENTITY, discarded.id),
            AuditResult.SUCCESS,
            metadata={
                "channel_id": comment.channel_id,
                "comment_id": comment.id,
                "youtube_video_id": comment.youtube_video_id,
                "reply_draft_id": discarded.id,
                "previous_status": draft.status.value,
                "status": discarded.status.value,
                "has_reason": bool(reason and reason.strip()),
            },
        )
        return discarded

    def post_approved(
        self,
        draft_id: str,
        *,
        actor: Actor,
    ) -> ReplyPostOutcome:
        """Post an ``APPROVED`` reply draft to YouTube after human approval."""
        _require_user_actor(actor, action="post")
        return self._execute_post(draft_id, actor=actor, require_human_approved=True)

    def _execute_post(
        self,
        draft_id: str,
        *,
        actor: Actor,
        require_human_approved: bool,
    ) -> ReplyPostOutcome:
        connection = self._database.connect()
        try:
            repo = CommentRepository(connection)
            draft, comment = _load_draft_and_comment(repo, draft_id)
            channel = ChannelRepository(connection).get(comment.channel_id)
            siblings = repo.list_reply_drafts(comment.id)
        finally:
            connection.close()

        if channel is None:
            raise ReplyApprovalError(
                ReplyApprovalErrorCode.CHANNEL_NOT_FOUND,
                f"channel {comment.channel_id} does not exist",
            )
        if channel.status not in (ChannelStatus.ACTIVE, ChannelStatus.PAUSED):
            raise ReplyApprovalError(
                ReplyApprovalErrorCode.CHANNEL_NOT_ACTIVE,
                f"channel {channel.id} has status {channel.status.value}",
            )

        # Idempotent replay when this exact draft is already POSTED
        if draft.status is ReplyStatus.POSTED:
            return ReplyPostOutcome(
                comment=comment,
                draft=draft,
                receipt=None,
                replayed=True,
            )

        if self._duplicate_guard is not None:
            self._duplicate_guard.ensure_can_post(draft, comment=comment, actor=actor)
        elif any(s.id != draft.id and s.status is ReplyStatus.POSTED for s in siblings):
            raise ReplyApprovalError(
                ReplyApprovalErrorCode.ALREADY_REPLIED,
                f"comment {comment.id} already has a posted reply",
            )

        if require_human_approved and draft.status is not ReplyStatus.APPROVED:
            raise ReplyApprovalError(
                ReplyApprovalErrorCode.NOT_APPROVED,
                f"reply draft {draft.id} has status {draft.status.value}, "
                "expected approved",
            )
        if not require_human_approved and draft.status not in (
            ReplyStatus.DRAFT,
            ReplyStatus.APPROVED,
        ):
            raise ReplyApprovalError(
                ReplyApprovalErrorCode.INVALID_STATUS_TRANSITION,
                f"reply draft {draft.id} cannot be posted from status "
                f"{draft.status.value}",
            )

        parent_yt_id = (
            comment.parent_comment_id
            if comment.parent_comment_id is not None
            else comment.youtube_comment_id
        )
        idem_key = reply_post_idempotency_key(comment.id, draft.text)
        request = ReplyPostRequest(
            channel_id=channel.id,
            youtube_channel_id=channel.youtube.channel_id,
            youtube_video_id=comment.youtube_video_id,
            parent_youtube_comment_id=parent_yt_id,
            reply_draft_id=draft.id,
            text=draft.text,
            idempotency_key=idem_key,
        )
        token = get_or_renew_token(
            self._oauth, channel.id, required_scopes=(OAuthScope.YOUTUBE_FORCE_SSL,)
        )

        try:
            receipt = call_with_retry(
                lambda: self._provider.post_reply(request, token=token),
                self._retry,
                sleep=self._sleep,
            )
        except CommentProviderError as exc:
            with self._database.transaction() as connection:
                repo = CommentRepository(connection)
                current = repo.get_reply_draft(draft.id)
                if current is not None and current.status is draft.status:
                    failed = dataclasses.replace(current, status=ReplyStatus.FAILED)
                    repo.update_reply_draft(failed, expected_status=current.status)
            self._audit.record(
                REPLY_POST_FAILED,
                actor,
                EntityRef(REPLY_DRAFT_ENTITY, draft.id),
                AuditResult.FAILURE,
                metadata={
                    "channel_id": channel.id,
                    "comment_id": comment.id,
                    "youtube_video_id": comment.youtube_video_id,
                    "reply_draft_id": draft.id,
                    "error_code": exc.code,
                    "retryable": exc.retryable,
                },
            )
            raise

        with self._database.transaction() as connection:
            repo = CommentRepository(connection)
            posted = dataclasses.replace(
                draft,
                status=ReplyStatus.POSTED,
                youtube_reply_id=receipt.youtube_reply_id,
            )
            repo.update_reply_draft(posted, expected_status=draft.status)

        self._audit.record(
            REPLY_POSTED,
            actor,
            EntityRef(REPLY_DRAFT_ENTITY, posted.id),
            AuditResult.SUCCESS,
            metadata={
                "channel_id": channel.id,
                "comment_id": comment.id,
                "youtube_video_id": comment.youtube_video_id,
                "reply_draft_id": posted.id,
                "youtube_reply_id": receipt.youtube_reply_id,
                "idempotency_key": idem_key,
                "provider": receipt.provider,
                "posted_at": receipt.posted_at.isoformat(),
            },
        )
        return ReplyPostOutcome(
            comment=comment,
            draft=posted,
            receipt=receipt,
            replayed=False,
        )


def _require_user_actor(actor: Actor, *, action: str) -> None:
    if not isinstance(actor, Actor):
        raise TypeError("actor must be an Actor")
    if actor.kind is not ActorKind.USER:
        raise ReplyApprovalError(
            ReplyApprovalErrorCode.USER_ACTOR_REQUIRED,
            f"actor {actor.kind.value}:{actor.id} cannot {action} a reply draft; "
            "human user required",
        )


def _load_draft_and_comment(
    repo: CommentRepository, draft_id: str
) -> tuple[ReplyDraft, Comment]:
    draft = repo.get_reply_draft(draft_id)
    if draft is None:
        raise ReplyApprovalError(
            ReplyApprovalErrorCode.DRAFT_NOT_FOUND,
            f"reply draft {draft_id} does not exist",
        )
    comment = repo.get(draft.comment_id)
    if comment is None:
        raise ReplyApprovalError(
            ReplyApprovalErrorCode.COMMENT_NOT_FOUND,
            f"comment {draft.comment_id} does not exist",
        )
    return draft, comment
