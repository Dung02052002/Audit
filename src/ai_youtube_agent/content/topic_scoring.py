"""Topic scores (Prompt Pack v8, prompt #059), context C4 Research.

Transparent, internal signals for the topics of one research request. The
rules were approved by the user on 2026-10-02:

- ``relevance`` (0 to 1): the share of the topic's words (all its key
  phrases) that also appear in the channel's niche name and pillars, the
  audience interests and the request's queries. ``matched_words`` lists them.
  A missing niche or audience is left out and named in ``missing_inputs``.
- ``novelty`` (0 to 1): ``1 / (1 + seen)``, where ``seen`` counts the earlier
  requests of the same channel whose stored topics include a near-duplicate
  label (word Jaccard >= 0.8). ``seen_in`` lists those requests.
- ``support`` (0 to 1): supporting sources / sources kept by deduplication.
- ``score`` = 0.5 relevance + 0.3 novelty + 0.2 support (``WEIGHTS``).
  Every value is rounded to 4 decimal places, and ``reasons`` explains each
  signal in words.

Scores are internal: they rank candidate topics and never change the
strategy (R-09). The topic ranks of #058 are kept as they are.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Any

WEIGHTS = {"relevance": 0.5, "novelty": 0.3, "support": 0.2}
STRATEGY_INPUTS = ("niche", "audience")


def _unit(name: str, value: float) -> None:
    if not 0 <= value <= 1:
        raise ValueError(f"{name} must be from 0 to 1")


def combined_score(relevance: float, novelty: float, support: float) -> float:
    return round(
        WEIGHTS["relevance"] * relevance
        + WEIGHTS["novelty"] * novelty
        + WEIGHTS["support"] * support,
        4,
    )


@dataclass(frozen=True)
class TopicScore:
    topic_id: str
    label: str
    relevance: float
    novelty: float
    support: float
    score: float
    topic_words: tuple[str, ...]
    matched_words: tuple[str, ...]
    seen_in: tuple[str, ...]
    support_count: int
    kept_sources: int

    def __post_init__(self) -> None:
        for name in ("relevance", "novelty", "support", "score"):
            _unit(name, getattr(self, name))
        if not set(self.matched_words) <= set(self.topic_words):
            raise ValueError("matched words must be topic words")
        if not 0 < self.support_count <= self.kept_sources:
            raise ValueError("support count must be from 1 to the kept sources")
        if self.score != combined_score(self.relevance, self.novelty, self.support):
            raise ValueError("score must be the weighted sum of the signals")

    @property
    def seen_count(self) -> int:
        return len(self.seen_in)

    @property
    def reasons(self) -> tuple[str, ...]:
        matched = ", ".join(self.matched_words) or "none"
        return (
            f"relevance {self.relevance}: {len(self.matched_words)} of "
            f"{len(self.topic_words)} topic words match the strategy or the "
            f"queries ({matched})",
            f"novelty {self.novelty}: seen in {self.seen_count} earlier "
            "research request(s) of this channel",
            f"support {self.support}: {self.support_count} of {self.kept_sources} "
            "kept sources support it",
            f"score {self.score} = 0.5 x relevance + 0.3 x novelty + 0.2 x support",
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "topic_id": self.topic_id,
            "label": self.label,
            "relevance": self.relevance,
            "novelty": self.novelty,
            "support": self.support,
            "score": self.score,
            "topic_words": list(self.topic_words),
            "matched_words": list(self.matched_words),
            "seen_in": list(self.seen_in),
            "support_count": self.support_count,
            "kept_sources": self.kept_sources,
            "reasons": list(self.reasons),
        }


@dataclass(frozen=True)
class TopicScoring:
    """The scores of one request's topics, highest score first."""

    request_id: str
    scores: tuple[TopicScore, ...]
    strategy_version: int | None
    missing_inputs: tuple[str, ...]
    scored_at: datetime

    def __post_init__(self) -> None:
        values = [s.score for s in self.scores]
        if values != sorted(values, reverse=True):
            raise ValueError("scores must be ordered highest first")
        if not set(self.missing_inputs) <= set(STRATEGY_INPUTS):
            raise ValueError(f"missing inputs must be among {STRATEGY_INPUTS}")
