"""F-065 Hook Generator (Prompt Pack v8, prompt #065).

Rules the user approved on 2026-10-03:

- a new ``TextGenerator`` provider interface with a deterministic mock
  (``Settings.text_provider = mock``, ``text_provider`` health check),
  answering ARCHITECTURE Q1;
- inputs: the content item's type, the strategy (primary language, brand,
  banned phrases, audience) and a stored research report of the same channel
  (chosen or best topic, claims), plus an optional angle;
- limits: Shorts at most 2 sentences and 15 words, LongForm at most 3 and 60;
  no banned phrase; violations are rejected with their issues;
- up to 3 candidates are stored per run (``hook_generations``, migration
  0016); the caller picks one; nothing is written to a script.
"""

import dataclasses
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ai_youtube_agent.bootstrap import build_container
from ai_youtube_agent.content.hook import (
    HOOK_LIMITS,
    HookCandidate,
    HookIssueCode,
    check_hook,
    count_sentences,
    normalize_hook,
)
from ai_youtube_agent.content.hook_generator import (
    ContentItemNotFoundError,
    HookGenerator,
    HookInputError,
    ResearchReportNotFoundError,
)
from ai_youtube_agent.content.report_generator import ResearchReportGenerator
from ai_youtube_agent.content.script import SectionKind
from ai_youtube_agent.content.source_collector import SourceCollector
from ai_youtube_agent.content.source_dedup import SourceDeduplicator
from ai_youtube_agent.content.strategy import Brand
from ai_youtube_agent.content.topic_extractor import TopicExtractor
from ai_youtube_agent.content.topic_scorer import TopicScorer
from ai_youtube_agent.core.audit import (
    Actor,
    ActorKind,
    AuditLog,
    AuditResult,
    InMemoryAuditSink,
)
from ai_youtube_agent.core.config import Environment, Settings, TextProviderKind
from ai_youtube_agent.core.content_item import ContentType
from ai_youtube_agent.core.db.database import Database
from ai_youtube_agent.core.db.repositories.channel import (
    ChannelRepository,
    StrategyProfileRepository,
)
from ai_youtube_agent.core.db.repositories.content import (
    ContentItemRepository,
    HookGenerationRepository,
)
from ai_youtube_agent.core.errors import ProviderError
from ai_youtube_agent.main import create_app
from ai_youtube_agent.providers.mock_research import MockHit, MockResearchProvider
from ai_youtube_agent.providers.mock_text_generation import MockTextGenerator
from ai_youtube_agent.providers.text_generation import (
    GeneratedText,
    TextErrorCode,
    TextGenerationError,
    TextGenerator,
    TextRequest,
)
from factories import make_channel, make_content_item, make_strategy_profile

T0 = datetime(2026, 10, 3, 12, 0, tzinfo=UTC)
USER = Actor(ActorKind.USER, "owner")
BRAND = Brand(
    "Money Minute",
    "calm and clear",
    tone_keywords=("calm", "practical"),
    voice_dos=("Speak to one viewer",),
    voice_donts=("Shout",),
    banned_phrases=("get rich quick", "you won't believe"),
)

TITLES = (
    "Bank fees explained",
    "Bank fees in Vietnam",
    "Hidden bank fees",
    "Savings account rates",
    "Savings account tips",
)


class Clock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> datetime:
        self.now += timedelta(seconds=1)
        return self.now


class World:
    def __init__(self, database: Database, **strategy) -> None:
        self.database = database
        self.clock = Clock()
        self.channel = make_channel()
        self.strategy = make_strategy_profile(
            self.channel, **{"brand": BRAND, **strategy}
        )
        self.item = self.add_item(ContentType.SHORTS)
        with database.transaction() as connection:
            ChannelRepository(connection).add(self.channel)
            StrategyProfileRepository(connection).add(self.strategy)
            ContentItemRepository(connection).add(self.item)
        self.text = MockTextGenerator(clock=self.clock)
        self.sink = InMemoryAuditSink()
        self.sleeps: list[float] = []
        self.hooks = HookGenerator(
            database,
            self.text,
            AuditLog(self.sink),
            clock=self.clock,
            sleep=self.sleeps.append,
        )

    def add_item(self, content_type: ContentType):
        item = make_content_item(self.channel, self.strategy, content_type=content_type)
        if hasattr(self, "text"):
            with self.database.transaction() as connection:
                ContentItemRepository(connection).add(item)
        return item

    def report(self, titles=TITLES, channel=None):
        provider = MockResearchProvider(clock=self.clock)
        hits = []
        for n, title in enumerate(titles):
            url = f"https://site{n}.org/"
            provider.add_page(url, f"{title}. Text {n}.", title=title)
            hits.append(MockHit(url, title))
        provider.add_results("bank fees", hits)
        collector = SourceCollector(
            self.database, provider, AuditLog(InMemoryAuditSink()), clock=self.clock
        )
        dedup = SourceDeduplicator(self.database)
        scorer = TopicScorer(self.database, dedup, TopicExtractor(self.database, dedup))
        request = collector.request(
            (channel or self.channel).id, ["bank fees"], actor=USER
        )
        collector.collect(request.id)
        return ResearchReportGenerator(self.database, dedup, scorer).generate(
            request.id
        )

    def actions(self):
        return [(e.action, e.result) for e in self.sink.events()]


@pytest.fixture
def world(database: Database) -> World:
    return World(database)


# The provider


@pytest.mark.parametrize(
    "bad",
    [
        dict(prompt=" "),
        dict(prompt="x" * 20_001),
        dict(prompt="p", system=" "),
        dict(prompt="p", language="not a tag!"),
        dict(prompt="p", candidates=0),
        dict(prompt="p", candidates=6),
        dict(prompt="p", max_tokens=0),
        dict(prompt="p", max_tokens=8_001),
    ],
)
def test_text_requests_reject_invalid_values(bad) -> None:
    with pytest.raises(ValueError):
        TextRequest(**bad)


def test_generated_text_needs_texts_names_and_utc() -> None:
    GeneratedText(("a",), "mock", "m", T0, 1, 2)
    for bad in (
        dict(texts=()),
        dict(texts=(" ",)),
        dict(texts=("a",) * 6),
        dict(model=""),
        dict(generated_at=datetime(2026, 10, 3)),
        dict(input_tokens=-1),
    ):
        values = {"texts": ("a",), "provider": "mock", "model": "m", "generated_at": T0}
        with pytest.raises(ValueError):
            GeneratedText(**{**values, **bad})


def test_text_errors_are_provider_errors_retryable_by_code() -> None:
    assert {c.value for c in TextErrorCode if c.retryable} == {
        "text.unavailable",
        "text.rate_limited",
        "text.timeout",
    }
    error = TextGenerationError(TextErrorCode.REFUSED, provider="mock")
    assert isinstance(error, ProviderError) and not error.retryable
    assert error.to_public().code == "text.refused"


def test_the_mock_is_deterministic_scriptable_and_recorded() -> None:
    mock = MockTextGenerator(clock=lambda: T0)
    assert isinstance(mock, TextGenerator)
    request = TextRequest("Write.", candidates=2)

    first = mock.generate(request)
    assert first.texts == mock.generate(request).texts
    assert len(first.texts) == 2 and first.texts[0].startswith("Mock text 1 ")
    assert (first.provider, first.model, first.generated_at) == ("mock", "mock-1", T0)

    mock.queue("one", "two", "three")
    assert mock.generate(request).texts == ("one", "two")
    mock.fail_next(TextErrorCode.TIMEOUT)
    with pytest.raises(TextGenerationError) as caught:
        mock.generate(request)
    assert caught.value.retryable
    assert len(mock.calls) == 4
    mock.set_healthy(False)
    with pytest.raises(TextGenerationError):
        mock.check()


def test_settings_and_bootstrap_choose_the_mock(tmp_path: Path, database_copy) -> None:
    assert Settings().text_provider is TextProviderKind.MOCK
    with pytest.raises(ValueError):
        Settings(text_provider="openai")
    container = build_container(
        Settings(
            environment=Environment.TEST,
            database_path=database_copy(tmp_path / "a.db"),
        )
    )
    assert isinstance(container.resolve(TextGenerator), MockTextGenerator)
    assert container.resolve(TextGenerator) is container.resolve(TextGenerator)
    assert isinstance(container.resolve(HookGenerator), HookGenerator)

    container.resolve(TextGenerator).set_healthy(False)
    with TestClient(create_app(container)) as client:
        body = client.get("/health").json()
    assert body["status"] == "degraded"
    [check] = [c for c in body["checks"] if c["name"] == "text_provider"]
    assert (check["kind"], check["status"]) == ("provider", "degraded")


# Hook rules


def test_limits_depend_on_the_content_type() -> None:
    assert (
        HOOK_LIMITS[ContentType.SHORTS].max_sentences,
        HOOK_LIMITS[ContentType.SHORTS].max_words,
    ) == (2, 15)
    assert (
        HOOK_LIMITS[ContentType.LONGFORM].max_sentences,
        HOOK_LIMITS[ContentType.LONGFORM].max_words,
    ) == (3, 60)
    fifteen = " ".join(["word"] * 15) + "."
    assert check_hook(fifteen, ContentType.SHORTS) == ()
    [issue] = check_hook(fifteen + " more", ContentType.SHORTS)
    assert issue.code is HookIssueCode.TOO_MANY_WORDS
    assert check_hook(" ".join(["word"] * 60), ContentType.LONGFORM) == ()
    three = "One. Two. Three."
    assert count_sentences(three) == 3
    [issue] = check_hook(three, ContentType.SHORTS)
    assert issue.code is HookIssueCode.TOO_MANY_SENTENCES
    assert check_hook(three, ContentType.LONGFORM) == ()
    assert check_hook(" ", ContentType.SHORTS)[0].code is HookIssueCode.EMPTY


def test_banned_phrases_match_whole_words_ignoring_case() -> None:
    banned = ("get rich quick", "ass")
    [issue] = check_hook("How to GET  RICH quick today?", ContentType.SHORTS, banned)
    assert issue.code is HookIssueCode.BANNED_PHRASE
    assert "get rich quick" in issue.detail
    assert check_hook("Your class starts now.", ContentType.SHORTS, banned) == ()


@pytest.mark.parametrize(
    ("raw", "clean"),
    [
        ('  "Stop paying fees."  ', "Stop paying fees."),
        ("“Stop paying fees.”", "Stop paying fees."),
        ("1. Stop   paying\nfees.", "Stop paying fees."),
        ("- Stop paying fees.", "Stop paying fees."),
        ("'Don't pay.'", "Don't pay."),
    ],
)
def test_generated_texts_are_cleaned_before_checking(raw, clean) -> None:
    assert normalize_hook(raw) == clean


# Generating hooks


def test_hooks_are_written_from_the_strategy_and_the_report(world: World) -> None:
    report = world.report()
    world.text.queue(
        "Bank fees eat your savings.",
        "Ever checked your bank fees?",
        "This fee costs you every month.",
    )

    generation = world.hooks.generate(world.item.id, report.id, actor=USER)

    assert [c.text for c in generation.candidates] == [
        "Bank fees eat your savings.",
        "Ever checked your bank fees?",
        "This fee costs you every month.",
    ]
    assert generation.rejected == ()
    assert (generation.content_type, generation.language) == (ContentType.SHORTS, "vi")
    assert generation.strategy_version == world.strategy.version
    assert (generation.topic_id, generation.topic_label) == (
        report.topics[0].topic_id,
        report.topics[0].label,
    )
    assert (generation.provider, generation.model) == ("mock", "mock-1")
    [request] = world.text.calls
    assert (request.language, request.candidates, request.max_tokens) == ("vi", 3, 80)
    assert "hook text only" in request.system
    for expected in (
        "YouTube Shorts video",
        "Language: vi.",
        "at most 2 sentences and 15 words",
        f"Topic: {report.topics[0].label}",
        "Audience: Adults interested in personal finance",
        "Tone: calm and clear",
        "Tone keywords: calm, practical.",
        "Do: Speak to one viewer",
        "Don't: Shout",
        "Never use: get rich quick; you won't believe.",
        "Do not state specific facts",  # the report has no claims (E-060 gap)
    ):
        assert expected in request.prompt
    with world.database.transaction() as connection:
        stored = HookGenerationRepository(connection).list_by_content_item(
            world.item.id
        )
    assert stored == [generation]
    assert world.actions() == [("hook.generated", AuditResult.SUCCESS)]
    section = generation.candidates[0].as_section()
    assert (section.kind, section.text) == (
        SectionKind.HOOK,
        "Bank fees eat your savings.",
    )


def test_invalid_and_repeated_texts_are_rejected_then_asked_again(
    world: World,
) -> None:
    report = world.report()
    long_text = " ".join(["fees"] * 16)
    world.text.queue(
        '"Bank fees add up fast."',
        "You won't believe these fees!",
        long_text,
    )
    world.text.queue("bank fees add up fast.", "Check your statement today.", "x")

    generation = world.hooks.generate(world.item.id, report.id, actor=USER)

    assert [c.text for c in generation.candidates] == [
        "Bank fees add up fast.",
        "Check your statement today.",
        "x",
    ]
    assert [(r.text, [i.code for i in r.issues]) for r in generation.rejected] == [
        ("You won't believe these fees!", [HookIssueCode.BANNED_PHRASE]),
        (long_text, [HookIssueCode.TOO_MANY_WORDS]),
        ("bank fees add up fast.", [HookIssueCode.DUPLICATE]),
    ]
    second = world.text.calls[1].prompt
    assert "Write something different from:\n- Bank fees add up fast." in second


def test_a_run_without_a_valid_hook_is_stored_as_a_failure(world: World) -> None:
    report = world.report()
    world.text.queue("Get rich quick!")
    world.text.queue("Get rich quick now!")

    generation = world.hooks.generate(world.item.id, report.id, actor=USER)

    assert generation.candidates == () and len(generation.rejected) == 2
    assert len(world.text.calls) == 2
    assert world.actions() == [("hook.generated", AuditResult.FAILURE)]


def test_longform_hooks_use_the_longform_limits(world: World) -> None:
    report = world.report()
    item = world.add_item(ContentType.LONGFORM)
    sixty = " ".join(["fees"] * 57) + " end. Two. Three."  # 60 words
    world.text.queue(sixty)

    generation = world.hooks.generate(item.id, report.id, actor=USER)

    assert [c.text for c in generation.candidates][0] == sixty
    request = world.text.calls[0]
    assert request.max_tokens == 240
    assert "long-form YouTube video" in request.prompt
    assert "at most 3 sentences and 60 words" in request.prompt


def test_the_caller_may_choose_the_topic_and_an_angle(world: World) -> None:
    report = world.report()
    assert len(report.topics) >= 2
    chosen = report.topics[1]

    generation = world.hooks.generate(
        world.item.id,
        report.id,
        topic_id=chosen.topic_id,
        angle="  for students  ",
        actor=USER,
    )

    assert (generation.topic_id, generation.angle) == (chosen.topic_id, "for students")
    assert f"Topic: {chosen.label}" in world.text.calls[0].prompt
    assert "Angle: for students" in world.text.calls[0].prompt
    with pytest.raises(HookInputError):
        world.hooks.generate(world.item.id, report.id, topic_id="nope", actor=USER)
    with pytest.raises(HookInputError):
        world.hooks.generate(world.item.id, report.id, angle="x" * 301, actor=USER)


def test_a_report_without_topics_gives_its_queries(world: World) -> None:
    report = world.report(titles=("Alpha", "Beta", "Gamma"))
    assert report.topics == ()

    generation = world.hooks.generate(world.item.id, report.id, actor=USER)

    assert generation.topic_id is None and generation.topic_label is None
    assert "Topic: bank fees." in world.text.calls[0].prompt


def test_inputs_must_exist_and_belong_together(world: World, database) -> None:
    report = world.report()
    with pytest.raises(ContentItemNotFoundError):
        world.hooks.generate("missing", report.id, actor=USER)
    with pytest.raises(ResearchReportNotFoundError):
        world.hooks.generate(world.item.id, "missing", actor=USER)

    other = World(database)
    with pytest.raises(HookInputError):
        world.hooks.generate(other.item.id, report.id, actor=USER)
    assert world.text.calls == []


def test_the_strategy_must_have_a_language(database) -> None:
    # A content item always has a strategy (foreign key), so only the
    # language can be missing.
    world = World(database, languages=None)
    report = world.report()

    with pytest.raises(HookInputError):
        world.hooks.generate(world.item.id, report.id, actor=USER)
    assert world.text.calls == []


def test_retryable_provider_errors_are_retried(world: World) -> None:
    report = world.report()
    world.text.fail_next(TextErrorCode.RATE_LIMITED, times=2)

    generation = world.hooks.generate(world.item.id, report.id, actor=USER)

    assert len(generation.candidates) == 3
    assert world.sleeps == [1.0, 2.0]


@pytest.mark.parametrize(
    ("code", "times", "attempts"),
    [(TextErrorCode.REFUSED, 1, 1), (TextErrorCode.TIMEOUT, 3, 3)],
)
def test_provider_failures_are_audited_and_raised(world, code, times, attempts):
    report = world.report()
    world.text.fail_next(code, times=times)

    with pytest.raises(TextGenerationError):
        world.hooks.generate(world.item.id, report.id, actor=USER)

    [event] = world.sink.events()
    assert (event.action, event.result) == ("hook.failed", AuditResult.FAILURE)
    assert event.metadata == {"code": code.value, "attempts": attempts}
    with world.database.transaction() as connection:
        assert (
            HookGenerationRepository(connection).list_by_content_item(world.item.id)
            == []
        )


def test_generations_reject_invalid_state(world: World) -> None:
    report = world.report()
    generation = world.hooks.generate(world.item.id, report.id, actor=USER)
    for bad in (
        dict(topic_label=None),
        dict(candidates=(HookCandidate("a"),) * 2),
        dict(candidates=tuple(HookCandidate(t) for t in "abcd")),
        dict(angle="x" * 301),
        dict(language=""),
    ):
        with pytest.raises(ValueError):
            dataclasses.replace(generation, **bad)
