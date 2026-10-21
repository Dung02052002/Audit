"""Tests for S-218 Least Privilege & Scope Policy."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import SecretStr

from ai_youtube_agent.core.audit import Actor, ActorKind
from ai_youtube_agent.core.least_privilege import (
    LeastPrivilegeError,
    LeastPrivilegePolicy,
    PrivilegedAction,
    YouTubeOperation,
)
from ai_youtube_agent.providers.youtube_oauth import OAuthScope, OAuthToken


def _make_token(*scopes: OAuthScope) -> OAuthToken:
    t0 = datetime(2026, 10, 21, 12, 0, tzinfo=UTC)
    return OAuthToken(
        channel_id="ch-1",
        access_token=SecretStr("ya29.test_token"),
        refresh_token=None,
        scopes=frozenset(scopes),
        issued_at=t0,
        expires_at=t0 + timedelta(hours=1),
    )


def test_minimal_and_excess_oauth_scopes_per_operation() -> None:
    policy = LeastPrivilegePolicy()

    readonly_tok = _make_token(OAuthScope.YOUTUBE_READONLY)
    eval_read = policy.enforce_token_scopes(
        readonly_tok,
        YouTubeOperation.CHANNEL_READ,
        forbid_excess_scopes=True,
    )
    assert eval_read.authorized is True
    assert eval_read.is_minimal is True

    # Readonly token cannot upload video
    with pytest.raises(LeastPrivilegeError) as exc_missing:
        policy.enforce_token_scopes(readonly_tok, YouTubeOperation.VIDEO_UPLOAD)
    assert exc_missing.value.code == "domain.insufficient_oauth_scope"

    # Over-scoped token flagged when forbid_excess_scopes=True
    over_scoped = _make_token(
        OAuthScope.YOUTUBE_READONLY,
        OAuthScope.YOUTUBE_UPLOAD,
        OAuthScope.YT_ANALYTICS_MONETARY_READONLY,
    )
    with pytest.raises(LeastPrivilegeError) as exc_excess:
        policy.enforce_token_scopes(
            over_scoped,
            YouTubeOperation.CHANNEL_READ,
            forbid_excess_scopes=True,
        )
    assert exc_excess.value.code == "domain.excess_oauth_scope"


def test_actor_permission_matrix_blocks_ai_and_system_from_human_only_actions() -> None:
    policy = LeastPrivilegePolicy()
    user = Actor(ActorKind.USER, "dung")
    system = Actor(ActorKind.SYSTEM, "scheduler")
    ai = Actor(ActorKind.AI, "planner")

    for action in PrivilegedAction:
        policy.enforce_actor_permission(user, action)
        with pytest.raises(LeastPrivilegeError):
            policy.enforce_actor_permission(ai, action)

    # System may activate kill switch, but cannot deactivate it or approve content
    policy.enforce_actor_permission(system, PrivilegedAction.ACTIVATE_KILL_SWITCH)
    with pytest.raises(LeastPrivilegeError):
        policy.enforce_actor_permission(system, PrivilegedAction.DEACTIVATE_KILL_SWITCH)
    with pytest.raises(LeastPrivilegeError):
        policy.enforce_actor_permission(system, PrivilegedAction.APPROVE_CONTENT)
    with pytest.raises(LeastPrivilegeError):
        policy.enforce_actor_permission(system, PrivilegedAction.UPDATE_STRATEGY)
