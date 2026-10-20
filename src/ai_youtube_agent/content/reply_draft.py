"""Reply draft generation service (Prompt Pack v8, prompt #185), context C15 Community.

``ReplyDraftService`` generates and stores ``ReplyDraft`` records (#026) with
``status = ReplyStatus.DRAFT`` without ever posting them to YouTube:

- Drafting never calls ``CommentProvider.post_reply``: every generated or manual
  reply starts in ``ReplyStatus.DRAFT`` with ``youtube_reply_id = None`` (#026,
  #185). Approval and posting belong to #186–#188.
- Refuses to draft replies for comments classified as ``CommentLabel.MODERATION``
  (``MODERATION_BLOCKED``), unless explicitly overridden by a human user in
  ``create_manual_draft``.
- Refuses to draft a reply when the comment already has a ``ReplyStatus.POSTED``
  reply (``ALREADY_REPLIED``).
- Enforces channel brand safety: if the generated or manual reply text contains
  any phrase from ``StrategyProfile.brand.banned_phrases``, ``BANNED_PHRASE`` is
  raised and nothing is stored.
- Idempotent when an active ``DRAFT`` or ``APPROVED`` reply with the same text
  already exists for the comment (reused with ``created=False``).
- Audits ``comment.reply_drafted`` after commit with ids and status flags only
  (never comment or reply text).
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from http import HTTPStatus
from typing import Any, Final

from ai_youtube_agent.content.comment import (
    Comment,
    CommentLabel,
    ReplyDraft,
    ReplyStatus,
)
from ai_youtube_agent.content.comment_classifier import (
    CommentClassifier,
    evaluate_comment_rules,
)
from ai_youtube_agent.content.strategy import StrategyProfile
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
from ai_youtube_agent.providers.comments import MAX_REPLY_TEXT_LENGTH
from ai_youtube_agent.providers.text_generation import (
    TextGenerator,
    TextRequest,
)

Clock = Callable[[], datetime]

REPLY_DRAFTED: Final[str] = "comment.reply_drafted"
REPLY_DRAFT_ENTITY: Final[str] = "reply_draft"
DEFAULT_REPLY_DRAFTER_ACTOR: Final[Actor] = Actor(
    ActorKind.AI, "reply_drafter"
)


class ReplyDraftErrorCode(StrEnum):
    CHANNEL_NOT_FOUND = "domain.reply_draft_channel_not_found"
    COMMENT_NOT_FOUND = "domain.reply_draft_comment_not_found"
    DRAFT_NOT_FOUND = "domain.reply_draft_not_found"
    MODERATION_BLOCKED = "domain.reply_draft_moderation_blocked"
    ALREADY_REPLIED = "domain.reply_draft_already_replied"
    BANNED_PHRASE = "domain.reply_draft_banned_phrase"
    INVALID_TEXT = "domain.reply_draft_invalid_text"


USER_MESSAGES: Final[dict[ReplyDraftErrorCode, str]] = {
    ReplyDraftErrorCode.CHANNEL_NOT_FOUND: "The channel does not exist.",
    ReplyDraftErrorCode.COMMENT_NOT_FOUND: "The comment does not exist.",
    ReplyDraftErrorCode.DRAFT_NOT_FOUND: "The reply draft does not exist.",
    ReplyDraftErrorCode.MODERATION_BLOCKED: (
        "Replies cannot be drafted for comments flagged for moderation."
    ),
    ReplyDraftErrorCode.ALREADY_REPLIED: (
        "A reply has already been posted for this comment."
    ),
    ReplyDraftErrorCode.BANNED_PHRASE: (
        "The drafted reply text violates the channel's banned phrase policy."
    ),
    ReplyDraftErrorCode.INVALID_TEXT: (
        "Reply draft text must be non-empty and within the maximum reply length."
    ),
}

_STATUS: Final[dict[ReplyDraftErrorCode, HTTPStatus]] = {
    ReplyDraftErrorCode.CHANNEL_NOT_FOUND: HTTPStatus.NOT_FOUND,
    ReplyDraftErrorCode.COMMENT_NOT_FOUND: HTTPStatus.NOT_FOUND,
    ReplyDraftErrorCode.DRAFT_NOT_FOUND: HTTPStatus.NOT_FOUND,
    ReplyDraftErrorCode.MODERATION_BLOCKED: HTTPStatus.CONFLICT,
    ReplyDraftErrorCode.ALREADY_REPLIED: HTTPStatus.CONFLICT,
    ReplyDraftErrorCode.BANNED_PHRASE: HTTPStatus.UNPROCESSABLE_ENTITY,
    ReplyDraftErrorCode.INVALID_TEXT: HTTPStatus.UNPROCESSABLE_ENTITY,
}


class ReplyDraftError(DomainError):
    default_code = ReplyDraftErrorCode.COMMENT_NOT_FOUND.value
    default_user_message = USER_MESSAGES[ReplyDraftErrorCode.COMMENT_NOT_FOUND]
    default_http_status = HTTPStatus.NOT_FOUND

    def __init__(self, code: ReplyDraftErrorCode, detail: str) -> None:
        if not isinstance(code, ReplyDraftErrorCode):
            raise TypeError("code must be a ReplyDraftErrorCode")
        self.draft_code = code
        super().__init__(detail, code=code.value, user_message=USER_MESSAGES[code])
        self.default_http_status = _STATUS[code]


@dataclass(frozen=True)
class ReplyDraftOutcome:
    comment: Comment
    draft: ReplyDraft
    created: bool

    @property
    def is_posted(self) -> bool:
        return self.draft.status is ReplyStatus.POSTED

    def as_dict(self) -> dict[str, Any]:
        return {
            "comment_id": self.comment.id,
            "draft": self.draft.as_dict(),
            "created": self.created,
            "is_posted": self.is_posted,
        }


class ReplyDraftService:
    """Generates and stores draft replies without posting by default."""

    def __init__(
        self,
        database: Database,
        text_generator: TextGenerator,
        audit_log: AuditLog,
        *,
        classifier: CommentClassifier | None = None,
        clock: Clock | None = None,
    ) -> None:
        self._database = database
        self._text = text_generator
        self._audit = audit_log
        self._classifier = classifier
        self._clock = clock

    def generate_draft(
        self,
        comment_id: str,
        *,
        actor: Actor = DEFAULT_REPLY_DRAFTER_ACTOR,
        instructions: str | None = None,
    ) -> ReplyDraftOutcome:
        """Generate an AI reply draft for ``comment_id`` without posting it."""
        comment, strategy, existing_drafts = self._load_comment_context(
            comment_id, allow_moderation=False
        )
        _ensure_not_already_replied(comment.id, existing_drafts)

        language = (
            strategy.languages.primary
            if strategy and strategy.languages
            else None
        )
        request = _build_reply_text_request(
            comment, strategy, instructions=instructions, language=language
        )
        generated = self._text.generate(request)
        reply_text = generated.texts[0].strip()
        banned_phrases = (
            strategy.brand.banned_phrases
            if strategy and strategy.brand
            else ()
        )
        _validate_reply_text(reply_text, banned_phrases=banned_phrases)

        return self._persist_draft(
            comment,
            reply_text,
            actor=actor,
            provider=generated.provider,
            model=generated.model,
        )

    def create_manual_draft(
        self,
        comment_id: str,
        *,
        text: str,
        actor: Actor,
        allow_moderation: bool = False,
    ) -> ReplyDraftOutcome:
        """Create a reply draft with explicit ``text`` without posting it."""
        if not isinstance(actor, Actor):
            raise TypeError("actor must be an Actor")
        comment, strategy, existing_drafts = self._load_comment_context(
            comment_id, allow_moderation=allow_moderation
        )
        _ensure_not_already_replied(comment.id, existing_drafts)
        banned_phrases = (
            strategy.brand.banned_phrases
            if strategy and strategy.brand
            else ()
        )
        cleaned = text.strip() if isinstance(text, str) else ""
        _validate_reply_text(cleaned, banned_phrases=banned_phrases)
        return self._persist_draft(
            comment,
            cleaned,
            actor=actor,
            provider="manual",
            model=None,
        )

    def draft_for_candidates(
        self,
        channel_id: str,
        *,
        actor: Actor = DEFAULT_REPLY_DRAFTER_ACTOR,
        youtube_video_id: str | None = None,
    ) -> tuple[ReplyDraftOutcome, ...]:
        """Generate draft replies for all ``REPLY_CANDIDATE`` comments on ``channel_id``."""
        connection = self._database.connect()
        try:
            if ChannelRepository(connection).get(channel_id) is None:
                raise ReplyDraftError(
                    ReplyDraftErrorCode.CHANNEL_NOT_FOUND,
                    f"channel {channel_id} does not exist",
                )
            repo = CommentRepository(connection)
            comments = repo.list_by_channel(channel_id)
            candidate_ids: list[str] = []
            for comment in comments:
                if (
                    youtube_video_id is not None
                    and comment.youtube_video_id != youtube_video_id
                ):
                    continue
                classifications = repo.list_classifications(comment.id)
                if (
                    not classifications
                    or classifications[-1].label
                    is not CommentLabel.REPLY_CANDIDATE
                ):
                    continue
                drafts = repo.list_reply_drafts(comment.id)
                if any(
                    d.status
                    in (
                        ReplyStatus.DRAFT,
                        ReplyStatus.APPROVED,
                        ReplyStatus.POSTED,
                    )
                    for d in drafts
                ):
                    continue
                candidate_ids.append(comment.id)
        finally:
            connection.close()

        outcomes: list[ReplyDraftOutcome] = []
        for cid in candidate_ids:
            outcomes.append(self.generate_draft(cid, actor=actor))
        return tuple(outcomes)

    def get(self, draft_id: str) -> ReplyDraft:
        connection = self._database.connect()
        try:
            draft = CommentRepository(connection).get_reply_draft(draft_id)
        finally:
            connection.close()
        if draft is None:
            raise ReplyDraftError(
                ReplyDraftErrorCode.DRAFT_NOT_FOUND,
                f"reply draft {draft_id} does not exist",
            )
        return draft

    def list_for_comment(self, comment_id: str) -> list[ReplyDraft]:
        connection = self._database.connect()
        try:
            repo = CommentRepository(connection)
            if repo.get(comment_id) is None:
                raise ReplyDraftError(
                    ReplyDraftErrorCode.COMMENT_NOT_FOUND,
                    f"comment {comment_id} does not exist",
                )
            return repo.list_reply_drafts(comment_id)
        finally:
            connection.close()

    def list_for_channel(
        self,
        channel_id: str,
        *,
        status: ReplyStatus | None = None,
    ) -> list[ReplyDraft]:
        if status is not None and not isinstance(status, ReplyStatus):
            raise TypeError("status must be a ReplyStatus or None")
        connection = self._database.connect()
        try:
            if ChannelRepository(connection).get(channel_id) is None:
                raise ReplyDraftError(
                    ReplyDraftErrorCode.CHANNEL_NOT_FOUND,
                    f"channel {channel_id} does not exist",
                )
            repo = CommentRepository(connection)
            comments = repo.list_by_channel(channel_id)
            drafts: list[ReplyDraft] = []
            for comment in comments:
                for draft in repo.list_reply_drafts(comment.id):
                    if status is None or draft.status is status:
                        drafts.append(draft)
            return drafts
        finally:
            connection.close()

    def _load_comment_context(
        self, comment_id: str, *, allow_moderation: bool
    ) -> tuple[Comment, StrategyProfile | None, list[ReplyDraft]]:
        connection = self._database.connect()
        try:
            repo = CommentRepository(connection)
            comment = repo.get(comment_id)
            if comment is None:
                raise ReplyDraftError(
                    ReplyDraftErrorCode.COMMENT_NOT_FOUND,
                    f"comment {comment_id} does not exist",
                )
            strategy = StrategyProfileRepository(connection).get_by_channel(
                comment.channel_id
            )
            classifications = repo.list_classifications(comment.id)
            existing_drafts = repo.list_reply_drafts(comment.id)
        finally:
            connection.close()

        latest_label: CommentLabel | None = (
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
        if not allow_moderation and latest_label is CommentLabel.MODERATION:
            raise ReplyDraftError(
                ReplyDraftErrorCode.MODERATION_BLOCKED,
                f"comment {comment.id} is classified as moderation",
            )
        return comment, strategy, existing_drafts

    def _persist_draft(
        self,
        comment: Comment,
        reply_text: str,
        *,
        actor: Actor,
        provider: str,
        model: str | None,
    ) -> ReplyDraftOutcome:
        now = self._now()
        with self._database.transaction() as connection:
            repo = CommentRepository(connection)
            current = repo.list_reply_drafts(comment.id)
            _ensure_not_already_replied(comment.id, current)
            for existing in reversed(current):
                if (
                    existing.status in (ReplyStatus.DRAFT, ReplyStatus.APPROVED)
                    and existing.text == reply_text
                ):
                    return ReplyDraftOutcome(comment, existing, False)

            draft = ReplyDraft.create(
                comment.id,
                reply_text,
                created_by=actor,
                clock=lambda: now,
            )
            repo.add_reply_draft(draft)

        self._audit.record(
            REPLY_DRAFTED,
            actor,
            EntityRef(REPLY_DRAFT_ENTITY, draft.id),
            AuditResult.SUCCESS,
            metadata={
                "channel_id": comment.channel_id,
                "comment_id": comment.id,
                "youtube_video_id": comment.youtube_video_id,
                "reply_draft_id": draft.id,
                "status": draft.status.value,
                "provider": provider,
                "model": model,
                "posted": False,
            },
        )
        return ReplyDraftOutcome(comment, draft, True)

    def _now(self) -> datetime:
        return self._clock() if self._clock else datetime.now(UTC)


def _ensure_not_already_replied(
    comment_id: str, drafts: Sequence[ReplyDraft]
) -> None:
    if any(d.status is ReplyStatus.POSTED for d in drafts):
        raise ReplyDraftError(
            ReplyDraftErrorCode.ALREADY_REPLIED,
            f"comment {comment_id} already has a posted reply",
        )


def _validate_reply_text(
    text: str, *, banned_phrases: Sequence[str] = ()
) -> None:
    if not text or len(text) > MAX_REPLY_TEXT_LENGTH:
        raise ReplyDraftError(
            ReplyDraftErrorCode.INVALID_TEXT,
            f"reply text must be 1..{MAX_REPLY_TEXT_LENGTH} characters",
        )
    lowered = text.lower()
    for phrase in banned_phrases:
        clean = phrase.strip().lower()
        if clean and clean in lowered:
            raise ReplyDraftError(
                ReplyDraftErrorCode.BANNED_PHRASE,
                "drafted reply matches a channel banned phrase",
            )


def _build_reply_text_request(
    comment: Comment,
    strategy: StrategyProfile | None,
    *,
    instructions: str | None,
    language: str | None,
) -> TextRequest:
    tone = (
        strategy.brand.tone
        if strategy and strategy.brand
        else "helpful and concise"
    )
    system = (
        f"You are drafting a helpful YouTube creator reply in a {tone} tone. "
        "Keep the reply concise, polite, and directly responsive to the viewer."
    )
    prompt = (
        f"Viewer ({comment.author_display_name}) wrote on video {comment.youtube_video_id}:\n"
        f"{comment.text}\n"
    )
    if instructions and instructions.strip():
        prompt += f"\nCreator instructions: {instructions.strip()}\n"
    prompt += "\nDraft a reply:"
    return TextRequest(
        prompt=prompt,
        system=system,
        language=language,
        candidates=1,
        max_tokens=256,
    )
