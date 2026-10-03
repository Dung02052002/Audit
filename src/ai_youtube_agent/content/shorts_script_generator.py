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

Input loading, storing and auditing are shared with the LongForm generator
(#067) in ``content/script_generation.py``.
"""

import time
from collections.abc import Callable

from ai_youtube_agent.content.script import Script, ScriptSection, SectionKind
from ai_youtube_agent.content.script_generation import (
    Clock,
    HookGenerationNotFoundError,
    ScriptGenerationError,
    ScriptInputError,
    ScriptInputs,
    banned_problems,
    clean,
    json_answer,
    load_inputs,
    record_failure,
    store_script,
    words,
)
from ai_youtube_agent.content.text_prompts import (
    call_with_retries,
    fact_lines,
    strategy_lines,
)
from ai_youtube_agent.core.audit import Actor, AuditLog
from ai_youtube_agent.core.content_item import ContentItem, ContentType
from ai_youtube_agent.core.db.database import Database
from ai_youtube_agent.providers.text_generation import (
    GeneratedText,
    TextGenerationError,
    TextGenerator,
    TextRequest,
)

__all__ = [
    "HookGenerationNotFoundError",
    "ScriptGenerationError",
    "ScriptInputError",
    "ShortsScriptGenerator",
    "build_prompt",
    "check_answer",
    "parse_answer",
]

MAX_ANSWERS = 3
MAX_BODY_SECTIONS = 3
MAX_TOKENS = 1_000
SYSTEM = (
    "You write short YouTube Shorts scripts. Answer with JSON only, exactly "
    '{"body": ["..."], "cta": "..."}: no title, no notes, no other keys.'
)


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
        inputs = load_inputs(
            self._database,
            content_item_id,
            hook_generation_id,
            candidate_index,
            ContentType.SHORTS,
        )
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
            return store_script(
                self._database,
                self._audit,
                inputs,
                sections,
                answer,
                reason=reason
                or (
                    "first Shorts script"
                    if inputs.latest is None
                    else "Shorts script written again"
                ),
                actor=actor,
                metadata={"rejected_answers": len(rejections)},
                clock=self._clock,
            )
        record_failure(
            self._audit,
            inputs.item,
            actor,
            {"answers": MAX_ANSWERS, "last_rejection": rejections[-1]},
        )
        raise ScriptGenerationError(
            f"all {MAX_ANSWERS} answers were rejected", rejections=tuple(rejections)
        )

    def _call(
        self, request: TextRequest, item: ContentItem, actor: Actor, rejected: int
    ) -> GeneratedText:
        def failed(error: TextGenerationError, attempts: int) -> None:
            record_failure(
                self._audit,
                item,
                actor,
                {"code": error.code, "attempts": attempts, "rejected": rejected},
            )

        return call_with_retries(
            self._generator, request, sleep=self._sleep, on_failure=failed
        )


def build_prompt(inputs: ScriptInputs, rejections: list[str]) -> str:
    """The instructions for one Shorts script around the chosen hook."""
    target = inputs.target
    lines = [
        "Write the rest of a YouTube Shorts script that starts with this hook:",
        f"Hook: {inputs.hook}",
        f"Language: {inputs.strategy.languages.primary}.",
        f"Length: the whole script, hook included, is spoken in "
        f"{target.min_seconds} to {target.max_seconds} seconds "
        f"(about {words(target.min_seconds)} to {words(target.max_seconds)} "
        "words).",
        f"Structure: 1 to {MAX_BODY_SECTIONS} short body paragraphs, then one "
        "call to action.",
        inputs.topic_line(),
    ]
    if inputs.hooks.angle:
        lines.append(f"Angle: {inputs.hooks.angle}")
    lines += strategy_lines(inputs.strategy)
    lines += fact_lines(inputs.report)
    lines.append('Answer with JSON only: {"body": ["..."], "cta": "..."}')
    if rejections:
        lines.append("Your last answer was rejected: " + rejections[-1])
    return "\n".join(lines)


def parse_answer(text: str) -> tuple[list[str], str]:
    """The body paragraphs and CTA of a JSON answer; ValueError when invalid."""
    data = json_answer(text)
    if not isinstance(data, dict) or set(data) != {"body", "cta"}:
        raise ValueError('the answer must be an object with "body" and "cta" only')
    body, cta = data["body"], data["cta"]
    if not isinstance(body, list) or not 1 <= len(body) <= MAX_BODY_SECTIONS:
        raise ValueError(f'"body" must list 1 to {MAX_BODY_SECTIONS} paragraphs')
    if not all(isinstance(p, str) and p.strip() for p in [*body, cta]):
        raise ValueError("every paragraph and the call to action must be text")
    return [clean(p) for p in body], clean(cta)


def check_answer(
    text: str, inputs: ScriptInputs
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
    problems = banned_problems([s.text for s in sections], inputs.strategy)
    if inputs.latest is not None and sections == inputs.latest.sections:
        problems.append("it repeats the latest script word for word")
    seconds = sum(section.estimated_seconds for section in sections)
    if not inputs.target.contains(seconds):
        problems.append(
            f"it takes about {seconds} seconds, not {inputs.target.min_seconds} "
            f"to {inputs.target.max_seconds}"
        )
    return sections, problems
