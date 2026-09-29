import dataclasses
import inspect
from datetime import UTC, datetime, timedelta, timezone

import pytest

from ai_youtube_agent.content import experiment as experiment_module
from ai_youtube_agent.content.experiment import (
    Experiment,
    ExperimentConclusion,
    ExperimentNotAllowedError,
    ExperimentStateError,
    ExperimentStatus,
    ExperimentType,
    ExperimentVariant,
)
from ai_youtube_agent.core.audit import Actor, ActorKind
from ai_youtube_agent.core.errors import DomainError

T0 = datetime(2026, 9, 29, 10, 0, tzinfo=UTC)
T1 = T0 + timedelta(hours=1)
T2 = T1 + timedelta(days=3)
USER = Actor(ActorKind.USER, "owner-1")
AI = Actor(ActorKind.AI, "growth-agent")
SYSTEM = Actor(ActorKind.SYSTEM, "scheduler")
VARIANTS = (
    ExperimentVariant("a", "5 bank fees you never noticed"),
    ExperimentVariant("b", "Your bank is charging you for this"),
)


def at(moment: datetime):
    return lambda: moment


def rebuild(entity, **changes):
    values = {f.name: getattr(entity, f.name) for f in dataclasses.fields(entity)}
    return type(entity)(**{**values, **changes})


def proposed(**overrides) -> Experiment:
    values = {
        "hypothesis": "  A direct 'you' title gets a higher CTR.  ",
        "variants": VARIANTS,
        "proposed_by": AI,
        "content_item_id": "item-1",
        "clock": at(T0),
    }
    return Experiment.propose(
        "channel-1", ExperimentType.TITLE, **{**values, **overrides}
    )


def running() -> Experiment:
    return proposed().start(actor=USER, clock=at(T1))


def concluded() -> Experiment:
    return running().conclude(
        actor=USER,
        winner_key="b",
        note=" b had 1.4x CTR ",
        metric_snapshot_ids=["snap-a", "snap-b"],
        clock=at(T2),
    )


# Values


def test_types_and_statuses() -> None:
    assert [t.value for t in ExperimentType] == ["title", "thumbnail", "content"]
    assert [s.value for s in ExperimentStatus] == [
        "proposed",
        "running",
        "concluded",
        "cancelled",
    ]


@pytest.mark.parametrize(
    ("key", "value"), [("", "x"), ("A", "x"), ("a b", "x"), ("-a", "x"), ("a", " ")]
)
def test_variant_rejects_bad_values(key: str, value: str) -> None:
    with pytest.raises(ValueError):
        ExperimentVariant(key, value)


# Proposing


@pytest.mark.parametrize("actor", [USER, AI, SYSTEM])
def test_any_actor_may_propose(actor: Actor) -> None:
    experiment = proposed(proposed_by=actor)

    assert len(experiment.id) == 32
    assert experiment.channel_id == "channel-1"
    assert experiment.content_item_id == "item-1"
    assert experiment.type is ExperimentType.TITLE
    assert experiment.hypothesis == "A direct 'you' title gets a higher CTR."
    assert experiment.variants == VARIANTS
    assert experiment.variant_keys == ("a", "b")
    assert experiment.status is ExperimentStatus.PROPOSED
    assert experiment.proposed_by == actor
    assert experiment.started_by is None
    assert experiment.conclusion is None
    assert experiment.created_at == experiment.updated_at == T0


@pytest.mark.parametrize("type_", list(ExperimentType))
def test_each_type_can_be_proposed(type_: ExperimentType) -> None:
    experiment = Experiment.propose(
        "channel-1",
        type_,
        hypothesis="h",
        variants=iter(VARIANTS),
        proposed_by=USER,
    )
    assert experiment.type is type_
    assert experiment.content_item_id is None


@pytest.mark.parametrize(
    "overrides",
    [
        {"hypothesis": "  "},
        {"variants": VARIANTS[:1]},
        {"variants": ()},
        {"variants": (VARIANTS[0], ExperimentVariant("a", "other"))},
        {"content_item_id": " "},
        {"clock": at(datetime(2026, 9, 29, 10, 0))},
    ],
)
def test_propose_rejects_bad_values(overrides) -> None:
    with pytest.raises(ValueError):
        proposed(**overrides)


@pytest.mark.parametrize(
    ("type_", "variants", "actor"),
    [
        ("title", VARIANTS, USER),
        (ExperimentType.TITLE, ("a", "b"), USER),
        (ExperimentType.TITLE, VARIANTS, "user"),
    ],
)
def test_propose_rejects_wrong_types(type_, variants, actor) -> None:
    with pytest.raises(TypeError):
        Experiment.propose(
            "channel-1", type_, hypothesis="h", variants=variants, proposed_by=actor
        )


# Starting and cancelling are user-only (concluding too, see below)


def test_a_user_starts_an_experiment() -> None:
    experiment = running()
    assert experiment.status is ExperimentStatus.RUNNING
    assert experiment.started_by == USER
    assert experiment.updated_at == T1


@pytest.mark.parametrize("actor", [AI, SYSTEM])
def test_ai_and_system_cannot_start(actor: Actor) -> None:
    with pytest.raises(ExperimentNotAllowedError) as info:
        proposed().start(actor=actor)
    assert isinstance(info.value, DomainError)
    assert info.value.code == "domain.experiment_not_allowed"


@pytest.mark.parametrize("experiment", [proposed, running], ids=["proposed", "running"])
def test_a_user_cancels_an_unfinished_experiment(experiment) -> None:
    cancelled = experiment().cancel(actor=USER, clock=at(T2))
    assert cancelled.status is ExperimentStatus.CANCELLED
    assert cancelled.cancelled_by == USER
    assert cancelled.is_final


@pytest.mark.parametrize("actor", [AI, SYSTEM])
def test_ai_and_system_cannot_cancel(actor: Actor) -> None:
    with pytest.raises(ExperimentNotAllowedError):
        running().cancel(actor=actor)


# Concluding is user-only, records a result and never applies it


def test_conclude_records_the_result() -> None:
    experiment = concluded()

    assert experiment.status is ExperimentStatus.CONCLUDED
    assert experiment.is_final
    assert experiment.conclusion == ExperimentConclusion(
        winner_key="b",
        note="b had 1.4x CTR",
        metric_snapshot_ids=("snap-a", "snap-b"),
        concluded_by=USER,
        concluded_at=T2,
    )
    assert not experiment.conclusion.is_inconclusive
    assert experiment.conclusion.concluded_at.utcoffset() == timedelta(0)
    assert experiment.updated_at == T2


@pytest.mark.parametrize("actor", [AI, SYSTEM])
def test_ai_and_system_cannot_conclude(actor: Actor) -> None:
    experiment = running()

    with pytest.raises(ExperimentNotAllowedError) as info:
        experiment.conclude(actor=actor, winner_key="b", clock=at(T2))

    assert isinstance(info.value, DomainError)
    assert info.value.code == "domain.experiment_not_allowed"
    assert experiment.status is ExperimentStatus.RUNNING
    assert experiment.conclusion is None


def test_ai_is_refused_before_the_state_check() -> None:
    with pytest.raises(ExperimentNotAllowedError):
        concluded().conclude(actor=AI, winner_key="a")


@pytest.mark.parametrize("actor", [AI, SYSTEM])
def test_a_stored_conclusion_needs_a_user(actor: Actor) -> None:
    with pytest.raises(ExperimentNotAllowedError):
        ExperimentConclusion("b", None, (), actor, T2)


def test_a_stored_conclusion_needs_an_actor() -> None:
    with pytest.raises(ExperimentNotAllowedError):
        ExperimentConclusion("b", None, (), "owner-1", T2)  # type: ignore[arg-type]


def test_a_stored_conclusion_needs_utc_time() -> None:
    with pytest.raises(ValueError):
        ExperimentConclusion("b", None, (), USER, datetime(2026, 10, 2, 11))


def test_an_experiment_can_be_inconclusive() -> None:
    experiment = running().conclude(actor=USER, winner_key=None, clock=at(T2))
    assert experiment.conclusion.is_inconclusive
    assert experiment.conclusion.metric_snapshot_ids == ()


def test_winner_must_be_one_of_the_variants() -> None:
    with pytest.raises(ValueError, match="not one of the variants"):
        running().conclude(actor=USER, winner_key="c", clock=at(T2))


@pytest.mark.parametrize(
    "kwargs",
    [{"note": " "}, {"metric_snapshot_ids": ["s", "s"]}, {"metric_snapshot_ids": [""]}],
)
def test_conclude_rejects_bad_evidence(kwargs) -> None:
    with pytest.raises(ValueError):
        running().conclude(actor=USER, winner_key="a", clock=at(T2), **kwargs)


def test_concluding_changes_no_variant_or_strategy() -> None:
    experiment = concluded()
    assert experiment.variants == VARIANTS
    assert experiment.type is ExperimentType.TITLE


def test_the_registry_cannot_apply_a_winner() -> None:
    names = {"apply", "apply_winner", "promote", "adopt", "rollout"}
    assert not names & set(dir(Experiment))
    source = inspect.getsource(experiment_module)
    assert "content.strategy" not in source
    assert "StrategyProfile(" not in source


# Refused actions


@pytest.mark.parametrize(
    ("experiment", "action"),
    [
        (proposed, lambda e: e.conclude(actor=USER, winner_key="a")),
        (running, lambda e: e.start(actor=USER)),
        (concluded, lambda e: e.start(actor=USER)),
        (concluded, lambda e: e.conclude(actor=USER, winner_key="a")),
        (concluded, lambda e: e.cancel(actor=USER)),
        (
            lambda: proposed().cancel(actor=USER, clock=at(T1)),
            lambda e: e.start(actor=USER),
        ),
        (
            lambda: proposed().cancel(actor=USER, clock=at(T1)),
            lambda e: e.cancel(actor=USER),
        ),
    ],
)
def test_actions_in_the_wrong_state_are_refused(experiment, action) -> None:
    with pytest.raises(ExperimentStateError) as info:
        action(experiment())
    assert info.value.code == "domain.experiment_state"


# Stored state


@pytest.mark.parametrize(
    "changes",
    [
        {"id": ""},
        {"status": "proposed"},
        {"started_by": USER},
        {"cancelled_by": USER},
        {"status": ExperimentStatus.RUNNING},
        {"status": ExperimentStatus.CANCELLED},
        {"updated_at": T0 - timedelta(seconds=1)},
        {"created_at": T0.astimezone(timezone(timedelta(hours=7)))},
    ],
)
def test_proposed_experiment_rejects_invalid_state(changes) -> None:
    with pytest.raises((ValueError, TypeError)):
        rebuild(proposed(), **changes)


@pytest.mark.parametrize(
    "changes",
    [
        {"conclusion": None},
        {"started_by": None},
        {"started_by": AI},
        {"updated_at": T1},
    ],
)
def test_concluded_experiment_rejects_invalid_state(changes) -> None:
    with pytest.raises((ValueError, DomainError)):
        rebuild(concluded(), **changes)


def test_running_experiment_has_no_conclusion() -> None:
    with pytest.raises(ValueError):
        rebuild(running(), conclusion=concluded().conclusion)


def test_cancelled_by_must_be_a_user() -> None:
    cancelled = proposed().cancel(actor=USER, clock=at(T1))
    with pytest.raises(ExperimentNotAllowedError):
        rebuild(cancelled, cancelled_by=AI)


def test_experiment_is_frozen() -> None:
    with pytest.raises(dataclasses.FrozenInstanceError):
        proposed().status = ExperimentStatus.RUNNING  # type: ignore[misc]


# Serialisation


def test_as_dict_is_json_friendly() -> None:
    experiment = concluded()

    assert experiment.as_dict() == {
        "id": experiment.id,
        "channel_id": "channel-1",
        "content_item_id": "item-1",
        "type": "title",
        "hypothesis": "A direct 'you' title gets a higher CTR.",
        "variants": [
            {"key": "a", "value": "5 bank fees you never noticed"},
            {"key": "b", "value": "Your bank is charging you for this"},
        ],
        "status": "concluded",
        "proposed_by": {"kind": "ai", "id": "growth-agent"},
        "started_by": {"kind": "user", "id": "owner-1"},
        "cancelled_by": None,
        "conclusion": {
            "winner_key": "b",
            "note": "b had 1.4x CTR",
            "metric_snapshot_ids": ["snap-a", "snap-b"],
            "concluded_by": {"kind": "user", "id": "owner-1"},
            "concluded_at": "2026-10-02T11:00:00+00:00",
        },
        "created_at": "2026-09-29T10:00:00+00:00",
        "updated_at": "2026-10-02T11:00:00+00:00",
    }
