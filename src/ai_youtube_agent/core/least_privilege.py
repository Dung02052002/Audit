"""Least Privilege & Scope Policy (Prompt Pack v8, prompt #218), area X4 Security.

Defines and enforces the minimal OAuth scopes per YouTube operation and the
actor-kind permission matrix so neither tokens nor AI/system actors hold or
exercise excessive privileges.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Final

from ai_youtube_agent.core.audit import Actor, ActorKind
from ai_youtube_agent.core.errors import DomainError
from ai_youtube_agent.providers.youtube_oauth import OAuthScope, OAuthToken


class YouTubeOperation(StrEnum):
    """Distinct YouTube API operations with minimized scope requirements."""

    CHANNEL_READ = "channel.read"
    VIDEO_UPLOAD = "video.upload"
    ANALYTICS_READ = "analytics.read"
    REVENUE_READ = "revenue.read"
    COMMENT_READ = "comment.read"
    COMMENT_REPLY_POST = "comment.reply_post"


class PrivilegedAction(StrEnum):
    """Governance and safety actions restricted by actor kind."""

    APPROVE_CONTENT = "content.approve"
    REJECT_CONTENT = "content.reject"
    REQUEST_CONTENT_CHANGES = "content.request_changes"
    UPDATE_STRATEGY = "strategy.update"
    UPDATE_VOICE_PROFILE = "voice_profile.update"
    RESOLVE_RIGHTS_RISK = "rights.resolve"
    START_EXPERIMENT = "experiment.start"
    CONCLUDE_EXPERIMENT = "experiment.conclude"
    CANCEL_EXPERIMENT = "experiment.cancel"
    START_SESSION = "session.start"
    ACTIVATE_KILL_SWITCH = "kill_switch.activate"
    DEACTIVATE_KILL_SWITCH = "kill_switch.deactivate"
    APPROVE_COMMENT_REPLY = "comment_reply.approve"


@dataclass(frozen=True)
class OperationScopeSpec:
    """Minimal and excess scope policy for one ``YouTubeOperation``."""

    operation: YouTubeOperation
    required_scopes: frozenset[OAuthScope]
    accepted_alternatives: frozenset[OAuthScope]
    justification: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "operation": self.operation.value,
            "required_scopes": sorted(s.value for s in self.required_scopes),
            "accepted_alternatives": sorted(s.value for s in self.accepted_alternatives),
            "justification": self.justification,
        }


OPERATION_SCOPE_MATRIX: Final[dict[YouTubeOperation, OperationScopeSpec]] = {
    YouTubeOperation.CHANNEL_READ: OperationScopeSpec(
        operation=YouTubeOperation.CHANNEL_READ,
        required_scopes=frozenset({OAuthScope.YOUTUBE_READONLY}),
        accepted_alternatives=frozenset(),
        justification="Read-only channel metadata verification requires only youtube.readonly.",
    ),
    YouTubeOperation.VIDEO_UPLOAD: OperationScopeSpec(
        operation=YouTubeOperation.VIDEO_UPLOAD,
        required_scopes=frozenset({OAuthScope.YOUTUBE_UPLOAD}),
        accepted_alternatives=frozenset(),
        justification="Uploading approved videos requires youtube.upload without full account management.",
    ),
    YouTubeOperation.ANALYTICS_READ: OperationScopeSpec(
        operation=YouTubeOperation.ANALYTICS_READ,
        required_scopes=frozenset({OAuthScope.YT_ANALYTICS_READONLY}),
        accepted_alternatives=frozenset(),
        justification="Reading non-monetary channel and video metrics requires only yt-analytics.readonly.",
    ),
    YouTubeOperation.REVENUE_READ: OperationScopeSpec(
        operation=YouTubeOperation.REVENUE_READ,
        required_scopes=frozenset({OAuthScope.YT_ANALYTICS_MONETARY_READONLY}),
        accepted_alternatives=frozenset(),
        justification="Reading estimated and finalized revenue requires yt-analytics-monetary.readonly.",
    ),
    YouTubeOperation.COMMENT_READ: OperationScopeSpec(
        operation=YouTubeOperation.COMMENT_READ,
        required_scopes=frozenset({OAuthScope.YOUTUBE_READONLY}),
        accepted_alternatives=frozenset({OAuthScope.YOUTUBE_FORCE_SSL}),
        justification="Syncing comments prefers youtube.readonly and accepts youtube.force-ssl when reply posting is authorized.",
    ),
    YouTubeOperation.COMMENT_REPLY_POST: OperationScopeSpec(
        operation=YouTubeOperation.COMMENT_REPLY_POST,
        required_scopes=frozenset({OAuthScope.YOUTUBE_FORCE_SSL}),
        accepted_alternatives=frozenset(),
        justification="Posting approved comment replies requires youtube.force-ssl per YouTube Data API v3.",
    ),
}

ALLOWED_ACTORS_BY_ACTION: Final[dict[PrivilegedAction, frozenset[ActorKind]]] = {
    PrivilegedAction.APPROVE_CONTENT: frozenset({ActorKind.USER}),
    PrivilegedAction.REJECT_CONTENT: frozenset({ActorKind.USER}),
    PrivilegedAction.REQUEST_CONTENT_CHANGES: frozenset({ActorKind.USER}),
    PrivilegedAction.UPDATE_STRATEGY: frozenset({ActorKind.USER}),
    PrivilegedAction.UPDATE_VOICE_PROFILE: frozenset({ActorKind.USER}),
    PrivilegedAction.RESOLVE_RIGHTS_RISK: frozenset({ActorKind.USER}),
    PrivilegedAction.START_EXPERIMENT: frozenset({ActorKind.USER}),
    PrivilegedAction.CONCLUDE_EXPERIMENT: frozenset({ActorKind.USER}),
    PrivilegedAction.CANCEL_EXPERIMENT: frozenset({ActorKind.USER}),
    PrivilegedAction.START_SESSION: frozenset({ActorKind.USER}),
    PrivilegedAction.ACTIVATE_KILL_SWITCH: frozenset({ActorKind.USER, ActorKind.SYSTEM}),
    PrivilegedAction.DEACTIVATE_KILL_SWITCH: frozenset({ActorKind.USER}),
    PrivilegedAction.APPROVE_COMMENT_REPLY: frozenset({ActorKind.USER}),
}


class LeastPrivilegeError(DomainError):
    """Raised when an actor or OAuth token violates least-privilege policy."""

    def __init__(
        self,
        detail: str,
        *,
        code: str = "domain.least_privilege_violation",
        user_message: str = "The requested operation is not permitted under least-privilege policy.",
    ) -> None:
        super().__init__(detail, code=code, user_message=user_message)


@dataclass(frozen=True)
class ScopeEvaluation:
    """Result of checking a token's scopes against an operation's minimal policy."""

    operation: YouTubeOperation
    authorized: bool
    missing_scopes: frozenset[OAuthScope]
    excess_scopes: frozenset[OAuthScope]

    @property
    def is_minimal(self) -> bool:
        return self.authorized and not self.excess_scopes

    def as_dict(self) -> dict[str, Any]:
        return {
            "operation": self.operation.value,
            "authorized": self.authorized,
            "is_minimal": self.is_minimal,
            "missing_scopes": sorted(s.value for s in self.missing_scopes),
            "excess_scopes": sorted(s.value for s in self.excess_scopes),
        }


class LeastPrivilegePolicy:
    """Evaluates and enforces minimal OAuth scopes and actor permissions (#218)."""

    def required_scopes_for(self, operation: YouTubeOperation) -> frozenset[OAuthScope]:
        return OPERATION_SCOPE_MATRIX[operation].required_scopes

    def evaluate_token_scopes(
        self,
        token: OAuthToken,
        operation: YouTubeOperation,
    ) -> ScopeEvaluation:
        spec = OPERATION_SCOPE_MATRIX[operation]
        allowed = spec.required_scopes | spec.accepted_alternatives
        if spec.required_scopes.issubset(token.scopes) or (
            spec.accepted_alternatives and bool(spec.accepted_alternatives & token.scopes)
        ):
            missing: frozenset[OAuthScope] = frozenset()
            authorized = True
        else:
            missing = spec.required_scopes - token.scopes
            authorized = False
        excess = token.scopes - allowed
        return ScopeEvaluation(
            operation=operation,
            authorized=authorized,
            missing_scopes=missing,
            excess_scopes=excess,
        )

    def enforce_token_scopes(
        self,
        token: OAuthToken,
        operation: YouTubeOperation,
        *,
        forbid_excess_scopes: bool = False,
    ) -> ScopeEvaluation:
        evaluation = self.evaluate_token_scopes(token, operation)
        if not evaluation.authorized:
            missing_str = ", ".join(sorted(s.value for s in evaluation.missing_scopes))
            raise LeastPrivilegeError(
                f"Token lacks required scopes for {operation.value}: {missing_str}",
                code="domain.insufficient_oauth_scope",
            )
        if forbid_excess_scopes and evaluation.excess_scopes:
            excess_str = ", ".join(sorted(s.value for s in evaluation.excess_scopes))
            raise LeastPrivilegeError(
                f"Token carries excess scopes for {operation.value}: {excess_str}",
                code="domain.excess_oauth_scope",
            )
        return evaluation

    def enforce_actor_permission(
        self,
        actor: Actor,
        action: PrivilegedAction,
    ) -> None:
        if not isinstance(actor, Actor):
            raise LeastPrivilegeError("actor must be an Actor.")
        allowed_kinds = ALLOWED_ACTORS_BY_ACTION[action]
        if actor.kind not in allowed_kinds:
            raise LeastPrivilegeError(
                f"Actor {actor.kind.value}:{actor.id} is not permitted to perform {action.value}.",
                code="domain.actor_privilege_denied",
                user_message="Only an authorized human user may perform this action.",
            )
