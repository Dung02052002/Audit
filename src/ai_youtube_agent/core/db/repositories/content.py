"""Repositories for content items, artifacts, scripts, hooks, claims and audio."""

import sqlite3
from datetime import datetime

from ai_youtube_agent.content.claim_extraction import ClaimExtraction
from ai_youtube_agent.content.evidence_matching import EvidenceMatch
from ai_youtube_agent.content.hook import (
    HookCandidate,
    HookGeneration,
    HookRejection,
)
from ai_youtube_agent.content.script import (
    Claim,
    ClaimKind,
    DurationTarget,
    Evidence,
    NumberAgreement,
    Script,
    ScriptSection,
)
from ai_youtube_agent.content.voice import AudioMetadata
from ai_youtube_agent.core.artifact import Artifact, ArtifactKind
from ai_youtube_agent.core.content_item import ContentItem, ContentStatus, ContentType
from ai_youtube_agent.core.db.repositories.base import (
    Repository,
    actor_columns,
    actor_from,
    dt,
    from_json,
    parse_dt,
    to_json,
)


class ContentItemRepository(Repository):
    table = "content_items"

    def add(self, item: ContentItem) -> None:
        self._insert(self.table, _item_row(item))

    def get(self, item_id: str) -> ContentItem | None:
        row = self._one("SELECT * FROM content_items WHERE id = ?", (item_id,))
        return _item(row) if row else None

    def list_by_channel(self, channel_id: str) -> list[ContentItem]:
        rows = self._all(
            "SELECT * FROM content_items WHERE channel_id = ? ORDER BY created_at, id",
            (channel_id,),
        )
        return [_item(row) for row in rows]

    def update(self, item: ContentItem, *, expected_updated_at: datetime) -> None:
        self._update(
            item.id,
            {
                "title": item.title,
                "status": item.status.value,
                "updated_at": dt(item.updated_at),
            },
            guard_column="updated_at",
            guard_value=dt(expected_updated_at),
        )


class ArtifactRepository(Repository):
    """Artifact versions are immutable: add only."""

    table = "artifacts"

    def add(self, artifact: Artifact) -> None:
        self._insert(
            self.table,
            {
                "id": artifact.id,
                "content_item_id": artifact.content_item_id,
                "kind": artifact.kind.value,
                "version": artifact.version,
                "uri": artifact.uri,
                "sha256": artifact.sha256,
                "size_bytes": artifact.size_bytes,
                "media_type": artifact.media_type,
                "created_at": dt(artifact.created_at),
            },
        )

    def get(self, artifact_id: str) -> Artifact | None:
        row = self._one("SELECT * FROM artifacts WHERE id = ?", (artifact_id,))
        return _artifact(row) if row else None

    def list_by_content_item(self, content_item_id: str) -> list[Artifact]:
        rows = self._all(
            "SELECT * FROM artifacts WHERE content_item_id = ? ORDER BY kind, version",
            (content_item_id,),
        )
        return [_artifact(row) for row in rows]


class ScriptRepository(Repository):
    """Script versions, claims and evidence are immutable: add only."""

    table = "scripts"

    def add(self, script: Script) -> None:
        target = script.duration_target
        self._insert(
            "scripts",
            {
                "id": script.id,
                "content_item_id": script.content_item_id,
                "version": script.version,
                "text": script.text,
                "created_at": dt(script.created_at),
                "sections_json": to_json([s.as_dict() for s in script.sections]),
                "duration_min_seconds": target.min_seconds if target else None,
                "duration_max_seconds": target.max_seconds if target else None,
                **(
                    actor_columns("created_by", script.created_by)
                    if script.created_by
                    else {"created_by_kind": None, "created_by_id": None}
                ),
                "reason": script.reason,
                "parent_id": script.parent_id,
                "strategy_version": script.strategy_version,
                "research_report_id": script.research_report_id,
            },
        )

    def get(self, script_id: str) -> Script | None:
        row = self._one("SELECT * FROM scripts WHERE id = ?", (script_id,))
        return _script(row) if row else None

    def list_by_content_item(self, content_item_id: str) -> list[Script]:
        rows = self._all(
            "SELECT * FROM scripts WHERE content_item_id = ? ORDER BY version",
            (content_item_id,),
        )
        return [_script(row) for row in rows]

    def add_claim(self, claim: Claim) -> None:
        self._insert(
            "claims",
            {
                "id": claim.id,
                "script_id": claim.script_id,
                "text": claim.text,
                "created_at": dt(claim.created_at),
                "section_index": claim.section_index,
                "kind": claim.kind.value if claim.kind else None,
                "extraction_id": claim.extraction_id,
            },
        )

    def list_claims(self, script_id: str) -> list[Claim]:
        rows = self._all(
            # rowid breaks ties, so the claims of one extraction run (which
            # share its time) keep their order in the script.
            "SELECT * FROM claims WHERE script_id = ? ORDER BY created_at, rowid",
            (script_id,),
        )
        return [_claim(row) for row in rows]

    def add_evidence(self, evidence: Evidence) -> None:
        self._insert(
            "evidence",
            {
                "id": evidence.id,
                "claim_id": evidence.claim_id,
                "source_ref": evidence.source_ref,
                "excerpt": evidence.excerpt,
                "created_at": dt(evidence.created_at),
                "match_id": evidence.match_id,
                "research_claim_id": evidence.research_claim_id,
                "score": evidence.score,
                "numbers": evidence.numbers.value if evidence.numbers else None,
            },
        )

    def list_evidence(self, claim_id: str) -> list[Evidence]:
        rows = self._all(
            # rowid breaks ties, so the links of one matching run (which share
            # its time) keep their rank.
            "SELECT * FROM evidence WHERE claim_id = ? ORDER BY created_at, rowid",
            (claim_id,),
        )
        return [_evidence(row) for row in rows]


class HookGenerationRepository(Repository):
    """Hook generator runs (#065): add only, never changed."""

    table = "hook_generations"

    def add(self, generation: HookGeneration) -> None:
        self._insert(
            self.table,
            {
                "id": generation.id,
                "content_item_id": generation.content_item_id,
                "content_type": generation.content_type.value,
                "language": generation.language,
                "strategy_version": generation.strategy_version,
                "research_report_id": generation.research_report_id,
                "topic_id": generation.topic_id,
                "topic_label": generation.topic_label,
                "angle": generation.angle,
                "candidates_json": to_json([c.text for c in generation.candidates]),
                "rejected_json": to_json([r.as_dict() for r in generation.rejected]),
                "provider": generation.provider,
                "model": generation.model,
                **actor_columns("requested_by", generation.requested_by),
                "created_at": dt(generation.created_at),
            },
        )

    def get(self, generation_id: str) -> HookGeneration | None:
        row = self._one("SELECT * FROM hook_generations WHERE id = ?", (generation_id,))
        return _hook_generation(row) if row else None

    def list_by_content_item(self, content_item_id: str) -> list[HookGeneration]:
        rows = self._all(
            "SELECT * FROM hook_generations WHERE content_item_id = ? "
            "ORDER BY created_at, id",
            (content_item_id,),
        )
        return [_hook_generation(row) for row in rows]


class ClaimExtractionRepository(Repository):
    """Claim extraction runs (#068): one per script version, never changed.

    ``add`` stores the run with its claims; a second run for the same script
    breaks the unique ``script_id`` (``sqlite3.IntegrityError``).
    """

    table = "claim_extractions"

    def add(self, extraction: ClaimExtraction) -> None:
        self._insert(
            self.table,
            {
                "id": extraction.id,
                "script_id": extraction.script_id,
                "content_item_id": extraction.content_item_id,
                "method": extraction.method,
                "claims_count": extraction.claims_count,
                "skipped_cta": extraction.skipped_cta,
                "skipped_question": extraction.skipped_question,
                "skipped_opinion": extraction.skipped_opinion,
                "skipped_no_signal": extraction.skipped_no_signal,
                "dropped_duplicates": extraction.dropped_duplicates,
                "dropped_over_cap": extraction.dropped_over_cap,
                "dropped_too_long": extraction.dropped_too_long,
                **actor_columns("requested_by", extraction.requested_by),
                "created_at": dt(extraction.created_at),
            },
        )
        scripts = ScriptRepository(self.connection)
        for claim in extraction.claims:
            scripts.add_claim(claim)

    def get(self, extraction_id: str) -> ClaimExtraction | None:
        row = self._one(
            "SELECT * FROM claim_extractions WHERE id = ?", (extraction_id,)
        )
        return self._extraction(row) if row else None

    def get_by_script(self, script_id: str) -> ClaimExtraction | None:
        row = self._one(
            "SELECT * FROM claim_extractions WHERE script_id = ?", (script_id,)
        )
        return self._extraction(row) if row else None

    def _extraction(self, row: sqlite3.Row) -> ClaimExtraction:
        # The claims of one run share its time; rowid keeps the script order.
        claims = self._all(
            "SELECT * FROM claims WHERE extraction_id = ? ORDER BY rowid", (row["id"],)
        )
        return ClaimExtraction(
            id=row["id"],
            script_id=row["script_id"],
            content_item_id=row["content_item_id"],
            method=row["method"],
            claims=tuple(_claim(claim) for claim in claims),
            skipped_cta=row["skipped_cta"],
            skipped_question=row["skipped_question"],
            skipped_opinion=row["skipped_opinion"],
            skipped_no_signal=row["skipped_no_signal"],
            dropped_duplicates=row["dropped_duplicates"],
            dropped_over_cap=row["dropped_over_cap"],
            dropped_too_long=row["dropped_too_long"],
            requested_by=actor_from(row, "requested_by"),
            created_at=parse_dt(row["created_at"]),
        )


class EvidenceMatchRepository(Repository):
    """Evidence matching runs (#069): one per claim extraction, never changed.

    ``add`` stores the run with its links; a second run for the same
    extraction breaks the unique ``extraction_id`` (``sqlite3.IntegrityError``).
    """

    table = "evidence_matches"

    def add(self, match: EvidenceMatch) -> None:
        self._insert(
            self.table,
            {
                "id": match.id,
                "extraction_id": match.extraction_id,
                "script_id": match.script_id,
                "content_item_id": match.content_item_id,
                "research_report_id": match.research_report_id,
                "method": match.method,
                "claims_count": match.claims_count,
                "matched": match.matched,
                "unmatched": match.unmatched,
                "links": match.links_count,
                "numbers_differ": match.numbers_differ,
                **actor_columns("requested_by", match.requested_by),
                "created_at": dt(match.created_at),
            },
        )
        scripts = ScriptRepository(self.connection)
        for link in match.links:
            scripts.add_evidence(link)

    def get(self, match_id: str) -> EvidenceMatch | None:
        row = self._one("SELECT * FROM evidence_matches WHERE id = ?", (match_id,))
        return self._match(row) if row else None

    def get_by_extraction(self, extraction_id: str) -> EvidenceMatch | None:
        row = self._one(
            "SELECT * FROM evidence_matches WHERE extraction_id = ?", (extraction_id,)
        )
        return self._match(row) if row else None

    def _match(self, row: sqlite3.Row) -> EvidenceMatch:
        # The links of one run share its time; rowid keeps the claim and rank
        # order.
        links = self._all(
            "SELECT * FROM evidence WHERE match_id = ? ORDER BY rowid", (row["id"],)
        )
        return EvidenceMatch(
            id=row["id"],
            extraction_id=row["extraction_id"],
            script_id=row["script_id"],
            content_item_id=row["content_item_id"],
            research_report_id=row["research_report_id"],
            method=row["method"],
            links=tuple(_evidence(link) for link in links),
            claims_count=row["claims_count"],
            matched=row["matched"],
            unmatched=row["unmatched"],
            numbers_differ=row["numbers_differ"],
            requested_by=actor_from(row, "requested_by"),
            created_at=parse_dt(row["created_at"]),
        )


def _evidence(row: sqlite3.Row) -> Evidence:
    return Evidence(
        id=row["id"],
        claim_id=row["claim_id"],
        source_ref=row["source_ref"],
        excerpt=row["excerpt"],
        created_at=parse_dt(row["created_at"]),
        match_id=row["match_id"],
        research_claim_id=row["research_claim_id"],
        score=row["score"],
        numbers=(
            NumberAgreement(row["numbers"]) if row["numbers"] is not None else None
        ),
    )


def _claim(row: sqlite3.Row) -> Claim:
    return Claim(
        id=row["id"],
        script_id=row["script_id"],
        text=row["text"],
        created_at=parse_dt(row["created_at"]),
        section_index=row["section_index"],
        kind=ClaimKind(row["kind"]) if row["kind"] is not None else None,
        extraction_id=row["extraction_id"],
    )


def _hook_generation(row: sqlite3.Row) -> HookGeneration:
    return HookGeneration(
        id=row["id"],
        content_item_id=row["content_item_id"],
        content_type=ContentType(row["content_type"]),
        language=row["language"],
        strategy_version=row["strategy_version"],
        research_report_id=row["research_report_id"],
        topic_id=row["topic_id"],
        topic_label=row["topic_label"],
        angle=row["angle"],
        candidates=tuple(HookCandidate(t) for t in from_json(row["candidates_json"])),
        rejected=tuple(
            HookRejection.from_dict(r) for r in from_json(row["rejected_json"])
        ),
        provider=row["provider"],
        model=row["model"],
        requested_by=actor_from(row, "requested_by"),
        created_at=parse_dt(row["created_at"]),
    )


class AudioMetadataRepository(Repository):
    """Audio metadata is immutable: add only."""

    table = "audio_metadata"

    def add(self, audio: AudioMetadata) -> None:
        self._insert(
            self.table,
            {
                "id": audio.id,
                "artifact_id": audio.artifact_id,
                "script_id": audio.script_id,
                "voice_profile_id": audio.voice_profile_id,
                "voice_profile_version": audio.voice_profile_version,
                "provider": audio.provider,
                "duration_ms": audio.duration_ms,
                "created_at": dt(audio.created_at),
            },
        )

    def get(self, audio_id: str) -> AudioMetadata | None:
        row = self._one("SELECT * FROM audio_metadata WHERE id = ?", (audio_id,))
        return _audio(row) if row else None

    def get_by_artifact(self, artifact_id: str) -> AudioMetadata | None:
        row = self._one(
            "SELECT * FROM audio_metadata WHERE artifact_id = ?", (artifact_id,)
        )
        return _audio(row) if row else None


def _item_row(item: ContentItem) -> dict:
    return {
        "id": item.id,
        "channel_id": item.channel_id,
        "strategy_profile_id": item.strategy_profile_id,
        "strategy_version": item.strategy_version,
        "content_type": item.content_type.value,
        "title": item.title,
        "status": item.status.value,
        "created_at": dt(item.created_at),
        "updated_at": dt(item.updated_at),
    }


def _item(row: sqlite3.Row) -> ContentItem:
    return ContentItem(
        id=row["id"],
        channel_id=row["channel_id"],
        strategy_profile_id=row["strategy_profile_id"],
        strategy_version=row["strategy_version"],
        content_type=ContentType(row["content_type"]),
        title=row["title"],
        status=ContentStatus(row["status"]),
        created_at=parse_dt(row["created_at"]),
        updated_at=parse_dt(row["updated_at"]),
    )


def _artifact(row: sqlite3.Row) -> Artifact:
    return Artifact(
        id=row["id"],
        content_item_id=row["content_item_id"],
        kind=ArtifactKind(row["kind"]),
        version=row["version"],
        uri=row["uri"],
        sha256=row["sha256"],
        size_bytes=row["size_bytes"],
        media_type=row["media_type"],
        created_at=parse_dt(row["created_at"]),
    )


def _script(row: sqlite3.Row) -> Script:
    return Script(
        id=row["id"],
        content_item_id=row["content_item_id"],
        version=row["version"],
        sections=tuple(
            ScriptSection.from_dict(item) for item in from_json(row["sections_json"])
        ),
        created_at=parse_dt(row["created_at"]),
        duration_target=(
            DurationTarget(row["duration_min_seconds"], row["duration_max_seconds"])
            if row["duration_min_seconds"] is not None
            else None
        ),
        created_by=(
            actor_from(row, "created_by")
            if row["created_by_kind"] is not None
            else None
        ),
        reason=row["reason"],
        parent_id=row["parent_id"],
        strategy_version=row["strategy_version"],
        research_report_id=row["research_report_id"],
    )


def _audio(row: sqlite3.Row) -> AudioMetadata:
    return AudioMetadata(
        id=row["id"],
        artifact_id=row["artifact_id"],
        script_id=row["script_id"],
        voice_profile_id=row["voice_profile_id"],
        voice_profile_version=row["voice_profile_version"],
        provider=row["provider"],
        duration_ms=row["duration_ms"],
        created_at=parse_dt(row["created_at"]),
    )
