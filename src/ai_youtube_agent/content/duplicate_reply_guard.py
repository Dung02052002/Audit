"""Duplicate reply guard (Prompt Pack v8, prompt #187), context C15 Community.

``DuplicateReplyGuard`` prevents duplicate replies from being posted to YouTube:

1. ``ReplyDuplicateDecision.REPLAY``: the requested ``ReplyDraft`` is already in
   ``ReplyStatus.POSTED`` (nothing is posted again).
2. ``DuplicateReplyErrorCode.COMMENT_ALREADY_REPLIED``: another ``ReplyDraft``
   for the same comment is already in ``ReplyStatus.POSTED``.
3. ``DuplicateReplyErrorCode.THREAD_ALREADY_REPLIED``: another comment in the
   same YouTube thread (sharing the top-level ``youtube_comment_id`` /
   ``parent_comment_id``) already has a ``ReplyStatus.POSTED`` reply.
4. ``DuplicateReplyErrorCode.IDENTICAL_TEXT_ON_VIDEO``: when
   ``block_identical_text_on_video=True`` (default), another ``ReplyStatus.POSTED``
   reply on the same video already has identical normalized reply text.
5. Audits ``comment.reply_duplicate_blocked`` (``AuditResult.DENIED``) whenever a
   duplicate post attempt is blocked by ``ensure_can_post``.
"""

import re
import sqlite3
from dataclasses import dataclass
from enum import StrEnum
from http import HTTPStatus
from typing import Any, Final

from ai_youtube_agent.content.comment import Comment, ReplyDraft, ReplyStatus
from ai_youtube_agent.content.reply_approval import reply_post_idempotency_key
from ai_youtube_agent.content.reply_draft import REPLY_DRAFT_ENTITY
from ai_youtube_agent.core.audit import (
    Actor,
    ActorKind,
    AuditLog,
    AuditResult,
    EntityRef,
)
from ai_youtube_agent.core.db.database import ConcurrencyError, Database
from ai_youtube_agent.core.db.repositories.community import CommentRepository
from ai_youtube_agent.core.errors import DomainError

REPLY_DUPLICATE_BLOCKED: Final[str] = "comment.reply_duplicate_blocked"
DEFAULT_GUARD_ACTOR: Final[Actor] = Actor(
    ActorKind.SYSTEM, "duplicate_reply_guard"
)


def normalize_reply_text(text: str) -> str:
    """Normalize reply text for duplicate detection (case-insensitive, collapsed whitespace, punctuation stripped)."""
    cleaned = re.sub(r"[^\w\s]", " ", text)
    return " ".join(cleaned.strip().lower().split())


class ReplyDuplicateDecision(StrEnum):
    PROCEED = "proceed"
    REPLAY = "replay"


class DuplicateReplyErrorCode(StrEnum):
    COMMENT_ALREADY_REPLIED = "domain.reply_duplicate_comment_already_replied"
    THREAD_ALREADY_REPLIED = "domain.reply_duplicate_thread_already_replied"
    IDENTICAL_TEXT_ON_VIDEO = "domain.reply_duplicate_identical_text_on_video"
    DUPLICATE_TEXT_ON_VIDEO = "domain.reply_duplicate_identical_text_on_video"
    CONCURRENT = "domain.reply_duplicate_concurrent"


USER_MESSAGES: Final[dict[DuplicateReplyErrorCode, str]] = {
    DuplicateReplyErrorCode.COMMENT_ALREADY_REPLIED: (
        "A reply has already been posted for this comment."
    ),
    DuplicateReplyErrorCode.THREAD_ALREADY_REPLIED: (
        "A reply has already been posted in this comment thread."
    ),
    DuplicateReplyErrorCode.IDENTICAL_TEXT_ON_VIDEO: (
        "An identical reply has already been posted on this video."
    ),
    DuplicateReplyErrorCode.CONCURRENT: (
        "Another reply post for this comment completed at the same time."
    ),
}


class DuplicateReplyError(DomainError):
    default_code = DuplicateReplyErrorCode.COMMENT_ALREADY_REPLIED.value
    default_user_message = USER_MESSAGES[
        DuplicateReplyErrorCode.COMMENT_ALREADY_REPLIED
    ]
    default_http_status = HTTPStatus.CONFLICT

    def __init__(self, code: DuplicateReplyErrorCode, detail: str) -> None:
        if not isinstance(code, DuplicateReplyErrorCode):
            raise TypeError("code must be a DuplicateReplyErrorCode")
        self.duplicate_code = code
        super().__init__(detail, code=code.value, user_message=USER_MESSAGES[code])

    @property
    def reason(self) -> DuplicateReplyErrorCode:
        return self.duplicate_code


ReplyDuplicateReason: Final = DuplicateReplyErrorCode


@dataclass(frozen=True)
class ReplyDuplicateCheck:
    decision: ReplyDuplicateDecision
    idempotency_key: str
    existing_draft: ReplyDraft | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "decision": self.decision.value,
            "idempotency_key": self.idempotency_key,
            "existing_draft": (
                self.existing_draft.as_dict() if self.existing_draft else None
            ),
        }


class DuplicateReplyGuard:
    """Checks for and blocks duplicate reply posts across comments, threads, and videos."""

    def __init__(
        self,
        database: Database,
        audit_log: AuditLog | None = None,
        *,
        block_identical_text_on_video: bool = True,
    ) -> None:
        if not isinstance(block_identical_text_on_video, bool):
            raise TypeError("block_identical_text_on_video must be a bool")
        self._database = database
        self._audit = audit_log
        self._block_identical_video = block_identical_text_on_video

    def check(
        self,
        draft: ReplyDraft,
        *,
        comment: Comment | None = None,
    ) -> ReplyDuplicateCheck:
        """Read a consistent snapshot and decide whether ``draft`` may be posted."""
        if not isinstance(draft, ReplyDraft):
            raise TypeError("draft must be a ReplyDraft")

        connection = self._database.connect()
        try:
            connection.execute("BEGIN")
            try:
                repo = CommentRepository(connection)
                stored_draft = repo.get_reply_draft(draft.id) or draft
                target_comment = comment or repo.get(stored_draft.comment_id)
                if target_comment is None:
                    raise ValueError(
                        f"comment {stored_draft.comment_id} does not exist"
                    )
                idem_key = reply_post_idempotency_key(
                    target_comment.id, stored_draft.text
                )
                if stored_draft.status is ReplyStatus.POSTED:
                    return ReplyDuplicateCheck(
                        decision=ReplyDuplicateDecision.REPLAY,
                        idempotency_key=idem_key,
                        existing_draft=stored_draft,
                    )

                # 1. Check other drafts on the exact same comment
                for sibling in repo.list_reply_drafts(target_comment.id):
                    if (
                        sibling.id != stored_draft.id
                        and sibling.status is ReplyStatus.POSTED
                    ):
                        raise DuplicateReplyError(
                            DuplicateReplyErrorCode.COMMENT_ALREADY_REPLIED,
                            f"comment {target_comment.id} already has posted reply {sibling.id}",
                        )

                # 2. Check other comments in the same thread or on the same video
                thread_root_yt_id = (
                    target_comment.parent_comment_id
                    if target_comment.parent_comment_id is not None
                    else target_comment.youtube_comment_id
                )
                norm_text = normalize_reply_text(stored_draft.text)
                channel_comments = repo.list_by_channel(
                    target_comment.channel_id
                )
                for other_comment in channel_comments:
                    if (
                        other_comment.id == target_comment.id
                        or other_comment.youtube_video_id
                        != target_comment.youtube_video_id
                    ):
                        continue
                    other_root_yt_id = (
                        other_comment.parent_comment_id
                        if other_comment.parent_comment_id is not None
                        else other_comment.youtube_comment_id
                    )
                    other_drafts = repo.list_reply_drafts(other_comment.id)
                    for other_draft in other_drafts:
                        if other_draft.status is not ReplyStatus.POSTED:
                            continue
                        if other_root_yt_id == thread_root_yt_id:
                            raise DuplicateReplyError(
                                DuplicateReplyErrorCode.THREAD_ALREADY_REPLIED,
                                f"thread {thread_root_yt_id} already has posted reply {other_draft.id}",
                            )
                        if (
                            self._block_identical_video
                            and normalize_reply_text(other_draft.text)
                            == norm_text
                        ):
                            raise DuplicateReplyError(
                                DuplicateReplyErrorCode.IDENTICAL_TEXT_ON_VIDEO,
                                f"video {target_comment.youtube_video_id} already has an identical posted reply {other_draft.id}",
                            )
            finally:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
        finally:
            connection.close()

        return ReplyDuplicateCheck(
            decision=ReplyDuplicateDecision.PROCEED,
            idempotency_key=idem_key,
        )

    def ensure_can_post(
        self,
        draft: ReplyDraft,
        *,
        comment: Comment | None = None,
        actor: Actor = DEFAULT_GUARD_ACTOR,
    ) -> ReplyDuplicateCheck:
        """Check ``draft`` and audit ``comment.reply_duplicate_blocked`` if blocked."""
        try:
            return self.check(draft, comment=comment)
        except DuplicateReplyError as exc:
            if self._audit is not None:
                self._audit.record(
                    REPLY_DUPLICATE_BLOCKED,
                    actor,
                    EntityRef(REPLY_DRAFT_ENTITY, draft.id),
                    AuditResult.DENIED,
                    metadata={
                        "channel_id": comment.channel_id if comment else None,
                        "comment_id": draft.comment_id,
                        "youtube_video_id": (
                            comment.youtube_video_id if comment else None
                        ),
                        "reply_draft_id": draft.id,
                        "reason_code": exc.code,
                    },
                )
            raise


def concurrent_reply_post(
    error: BaseException, draft_id: str
) -> DuplicateReplyError:
    """Convert a lost optimistic status update race into ``DuplicateReplyError(CONCURRENT)``."""
    if not isinstance(error, sqlite3.IntegrityError | ConcurrencyError):
        raise TypeError(
            "only IntegrityError and ConcurrencyError mean a lost race"
        )
    return DuplicateReplyError(
        DuplicateReplyErrorCode.CONCURRENT,
        f"a concurrent reply post for draft {draft_id} won the race",
    )
