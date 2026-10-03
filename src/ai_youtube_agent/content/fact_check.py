"""Fact check rules and runs (Prompt Pack v8, prompt #070), context C5.

The rules were approved by the user on 2026-10-03. A fact check is
deterministic, with no AI model; the run records its method
(``FACT_CHECK_METHOD`` = ``"rules-v1"``) so another method can be added
later.

- A run reads one stored evidence matching run (#069) and gives every claim
  of its claim extraction run one result: ``FactCheckStatus`` (PASS, WARN or
  FAIL) and a ``FactCheckCode`` naming the rule that decided. The first
  matching row of the table wins. "Agree" and "differ" mean a link of the
  claim with ``numbers`` = agree or differ (#069); a numeric or date claim is
  a claim of kind ``numeric`` or ``date`` (a claim without a kind is none).
  The uncertainty of a claim is the lowest (most certain) uncertainty of the
  research report claims its links matched, read in the research report of
  the matching run (``research_claim_id`` of each link, which must exist in
  the report). ``STRONG_SCORE`` is 0.75.

  ==  =============================================  ======  ===================
  1   the matching run has no report                 WARN    no_report
  2   no links                                       WARN    unmatched
  3   numeric/date, a differ link and no agree link  FAIL    numbers_differ
  4   an agree link and a differ link both exist     WARN    sources_conflict
  5   numeric/date and no agree link (all none)      WARN    number_unverified
  6   all linked research claims have high           WARN    weak_evidence
      uncertainty
  7   absolute, exactly 1 link, uncertainty not low  WARN    absolute_needs_support
  8   exactly 1 link, best score under 0.75, numbers WARN    low_score
      not agree
  9   otherwise                                      PASS    supported
  ==  =============================================  ======  ===================

  A claim without a link, or a run without a report, is a WARN, never a FAIL;
  differing numbers are a FAIL only when no link agrees.
- Each result keeps a snapshot of what the rules read: the number of links,
  the best score, how the numbers compare (agree when a link agrees, else
  differ when a link differs, else none) and the lowest uncertainty; the last
  three are ``None`` for a claim without a link.
- ``FactCheck`` is one stored run for one evidence matching run, with its
  results and counts. Its ``status`` is derived: the worst of its results
  (FAIL, then WARN, then PASS; no claim is PASS).
- A result is a record only: it blocks nothing, and it is never changed or
  overridden. A later task may add a separate table of overrides.
"""

import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum

from ai_youtube_agent.content.claim_extraction import MAX_CLAIMS, ClaimExtraction
from ai_youtube_agent.content.evidence_matching import MAX_LINKS, EvidenceMatch
from ai_youtube_agent.content.research_report import ResearchReport, Uncertainty
from ai_youtube_agent.content.script import (
    Claim,
    ClaimKind,
    Evidence,
    NumberAgreement,
)
from ai_youtube_agent.core.audit import Actor

__all__ = [
    "FACT_CHECK_METHOD",
    "STRONG_SCORE",
    "ClaimCheck",
    "FactCheck",
    "FactCheckCode",
    "FactCheckStatus",
    "check_claims",
]

Clock = Callable[[], datetime]

FACT_CHECK_METHOD = "rules-v1"
STRONG_SCORE = 0.75
MAX_METHOD = 100


class FactCheckStatus(StrEnum):
    PASS = "pass"
    WARN = "warn"
    FAIL = "fail"


class FactCheckCode(StrEnum):
    """The rule that gave a result, in the order of the table."""

    NO_REPORT = "no_report"
    UNMATCHED = "unmatched"
    NUMBERS_DIFFER = "numbers_differ"
    SOURCES_CONFLICT = "sources_conflict"
    NUMBER_UNVERIFIED = "number_unverified"
    WEAK_EVIDENCE = "weak_evidence"
    ABSOLUTE_NEEDS_SUPPORT = "absolute_needs_support"
    LOW_SCORE = "low_score"
    SUPPORTED = "supported"


_STATUS_OF_CODE = {
    FactCheckCode.NUMBERS_DIFFER: FactCheckStatus.FAIL,
    FactCheckCode.SUPPORTED: FactCheckStatus.PASS,
}
_WITHOUT_LINKS = (FactCheckCode.NO_REPORT, FactCheckCode.UNMATCHED)
_CHECKABLE = (ClaimKind.NUMERIC, ClaimKind.DATE)
_CERTAINTY = {Uncertainty.LOW: 0, Uncertainty.MEDIUM: 1, Uncertainty.HIGH: 2}


def status_of(code: FactCheckCode) -> FactCheckStatus:
    """The status a code stands for: only two codes are not a WARN."""
    return _STATUS_OF_CODE.get(code, FactCheckStatus.WARN)


@dataclass(frozen=True)
class ClaimCheck:
    """The result for one claim, with the snapshot the rules read."""

    claim_id: str
    status: FactCheckStatus
    code: FactCheckCode
    links: int = 0
    best_score: float | None = None
    numbers: NumberAgreement | None = None
    uncertainty: Uncertainty | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.claim_id, str) or not self.claim_id.strip():
            raise ValueError("claim_id must not be empty")
        if not isinstance(self.status, FactCheckStatus):
            raise TypeError("status must be a FactCheckStatus")
        if not isinstance(self.code, FactCheckCode):
            raise TypeError("code must be a FactCheckCode")
        if self.status is not status_of(self.code):
            raise ValueError(f"the code {self.code.value} is not {self.status.value}")
        if (
            isinstance(self.links, bool)
            or not isinstance(self.links, int)
            or not 0 <= self.links <= MAX_LINKS
        ):
            raise ValueError(f"links must be a whole number from 0 to {MAX_LINKS}")
        if self.best_score is not None and (
            isinstance(self.best_score, bool)
            or not isinstance(self.best_score, int | float)
            or not 0 <= self.best_score <= 1
        ):
            raise ValueError("best_score must be a number from 0 to 1")
        if self.numbers is not None and not isinstance(self.numbers, NumberAgreement):
            raise TypeError("numbers must be a NumberAgreement")
        if self.uncertainty is not None and not isinstance(
            self.uncertainty, Uncertainty
        ):
            raise TypeError("uncertainty must be an Uncertainty")
        snapshot = (self.best_score, self.numbers, self.uncertainty)
        if self.links == 0 and any(item is not None for item in snapshot):
            raise ValueError("a claim without a link has no snapshot")
        if self.links > 0 and any(item is None for item in snapshot):
            raise ValueError("a claim with links has a full snapshot")
        if (self.links == 0) != (self.code in _WITHOUT_LINKS):
            raise ValueError("no_report and unmatched are the codes without links")


def _uncertainties(
    match: EvidenceMatch, report: ResearchReport | None
) -> Mapping[str, Uncertainty]:
    if match.research_report_id is None:
        return {}
    if report is None or report.id != match.research_report_id:
        raise ValueError(f"research report {match.research_report_id} is needed")
    return {claim.id: claim.uncertainty for claim in report.claims}


def _lowest(links: Sequence[Evidence], known: Mapping[str, Uncertainty]) -> Uncertainty:
    found = []
    for link in links:
        if link.research_claim_id not in known:
            raise ValueError(
                f"research claim {link.research_claim_id} is not in the report"
            )
        found.append(known[link.research_claim_id])
    return min(found, key=_CERTAINTY.__getitem__)


def _verdict(
    claim: Claim, links: Sequence[Evidence], uncertainty: Uncertainty, best: float
) -> tuple[FactCheckCode, NumberAgreement]:
    agree = any(link.numbers is NumberAgreement.AGREE for link in links)
    differ = any(link.numbers is NumberAgreement.DIFFER for link in links)
    numbers = (
        NumberAgreement.AGREE
        if agree
        else NumberAgreement.DIFFER
        if differ
        else NumberAgreement.NONE
    )
    checkable = claim.kind in _CHECKABLE
    if checkable and differ and not agree:
        code = FactCheckCode.NUMBERS_DIFFER
    elif agree and differ:
        code = FactCheckCode.SOURCES_CONFLICT
    elif checkable and not agree:
        code = FactCheckCode.NUMBER_UNVERIFIED
    elif uncertainty is Uncertainty.HIGH:
        code = FactCheckCode.WEAK_EVIDENCE
    elif (
        claim.kind is ClaimKind.ABSOLUTE
        and len(links) == 1
        and uncertainty is not Uncertainty.LOW
    ):
        code = FactCheckCode.ABSOLUTE_NEEDS_SUPPORT
    elif len(links) == 1 and best < STRONG_SCORE and not agree:
        code = FactCheckCode.LOW_SCORE
    else:
        code = FactCheckCode.SUPPORTED
    return code, numbers


def check_claims(
    claims: Sequence[Claim], match: EvidenceMatch, report: ResearchReport | None
) -> tuple[ClaimCheck, ...]:
    """One result per claim, by the table of the module docstring.

    ``report`` is the research report of ``match`` (``None`` when the run has
    none). A link whose research claim is not in it raises ``ValueError``.
    """
    known = _uncertainties(match, report)
    by_claim: dict[str, list[Evidence]] = {}
    for link in match.links:
        by_claim.setdefault(link.claim_id, []).append(link)
    results = []
    for claim in claims:
        links = by_claim.get(claim.id, [])
        if match.no_report:
            results.append(
                ClaimCheck(claim.id, FactCheckStatus.WARN, FactCheckCode.NO_REPORT)
            )
        elif not links:
            results.append(
                ClaimCheck(claim.id, FactCheckStatus.WARN, FactCheckCode.UNMATCHED)
            )
        else:
            uncertainty = _lowest(links, known)
            best = max(link.score for link in links)
            code, numbers = _verdict(claim, links, uncertainty, best)
            results.append(
                ClaimCheck(
                    claim.id,
                    status_of(code),
                    code,
                    len(links),
                    best,
                    numbers,
                    uncertainty,
                )
            )
    return tuple(results)


@dataclass(frozen=True)
class FactCheck:
    id: str
    match_id: str
    extraction_id: str
    script_id: str
    content_item_id: str
    research_report_id: str | None
    method: str
    results: tuple[ClaimCheck, ...]
    claims_count: int
    pass_count: int
    warn_count: int
    fail_count: int
    requested_by: Actor
    created_at: datetime

    def __post_init__(self) -> None:
        for name in (
            "id",
            "match_id",
            "extraction_id",
            "script_id",
            "content_item_id",
        ):
            if not getattr(self, name):
                raise ValueError(f"{name} must not be empty")
        if self.research_report_id is not None and not self.research_report_id.strip():
            raise ValueError("research_report_id must not be empty")
        if (
            not isinstance(self.method, str)
            or not self.method.strip()
            or len(self.method) > MAX_METHOD
        ):
            raise ValueError(f"a method is 1 to {MAX_METHOD} characters")
        for name in ("claims_count", "pass_count", "warn_count", "fail_count"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a whole number of 0 or more")
        if self.claims_count > MAX_CLAIMS:
            raise ValueError(f"at most {MAX_CLAIMS} claims")
        if self.pass_count + self.warn_count + self.fail_count != self.claims_count:
            raise ValueError("every claim is either PASS, WARN or FAIL")
        self._check_results()
        if not isinstance(self.requested_by, Actor):
            raise TypeError("requested_by must be an Actor")
        if not isinstance(
            self.created_at, datetime
        ) or self.created_at.utcoffset() != timedelta(0):
            raise ValueError("created_at must be timezone-aware UTC")

    def _check_results(self) -> None:
        for result in self.results:
            if not isinstance(result, ClaimCheck):
                raise TypeError("results must be ClaimCheck values")
        if len({result.claim_id for result in self.results}) != len(self.results):
            raise ValueError("a claim has one result")
        if len(self.results) != self.claims_count:
            raise ValueError("claims_count must count the results")
        for status, name in (
            (FactCheckStatus.PASS, "pass_count"),
            (FactCheckStatus.WARN, "warn_count"),
            (FactCheckStatus.FAIL, "fail_count"),
        ):
            counted = sum(result.status is status for result in self.results)
            if counted != getattr(self, name):
                raise ValueError(f"{name} must count the {status.value} results")
        no_report = [r.code is FactCheckCode.NO_REPORT for r in self.results]
        if self.research_report_id is None and not all(no_report):
            raise ValueError("a run without a research report has only no_report")
        if self.research_report_id is not None and any(no_report):
            raise ValueError("no_report belongs to a run without a research report")

    @property
    def status(self) -> FactCheckStatus:
        """The worst result: FAIL, then WARN, then PASS (no claim is PASS)."""
        if self.fail_count:
            return FactCheckStatus.FAIL
        if self.warn_count:
            return FactCheckStatus.WARN
        return FactCheckStatus.PASS

    @property
    def no_report(self) -> bool:
        return self.research_report_id is None

    @classmethod
    def create(
        cls,
        extraction: ClaimExtraction,
        match: EvidenceMatch,
        results: Sequence[ClaimCheck],
        *,
        method: str = FACT_CHECK_METHOD,
        requested_by: Actor,
        clock: Clock | None = None,
    ) -> "FactCheck":
        """A run for ``match``, which must be the run of ``extraction``."""
        if match.extraction_id != extraction.id:
            raise ValueError("the matching run belongs to another extraction run")
        results = tuple(results)
        return cls(
            id=uuid.uuid4().hex,
            match_id=match.id,
            extraction_id=extraction.id,
            script_id=extraction.script_id,
            content_item_id=extraction.content_item_id,
            research_report_id=match.research_report_id,
            method=method,
            results=results,
            claims_count=len(results),
            pass_count=sum(r.status is FactCheckStatus.PASS for r in results),
            warn_count=sum(r.status is FactCheckStatus.WARN for r in results),
            fail_count=sum(r.status is FactCheckStatus.FAIL for r in results),
            requested_by=requested_by,
            created_at=clock() if clock else datetime.now(UTC),
        )

    def counts(self) -> dict[str, int | bool | str]:
        """The scalar counts of the run, as audited."""
        return {
            "claims": self.claims_count,
            "pass": self.pass_count,
            "warn": self.warn_count,
            "fail": self.fail_count,
            "status": self.status.value,
            "no_report": self.no_report,
        }
