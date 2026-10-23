"""Paid request guard (BUDGET-01): the one backend layer every AI call passes.

``PaidRequestGuard`` decides, before a provider is called, whether the call
may run under the paid-API policy of ``PaidApiBudgetService``:

- A provider whose cost declaration is free (``mock``, ``local_free`` or
  ``free_service``) always runs: the decision is ``free``. Its own failures,
  a free quota running out included, propagate unchanged; the guard never
  calls another provider instead.
- ``free_only`` refuses every ``paid`` or ``unknown`` provider: the request is
  stored blocked and ``PaidRequestBlockedError`` is raised, with no provider
  call.
- ``paid_with_per_request_approval`` lets a call run only by consuming the
  approved quote of exactly that request (hash, price, provider and model)
  just before the call. Otherwise a pending quote is stored and
  ``PaidRequestAwaitingApprovalError`` names it. A provider without a verified
  price is blocked (``budget.price_unknown``, or
  ``budget.provider_cost_unknown`` for an undeclared provider), never offered
  for approval. A provider failure after the approval propagates; nothing is
  tried again without a new approval.

``preflight`` checks the providers of a run up front, ``authorize_batch``
quotes every call of a stage in one pass (all scene clips, all TTS chunks),
and ``authorize_call`` runs on every single call. ``wrap_speech``,
``wrap_image`` and ``wrap_video`` return wrappers that call
``authorize_call`` before each provider call. A wrapper keeps the inner
provider's ``name`` and exposes only explicit properties: there is no generic
attribute forwarding, so a helper of the inner provider that calls it
directly (``synthesize_to_file``, ``generate_and_store``, ``with_wav_output``)
cannot be reached through it. In ``free_only`` a wrapper over a paid provider
reports itself blocked by the budget policy from ``check`` and its
capabilities, without calling the provider.

``bind`` sets the content item and job of the calls made inside it (a
context variable) and collects the quotes they consumed.

A speech request that repeats in a TTS plan (the same text, voice, language
and style) is one call per occurrence: its occurrence index in the plan is
part of the fingerprint, so each call has its own quote and the hashes stay
the same on a re-run. ``GuardedSpeechSynthesizer.authorize_batch`` queues the
occurrences it quoted in the bound session and each ``synthesize`` uses the
first one of its text; chunks a resumed TTS job reuses are not quoted.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Collection, Iterable, Iterator, Sequence
from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any, Final

from ai_youtube_agent.content.paid_api_budget import (
    PaidApiMode,
    PaidBudgetErrorCode,
    PaidRequestAwaitingApprovalError,
    PaidRequestBlockedError,
    PaidRequestContext,
    PaidRequestSpec,
    PaidRequestStatus,
    PaidTaskType,
)
from ai_youtube_agent.content.paid_api_budget_service import PaidApiBudgetService
from ai_youtube_agent.core.log import get_logger
from ai_youtube_agent.providers.image_generation import (
    GeneratedImage,
    ImageGenerationRequest,
    ImageGenerator,
    ImageGeneratorCapabilities,
)
from ai_youtube_agent.providers.provider_cost import (
    ProviderCostClass,
    ProviderCostDeclaration,
    classify_provider,
    task_type_of,
)
from ai_youtube_agent.providers.speech_synthesis import (
    SpeechRequest,
    SpeechSynthesizer,
    SynthesizedSpeech,
)
from ai_youtube_agent.providers.video_generation import (
    GeneratedVideoClip,
    VideoClipGenerationRequest,
    VideoClipGenerator,
    VideoClipGeneratorCapabilities,
)

Clock = Callable[[], datetime]
logger = get_logger(__name__)

PAID_APPROVAL_REQUIRED: Final = "paid_approval_required"
BUDGET_BLOCKED_REASON: Final = (
    "bị chặn bởi chính sách ngân sách: chế độ 'Chỉ miễn phí' không gọi API trả phí"
)


@dataclass(frozen=True)
class BudgetPreflight:
    """The budget check of the providers a run will call."""

    mode: PaidApiMode
    status: str
    declarations: tuple[ProviderCostDeclaration, ...]

    @property
    def paid_providers(self) -> tuple[str, ...]:
        return tuple(d.provider for d in self.declarations if not d.is_free)

    def as_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode.value,
            "status": self.status,
            "paid_providers": list(self.paid_providers),
            "providers": [d.as_dict() for d in self.declarations],
        }


@dataclass
class PaidRequestSession:
    """The calls made inside one ``bind``: their context and consumed quotes."""

    context: PaidRequestContext
    consumed_quote_ids: list[str] = field(default_factory=list)
    # Speech occurrences still to be called, in call order, per request key.
    speech_occurrences: dict[str, list[int]] = field(default_factory=dict)
    _token: Token[PaidRequestSession | None] | None = None

    @property
    def status(self) -> PaidRequestStatus:
        return (
            PaidRequestStatus.PAID_APPROVED
            if self.consumed_quote_ids
            else PaidRequestStatus.FREE
        )

    def close(self) -> None:
        if self._token is not None:
            _SESSION.reset(self._token)
            self._token = None


_SESSION: ContextVar[PaidRequestSession | None] = ContextVar(
    "paid_request_session", default=None
)


def _sha256(*parts: str) -> str:
    return hashlib.sha256("\x00".join(parts).encode("utf-8")).hexdigest()


def _speech_key(request: SpeechRequest) -> str:
    return _sha256(
        request.text,
        request.voice_id,
        request.language,
        request.speaking_style or "",
    )


def speech_occurrences(requests: Sequence[SpeechRequest]) -> list[int]:
    """The occurrence index of each request among the equal ones before it."""
    seen: dict[str, int] = {}
    occurrences = []
    for request in requests:
        key = _speech_key(request)
        occurrences.append(seen.get(key, 0))
        seen[key] = occurrences[-1] + 1
    return occurrences


def speech_spec(
    declaration: ProviderCostDeclaration,
    request: SpeechRequest,
    occurrence: int = 0,
) -> PaidRequestSpec:
    return PaidRequestSpec(
        provider=declaration.provider,
        model=declaration.model,
        task_type=PaidTaskType.SPEECH_SYNTHESIS,
        unit="character",
        unit_count=Decimal(len(request.text)),
        fingerprint=_sha256(_speech_key(request), f"occurrence:{occurrence}"),
    )


def image_spec(
    declaration: ProviderCostDeclaration, request: ImageGenerationRequest
) -> PaidRequestSpec:
    return PaidRequestSpec(
        provider=declaration.provider,
        model=declaration.model,
        task_type=PaidTaskType.IMAGE_GENERATION,
        unit="image",
        unit_count=Decimal(1),
        fingerprint=_sha256(
            request.prompt,
            str(request.width),
            str(request.height),
            request.style,
            request.seed_key,
            request.shot_id,
            ";".join(
                ",".join(str(part) for part in rgb) for rgb in request.palette_rgb
            ),
        ),
        scene_id=request.scene_id,
    )


def clip_spec(
    declaration: ProviderCostDeclaration, request: VideoClipGenerationRequest
) -> PaidRequestSpec:
    return PaidRequestSpec(
        provider=declaration.provider,
        model=declaration.model,
        task_type=PaidTaskType.VIDEO_CLIP_GENERATION,
        unit="second",
        unit_count=Decimal(request.duration_ms) / Decimal(1000),
        fingerprint=request.prompt_hash,
        duration_ms=request.duration_ms,
        scene_id=request.scene_id,
    )


def _preflight_spec(
    declaration: ProviderCostDeclaration, task_type: PaidTaskType
) -> PaidRequestSpec:
    return PaidRequestSpec(
        provider=declaration.provider,
        model=declaration.model,
        task_type=task_type,
        unit="request",
        unit_count=Decimal(1),
        fingerprint=_sha256("preflight", declaration.provider, declaration.model),
    )


def _blocking_code(
    declaration: ProviderCostDeclaration, mode: PaidApiMode
) -> PaidBudgetErrorCode:
    if declaration.cost_class is ProviderCostClass.UNKNOWN:
        return PaidBudgetErrorCode.PROVIDER_COST_UNKNOWN
    if mode is PaidApiMode.FREE_ONLY:
        return PaidBudgetErrorCode.BLOCKED_FREE_ONLY
    return PaidBudgetErrorCode.PRICE_UNKNOWN


class PaidRequestGuard:
    def __init__(
        self, budget: PaidApiBudgetService, *, clock: Clock | None = None
    ) -> None:
        self._budget = budget
        self._clock = clock

    # Context

    def start(self, context: PaidRequestContext) -> PaidRequestSession:
        """Begin a session; ``close`` it in a ``finally`` (or use ``bind``)."""
        session = PaidRequestSession(context)
        session._token = _SESSION.set(session)
        return session

    @contextmanager
    def bind(self, context: PaidRequestContext) -> Iterator[PaidRequestSession]:
        session = self.start(context)
        try:
            yield session
        finally:
            session.close()

    @property
    def context(self) -> PaidRequestContext:
        session = _SESSION.get()
        return session.context if session is not None else PaidRequestContext()

    # Decisions

    def blocked_reason(self, provider: object) -> str | None:
        """Why ``provider`` may not run at all now, without calling it.

        Only a provider that is not free reads the policy; when the policy
        cannot be read the provider is blocked (fail closed).
        """
        declaration = classify_provider(provider)
        if declaration.is_free:
            return None
        try:
            mode = self._budget.get_policy().mode
        except Exception:
            logger.exception("paid-API policy unreadable; paid provider blocked")
            return BUDGET_BLOCKED_REASON
        if mode is PaidApiMode.FREE_ONLY:
            return BUDGET_BLOCKED_REASON
        return None

    def preflight(self, providers: Iterable[object]) -> BudgetPreflight:
        """Check the providers of a run before any of them is called."""
        candidates = list(providers)
        declarations = tuple(classify_provider(p) for p in candidates)
        mode = self._budget.get_policy().mode
        paid = [
            (provider, declaration)
            for provider, declaration in zip(candidates, declarations, strict=True)
            if not declaration.is_free
        ]
        if not paid:
            return BudgetPreflight(mode, PaidRequestStatus.FREE.value, declarations)
        if mode is PaidApiMode.FREE_ONLY:
            quote_ids = tuple(
                self._budget.record_blocked(
                    _preflight_spec(declaration, task_type_of(provider)),
                    _blocking_code(declaration, mode),
                    self.context,
                    unknowns=declaration.unknowns,
                ).id
                for provider, declaration in paid
            )
            first = paid[0][1]
            raise PaidRequestBlockedError(
                f"free_only refuses {', '.join(d.provider for _, d in paid)}",
                code=_blocking_code(first, mode),
                provider=first.provider,
                quote_ids=quote_ids,
            )
        return BudgetPreflight(mode, PAID_APPROVAL_REQUIRED, declarations)

    def authorize_batch(
        self, provider: object, specs: Sequence[PaidRequestSpec]
    ) -> PaidRequestStatus:
        """Quote every call of a stage in one pass.

        ``free`` for a free provider, ``paid_approved`` when every spec has a
        consumable approved quote (nothing is consumed here). Otherwise the
        missing quotes are stored and one ``PaidRequestAwaitingApprovalError``
        names them all, or the batch is blocked.
        """
        declaration = classify_provider(provider)
        if declaration.is_free or not specs:
            return PaidRequestStatus.FREE
        unique = list({spec.request_hash: spec for spec in specs}.values())
        mode = self._budget.get_policy().mode
        if mode is PaidApiMode.FREE_ONLY or declaration.pricing is None:
            self._block(declaration, mode, unique)
        pricing = declaration.pricing
        assert pricing is not None  # _block raised otherwise
        missing = [
            spec
            for spec in unique
            if self._budget.approved_quote(spec, pricing) is None
        ]
        if not missing:
            return PaidRequestStatus.PAID_APPROVED
        quote_ids = tuple(
            self._budget.open_quote(
                spec, pricing, self.context, unknowns=declaration.unknowns
            ).id
            for spec in missing
        )
        raise PaidRequestAwaitingApprovalError(
            f"{len(missing)} of {len(unique)} requests need an approval",
            provider=declaration.provider,
            quote_ids=quote_ids,
        )

    def authorize_call(
        self, provider: object, spec: PaidRequestSpec
    ) -> PaidRequestStatus:
        """Decide one call just before it is made; consumes its approval."""
        declaration = classify_provider(provider)
        if declaration.is_free:
            return PaidRequestStatus.FREE
        mode = self._budget.get_policy().mode
        if mode is PaidApiMode.FREE_ONLY or declaration.pricing is None:
            self._block(declaration, mode, [spec])
        pricing = declaration.pricing
        assert pricing is not None  # _block raised otherwise
        consumed = self._budget.consume(spec, pricing)
        if consumed is not None:
            session = _SESSION.get()
            if session is not None:
                session.consumed_quote_ids.append(consumed.id)
            return PaidRequestStatus.PAID_APPROVED
        quote = self._budget.open_quote(
            spec, pricing, self.context, unknowns=declaration.unknowns
        )
        raise PaidRequestAwaitingApprovalError(
            "the request needs an approval",
            provider=declaration.provider,
            quote_ids=(quote.id,),
        )

    def _block(
        self,
        declaration: ProviderCostDeclaration,
        mode: PaidApiMode,
        specs: Sequence[PaidRequestSpec],
    ) -> None:
        code = _blocking_code(declaration, mode)
        quote_ids = tuple(
            self._budget.record_blocked(
                spec, code, self.context, unknowns=declaration.unknowns
            ).id
            for spec in specs
        )
        raise PaidRequestBlockedError(
            f"{declaration.provider} blocked: {code.value}",
            code=code,
            provider=declaration.provider,
            quote_ids=quote_ids,
        )

    # Wrappers

    # A provider already wrapped by this guard is returned as it is: wrapping
    # it twice would ask for two approvals per call.

    def wrap_speech(self, inner: SpeechSynthesizer) -> GuardedSpeechSynthesizer:
        if isinstance(inner, GuardedSpeechSynthesizer) and inner.guard is self:
            return inner
        return GuardedSpeechSynthesizer(inner, self)

    def wrap_image(self, inner: ImageGenerator) -> GuardedImageGenerator:
        if isinstance(inner, GuardedImageGenerator) and inner.guard is self:
            return inner
        return GuardedImageGenerator(inner, self)

    def wrap_video(self, inner: VideoClipGenerator) -> GuardedVideoClipGenerator:
        if isinstance(inner, GuardedVideoClipGenerator) and inner.guard is self:
            return inner
        return GuardedVideoClipGenerator(inner, self)


class _Guarded:
    """Shared parts of the wrappers: explicit properties only, no forwarding."""

    task_type: PaidTaskType

    def __init__(self, inner: Any, guard: PaidRequestGuard) -> None:
        self._inner = inner
        self._guard = guard
        self.name: str = inner.name

    @property
    def guard(self) -> PaidRequestGuard:
        return self._guard

    @property
    def model_name(self) -> str | None:
        return getattr(self._inner, "model_name", None)

    @property
    def last_job(self) -> Any:
        return getattr(self._inner, "last_job", None)

    @property
    def is_paid(self) -> bool:
        return not classify_provider(self._inner).is_free

    def cost_declaration(self) -> ProviderCostDeclaration:
        return classify_provider(self._inner)

    def budget_blocked_reason(self) -> str | None:
        return self._guard.blocked_reason(self._inner)

    def _blocked_error(self) -> PaidRequestBlockedError:
        declaration = classify_provider(self._inner)
        return PaidRequestBlockedError(
            f"{self.name}: {BUDGET_BLOCKED_REASON}",
            code=_blocking_code(declaration, PaidApiMode.FREE_ONLY),
            provider=declaration.provider,
        )

    def check(self) -> None:
        """The inner health probe, unless the budget blocks the provider (then
        no call is made)."""
        if self.budget_blocked_reason() is not None:
            raise self._blocked_error()
        self._inner.check()


class GuardedSpeechSynthesizer(_Guarded):
    task_type = PaidTaskType.SPEECH_SYNTHESIS

    @property
    def default_voice(self) -> str | None:
        return getattr(self._inner, "default_voice", None)

    @property
    def output_format(self) -> str | None:
        return getattr(self._inner, "output_format", None)

    def spec_for(self, request: SpeechRequest, occurrence: int = 0) -> PaidRequestSpec:
        return speech_spec(classify_provider(self._inner), request, occurrence)

    def authorize_batch(
        self,
        requests: Iterable[SpeechRequest],
        *,
        reused: Collection[int] = frozenset(),
    ) -> PaidRequestStatus:
        """Quote the calls of a TTS plan in one pass.

        ``requests`` is the whole plan in call order; the positions in
        ``reused`` are not called again (their audio is stored), so they are
        not quoted, but they still count for the occurrence index of a text
        that repeats. The occurrences quoted are queued in the bound session.
        """
        if not self.is_paid:
            return PaidRequestStatus.FREE
        planned = list(requests)
        calls = [
            (request, occurrence)
            for position, (request, occurrence) in enumerate(
                zip(planned, speech_occurrences(planned), strict=True)
            )
            if position not in reused
        ]
        session = _SESSION.get()
        if session is not None:
            queue: dict[str, list[int]] = {}
            for request, occurrence in calls:
                queue.setdefault(_speech_key(request), []).append(occurrence)
            session.speech_occurrences = queue
        return self._guard.authorize_batch(
            self._inner,
            [self.spec_for(request, occurrence) for request, occurrence in calls],
        )

    def synthesize(self, request: SpeechRequest) -> SynthesizedSpeech:
        """One call; it uses the quote of the next queued occurrence of its
        text (the first one without a queue). A failed call keeps that
        occurrence, so a retry needs a new approval of the same request."""
        session = _SESSION.get()
        queue = (
            session.speech_occurrences.get(_speech_key(request))
            if session is not None
            else None
        )
        occurrence = queue[0] if queue else 0
        self._guard.authorize_call(self._inner, self.spec_for(request, occurrence))
        speech = self._inner.synthesize(request)
        if queue:
            queue.pop(0)
        return speech


class GuardedImageGenerator(_Guarded):
    task_type = PaidTaskType.IMAGE_GENERATION

    def _blocked_capabilities(self) -> ImageGeneratorCapabilities:
        return ImageGeneratorCapabilities(
            provider_name=self.name,
            model_name=classify_provider(self._inner).model,
            available=False,
            is_mock_or_local=False,
            reason=BUDGET_BLOCKED_REASON,
        )

    def capabilities_image_gen(self) -> ImageGeneratorCapabilities:
        if self.budget_blocked_reason() is not None:
            return self._blocked_capabilities()
        if hasattr(self._inner, "capabilities_image_gen"):
            return self._inner.capabilities_image_gen()
        return self._inner.capabilities()

    def capabilities(self) -> ImageGeneratorCapabilities:
        return self.capabilities_image_gen()

    def spec_for(self, request: ImageGenerationRequest) -> PaidRequestSpec:
        return image_spec(classify_provider(self._inner), request)

    def authorize_batch(
        self, requests: Iterable[ImageGenerationRequest]
    ) -> PaidRequestStatus:
        if not self.is_paid:
            return PaidRequestStatus.FREE
        return self._guard.authorize_batch(
            self._inner, [self.spec_for(request) for request in requests]
        )

    def generate(self, request: ImageGenerationRequest) -> GeneratedImage:
        self._guard.authorize_call(self._inner, self.spec_for(request))
        return self._inner.generate(request)


class GuardedVideoClipGenerator(_Guarded):
    task_type = PaidTaskType.VIDEO_CLIP_GENERATION

    def capabilities_video_clip(self) -> VideoClipGeneratorCapabilities:
        if self.budget_blocked_reason() is not None:
            return VideoClipGeneratorCapabilities(
                provider_name=self.name,
                model_name=classify_provider(self._inner).model,
                available=False,
                is_mock=False,
                reason=BUDGET_BLOCKED_REASON,
            )
        if hasattr(self._inner, "capabilities_video_clip"):
            return self._inner.capabilities_video_clip()
        return self._inner.capabilities()

    def capabilities(self) -> VideoClipGeneratorCapabilities:
        return self.capabilities_video_clip()

    def spec_for(self, request: VideoClipGenerationRequest) -> PaidRequestSpec:
        return clip_spec(classify_provider(self._inner), request)

    def authorize_batch(
        self, requests: Iterable[VideoClipGenerationRequest]
    ) -> PaidRequestStatus:
        if not self.is_paid:
            return PaidRequestStatus.FREE
        return self._guard.authorize_batch(
            self._inner, [self.spec_for(request) for request in requests]
        )

    def generate_clip(self, request: VideoClipGenerationRequest) -> GeneratedVideoClip:
        self._guard.authorize_call(self._inner, self.spec_for(request))
        return self._inner.generate_clip(request)


__all__ = [
    "BUDGET_BLOCKED_REASON",
    "BudgetPreflight",
    "GuardedImageGenerator",
    "GuardedSpeechSynthesizer",
    "GuardedVideoClipGenerator",
    "PAID_APPROVAL_REQUIRED",
    "PaidRequestGuard",
    "PaidRequestSession",
    "clip_spec",
    "image_spec",
    "speech_occurrences",
    "speech_spec",
]
