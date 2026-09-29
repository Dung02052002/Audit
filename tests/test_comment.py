import dataclasses
from datetime import UTC, datetime, timedelta, timezone

import pytest

from ai_youtube_agent.content.comment import (
    Comment,
    CommentClassification,
    CommentLabel,
    ReplyDraft,
    ReplyStatus,
)
from ai_youtube_agent.core.audit import Actor, ActorKind

T0 = datetime(2026, 9, 29, 10, 0, tzinfo=UTC)
T1 = T0 + timedelta(minutes=5)
VIDEO_ID = "dQw4w9WgXcQ"
CLASSIFIER = Actor(ActorKind.AI, "comment-classifier")
USER = Actor(ActorKind.USER, "owner-1")


def at(moment: datetime):
    return lambda: moment


def new_comment(**overrides) -> Comment:
    values = {
        "author_display_name": "Viewer One",
        "text": "Great video, what about index funds?",
        "published_at": T0,
    }
    return Comment.create(
        "channel-1", "Ugx-comment-1", VIDEO_ID, **{**values, **overrides}
    )


def rebuild(entity, **changes):
    values = {f.name: getattr(entity, f.name) for f in dataclasses.fields(entity)}
    return type(entity)(**{**values, **changes})


# Comments


def test_create_keeps_the_synced_comment() -> None:
    comment = new_comment()

    assert len(comment.id) == 32
    assert comment.channel_id == "channel-1"
    assert comment.youtube_comment_id == "Ugx-comment-1"
    assert comment.youtube_video_id == VIDEO_ID
    assert comment.parent_comment_id is None
    assert not comment.is_reply
    assert comment.author_display_name == "Viewer One"
    assert comment.text == "Great video, what about index funds?"
    assert comment.published_at == T0


def test_comment_keeps_no_other_author_data() -> None:
    names = {f.name for f in dataclasses.fields(Comment)}
    assert {n for n in names if n.startswith("author")} == {"author_display_name"}
    assert not names & {"author_channel_id", "email", "avatar_url"}


def test_a_reply_in_a_thread_has_a_parent() -> None:
    reply = new_comment(parent_comment_id="Ugx-parent")
    assert reply.is_reply
    assert reply.parent_comment_id == "Ugx-parent"


def test_comment_text_is_kept_as_synced() -> None:
    assert new_comment(text="  spaced  ").text == "  spaced  "


@pytest.mark.parametrize(
    "overrides",
    [
        {"author_display_name": " "},
        {"text": "  "},
        {"published_at": datetime(2026, 9, 29, 10, 0)},
        {"published_at": T0.astimezone(timezone(timedelta(hours=7)))},
        {"parent_comment_id": " "},
        {"parent_comment_id": "Ugx-comment-1"},
    ],
)
def test_create_rejects_bad_values(overrides) -> None:
    with pytest.raises(ValueError):
        new_comment(**overrides)


@pytest.mark.parametrize(
    ("channel", "comment_id", "video_id"),
    [("", "c", VIDEO_ID), ("ch", " ", VIDEO_ID), ("ch", "c", "short"), ("ch", "c", "")],
)
def test_create_needs_valid_ids(channel, comment_id, video_id) -> None:
    with pytest.raises(ValueError):
        Comment.create(
            channel,
            comment_id,
            video_id,
            author_display_name="Viewer",
            text="Hi",
            published_at=T0,
        )


def test_each_comment_gets_its_own_id() -> None:
    assert new_comment().id != new_comment().id


def test_comment_is_frozen() -> None:
    with pytest.raises(dataclasses.FrozenInstanceError):
        new_comment().text = "edited"  # type: ignore[misc]


# Classification


def test_labels() -> None:
    assert [label.value for label in CommentLabel] == [
        "needs_attention",
        "moderation",
        "reply_candidate",
        "no_action",
    ]


def test_classification_records_label_actor_and_time() -> None:
    comment = new_comment()

    result = CommentClassification.create(
        comment.id,
        CommentLabel.REPLY_CANDIDATE,
        classified_by=CLASSIFIER,
        rationale="  asks a question  ",
        clock=at(T1),
    )

    assert len(result.id) == 32
    assert result.comment_id == comment.id
    assert result.label is CommentLabel.REPLY_CANDIDATE
    assert result.classified_by == CLASSIFIER
    assert result.rationale == "asks a question"
    assert result.classified_at == T1


def test_reclassifying_keeps_history() -> None:
    comment = new_comment()
    first = CommentClassification.create(
        comment.id, CommentLabel.NO_ACTION, classified_by=CLASSIFIER, clock=at(T0)
    )
    second = CommentClassification.create(
        comment.id, CommentLabel.MODERATION, classified_by=USER, clock=at(T1)
    )
    assert first.id != second.id
    assert (first.label, second.label) == (
        CommentLabel.NO_ACTION,
        CommentLabel.MODERATION,
    )


def test_rationale_is_optional() -> None:
    result = CommentClassification.create(
        "c", CommentLabel.NO_ACTION, classified_by=CLASSIFIER
    )
    assert result.rationale is None


@pytest.mark.parametrize(
    ("comment_id", "label", "actor", "rationale", "error"),
    [
        ("", CommentLabel.NO_ACTION, CLASSIFIER, None, ValueError),
        ("c", "no_action", CLASSIFIER, None, TypeError),
        ("c", CommentLabel.NO_ACTION, "classifier", None, TypeError),
        ("c", CommentLabel.NO_ACTION, CLASSIFIER, "  ", ValueError),
    ],
)
def test_classification_rejects_bad_values(
    comment_id, label, actor, rationale, error
) -> None:
    with pytest.raises(error):
        CommentClassification.create(
            comment_id, label, classified_by=actor, rationale=rationale
        )


def test_classification_needs_utc_time() -> None:
    with pytest.raises(ValueError):
        CommentClassification.create(
            "c",
            CommentLabel.NO_ACTION,
            classified_by=CLASSIFIER,
            clock=at(datetime(2026, 9, 29, 10, 0)),
        )


# Reply drafts


def test_statuses() -> None:
    assert [s.value for s in ReplyStatus] == [
        "draft",
        "approved",
        "posted",
        "failed",
        "discarded",
    ]


def test_a_new_reply_is_always_a_draft() -> None:
    draft = ReplyDraft.create(
        "comment-1",
        "  Thanks! Index funds are next week.  ",
        created_by=CLASSIFIER,
        clock=at(T1),
    )

    assert len(draft.id) == 32
    assert draft.comment_id == "comment-1"
    assert draft.text == "Thanks! Index funds are next week."
    assert draft.status is ReplyStatus.DRAFT
    assert draft.created_by == CLASSIFIER
    assert draft.created_at == T1
    assert draft.youtube_reply_id is None


def test_a_draft_cannot_be_approved_or_posted_yet() -> None:
    for name in ("approve", "post", "mark_posted", "send", "publish"):
        assert not hasattr(ReplyDraft, name)
    with pytest.raises(dataclasses.FrozenInstanceError):
        ReplyDraft.create("c", "Hi", created_by=USER).status = ReplyStatus.POSTED  # type: ignore[misc]


@pytest.mark.parametrize(("comment_id", "text"), [("", "Hi"), ("c", "  ")])
def test_draft_needs_a_comment_and_text(comment_id: str, text: str) -> None:
    with pytest.raises(ValueError):
        ReplyDraft.create(comment_id, text, created_by=USER)


def test_draft_needs_an_actor_and_utc_time() -> None:
    with pytest.raises(TypeError):
        ReplyDraft.create("c", "Hi", created_by="ai")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        ReplyDraft.create(
            "c", "Hi", created_by=USER, clock=at(datetime(2026, 9, 29, 10, 0))
        )


def test_a_stored_posted_reply_needs_its_youtube_id() -> None:
    draft = ReplyDraft.create("c", "Hi", created_by=USER, clock=at(T0))

    posted = rebuild(draft, status=ReplyStatus.POSTED, youtube_reply_id="Ugx-reply")

    assert posted.youtube_reply_id == "Ugx-reply"
    with pytest.raises(ValueError):
        rebuild(draft, status=ReplyStatus.POSTED)
    with pytest.raises(ValueError):
        rebuild(draft, status=ReplyStatus.POSTED, youtube_reply_id=" ")


@pytest.mark.parametrize(
    "status", [s for s in ReplyStatus if s is not ReplyStatus.POSTED]
)
def test_only_a_posted_reply_has_a_youtube_id(status: ReplyStatus) -> None:
    draft = ReplyDraft.create("c", "Hi", created_by=USER, clock=at(T0))
    with pytest.raises(ValueError):
        rebuild(draft, status=status, youtube_reply_id="Ugx-reply")


def test_draft_status_must_be_a_reply_status() -> None:
    draft = ReplyDraft.create("c", "Hi", created_by=USER, clock=at(T0))
    with pytest.raises(TypeError):
        rebuild(draft, status="draft")


# Serialisation


def test_as_dict_is_json_friendly() -> None:
    comment = new_comment(parent_comment_id="Ugx-parent")
    result = CommentClassification.create(
        comment.id,
        CommentLabel.NEEDS_ATTENTION,
        classified_by=CLASSIFIER,
        clock=at(T1),
    )
    draft = ReplyDraft.create(
        comment.id, "Thanks!", created_by=CLASSIFIER, clock=at(T1)
    )
    stamp = "2026-09-29T10:05:00+00:00"

    assert comment.as_dict() == {
        "id": comment.id,
        "channel_id": "channel-1",
        "youtube_comment_id": "Ugx-comment-1",
        "youtube_video_id": VIDEO_ID,
        "parent_comment_id": "Ugx-parent",
        "author_display_name": "Viewer One",
        "text": "Great video, what about index funds?",
        "published_at": "2026-09-29T10:00:00+00:00",
    }
    assert result.as_dict() == {
        "id": result.id,
        "comment_id": comment.id,
        "label": "needs_attention",
        "classified_by": {"kind": "ai", "id": "comment-classifier"},
        "rationale": None,
        "classified_at": stamp,
    }
    assert draft.as_dict() == {
        "id": draft.id,
        "comment_id": comment.id,
        "text": "Thanks!",
        "status": "draft",
        "created_by": {"kind": "ai", "id": "comment-classifier"},
        "created_at": stamp,
        "youtube_reply_id": None,
    }
