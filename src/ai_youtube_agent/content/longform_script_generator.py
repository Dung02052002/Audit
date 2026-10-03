"""LongForm Script Generator (Prompt Pack v8, prompt #067), context C5.

``LongFormScriptGenerator`` writes a structured LongForm script through the
``TextGenerator`` provider. The rules were approved by the user on 2026-10-03:

- While ``LONGFORM_ENABLED`` is off (the default until #237) every call is
  refused with ``LongFormDisabledError`` before anything is read or asked.
- Inputs are those of the Shorts generator (#066) for a LongForm item: a
  chosen hook candidate of the same item (the HOOK section word for word),
  the research report the hooks came from and the current strategy (primary
  language and a format); the duration target is the LongForm format's.
- A LongForm script is HOOK, INTRO, 3 to 12 CHAPTERs with titles, OUTRO and
  CTA, all required. When the format has ``chapters`` off, the chapters are
  stored as BODY sections without titles.
- The provider writes in two steps, because one answer is capped at 8,000
  tokens: first an outline as JSON ``{"intro", "chapters": [{"title",
  "points"}], "outro", "cta"}`` (intro, outro and CTA in full), then one call
  per chapter answering ``{"text": "..."}``.
- The F-066 checks are reused: shape, no brand banned phrase, the duration
  within the format target at 150 words per minute and no word-for-word
  repeat of the latest script. The duration is split over the chapters: the
  outline fixes the seconds of the hook, intro, outro and CTA, and each
  chapter must take its share of the rest (at most 1,200 seconds, about 3,000
  words, so it fits one answer), so the chapters together keep the whole
  script within the target. An outline whose fixed parts leave no valid share
  is rejected. Each outline or chapter answer that fails is asked again with
  the reasons, at most 3 answers each; a retryable provider error is tried up
  to 3 times per answer.
- Every section stores its own estimate as ``seconds``; there are no
  timestamps. Claims are not checked (#068-#070); full validation is #072.
- Storing and auditing follow #066: version 1 or the next version by the AI
  actor ``<provider>/<model>``, ``script.generated`` after commit. When an
  outline or a chapter has 3 rejected answers, or the finished script repeats
  the latest one, nothing is stored, ``script.generation_failed`` is audited
  with the stage and ``ScriptGenerationError`` raised; a provider failure is
  audited the same way and re-raised.
"""

import math
import time
from collections.abc import Callable
from dataclasses import dataclass
from http import HTTPStatus

from ai_youtube_agent.content.script import (
    MAX_TITLE,
    Script,
    ScriptSection,
    SectionKind,
)
from ai_youtube_agent.content.script_generation import (
    Clock,
    ScriptGenerationError,
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
from ai_youtube_agent.core.content_item import ContentType
from ai_youtube_agent.core.db.database import Database
from ai_youtube_agent.core.errors import DomainError
from ai_youtube_agent.core.flags import FeatureFlags
from ai_youtube_agent.providers.text_generation import (
    GeneratedText,
    TextGenerationError,
    TextGenerator,
    TextRequest,
)

MAX_ANSWERS = 3
MIN_CHAPTERS = 3
MAX_CHAPTERS = 12
MAX_POINTS = 5
MAX_CHAPTER_SECONDS = 1_200
OUTLINE_TOKENS = 2_000
CHAPTER_TOKENS = 8_000
OUTLINE_KEYS = {"intro", "chapters", "outro", "cta"}
OUTLINE_JSON = (
    '{"intro": "...", "chapters": [{"title": "...", "points": ["..."]}], '
    '"outro": "...", "cta": "..."}'
)
OUTLINE_SYSTEM = (
    "You plan long YouTube video scripts. Answer with JSON only, exactly "
    f"{OUTLINE_JSON}: no notes, no other keys."
)
CHAPTER_SYSTEM = (
    "You write one chapter of a long YouTube video script. Answer with JSON "
    'only, exactly {"text": "..."}: no title, no notes, no other keys.'
)


class LongFormDisabledError(DomainError):
    default_code = "domain.longform_disabled"
    default_user_message = "LongForm is switched off, so no LongForm script is written."
    default_http_status = HTTPStatus.CONFLICT


@dataclass(frozen=True)
class OutlineChapter:
    title: str
    points: tuple[str, ...]


@dataclass(frozen=True)
class Outline:
    intro: str
    chapters: tuple[OutlineChapter, ...]
    outro: str
    cta: str


@dataclass(frozen=True)
class ChapterShare:
    """The seconds each chapter may take so the whole script fits the target."""

    min_seconds: int
    max_seconds: int


class _Rejected(Exception):
    def __init__(self, stage: str, rejections: list[str], chapter: int | None):
        super().__init__(rejections[-1])
        self.stage = stage
        self.rejections = rejections
        self.chapter = chapter


class LongFormScriptGenerator:
    def __init__(
        self,
        database: Database,
        generator: TextGenerator,
        audit: AuditLog,
        flags: FeatureFlags,
        *,
        clock: Clock | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._database = database
        self._generator = generator
        self._audit = audit
        self._flags = flags
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
        if not self._flags.longform_enabled:
            raise LongFormDisabledError("LONGFORM_ENABLED is off")
        inputs = load_inputs(
            self._database,
            content_item_id,
            hook_generation_id,
            candidate_index,
            ContentType.LONGFORM,
        )
        run = _Run(self, inputs, actor)
        try:
            outline, share = run.outline()
            texts = [
                run.chapter(outline, share, index)
                for index in range(len(outline.chapters))
            ]
            sections = build_sections(inputs, outline, texts)
            if inputs.latest is not None and sections == inputs.latest.sections:
                raise _Rejected(
                    "script", ["it repeats the latest script word for word"], None
                )
        except _Rejected as rejected:
            metadata = {
                "stage": rejected.stage,
                "answers": len(rejected.rejections),
                "last_rejection": rejected.rejections[-1],
            }
            if rejected.chapter is not None:
                metadata["chapter"] = rejected.chapter
            record_failure(self._audit, inputs.item, actor, metadata)
            raise ScriptGenerationError(
                f"the {rejected.stage} was rejected: {rejected.rejections[-1]}",
                rejections=tuple(rejected.rejections),
            ) from None
        return store_script(
            self._database,
            self._audit,
            inputs,
            sections,
            run.last_answer,
            reason=reason
            or (
                "first LongForm script"
                if inputs.latest is None
                else "LongForm script written again"
            ),
            actor=actor,
            metadata={
                "chapters": len(outline.chapters),
                "answers": run.answers,
                "rejected_answers": run.rejected,
            },
            clock=self._clock,
        )


class _Run:
    """One generation: asks for the outline, then each chapter."""

    def __init__(
        self, owner: LongFormScriptGenerator, inputs: ScriptInputs, actor: Actor
    ) -> None:
        self._owner = owner
        self._inputs = inputs
        self._actor = actor
        self.answers = 0
        self.rejected = 0
        self.last_answer: GeneratedText

    def outline(self) -> tuple[Outline, ChapterShare]:
        rejections: list[str] = []
        for _ in range(MAX_ANSWERS):
            prompt = outline_prompt(self._inputs, rejections)
            answer = self._ask(prompt, OUTLINE_SYSTEM, OUTLINE_TOKENS, "outline")
            try:
                outline = parse_outline(answer.texts[0])
            except ValueError as error:
                problems = [str(error)]
            else:
                share, problems = check_outline(outline, self._inputs)
            if not problems:
                return outline, share
            rejections.append("; ".join(problems))
            self.rejected += 1
        raise _Rejected("outline", rejections, None)

    def chapter(self, outline: Outline, share: ChapterShare, index: int) -> str:
        rejections: list[str] = []
        for _ in range(MAX_ANSWERS):
            prompt = chapter_prompt(self._inputs, outline, share, index, rejections)
            answer = self._ask(
                prompt, CHAPTER_SYSTEM, CHAPTER_TOKENS, "chapter", index + 1
            )
            text, problems = check_chapter(answer.texts[0], share, self._inputs)
            if not problems:
                return text
            rejections.append("; ".join(problems))
            self.rejected += 1
        raise _Rejected("chapter", rejections, index + 1)

    def _ask(
        self,
        prompt: str,
        system: str,
        max_tokens: int,
        stage: str,
        chapter: int | None = None,
    ) -> GeneratedText:
        owner = self._owner
        request = TextRequest(
            prompt,
            system=system,
            language=self._inputs.strategy.languages.primary,
            max_tokens=max_tokens,
        )

        def failed(error: TextGenerationError, attempts: int) -> None:
            metadata = {
                "stage": stage,
                "code": error.code,
                "attempts": attempts,
                "rejected": self.rejected,
            }
            if chapter is not None:
                metadata["chapter"] = chapter
            record_failure(owner._audit, self._inputs.item, self._actor, metadata)

        answer = call_with_retries(
            owner._generator, request, sleep=owner._sleep, on_failure=failed
        )
        self.answers += 1
        self.last_answer = answer
        return answer


def _intro_lines(inputs: ScriptInputs) -> list[str]:
    target = inputs.target
    lines = [
        f"Hook: {inputs.hook}",
        f"Language: {inputs.strategy.languages.primary}.",
        f"Length: the whole script, hook included, is spoken in "
        f"{target.min_seconds} to {target.max_seconds} seconds "
        f"(about {words(target.min_seconds)} to {words(target.max_seconds)} "
        "words).",
        inputs.topic_line(),
    ]
    if inputs.hooks.angle:
        lines.append(f"Angle: {inputs.hooks.angle}")
    return lines


def outline_prompt(inputs: ScriptInputs, rejections: list[str]) -> str:
    """The instructions for the outline of a LongForm script."""
    lines = [
        "Plan a long YouTube video script that starts with this hook:",
        *_intro_lines(inputs),
        f"Structure: a short intro, {MIN_CHAPTERS} to {MAX_CHAPTERS} chapters, "
        "a short outro and one call to action. Write the intro, outro and call "
        f"to action in full; give each chapter a title (at most {MAX_TITLE} "
        f"characters) and 1 to {MAX_POINTS} points it will cover. Keep the "
        "intro, outro and call to action short so the chapters hold most of "
        "the time.",
        *strategy_lines(inputs.strategy),
        *fact_lines(inputs.report),
        f"Answer with JSON only: {OUTLINE_JSON}",
    ]
    if rejections:
        lines.append("Your last answer was rejected: " + rejections[-1])
    return "\n".join(lines)


def chapter_prompt(
    inputs: ScriptInputs,
    outline: Outline,
    share: ChapterShare,
    index: int,
    rejections: list[str],
) -> str:
    """The instructions for one chapter of the outline."""
    chapter = outline.chapters[index]
    lines = [
        "Write one chapter of a long YouTube video script that starts with this hook:",
        *_intro_lines(inputs),
        "Chapters:",
        *(f"{n}. {c.title}" for n, c in enumerate(outline.chapters, start=1)),
        f"Write chapter {index + 1} of {len(outline.chapters)}: {chapter.title}",
        "Cover these points:",
        *(f"- {point}" for point in chapter.points),
        f"Length: this chapter is spoken in {share.min_seconds} to "
        f"{share.max_seconds} seconds (about {words(share.min_seconds)} to "
        f"{words(share.max_seconds)} words).",
        "Do not repeat the intro, the other chapters or the call to action.",
        *strategy_lines(inputs.strategy),
        *fact_lines(inputs.report),
        'Answer with JSON only: {"text": "..."}',
    ]
    if rejections:
        lines.append("Your last answer was rejected: " + rejections[-1])
    return "\n".join(lines)


def _text(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("every text in the answer must be non-empty text")
    return clean(value)


def parse_outline(text: str) -> Outline:
    """The outline of a JSON answer; ValueError when its shape is invalid."""
    data = json_answer(text)
    if not isinstance(data, dict) or set(data) != OUTLINE_KEYS:
        raise ValueError(
            'the answer must be an object with "intro", "chapters", "outro" '
            'and "cta" only'
        )
    chapters = data["chapters"]
    if not isinstance(chapters, list) or not (
        MIN_CHAPTERS <= len(chapters) <= MAX_CHAPTERS
    ):
        raise ValueError(
            f'"chapters" must list {MIN_CHAPTERS} to {MAX_CHAPTERS} chapters'
        )
    parsed = []
    for chapter in chapters:
        if not isinstance(chapter, dict) or set(chapter) != {"title", "points"}:
            raise ValueError('every chapter must have "title" and "points" only')
        title = _text(chapter["title"])
        if len(title) > MAX_TITLE:
            raise ValueError(f"a chapter title is at most {MAX_TITLE} characters")
        points = chapter["points"]
        if not isinstance(points, list) or not 1 <= len(points) <= MAX_POINTS:
            raise ValueError(f'every chapter must list 1 to {MAX_POINTS} "points"')
        parsed.append(OutlineChapter(title, tuple(_text(p) for p in points)))
    return Outline(
        _text(data["intro"]), tuple(parsed), _text(data["outro"]), _text(data["cta"])
    )


def _fixed_sections(inputs: ScriptInputs, outline: Outline) -> list[ScriptSection]:
    return [
        ScriptSection(SectionKind.HOOK, inputs.hook),
        ScriptSection(SectionKind.INTRO, outline.intro),
        ScriptSection(SectionKind.OUTRO, outline.outro),
        ScriptSection(SectionKind.CTA, outline.cta),
    ]


def chapter_share(inputs: ScriptInputs, outline: Outline) -> ChapterShare | None:
    """Each chapter's seconds, or None when the outline leaves no valid share."""
    fixed = sum(s.estimated_seconds for s in _fixed_sections(inputs, outline))
    count = len(outline.chapters)
    low = max(1, math.ceil((inputs.target.min_seconds - fixed) / count))
    high = min(MAX_CHAPTER_SECONDS, (inputs.target.max_seconds - fixed) // count)
    return ChapterShare(low, high) if low <= high else None


def check_outline(
    outline: Outline, inputs: ScriptInputs
) -> tuple[ChapterShare, list[str]]:
    """The chapter share of an outline and the problems that reject it."""
    texts = [outline.intro, outline.outro, outline.cta]
    texts += [c.title for c in outline.chapters]
    problems = banned_problems(texts, inputs.strategy)
    share = chapter_share(inputs, outline)
    if share is None:
        fixed = sum(s.estimated_seconds for s in _fixed_sections(inputs, outline))
        problems.append(
            f"the hook, intro, outro and call to action take about {fixed} "
            f"seconds, which leaves {len(outline.chapters)} chapters no length "
            f"between 1 and {MAX_CHAPTER_SECONDS} seconds each within "
            f"{inputs.target.min_seconds} to {inputs.target.max_seconds} seconds"
        )
        share = ChapterShare(1, 1)
    return share, problems


def check_chapter(
    text: str, share: ChapterShare, inputs: ScriptInputs
) -> tuple[str, list[str]]:
    """The cleaned chapter text of an answer and the problems that reject it."""
    try:
        data = json_answer(text)
        if not isinstance(data, dict) or set(data) != {"text"}:
            raise ValueError('the answer must be an object with "text" only')
        chapter = _text(data["text"])
    except ValueError as error:
        return "", [str(error)]
    problems = banned_problems([chapter], inputs.strategy)
    seconds = ScriptSection(SectionKind.BODY, chapter).estimated_seconds
    if not share.min_seconds <= seconds <= share.max_seconds:
        problems.append(
            f"it takes about {seconds} seconds, not {share.min_seconds} "
            f"to {share.max_seconds}"
        )
    return chapter, problems


def build_sections(
    inputs: ScriptInputs, outline: Outline, texts: list[str]
) -> tuple[ScriptSection, ...]:
    """The script sections, each with its estimate as ``seconds``."""
    hook, intro, outro, cta = _fixed_sections(inputs, outline)
    titled = inputs.strategy.format.longform.chapters
    chapters = [
        ScriptSection(SectionKind.CHAPTER, text, chapter.title)
        if titled
        else ScriptSection(SectionKind.BODY, text)
        for chapter, text in zip(outline.chapters, texts, strict=True)
    ]
    return tuple(
        ScriptSection(s.kind, s.text, s.title, s.estimated_seconds)
        for s in (hook, intro, *chapters, outro, cta)
    )
