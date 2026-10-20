"""TTS voice generation cost ledger bridge (Prompt Pack v8, prompt #176), contexts C7 and C14.

Connects voice generation costs (#092 ``AudioCostRecorder``, #086 ``SynthesizedSpeech``,
#088 ``TtsResult``) to the unified ``CostLedger`` (#174):

- ``TTSCostService.record_artifact`` records an audio artifact's TTS cost via
  ``AudioCostRecorder`` and returns a ``CostLedgerEntry`` so artifact-bound voice
  costs and ledger queries share a single ``cost_records`` source of truth.
- ``TTSCostService.record_speech`` records ``CostCategory.TTS`` usage for a
  ``SynthesizedSpeech`` result directly into ``CostLedger`` (for voice previews or
  synthesis outside an audio artifact).
- ``TTSCostService.record_rate_correction`` appends a supplementary ``CostCategory.TTS``
  record via ``CostLedger.record_correction`` when a provider rate adjustment increases
  the cost of an already-recorded audio artifact (#092 note: corrections are new
  records made by the ledger).
- ``for_artifact`` and ``list_for_channel`` query ``CostCategory.TTS`` entries through
  ``CostLedger``.
"""

from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum
from http import HTTPStatus
from typing import Any, Final

from ai_youtube_agent.content.audio_artifact import AudioArtifactRecord
from ai_youtube_agent.content.audio_cost import (
    ACTOR as AUDIO_COST_ACTOR,
)
from ai_youtube_agent.content.audio_cost import (
    AMOUNT_STEP,
    CHARACTERS_PER_PRICE,
    AudioCostRecorder,
)
from ai_youtube_agent.content.cost import CostCategory, CostRecord
from ai_youtube_agent.content.cost_ledger import (
    CostLedger,
    CostLedgerEntry,
    quantize_cost_amount,
)
from ai_youtube_agent.content.strategy import CURRENCY_PATTERN
from ai_youtube_agent.content.tts_job import TtsResult
from ai_youtube_agent.content.voice import VoiceProfile
from ai_youtube_agent.core.audit import Actor
from ai_youtube_agent.core.errors import DomainError
from ai_youtube_agent.providers.speech_synthesis import SynthesizedSpeech


class TTSCostErrorCode(StrEnum):
    ARTIFACT_COST_NOT_FOUND = "domain.tts_cost_artifact_not_found"
    INVALID_CORRECTION = "domain.tts_cost_invalid_correction"


USER_MESSAGES: Final[dict[TTSCostErrorCode, str]] = {
    TTSCostErrorCode.ARTIFACT_COST_NOT_FOUND: (
        "No existing TTS cost record was found for the specified audio artifact."
    ),
    TTSCostErrorCode.INVALID_CORRECTION: (
        "The adjusted TTS rate does not exceed the already recorded cost."
    ),
}

_STATUS: Final[dict[TTSCostErrorCode, HTTPStatus]] = {
    TTSCostErrorCode.ARTIFACT_COST_NOT_FOUND: HTTPStatus.NOT_FOUND,
    TTSCostErrorCode.INVALID_CORRECTION: HTTPStatus.UNPROCESSABLE_ENTITY,
}


class TTSCostError(DomainError):
    default_code = TTSCostErrorCode.INVALID_CORRECTION.value
    default_user_message = USER_MESSAGES[TTSCostErrorCode.INVALID_CORRECTION]
    default_http_status = HTTPStatus.UNPROCESSABLE_ENTITY

    def __init__(self, code: TTSCostErrorCode, detail: str) -> None:
        if not isinstance(code, TTSCostErrorCode):
            raise TypeError("code must be a TTSCostErrorCode")
        self.tts_cost_code = code
        super().__init__(detail, code=code.value, user_message=USER_MESSAGES[code])
        self.default_http_status = _STATUS[code]


def compute_tts_amount(characters: int, price_per_1k: Decimal) -> Decimal:
    """Compute ``characters * price_per_1k / 1000`` rounded half-up to 6 decimals."""
    if isinstance(characters, bool) or not isinstance(characters, int) or characters < 0:
        raise ValueError("characters must be a non-negative integer")
    if (
        isinstance(price_per_1k, bool)
        or not isinstance(price_per_1k, Decimal)
        or not price_per_1k.is_finite()
        or price_per_1k < 0
    ):
        raise ValueError("price_per_1k must be a finite Decimal of 0 or more")
    raw = Decimal(characters) * price_per_1k / CHARACTERS_PER_PRICE
    return raw.quantize(AMOUNT_STEP, ROUND_HALF_UP)


@dataclass(frozen=True)
class TTSChannelCostSummary:
    channel_id: str
    currency: str | None
    total_amount: Decimal
    records: tuple[CostRecord, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "channel_id": self.channel_id,
            "category": CostCategory.TTS.value,
            "currency": self.currency,
            "total_amount": str(self.total_amount),
            "records": [record.as_dict() for record in self.records],
        }


class TTSCostService:
    """Connects voice generation costs (``AudioCostRecorder`` & ``SynthesizedSpeech``) to ``CostLedger``."""

    def __init__(
        self,
        ledger: CostLedger,
        audio_cost_recorder: AudioCostRecorder,
        *,
        price_per_1k: Decimal,
        currency: str,
    ) -> None:
        if not isinstance(ledger, CostLedger):
            raise TypeError("ledger must be a CostLedger")
        if not isinstance(audio_cost_recorder, AudioCostRecorder):
            raise TypeError("audio_cost_recorder must be an AudioCostRecorder")
        if (
            isinstance(price_per_1k, bool)
            or not isinstance(price_per_1k, Decimal)
            or not price_per_1k.is_finite()
            or price_per_1k < 0
        ):
            raise ValueError("price_per_1k must be a finite Decimal of 0 or more")
        if not isinstance(currency, str) or not CURRENCY_PATTERN.match(currency):
            raise ValueError(
                f"currency {currency!r} must be an ISO 4217 code such as 'USD'"
            )
        self._ledger = ledger
        self._audio_recorder = audio_cost_recorder
        self._price_per_1k = price_per_1k
        self._currency = currency

    def record_artifact(
        self,
        record: AudioArtifactRecord,
        result: TtsResult,
        voice: VoiceProfile,
    ) -> CostLedgerEntry:
        """Record the TTS cost of a stored audio artifact and return a ``CostLedgerEntry``."""
        outcome = self._audio_recorder.record(record, result, voice)
        return CostLedgerEntry(cost=outcome.cost, created=outcome.created)

    def record_speech(
        self,
        channel_id: str,
        speech: SynthesizedSpeech,
        *,
        content_item_id: str | None = None,
        ref: str | None = None,
        price_per_1k: Decimal | None = None,
        actor: Actor = AUDIO_COST_ACTOR,
    ) -> CostLedgerEntry:
        """Record the TTS cost of a ``SynthesizedSpeech`` call directly into ``CostLedger``."""
        if not isinstance(speech, SynthesizedSpeech):
            raise TypeError("speech must be a SynthesizedSpeech")
        effective_price = (
            price_per_1k if price_per_1k is not None else self._price_per_1k
        )
        amount = compute_tts_amount(speech.characters, effective_price)
        return self._ledger.record(
            channel_id,
            CostCategory.TTS,
            provider=speech.provider,
            amount=amount,
            currency=self._currency,
            content_item_id=content_item_id,
            ref=ref,
            actor=actor,
            extra_metadata={
                "characters": speech.characters,
                "voice_id": speech.voice_id,
                "duration_ms": speech.duration_ms,
            },
        )

    def record_rate_correction(
        self,
        artifact_id: str,
        *,
        characters: int,
        new_price_per_1k: Decimal,
        actor: Actor = AUDIO_COST_ACTOR,
        ref: str | None = None,
    ) -> CostLedgerEntry:
        """Append a ledger correction when an audio artifact's TTS rate increases."""
        existing = self._ledger.find_by_ref(CostCategory.TTS, artifact_id)
        if existing is None:
            raise TTSCostError(
                TTSCostErrorCode.ARTIFACT_COST_NOT_FOUND,
                f"no TTS cost record found for artifact {artifact_id}",
            )
        recalculated = compute_tts_amount(characters, new_price_per_1k)
        delta = quantize_cost_amount(max(Decimal("0"), recalculated - existing.amount))
        if delta <= Decimal("0"):
            raise TTSCostError(
                TTSCostErrorCode.INVALID_CORRECTION,
                f"recalculated amount {recalculated} is not above existing cost {existing.amount}",
            )
        return self._ledger.record_correction(
            existing.id,
            additional_amount=delta,
            actor=actor,
            ref=ref or f"correction:{artifact_id}:{new_price_per_1k}",
        )

    def for_artifact(self, artifact_id: str) -> CostRecord | None:
        """Return the primary ``CostCategory.TTS`` record for ``artifact_id``, if any."""
        return self._ledger.find_by_ref(CostCategory.TTS, artifact_id)

    def summarize_channel(
        self,
        channel_id: str,
        *,
        start: datetime | None = None,
        end: datetime | None = None,
        content_item_id: str | None = None,
    ) -> TTSChannelCostSummary:
        """Summarise all ``CostCategory.TTS`` records for a channel."""
        summary = self._ledger.summarize(
            channel_id,
            start=start,
            end=end,
            content_item_id=content_item_id,
        )
        tts_records = tuple(
            rec for rec in summary.records if rec.category is CostCategory.TTS
        )
        total = sum((rec.amount for rec in tts_records), Decimal("0"))
        return TTSChannelCostSummary(
            channel_id=channel_id,
            currency=summary.currency,
            total_amount=total,
            records=tts_records,
        )
