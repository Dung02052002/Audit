import dataclasses
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from ai_youtube_agent.content.strategy import (
    Audience,
    Brand,
    Budget,
    Cadence,
    FormatSettings,
    LanguageSettings,
    LongFormFormat,
    Market,
    Monetization,
    Niche,
    Pillar,
    ShortsFormat,
    StrategyChangeNotAllowedError,
    StrategyProfile,
)
from ai_youtube_agent.core.audit import Actor, ActorKind
from ai_youtube_agent.core.errors import DomainError

T0 = datetime(2026, 9, 29, 10, 0, tzinfo=UTC)
T1 = T0 + timedelta(minutes=5)
USER = Actor(ActorKind.USER, "owner-1")
OTHER_USER = Actor(ActorKind.USER, "owner-2")
AI = Actor(ActorKind.AI, "script-agent")
SYSTEM = Actor(ActorKind.SYSTEM, "scheduler")

SETTINGS = {
    "market": Market("VN"),
    "languages": LanguageSettings("vi", ("en-US",)),
    "audience": Audience("Adults interested in personal finance"),
    "niche": Niche("Personal finance", (Pillar("budgeting"), Pillar("investing"))),
    "brand": Brand("Money Minute", "calm and clear"),
    "format": FormatSettings(ShortsFormat(15, 60), LongFormFormat(480, 900)),
    "cadence": Cadence(shorts_per_day=2, longform_per_day=0),
    "budget": Budget("USD", Decimal("5.00"), Decimal("100.00")),
    "monetization": Monetization(("ads", "affiliate")),
}

WEEK = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


def at(moment: datetime):
    return lambda: moment


def new_profile(**overrides) -> StrategyProfile:
    settings = {**SETTINGS, **overrides}
    return StrategyProfile.create("channel-1", **settings, actor=USER, clock=at(T0))


# Market, languages and currency codes


@pytest.mark.parametrize("country", ["US", "VN", "JP"])
def test_market_accepts_iso_country_codes(country: str) -> None:
    assert Market(country).country == country


@pytest.mark.parametrize("country", ["", "us", "USA", "U", "1A"])
def test_market_rejects_malformed_country_codes(country: str) -> None:
    with pytest.raises(ValueError):
        Market(country)


@pytest.mark.parametrize("tag", ["en", "vi", "fil", "en-US", "zh-Hant-TW"])
def test_languages_accept_bcp47_tags(tag: str) -> None:
    assert LanguageSettings(tag).primary == tag


@pytest.mark.parametrize("tag", ["", "EN", "e", "english-", "en_US", "en-"])
def test_languages_reject_malformed_tags(tag: str) -> None:
    with pytest.raises(ValueError):
        LanguageSettings(tag)


def test_secondary_languages_are_checked_too() -> None:
    with pytest.raises(ValueError):
        LanguageSettings("en", ("vi", "bad tag"))


@pytest.mark.parametrize("secondary", [("en",), ("vi", "vi"), ("en-us", "en-US")])
def test_languages_must_not_repeat(secondary: tuple[str, ...]) -> None:
    with pytest.raises(ValueError):
        LanguageSettings(secondary[0] if len(secondary) == 1 else "fr", secondary)


def test_secondary_languages_are_optional() -> None:
    assert LanguageSettings("en").secondary == ()


@pytest.mark.parametrize("currency", ["", "usd", "US", "USDT"])
def test_budget_rejects_malformed_currency(currency: str) -> None:
    with pytest.raises(ValueError):
        Budget(currency, Decimal("1"), Decimal("10"))


# Other settings


@pytest.mark.parametrize(
    "build",
    [
        lambda: Audience("  "),
        lambda: Niche(
            "", (Pillar("budgeting"),)
        ),  # D-047: pillars are Pillar values, 1 to 10 (user decision 2026-10-01)
        lambda: Niche("Finance", (Pillar("budgeting"), Pillar(" "))),
        lambda: Niche("Finance", (Pillar("budgeting"), Pillar("Budgeting"))),
        lambda: Niche("Finance", ()),
        lambda: Brand(" "),
        lambda: Brand("Money Minute", ""),
    ],
)
def test_text_settings_reject_empty_or_repeated_values(build) -> None:
    with pytest.raises(ValueError):
        build()


def test_optional_text_settings_default_to_empty() -> None:
    assert (
        Pillar("budgeting").description is None
    )  # D-047: pillars are Pillar values, 1 to 10 (user decision 2026-10-01)
    assert Brand("Money Minute").tone is None
    assert Monetization().tracked_sources == ()


@pytest.mark.parametrize(
    ("shorts", "longform"), [(-1, 0), (0, -1), (1.5, 0), (True, 0), ("2", 0)]
)
def test_cadence_needs_whole_numbers_of_zero_or_more(shorts, longform) -> None:
    with pytest.raises(ValueError):
        Cadence(shorts_per_day=shorts, longform_per_day=longform)


def test_cadence_allows_zero() -> None:
    assert Cadence(shorts_per_day=0, longform_per_day=0).shorts_per_day == 0


@pytest.mark.parametrize(
    ("daily", "monthly"),
    [
        (Decimal("-1"), Decimal("10")),
        (Decimal("1"), Decimal("-10")),
        (Decimal("NaN"), Decimal("10")),
        (Decimal("1"), Decimal("Infinity")),
        (1.5, Decimal("10")),
        (1, Decimal("10")),
        (Decimal("11"), Decimal("10")),
    ],
)
def test_budget_limits_must_be_valid_decimals(daily, monthly) -> None:
    with pytest.raises(ValueError):
        Budget("USD", daily, monthly)


def test_budget_allows_zero_and_equal_limits() -> None:
    assert Budget("USD", Decimal("0"), Decimal("0")).daily_limit == 0
    assert Budget("EUR", Decimal("10"), Decimal("10")).currency == "EUR"


@pytest.mark.parametrize(
    "sources", [("",), ("Ads",), ("ad revenue",), ("1ads",), ("ads", "ads")]
)
def test_monetization_rejects_malformed_or_repeated_sources(sources) -> None:
    with pytest.raises(ValueError):
        Monetization(sources)


# Creating a profile


def test_create_builds_a_first_version_owned_by_the_user() -> None:
    profile = new_profile()

    assert len(profile.id) == 32
    assert profile.channel_id == "channel-1"
    assert profile.version == 1
    assert profile.updated_by == USER
    assert profile.created_at == profile.updated_at == T0
    for name, value in SETTINGS.items():
        assert getattr(profile, name) == value


def test_create_gives_each_profile_its_own_id() -> None:
    assert new_profile().id != new_profile().id


def test_create_defaults_to_the_current_utc_time() -> None:
    before = datetime.now(UTC)
    profile = StrategyProfile.create("channel-1", **SETTINGS, actor=USER)
    assert before <= profile.created_at <= datetime.now(UTC)
    assert profile.created_at.utcoffset() == timedelta(0)


@pytest.mark.parametrize("actor", [AI, SYSTEM])
def test_only_a_user_can_create_a_profile(actor: Actor) -> None:
    with pytest.raises(StrategyChangeNotAllowedError):
        StrategyProfile.create("channel-1", **SETTINGS, actor=actor, clock=at(T0))


def test_refusal_is_a_domain_error_with_a_safe_message() -> None:
    with pytest.raises(DomainError) as info:
        StrategyProfile.create("channel-1", **SETTINGS, actor=AI, clock=at(T0))

    assert info.value.code == "domain.strategy_change_not_allowed"
    assert info.value.user_message == "Only a user can change the channel strategy."


def test_profile_needs_a_channel() -> None:
    with pytest.raises(ValueError):
        StrategyProfile.create("", **SETTINGS, actor=USER, clock=at(T0))


def test_a_setting_may_be_left_unconfigured() -> None:
    # Changed by D-044 (user decision, 2026-10-01): a strategy is configured
    # one section at a time, so a missing setting is allowed and reported.
    settings = dict(SETTINGS)
    del settings["budget"]

    profile = StrategyProfile.create("channel-1", **settings, actor=USER)

    assert profile.budget is None
    assert profile.missing_settings == ("budget",)
    assert not profile.is_complete


def test_settings_must_use_their_value_types() -> None:
    with pytest.raises(TypeError):
        new_profile(market="VN")


@pytest.mark.parametrize(
    "changes",
    [
        {"id": ""},
        {"version": 0},
        {"updated_by": AI},
        {"created_at": datetime(2026, 9, 29, 10, 0)},
        {"updated_at": T0.astimezone(timezone(timedelta(hours=7)))},
        {"updated_at": T0 - timedelta(seconds=1)},
    ],
)
def test_profile_rejects_invalid_state(changes) -> None:
    values = {
        f.name: getattr(new_profile(), f.name)
        for f in dataclasses.fields(StrategyProfile)
    }
    with pytest.raises((ValueError, DomainError)):
        StrategyProfile(**{**values, **changes})


def test_profile_is_frozen() -> None:
    profile = new_profile()
    with pytest.raises(dataclasses.FrozenInstanceError):
        profile.market = Market("US")  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        profile.budget.daily_limit = Decimal("1000")  # type: ignore[misc]


# Updating a profile


def test_update_returns_a_new_version_and_keeps_identity() -> None:
    profile = new_profile()
    budget = Budget("USD", Decimal("10"), Decimal("200"))

    updated = profile.update(
        actor=OTHER_USER, clock=at(T1), budget=budget, market=Market("US")
    )

    assert updated is not profile
    assert updated.budget == budget
    assert updated.market == Market("US")
    assert updated.version == 2
    assert updated.updated_by == OTHER_USER
    assert updated.updated_at == T1
    assert (updated.id, updated.channel_id, updated.created_at) == (
        profile.id,
        profile.channel_id,
        profile.created_at,
    )
    assert profile.version == 1
    assert profile.budget == SETTINGS["budget"]


def test_every_real_change_bumps_the_version() -> None:
    profile = new_profile()
    profile = profile.update(
        actor=USER, clock=at(T1), niche=Niche("Tech", (Pillar("AI"),))
    )
    profile = profile.update(actor=USER, clock=at(T1), brand=Brand("Tech Minute"))
    assert profile.version == 3


def test_update_without_a_change_returns_the_same_profile() -> None:
    profile = new_profile()
    assert profile.update(actor=USER, clock=at(T1)) is profile
    assert profile.update(actor=USER, clock=at(T1), market=Market("VN")) is profile


@pytest.mark.parametrize("actor", [AI, SYSTEM])
@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("market", Market("US")),
        ("languages", LanguageSettings("en")),
        ("niche", Niche("Gaming", (Pillar("Reviews"),))),
        ("cadence", Cadence(shorts_per_day=10, longform_per_day=1)),
        ("budget", Budget("USD", Decimal("50"), Decimal("1000"))),
    ],
)
def test_ai_and_system_cannot_change_strategy(actor: Actor, name, value) -> None:
    profile = new_profile()
    with pytest.raises(StrategyChangeNotAllowedError):
        profile.update(actor=actor, clock=at(T1), **{name: value})


def test_ai_is_refused_even_when_nothing_would_change() -> None:
    with pytest.raises(StrategyChangeNotAllowedError):
        new_profile().update(actor=AI, market=Market("VN"))


@pytest.mark.parametrize("name", ["id", "channel_id", "version", "created_at"])
def test_update_only_accepts_strategy_settings(name: str) -> None:
    with pytest.raises(ValueError, match="not a strategy setting"):
        new_profile().update(actor=USER, **{name: "x"})


def test_update_checks_value_types() -> None:
    with pytest.raises(TypeError):
        new_profile().update(actor=USER, clock=at(T1), market="US")


# Serialisation


def test_as_dict_is_json_friendly() -> None:
    profile = new_profile()

    assert profile.as_dict() == {
        "id": profile.id,
        "channel_id": "channel-1",
        "market": {"country": "VN"},
        "languages": {"primary": "vi", "secondary": ["en-US"]},
        # D-046 added the optional audience fields (user decision, 2026-10-01).
        "audience": {
            "description": "Adults interested in personal finance",
            "age_range": None,
            "interests": [],
            "level": None,
        },
        "niche": {
            "name": "Personal finance",
            "pillars": [
                {"name": "budgeting", "description": None},
                {"name": "investing", "description": None},
            ],
        },
        # D-048 added the tone, voice and visual rules (user decision, 2026-10-01).
        "brand": {
            "name": "Money Minute",
            "tone": "calm and clear",
            "tone_keywords": [],
            "voice_dos": [],
            "voice_donts": [],
            "banned_phrases": [],
            "visual": None,
        },
        # D-049 added the Shorts and LongForm format (user decision, 2026-10-02).
        "format": {
            "shorts": {
                "min_seconds": 15,
                "max_seconds": 60,
                "resolution": "1080p",
                "captions": True,
                "aspect_ratio": "9:16",
            },
            "longform": {
                "min_seconds": 480,
                "max_seconds": 900,
                "resolution": "1080p",
                "captions": True,
                "chapters": True,
                "aspect_ratio": "16:9",
            },
        },
        # D-050 added the time zone and publish preferences (user decision,
        # 2026-10-02); the defaults are UTC, every day, no times and no gap.
        "cadence": {
            "shorts_per_day": 2,
            "longform_per_day": 0,
            "time_zone": "UTC",
            "shorts_schedule": {"weekdays": WEEK, "times": [], "min_gap_minutes": 0},
            "longform_schedule": {
                "weekdays": WEEK,
                "times": [],
                "min_gap_minutes": 0,
            },
        },
        "budget": {
            "currency": "USD",
            "daily_limit": "5.00",
            "monthly_limit": "100.00",
            # D-051 added alert thresholds (user decision, 2026-10-02).
            "alert_thresholds": [50, 80, 100],
        },
        "monetization": {"tracked_sources": ["ads", "affiliate"]},
        "version": 1,
        "updated_by": {"kind": "user", "id": "owner-1"},
        "created_at": "2026-09-29T10:00:00+00:00",
        "updated_at": "2026-09-29T10:00:00+00:00",
    }
