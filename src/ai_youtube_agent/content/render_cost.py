"""Video rendering cost recorder (Prompt Pack v8, prompt #177), contexts C8, C9 and C14.

``RenderCostRecorder`` computes and records video rendering costs of category
``CostCategory.RENDER`` into ``CostLedger`` (#174) for rendered video artifacts
(#100, #111) or explicit render telemetry:

- ``RenderPricingPolicy`` defines ``price_per_minute`` (finite ``Decimal >= 0``)
  in an ISO 4217 ``currency`` and an optional ``uhd_multiplier`` (applied when
  ``max(width, height) > 1920``).
- Amount is ``(duration_ms / 60_000) * price_per_minute * multiplier`` rounded
  half-up to 6 decimal places via ``quantize_cost_amount``. A zero price still
  writes a ``CostRecord`` of ``0.000000`` so every rendered video has a cost row.
- ``record_rendered_artifact`` requires ``artifact.kind is ArtifactKind.VIDEO``
  and uses ``ref = artifact.id`` for idempotent replay.
"""

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from http import HTTPStatus
from typing import Any, Final

from ai_youtube_agent.content.cost import CostCategory
from ai_youtube_agent.content.cost_ledger import (
    CostLedger,
    CostLedgerEntry,
    quantize_cost_amount,
)
from ai_youtube_agent.content.strategy import CURRENCY_PATTERN
from ai_youtube_agent.core.artifact import Artifact, ArtifactKind
from ai_youtube_agent.core.audit import Actor, ActorKind
from ai_youtube_agent.core.errors import DomainError
from ai_youtube_agent.providers.video_render import MAX_DURATION_MS, RenderedVideo

MS_PER_MINUTE: Final[Decimal] = Decimal(60_000)
FULL_HD_MAX_SIDE: Final[int] = 1920
RENDER_COST_ACTOR: Final[Actor] = Actor(ActorKind.SYSTEM, "render_cost")


class RenderCostErrorCode(StrEnum):
    NOT_VIDEO_ARTIFACT = "domain.render_cost_not_video_artifact"
    INVALID_TELEMETRY = "domain.render_cost_invalid_telemetry"


USER_MESSAGES: Final[dict[RenderCostErrorCode, str]] = {
    RenderCostErrorCode.NOT_VIDEO_ARTIFACT: (
        "Only the cost of a video artifact can be recorded as a render cost."
    ),
    RenderCostErrorCode.INVALID_TELEMETRY: (
        "Render duration and canvas dimensions must be positive whole numbers."
    ),
}

_STATUS: Final[dict[RenderCostErrorCode, HTTPStatus]] = {
    RenderCostErrorCode.NOT_VIDEO_ARTIFACT: HTTPStatus.UNPROCESSABLE_ENTITY,
    RenderCostErrorCode.INVALID_TELEMETRY: HTTPStatus.UNPROCESSABLE_ENTITY,
}


class RenderCostError(DomainError):
    default_code = RenderCostErrorCode.NOT_VIDEO_ARTIFACT.value
    default_user_message = USER_MESSAGES[RenderCostErrorCode.NOT_VIDEO_ARTIFACT]
    default_http_status = HTTPStatus.UNPROCESSABLE_ENTITY

    def __init__(self, code: RenderCostErrorCode, detail: str) -> None:
        if not isinstance(code, RenderCostErrorCode):
            raise TypeError("code must be a RenderCostErrorCode")
        self.render_cost_code = code
        super().__init__(detail, code=code.value, user_message=USER_MESSAGES[code])
        self.default_http_status = _STATUS[code]


@dataclass(frozen=True)
class RenderPricingPolicy:
    price_per_minute: Decimal = Decimal("0")
    currency: str = "USD"
    uhd_multiplier: Decimal = Decimal("2.0")

    def __post_init__(self) -> None:
        if (
            isinstance(self.price_per_minute, bool)
            or not isinstance(self.price_per_minute, Decimal)
            or not self.price_per_minute.is_finite()
            or self.price_per_minute < 0
        ):
            raise ValueError("price_per_minute must be a finite Decimal of 0 or more")
        if (
            isinstance(self.uhd_multiplier, bool)
            or not isinstance(self.uhd_multiplier, Decimal)
            or not self.uhd_multiplier.is_finite()
            or self.uhd_multiplier < Decimal("1")
        ):
            raise ValueError("uhd_multiplier must be a finite Decimal of 1 or more")
        if not isinstance(self.currency, str) or not CURRENCY_PATTERN.match(
            self.currency
        ):
            raise ValueError(
                f"currency {self.currency!r} must be an ISO 4217 code such as 'USD'"
            )

    def compute_amount(
        self, duration_ms: int, *, width: int = 1080, height: int = 1920
    ) -> Decimal:
        _check_positive_int("duration_ms", duration_ms, MAX_DURATION_MS)
        _check_positive_int("width", width, 4096)
        _check_positive_int("height", height, 4096)
        multiplier = (
            self.uhd_multiplier
            if max(width, height) > FULL_HD_MAX_SIDE
            else Decimal("1")
        )
        raw = (
            (Decimal(duration_ms) / MS_PER_MINUTE) * self.price_per_minute * multiplier
        )
        return quantize_cost_amount(raw)

    def as_dict(self) -> dict[str, Any]:
        return {
            "price_per_minute": str(self.price_per_minute),
            "currency": self.currency,
            "uhd_multiplier": str(self.uhd_multiplier),
        }


class RenderCostRecorder:
    """Records ``CostCategory.RENDER`` costs into ``CostLedger``."""

    def __init__(
        self,
        ledger: CostLedger,
        *,
        policy: RenderPricingPolicy | None = None,
    ) -> None:
        if not isinstance(ledger, CostLedger):
            raise TypeError("ledger must be a CostLedger")
        if policy is not None and not isinstance(policy, RenderPricingPolicy):
            raise TypeError("policy must be a RenderPricingPolicy")
        self._ledger = ledger
        self._policy = policy or RenderPricingPolicy()

    @property
    def policy(self) -> RenderPricingPolicy:
        return self._policy

    def record_rendered_artifact(
        self,
        channel_id: str,
        artifact: Artifact,
        rendered: RenderedVideo,
        *,
        actor: Actor = RENDER_COST_ACTOR,
    ) -> CostLedgerEntry:
        """Record the rendering cost of a stored ``ArtifactKind.VIDEO`` artifact."""
        if not isinstance(artifact, Artifact):
            raise TypeError("artifact must be an Artifact")
        if not isinstance(rendered, RenderedVideo):
            raise TypeError("rendered must be a RenderedVideo")
        if artifact.kind is not ArtifactKind.VIDEO:
            raise RenderCostError(
                RenderCostErrorCode.NOT_VIDEO_ARTIFACT,
                f"artifact {artifact.id} has kind {artifact.kind.value}, "
                "expected video",
            )
        return self.record_usage(
            channel_id,
            provider=rendered.provider,
            duration_ms=rendered.duration_ms,
            width=rendered.width,
            height=rendered.height,
            fps=rendered.fps,
            content_item_id=artifact.content_item_id,
            ref=artifact.id,
            actor=actor,
        )

    def record_usage(
        self,
        channel_id: str,
        *,
        provider: str,
        duration_ms: int,
        width: int = 1080,
        height: int = 1920,
        fps: int = 30,
        content_item_id: str | None = None,
        ref: str | None = None,
        actor: Actor = RENDER_COST_ACTOR,
    ) -> CostLedgerEntry:
        """Compute and record a render cost from duration and canvas dimensions."""
        _check_positive_int("fps", fps, 120)
        amount = self._policy.compute_amount(duration_ms, width=width, height=height)
        return self._ledger.record(
            channel_id,
            CostCategory.RENDER,
            provider=provider,
            amount=amount,
            currency=self._policy.currency,
            content_item_id=content_item_id,
            ref=ref,
            actor=actor,
            extra_metadata={
                "duration_ms": duration_ms,
                "width": width,
                "height": height,
                "fps": fps,
            },
        )


def _check_positive_int(name: str, value: object, maximum: int) -> None:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 1 <= value <= maximum
    ):
        raise RenderCostError(
            RenderCostErrorCode.INVALID_TELEMETRY,
            f"{name} must be an integer between 1 and {maximum}",
        )
