"""Hook Generator (Prompt Pack v8, prompt #065), context C5 Script & Fact Check.

``HookGenerator`` writes opening hooks through the ``TextGenerator`` provider.
The rules were approved by the user on 2026-10-03:

- Inputs: the content item (its content type), the channel strategy (primary
  language, brand tone, tone keywords, voice dos and donts, banned phrases,
  audience) and a stored research report of the same channel: the topic the
  caller names, else the report's best-scored topic, else its queries, plus
  up to 5 of its claims. The caller may add an angle (at most 300 characters).
  The strategy is only read.
- The provider is asked for 3 candidates. Each text is cleaned and checked
  against the content type's limits and the banned phrases (``check_hook``);
  invalid texts and repeats are kept as rejections with their issues. When
  fewer than 3 are valid, the provider is asked once more, told to avoid the
  texts already seen. At most 3 candidates are kept.
- A retryable provider error is tried up to 3 times (waits 1 then 2 seconds);
  other errors, or the last one, are audited as ``hook.failed`` and raised.
- The run is stored as a ``HookGeneration`` (also when no candidate is valid)
  and audited as ``hook.generated`` after commit. Nothing is written to a
  script: the caller picks a candidate (#066, #067).
"""

import time
from collections.abc import Callable, Sequence
from datetime import datetime
from http import HTTPStatus
from typing import TypeVar

from ai_youtube_agent.content.hook import (
    HOOK_LIMITS,
    MAX_ANGLE,
    MAX_CANDIDATES,
    HookCandidate,
    HookGeneration,
    HookIssue,
    HookIssueCode,
    HookRejection,
    check_hook,
    normalize_hook,
)
from ai_youtube_agent.content.research_report import ReportTopic, ResearchReport
from ai_youtube_agent.content.strategy import StrategyProfile
from ai_youtube_agent.content.strategy_settings import StrategyNotFoundError
from ai_youtube_agent.core.audit import Actor, AuditLog, AuditResult, EntityRef
from ai_youtube_agent.core.content_item import ContentItem, ContentType
from ai_youtube_agent.core.db.database import Database
from ai_youtube_agent.core.db.repositories.channel import StrategyProfileRepository
from ai_youtube_agent.core.db.repositories.content import (
    ContentItemRepository,
    HookGenerationRepository,
)
from ai_youtube_agent.core.db.repositories.research import ResearchReportRepository
from ai_youtube_agent.core.errors import DomainError
from ai_youtube_agent.providers.text_generation import (
    GeneratedText,
    TextGenerationError,
    TextGenerator,
    TextRequest,
)

MAX_ATTEMPTS = 3
BACKOFF_SECONDS = (1.0, 2.0)
MAX_ROUNDS = 2
MAX_PROMPT_CLAIMS = 5
MAX_TOKENS = {ContentType.SHORTS: 80, ContentType.LONGFORM: 240}
SYSTEM = (
    "You write the opening hook of YouTube videos. Answer with the hook text "
    "only: no title, no quotes, no explanation."
)
Clock = Callable[[], datetime]
T = TypeVar("T")


class ContentItemNotFoundError(DomainError):
    default_code = "domain.content_item_not_found"
    default_user_message = "This content item does not exist."
    default_http_status = HTTPStatus.NOT_FOUND


class ResearchReportNotFoundError(DomainError):
    default_code = "domain.research_report_not_found"
    default_user_message = "This research report does not exist."
    default_http_status = HTTPStatus.NOT_FOUND


class HookInputError(DomainError):
    default_code = "domain.hook_input"
    default_user_message = "A hook cannot be written from these inputs."


class HookGenerator:
    def __init__(
        self,
        database: Database,
        generator: TextGenerator,
        audit: AuditLog,
        *,
        clock: Clock | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._database = database
        self._generator = generator
        self._audit = audit
        self._clock = clock
        self._sleep = sleep

    def generate(
        self,
        content_item_id: str,
        research_report_id: str,
        *,
        topic_id: str | None = None,
        angle: str | None = None,
        actor: Actor,
    ) -> HookGeneration:
        angle = angle.strip() if angle is not None and angle.strip() else None
        if angle is not None and len(angle) > MAX_ANGLE:
            raise HookInputError(f"an angle is at most {MAX_ANGLE} characters")
        item, strategy, report = self._inputs(content_item_id, research_report_id)
        language = strategy.languages.primary
        topic = _topic(report, topic_id)
        candidates: list[HookCandidate] = []
        rejected: list[HookRejection] = []
        seen: dict[str, str] = {}  # casefolded -> text as written
        answer: GeneratedText | None = None
        for _ in range(MAX_ROUNDS):
            prompt = build_prompt(
                item.content_type,
                strategy,
                report,
                topic,
                angle,
                avoid=list(seen.values()),
            )
            request = TextRequest(
                prompt,
                system=SYSTEM,
                language=language,
                candidates=MAX_CANDIDATES,
                max_tokens=MAX_TOKENS[item.content_type],
            )
            answer = self._call(request, item, actor)
            for raw in answer.texts:
                text = normalize_hook(raw)
                key = text.casefold()
                if key in seen:
                    issues: tuple[HookIssue, ...] = (
                        HookIssue(HookIssueCode.DUPLICATE, "already written"),
                    )
                else:
                    seen[key] = text
                    issues = check_hook(text, item.content_type, _banned(strategy))
                if issues or len(candidates) == MAX_CANDIDATES:
                    if issues:
                        rejected.append(HookRejection(text, issues))
                    continue
                candidates.append(HookCandidate(text))
            if len(candidates) == MAX_CANDIDATES:
                break

        if answer is None:  # MAX_ROUNDS is at least 1
            raise AssertionError("no provider answer")
        generation = HookGeneration.create(
            content_item_id=item.id,
            content_type=item.content_type,
            language=language,
            strategy_version=strategy.version,
            research_report_id=report.id,
            topic_id=topic.topic_id if topic else None,
            topic_label=topic.label if topic else None,
            angle=angle,
            candidates=tuple(candidates),
            rejected=tuple(rejected),
            provider=answer.provider,
            model=answer.model,
            requested_by=actor,
            clock=self._clock,
        )
        with self._database.transaction() as connection:
            HookGenerationRepository(connection).add(generation)
        self._audit.record(
            "hook.generated",
            actor,
            EntityRef("content_item", item.id),
            AuditResult.SUCCESS if candidates else AuditResult.FAILURE,
            {
                "hook_generation_id": generation.id,
                "candidates": len(candidates),
                "rejected": len(rejected),
                "provider": generation.provider,
            },
        )
        return generation

    def _inputs(
        self, content_item_id: str, research_report_id: str
    ) -> tuple[ContentItem, StrategyProfile, ResearchReport]:
        with self._database.transaction() as connection:
            item = ContentItemRepository(connection).get(content_item_id)
            if item is None:
                raise ContentItemNotFoundError(
                    f"content item {content_item_id} does not exist"
                )
            strategy = StrategyProfileRepository(connection).get_by_channel(
                item.channel_id
            )
            report = ResearchReportRepository(connection).get(research_report_id)
        if strategy is None:
            raise StrategyNotFoundError(f"channel {item.channel_id} has no strategy")
        if strategy.languages is None:
            raise HookInputError(
                f"the strategy of channel {item.channel_id} has no language"
            )
        if report is None:
            raise ResearchReportNotFoundError(
                f"research report {research_report_id} does not exist"
            )
        if report.channel_id != item.channel_id:
            raise HookInputError(
                f"research report {report.id} belongs to another channel"
            )
        return item, strategy, report

    def _call(
        self, request: TextRequest, item: ContentItem, actor: Actor
    ) -> GeneratedText:
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                return self._generator.generate(request)
            except TextGenerationError as error:
                if not error.retryable or attempt == MAX_ATTEMPTS:
                    self._audit.record(
                        "hook.failed",
                        actor,
                        EntityRef("content_item", item.id),
                        AuditResult.FAILURE,
                        {"code": error.code, "attempts": attempt},
                    )
                    raise
                self._sleep(BACKOFF_SECONDS[attempt - 1])
        raise AssertionError("unreachable: the last attempt returns or raises")


def _topic(report: ResearchReport, topic_id: str | None) -> ReportTopic | None:
    if topic_id is None:
        return report.topics[0] if report.topics else None
    for topic in report.topics:
        if topic.topic_id == topic_id:
            return topic
    raise HookInputError(f"topic {topic_id} is not in research report {report.id}")


def _banned(strategy: StrategyProfile) -> tuple[str, ...]:
    return strategy.brand.banned_phrases if strategy.brand else ()


def build_prompt(
    content_type: ContentType,
    strategy: StrategyProfile,
    report: ResearchReport,
    topic: ReportTopic | None,
    angle: str | None,
    *,
    avoid: Sequence[str] = (),
) -> str:
    """The instructions for one hook, from the strategy and the report."""
    limits = HOOK_LIMITS[content_type]
    video = (
        "YouTube Shorts video"
        if content_type is ContentType.SHORTS
        else ("long-form YouTube video")
    )
    lines = [
        f"Write one opening hook for a {video}.",
        f"Language: {strategy.languages.primary}.",
        f"Length: at most {limits.max_sentences} sentences and "
        f"{limits.max_words} words.",
    ]
    if topic is not None:
        lines.append(
            f"Topic: {topic.label} (key phrases: {', '.join(topic.keyphrases)})."
        )
    else:
        lines.append(f"Topic: {'; '.join(report.queries)}.")
    if angle:
        lines.append(f"Angle: {angle}")
    if strategy.audience is not None:
        lines.append(f"Audience: {strategy.audience.description}")
    brand = strategy.brand
    if brand is not None:
        if brand.tone:
            lines.append(f"Tone: {brand.tone}")
        if brand.tone_keywords:
            lines.append(f"Tone keywords: {', '.join(brand.tone_keywords)}.")
        lines += [f"Do: {rule}" for rule in brand.voice_dos]
        lines += [f"Don't: {rule}" for rule in brand.voice_donts]
        if brand.banned_phrases:
            lines.append(f"Never use: {'; '.join(brand.banned_phrases)}.")
    claims = report.claims[:MAX_PROMPT_CLAIMS]
    if claims:
        lines.append("State only facts supported by these research claims:")
        lines += [f"- {claim.text}" for claim in claims]
    else:
        lines.append("Do not state specific facts, numbers or claims.")
    if avoid:
        lines.append("Write something different from:")
        lines += [f"- {text}" for text in avoid]
    return "\n".join(lines)
