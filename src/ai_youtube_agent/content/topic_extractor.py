"""Topic Extractor (Prompt Pack v8, prompt #058), context C4 Research.

``TopicExtractor`` finds candidate topics in a finished research request by
keyword statistics, with no AI model. The rules were approved by the user on
2026-10-02:

- Only the sources kept by deduplication (#057) count; the extractor runs the
  (idempotent) ``SourceDeduplicator`` first.
- Each kept source offers its title and its evidence notes and quotes. Their
  words (``content/similarity.py``) give candidate phrases of 1 to 3
  consecutive words that neither start nor end with a stop word (English or
  Vietnamese) and are not only digits.
- A phrase is a candidate when at least 2 different sources contain it. A
  shorter phrase is dropped when a longer phrase containing it has the same
  sources.
- Candidates are ranked by the number of sources (most first), then by length
  (longer first), then alphabetically. Walking down that order, a candidate
  whose label has a word Jaccard of at least 0.8 with a stronger topic is
  merged into it (its phrase becomes a key phrase and its sources are added).
- At most 20 topics are kept. Each topic's evidence is, per source, the first
  field (title, then notes and quotes in order) that contains the phrase.
- The result is stored once per request (migration 0010); running again
  returns the stored topics.
"""

import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime

from ai_youtube_agent.content.research_request import ResearchRequestStateError
from ai_youtube_agent.content.similarity import (
    SHORT_TEXT_THRESHOLD,
    word_jaccard,
    words,
)
from ai_youtube_agent.content.source import Source
from ai_youtube_agent.content.source_collector import ResearchRequestNotFoundError
from ai_youtube_agent.content.source_dedup import SourceDeduplicator
from ai_youtube_agent.content.topic import (
    MAX_EVIDENCE_TEXT,
    MAX_KEYPHRASES,
    MAX_LABEL,
    MAX_TOPICS,
    MIN_SUPPORT,
    EvidenceField,
    Topic,
    TopicEvidence,
    TopicExtraction,
)
from ai_youtube_agent.core.db.database import Database
from ai_youtube_agent.core.db.repositories.research import (
    ResearchRequestRepository,
    SourceRepository,
    TopicRepository,
)

MAX_PHRASE_WORDS = 3
_ENGLISH_STOP_WORDS = (
    "a an and are as at be been but by can do does for from has have how i if in"
    " into is it its more most my no not of on or our so than that the their them"
    " then there these they this to too up us was we what when where which who"
    " why will with you your"
)
_VIETNAMESE_STOP_WORDS = (
    "và của là các những cho với trong có được không một này đã để khi thì từ"
    " người như về ra theo cũng đến nhiều hay bạn tôi sẽ nên làm vào rằng đó nó"
    " mà bị lại còn nhưng"
)
STOP_WORDS = frozenset(_ENGLISH_STOP_WORDS.split()) | frozenset(
    _VIETNAMESE_STOP_WORDS.split()
)
Clock = Callable[[], datetime]


def phrases(text: str) -> set[str]:
    """The candidate phrases of a text (see the module docstring)."""
    tokens = words(text)
    found = set()
    for size in range(1, MAX_PHRASE_WORDS + 1):
        for start in range(len(tokens) - size + 1):
            gram = tokens[start : start + size]
            if gram[0] in STOP_WORDS or gram[-1] in STOP_WORDS:
                continue
            if all(token.isdigit() for token in gram):
                continue
            phrase = " ".join(gram)
            if len(phrase) <= MAX_LABEL:
                found.add(phrase)
    return found


def _fields(source: Source) -> list[tuple[EvidenceField, str]]:
    fields = [(EvidenceField.TITLE, source.title)]
    for note in source.evidence_notes:
        fields.append((EvidenceField.NOTE, note.note))
        if note.quote is not None:
            fields.append((EvidenceField.QUOTE, note.quote))
    return fields


@dataclass
class _Candidate:
    label: str
    sources: dict[str, TopicEvidence]  # source id -> evidence, in source order
    keyphrases: list[str] = field(default_factory=list)


def extract_topics(request_id: str, sources: Sequence[Source]) -> tuple[Topic, ...]:
    """Ranked topics for ``sources`` (the kept sources, in collection order)."""
    order = {source.id: index for index, source in enumerate(sources)}
    evidence: dict[str, dict[str, TopicEvidence]] = {}
    for source in sources:
        for kind, text in _fields(source):
            for phrase in phrases(text):
                per_source = evidence.setdefault(phrase, {})
                if source.id not in per_source:
                    per_source[source.id] = TopicEvidence(
                        source.id, kind, text[:MAX_EVIDENCE_TEXT]
                    )
    supported = {p: e for p, e in evidence.items() if len(e) >= MIN_SUPPORT}
    # Drop a phrase when a longer phrase containing it has the same sources.
    candidates = [
        phrase
        for phrase, items in supported.items()
        if not any(
            other != phrase
            and f" {phrase} " in f" {other} "
            and other_items.keys() == items.keys()
            for other, other_items in supported.items()
        )
    ]
    candidates.sort(key=lambda p: (-len(supported[p]), -len(p.split()), p))

    topics: list[_Candidate] = []
    for phrase in candidates:
        target = next(
            (
                topic
                for topic in topics
                if word_jaccard(topic.label, phrase) >= SHORT_TEXT_THRESHOLD
            ),
            None,
        )
        if target is None:
            if len(topics) < MAX_TOPICS:
                topics.append(_Candidate(phrase, dict(supported[phrase]), [phrase]))
            continue
        if len(target.keyphrases) < MAX_KEYPHRASES:
            target.keyphrases.append(phrase)
        for source_id, item in supported[phrase].items():
            target.sources.setdefault(source_id, item)

    return tuple(
        Topic(
            id=uuid.uuid4().hex,
            request_id=request_id,
            rank=rank,
            label=candidate.label,
            keyphrases=tuple(candidate.keyphrases),
            evidence=tuple(
                sorted(candidate.sources.values(), key=lambda e: order[e.source_id])
            ),
        )
        for rank, candidate in enumerate(topics, start=1)
    )


class TopicExtractor:
    def __init__(
        self,
        database: Database,
        deduplicator: SourceDeduplicator,
        *,
        clock: Clock | None = None,
    ) -> None:
        self._database = database
        self._deduplicator = deduplicator
        self._clock = clock

    def extract(self, request_id: str) -> TopicExtraction:
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
            stored = TopicRepository(connection).get(request_id)
        if stored is not None:
            return stored
        kept = self._deduplicator.deduplicate(request_id).kept
        with self._database.transaction() as connection:
            sources = SourceRepository(connection).list_by_ids(kept)
            now = self._clock() if self._clock else datetime.now(UTC)
            extraction = TopicExtraction(
                request_id, extract_topics(request_id, sources), now
            )
            TopicRepository(connection).add(extraction)
        return extraction
