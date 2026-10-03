"""Shorts Script Generator (Prompt Pack v8, prompt #066), context C5.

``ShortsScriptGenerator`` writes a concise Shorts script through the
``TextGenerator`` provider. The rules were approved by the user on 2026-10-03:

- The caller names a stored hook candidate (``hook_generation_id`` and the
  candidate's index) of the same Shorts content item. Its text becomes the
  HOOK section word for word; the provider writes the rest. The research
  report is the one the hooks were written from; the strategy is the
  channel's current one (primary language and a format are required).
- A Shorts script is HOOK, 1 to 3 BODY sections and one CTA; no intro,
  chapter or outro. The provider answers JSON ``{"body": [...], "cta": "..."}``
  (a code fence around it is allowed).
- Before storing, the answer must parse into that shape, no section may hold
  a brand banned phrase, the estimated duration (``Script``, 150 words per
  minute) must be within the strategy's Shorts format target, and it must not
  repeat the latest script word for word. A failing answer is rejected and
  the provider asked again with the reasons, up to 3 answers.
  Claims are not checked here (#068-#070); full validation is #072.
- A retryable provider error is tried up to 3 times per answer
  (``text_prompts.call_with_retries``).
- The accepted script is stored as version 1, or as the next version of the
  item's latest script. ``created_by`` is the AI actor ``<provider>/<model>``;
  the version records the strategy version, the research report and the
  duration target, and a reason. ``script.generated`` is audited for the
  caller after commit. When every answer is rejected, nothing is stored,
  ``script.generation_failed`` is audited and ``ScriptGenerationError`` raised;
  a provider failure is audited the same way and re-raised.
"""

import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from http import HTTPStatus
from typing import Any

from ai_youtube_agent.content.hook import HookGeneration, find_banned_phrases
from ai_youtube_agent.content.hook_generator import (
    ContentItemNotFoundError,
    ResearchReportNotFoundError,
)
from ai_youtube_agent.content.research_report import ReportTopic, ResearchReport
from ai_youtube_agent.content.script import (
    WORDS_PER_MINUTE,
    DurationTarget,
    Script,
    ScriptSection,
    SectionKind,
)
from ai_youtube_agent.content.strategy import StrategyProfile
from ai_youtube_agent.content.strategy_settings import StrategyNotFoundError
from ai_youtube_agent.content.text_prompts import (
    banned_phrases,
    call_with_retries,
    fact_lines,
    strategy_lines,
)
from ai_youtube_agent.core.audit import (
    Actor,
    ActorKind,
    AuditLog,
    AuditResult,
    EntityRef,
)
from ai_youtube_agent.core.content_item import ContentItem, ContentType
from ai_youtube_agent.core.db.database import Database
from ai_youtube_agent.core.db.repositories.channel import StrategyProfileRepository
from ai_youtube_agent.core.db.repositories.content import (
    ContentItemRepository,
    HookGenerationRepository,
    ScriptRepository,
)
from ai_youtube_agent.core.db.repositories.research import ResearchReportRepository
from ai_youtube_agent.core.errors import DomainError
from ai_youtube_agent.providers.text_generation import (
    GeneratedText,
    TextGenerationError,
    TextGenerator,
    TextRequest,
)

MAX_ANSWERS = 3
MAX_BODY_SECTIONS = 3
MAX_TOKENS = 1_000
SYSTEM = (
    "You write short YouTube Shorts scripts. Answer with JSON only, exactly "
    '{"body": ["..."], "cta": "..."}: no title, no notes, no other keys.'
)
Clock = Callable[[], datetime]


class HookGenerationNotFoundError(DomainError):
    default_code = "domain.hook_generation_not_found"
    default_user_message = "These hook candidates do not exist."
    default_http_status = HTTPStatus.NOT_FOUND


class ScriptInputError(DomainError):
    default_code = "domain.script_input"
    default_user_message = "A script cannot be written from these inputs."


class ScriptGenerationError(DomainError):
    default_code = "domain.script_generation_failed"
    default_user_message = (
        "No usable script was written. Please try again or change the inputs."
    )

    def __init__(self, detail: str, *, rejections: tuple[str, ...]) -> None:
        super().__init__(detail)
        self.rejections = rejections


@dataclass(frozen=True)
class _Inputs:
    item: ContentItem
    strategy: StrategyProfile
    report: ResearchReport
    hooks: HookGeneration
    hook: str
    target: DurationTarget
    latest: Script | None


class ShortsScriptGenerator:
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
        hook_generation_id: str,
        candidate_index: int,
        *,
        reason: str | None = None,
        actor: Actor,
    ) -> Script:
        inputs = self._inputs(content_item_id, hook_generation_id, candidate_index)
        rejections: list[str] = []
        for _ in range(MAX_ANSWERS):
            request = TextRequest(
                build_prompt(inputs, rejections),
                system=SYSTEM,
                language=inputs.strategy.languages.primary,
                max_tokens=MAX_TOKENS,
            )
            answer = self._call(request, inputs.item, actor, len(rejections))
            sections, problems = check_answer(answer.texts[0], inputs)
            if problems:
                rejections.append("; ".join(problems))
                continue
            return self._store(inputs, sections, answer, reason, actor, rejections)
        self._failed(
            inputs.item,
            actor,
            {"answers": MAX_ANSWERS, "last_rejection": rejections[-1]},
        )
        raise ScriptGenerationError(
            f"all {MAX_ANSWERS} answers were rejected", rejections=tuple(rejections)
        )

    def _inputs(
        self, content_item_id: str, hook_generation_id: str, candidate_index: int
    ) -> _Inputs:
        with self._database.transaction() as connection:
            item = ContentItemRepository(connection).get(content_item_id)
            if item is None:
                raise ContentItemNotFoundError(
                    f"content item {content_item_id} does not exist"
                )
            hooks = HookGenerationRepository(connection).get(hook_generation_id)
            strategy = StrategyProfileRepository(connection).get_by_channel(
                item.channel_id
            )
            report = (
                ResearchReportRepository(connection).get(hooks.research_report_id)
                if hooks
                else None
            )
            scripts = ScriptRepository(connection).list_by_content_item(item.id)
        if item.content_type is not ContentType.SHORTS:
            raise ScriptInputError(f"content item {item.id} is not a Shorts item")
        if hooks is None:
            raise HookGenerationNotFoundError(
                f"hook generation {hook_generation_id} does not exist"
            )
        if hooks.content_item_id != item.id:
            raise ScriptInputError(
                f"hook generation {hooks.id} belongs to another content item"
            )
        if (
            isinstance(candidate_index, bool)
            or not isinstance(candidate_index, int)
            or not 0 <= candidate_index < len(hooks.candidates)
        ):
            raise ScriptInputError(
                f"hook generation {hooks.id} has no candidate {candidate_index!r}"
            )
        if strategy is None:
            raise StrategyNotFoundError(f"channel {item.channel_id} has no strategy")
        if strategy.languages is None or strategy.format is None:
            raise ScriptInputError(
                f"the strategy of channel {item.channel_id} needs a language "
                "and a format"
            )
        if report is None:
            raise ResearchReportNotFoundError(
                f"research report {hooks.research_report_id} does not exist"
            )
        return _Inputs(
            item=item,
            strategy=strategy,
            report=report,
            hooks=hooks,
            hook=hooks.candidates[candidate_index].text,
            target=DurationTarget.from_format(strategy.format, ContentType.SHORTS),
            latest=scripts[-1] if scripts else None,
        )

    def _call(
        self, request: TextRequest, item: ContentItem, actor: Actor, rejected: int
    ) -> GeneratedText:
        def failed(error: TextGenerationError, attempts: int) -> None:
            self._failed(
                item,
                actor,
                {"code": error.code, "attempts": attempts, "rejected": rejected},
            )

        return call_with_retries(
            self._generator, request, sleep=self._sleep, on_failure=failed
        )

    def _store(
        self,
        inputs: _Inputs,
        sections: tuple[ScriptSection, ...],
        answer: GeneratedText,
        reason: str | None,
        actor: Actor,
        rejections: list[str],
    ) -> Script:
        author = Actor(ActorKind.AI, f"{answer.provider}/{answer.model}")
        values: dict[str, Any] = {
            "sections": sections,
            "duration_target": inputs.target,
            "created_by": author,
            "strategy_version": inputs.strategy.version,
            "research_report_id": inputs.report.id,
            "clock": self._clock,
        }
        if inputs.latest is None:
            script = Script.create(
                inputs.item.id,
                reason=reason or "first Shorts script",
                **values,
            )
        else:
            script = inputs.latest.next_version(
                reason=reason or "Shorts script written again", **values
            )
        with self._database.transaction() as connection:
            ScriptRepository(connection).add(script)
        self._audit.record(
            "script.generated",
            actor,
            EntityRef("content_item", inputs.item.id),
            AuditResult.SUCCESS,
            {
                "script_id": script.id,
                "version": script.version,
                "hook_generation_id": inputs.hooks.id,
                "estimated_seconds": script.estimated_seconds,
                "rejected_answers": len(rejections),
                "author": author.id,
            },
        )
        return script

    def _failed(self, item: ContentItem, actor: Actor, metadata: dict) -> None:
        self._audit.record(
            "script.generation_failed",
            actor,
            EntityRef("content_item", item.id),
            AuditResult.FAILURE,
            metadata,
        )


def _topic(inputs: _Inputs) -> ReportTopic | None:
    for topic in inputs.report.topics:
        if topic.topic_id == inputs.hooks.topic_id:
            return topic
    return None


def build_prompt(inputs: _Inputs, rejections: list[str]) -> str:
    """The instructions for one Shorts script around the chosen hook."""
    target = inputs.target
    lines = [
        "Write the rest of a YouTube Shorts script that starts with this hook:",
        f"Hook: {inputs.hook}",
        f"Language: {inputs.strategy.languages.primary}.",
        f"Length: the whole script, hook included, is spoken in "
        f"{target.min_seconds} to {target.max_seconds} seconds "
        f"(about {_words(target.min_seconds)} to {_words(target.max_seconds)} "
        "words).",
        f"Structure: 1 to {MAX_BODY_SECTIONS} short body paragraphs, then one "
        "call to action.",
    ]
    topic = _topic(inputs)
    if topic is not None:
        lines.append(
            f"Topic: {topic.label} (key phrases: {', '.join(topic.keyphrases)})."
        )
    else:
        lines.append(f"Topic: {'; '.join(inputs.report.queries)}.")
    if inputs.hooks.angle:
        lines.append(f"Angle: {inputs.hooks.angle}")
    lines += strategy_lines(inputs.strategy)
    lines += fact_lines(inputs.report)
    lines.append('Answer with JSON only: {"body": ["..."], "cta": "..."}')
    if rejections:
        lines.append("Your last answer was rejected: " + rejections[-1])
    return "\n".join(lines)


def _words(seconds: int) -> int:
    return seconds * WORDS_PER_MINUTE // 60


def parse_answer(text: str) -> tuple[list[str], str]:
    """The body paragraphs and CTA of a JSON answer; ValueError when invalid."""
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else ""
        text = text.rsplit("```", 1)[0]
    try:
        data = json.loads(text)
    except json.JSONDecodeError as error:
        raise ValueError(f"the answer is not JSON ({error.msg})") from None
    if not isinstance(data, dict) or set(data) != {"body", "cta"}:
        raise ValueError('the answer must be an object with "body" and "cta" only')
    body, cta = data["body"], data["cta"]
    if not isinstance(body, list) or not 1 <= len(body) <= MAX_BODY_SECTIONS:
        raise ValueError(f'"body" must list 1 to {MAX_BODY_SECTIONS} paragraphs')
    if not all(isinstance(p, str) and p.strip() for p in [*body, cta]):
        raise ValueError("every paragraph and the call to action must be text")
    return [" ".join(p.split()) for p in body], " ".join(cta.split())


def check_answer(
    text: str, inputs: _Inputs
) -> tuple[tuple[ScriptSection, ...], list[str]]:
    """The script sections of an answer and the problems that reject it."""
    try:
        body, cta = parse_answer(text)
    except ValueError as error:
        return (), [str(error)]
    sections = (
        ScriptSection(SectionKind.HOOK, inputs.hook),
        *(ScriptSection(SectionKind.BODY, paragraph) for paragraph in body),
        ScriptSection(SectionKind.CTA, cta),
    )
    problems = []
    banned = banned_phrases(inputs.strategy)
    found = dict.fromkeys(
        phrase for s in sections for phrase in find_banned_phrases(s.text, banned)
    )
    problems += [f"it uses the banned phrase {phrase!r}" for phrase in found]
    if inputs.latest is not None and sections == inputs.latest.sections:
        problems.append("it repeats the latest script word for word")
    seconds = sum(section.estimated_seconds for section in sections)
    if not inputs.target.contains(seconds):
        problems.append(
            f"it takes about {seconds} seconds, not {inputs.target.min_seconds} "
            f"to {inputs.target.max_seconds}"
        )
    return sections, problems
