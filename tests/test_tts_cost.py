"""Tests for TTS voice generation cost ledger bridge (O-176, prompt #176)."""

import dataclasses
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from ai_youtube_agent.content.audio_artifact import (
    AudioArtifactRecord,
    AudioArtifactRecorder,
)
from ai_youtube_agent.content.audio_cost import AudioCostRecorder
from ai_youtube_agent.content.cost import CostCategory
from ai_youtube_agent.content.cost_ledger import CostLedger
from ai_youtube_agent.content.script import Script, ScriptSection, SectionKind
from ai_youtube_agent.content.tts_cost import (
    TTSCostError,
    TTSCostErrorCode,
    TTSCostService,
)
from ai_youtube_agent.content.tts_job import (
    InMemoryChunkStore,
    TtsJobRunner,
    TtsResult,
)
from ai_youtube_agent.core.artifact_storage import LocalFileStorage
from ai_youtube_agent.core.audit import AuditLog, InMemoryAuditSink
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
from ai_youtube_agent.providers.mock_speech_synthesis import MockSpeechSynthesizer
from ai_youtube_agent.providers.speech_synthesis import SpeechRequest
from factories import (
    T0,
    at,
    make_channel,
    make_content_item,
    make_strategy_profile,
    make_voice_profile,
)

T1 = T0 + timedelta(hours=1)


class Ticker:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self):
        self.now += timedelta(milliseconds=1)
        return self.now


class TestTTSCostService:
    def test_connects_audio_artifact_speech_and_rate_corrections_to_cost_ledger(
        self, database: Database, tmp_path: Path
    ) -> None:
        channel = make_channel()
        strategy = make_strategy_profile(channel)
        item = make_content_item(channel, strategy)
        voice = make_voice_profile(channel)
        with database.transaction() as connection:
            ChannelRepository(connection).add(channel)
            StrategyProfileRepository(connection).add(strategy)
            ContentItemRepository(connection).add(item)
            VoiceProfileRepository(connection).add(voice)

        ticker = Ticker()
        synth = MockSpeechSynthesizer(name="mock_tts", clock=ticker)
        store = InMemoryChunkStore()
        storage = LocalFileStorage(tmp_path / "artifacts")
        sink = InMemoryAuditSink()
        audit = AuditLog(sink, clock=at(T1))

        script = Script.create(
            item.id,
            sections=(
                ScriptSection(SectionKind.BODY, "First voice line for testing."),
                ScriptSection(SectionKind.BODY, "Second voice line for testing."),
            ),
            clock=ticker,
        )
        with database.transaction() as connection:
            ScriptRepository(connection).add(script)

        runner = TtsJobRunner(
            database, synth, store, clock=ticker, sleep=lambda _s: None
        )
        tts_result: TtsResult = dataclasses.replace(
            runner.run(script, voice), characters=2000
        )
        artifact_recorder = AudioArtifactRecorder(
            database, storage, AuditLog(InMemoryAuditSink()), clock=at(T1)
        )
        audio_record: AudioArtifactRecord = artifact_recorder.record(
            tts_result, script, voice
        )

        ledger = CostLedger(database, audit, clock=at(T1))
        audio_cost_recorder = AudioCostRecorder(
            database,
            audit,
            price_per_1k=Decimal("0.015"),
            currency="USD",
            clock=at(T1),
        )
        service = TTSCostService(
            ledger,
            audio_cost_recorder,
            price_per_1k=Decimal("0.015"),
            currency="USD",
        )

        # 1. Record artifact TTS cost -> 2000 * 0.015 / 1000 = 0.030000
        art_entry = service.record_artifact(audio_record, tts_result, voice)
        assert art_entry.created is True
        assert art_entry.cost.category is CostCategory.TTS
        assert art_entry.cost.amount == Decimal("0.030000")
        assert service.for_artifact(audio_record.artifact.id) == art_entry.cost

        # Replay returns created=False
        replay_art = service.record_artifact(audio_record, tts_result, voice)
        assert replay_art.created is False
        assert replay_art.cost.id == art_entry.cost.id

        # 2. Record standalone synthesized speech sample -> 1000 * 0.015 / 1000 = 0.015000
        speech = synth.synthesize(
            SpeechRequest(text="A" * 1000, voice_id=voice.voice_id, language="en-US")
        )
        speech_entry = service.record_speech(
            channel.id,
            speech,
            content_item_id=item.id,
            ref="voice_preview:1",
        )
        assert speech_entry.created is True
        assert speech_entry.cost.amount == Decimal("0.015000")

        # 3. Record rate correction on artifact (0.015 -> 0.020 per 1k chars => +0.010000)
        corr_entry = service.record_rate_correction(
            audio_record.artifact.id,
            characters=2000,
            new_price_per_1k=Decimal("0.020"),
        )
        assert corr_entry.created is True
        assert corr_entry.cost.amount == Decimal("0.010000")

        # 4. Channel TTS summary via CostLedger includes all 3 TTS records
        summary = service.summarize_channel(channel.id)
        assert summary.currency == "USD"
        assert summary.total_amount == Decimal("0.055000")
        assert len(summary.records) == 3

        # Lower or equal rate correction is refused
        with pytest.raises(TTSCostError) as exc_invalid:
            service.record_rate_correction(
                audio_record.artifact.id,
                characters=2000,
                new_price_per_1k=Decimal("0.010"),
            )
        assert (
            exc_invalid.value.tts_cost_code
            is TTSCostErrorCode.INVALID_CORRECTION
        )

        with pytest.raises(TTSCostError) as exc_missing:
            service.record_rate_correction(
                "nonexistent_artifact",
                characters=1000,
                new_price_per_1k=Decimal("0.020"),
            )
        assert (
            exc_missing.value.tts_cost_code
            is TTSCostErrorCode.ARTIFACT_COST_NOT_FOUND
        )
