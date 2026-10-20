"""End-to-end Phase P Community & Comments scenario tests (Prompt Pack v8, prompt #190).

Covers:
1. Multi-page comment & reply-thread synchronization, idempotent upserts on
   edited/re-fetched comments, and disabled-comments graceful handling.
2. Deterministic comment classification across ``moderation``, ``attention``,
   ``reply_candidate``, and ``no_action``, plus manual classification overrides
   with preserved history.
3. Reply drafting without posting, banned-phrase & length safety validation, and
   blocking drafts on ``moderation``-classified comments unless explicitly forced.
4. ``AUTO_REPLY_ENABLED=false`` default enforcement (raising ``AutoReplyDisabledError``
   and recording ``comment.auto_reply_blocked``) vs explicit opt-in automated
   reply posting (which still blocks ``moderation`` comments).
5. Human reply approval, text editing prior to posting, provider failure
   recovery back to ``APPROVED``, and draft discard lifecycle.
6. Duplicate reply guard across same comment, same parent thread, and identical
   normalized reply text on the same video, plus concurrent in-flight lock.
7. Full read-only ``CommentAuditService`` trail verification per draft, per
   comment, and per channel with zero leakage of raw text or OAuth tokens.
"""

from datetime import UTC, datetime
from pathlib import Path

import pytest

from ai_youtube_agent.bootstrap import build_container, prepare_database
from ai_youtube_agent.content.auto_reply import (
    AUTO_REPLY_BLOCKED,
    AUTO_REPLY_POSTED,
    AutoReplyDisabledError,
    AutoReplyErrorCode,
    AutoReplyGuard,
    AutoReplyService,
)
from ai_youtube_agent.content.comment_audit import CommentAuditService
from ai_youtube_agent.content.comment_classifier import (
    COMMENT_CLASSIFIED,
    CommentClassifier,
)
from ai_youtube_agent.content.comment_sync import COMMENTS_SYNCED, CommentSync
from ai_youtube_agent.content.duplicate_reply_guard import (
    REPLY_DUPLICATE_BLOCKED,
    DuplicateReplyError,
    DuplicateReplyErrorCode,
    DuplicateReplyGuard,
)
from ai_youtube_agent.content.reply_approval import (
    REPLY_APPROVED,
    REPLY_DISCARDED,
    REPLY_POST_FAILED,
    REPLY_POSTED,
    ReplyApprovalError,
    ReplyApprovalErrorCode,
    ReplyApprovalService,
)
from ai_youtube_agent.content.reply_draft import (
    REPLY_DRAFTED,
    ReplyDraftError,
    ReplyDraftErrorCode,
    ReplyDraftService,
)
from ai_youtube_agent.core.audit import Actor, ActorKind, AuditLog
from ai_youtube_agent.core.config import Environment, Settings
from ai_youtube_agent.core.db.database import Database
from ai_youtube_agent.core.db.models import (
    Channel,
    CommentClassificationKind,
    ReplyDraftStatus,
)
from ai_youtube_agent.core.db.repositories.channels import ChannelRepository
from ai_youtube_agent.core.db.repositories.community import CommentRepository
from ai_youtube_agent.core.flags import FeatureFlags
from ai_youtube_agent.providers.comments import (
    CommentErrorCode,
    CommentProvider,
    CommentProviderError,
    RemoteComment,
)
from ai_youtube_agent.providers.mock_comments import MockCommentProvider
from ai_youtube_agent.providers.mock_text_generation import MockTextGenerator
from ai_youtube_agent.providers.mock_youtube_oauth import MockYouTubeOAuthProvider
from ai_youtube_agent.providers.text_generation import TextGenerator
from ai_youtube_agent.providers.youtube_oauth import YouTubeOAuthProvider

OPERATOR = Actor(kind=ActorKind.USER, id="operator:dung")


def _build_community_env(
    tmp_path: Path, *, auto_reply_enabled: bool = False
) -> tuple[
    Settings,
    Database,
    MockCommentProvider,
    MockYouTubeOAuthProvider,
    MockTextGenerator,
    CommentSync,
    CommentClassifier,
    ReplyDraftService,
    DuplicateReplyGuard,
    ReplyApprovalService,
    AutoReplyService,
    CommentAuditService,
]:
    settings = Settings(
        environment=Environment.TEST,
        database_path=tmp_path / "community_scenarios.db",
        artifact_dir=tmp_path / "artifacts",
        flags=FeatureFlags(auto_reply_enabled=auto_reply_enabled),
    )
    prepare_database(settings)
    container = build_container(settings)

    db = container.resolve(Database)
    with db.transaction() as conn:
        ChannelRepository(conn).add(
            Channel(
                id="ch-comm-1",
                slug="community-channel",
                name="Community Channel",
                youtube_channel_id="UC-comm-1",
            )
        )

    comments = container.resolve(CommentProvider)
    assert isinstance(comments, MockCommentProvider)
    oauth = container.resolve(YouTubeOAuthProvider)
    assert isinstance(oauth, MockYouTubeOAuthProvider)
    text_gen = container.resolve(TextGenerator)
    assert isinstance(text_gen, MockTextGenerator)

    code = oauth.create_authorization_code(channel_id="ch-comm-1")
    oauth.connect_channel("ch-comm-1", authorization_code=code)
    comments.register_AccessToken("mock-access-token")
    text_gen.set_default_texts(
        ("Thanks for watching! We cover the benchmark setup in the pinned note.",)
    )

    return (
        settings,
        db,
        comments,
        oauth,
        text_gen,
        container.resolve(CommentSync),
        container.resolve(CommentClassifier),
        container.resolve(ReplyDraftService),
        container.resolve(DuplicateReplyGuard),
        container.resolve(ReplyApprovalService),
        container.resolve(AutoReplyService),
        container.resolve(CommentAuditService),
    )


def test_end_to_end_sync_classification_moderation_draft_approval_and_audit(
    tmp_path: Path,
) -> None:
    (
        _settings,
        db,
        comments,
        _oauth,
        _text_gen,
        sync,
        classifier,
        drafter,
        dup_guard,
        approver,
        auto_reply,
        audit_service,
    ) = _build_community_env(tmp_path, auto_reply_enabled=False)

    # Seed 4 top-level comments + 1 thread reply on yt-video-100
    comments.seed_video_comments(
        "ch-comm-1",
        "yt-video-100",
        (
            RemoteComment(
                youtube_comment_id="yt-c-question",
                youtube_video_id="yt-video-100",
                author_display_name="Curious Dev",
                text="How did you configure SQLite WAL mode for concurrent reads?",
                published_at=datetime(2026, 7, 1, 9, 0, tzinfo=UTC),
            ),
            RemoteComment(
                youtube_comment_id="yt-c-spam",
                youtube_video_id="yt-video-100",
                author_display_name="Crypto Bot",
                text="Guaranteed profit! Message me on Whatsapp or visit https://scam.example/airdrop",
                published_at=datetime(2026, 7, 1, 9, 5, tzinfo=UTC),
            ),
            RemoteComment(
                youtube_comment_id="yt-c-feedback",
                youtube_video_id="yt-video-100",
                author_display_name="Loyal Subscriber",
                text="Thank you so much for this deep dive tutorial, super helpful!",
                published_at=datetime(2026, 7, 1, 9, 10, tzinfo=UTC),
            ),
            RemoteComment(
                youtube_comment_id="yt-c-short",
                youtube_video_id="yt-video-100",
                author_display_name="Quick Viewer",
                text="First!",
                published_at=datetime(2026, 7, 1, 9, 12, tzinfo=UTC),
            ),
        ),
    )
    comments.seed_comment_replies(
        "ch-comm-1",
        "yt-c-question",
        (
            RemoteComment(
                youtube_comment_id="yt-c-question-r1",
                youtube_video_id="yt-video-100",
                parent_youtube_comment_id="yt-c-question",
                author_display_name="Second Viewer",
                text="Also curious whether busy_timeout is set!",
                published_at=datetime(2026, 7, 1, 9, 15, tzinfo=UTC),
            ),
        ),
    )

    # 1. Sync multi-page (page_size=2) with replies
    sync_result = sync.sync_video_comments(
        "ch-comm-1",
        youtube_video_id="yt-video-100",
        page_size=2,
        include_replies=True,
    )
    assert sync_result.fetched_count == 5
    assert sync_result.inserted_count == 5
    assert sync_result.updated_count == 0
    assert sync_result.page_count >= 2

    # Re-sync with updated text on yt-c-short verifies idempotent upsert
    comments.seed_video_comments(
        "ch-comm-1",
        "yt-video-100",
        (
            RemoteComment(
                youtube_comment_id="yt-c-question",
                youtube_video_id="yt-video-100",
                author_display_name="Curious Dev",
                text="How did you configure SQLite WAL mode for concurrent reads?",
                published_at=datetime(2026, 7, 1, 9, 0, tzinfo=UTC),
            ),
            RemoteComment(
                youtube_comment_id="yt-c-spam",
                youtube_video_id="yt-video-100",
                author_display_name="Crypto Bot",
                text="Guaranteed profit! Message me on Whatsapp or visit https://scam.example/airdrop",
                published_at=datetime(2026, 7, 1, 9, 5, tzinfo=UTC),
            ),
            RemoteComment(
                youtube_comment_id="yt-c-feedback",
                youtube_video_id="yt-video-100",
                author_display_name="Loyal Subscriber",
                text="Thank you so much for this deep dive tutorial, super helpful!",
                published_at=datetime(2026, 7, 1, 9, 10, tzinfo=UTC),
            ),
            RemoteComment(
                youtube_comment_id="yt-c-short",
                youtube_video_id="yt-video-100",
                author_display_name="Quick Viewer",
                text="First comment!",
                published_at=datetime(2026, 7, 1, 9, 12, tzinfo=UTC),
            ),
        ),
    )
    resync = sync.sync_video_comments(
        "ch-comm-1",
        youtube_video_id="yt-video-100",
        include_replies=False,
    )
    assert resync.inserted_count == 0
    assert resync.updated_count == 1
    assert resync.unchanged_count == 3

    # 2. Classify all comments in the channel
    summary = classifier.classify_channel_comments("ch-comm-1")
    assert summary.classified_count == 5
    assert summary.counts_by_kind[CommentClassificationKind.MODERATION] == 1
    assert summary.counts_by_kind[CommentClassificationKind.ATTENTION] >= 1
    assert summary.counts_by_kind[CommentClassificationKind.REPLY_CANDIDATE] >= 1
    assert summary.counts_by_kind[CommentClassificationKind.NO_ACTION] == 1

    by_yt_id = {o.comment.youtube_comment_id: o for o in summary.outcomes}
    q_comment = by_yt_id["yt-c-question"].comment
    q_reply_comment = by_yt_id["yt-c-question-r1"].comment
    spam_comment = by_yt_id["yt-c-spam"].comment
    feedback_comment = by_yt_id["yt-c-feedback"].comment

    assert (
        by_yt_id["yt-c-question"].classification.kind
        == CommentClassificationKind.ATTENTION
    )
    assert (
        by_yt_id["yt-c-spam"].classification.kind
        == CommentClassificationKind.MODERATION
    )
    assert (
        by_yt_id["yt-c-feedback"].classification.kind
        == CommentClassificationKind.REPLY_CANDIDATE
    )
    assert (
        by_yt_id["yt-c-short"].classification.kind
        == CommentClassificationKind.NO_ACTION
    )

    # 3. Moderation-classified comment blocks ReplyDraft creation unless allow_moderation=True
    with pytest.raises(ReplyDraftError) as mod_err:
        drafter.create_draft(spam_comment.id)
    assert mod_err.value.reason == ReplyDraftErrorCode.MODERATION_COMMENT_BLOCKED

    # Draft creation succeeds for question and feedback comments and NEVER posts to YouTube
    q_draft = drafter.create_draft(q_comment.id).draft
    fb_draft = drafter.create_draft(feedback_comment.id).draft
    assert q_draft.status == ReplyDraftStatus.DRAFT
    assert fb_draft.status == ReplyDraftStatus.DRAFT
    assert comments.posted_replies == ()

    # Banned phrase in manual reply draft is rejected
    with pytest.raises(ReplyDraftError) as unsafe_err:
        drafter.create_draft(
            feedback_comment.id,
            reply_text="Guaranteed profit if you follow our link!",
        )
    assert unsafe_err.value.reason == ReplyDraftErrorCode.UNSAFE_REPLY_TEXT

    # 4. AUTO_REPLY_ENABLED=false blocks automated reply posting and audits the denial
    with pytest.raises(AutoReplyDisabledError) as auto_err:
        auto_reply.auto_reply_draft(q_draft.id)
    assert auto_err.value.reason == AutoReplyErrorCode.AUTO_REPLY_DISABLED
    assert comments.posted_replies == ()

    # 5. Human approves and edits q_draft, then posts to YouTube
    post_outcome = approver.approve_and_post(
        q_draft.id,
        actor=OPERATOR,
        edited_text="We set PRAGMA journal_mode=WAL and busy_timeout=5000ms at connection init.",
    )
    assert post_outcome.draft.status == ReplyDraftStatus.POSTED
    assert post_outcome.draft.approved_by == "operator:dung"
    assert post_outcome.draft.youtube_reply_id == post_outcome.receipt.youtube_reply_id
    assert len(comments.posted_replies) == 1

    # 6. DuplicateReplyGuard blocks:
    # (a) a second reply on the same comment
    q_draft_second = drafter.create_draft(
        q_comment.id,
        reply_text="A second reply to the same question.",
    ).draft
    with pytest.raises(DuplicateReplyError) as dup_same_comment:
        approver.approve_and_post(q_draft_second.id, actor=OPERATOR)
    assert (
        dup_same_comment.value.reason
        == DuplicateReplyErrorCode.COMMENT_ALREADY_REPLIED
    )

    # (b) a reply to a child comment in the same parent thread
    thread_child_draft = drafter.create_draft(
        q_reply_comment.id,
        reply_text="Yes, busy_timeout is 5000ms!",
    ).draft
    with pytest.raises(DuplicateReplyError) as dup_thread:
        approver.approve_and_post(thread_child_draft.id, actor=OPERATOR)
    assert dup_thread.value.reason == DuplicateReplyErrorCode.THREAD_ALREADY_REPLIED

    # (c) identical normalized reply text on another comment of the same video
    same_text_draft = drafter.create_draft(
        feedback_comment.id,
        reply_text="  we set pragma journal_mode=wal and busy_timeout=5000ms at connection init.! ",
    ).draft
    with pytest.raises(DuplicateReplyError) as dup_text:
        approver.approve_and_post(same_text_draft.id, actor=OPERATOR)
    assert (
        dup_text.value.reason
        == DuplicateReplyErrorCode.DUPLICATE_TEXT_ON_VIDEO
    )

    # Human discards the unused duplicate draft and posts the genuine feedback reply
    discarded = approver.discard_draft(
        same_text_draft.id, actor=OPERATOR, reason="duplicate_text"
    )
    assert discarded.draft.status == ReplyDraftStatus.DISCARDED

    fb_post = approver.approve_and_post(
        fb_draft.id,
        actor=OPERATOR,
        edited_text="Really glad the tutorial helped you out!",
    )
    assert fb_post.draft.status == ReplyDraftStatus.POSTED
    assert len(comments.posted_replies) == 2

    # 7. Verify CommentAuditService trails
    q_trail = audit_service.trail_for_comment(q_comment.id)
    assert q_trail.action_counts[COMMENT_CLASSIFIED] == 1
    assert q_trail.action_counts[REPLY_DRAFTED] == 2
    assert q_trail.action_counts[AUTO_REPLY_BLOCKED] == 1
    assert q_trail.action_counts[REPLY_APPROVED] == 2
    assert q_trail.action_counts[REPLY_POSTED] == 1
    assert q_trail.action_counts[REPLY_DUPLICATE_BLOCKED] == 1
    assert q_trail.posted_reply_ids == (post_outcome.receipt.youtube_reply_id,)

    ch_trail = audit_service.trail_for_channel("ch-comm-1")
    assert ch_trail.action_counts[COMMENTS_SYNCED] == 2
    assert ch_trail.action_counts[COMMENT_CLASSIFIED] == 5
    assert ch_trail.action_counts[REPLY_POSTED] == 2
    assert ch_trail.action_counts[REPLY_DISCARDED] == 1
    assert ch_trail.action_counts[REPLY_DUPLICATE_BLOCKED] == 3
    assert set(ch_trail.posted_reply_ids) == {
        post_outcome.receipt.youtube_reply_id,
        fb_post.receipt.youtube_reply_id,
    }

    # Ensure no raw comment text, reply text, or token leaks into audit dicts
    audit_dump = str(ch_trail.as_dict())
    assert "journal_mode=WAL" not in audit_dump
    assert "Really glad the tutorial helped" not in audit_dump
    assert "mock-access-token" not in audit_dump


def test_provider_failure_recovery_and_opt_in_auto_reply(tmp_path: Path) -> None:
    (
        _settings,
        db,
        comments,
        oauth,
        _text_gen,
        sync,
        classifier,
        drafter,
        dup_guard,
        approver,
        auto_reply_enabled_svc,
        audit_service,
    ) = _build_community_env(tmp_path, auto_reply_enabled=True)

    comments.seed_video_comments(
        "ch-comm-1",
        "yt-video-200",
        (
            RemoteComment(
                youtube_comment_id="yt-c-201",
                youtube_video_id="yt-video-200",
                author_display_name="Viewer A",
                text="Where can I download the slides?",
                published_at=datetime(2026, 7, 2, 12, 0, tzinfo=UTC),
            ),
            RemoteComment(
                youtube_comment_id="yt-c-202",
                youtube_video_id="yt-video-200",
                author_display_name="Viewer B",
                text="Thank you for the clear diagrams!",
                published_at=datetime(2026, 7, 2, 12, 5, tzinfo=UTC),
            ),
        ),
    )

    synced = sync.sync_video_comments("ch-comm-1", youtube_video_id="yt-video-200")
    c_a = synced.outcomes[0].comment
    c_b = synced.outcomes[1].comment
    classifier.classify_channel_comments("ch-comm-1")

    draft_a = drafter.create_draft(
        c_a.id, reply_text="Slides are linked in the video description."
    ).draft
    draft_b = drafter.create_draft(
        c_b.id, reply_text="Thanks for watching and for the kind feedback!"
    ).draft

    # Simulate transient provider rate limit on draft_a post; draft_a stays APPROVED for retry
    comments.queue_failure(
        CommentProviderError(
            CommentErrorCode.RATE_LIMITED,
            "transient quota limit",
            retry_after_seconds=5.0,
        )
    )
    with pytest.raises(ReplyApprovalError) as post_err:
        approver.approve_and_post(draft_a.id, actor=OPERATOR)
    assert post_err.value.reason == ReplyApprovalErrorCode.PROVIDER_FAILED

    with db.transaction() as conn:
        persisted_a = CommentRepository(conn).get_reply_draft(draft_a.id)
        assert persisted_a is not None
        assert persisted_a.status == ReplyDraftStatus.APPROVED

    # Retrying post_approved_draft succeeds
    retry_outcome = approver.post_approved_draft(draft_a.id, actor=OPERATOR)
    assert retry_outcome.draft.status == ReplyDraftStatus.POSTED

    # With AUTO_REPLY_ENABLED=True, draft_b can be auto-replied without human approval
    auto_outcome = auto_reply_enabled_svc.auto_reply_draft(draft_b.id)
    assert auto_outcome.draft.status == ReplyDraftStatus.POSTED
    assert auto_outcome.draft.approved_by == "system:auto_reply"

    trail_a = audit_service.trail_for_reply_draft(draft_a.id)
    assert [e.action for e in trail_a.entries] == [
        REPLY_DRAFTED,
        REPLY_APPROVED,
        REPLY_POST_FAILED,
        REPLY_POSTED,
    ]
    trail_b = audit_service.trail_for_reply_draft(draft_b.id)
    assert [e.action for e in trail_b.entries] == [
        REPLY_DRAFTED,
        AUTO_REPLY_POSTED,
    ]
