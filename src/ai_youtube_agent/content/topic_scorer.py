"""Topic Scoring (Prompt Pack v8, prompt #059), context C4 Research.

``TopicScorer`` computes the signals of ``content/topic_scoring.py`` for the
topics of a finished research request. The rules were approved by the user on
2026-10-02:

- It extracts the topics first (``TopicExtractor``, idempotent) and counts the
  kept sources (``SourceDeduplicator``, idempotent).
- Relevance words come from the current strategy (niche name and pillar
  names, audience interests) and the request's queries, without stop words.
  The strategy version used is recorded; a missing niche or audience is named
  in ``missing_inputs`` and the other inputs still count.
- Novelty compares each label with the stored topics (labels and key phrases)
  of the channel's earlier requests, those created before this one.
- Scores are ordered by score, highest first, then by the topic rank of
  #058. The result is stored once per request (migration 0011): running again
  returns it, even if the strategy changed since.
- Scoring only reads the strategy and never changes it (R-09).
"""

from collections.abc import Callable, Iterable
from datetime import UTC, datetime

from ai_youtube_agent.content.research_request import (
    ResearchRequest,
    ResearchRequestStateError,
)
from ai_youtube_agent.content.similarity import (
    SHORT_TEXT_THRESHOLD,
    word_jaccard,
    words,
)
from ai_youtube_agent.content.source_collector import ResearchRequestNotFoundError
from ai_youtube_agent.content.source_dedup import SourceDeduplicator
from ai_youtube_agent.content.strategy import StrategyProfile
from ai_youtube_agent.content.topic import Topic
from ai_youtube_agent.content.topic_extractor import STOP_WORDS, TopicExtractor
from ai_youtube_agent.content.topic_scoring import (
    TopicScore,
    TopicScoring,
    combined_score,
)
from ai_youtube_agent.core.db.database import Database
from ai_youtube_agent.core.db.repositories.channel import StrategyProfileRepository
from ai_youtube_agent.core.db.repositories.research import (
    ResearchRequestRepository,
    TopicRepository,
    TopicScoreRepository,
)

Clock = Callable[[], datetime]


def content_words(texts: Iterable[str]) -> set[str]:
    return {w for text in texts for w in words(text) if w not in STOP_WORDS}


def reference_words(
    strategy: StrategyProfile | None, queries: Iterable[str]
) -> tuple[set[str], tuple[str, ...]]:
    """The relevance vocabulary and the strategy inputs that are missing."""
    texts = list(queries)
    missing = []
    niche = strategy.niche if strategy else None
    audience = strategy.audience if strategy else None
    if niche is None:
        missing.append("niche")
    else:
        texts += [niche.name, *(pillar.name for pillar in niche.pillars)]
    if audience is None:
        missing.append("audience")
    else:
        texts += list(audience.interests)
    return content_words(texts), tuple(missing)


def score_topics(
    topics: Iterable[Topic],
    *,
    reference: set[str],
    earlier: dict[str, list[str]],
    kept_sources: int,
) -> tuple[TopicScore, ...]:
    """Scores, highest first; ``earlier`` maps request ids to their phrases."""
    scores = []
    for topic in topics:
        topic_words = sorted(content_words(topic.keyphrases))
        matched = [w for w in topic_words if w in reference]
        relevance = round(len(matched) / len(topic_words), 4) if topic_words else 0.0
        seen_in = tuple(
            request_id
            for request_id, phrases in earlier.items()
            if any(
                word_jaccard(topic.label, phrase) >= SHORT_TEXT_THRESHOLD
                for phrase in phrases
            )
        )
        novelty = round(1 / (1 + len(seen_in)), 4)
        support = round(topic.support_count / kept_sources, 4)
        scores.append(
            (
                topic.rank,
                TopicScore(
                    topic_id=topic.id,
                    label=topic.label,
                    relevance=relevance,
                    novelty=novelty,
                    support=support,
                    score=combined_score(relevance, novelty, support),
                    topic_words=tuple(topic_words),
                    matched_words=tuple(matched),
                    seen_in=seen_in,
                    support_count=topic.support_count,
                    kept_sources=kept_sources,
                ),
            )
        )
    scores.sort(key=lambda item: (-item[1].score, item[0]))
    return tuple(score for _, score in scores)


class TopicScorer:
    def __init__(
        self,
        database: Database,
        deduplicator: SourceDeduplicator,
        extractor: TopicExtractor,
        *,
        clock: Clock | None = None,
    ) -> None:
        self._database = database
        self._deduplicator = deduplicator
        self._extractor = extractor
        self._clock = clock

    def score(self, request_id: str) -> TopicScoring:
        with self._database.transaction() as connection:
            request = ResearchRequestRepository(connection).get(request_id)
            if request is None:
                raise ResearchRequestNotFoundError(
                    f"research request {request_id} does not exist"
                )
            if not request.is_finished:
                raise ResearchRequestStateError(
                    f"research request {request_id} is {request.status.value}, "
                    "not finished"
                )
            stored = TopicScoreRepository(connection).get(request_id)
        if stored is not None:
            return stored
        kept = len(self._deduplicator.deduplicate(request_id).kept)
        extraction = self._extractor.extract(request_id)
        with self._database.transaction() as connection:
            strategy = StrategyProfileRepository(connection).get_by_channel(
                request.channel_id
            )
            reference, missing = reference_words(strategy, request.queries)
            scoring = TopicScoring(
                request_id=request_id,
                scores=score_topics(
                    extraction.topics,
                    reference=reference,
                    earlier=self._earlier_phrases(connection, request),
                    kept_sources=kept,
                ),
                strategy_version=strategy.version if strategy else None,
                missing_inputs=missing,
                scored_at=self._clock() if self._clock else datetime.now(UTC),
            )
            TopicScoreRepository(connection).add(scoring)
        return scoring

    @staticmethod
    def _earlier_phrases(connection, request: ResearchRequest) -> dict[str, list[str]]:
        topics = TopicRepository(connection)
        earlier: dict[str, list[str]] = {}
        for other in ResearchRequestRepository(connection).list_by_channel(
            request.channel_id
        ):
            if other.id == request.id or other.created_at >= request.created_at:
                continue
            extraction = topics.get(other.id)
            if extraction is not None:
                earlier[other.id] = [
                    phrase for topic in extraction.topics for phrase in topic.keyphrases
                ]
        return earlier
