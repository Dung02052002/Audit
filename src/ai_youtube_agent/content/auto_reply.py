"""AUTO_REPLY_ENABLED flag enforcement (Prompt Pack v8, prompt #188), context C15 Community.

``AutoReplyGuard`` and ``AutoReplyService`` enforce that ``FeatureFlags.auto_reply_enabled``
is ``False`` by default (#006, #188) and that no comment reply is ever auto-posted
while the flag is off:

- ``FeatureFlags().auto_reply_enabled`` is ``False`` by default.
- ``AutoReplyGuard.require_enabled(draft_id=..., actor=...)`` raises
  ``AutoReplyDisabledError`` (403, ``domain.auto_reply_disabled``) and audits
  ``comment.auto_reply_blocked`` (``AuditResult.DENIED``) whenever
  ``flags.auto_reply_enabled`` is ``False``.
- ``AutoReplyService.auto_post_draft(draft_id, actor=...)`` always checks
  ``AutoReplyGuard.require_enabled`` first. When ``auto_reply_enabled`` is
  ``False``, nothing is posted and the draft remains in its existing state.
  Even when ``auto_reply_enabled`` is explicitly enabled, ``auto_post_draft``
  still refuses comments classified as ``CommentLabel.MODERATION`` and enforces
  ``DuplicateReplyGuard`` (#187) and channel banned phrases (#185).
"""

from dataclasses import dataclass
from enum import StrEnum
from http import HTTPStatus
from typing import Any, Final

from ai_youtube_agent.content.comment import CommentLabel, ReplyDraft
from ai_youtube_agent.content.comment_classifier import evaluate_comment_rules
from ai_youtube_agent.content.reply_approval import (
    ReplyApprovalService,
    ReplyPostOutcome,
)
from ai_youtube_agent.content.reply_draft import (
    REPLY_DRAFT_ENTITY,
    ReplyDraftError,
    ReplyDraftErrorCode,
    ReplyDraftOutcome,
    ReplyDraftService,
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
    StrategyProfileRepository,
)
from ai_youtube_agent.core.db.repositories.community import CommentRepository
from ai_youtube_agent.core.errors import DomainError
from ai_youtube_agent.core.flags import FeatureFlags

AUTO_REPLY_BLOCKED: Final[str] = "comment.auto_reply_blocked"
AUTO_REPLY_POSTED: Final[str] = "comment.auto_reply_posted"
DEFAULT_AUTO_REPLY_ACTOR: Final[Actor] = Actor(
    ActorKind.AI, "auto_reply_service"
)


class AutoReplyErrorCode(StrEnum):
    DISABLED = "domain.auto_reply_disabled"
    MODERATION_BLOCKED = "domain.auto_reply_moderation_blocked"


USER_MESSAGES: Final[dict[AutoReplyErrorCode, str]] = {
    AutoReplyErrorCode.DISABLED: (
        "Automatic comment replies are disabled (AUTO_REPLY_ENABLED=false); "
        "a human user must approve and post replies."
    ),
    AutoReplyErrorCode.MODERATION_BLOCKED: (
        "Automatic replies are never posted to comments flagged for moderation."
    ),
}

_STATUS: Final[dict[AutoReplyErrorCode, HTTPStatus]] = {
    AutoReplyErrorCode.DISABLED: HTTPStatus.FORBIDDEN,
    AutoReplyErrorCode.MODERATION_BLOCKED: HTTPStatus.CONFLICT,
}


class AutoReplyDisabledError(DomainError):
    default_code = AutoReplyErrorCode.DISABLED.value
    default_user_message = USER_MESSAGES[AutoReplyErrorCode.DISABLED]
    default_http_status = HTTPStatus.FORBIDDEN

    def __init__(
        self,
        detail: str = "AUTO_REPLY_ENABLED is false",
        *,
        error_code: AutoReplyErrorCode = AutoReplyErrorCode.DISABLED,
    ) -> None:
        if not isinstance(error_code, AutoReplyErrorCode):
            raise TypeError("error_code must be an AutoReplyErrorCode")
        self.auto_reply_code = error_code
        super().__init__(
            detail,
            code=error_code.value,
            user_message=USER_MESSAGES[error_code],
        )
        self.default_http_status = _STATUS[error_code]


@dataclass(frozen=True)
class AutoReplyOutcome:
    draft: ReplyDraft
    posted: bool
    post_outcome: ReplyPostOutcome | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "draft": self.draft.as_dict(),
            "posted": self.posted,
            "post_outcome": (
                self.post_outcome.as_dict() if self.post_outcome else None
            ),
        }


class AutoReplyGuard:
    """Enforces ``FeatureFlags.auto_reply_enabled`` (default ``False``)."""

    def __init__(
        self,
        flags: FeatureFlags | None = None,
        audit_log: AuditLog | None = None,
    ) -> None:
        self._flags = flags if flags is not None else FeatureFlags()
        self._audit = audit_log

    @property
    def is_enabled(self) -> bool:
        return bool(self._flags.auto_reply_enabled)

    def require_enabled(
        self,
        *,
        entity_id: str = "global",
        channel_id: str | None = None,
        comment_id: str | None = None,
        actor: Actor = DEFAULT_AUTO_REPLY_ACTOR,
    ) -> None:
        """Raise ``AutoReplyDisabledError`` and audit when ``auto_reply_enabled`` is ``False``."""
        if self.is_enabled:
            return
        if self._audit is not None:
            self._audit.record(
                AUTO_REPLY_BLOCKED,
                actor,
                EntityRef(REPLY_DRAFT_ENTITY, entity_id),
                AuditResult.DENIED,
                metadata={
                    "channel_id": channel_id,
                    "comment_id": comment_id,
                    "reply_draft_id": (
                        entity_id if entity_id != "global" else None
                    ),
                    "auto_reply_enabled": False,
                    "reason_code": AutoReplyErrorCode.DISABLED.value,
                },
            )
        raise AutoReplyDisabledError(
            f"auto-reply blocked for {entity_id}: AUTO_REPLY_ENABLED is false"
        )


class AutoReplyService:
    """Drafts and (only when ``AUTO_REPLY_ENABLED=true``) auto-posts comment replies."""

    def __init__(
        self,
        database: Database,
        draft_service: ReplyDraftService,
        approval_service: ReplyApprovalService,
        audit_log: AuditLog,
        *,
        flags: FeatureFlags | None = None,
    ) -> None:
        self._database = database
        self._drafts = draft_service
        self._approvals = approval_service
        self._audit = audit_log
        self._guard = AutoReplyGuard(flags, audit_log)

    @property
    def guard(self) -> AutoReplyGuard:
        return self._guard

    def draft_or_auto_reply(
        self,
        comment_id: str,
        *,
        actor: Actor = DEFAULT_AUTO_REPLY_ACTOR,
        instructions: str | None = None,
    ) -> AutoReplyOutcome:
        """Always generate a ``ReplyDraft``; only post when ``auto_reply_enabled`` is ``True``."""
        draft_outcome: ReplyDraftOutcome = self._drafts.generate_draft(
            comment_id, actor=actor, instructions=instructions
        )
        if not self._guard.is_enabled:
            return AutoReplyOutcome(
                draft=draft_outcome.draft,
                posted=False,
                post_outcome=None,
            )
        post_outcome = self.auto_post_draft(
            draft_outcome.draft.id, actor=actor
        )
        return AutoReplyOutcome(
            draft=post_outcome.draft,
            posted=True,
            post_outcome=post_outcome,
        )

    def auto_post_draft(
        self,
        draft_id: str,
        *,
        actor: Actor = DEFAULT_AUTO_REPLY_ACTOR,
    ) -> ReplyPostOutcome:
        """Auto-post ``draft_id`` only when ``auto_reply_enabled`` is ``True``."""
        connection = self._database.connect()
        try:
            repo = CommentRepository(connection)
            draft = repo.get_reply_draft(draft_id)
            comment = repo.get(draft.comment_id) if draft else None
            classifications = (
                repo.list_classifications(comment.id) if comment else []
            )
            strategy = (
                StrategyProfileRepository(connection).get_by_channel(
                    comment.channel_id
                )
                if comment
                else None
            )
        finally:
            connection.close()

        self._guard.require_enabled(
            entity_id=draft_id,
            channel_id=comment.channel_id if comment else None,
            comment_id=comment.id if comment else None,
            actor=actor,
        )
        if draft is None or comment is None:
            raise ReplyDraftError(
                ReplyDraftErrorCode.DRAFT_NOT_FOUND,
                f"reply draft {draft_id} does not exist",
            )

        latest_label = (
            classifications[-1].label if classifications else None
        )
        if latest_label is None:
            banned = (
                strategy.brand.banned_phrases
                if strategy and strategy.brand
                else ()
            )
            latest_label = evaluate_comment_rules(
                comment.text, banned_phrases=banned
            ).label
        if latest_label is CommentLabel.MODERATION:
            raise AutoReplyDisabledError(
                f"comment {comment.id} is flagged for moderation",
                error_code=AutoReplyErrorCode.MODERATION_BLOCKED,
            )

        outcome = self._approvals._execute_post(
            draft.id, actor=actor, require_human_approved=False
        )
        if not outcome.replayed:
            self._audit.record(
                AUTO_REPLY_POSTED,
                actor,
                EntityRef(REPLY_DRAFT_ENTITY, outcome.draft.id),
                AuditResult.SUCCESS,
                metadata={
                    "channel_id": comment.channel_id,
                    "comment_id": comment.id,
                    "youtube_video_id": comment.youtube_video_id,
                    "reply_draft_id": outcome.draft.id,
                    "youtube_reply_id": outcome.draft.youtube_reply_id,
                    "auto_reply_enabled": True,
                },
            )
        return outcome
