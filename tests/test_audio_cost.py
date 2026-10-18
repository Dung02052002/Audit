"""H-092 Audio Cost (Prompt Pack v8, prompt #092).

Rules written for the user (build-first, 2026-10-17):

- the cost of an audio artifact is ``characters * price per 1,000 / 1,000``,
  rounded half up to 6 decimals, in the configured currency, as one ``tts``
  ``CostRecord`` whose ``ref`` is the artifact id;
- a zero price still writes a record of 0;
- the same artifact twice is not a second cost (``created=False``), even when
  the price changed;
- a channel budget in another currency refuses the cost and writes nothing;
- ``cost.recorded`` is audited after the commit with scalar data only.
"""

import dataclasses
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from ai_youtube_agent.content import audio_cost
from ai_youtube_agent.content.audio_artifact import (
    AudioArtifactRecord,
    AudioArtifactRecorder,
)
from ai_youtube_agent.content.audio_cost import (
    ACTOR,
    RECORDED,
    AudioCostError,
    AudioCostErrorCode,
    AudioCostRecord,
    AudioCostRecorder,
)
from ai_youtube_agent.content.cost import CostCategory
from ai_youtube_agent.content.script import Script, ScriptSection, SectionKind
from ai_youtube_agent.content.strategy import Budget
from ai_youtube_agent.content.tts_job import InMemoryChunkStore, TtsJobRunner, TtsResult
from ai_youtube_agent.core.artifact import ArtifactKind
from ai_youtube_agent.core.artifact_storage import LocalFileStorage
from ai_youtube_agent.core.audit import (
    Actor,
    ActorKind,
    AuditLog,
    AuditResult,
    InMemoryAuditSink,
)
from ai_youtube_agent.core.budget_gate import BudgetGate
from ai_youtube_agent.core.content_item import ContentStatus
from ai_youtube_agent.core.db.database import Database
from ai_youtube_agent.core.db.repositories.channel import (
    ChannelRepository,
    StrategyProfileRepository,
    VoiceProfileRepository,
)
from ai_youtube_agent.core.db.repositories.content import (
    ContentItemRepository,
    ScriptRepository,
)
from ai_youtube_agent.core.db.repositories.economics import CostRecordRepository
from ai_youtube_agent.core.errors import DomainError
from ai_youtube_agent.core.gates import GateContext, GateOutcome
from ai_youtube_agent.providers.mock_speech_synthesis import MockSpeechSynthesizer
from factories import (
    T0,
    at,
    make_artifact,
    make_channel,
    make_content_item,
    make_strategy_profile,
    make_voice_profile,
)

T1 = T0 + timedelta(hours=1)
D = Decimal
TEXTS = ("Opening line of the audio.", "A second part.", "The last part.")
SYSTEM = Actor(ActorKind.SYSTEM, "pipeline")
NO_BUDGET = object()


class Ticker:
    """A clock that moves 1 ms forward on every call."""

    def __init__(self) -> None:
        self.now = T0

    def __call__(self):
        self.now += timedelta(milliseconds=1)
        return self.now


class World:
    def __init__(
        self, database: Database, tmp_path: Path, budget: object = None
    ) -> None:
        self.database = database
        self.channel = make_channel()
        self.strategy = make_strategy_profile(self.channel)
        if budget is NO_BUDGET:
            self.strategy = dataclasses.replace(self.strategy, budget=None)
        elif budget is not None:
            self.strategy = dataclasses.replace(self.strategy, budget=budget)
        self.item = make_content_item(self.channel, self.strategy)
        self.voice = make_voice_profile(self.channel)
        with database.transaction() as connection:
            ChannelRepository(connection).add(self.channel)
            StrategyProfileRepository(connection).add(self.strategy)
            ContentItemRepository(connection).add(self.item)
            VoiceProfileRepository(connection).add(self.voice)
        self.last_script: Script | None = None
        self.clock = Ticker()
        self.mock = MockSpeechSynthesizer(name="mock_tts", clock=self.clock)
        self.store = InMemoryChunkStore()
        self.storage = LocalFileStorage(tmp_path / "artifacts")
        self.sink = InMemoryAuditSink()
        self.audit = AuditLog(self.sink, clock=at(T1))

    def cost_recorder(
        self, price: str = "0.015", currency: str = "USD"
    ) -> AudioCostRecorder:
        return AudioCostRecorder(
            self.database,
            self.audit,
            price_per_1k=D(price),
            currency=currency,
            clock=at(T1),
        )

    def script(self, *texts: str) -> Script:
        sections = tuple(
            ScriptSection(SectionKind.BODY, text) for text in texts or TEXTS
        )
        if self.last_script is None:
            script = Script.create(self.item.id, sections=sections, clock=self.clock)
        else:
            script = self.last_script.next_version(
                sections=sections, clock=self.clock
            )
        self.last_script = script
        with self.database.transaction() as connection:
            ScriptRepository(connection).add(script)
        return script

    def run(self, script: Script) -> TtsResult:
        runner = TtsJobRunner(
            self.database,
            self.mock,
            self.store,
            clock=self.clock,
            sleep=lambda seconds: None,
        )
        return runner.run(script, self.voice)

    def stored_audio(self, *texts: str) -> tuple[TtsResult, AudioArtifactRecord]:
        """Voice a new script and store the audio, without recording a cost."""
        script = self.script(*texts)
        result = self.run(script)
        recorder = AudioArtifactRecorder(
            self.database, self.storage, AuditLog(InMemoryAuditSink()), clock=at(T1)
        )
        return result, recorder.record(result, script, self.voice)

    def costs(self):
        with self.database.transaction() as connection:
            return CostRecordRepository(connection).list_by_channel(self.channel.id)

    def events(self):
        return self.sink.events()


@pytest.fixture
def world(database: Database, tmp_path: Path) -> World:
    return World(database, tmp_path)


def with_characters(result: TtsResult, characters: int) -> TtsResult:
    return dataclasses.replace(result, characters=characters)


# The amount


def test_1500_characters_at_0_015_cost_0_0225(world: World) -> None:
    result, stored = world.stored_audio()

    recorded = world.cost_recorder("0.015").record(
        stored, with_characters(result, 1500), world.voice
    )

    assert isinstance(recorded, AudioCostRecord)
    assert recorded.created is True
    assert recorded.cost.amount == D("0.022500")
    assert str(recorded.cost.amount) == "0.022500"


def test_the_real_character_count_of_the_job_is_billed(world: World) -> None:
    result, stored = world.stored_audio()

    recorded = world.cost_recorder("2").record(stored, result, world.voice)

    assert result.characters > 0
    assert recorded.cost.amount == (D(result.characters) * 2 / 1000).quantize(
        D("0.000001")
    )


def test_the_amount_rounds_half_up_to_six_decimals(world: World) -> None:
    result, stored = world.stored_audio()

    # 1 * 0.0005 / 1000 = 0.0000005: half up gives 0.000001 (half even would not).
    recorded = world.cost_recorder("0.0005").record(
        stored, with_characters(result, 1), world.voice
    )

    assert recorded.cost.amount == D("0.000001")


def test_a_value_just_under_half_rounds_down(world: World) -> None:
    result, stored = world.stored_audio()

    recorded = world.cost_recorder("0.0004").record(
        stored, with_characters(result, 1), world.voice
    )

    assert recorded.cost.amount == D("0.000000")


def test_a_zero_price_still_writes_a_record_of_zero(world: World) -> None:
    result, stored = world.stored_audio()

    recorded = world.cost_recorder("0").record(stored, result, world.voice)

    assert recorded.created is True
    assert recorded.cost.amount == 0
    assert world.costs() == [recorded.cost]
    assert [e.action for e in world.events()] == [RECORDED]


# The record


def test_the_cost_has_the_channel_item_ref_provider_and_clock_time(
    world: World,
) -> None:
    result, stored = world.stored_audio()

    cost = world.cost_recorder().record(stored, result, world.voice).cost

    assert cost.channel_id == world.channel.id
    assert cost.content_item_id == world.item.id
    assert cost.ref == stored.artifact.id
    assert cost.category is CostCategory.TTS
    assert cost.provider == "mock_tts" == world.voice.provider
    assert cost.currency == "USD"
    assert cost.incurred_at == T1
    assert world.costs() == [cost]


def test_the_cost_is_counted_by_the_channel_list(world: World) -> None:
    result, stored = world.stored_audio()
    cost = world.cost_recorder().record(stored, result, world.voice).cost

    with world.database.transaction() as connection:
        listed = CostRecordRepository(connection).list_by_channel(
            world.channel.id, start=T1, end=T1 + timedelta(seconds=1)
        )

    assert listed == [cost]


def test_the_cost_counts_in_the_budget_gate_spending(world: World) -> None:
    result, stored = world.stored_audio()
    # 1,000 characters at 10 per 1,000 is 10, over the daily budget of 5.00.
    world.cost_recorder("10").record(
        stored, with_characters(result, 1000), world.voice
    )

    with world.database.transaction() as connection:
        gate = BudgetGate(
            StrategyProfileRepository(connection), CostRecordRepository(connection)
        )
        verdict = gate.evaluate(
            GateContext(world.item, ContentStatus.GENERATING, SYSTEM, T1)
        )

    assert verdict.outcome is GateOutcome.BLOCK
    assert [r.code for r in verdict.reasons] == ["budget.daily_exceeded"]


# One cost per artifact


def test_the_same_artifact_twice_is_one_cost_and_one_event(world: World) -> None:
    result, stored = world.stored_audio()
    recorder = world.cost_recorder()

    first = recorder.record(stored, result, world.voice)
    second = recorder.record(stored, result, world.voice)

    assert first.created is True
    assert second.created is False
    assert second.cost == first.cost
    assert world.costs() == [first.cost]
    assert [e.action for e in world.events()] == [RECORDED]


def test_a_replay_keeps_the_amount_when_the_price_changed(world: World) -> None:
    result, stored = world.stored_audio()
    first = world.cost_recorder("0.015").record(stored, result, world.voice)

    replay = world.cost_recorder("9").record(stored, result, world.voice)

    assert replay.created is False
    assert replay.cost == first.cost
    assert world.costs() == [first.cost]
    assert len(world.events()) == 1


def test_a_replay_writes_the_cost_missing_after_a_crash(world: World) -> None:
    result, stored = world.stored_audio()
    assert world.costs() == []

    recorded = world.cost_recorder().record(stored, result, world.voice)

    assert recorded.created is True
    assert world.costs() == [recorded.cost]


def test_a_new_artifact_version_gets_its_own_cost(world: World) -> None:
    first_result, first_stored = world.stored_audio()
    second_result, second_stored = world.stored_audio("A different script.")
    recorder = world.cost_recorder()

    first = recorder.record(first_stored, first_result, world.voice)
    second = recorder.record(second_stored, second_result, world.voice)

    assert first_stored.artifact.id != second_stored.artifact.id
    assert second_stored.artifact.version == 2
    assert first.created is second.created is True
    assert first.cost.ref == first_stored.artifact.id
    assert second.cost.ref == second_stored.artifact.id
    assert {c.id for c in world.costs()} == {first.cost.id, second.cost.id}
    assert [e.action for e in world.events()] == [RECORDED, RECORDED]


# The audit event


def test_one_system_event_with_scalar_metadata(world: World) -> None:
    result, stored = world.stored_audio()

    cost = world.cost_recorder("0.015").record(stored, result, world.voice).cost

    (event,) = world.events()
    assert event.action == RECORDED == "cost.recorded"
    assert event.actor == ACTOR
    assert event.actor.kind is ActorKind.SYSTEM
    assert event.actor.id == "audio_cost"
    assert (event.entity.type, event.entity.id) == ("cost_record", cost.id)
    assert event.result is AuditResult.SUCCESS
    assert dict(event.metadata) == {
        "artifact_id": stored.artifact.id,
        "content_item_id": world.item.id,
        "channel_id": world.channel.id,
        "characters": result.characters,
        "amount": str(cost.amount),
        "currency": "USD",
        "provider": "mock_tts",
    }
    assert all(isinstance(v, str | int) for v in event.metadata.values())
    assert not any(text in str(event.metadata) for text in TEXTS)


def test_a_failed_insert_writes_and_audits_nothing(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    result, stored = world.stored_audio()

    def boom(self, record):
        raise RuntimeError("disk full")

    monkeypatch.setattr(audio_cost.CostRecordRepository, "add", boom)

    with pytest.raises(RuntimeError, match="disk full"):
        world.cost_recorder().record(stored, result, world.voice)

    assert world.costs() == []
    assert world.events() == ()


# Refusals


def test_a_budget_in_another_currency_refuses_and_writes_nothing(
    database: Database, tmp_path: Path
) -> None:
    world = World(database, tmp_path, Budget("EUR", D("5.00"), D("100.00")))
    result, stored = world.stored_audio()

    with pytest.raises(AudioCostError) as caught:
        world.cost_recorder("0", "USD").record(stored, result, world.voice)

    assert caught.value.code == "domain.audio_cost_currency_mismatch"
    assert caught.value.code == AudioCostErrorCode.CURRENCY_MISMATCH.value
    assert isinstance(caught.value, DomainError)
    assert world.costs() == []
    assert world.events() == ()


def test_a_budget_in_the_same_currency_records(
    database: Database, tmp_path: Path
) -> None:
    world = World(database, tmp_path, Budget("EUR", D("5.00"), D("100.00")))
    result, stored = world.stored_audio()

    recorded = world.cost_recorder("0.015", "EUR").record(stored, result, world.voice)

    assert recorded.created is True
    assert recorded.cost.currency == "EUR"


def test_a_channel_without_a_budget_records_normally(
    database: Database, tmp_path: Path
) -> None:
    world = World(database, tmp_path, NO_BUDGET)
    result, stored = world.stored_audio()

    recorded = world.cost_recorder("0.015", "EUR").record(stored, result, world.voice)

    assert recorded.created is True
    assert world.costs() == [recorded.cost]


def test_a_voice_profile_of_another_channel_is_refused(world: World) -> None:
    result, stored = world.stored_audio()
    other_voice = make_voice_profile(make_channel())

    with pytest.raises(AudioCostError) as caught:
        world.cost_recorder().record(stored, result, other_voice)

    assert caught.value.code == AudioCostErrorCode.CHANNEL_MISMATCH.value
    assert world.costs() == []
    assert world.events() == ()


def test_an_artifact_of_a_missing_item_is_refused(world: World) -> None:
    result, stored = world.stored_audio()
    orphan = dataclasses.replace(stored.artifact, content_item_id="missing-item")

    with pytest.raises(AudioCostError) as caught:
        world.cost_recorder().record(
            dataclasses.replace(stored, artifact=orphan), result, world.voice
        )

    assert caught.value.code == AudioCostErrorCode.ITEM_NOT_FOUND.value
    assert world.costs() == []


def test_a_non_audio_artifact_is_refused(world: World) -> None:
    result, stored = world.stored_audio()
    video = make_artifact(world.item, ArtifactKind.VIDEO)

    with pytest.raises(AudioCostError) as caught:
        world.cost_recorder().record(
            dataclasses.replace(stored, artifact=video), result, world.voice
        )

    assert caught.value.code == AudioCostErrorCode.NOT_AUDIO.value
    assert world.costs() == []
    assert world.events() == ()


# The constructor


@pytest.mark.parametrize(
    "price",
    [D("-0.01"), D("NaN"), D("Infinity"), D("-Infinity"), 0.015, 1, "0.01", True, None],
)
def test_the_constructor_refuses_a_bad_price(world: World, price) -> None:
    with pytest.raises(ValueError, match="price_per_1k"):
        AudioCostRecorder(
            world.database, world.audit, price_per_1k=price, currency="USD"
        )


@pytest.mark.parametrize("currency", ["usd", "US", "USDD", "", "U1D", None, 1])
def test_the_constructor_refuses_a_bad_currency(world: World, currency) -> None:
    with pytest.raises(ValueError, match="currency"):
        AudioCostRecorder(
            world.database, world.audit, price_per_1k=D("0.015"), currency=currency
        )


def test_the_error_codes_and_default() -> None:
    assert AudioCostError().code == "domain.audio_cost"
    assert {code.value for code in AudioCostErrorCode} == {
        "domain.audio_cost",
        "domain.audio_cost_not_audio",
        "domain.audio_cost_channel_mismatch",
        "domain.audio_cost_item_not_found",
        "domain.audio_cost_currency_mismatch",
    }
    assert set(audio_cost.USER_MESSAGES) == set(AudioCostErrorCode)


# End to end


def test_job_artifact_and_cost_end_to_end(world: World) -> None:
    script = world.script()
    result = world.run(script)
    artifacts = AudioArtifactRecorder(
        world.database, world.storage, world.audit, clock=at(T1)
    )
    costs = world.cost_recorder("0.015")

    stored = artifacts.record(result, script, world.voice)
    recorded = costs.record(stored, result, world.voice)
    replay_stored = artifacts.record(result, script, world.voice)
    replay = costs.record(replay_stored, result, world.voice)

    assert stored.created is True
    assert replay_stored.created is False
    assert recorded.created is True
    assert replay.created is False
    assert replay.cost == recorded.cost
    assert recorded.cost.ref == stored.artifact.id
    assert recorded.cost.amount == (D(result.characters) * D("0.015") / 1000).quantize(
        D("0.000001")
    )
    assert world.costs() == [recorded.cost]
    assert [e.action for e in world.events()].count(RECORDED) == 1
