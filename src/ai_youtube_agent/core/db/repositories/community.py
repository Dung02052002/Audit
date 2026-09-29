"""Repository for comments, their classifications and reply drafts (C15)."""

import sqlite3

from ai_youtube_agent.content.comment import (
    Comment,
    CommentClassification,
    CommentLabel,
    ReplyDraft,
    ReplyStatus,
)
from ai_youtube_agent.core.db.repositories.base import (
    Repository,
    actor_columns,
    actor_from,
    dt,
    parse_dt,
)


class CommentRepository(Repository):
    """Comments and classifications are add only; reply drafts change status."""

    table = "comments"

    def add(self, comment: Comment) -> None:
        self._insert(
            "comments",
            {
                "id": comment.id,
                "channel_id": comment.channel_id,
                "youtube_comment_id": comment.youtube_comment_id,
                "youtube_video_id": comment.youtube_video_id,
                "parent_comment_id": comment.parent_comment_id,
                "author_display_name": comment.author_display_name,
                "text": comment.text,
                "published_at": dt(comment.published_at),
            },
        )

    def get(self, comment_id: str) -> Comment | None:
        row = self._one("SELECT * FROM comments WHERE id = ?", (comment_id,))
        return _comment(row) if row else None

    def get_by_youtube_comment_id(self, youtube_comment_id: str) -> Comment | None:
        row = self._one(
            "SELECT * FROM comments WHERE youtube_comment_id = ?",
            (youtube_comment_id,),
        )
        return _comment(row) if row else None

    def list_by_channel(self, channel_id: str) -> list[Comment]:
        rows = self._all(
            "SELECT * FROM comments WHERE channel_id = ? ORDER BY published_at, id",
            (channel_id,),
        )
        return [_comment(row) for row in rows]

    def add_classification(self, classification: CommentClassification) -> None:
        self._insert(
            "comment_classifications",
            {
                "id": classification.id,
                "comment_id": classification.comment_id,
                "label": classification.label.value,
                **actor_columns("classified_by", classification.classified_by),
                "rationale": classification.rationale,
                "classified_at": dt(classification.classified_at),
            },
        )

    def list_classifications(self, comment_id: str) -> list[CommentClassification]:
        rows = self._all(
            "SELECT * FROM comment_classifications WHERE comment_id = ? "
            "ORDER BY classified_at, id",
            (comment_id,),
        )
        return [
            CommentClassification(
                id=row["id"],
                comment_id=row["comment_id"],
                label=CommentLabel(row["label"]),
                classified_by=actor_from(row, "classified_by"),
                rationale=row["rationale"],
                classified_at=parse_dt(row["classified_at"]),
            )
            for row in rows
        ]

    def add_reply_draft(self, draft: ReplyDraft) -> None:
        self._insert(
            "reply_drafts",
            {
                "id": draft.id,
                "comment_id": draft.comment_id,
                "text": draft.text,
                "status": draft.status.value,
                **actor_columns("created_by", draft.created_by),
                "created_at": dt(draft.created_at),
                "youtube_reply_id": draft.youtube_reply_id,
            },
        )

    def get_reply_draft(self, draft_id: str) -> ReplyDraft | None:
        row = self._one("SELECT * FROM reply_drafts WHERE id = ?", (draft_id,))
        return _draft(row) if row else None

    def list_reply_drafts(self, comment_id: str) -> list[ReplyDraft]:
        rows = self._all(
            "SELECT * FROM reply_drafts WHERE comment_id = ? ORDER BY created_at, id",
            (comment_id,),
        )
        return [_draft(row) for row in rows]

    def update_reply_draft(
        self, draft: ReplyDraft, *, expected_status: ReplyStatus
    ) -> None:
        self._update(
            draft.id,
            {
                "text": draft.text,
                "status": draft.status.value,
                "youtube_reply_id": draft.youtube_reply_id,
            },
            guard_column="status",
            guard_value=expected_status.value,
            table="reply_drafts",
        )


def _comment(row: sqlite3.Row) -> Comment:
    return Comment(
        id=row["id"],
        channel_id=row["channel_id"],
        youtube_comment_id=row["youtube_comment_id"],
        youtube_video_id=row["youtube_video_id"],
        parent_comment_id=row["parent_comment_id"],
        author_display_name=row["author_display_name"],
        text=row["text"],
        published_at=parse_dt(row["published_at"]),
    )


def _draft(row: sqlite3.Row) -> ReplyDraft:
    return ReplyDraft(
        id=row["id"],
        comment_id=row["comment_id"],
        text=row["text"],
        status=ReplyStatus(row["status"]),
        created_by=actor_from(row, "created_by"),
        created_at=parse_dt(row["created_at"]),
        youtube_reply_id=row["youtube_reply_id"],
    )
