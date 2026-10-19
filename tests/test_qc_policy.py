"""Tests for K-127 Policy QC check (`qc.policy`)."""

from datetime import UTC, datetime

from ai_youtube_agent.content.policy_check import POLICY_RULES, PolicyStatus
from ai_youtube_agent.content.policy_report import (
    FindingReport,
    PolicyReport,
    RuleOutcomeReport,
)
from ai_youtube_agent.content.qc import QCStatus
from ai_youtube_agent.content.qc_policy import (
    POLICY_QC_CHECK_NAME,
    PolicyQCChecker,
    evaluate_policy_qc,
)
from ai_youtube_agent.content.strategy import Brand
from ai_youtube_agent.core.db.database import Database
from ai_youtube_agent.core.db.repositories.channel import (
    ChannelRepository,
    StrategyProfileRepository,
)
from ai_youtube_agent.core.db.repositories.content import ContentItemRepository
from factories import make_channel, make_content_item, make_strategy_profile

NOW = datetime(2026, 10, 19, 12, 0, tzinfo=UTC)


def test_evaluate_policy_qc_fails_on_blocking_report() -> None:
    report = PolicyReport(
        content_item_id="item-1",
        channel_id="ch-1",
        generated_at=NOW,
        rule_set=POLICY_RULES,
        status=PolicyStatus.BLOCK,
        blocks=True,
        outcomes=(
            RuleOutcomeReport(
                rule_id="policy.banned_phrase",
                version=1,
                status="block",
                code="policy.banned_phrase.matched",
                message="Banned phrase detected.",
                field="title",
                blocking=True,
            ),
        ),
        findings=(
            FindingReport(
                rule_id="policy.banned_phrase",
                rule_version=1,
                blocking=True,
                message="Banned phrase detected.",
            ),
        ),
        blocking_findings=(
            FindingReport(
                rule_id="policy.banned_phrase",
                rule_version=1,
                blocking=True,
                message="Banned phrase detected.",
            ),
        ),
    )
    check = evaluate_policy_qc(report)
    assert check.name == POLICY_QC_CHECK_NAME
    assert check.status is QCStatus.FAIL
    assert "policy.banned_phrase" in (check.detail or "")


def test_evaluate_policy_qc_warns_on_warn_report() -> None:
    report = PolicyReport(
        content_item_id="item-1",
        channel_id="ch-1",
        generated_at=NOW,
        rule_set=POLICY_RULES,
        status=PolicyStatus.WARN,
        blocks=False,
        outcomes=(
            RuleOutcomeReport(
                rule_id="policy.warn_rule",
                version=1,
                status="warn",
                code="policy.warn_rule.warn",
                message="Non-blocking warning.",
                field="description",
                blocking=False,
            ),
        ),
        findings=(
            FindingReport(
                rule_id="policy.warn_rule",
                rule_version=1,
                blocking=False,
                message="Non-blocking warning.",
            ),
        ),
        blocking_findings=(),
    )
    check = evaluate_policy_qc(report)
    assert check.status is QCStatus.WARN
    assert "policy.warn_rule" in (check.detail or "")


def test_policy_qc_checker_end_to_end(database: Database) -> None:
    channel = make_channel()
    strategy = make_strategy_profile(
        channel,
        brand=Brand(
            "Money Minute",
            "calm and clear",
            banned_phrases=("forbidden_word",),
        ),
    )
    clean_item = make_content_item(
        channel,
        strategy,
        title="Clean educational guide",
    )
    blocked_item = make_content_item(
        channel,
        strategy,
        title="Contains forbidden_word in title",
    )
    with database.transaction() as conn:
        ChannelRepository(conn).add(channel)
        StrategyProfileRepository(conn).add(strategy)
        ContentItemRepository(conn).add(clean_item)
        ContentItemRepository(conn).add(blocked_item)

    checker = PolicyQCChecker(database, clock=lambda: NOW)
    clean_eval = checker.evaluate(clean_item.id)
    assert not clean_eval.is_blocking
    assert clean_eval.check.status is QCStatus.PASS

    blocked_eval = checker.evaluate(blocked_item.id)
    assert blocked_eval.is_blocking
    assert blocked_eval.check.status is QCStatus.FAIL
