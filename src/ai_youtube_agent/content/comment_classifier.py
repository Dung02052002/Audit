"""Comment classification service (Prompt Pack v8, prompt #184), context C15 Community.

``CommentClassifier`` classifies synced ``Comment`` records (#026, #183) into
``CommentLabel`` (``moderation``, ``needs_attention``, ``reply_candidate``,
``no_action``) and stores append-only ``CommentClassification`` records:

- Deterministic rule evaluation (``CLASSIFICATION_RULES_VERSION = "comment-rules-v1"``):
  1. ``MODERATION``: external links / URLs, spam/scam patterns (crypto/telegram/whatsapp
     solicitation, giveaway scams), or matches against the channel's
     ``StrategyProfile.brand.banned_phrases``.
  2. ``NEEDS_ATTENTION``: urgent viewer reports (audio/video/caption issues, factual
     corrections, business/sponsorship inquiries).
  3. ``REPLY_CANDIDATE``: substantive viewer questions or topic/tutorial requests.
  4. ``NO_ACTION``: short praise, emojis, or general remarks requiring no action.
- Also supports explicit human/manual classification (``classify_comment(...,
  override_label=..., rationale=...)``).
- Idempotent when the latest stored ``CommentClassification`` for a comment already
  has the same ``label`` and ``rationale`` (unless ``force=True``); reclassifying
  with a new label appends a new ``CommentClassification`` row so history is kept
  (#026).
- Audits ``comment.classified`` after commit with ids, labels, and rule codes only
  (never comment text or banned phrases).
"""

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from http import HTTPStatus
from typing import Any, Final

from ai_youtube_agent.content.comment import (
    Comment,
    CommentClassification,
    CommentLabel,
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

Clock = Callable[[], datetime]

CLASSIFICATION_RULES_VERSION: Final[str] = "comment-rules-v1"
COMMENT_CLASSIFIED: Final[str] = "comment.classified"
COMMENT_ENTITY: Final[str] = "comment"
DEFAULT_CLASSIFIER_ACTOR: Final[Actor] = Actor(ActorKind.AI, "comment_classifier")

_URL_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"(?:https?://|www\.|[a-z0-9-]+\.(?:com|net|org|io|me|gg|xyz|ly|co)/)",
    re.IGNORECASE,
)
_SPAM_KEYWORDS: Final[tuple[str, ...]] = (
    "whatsapp",
    "telegram",
    "crypto giveaway",
    "free bitcoin",
    "guaranteed profit",
    "dm me on",
    "check my bio",
    "sub4sub",
    "sub for sub",
)
_ATTENTION_KEYWORDS: Final[tuple[str, ...]] = (
    "no sound",
    "audio is out of sync",
    "audio issue",
    "broken link",
    "wrong number",
    "typo in",
    "correction:",
    "inaccurate",
    "sponsorship",
    "business inquiry",
    "collab",
)
_QUESTION_STARTERS: Final[tuple[str, ...]] = (
    "how ",
    "what ",
    "why ",
    "when ",
    "where ",
    "which ",
    "can you",
    "could you",
    "would you",
    "do you",
    "will you",
    "please explain",
    "next video on",
)


class CommentClassificationErrorCode(StrEnum):
    CHANNEL_NOT_FOUND = "domain.comment_classification_channel_not_found"
    COMMENT_NOT_FOUND = "domain.comment_classification_comment_not_found"
    INVALID_RATIONALE = "domain.comment_classification_invalid_rationale"


USER_MESSAGES: Final[dict[CommentClassificationErrorCode, str]] = {
    CommentClassificationErrorCode.CHANNEL_NOT_FOUND: "The channel does not exist.",
    CommentClassificationErrorCode.COMMENT_NOT_FOUND: "The comment does not exist.",
    CommentClassificationErrorCode.INVALID_RATIONALE: (
        "Classification rationale must not be blank when provided."
    ),
}

_STATUS: Final[dict[CommentClassificationErrorCode, HTTPStatus]] = {
    CommentClassificationErrorCode.CHANNEL_NOT_FOUND: HTTPStatus.NOT_FOUND,
    CommentClassificationErrorCode.COMMENT_NOT_FOUND: HTTPStatus.NOT_FOUND,
    CommentClassificationErrorCode.INVALID_RATIONALE: HTTPStatus.UNPROCESSABLE_ENTITY,
}


class CommentClassificationError(DomainError):
    default_code = CommentClassificationErrorCode.COMMENT_NOT_FOUND.value
    default_user_message = USER_MESSAGES[
        CommentClassificationErrorCode.COMMENT_NOT_FOUND
    ]
    default_http_status = HTTPStatus.NOT_FOUND

    def __init__(self, code: CommentClassificationErrorCode, detail: str) -> None:
        if not isinstance(code, CommentClassificationErrorCode):
            raise TypeError("code must be a CommentClassificationErrorCode")
        self.classification_code = code
        super().__init__(detail, code=code.value, user_message=USER_MESSAGES[code])
        self.default_http_status = _STATUS[code]


@dataclass(frozen=True)
class ClassificationDecision:
    label: CommentLabel
    rule_code: str
    rationale: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "label": self.label.value,
            "rule_code": self.rule_code,
            "rationale": self.rationale,
        }


@dataclass(frozen=True)
class CommentClassificationOutcome:
    comment: Comment
    classification: CommentClassification
    rule_code: str
    created: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "comment_id": self.comment.id,
            "classification": self.classification.as_dict(),
            "rule_code": self.rule_code,
            "created": self.created,
        }


@dataclass(frozen=True)
class ChannelClassificationSummary:
    channel_id: str
    outcomes: tuple[CommentClassificationOutcome, ...]
    by_label: dict[str, int]

    @property
    def created_count(self) -> int:
        return sum(1 for o in self.outcomes if o.created)

    @property
    def unchanged_count(self) -> int:
        return sum(1 for o in self.outcomes if not o.created)

    def as_dict(self) -> dict[str, Any]:
        return {
            "channel_id": self.channel_id,
            "outcomes": [o.as_dict() for o in self.outcomes],
            "by_label": dict(self.by_label),
            "created_count": self.created_count,
            "unchanged_count": self.unchanged_count,
        }


def evaluate_comment_rules(
    text: str,
    *,
    banned_phrases: Sequence[str] = (),
) -> ClassificationDecision:
    """Evaluate deterministic classification rules on ``text`` without echoing
    content."""
    lowered = text.lower()
    stripped = lowered.strip()

    for phrase in banned_phrases:
        clean = phrase.strip().lower()
        if clean and clean in lowered:
            return ClassificationDecision(
                label=CommentLabel.MODERATION,
                rule_code="moderation.banned_phrase",
                rationale="matches channel banned phrase policy",
            )

    if _URL_PATTERN.search(text):
        return ClassificationDecision(
            label=CommentLabel.MODERATION,
            rule_code="moderation.external_link",
            rationale="contains external URL or link",
        )

    for kw in _SPAM_KEYWORDS:
        if kw in lowered:
            return ClassificationDecision(
                label=CommentLabel.MODERATION,
                rule_code="moderation.spam_solicitation",
                rationale="matches spam or solicitation pattern",
            )

    for kw in _ATTENTION_KEYWORDS:
        if kw in lowered:
            return ClassificationDecision(
                label=CommentLabel.NEEDS_ATTENTION,
                rule_code="attention.viewer_report",
                rationale=(
                    "reports a technical, factual, or business inquiry needing creator "
                    "attention"
                ),
            )

    if "?" in stripped or any(q in lowered for q in _QUESTION_STARTERS):
        return ClassificationDecision(
            label=CommentLabel.REPLY_CANDIDATE,
            rule_code="reply.viewer_question",
            rationale="asks a question or requests a topic suitable for a reply draft",
        )

    return ClassificationDecision(
        label=CommentLabel.NO_ACTION,
        rule_code="no_action.general",
        rationale="general viewer comment requiring no action",
    )


class CommentClassifier:
    """Classifies comments for attention, moderation, and reply drafting."""

    def __init__(
        self,
        database: Database,
        audit_log: AuditLog,
        *,
        clock: Clock | None = None,
    ) -> None:
        self._database = database
        self._audit = audit_log
        self._clock = clock

    def classify_comment(
        self,
        comment_id: str,
        *,
        actor: Actor = DEFAULT_CLASSIFIER_ACTOR,
        override_label: CommentLabel | None = None,
        rationale: str | None = None,
        force: bool = False,
    ) -> CommentClassificationOutcome:
        """Classify a single stored ``Comment`` and persist a
        ``CommentClassification``."""
        if override_label is not None and not isinstance(override_label, CommentLabel):
            raise TypeError("override_label must be a CommentLabel or None")
        if rationale is not None and (
            not isinstance(rationale, str) or not rationale.strip()
        ):
            raise CommentClassificationError(
                CommentClassificationErrorCode.INVALID_RATIONALE,
                "rationale must not be blank when provided",
            )

        now = self._now()
        with self._database.transaction() as connection:
            comments = CommentRepository(connection)
            comment = comments.get(comment_id)
            if comment is None:
                raise CommentClassificationError(
                    CommentClassificationErrorCode.COMMENT_NOT_FOUND,
                    f"comment {comment_id} does not exist",
                )
            strategy = StrategyProfileRepository(connection).get_by_channel(
                comment.channel_id
            )
            banned_phrases = (
                strategy.brand.banned_phrases if strategy and strategy.brand else ()
            )
            if override_label is not None:
                decision = ClassificationDecision(
                    label=override_label,
                    rule_code="manual.override",
                    rationale=(
                        rationale.strip()
                        if rationale is not None
                        else f"manual classification as {override_label.value}"
                    ),
                )
            else:
                decision = evaluate_comment_rules(
                    comment.text, banned_phrases=banned_phrases
                )
                if rationale is not None:
                    decision = ClassificationDecision(
                        label=decision.label,
                        rule_code=decision.rule_code,
                        rationale=rationale.strip(),
                    )

            history = comments.list_classifications(comment.id)
            if (
                not force
                and history
                and history[-1].label is decision.label
                and history[-1].rationale == decision.rationale
            ):
                return CommentClassificationOutcome(
                    comment=comment,
                    classification=history[-1],
                    rule_code=decision.rule_code,
                    created=False,
                )

            classification = CommentClassification.create(
                comment.id,
                decision.label,
                classified_by=actor,
                rationale=decision.rationale,
                clock=lambda: now,
            )
            comments.add_classification(classification)

        self._audit.record(
            COMMENT_CLASSIFIED,
            actor,
            EntityRef(COMMENT_ENTITY, comment.id),
            AuditResult.SUCCESS,
            metadata={
                "channel_id": comment.channel_id,
                "comment_id": comment.id,
                "classification_id": classification.id,
                "label": classification.label.value,
                "rule_code": decision.rule_code,
                "rules_version": CLASSIFICATION_RULES_VERSION,
            },
        )
        return CommentClassificationOutcome(
            comment=comment,
            classification=classification,
            rule_code=decision.rule_code,
            created=True,
        )

    def classify_channel(
        self,
        channel_id: str,
        *,
        actor: Actor = DEFAULT_CLASSIFIER_ACTOR,
        youtube_video_id: str | None = None,
        only_unclassified: bool = False,
    ) -> ChannelClassificationSummary:
        """Classify comments of a channel (or a specific video on the channel)."""
        connection = self._database.connect()
        try:
            if ChannelRepository(connection).get(channel_id) is None:
                raise CommentClassificationError(
                    CommentClassificationErrorCode.CHANNEL_NOT_FOUND,
                    f"channel {channel_id} does not exist",
                )
            repo = CommentRepository(connection)
            all_comments = repo.list_by_channel(channel_id)
            target_ids: list[str] = []
            for comment in all_comments:
                if (
                    youtube_video_id is not None
                    and comment.youtube_video_id != youtube_video_id
                ):
                    continue
                if only_unclassified and repo.list_classifications(comment.id):
                    continue
                target_ids.append(comment.id)
        finally:
            connection.close()

        outcomes: list[CommentClassificationOutcome] = []
        by_label: dict[str, int] = {}
        for cid in target_ids:
            outcome = self.classify_comment(cid, actor=actor)
            outcomes.append(outcome)
            lbl = outcome.classification.label.value
            by_label[lbl] = by_label.get(lbl, 0) + 1

        return ChannelClassificationSummary(
            channel_id=channel_id,
            outcomes=tuple(outcomes),
            by_label=by_label,
        )

    def latest_for_comment(self, comment_id: str) -> CommentClassification | None:
        """Return the newest ``CommentClassification`` for ``comment_id``, or
        ``None``."""
        history = self.history_for_comment(comment_id)
        return history[-1] if history else None

    def history_for_comment(self, comment_id: str) -> list[CommentClassification]:
        """Return all stored ``CommentClassification`` rows for ``comment_id`` in
        chronological order."""
        connection = self._database.connect()
        try:
            repo = CommentRepository(connection)
            if repo.get(comment_id) is None:
                raise CommentClassificationError(
                    CommentClassificationErrorCode.COMMENT_NOT_FOUND,
                    f"comment {comment_id} does not exist",
                )
            return repo.list_classifications(comment_id)
        finally:
            connection.close()

    def list_by_label(
        self,
        channel_id: str,
        label: CommentLabel,
        *,
        youtube_video_id: str | None = None,
    ) -> list[tuple[Comment, CommentClassification]]:
        """List comments on ``channel_id`` whose latest classification matches
        ``label``."""
        if not isinstance(label, CommentLabel):
            raise TypeError("label must be a CommentLabel")
        connection = self._database.connect()
        try:
            if ChannelRepository(connection).get(channel_id) is None:
                raise CommentClassificationError(
                    CommentClassificationErrorCode.CHANNEL_NOT_FOUND,
                    f"channel {channel_id} does not exist",
                )
            repo = CommentRepository(connection)
            comments = repo.list_by_channel(channel_id)
            matched: list[tuple[Comment, CommentClassification]] = []
            for comment in comments:
                if (
                    youtube_video_id is not None
                    and comment.youtube_video_id != youtube_video_id
                ):
                    continue
                history = repo.list_classifications(comment.id)
                if history and history[-1].label is label:
                    matched.append((comment, history[-1]))
            return matched
        finally:
            connection.close()

    def _now(self) -> datetime:
        return self._clock() if self._clock else datetime.now(UTC)
