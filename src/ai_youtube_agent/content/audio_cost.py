"""Audio Cost (Prompt Pack v8, prompt #092), contexts C7 Voice & Audio and
C14 Economics.

``AudioCostRecorder`` records the TTS cost of a stored audio ``Artifact``
(#089) as one ``CostRecord`` (#025) of category ``tts``. Decisions (written for
the user, build-first, 2026-10-17):

- Price: ``Settings.tts_price_per_1k_characters`` (a ``Decimal`` of 0 or more)
  in ``Settings.tts_cost_currency``. The amount is
  ``TtsResult.characters * price / 1000``, rounded half up to 6 decimal places.
  The whole script text is billed once per artifact. Retried or failed provider
  calls (``TtsResult.provider_calls``, ``retries``) are not billed (see
  ``docs/TECH_DEBT.md``).
- A zero price still writes a record with amount 0, so every audio has a cost
  row.
- One cost per artifact: the record has ``ref`` = the artifact id. When a ``tts``
  cost with that ref exists, it is returned with ``created=False`` and nothing
  is written or audited, even when the price changed since (a correction is a
  new record made by the ledger, #174). A new artifact version has its own cost.
- Currency guard: when the channel has a strategy with a budget in another
  currency than the recorder's, the cost is refused
  (``domain.audio_cost_currency_mismatch``) and nothing is written, because a
  cost row in another currency (even an amount of 0) would make the budget gate
  block the channel (``budget.currency_mismatch``). A channel without a budget
  records normally.
- The content item must exist and belong to the voice profile's channel, and
  the artifact must be an audio artifact. All checks, the replay lookup and the
  insert run in one transaction.
- After the commit, ``cost.recorded`` is audited by the system with scalar data
  only (ids, characters, amount as text, currency, provider).

Call order: ``TtsJobRunner.run`` (#088), then ``AudioArtifactRecorder.record``
(#089), then ``AudioCostRecorder.record``. The artifact and the cost are two
commits; calling ``record`` again with the same artifact (also the
``created=False`` replay of the artifact recorder) is safe and writes the
missing cost after a crash between the two commits.
"""

import sqlite3
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum

from ai_youtube_agent.content.audio_artifact import AudioArtifactRecord
from ai_youtube_agent.content.cost import CostCategory, CostRecord
from ai_youtube_agent.content.strategy import CURRENCY_PATTERN
from ai_youtube_agent.content.tts_job import TtsResult
from ai_youtube_agent.content.voice import VoiceProfile
from ai_youtube_agent.core.artifact import ArtifactKind
from ai_youtube_agent.core.audit import (
    Actor,
    ActorKind,
    AuditLog,
    AuditResult,
    EntityRef,
    MetadataValue,
)
from ai_youtube_agent.core.db.database import Database
from ai_youtube_agent.core.db.repositories.channel import StrategyProfileRepository
from ai_youtube_agent.core.db.repositories.content import ContentItemRepository
from ai_youtube_agent.core.db.repositories.economics import CostRecordRepository
from ai_youtube_agent.core.errors import DomainError

ACTOR = Actor(ActorKind.SYSTEM, "audio_cost")
RECORDED = "cost.recorded"
CHARACTERS_PER_PRICE = Decimal(1000)
AMOUNT_STEP = Decimal("0.000001")
Clock = Callable[[], datetime]


class AudioCostErrorCode(StrEnum):
    COST = "domain.audio_cost"
    NOT_AUDIO = "domain.audio_cost_not_audio"
    CHANNEL_MISMATCH = "domain.audio_cost_channel_mismatch"
    ITEM_NOT_FOUND = "domain.audio_cost_item_not_found"
    CURRENCY_MISMATCH = "domain.audio_cost_currency_mismatch"


USER_MESSAGES = {
    AudioCostErrorCode.COST: "The audio cost could not be recorded.",
    AudioCostErrorCode.NOT_AUDIO: "Only the cost of an audio file can be recorded.",
    AudioCostErrorCode.CHANNEL_MISMATCH: "The voice profile belongs to another "
    "channel.",
    AudioCostErrorCode.ITEM_NOT_FOUND: "This content item does not exist.",
    AudioCostErrorCode.CURRENCY_MISMATCH: "The audio cost currency differs from the "
    "channel budget currency.",
}


class AudioCostError(DomainError):
    """The audio cost was refused; ``code`` is an ``AudioCostErrorCode``."""

    default_code = AudioCostErrorCode.COST.value
    default_user_message = USER_MESSAGES[AudioCostErrorCode.COST]

    def __init__(
        self,
        detail: str = "",
        *,
        code: AudioCostErrorCode = AudioCostErrorCode.COST,
    ) -> None:
        super().__init__(detail, code=code.value, user_message=USER_MESSAGES[code])


@dataclass(frozen=True)
class AudioCostRecord:
    cost: CostRecord
    created: bool


class AudioCostRecorder:
    def __init__(
        self,
        database: Database,
        audit: AuditLog,
        *,
        price_per_1k: Decimal,
        currency: str,
        clock: Clock | None = None,
    ) -> None:
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
        self._database = database
        self._audit = audit
        self._price_per_1k = price_per_1k
        self._currency = currency
        self._clock = clock

    def record(
        self, record: AudioArtifactRecord, result: TtsResult, voice: VoiceProfile
    ) -> AudioCostRecord:
        artifact = record.artifact
        if artifact.kind is not ArtifactKind.AUDIO:
            raise AudioCostError(
                f"artifact {artifact.id} is a {artifact.kind.value} artifact",
                code=AudioCostErrorCode.NOT_AUDIO,
            )
        with self._database.transaction() as connection:
            self._check(connection, artifact.content_item_id, voice)
            costs = CostRecordRepository(connection)
            existing = costs.find_by_ref(CostCategory.TTS, artifact.id)
            if existing is not None:
                return AudioCostRecord(existing, False)
            cost = CostRecord.create(
                voice.channel_id,
                CostCategory.TTS,
                provider=voice.provider,
                amount=self._amount(result.characters),
                currency=self._currency,
                content_item_id=artifact.content_item_id,
                ref=artifact.id,
                clock=self._clock,
            )
            costs.add(cost)
        self._audit.record(
            RECORDED,
            ACTOR,
            EntityRef("cost_record", cost.id),
            AuditResult.SUCCESS,
            _event_metadata(cost, result),
        )
        return AudioCostRecord(cost, True)

    def _amount(self, characters: int) -> Decimal:
        raw = Decimal(characters) * self._price_per_1k / CHARACTERS_PER_PRICE
        return raw.quantize(AMOUNT_STEP, ROUND_HALF_UP)

    def _check(
        self, connection: sqlite3.Connection, item_id: str, voice: VoiceProfile
    ) -> None:
        item = ContentItemRepository(connection).get(item_id)
        if item is None:
            raise AudioCostError(
                f"content item {item_id} does not exist",
                code=AudioCostErrorCode.ITEM_NOT_FOUND,
            )
        if item.channel_id != voice.channel_id:
            raise AudioCostError(
                f"voice profile {voice.id} belongs to another channel than "
                f"content item {item.id}",
                code=AudioCostErrorCode.CHANNEL_MISMATCH,
            )
        strategy = StrategyProfileRepository(connection).get_by_channel(item.channel_id)
        budget = strategy.budget if strategy else None
        if budget is not None and budget.currency != self._currency:
            raise AudioCostError(
                f"the TTS cost currency {self._currency} differs from the "
                f"{budget.currency} budget of channel {item.channel_id}",
                code=AudioCostErrorCode.CURRENCY_MISMATCH,
            )


def _event_metadata(cost: CostRecord, result: TtsResult) -> Mapping[str, MetadataValue]:
    return {
        "artifact_id": cost.ref,
        "content_item_id": cost.content_item_id,
        "channel_id": cost.channel_id,
        "characters": result.characters,
        "amount": str(cost.amount),
        "currency": cost.currency,
        "provider": cost.provider,
    }
