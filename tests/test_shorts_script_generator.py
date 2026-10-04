"""F-066 Shorts Script Generator (Prompt Pack v8, prompt #066).

Rules the user approved on 2026-10-03:

- the caller names a stored hook candidate (generation id + index) of the
  same Shorts item; its text is the HOOK section word for word;
- a Shorts script is HOOK + 1-3 BODY + 1 CTA, answered by the provider as
  JSON ``{"body": [...], "cta": "..."}``;
- before storing: the answer parses, no banned phrase, the estimate is within
  the strategy's Shorts format target; a failing answer is re-asked with the
  reasons, at most 3 answers; claims are not checked (#068-#070);
- the result is Script version 1 or the next version, written by the AI actor
  ``<provider>/<model>`` with strategy version, report and duration target;
  ``script.generated`` / ``script.generation_failed`` are audited.
"""

import json

import pytest

from ai_youtube_agent.bootstrap import build_container
from ai_youtube_agent.content.hook_generator import ContentItemNotFoundError
from ai_youtube_agent.content.script import (
    DurationTarget,
    Script,
    ScriptSection,
    SectionKind,
)
from ai_youtube_agent.content.script_generation import ScriptConflictError
from ai_youtube_agent.content.shorts_script_generator import (
    HookGenerationNotFoundError,
    ScriptGenerationError,
    ScriptInputError,
    ShortsScriptGenerator,
    parse_answer,
)
from ai_youtube_agent.content.text_prompts import NO_FACTS
from ai_youtube_agent.core.audit import Actor, ActorKind, AuditLog, AuditResult
from ai_youtube_agent.core.config import Environment, Settings
from ai_youtube_agent.core.content_item import ContentType
from ai_youtube_agent.core.db.database import Database
from ai_youtube_agent.core.db.repositories.content import ScriptRepository
from ai_youtube_agent.providers.text_generation import (
    TextErrorCode,
    TextGenerationError,
)
from test_hook_generator import USER, World

HOOK = "Bank fees eat your savings."
CTA = "Follow for one money tip a day."


def words(n: int, word: str = "fees") -> str:
    return " ".join([word] * n) + "."


def answer(*body: str, cta: str = CTA, **extra) -> str:
    return json.dumps({"body": list(body), "cta": cta, **extra})


class ShortsWorld(World):
    def __init__(self, database: Database, **strategy) -> None:
        super().__init__(database, **strategy)
        self.scripts = ShortsScriptGenerator(
            database,
            self.text,
            AuditLog(self.sink),
            clock=self.clock,
            sleep=self.sleeps.append,
        )
        self.research = self.report()
        self.text.queue(HOOK, "Ever checked your bank fees?", "Fees add up.")
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
def world(database: Database) -> ShortsWorld:
    return ShortsWorld(database)


# Writing a script


def test_a_first_script_is_written_around_the_chosen_hook(world) -> None:
    script = world.write(answer(words(20), words(20, "save")))

    assert script.version == 1 and script.parent_id is None
    assert [s.kind for s in script.sections] == [
        SectionKind.HOOK,
        SectionKind.BODY,
        SectionKind.BODY,
        SectionKind.CTA,
    ]
    assert script.sections[0].text == HOOK
    assert script.sections[-1].text == CTA
    assert script.created_by == Actor(ActorKind.AI, "mock/mock-1")
    assert script.reason == "first Shorts script"
    assert script.duration_target == DurationTarget(15, 60)
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
    assert event.metadata["hook_generation_id"] == world.hooks_run.id
    assert event.metadata["rejected_answers"] == 0


def test_the_prompt_carries_the_hook_strategy_and_report(world) -> None:
    world.write(answer(words(40)))

    [request] = world.text.calls
    assert (request.language, request.candidates, request.max_tokens) == ("vi", 1, 1000)
    assert "JSON only" in request.system
    for expected in (
        f"Hook: {HOOK}",
        "Language: vi.",
        "spoken in 15 to 60 seconds (about 37 to 150 words)",
        "1 to 3 short body paragraphs, then one call to action",
        f"Topic: {world.research.topics[0].label}",
        "Tone: calm and clear",
        "Never use: get rich quick; you won't believe.",
        NO_FACTS,
        '{"body": ["..."], "cta": "..."}',
    ):
        assert expected in request.prompt


def test_another_candidate_and_a_reason_can_be_given(world) -> None:
    first = world.write(answer(words(40)))
    second = world.write(answer(words(40, "save")), index=1, reason="new hook")

    assert second.sections[0].text == "Ever checked your bank fees?"
    assert (second.version, second.parent_id, second.reason) == (
        2,
        first.id,
        "new hook",
    )
    assert world.stored() == [first, second]


def test_a_code_fence_around_the_json_is_accepted(world) -> None:
    script = world.write("```json\n" + answer(words(40)) + "\n```")

    assert len(script.sections) == 3


# Rejected answers


@pytest.mark.parametrize(
    ("bad", "reason"),
    [
        ("Here is your script!", "not JSON"),
        (json.dumps(["a"]), '"body" and "cta" only'),
        (answer(words(40), title="x"), '"body" and "cta" only'),
        (answer(), "1 to 3 paragraphs"),
        (answer("a", "b", "c", "d"), "1 to 3 paragraphs"),
        (answer(words(40), cta=" "), "must be text"),
        (
            answer("Get rich quick with this " + words(40)),
            "banned phrase 'get rich quick'",
        ),
        (answer(words(10)), "seconds, not 15 to 60"),
        (answer(words(200)), "seconds, not 15 to 60"),
    ],
)
def test_a_failing_answer_is_asked_again_with_the_reason(world, bad, reason) -> None:
    script = world.write(bad, answer(words(40)))

    assert script.version == 1
    assert len(world.text.calls) == 2
    retry = world.text.calls[1].prompt
    assert "Your last answer was rejected: " in retry and reason in retry
    assert world.events()[-1].metadata["rejected_answers"] == 1


def test_three_rejected_answers_store_nothing(world) -> None:
    with pytest.raises(ScriptGenerationError) as caught:
        world.write("no", answer(words(5)), answer("you won't believe " + words(40)))

    assert len(caught.value.rejections) == 3
    assert caught.value.to_public().code == "domain.script_generation_failed"
    assert world.stored() == []
    assert len(world.text.calls) == 3
    [event] = world.events()
    assert (event.action, event.result) == (
        "script.generation_failed",
        AuditResult.FAILURE,
    )
    assert event.metadata["answers"] == 3
    assert event.metadata["last_rejection"] == caught.value.rejections[-1]


def test_an_answer_repeating_the_latest_script_is_rejected(world) -> None:
    same = answer(words(40))
    world.write(same)

    second = world.write(same, answer(words(40, "save")))

    assert second.version == 2
    assert "repeats the latest script" in world.text.calls[-1].prompt


# Inputs


def test_inputs_must_exist_and_fit_together(world, database) -> None:
    run = world.hooks_run.id
    with pytest.raises(ContentItemNotFoundError):
        world.scripts.generate("missing", run, 0, actor=USER)
    with pytest.raises(HookGenerationNotFoundError):
        world.scripts.generate(world.item.id, "missing", 0, actor=USER)
    for index in (-1, 3, True, "0"):
        with pytest.raises(ScriptInputError):
            world.scripts.generate(world.item.id, run, index, actor=USER)
    longform = world.add_item(ContentType.LONGFORM)
    with pytest.raises(ScriptInputError):
        world.scripts.generate(longform.id, run, 0, actor=USER)
    other = world.add_item(ContentType.SHORTS)
    with pytest.raises(ScriptInputError):
        world.scripts.generate(other.id, run, 0, actor=USER)
    assert world.text.calls == []


def test_the_strategy_needs_a_format(database) -> None:
    world = ShortsWorld(database, format=None)

    with pytest.raises(ScriptInputError):
        world.write(answer(words(40)))
    assert world.text.calls == []


# Provider failures


def test_retryable_provider_errors_are_retried(world) -> None:
    world.text.fail_next(TextErrorCode.TIMEOUT, times=2)

    script = world.write(answer(words(40)))

    assert script.version == 1 and world.sleeps == [1.0, 2.0]


def test_a_provider_failure_is_audited_and_raised(world) -> None:
    world.text.fail_next(TextErrorCode.REFUSED)

    with pytest.raises(TextGenerationError):
        world.write(answer(words(40)))

    [event] = world.events()
    assert (event.action, event.result) == (
        "script.generation_failed",
        AuditResult.FAILURE,
    )
    assert event.metadata == {"code": "text.refused", "attempts": 1, "rejected": 0}
    assert world.stored() == []


# Another version stored while the provider answers (F-074)


class Racing:
    """A provider that lets another writer store a version while it answers."""

    def __init__(self, inner, race) -> None:
        self.inner, self.race = inner, race

    def generate(self, request):
        race, self.race = self.race, None
        if race is not None:
            race()
        return self.inner.generate(request)


def racing_scripts(world: ShortsWorld, race) -> ShortsScriptGenerator:
    return ShortsScriptGenerator(
        world.database,
        Racing(world.text, race),
        AuditLog(world.sink),
        clock=world.clock,
        sleep=world.sleeps.append,
    )


def other_writer(world: ShortsWorld, parent: Script | None) -> Script:
    sections = (ScriptSection(SectionKind.BODY, "Another writer was faster."),)
    values = {"sections": sections, "created_by": USER, "clock": world.clock}
    script = (
        Script.create(world.item.id, **values)
        if parent is None
        else parent.next_version(**values)
    )
    with world.database.transaction() as connection:
        ScriptRepository(connection).add(script)
    return script


def test_a_first_script_is_not_stored_when_another_first_version_appeared(
    world,
) -> None:
    racer: list[Script] = []
    scripts = racing_scripts(world, lambda: racer.append(other_writer(world, None)))
    world.text.queue(answer(words(40)))

    with pytest.raises(ScriptConflictError) as caught:
        scripts.generate(world.item.id, world.hooks_run.id, 0, actor=USER)

    public = caught.value.to_public()
    assert (public.code, public.http_status) == ("domain.script_conflict", 409)
    assert world.stored() == racer  # nothing was stored, nothing re-parented
    [event] = world.events()
    assert (event.action, event.result) == (
        "script.generation_failed",
        AuditResult.FAILURE,
    )
    assert event.metadata == {
        "stage": "store",
        "code": "script_conflict",
        "expected_version": 0,
        "latest_version": 1,
    }


def test_a_next_version_is_not_stored_when_another_version_appeared(world) -> None:
    first = world.write(answer(words(40)))
    racer: list[Script] = []
    scripts = racing_scripts(world, lambda: racer.append(other_writer(world, first)))
    world.text.queue(answer(words(40, "save")))

    with pytest.raises(ScriptConflictError) as caught:
        scripts.generate(world.item.id, world.hooks_run.id, 0, actor=USER)

    assert caught.value.to_public().http_status == 409
    assert world.stored() == [first, *racer]
    assert [e.action for e in world.events()] == [
        "script.generated",
        "script.generation_failed",
    ]
    assert world.events()[-1].metadata == {
        "stage": "store",
        "code": "script_conflict",
        "expected_version": 1,
        "latest_version": 2,
    }
    # Asked again, the script is written from the version that now exists.
    third = world.write(answer(words(40, "keep")))
    assert (third.version, third.parent_id) == (3, racer[0].id)


# Parsing


def test_parse_answer_cleans_spacing_and_fences() -> None:
    assert parse_answer('```\n{"body": ["  a\\n b "], "cta": " c "}\n```') == (
        ["a b"],
        "c",
    )
    with pytest.raises(ValueError):
        parse_answer('{"body": "a", "cta": "c"}')


def test_bootstrap_registers_the_generator(tmp_path) -> None:
    container = build_container(
        Settings(environment=Environment.TEST, database_path=tmp_path / "a.db")
    )
    assert isinstance(container.resolve(ShortsScriptGenerator), ShortsScriptGenerator)
