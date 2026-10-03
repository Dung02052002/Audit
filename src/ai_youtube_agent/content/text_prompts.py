"""Shared helpers for writing with the text provider (#065, #066), context C5.

- ``strategy_lines`` and ``fact_lines`` are the prompt lines every generator
  takes from the strategy (audience, brand tone, voice rules, banned phrases)
  and from the research report (its claims, or a ban on specific facts when it
  has none).
- ``call_with_retries`` asks the provider, retrying a retryable error up to 3
  times (waits 1 then 2 seconds); the last error, or one that is not
  retryable, is passed to ``on_failure`` with the attempts made and raised.
"""

from collections.abc import Callable

from ai_youtube_agent.content.research_report import ResearchReport
from ai_youtube_agent.content.strategy import StrategyProfile
from ai_youtube_agent.providers.text_generation import (
    GeneratedText,
    TextGenerationError,
    TextGenerator,
    TextRequest,
)

MAX_ATTEMPTS = 3
BACKOFF_SECONDS = (1.0, 2.0)
MAX_PROMPT_CLAIMS = 5
NO_FACTS = "Do not state specific facts, numbers or claims."


def strategy_lines(strategy: StrategyProfile) -> list[str]:
    lines = []
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
    return lines


def fact_lines(report: ResearchReport) -> list[str]:
    claims = report.claims[:MAX_PROMPT_CLAIMS]
    if not claims:
        return [NO_FACTS]
    return [
        "State only facts supported by these research claims:",
        *(f"- {claim.text}" for claim in claims),
    ]


def banned_phrases(strategy: StrategyProfile) -> tuple[str, ...]:
    return strategy.brand.banned_phrases if strategy.brand else ()


def call_with_retries(
    generator: TextGenerator,
    request: TextRequest,
    *,
    sleep: Callable[[float], None],
    on_failure: Callable[[TextGenerationError, int], None],
) -> GeneratedText:
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            return generator.generate(request)
        except TextGenerationError as error:
            if not error.retryable or attempt == MAX_ATTEMPTS:
                on_failure(error, attempt)
                raise
            sleep(BACKOFF_SECONDS[attempt - 1])
    raise AssertionError("unreachable: the last attempt returns or raises")
