"""Source Deduplication (Prompt Pack v8, prompt #057), context C4 Research.

``SourceDeduplicator`` finds near-duplicate sources within one finished
research request. The rules were approved by the user on 2026-10-02:

- Sources are compared in collection order. The first source of a group is
  kept; each later source that is a near-duplicate of a kept source is marked
  ``duplicate_of`` that kept source (never of another duplicate), so groups do
  not chain.
- Two sources are near-duplicates when both have a content fingerprint that
  differs in at most 6 bits (reason ``fingerprint``), or else when their
  titles have a word Jaccard similarity of at least 0.9 (reason ``title``).
  ``similarity`` is the fingerprint similarity or the title Jaccard.
- Nothing is deleted or merged: the request keeps every collected source.
  The research report (#060) uses only the kept sources.
- The result is stored once per request (migration 0009). Running it again
  returns the stored result, so it is the same every time.
- Only finished requests can be deduplicated, and sources are compared only
  with sources of the same request.
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from ai_youtube_agent.content.research_request import ResearchRequestStateError
from ai_youtube_agent.content.similarity import (
    MAX_HAMMING_DISTANCE,
    TITLE_THRESHOLD,
    fingerprint_similarity,
    hamming_distance,
    word_jaccard,
)
from ai_youtube_agent.content.source import DuplicateReason, Source, SourceDuplicate
from ai_youtube_agent.content.source_collector import ResearchRequestNotFoundError
from ai_youtube_agent.core.db.database import Database
from ai_youtube_agent.core.db.repositories.research import (
    ResearchRequestRepository,
    SourceDuplicateRepository,
    SourceRepository,
)

Clock = Callable[[], datetime]


@dataclass(frozen=True)
class DeduplicationResult:
    request_id: str
    kept: tuple[str, ...]
    duplicates: tuple[SourceDuplicate, ...]
    deduplicated_at: datetime

    @property
    def groups(self) -> dict[str, tuple[str, ...]]:
        """Each kept source id with the ids of its duplicates, in order."""
        return {
            kept: tuple(d.source_id for d in self.duplicates if d.duplicate_of == kept)
            for kept in self.kept
        }


def match(first: Source, second: Source) -> SourceDuplicate | None:
    """``second`` as a near-duplicate of ``first``, or None."""
    a, b = first.content_fingerprint, second.content_fingerprint
    if (
        a is not None
        and b is not None
        and hamming_distance(a, b) <= (MAX_HAMMING_DISTANCE)
    ):
        return SourceDuplicate(
            second.id,
            first.id,
            DuplicateReason.FINGERPRINT,
            round(fingerprint_similarity(a, b), 4),
        )
    similarity = word_jaccard(first.title, second.title)
    if similarity >= TITLE_THRESHOLD:
        return SourceDuplicate(
            second.id, first.id, DuplicateReason.TITLE, round(similarity, 4)
        )
    return None


def find_duplicates(
    sources: Sequence[Source],
) -> tuple[tuple[str, ...], tuple[SourceDuplicate, ...]]:
    """The kept source ids and the duplicates, comparing in the given order."""
    kept: list[Source] = []
    duplicates: list[SourceDuplicate] = []
    for source in sources:
        found = next((d for k in kept if (d := match(k, source)) is not None), None)
        if found is None:
            kept.append(source)
        else:
            duplicates.append(found)
    return tuple(s.id for s in kept), tuple(duplicates)


class SourceDeduplicator:
    def __init__(self, database: Database, *, clock: Clock | None = None) -> None:
        self._database = database
        self._clock = clock

    def deduplicate(self, request_id: str) -> DeduplicationResult:
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
            ids = [c.source_id for c in request.collected]
            results = SourceDuplicateRepository(connection)
            stored = results.get(request_id)
            if stored is not None:
                deduplicated_at, duplicates = stored
                return _result(request_id, ids, duplicates, deduplicated_at)
            sources = SourceRepository(connection).list_by_ids(ids)
            kept, duplicates = find_duplicates(sources)
            now = self._clock() if self._clock else datetime.now(UTC)
            results.add(request_id, duplicates, now)
        return DeduplicationResult(request_id, kept, duplicates, now)


def _result(
    request_id: str,
    ids: list[str],
    duplicates: tuple[SourceDuplicate, ...],
    deduplicated_at: datetime,
) -> DeduplicationResult:
    marked = {d.source_id for d in duplicates}
    kept = tuple(i for i in ids if i not in marked)
    order = {source_id: index for index, source_id in enumerate(ids)}
    ordered = tuple(sorted(duplicates, key=lambda d: order[d.source_id]))
    return DeduplicationResult(request_id, kept, ordered, deduplicated_at)
