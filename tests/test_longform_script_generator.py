"""F-067 LongForm Script Generator (Prompt Pack v8, prompt #067).

Rules the user approved on 2026-10-03:

- while ``LONGFORM_ENABLED`` is off, generation is refused before anything
  is read or asked;
- a LongForm script is HOOK + INTRO + 3-12 titled CHAPTERs + OUTRO + CTA
  (chapters become untitled BODY sections when the format has chapters off);
- the provider writes an outline first (intro, chapter titles + points,
  outro, CTA), then one chapter per call;
- the F-066 checks are reused (shape, banned phrases, duration at 150 wpm,
  no repeat), each section stores its own estimate as ``seconds``; each
  outline or chapter answer is re-asked at most 3 times.
"""

import json

import pytest

from ai_youtube_agent.bootstrap import build_container
from ai_youtube_agent.content.longform_script_generator import (
    MAX_CHAPTER_SECONDS,
    ChapterShare,
    LongFormDisabledError,
    LongFormScriptGenerator,
    Outline,
    OutlineChapter,
    chapter_share,
    parse_outline,
)
from ai_youtube_agent.content.script import DurationTarget, Script, SectionKind
from ai_youtube_agent.content.script_generation import (
    ScriptGenerationError,
    ScriptInputError,
    load_inputs,
)
from ai_youtube_agent.content.strategy import (
    FormatSettings,
    LongFormFormat,
    ShortsFormat,
)
from ai_youtube_agent.core.audit import Actor, ActorKind, AuditLog, AuditResult
from ai_youtube_agent.core.config import Environment, Settings
from ai_youtube_agent.core.content_item import ContentType
from ai_youtube_agent.core.db.database import Database
from ai_youtube_agent.core.db.repositories.content import ScriptRepository
from ai_youtube_agent.core.flags import FeatureFlags
from ai_youtube_agent.providers.text_generation import (
    TextErrorCode,
    TextGenerationError,
)
from test_hook_generator import USER, World

HOOK = "Bank fees eat your savings. Here is how to stop them."
TITLES = ("Where fees hide", "How to compare banks", "Switching safely")


def words(n: int, word: str = "fees") -> str:
    return " ".join([word] * n) + "."


def outline(
    titles=TITLES,
    intro: str = words(20, "intro"),
    outro: str = words(20, "outro"),
    cta: str = "Subscribe for more money tips.",
    points=("one point",),
    **extra,
) -> str:
    chapters = [{"title": t, "points": list(points)} for t in titles]
    return json.dumps(
        {"intro": intro, "chapters": chapters, "outro": outro, "cta": cta, **extra}
    )


def chapter(text: str) -> str:
    return json.dumps({"text": text})


GOOD = chapter(words(500))


class LongFormWorld(World):
    def __init__(self, database: Database, *, enabled: bool = True, **strategy):
        super().__init__(database, **strategy)
        self.scripts = LongFormScriptGenerator(
            database,
            self.text,
            AuditLog(self.sink),
            FeatureFlags(longform_enabled=enabled),
            clock=self.clock,
            sleep=self.sleeps.append,
        )
        self.item = self.add_item(ContentType.LONGFORM)
        self.research = self.report()
        self.text.queue(HOOK, "Do you know what your bank charges?", "Fees add up.")
        self.hooks_run = self.hooks.generate(self.item.id, self.research.id, actor=USER)
        self.text.calls.clear()
        self.first_event = len(self.sink.events())

    def write(self, *answers: str, index: int = 0, **kwargs) -> Script:
        for text in answers:
            self.text.queue(text)
        return self.scripts.generate(
            self.item.id, self.hooks_run.id, index, actor=USER, **kwargs
        )

    def stored(self) -> list[Script]:
        with self.database.transaction() as connection:
            return ScriptRepository(connection).list_by_content_item(self.item.id)

    def events(self):
        return list(self.sink.events())[self.first_event :]


@pytest.fixture
def world(database: Database) -> LongFormWorld:
    return LongFormWorld(database)


# The LongForm flag


def test_generation_is_refused_while_longform_is_off(database) -> None:
    world = LongFormWorld(database, enabled=False)

    with pytest.raises(LongFormDisabledError) as caught:
        world.write(outline(), GOOD, GOOD, GOOD)

    public = caught.value.to_public()
    assert (public.code, public.http_status) == ("domain.longform_disabled", 409)
    assert world.text.calls == []
    assert world.stored() == [] and world.events() == []


def test_the_flag_is_checked_before_the_inputs(database) -> None:
    world = LongFormWorld(database, enabled=False)

    with pytest.raises(LongFormDisabledError):
        world.scripts.generate("missing", "missing", 0, actor=USER)


# Writing a script


def test_a_first_script_is_written_from_an_outline_and_chapters(world) -> None:
    script = world.write(outline(), GOOD, chapter(words(500, "save")), GOOD)

    assert script.version == 1 and script.parent_id is None
    assert [s.kind for s in script.sections] == [
        SectionKind.HOOK,
        SectionKind.INTRO,
        SectionKind.CHAPTER,
        SectionKind.CHAPTER,
        SectionKind.CHAPTER,
        SectionKind.OUTRO,
        SectionKind.CTA,
    ]
    assert script.sections[0].text == HOOK
    assert [s.title for s in script.sections[2:5]] == list(TITLES)
    assert script.sections[3].text == words(500, "save")
    assert script.sections[-1].text == "Subscribe for more money tips."
    assert all(s.seconds is not None for s in script.sections)
    assert script.sections[2].seconds == 200
    assert script.created_by == Actor(ActorKind.AI, "mock/mock-1")
    assert script.reason == "first LongForm script"
    assert script.duration_target == DurationTarget(480, 900)
    assert script.within_target is True
    assert (script.strategy_version, script.research_report_id) == (
        world.strategy.version,
        world.research.id,
    )
    assert world.stored() == [script]
    [event] = world.events()
    assert (event.action, event.result, event.actor) == (
        "script.generated",
        AuditResult.SUCCESS,
        USER,
    )
    assert event.metadata["script_id"] == script.id
    assert event.metadata["chapters"] == 3
    assert event.metadata["answers"] == 4
    assert event.metadata["rejected_answers"] == 0


def test_the_outline_prompt_carries_the_hook_strategy_and_report(world) -> None:
    world.write(outline(), GOOD, GOOD, GOOD)

    request = world.text.calls[0]
    assert (request.language, request.candidates, request.max_tokens) == ("vi", 1, 2000)
    assert "JSON only" in request.system
    for expected in (
        f"Hook: {HOOK}",
        "Language: vi.",
        "spoken in 480 to 900 seconds (about 1200 to 2250 words)",
        "3 to 12 chapters",
        f"Topic: {world.research.topics[0].label}",
        "Tone: calm and clear",
        "Never use: get rich quick; you won't believe.",
        '"chapters": [{"title": "...", "points": ["..."]}]',
    ):
        assert expected in request.prompt


def test_each_chapter_prompt_names_its_chapter_and_share(world) -> None:
    world.write(outline(points=("hidden fees", "fee tables")), GOOD, GOOD, GOOD)

    requests = world.text.calls[1:]
    assert len(requests) == 3
    assert all(r.max_tokens == 8000 for r in requests)
    second = requests[1].prompt
    assert "Write chapter 2 of 3: How to compare banks" in second
    assert "1. Where fees hide\n2. How to compare banks\n3. Switching safely" in second
    assert "- hidden fees\n- fee tables" in second
    # hook 4 s + intro 8 s + outro 8 s + CTA 3 s = 23 s fixed.
    assert "spoken in 153 to 292 seconds (about 382 to 730 words)" in second
    assert '{"text": "..."}' in second


def test_chapters_are_body_sections_when_the_format_has_none(database) -> None:
    format_ = FormatSettings(
        ShortsFormat(15, 60), LongFormFormat(480, 900, chapters=False)
    )
    world = LongFormWorld(database, format=format_)

    script = world.write(outline(), GOOD, GOOD, GOOD)

    middle = script.sections[2:5]
    assert [s.kind for s in middle] == [SectionKind.BODY] * 3
    assert all(s.title is None for s in middle)


def test_another_candidate_and_a_reason_can_be_given(world) -> None:
    first = world.write(outline(), GOOD, GOOD, GOOD)
    second = world.write(outline(), GOOD, GOOD, GOOD, index=1, reason="new hook")

    assert second.sections[0].text == "Do you know what your bank charges?"
    assert (second.version, second.parent_id, second.reason) == (
        2,
        first.id,
        "new hook",
    )


# Rejected answers


@pytest.mark.parametrize(
    ("bad", "reason"),
    [
        ("Here is the plan!", "not JSON"),
        (outline(notes="x"), '"intro", "chapters", "outro" and "cta" only'),
        (outline(titles=TITLES[:2]), "3 to 12 chapters"),
        (outline(titles=[f"Part {n}" for n in range(13)]), "3 to 12 chapters"),
        (outline(titles=["x" * 101, "b", "c"]), "at most 100 characters"),
        (outline(points=()), '1 to 5 "points"'),
        (outline(points=("p",) * 6), '1 to 5 "points"'),
        (outline(intro=" "), "non-empty text"),
        (
            outline(titles=["Get rich quick", "b", "c"]),
            "banned phrase 'get rich quick'",
        ),
        (outline(intro=words(2300)), "leaves 3 chapters no length"),
    ],
)
def test_a_failing_outline_is_asked_again(world, bad, reason) -> None:
    script = world.write(bad, outline(), GOOD, GOOD, GOOD)

    assert script.version == 1
    retry = world.text.calls[1].prompt
    assert "Your last answer was rejected: " in retry and reason in retry
    assert world.events()[-1].metadata["rejected_answers"] == 1


@pytest.mark.parametrize(
    ("bad", "reason"),
    [
        ("nope", "not JSON"),
        (json.dumps({"text": "a", "title": "b"}), '"text" only'),
        (chapter(words(100)), "seconds, not 153 to 292"),
        (chapter(words(1000)), "seconds, not 153 to 292"),
        (chapter("You won't believe " + words(500)), "banned phrase"),
    ],
)
def test_a_failing_chapter_is_asked_again(world, bad, reason) -> None:
    script = world.write(outline(), GOOD, bad, GOOD, GOOD)

    assert len(script.sections) == 7
    retry = world.text.calls[3].prompt
    assert "Write chapter 2 of 3" in retry
    assert "Your last answer was rejected: " in retry and reason in retry
    assert world.events()[-1].metadata["rejected_answers"] == 1


def test_three_rejected_outlines_store_nothing(world) -> None:
    with pytest.raises(ScriptGenerationError) as caught:
        world.write("no", outline(titles=TITLES[:1]), outline(points=()))

    assert len(caught.value.rejections) == 3
    assert caught.value.to_public().code == "domain.script_generation_failed"
    assert world.stored() == [] and len(world.text.calls) == 3
    [event] = world.events()
    assert (event.action, event.result) == (
        "script.generation_failed",
        AuditResult.FAILURE,
    )
    assert event.metadata == {
        "stage": "outline",
        "answers": 3,
        "last_rejection": caught.value.rejections[-1],
    }


def test_three_rejected_answers_for_a_chapter_store_nothing(world) -> None:
    short = chapter(words(10))

    with pytest.raises(ScriptGenerationError):
        world.write(outline(), GOOD, short, short, short)

    assert world.stored() == [] and len(world.text.calls) == 5
    [event] = world.events()
    assert (event.metadata["stage"], event.metadata["chapter"]) == ("chapter", 2)
    assert event.metadata["answers"] == 3


def test_a_script_repeating_the_latest_is_not_stored(world) -> None:
    world.write(outline(), GOOD, GOOD, GOOD)

    with pytest.raises(ScriptGenerationError) as caught:
        world.write(outline(), GOOD, GOOD, GOOD)

    assert "repeats the latest script" in caught.value.rejections[-1]
    assert len(world.stored()) == 1
    assert world.events()[-1].metadata["stage"] == "script"


# Inputs


def test_the_item_must_be_longform(world) -> None:
    shorts = world.add_item(ContentType.SHORTS)
    world.text.queue(HOOK)
    hooks = world.hooks.generate(shorts.id, world.research.id, actor=USER)
    world.text.calls.clear()

    with pytest.raises(ScriptInputError, match="not a LongForm item"):
        world.scripts.generate(shorts.id, hooks.id, 0, actor=USER)
    with pytest.raises(ScriptInputError):
        world.scripts.generate(world.item.id, world.hooks_run.id, 3, actor=USER)
    assert world.text.calls == []


def test_the_target_is_the_longform_format(world) -> None:
    inputs = load_inputs(
        world.database, world.item.id, world.hooks_run.id, 0, ContentType.LONGFORM
    )
    assert inputs.target == DurationTarget(480, 900)


# Chapter share


def _outline(count: int) -> Outline:
    return Outline(
        "intro",
        tuple(OutlineChapter(f"c{n}", ("p",)) for n in range(count)),
        "outro",
        "cta",
    )


def test_a_chapter_share_is_capped_to_fit_one_answer(database) -> None:
    format_ = FormatSettings(ShortsFormat(15, 60), LongFormFormat(3_600, 14_400))
    world = LongFormWorld(database, format=format_)
    inputs = load_inputs(
        world.database, world.item.id, world.hooks_run.id, 0, ContentType.LONGFORM
    )

    # 7 s fixed: 3 chapters need 1,198-4,797 s each, capped at 1,200.
    assert chapter_share(inputs, _outline(3)) == ChapterShare(1198, MAX_CHAPTER_SECONDS)
    # 2 chapters would need 1,797 s each, above the cap.
    assert chapter_share(inputs, _outline(2)) is None


def test_chapter_shares_keep_the_whole_script_within_the_target(world) -> None:
    inputs = load_inputs(
        world.database, world.item.id, world.hooks_run.id, 0, ContentType.LONGFORM
    )
    share = chapter_share(inputs, _outline(3))
    # hook 4 s, intro/outro/cta 1 s each = 7 s fixed.
    assert share == ChapterShare(158, 297)
    assert 7 + 3 * share.min_seconds >= 480 and 7 + 3 * share.max_seconds <= 900


# Provider failures


def test_retryable_provider_errors_are_retried(world) -> None:
    world.text.fail_next(TextErrorCode.TIMEOUT, times=2)

    script = world.write(outline(), GOOD, GOOD, GOOD)

    assert script.version == 1 and world.sleeps == [1.0, 2.0]


def test_a_provider_failure_in_a_chapter_is_audited_and_raised(world) -> None:
    class FailThirdCall:
        def __init__(self, inner) -> None:
            self.inner = inner

        def generate(self, request):
            if len(self.inner.calls) == 2:
                world.text.fail_next(TextErrorCode.REFUSED)
            return self.inner.generate(request)

    scripts = LongFormScriptGenerator(
        world.database,
        FailThirdCall(world.text),
        AuditLog(world.sink),
        FeatureFlags(longform_enabled=True),
        sleep=world.sleeps.append,
    )
    world.text.queue(outline())
    world.text.queue(chapter(words(10)))

    with pytest.raises(TextGenerationError):
        scripts.generate(world.item.id, world.hooks_run.id, 0, actor=USER)

    [event] = world.events()
    assert (event.action, event.result) == (
        "script.generation_failed",
        AuditResult.FAILURE,
    )
    assert event.metadata == {
        "stage": "chapter",
        "code": "text.refused",
        "attempts": 1,
        "rejected": 1,
        "chapter": 1,
    }
    assert world.stored() == []


def test_bootstrap_registers_the_generator_with_longform_off(tmp_path) -> None:
    container = build_container(
        Settings(environment=Environment.TEST, database_path=tmp_path / "a.db")
    )
    scripts = container.resolve(LongFormScriptGenerator)
    with pytest.raises(LongFormDisabledError):
        scripts.generate("item", "hooks", 0, actor=USER)


def test_parse_outline_cleans_spacing_and_fences() -> None:
    parsed = parse_outline("```json\n" + outline(intro="  a\n b ") + "\n```")

    assert parsed.intro == "a b"
    assert [c.title for c in parsed.chapters] == list(TITLES)
    assert parsed.chapters[0].points == ("one point",)
