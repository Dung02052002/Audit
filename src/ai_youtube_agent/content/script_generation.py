"""Shared parts of the script generators (#066, #067), context C5.

- ``load_inputs`` reads and checks what a script is written from: the content
  item of the expected type, the chosen hook candidate of a stored hook
  generation of the same item, the research report the hooks came from, the
  channel's current strategy (primary language and a format required), the
  duration target of the item's type and the item's latest script.
- ``json_answer`` reads a provider's JSON answer (a code fence around it is
  allowed); ``clean`` collapses spaces; ``banned_problems`` lists the brand
  banned phrases used by any section.
- ``store_script`` stores the accepted sections as version 1 or the next
  version of the latest script, written by the AI actor ``<provider>/<model>``,
  and audits ``script.generated`` for the caller after commit;
  ``record_failure`` audits ``script.generation_failed``.
"""

import json
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
)
from ai_youtube_agent.content.strategy import StrategyProfile
from ai_youtube_agent.content.strategy_settings import StrategyNotFoundError
from ai_youtube_agent.content.text_prompts import banned_phrases
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
from ai_youtube_agent.providers.text_generation import GeneratedText

Clock = Callable[[], datetime]
TYPE_LABELS = {ContentType.SHORTS: "Shorts", ContentType.LONGFORM: "LongForm"}


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
class ScriptInputs:
    item: ContentItem
    strategy: StrategyProfile
    report: ResearchReport
    hooks: HookGeneration
    hook: str
    target: DurationTarget
    latest: Script | None

    @property
    def topic(self) -> ReportTopic | None:
        for topic in self.report.topics:
            if topic.topic_id == self.hooks.topic_id:
                return topic
        return None

    def topic_line(self) -> str:
        topic = self.topic
        if topic is not None:
            return f"Topic: {topic.label} (key phrases: {', '.join(topic.keyphrases)})."
        return f"Topic: {'; '.join(self.report.queries)}."


def load_inputs(
    database: Database,
    content_item_id: str,
    hook_generation_id: str,
    candidate_index: int,
    content_type: ContentType,
) -> ScriptInputs:
    with database.transaction() as connection:
        item = ContentItemRepository(connection).get(content_item_id)
        if item is None:
            raise ContentItemNotFoundError(
                f"content item {content_item_id} does not exist"
            )
        hooks = HookGenerationRepository(connection).get(hook_generation_id)
        strategy = StrategyProfileRepository(connection).get_by_channel(item.channel_id)
        report = (
            ResearchReportRepository(connection).get(hooks.research_report_id)
            if hooks
            else None
        )
        scripts = ScriptRepository(connection).list_by_content_item(item.id)
    if item.content_type is not content_type:
        raise ScriptInputError(
            f"content item {item.id} is not a {TYPE_LABELS[content_type]} item"
        )
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
            f"the strategy of channel {item.channel_id} needs a language and a format"
        )
    if report is None:
        raise ResearchReportNotFoundError(
            f"research report {hooks.research_report_id} does not exist"
        )
    return ScriptInputs(
        item=item,
        strategy=strategy,
        report=report,
        hooks=hooks,
        hook=hooks.candidates[candidate_index].text,
        target=DurationTarget.from_format(strategy.format, content_type),
        latest=scripts[-1] if scripts else None,
    )


def words(seconds: int) -> int:
    """The words spoken in ``seconds`` at ``WORDS_PER_MINUTE``."""
    return seconds * WORDS_PER_MINUTE // 60


def clean(text: str) -> str:
    return " ".join(text.split())


def json_answer(text: str) -> Any:
    """The JSON value of an answer, a code fence allowed; ValueError if invalid."""
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else ""
        text = text.rsplit("```", 1)[0]
    try:
        return json.loads(text)
    except json.JSONDecodeError as error:
        raise ValueError(f"the answer is not JSON ({error.msg})") from None


def banned_problems(texts: list[str], strategy: StrategyProfile) -> list[str]:
    banned = banned_phrases(strategy)
    found = dict.fromkeys(
        phrase for text in texts for phrase in find_banned_phrases(text, banned)
    )
    return [f"it uses the banned phrase {phrase!r}" for phrase in found]


def store_script(
    database: Database,
    audit: AuditLog,
    inputs: ScriptInputs,
    sections: tuple[ScriptSection, ...],
    answer: GeneratedText,
    *,
    reason: str,
    actor: Actor,
    metadata: dict[str, Any],
    clock: Clock | None,
) -> Script:
    author = Actor(ActorKind.AI, f"{answer.provider}/{answer.model}")
    values: dict[str, Any] = {
        "sections": sections,
        "duration_target": inputs.target,
        "created_by": author,
        "strategy_version": inputs.strategy.version,
        "research_report_id": inputs.report.id,
        "reason": reason,
        "clock": clock,
    }
    if inputs.latest is None:
        script = Script.create(inputs.item.id, **values)
    else:
        script = inputs.latest.next_version(**values)
    with database.transaction() as connection:
        ScriptRepository(connection).add(script)
    audit.record(
        "script.generated",
        actor,
        EntityRef("content_item", inputs.item.id),
        AuditResult.SUCCESS,
        {
            "script_id": script.id,
            "version": script.version,
            "hook_generation_id": inputs.hooks.id,
            "estimated_seconds": script.estimated_seconds,
            **metadata,
            "author": author.id,
        },
    )
    return script


def record_failure(
    audit: AuditLog, item: ContentItem, actor: Actor, metadata: dict[str, Any]
) -> None:
    audit.record(
        "script.generation_failed",
        actor,
        EntityRef("content_item", item.id),
        AuditResult.FAILURE,
        metadata,
    )
