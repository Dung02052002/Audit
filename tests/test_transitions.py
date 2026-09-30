"""C-032 Transition Rules (Prompt Pack v8, prompt #032).

The allowed transitions are the table the user approved on 2026-09-30. Every
other pair of different statuses is blocked with ``ContentTransitionError``.
"""

import dataclasses
import itertools
from datetime import UTC, datetime, timedelta

import pytest

from ai_youtube_agent.core.content_item import (
    ALLOWED_TRANSITIONS,
    ContentItem,
    ContentStatus,
    ContentTransitionError,
    ContentType,
    allowed_transitions,
    can_transition,
    is_final,
)
from ai_youtube_agent.core.errors import DomainError, ErrorCategory

S = ContentStatus
T0 = datetime(2026, 9, 30, 10, 0, tzinfo=UTC)
T1 = T0 + timedelta(minutes=5)

# The approved table, written out in full so a change to the code shows here.
EXPECTED = {
    S.DRAFT: {S.GENERATING, S.FAILED},
    S.GENERATING: {S.TESTING, S.FAILED},
    S.TESTING: {S.PREVIEW_READY, S.GENERATING, S.FAILED},
    S.PREVIEW_READY: {S.AWAITING_APPROVAL, S.GENERATING, S.FAILED},
    S.AWAITING_APPROVAL: {S.APPROVED, S.REJECTED, S.GENERATING, S.FAILED},
    S.APPROVED: {S.PUBLISHING, S.GENERATING, S.FAILED},
    S.PUBLISHING: {S.PUBLISHED, S.FAILED},
    S.PUBLISHED: set(),
    S.REJECTED: {S.DRAFT},
    S.FAILED: {S.DRAFT},
}

ALLOWED = [(a, b) for a, targets in EXPECTED.items() for b in sorted(targets)]
BLOCKED = [
    (a, b) for a, b in itertools.permutations(ContentStatus, 2) if b not in EXPECTED[a]
]


def at(moment: datetime):
    return lambda: moment


def item_in(status: ContentStatus) -> ContentItem:
    item = ContentItem.create(
        "channel-1", "strategy-1", 1, ContentType.SHORTS, "Video", clock=at(T0)
    )
    return dataclasses.replace(item, status=status)


# The table


def test_the_table_matches_the_approved_rules() -> None:
    assert set(ALLOWED_TRANSITIONS) == set(ContentStatus)
    assert {a: set(b) for a, b in ALLOWED_TRANSITIONS.items()} == EXPECTED


def test_the_table_is_read_only() -> None:
    with pytest.raises(TypeError):
        ALLOWED_TRANSITIONS[S.PUBLISHED] = frozenset({S.DRAFT})  # type: ignore[index]
    assert isinstance(ALLOWED_TRANSITIONS[S.DRAFT], frozenset)


def test_every_pair_is_either_allowed_or_blocked() -> None:
    assert len(ALLOWED) + len(BLOCKED) == 10 * 9
    assert len(ALLOWED) == 21


def test_no_status_moves_to_itself() -> None:
    for status, targets in ALLOWED_TRANSITIONS.items():
        assert status not in targets


def test_only_published_is_final() -> None:
    assert [s for s in ContentStatus if is_final(s)] == [S.PUBLISHED]


def test_every_status_can_reach_published() -> None:
    reached = {S.PUBLISHED}
    changed = True
    while changed:
        changed = False
        for status, targets in ALLOWED_TRANSITIONS.items():
            if status not in reached and targets & reached:
                reached.add(status)
                changed = True
    assert reached == set(ContentStatus)


def test_the_only_way_into_publishing_is_from_approved() -> None:
    sources = {
        a for a, targets in ALLOWED_TRANSITIONS.items() if S.PUBLISHING in targets
    }
    assert sources == {S.APPROVED}


def test_the_only_way_into_approved_is_from_awaiting_approval() -> None:
    sources = {a for a, targets in ALLOWED_TRANSITIONS.items() if S.APPROVED in targets}
    assert sources == {S.AWAITING_APPROVAL}


def test_regeneration_always_passes_testing_and_preview_again() -> None:
    assert ALLOWED_TRANSITIONS[S.GENERATING] == {S.TESTING, S.FAILED}
    assert {a for a, t in ALLOWED_TRANSITIONS.items() if S.PREVIEW_READY in t} == {
        S.TESTING
    }


# Queries


@pytest.mark.parametrize(("source", "target"), ALLOWED)
def test_can_transition_allows_the_table(source, target) -> None:
    assert can_transition(source, target)
    assert target in allowed_transitions(source)


@pytest.mark.parametrize(("source", "target"), BLOCKED)
def test_can_transition_blocks_everything_else(source, target) -> None:
    assert not can_transition(source, target)
    assert target not in allowed_transitions(source)


def test_queries_refuse_values_that_are_not_statuses() -> None:
    with pytest.raises(TypeError):
        can_transition("draft", S.GENERATING)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        can_transition(S.DRAFT, "generating")  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        allowed_transitions("draft")  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        is_final("published")  # type: ignore[arg-type]


# The guard on ContentItem.with_status


@pytest.mark.parametrize(("source", "target"), ALLOWED)
def test_with_status_makes_every_allowed_move(source, target) -> None:
    item = item_in(source)

    changed = item.with_status(target, clock=at(T1))

    assert changed.status is target
    assert changed.updated_at == T1
    assert (changed.id, changed.created_at) == (item.id, item.created_at)
    assert item.status is source


@pytest.mark.parametrize(("source", "target"), BLOCKED)
def test_with_status_refuses_every_blocked_move(source, target) -> None:
    item = item_in(source)

    with pytest.raises(ContentTransitionError) as caught:
        item.with_status(target, clock=at(T1))

    error = caught.value
    assert (error.item_id, error.from_status, error.to_status) == (
        item.id,
        source,
        target,
    )
    assert item.status is source


@pytest.mark.parametrize("status", list(ContentStatus))
def test_the_same_status_is_a_no_op_even_when_final(status) -> None:
    item = item_in(status)
    assert item.with_status(status, clock=at(T1)) is item


def test_the_full_happy_path() -> None:
    item = item_in(S.DRAFT)
    path = [
        S.GENERATING,
        S.TESTING,
        S.PREVIEW_READY,
        S.AWAITING_APPROVAL,
        S.APPROVED,
        S.PUBLISHING,
        S.PUBLISHED,
    ]
    for status in path:
        item = item.with_status(status, clock=at(T1))
    assert item.status is S.PUBLISHED


def test_rejected_and_failed_items_restart_from_draft() -> None:
    rejected = item_in(S.AWAITING_APPROVAL).with_status(S.REJECTED)
    failed = item_in(S.PUBLISHING).with_status(S.FAILED)

    for item in (rejected, failed):
        assert item.with_status(S.DRAFT).status is S.DRAFT
        with pytest.raises(ContentTransitionError):
            item.with_status(S.APPROVED)


def test_draft_cannot_skip_to_publish() -> None:
    with pytest.raises(ContentTransitionError):
        item_in(S.DRAFT).with_status(S.PUBLISHING)


# The typed error


def test_the_error_is_a_domain_error_with_a_safe_public_view() -> None:
    item = item_in(S.PUBLISHED)
    with pytest.raises(ContentTransitionError) as caught:
        item.with_status(S.DRAFT)

    error = caught.value
    assert isinstance(error, DomainError)
    assert error.code == "domain.content_transition_blocked"
    public = error.to_public()
    assert public.category is ErrorCategory.DOMAIN
    assert public.http_status == 422
    assert public.retryable is False
    assert item.id not in public.message
    assert "published" in error.detail and "draft" in error.detail


def test_the_error_log_fields_name_the_transition() -> None:
    error = ContentTransitionError("item-1", S.PUBLISHED, S.DRAFT)
    fields = error.log_fields()
    assert fields["content_item_id"] == "item-1"
    assert fields["from_status"] == "published"
    assert fields["to_status"] == "draft"
    assert fields["error_code"] == "domain.content_transition_blocked"
