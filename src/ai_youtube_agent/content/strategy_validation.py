"""Strategy validation (Prompt Pack v8, prompt #053), context C1.

``validate_strategy`` checks a channel's strategy for missing or incompatible
configuration before a run starts. The rules were approved by the user on
2026-10-02:

- A run is a move into generating (a new production or a regeneration).
  ``StrategyGate`` (``core/strategy_gate.py``) blocks it on any blocking
  finding, and ``GET /channels/{id}/strategy/validation`` shows every finding
  beforehand.
- Missing configuration and conflicts that make a run impossible block;
  other conflicts are warnings, which never block.
- Blocking findings: no strategy (``strategy.missing``); each unset setting
  (``strategy.missing_setting``); the item's content type switched off by its
  feature flag (``strategy.content_type_disabled``); a daily limit of 0 for
  the item's content type (``strategy.no_daily_limit``); a daily or monthly
  budget of 0 (``strategy.zero_budget``).
- Warnings: the primary language has a region other than the market
  (``strategy.language_region_mismatch``, for example ``en-GB`` with market
  ``US``); the monetization currency differs from the budget currency
  (``strategy.currency_mismatch``); a daily limit above 0 for a content type
  whose flag is off (``strategy.limit_for_disabled_type``); every daily limit
  is 0 (``strategy.nothing_can_run``); the item was created from an older
  strategy version (``strategy.version_changed``).

Validation only reads. It never changes a setting (R-09). Findings are in a
fixed order: missing configuration first, then the rules above in order.
"""

from dataclasses import dataclass
from enum import StrEnum

from ai_youtube_agent.content.strategy import StrategyProfile
from ai_youtube_agent.core.content_item import ContentType
from ai_youtube_agent.core.flags import FeatureFlags

TYPE_LABELS = {ContentType.SHORTS: "Shorts", ContentType.LONGFORM: "LongForm"}


class FindingSeverity(StrEnum):
    BLOCKING = "blocking"
    WARNING = "warning"


@dataclass(frozen=True)
class StrategyFinding:
    """One problem found. ``message`` is shown to users, so keep it safe."""

    code: str
    severity: FindingSeverity
    message: str
    setting: str | None = None

    def as_dict(self) -> dict[str, str | None]:
        return {
            "code": self.code,
            "severity": self.severity.value,
            "message": self.message,
            "setting": self.setting,
        }


@dataclass(frozen=True)
class StrategyValidation:
    findings: tuple[StrategyFinding, ...]

    @property
    def blocking(self) -> tuple[StrategyFinding, ...]:
        return tuple(f for f in self.findings if f.severity is FindingSeverity.BLOCKING)

    @property
    def warnings(self) -> tuple[StrategyFinding, ...]:
        return tuple(f for f in self.findings if f.severity is FindingSeverity.WARNING)

    @property
    def is_valid(self) -> bool:
        return not self.blocking


def _blocking(code: str, message: str, setting: str | None = None):
    return StrategyFinding(code, FindingSeverity.BLOCKING, message, setting)


def _warning(code: str, message: str, setting: str | None = None):
    return StrategyFinding(code, FindingSeverity.WARNING, message, setting)


def _enabled(flags: FeatureFlags, content_type: ContentType) -> bool:
    if content_type is ContentType.SHORTS:
        return flags.shorts_enabled
    return flags.longform_enabled


def validate_strategy(
    strategy: StrategyProfile | None,
    flags: FeatureFlags,
    *,
    content_type: ContentType | None = None,
    item_strategy_version: int | None = None,
) -> StrategyValidation:
    """Every finding for ``strategy``, optionally for one content item.

    ``content_type`` and ``item_strategy_version`` describe the item about to
    run; without them only the channel-wide rules are checked.
    """
    if strategy is None:
        return StrategyValidation(
            (_blocking("strategy.missing", "This channel has no strategy yet."),)
        )
    findings = [
        _blocking(
            "strategy.missing_setting",
            f"The {name} setting is not configured yet.",
            name,
        )
        for name in strategy.missing_settings
    ]
    cadence, budget = strategy.cadence, strategy.budget

    if content_type is not None:
        label = TYPE_LABELS[content_type]
        if not _enabled(flags, content_type):
            findings.append(
                _blocking(
                    "strategy.content_type_disabled",
                    f"{label} production is switched off.",
                )
            )
        if cadence is not None and _limit(strategy, content_type) == 0:
            findings.append(
                _blocking(
                    "strategy.no_daily_limit",
                    f"The daily {label} limit is 0, so no {label} can be produced.",
                    "cadence",
                )
            )
    if budget is not None and (budget.daily_limit == 0 or budget.monthly_limit == 0):
        findings.append(
            _blocking(
                "strategy.zero_budget",
                "The daily or monthly budget is 0, so nothing can be produced.",
                "budget",
            )
        )

    languages, market = strategy.languages, strategy.market
    if languages is not None and market is not None:
        region = _region(languages.primary)
        if region is not None and region != market.country:
            findings.append(
                _warning(
                    "strategy.language_region_mismatch",
                    f"The primary language {languages.primary} is for region "
                    f"{region}, but the market is {market.country}.",
                    "languages",
                )
            )
    money = strategy.monetization
    if (
        money is not None
        and money.currency is not None
        and budget is not None
        and money.currency != budget.currency
    ):
        findings.append(
            _warning(
                "strategy.currency_mismatch",
                f"Revenue goals are in {money.currency} but the budget is in "
                f"{budget.currency}.",
                "monetization",
            )
        )
    if cadence is not None:
        for kind in ContentType:
            if _limit(strategy, kind) > 0 and not _enabled(flags, kind):
                label = TYPE_LABELS[kind]
                findings.append(
                    _warning(
                        "strategy.limit_for_disabled_type",
                        f"The daily {label} limit is above 0, but {label} "
                        "production is switched off.",
                        "cadence",
                    )
                )
        if cadence.shorts_per_day == 0 and cadence.longform_per_day == 0:
            findings.append(
                _warning(
                    "strategy.nothing_can_run",
                    "Every daily limit is 0, so nothing can be produced.",
                    "cadence",
                )
            )
    if item_strategy_version is not None and item_strategy_version < strategy.version:
        findings.append(
            _warning(
                "strategy.version_changed",
                f"This item was created from strategy version "
                f"{item_strategy_version}; the strategy is now at version "
                f"{strategy.version}.",
            )
        )
    return StrategyValidation(tuple(findings))


def _limit(strategy: StrategyProfile, content_type: ContentType) -> int:
    cadence = strategy.cadence
    assert cadence is not None
    if content_type is ContentType.SHORTS:
        return cadence.shorts_per_day
    return cadence.longform_per_day


def _region(tag: str) -> str | None:
    """The two-letter region subtag of a canonical BCP-47 tag, if any."""
    for subtag in tag.split("-")[1:]:
        if len(subtag) == 2 and subtag.isalpha():
            return subtag
    return None
