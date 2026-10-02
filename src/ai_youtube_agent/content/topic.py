"""Topic entity (Prompt Pack v8, prompt #058), context C4 Research.

A ``Topic`` is a candidate subject found in the sources of one research
request. The rules were approved by the user on 2026-10-02:

- ``label`` is the topic's main key phrase (1 to 3 words, lower case, at most
  100 characters); ``keyphrases`` are the label followed by the phrases merged
  into it (at most 10).
- ``evidence`` holds one ``TopicEvidence`` per supporting source: the source
  id, which field held the phrase (``title``, ``note`` or ``quote``) and that
  field's text. ``support_count`` is the number of supporting sources, at
  least 2.
- ``rank`` orders the topics of a request from 1. A request has at most 20.

Topics are candidates only. Scoring them is #059, and nothing here changes the
strategy (R-09). A topic is an immutable record.
"""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any

MAX_TOPICS = 20
MIN_SUPPORT = 2
MAX_LABEL = 100
MAX_KEYPHRASES = 10
MAX_EVIDENCE_TEXT = 1000


class EvidenceField(StrEnum):
    TITLE = "title"
    NOTE = "note"
    QUOTE = "quote"


@dataclass(frozen=True)
class TopicEvidence:
    source_id: str
    field: EvidenceField
    text: str

    def __post_init__(self) -> None:
        if not self.source_id:
            raise ValueError("source id must not be empty")
        if not isinstance(self.field, EvidenceField):
            raise TypeError("field must be an EvidenceField")
        if not self.text.strip() or len(self.text) > MAX_EVIDENCE_TEXT:
            raise ValueError(
                f"evidence text must be 1 to {MAX_EVIDENCE_TEXT} characters"
            )

    def as_dict(self) -> dict[str, str]:
        return {
            "source_id": self.source_id,
            "field": self.field.value,
            "text": self.text,
        }


@dataclass(frozen=True)
class Topic:
    id: str
    request_id: str
    rank: int
    label: str
    keyphrases: tuple[str, ...]
    evidence: tuple[TopicEvidence, ...]

    def __post_init__(self) -> None:
        if not self.id or not self.request_id:
            raise ValueError("topic id and request id must not be empty")
        if isinstance(self.rank, bool) or not 1 <= self.rank <= MAX_TOPICS:
            raise ValueError(f"rank must be from 1 to {MAX_TOPICS}")
        if not self.label.strip() or len(self.label) > MAX_LABEL:
            raise ValueError(f"label must be 1 to {MAX_LABEL} characters")
        if self.label != self.label.lower():
            raise ValueError("label must be lower case")
        if not 1 <= len(self.keyphrases) <= MAX_KEYPHRASES:
            raise ValueError(f"a topic has 1 to {MAX_KEYPHRASES} key phrases")
        if self.keyphrases[0] != self.label:
            raise ValueError("the first key phrase is the label")
        if len(set(self.keyphrases)) != len(self.keyphrases):
            raise ValueError("key phrases must not repeat")
        if not isinstance(self.evidence, tuple) or not all(
            isinstance(item, TopicEvidence) for item in self.evidence
        ):
            raise TypeError("evidence must be a tuple of TopicEvidence values")
        sources = [item.source_id for item in self.evidence]
        if len(set(sources)) != len(sources):
            raise ValueError("one evidence item per source")
        if len(sources) < MIN_SUPPORT:
            raise ValueError(f"a topic needs at least {MIN_SUPPORT} sources")

    @property
    def support_count(self) -> int:
        return len(self.evidence)

    @property
    def source_ids(self) -> tuple[str, ...]:
        return tuple(item.source_id for item in self.evidence)

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "request_id": self.request_id,
            "rank": self.rank,
            "label": self.label,
            "keyphrases": list(self.keyphrases),
            "support_count": self.support_count,
            "evidence": [item.as_dict() for item in self.evidence],
        }


@dataclass(frozen=True)
class TopicExtraction:
    """The topics of one request, ranked, and when they were extracted."""

    request_id: str
    topics: tuple[Topic, ...]
    extracted_at: datetime

    def __post_init__(self) -> None:
        if len(self.topics) > MAX_TOPICS:
            raise ValueError(f"a request has at most {MAX_TOPICS} topics")
        if [t.rank for t in self.topics] != list(range(1, len(self.topics) + 1)):
            raise ValueError("topics must be ranked 1..n in order")
        if any(t.request_id != self.request_id for t in self.topics):
            raise ValueError("every topic must belong to the request")
