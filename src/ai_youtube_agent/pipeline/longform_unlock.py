"""LongForm Unlock Gate & Service (Prompt Pack v8, prompt T-237), context C3/C16.

Evaluates whether all required technical gates pass before allowing a human operator
(``ActorKind.USER``) to unlock LongForm production
(``FeatureFlags.longform_enabled = True``,
R-04 / R-05). Never unlocks LongForm autonomously.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final

from ai_youtube_agent.content.channel import ChannelStatus
from ai_youtube_agent.content.rights import RiskLevel
from ai_youtube_agent.core.audit import (
    Actor,
    ActorKind,
    AuditLog,
    AuditResult,
    EntityRef,
)
from ai_youtube_agent.core.config import Settings
from ai_youtube_agent.core.content_item import ContentStatus, ContentType
from ai_youtube_agent.core.db.database import Database
from ai_youtube_agent.core.db.repositories.channel import (
    ChannelRepository,
    StrategyProfileRepository,
)
from ai_youtube_agent.core.db.repositories.content import ContentItemRepository
from ai_youtube_agent.core.db.repositories.review import RightsRecordRepository
from ai_youtube_agent.core.errors import DomainError
from ai_youtube_agent.core.flags import FeatureFlags
from ai_youtube_agent.core.kill_switch_gate import KillSwitchSource
from ai_youtube_agent.core.security_audit import SecurityAuditor
from ai_youtube_agent.pipeline.pilot_runner import ShortsPilotReport, ShortsPilotRunner

Clock = Callable[[], datetime]

LONGFORM_UNLOCKED_ACTION: Final[str] = "longform.unlocked"
LONGFORM_UNLOCK_BLOCKED_ACTION: Final[str] = "longform.unlock_blocked"


class LongFormUnlockError(DomainError):
    """Base error for LongForm unlock evaluation and enforcement."""

    default_code = "domain.longform_unlock_error"
    default_user_message = "LongForm production could not be unlocked."


class LongFormUnlockNotAllowedError(LongFormUnlockError):
    """Raised when a non-human actor attempts to unlock LongForm production."""

    default_code = "domain.longform_unlock_not_allowed"
    default_user_message = "Only an explicit human user may unlock LongForm production."

    def __init__(
        self, detail: str = "Only ActorKind.USER may unlock LongForm."
    ) -> None:
        super().__init__(
            detail,
            code=self.default_code,
            user_message=self.default_user_message,
        )


class LongFormUnlockBlockedError(LongFormUnlockError):
    """Raised when required technical gates for LongForm unlock have not passed."""

    default_code = "domain.longform_unlock_blocked"
    default_user_message = (
        "LongForm production cannot be unlocked until all required technical "
        "gates pass."
    )

    def __init__(self, evaluation: LongFormUnlockEvaluation) -> None:
        self.evaluation = evaluation
        codes = ", ".join(evaluation.blockers)
        super().__init__(
            f"LongForm unlock blocked for channel {evaluation.channel_id}: {codes}",
            code=self.default_code,
            user_message=self.default_user_message,
        )


@dataclass(frozen=True)
class UnlockGateCheck:
    """Result of one technical gate required to unlock LongForm."""

    gate_name: str
    passed: bool
    detail: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "gate_name": self.gate_name,
            "passed": self.passed,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class LongFormUnlockEvaluation:
    """Comprehensive technical gate evaluation for unlocking LongForm on a channel."""

    channel_id: str
    eligible: bool
    checks: tuple[UnlockGateCheck, ...]
    blockers: tuple[str, ...]
    passing_pilot_item_id: str | None
    evaluated_at: datetime

    def as_dict(self) -> dict[str, Any]:
        return {
            "channel_id": self.channel_id,
            "eligible": self.eligible,
            "checks": [c.as_dict() for c in self.checks],
            "blockers": list(self.blockers),
            "passing_pilot_item_id": self.passing_pilot_item_id,
            "evaluated_at": self.evaluated_at.isoformat(),
        }


@dataclass(frozen=True)
class LongFormUnlockDecision:
    """Outcome of an explicit user request to unlock LongForm production."""

    channel_id: str
    unlocked: bool
    updated_flags: FeatureFlags
    evaluation: LongFormUnlockEvaluation
    decided_by: Actor

    def as_dict(self) -> dict[str, Any]:
        return {
            "channel_id": self.channel_id,
            "unlocked": self.unlocked,
            "updated_flags": {
                "shorts_enabled": self.updated_flags.shorts_enabled,
                "longform_enabled": self.updated_flags.longform_enabled,
                "publish_enabled": self.updated_flags.publish_enabled,
                "auto_reply_enabled": self.updated_flags.auto_reply_enabled,
                "test_required": self.updated_flags.test_required,
                "approval_required": self.updated_flags.approval_required,
            },
            "evaluation": self.evaluation.as_dict(),
            "decided_by": {
                "kind": self.decided_by.kind.value,
                "id": self.decided_by.id,
            },
        }


class LongFormUnlockService:
    """Evaluates technical gates and unlocks LongForm only when all gates pass.

    (T-237)
    """

    def __init__(
        self,
        database: Database,
        audit_log: AuditLog,
        *,
        pilot_runner: ShortsPilotRunner | None = None,
        kill_switch: KillSwitchSource | None = None,
        security_auditor: SecurityAuditor | None = None,
        settings: Settings | None = None,
        clock: Clock | None = None,
    ) -> None:
        self._database = database
        self._audit = audit_log
        self._pilot_runner = pilot_runner or ShortsPilotRunner(
            database, audit_log, clock=clock
        )
        self._kill_switch = kill_switch
        self._security_auditor = security_auditor
        self._settings = settings
        self._clock = clock

    def _now(self) -> datetime:
        return self._clock() if self._clock else datetime.now(UTC)

    def evaluate(self, channel_id: str) -> LongFormUnlockEvaluation:
        """Evaluates all required technical gates for unlocking LongForm.

        For ``channel_id``.
        """
        now = self._now()
        checks: list[UnlockGateCheck] = []
        blockers: list[str] = []

        with self._database.connection() as conn:
            channel = ChannelRepository(conn).get(channel_id)
            strategy = StrategyProfileRepository(conn).get_by_channel(channel_id)
            items = ContentItemRepository(conn).list_by_channel(channel_id)
            rights_repo = RightsRecordRepository(conn)
            high_risk_count = 0
            for item in items:
                for rec in rights_repo.list_by_content_item(item.id):
                    if rec.risk_level is RiskLevel.HIGH:
                        high_risk_count += 1

        # Gate 1: Emergency stop inactive
        ks_active = bool(self._kill_switch and self._kill_switch.current().active)
        checks.append(
            UnlockGateCheck(
                "kill_switch_inactive",
                not ks_active,
                "Emergency stop is inactive"
                if not ks_active
                else "Emergency stop (kill switch) is currently active",
            )
        )
        if ks_active:
            blockers.append("unlock.kill_switch_active")

        # Gate 2: Active channel & complete strategy with LongForm cadence > 0
        channel_ok = channel is not None and channel.status is ChannelStatus.ACTIVE
        strategy_ok = (
            strategy is not None
            and strategy.is_complete
            and strategy.format is not None
            and strategy.cadence is not None
            and strategy.cadence.longform_per_day >= 1
        )
        config_ok = channel_ok and strategy_ok
        checks.append(
            UnlockGateCheck(
                "channel_and_longform_strategy",
                config_ok,
                "Channel is ACTIVE and strategy is complete with longform_per_day >= 1"
                if config_ok
                else (
                    "Channel is missing/inactive, strategy is incomplete, "
                    "or longform_per_day < 1"
                ),
            )
        )
        if not config_ok:
            blockers.append("unlock.strategy_or_channel_not_ready")

        # Gate 3: Completed & approved Shorts pilot on this channel (T-236)
        published_shorts = [
            i
            for i in items
            if i.content_type is ContentType.SHORTS
            and i.status is ContentStatus.PUBLISHED
        ]
        passing_pilot: ShortsPilotReport | None = None
        for short_item in published_shorts:
            report = self._pilot_runner.evaluate_pilot(short_item.id)
            if report.passed:
                passing_pilot = report
                break

        pilot_ok = passing_pilot is not None
        checks.append(
            UnlockGateCheck(
                "shorts_pilot_passed",
                pilot_ok,
                (
                    f"Shorts pilot passed on item {passing_pilot.content_item_id} "
                    f"({passing_pilot.pilot_id})"
                )
                if passing_pilot
                else (
                    "No completed Shorts pilot with human approval and published "
                    "receipt found"
                ),
            )
        )
        if not pilot_ok:
            blockers.append("unlock.shorts_pilot_required")

        # Gate 4: Zero unresolved HIGH-risk rights findings on channel
        rights_ok = high_risk_count == 0
        checks.append(
            UnlockGateCheck(
                "rights_compliance_clean",
                rights_ok,
                "Zero high-risk rights records on channel"
                if rights_ok
                else (
                    f"{high_risk_count} unresolved HIGH-risk rights record(s) "
                    "on channel"
                ),
            )
        )
        if not rights_ok:
            blockers.append("unlock.unresolved_high_rights_risk")

        # Gate 5: Security audit (if configured)
        if self._security_auditor is not None and self._settings is not None:
            sec_report = self._security_auditor.run_audit(self._settings)
            sec_ok = sec_report.passed
            checks.append(
                UnlockGateCheck(
                    "security_audit_passed",
                    sec_ok,
                    "Security audit passed with 0 high/critical findings"
                    if sec_ok
                    else (
                        f"Security audit failed ({sec_report.critical_count} "
                        f"critical, {sec_report.high_count} high)"
                    ),
                )
            )
            if not sec_ok:
                blockers.append("unlock.security_audit_failed")

        eligible = len(blockers) == 0
        return LongFormUnlockEvaluation(
            channel_id=channel_id,
            eligible=eligible,
            checks=tuple(checks),
            blockers=tuple(blockers),
            passing_pilot_item_id=(
                passing_pilot.content_item_id if passing_pilot else None
            ),
            evaluated_at=now,
        )

    def unlock_longform(
        self,
        channel_id: str,
        *,
        actor: Actor,
        current_flags: FeatureFlags | None = None,
    ) -> LongFormUnlockDecision:
        """Unlocks LongForm production (``longform_enabled=True``) if all technical
        gates pass.

        Requires an explicit human actor (``ActorKind.USER``). Raises
        ``LongFormUnlockBlockedError`` if any technical gate fails.
        """
        if not isinstance(actor, Actor) or actor.kind is not ActorKind.USER:
            raise LongFormUnlockNotAllowedError()

        base_flags = current_flags or (
            self._settings.flags if self._settings is not None else FeatureFlags()
        )
        evaluation = self.evaluate(channel_id)
        if not evaluation.eligible:
            self._audit.record(
                action=LONGFORM_UNLOCK_BLOCKED_ACTION,
                actor=actor,
                entity=EntityRef("channel", channel_id),
                result=AuditResult.DENIED,
                metadata={
                    "channel_id": channel_id,
                    "blockers": ",".join(evaluation.blockers),
                },
            )
            raise LongFormUnlockBlockedError(evaluation)

        updated_flags = base_flags.model_copy(update={"longform_enabled": True})
        self._audit.record(
            action=LONGFORM_UNLOCKED_ACTION,
            actor=actor,
            entity=EntityRef("channel", channel_id),
            result=AuditResult.SUCCESS,
            metadata={
                "channel_id": channel_id,
                "passing_pilot_item_id": evaluation.passing_pilot_item_id or "",
                "longform_enabled": True,
            },
        )
        return LongFormUnlockDecision(
            channel_id=channel_id,
            unlocked=True,
            updated_flags=updated_flags,
            evaluation=evaluation,
            decided_by=actor,
        )
