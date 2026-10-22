"""LongForm Metadata (Prompt Pack v8, prompt J-113), context C9 LongForm Production.

``LongFormMetadata`` is the canonical YouTube metadata package for a
``LongFormProject`` (#105),
and ``LongFormMetadataRecorder`` validates and stores it as the item's
``ArtifactKind.METADATA`` artifact (``application/json``):

Key invariants & rules:
- Guard: FeatureFlags.longform_enabled must be True, else LongFormDisabledError.
- Bound to the current LongFormProject (project_id, project_version, content_item_id,
  channel_id, script_id, script_version, strategy_version).
- Title: 1..100 characters, no '<' or '>', single-space collapsed, NFC normalized.
- Description: 1..5000 characters, no '<' or '>', NFC normalized.
  - If project.config.chapters is enabled and embed_chapters is True, YouTube-compatible
    chapters formatted via format_youtube_chapters(timeline.chapters) are embedded.
  - Timeline asset attributions are appended if append_attributions is True.
- Tags: 0..30 unique tags, max 500 characters total, no '<' or '>', NFC normalized.
- Chapters: structured tuple of chapter records (title, start_ms, end_ms).
- Language: BCP-47 canonical tag from strategy profile.
- Made for kids: boolean flag.
- AI disclosure: requires a recorded DisclosureRecord on the item; binds
  disclosure_decision_id and contains_synthetic_media (True when REQUIRED).
- Policy check: runs PolicyChecker over title, description, tags, channel banned
  phrases; refuses with POLICY_BLOCKED if status is BLOCK.
- Deterministic JSON serialized under ArtifactKind.METADATA in ArtifactStorage.
- Replays with created=False when latest METADATA artifact has identical sha256.
- Invalidates stale approvals on new version and audits `longform.metadata_recorded`.
"""

import hashlib
import json
import re
import unicodedata
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from http import HTTPStatus
from typing import Any, Final

from ai_youtube_agent.content.disclosure import DisclosureDecision
from ai_youtube_agent.content.longform_chapters import format_youtube_chapters
from ai_youtube_agent.content.longform_project import (
    LongFormProjectStatus,
)
from ai_youtube_agent.content.longform_script_generator import LongFormDisabledError
from ai_youtube_agent.content.longform_service import (
    longform_project_is_current,
)
from ai_youtube_agent.content.policy_check import (
    POLICY_RULES,
    PolicyChecker,
    PolicyInput,
    PolicyRuleSetCatalog,
    PolicyStatus,
    default_catalog,
)
from ai_youtube_agent.content.policy_rule import RuleSet
from ai_youtube_agent.content.strategy import (
    LANGUAGE_TAG_PATTERN,
    canonical_language_tag,
)
from ai_youtube_agent.content.text_prompts import banned_phrases
from ai_youtube_agent.core.artifact import Artifact, ArtifactKind
from ai_youtube_agent.core.artifact_storage import ArtifactStorage, StoredFile
from ai_youtube_agent.core.audit import (
    Actor,
    AuditLog,
    AuditResult,
    EntityRef,
)
from ai_youtube_agent.core.db.database import Database
from ai_youtube_agent.core.db.repositories.asset import AssetRepository
from ai_youtube_agent.core.db.repositories.channel import StrategyProfileRepository
from ai_youtube_agent.core.db.repositories.content import (
    ArtifactRepository,
    ContentItemRepository,
)
from ai_youtube_agent.core.db.repositories.disclosure import DisclosureRepository
from ai_youtube_agent.core.db.repositories.longform import LongFormProjectRepository
from ai_youtube_agent.core.errors import DomainError
from ai_youtube_agent.core.flags import FeatureFlags
from ai_youtube_agent.core.log import get_logger
from ai_youtube_agent.core.version_invalidation import (
    InvalidationResult,
    invalidate_stale_approvals,
    record_invalidations,
)

logger = get_logger(__name__)

METADATA_RECORDED: Final[str] = "longform.metadata_recorded"
METADATA_MEDIA_TYPE: Final[str] = "application/json"
METADATA_SCHEMA_VERSION: Final[int] = 1

MAX_TITLE_LENGTH: Final[int] = 100
MAX_DESCRIPTION_LENGTH: Final[int] = 5000
MAX_TAG_COUNT: Final[int] = 30
MAX_TAG_LENGTH: Final[int] = 100
MAX_TOTAL_TAGS_LENGTH: Final[int] = 500

_FORBIDDEN_CHARS: Final[re.Pattern[str]] = re.compile(r"[<>]")

Clock = Callable[[], datetime]


class LongFormMetadataErrorCode(StrEnum):
    METADATA = "domain.longform_metadata"
    PROJECT_NOT_FOUND = "domain.longform_metadata_project_not_found"
    NOT_READY = "domain.longform_metadata_not_ready"
    CONFLICT = "domain.longform_metadata_conflict"
    STALE_INPUT = "domain.longform_metadata_stale_input"
    STRATEGY_MISSING = "domain.longform_metadata_strategy_missing"
    DISCLOSURE_MISSING = "domain.longform_metadata_disclosure_missing"
    POLICY_BLOCKED = "domain.longform_metadata_policy_blocked"
    INVALID_METADATA = "domain.longform_metadata_invalid"


USER_MESSAGES: Final[dict[LongFormMetadataErrorCode, str]] = {
    LongFormMetadataErrorCode.METADATA: (
        "The LongForm metadata could not be recorded."
    ),
    LongFormMetadataErrorCode.PROJECT_NOT_FOUND: (
        "This LongForm project does not exist."
    ),
    LongFormMetadataErrorCode.NOT_READY: (
        "This LongForm project is a draft; plan the timeline first."
    ),
    LongFormMetadataErrorCode.CONFLICT: (
        "The LongForm project was updated concurrently; reload and try again."
    ),
    LongFormMetadataErrorCode.STALE_INPUT: (
        "The LongForm project is out of date with its script or audio."
    ),
    LongFormMetadataErrorCode.STRATEGY_MISSING: (
        "The channel does not have a strategy profile."
    ),
    LongFormMetadataErrorCode.DISCLOSURE_MISSING: (
        "An AI disclosure decision must be recorded before metadata."
    ),
    LongFormMetadataErrorCode.POLICY_BLOCKED: (
        "The LongForm metadata was blocked by a policy rule."
    ),
    LongFormMetadataErrorCode.INVALID_METADATA: (
        "The LongForm title, description, tags or language are invalid."
    ),
}


class LongFormMetadataError(DomainError):
    default_code = LongFormMetadataErrorCode.METADATA.value
    default_user_message = USER_MESSAGES[LongFormMetadataErrorCode.METADATA]
    default_http_status = HTTPStatus.UNPROCESSABLE_ENTITY

    def __init__(
        self,
        detail: str = "",
        *,
        code: LongFormMetadataErrorCode = LongFormMetadataErrorCode.METADATA,
        http_status: int | None = None,
    ) -> None:
        status = (
            http_status
            if http_status is not None
            else (
                HTTPStatus.NOT_FOUND
                if code is LongFormMetadataErrorCode.PROJECT_NOT_FOUND
                else (
                    HTTPStatus.CONFLICT
                    if code
                    in (
                        LongFormMetadataErrorCode.NOT_READY,
                        LongFormMetadataErrorCode.CONFLICT,
                        LongFormMetadataErrorCode.STALE_INPUT,
                        LongFormMetadataErrorCode.STRATEGY_MISSING,
                        LongFormMetadataErrorCode.DISCLOSURE_MISSING,
                    )
                    else HTTPStatus.UNPROCESSABLE_ENTITY
                )
            )
        )
        super().__init__(
            detail,
            code=code.value,
            user_message=USER_MESSAGES.get(
                code, USER_MESSAGES[LongFormMetadataErrorCode.METADATA]
            ),
            http_status=status,
        )


@dataclass(frozen=True)
class ChapterMetadata:
    title: str
    start_ms: int
    end_ms: int

    def as_dict(self) -> dict[str, Any]:
        return {"title": self.title, "start_ms": self.start_ms, "end_ms": self.end_ms}


@dataclass(frozen=True)
class LongFormMetadata:
    project_id: str
    project_version: int
    content_item_id: str
    channel_id: str
    script_id: str
    script_version: int
    strategy_version: int
    title: str
    description: str
    tags: tuple[str, ...]
    chapters: tuple[ChapterMetadata, ...]
    language: str
    made_for_kids: bool
    contains_synthetic_media: bool
    disclosure_decision_id: str
    policy_rule_set_id: str
    policy_rule_set_version: int
    policy_status: PolicyStatus
    schema_version: int = METADATA_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != METADATA_SCHEMA_VERSION:
            raise ValueError(f"unsupported schema version {self.schema_version}")
        _check_title(self.title)
        _check_description(self.description)
        _check_tags(self.tags)
        if not LANGUAGE_TAG_PATTERN.fullmatch(self.language):
            raise ValueError(f"invalid language tag {self.language}")

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "project_id": self.project_id,
            "project_version": self.project_version,
            "content_item_id": self.content_item_id,
            "channel_id": self.channel_id,
            "script_id": self.script_id,
            "script_version": self.script_version,
            "strategy_version": self.strategy_version,
            "title": self.title,
            "description": self.description,
            "tags": list(self.tags),
            "chapters": [ch.as_dict() for ch in self.chapters],
            "language": self.language,
            "made_for_kids": self.made_for_kids,
            "contains_synthetic_media": self.contains_synthetic_media,
            "disclosure_decision_id": self.disclosure_decision_id,
            "policy": {
                "rule_set_id": self.policy_rule_set_id,
                "rule_set_version": self.policy_rule_set_version,
                "status": self.policy_status.value,
            },
        }

    def to_bytes(self) -> bytes:
        return json.dumps(
            self.as_dict(),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        ).encode("utf-8")

    @classmethod
    def from_bytes(cls, raw: bytes) -> "LongFormMetadata":
        payload = json.loads(raw.decode("utf-8"))
        chapters = tuple(
            ChapterMetadata(ch["title"], ch["start_ms"], ch["end_ms"])
            for ch in payload.get("chapters", [])
        )
        policy = payload.get("policy", {})
        return cls(
            project_id=payload["project_id"],
            project_version=payload["project_version"],
            content_item_id=payload["content_item_id"],
            channel_id=payload["channel_id"],
            script_id=payload["script_id"],
            script_version=payload["script_version"],
            strategy_version=payload["strategy_version"],
            title=payload["title"],
            description=payload["description"],
            tags=tuple(payload.get("tags", ())),
            chapters=chapters,
            language=payload["language"],
            made_for_kids=payload["made_for_kids"],
            contains_synthetic_media=payload["contains_synthetic_media"],
            disclosure_decision_id=payload["disclosure_decision_id"],
            policy_rule_set_id=policy.get("rule_set_id", "policy"),
            policy_rule_set_version=policy.get("rule_set_version", 1),
            policy_status=PolicyStatus(policy.get("status", "pass")),
            schema_version=payload.get("schema_version", METADATA_SCHEMA_VERSION),
        )


@dataclass(frozen=True)
class LongFormMetadataRecordResult:
    artifact: Artifact
    metadata: LongFormMetadata
    created: bool
    invalidation: InvalidationResult | None


def _check_title(title: str) -> None:
    if not isinstance(title, str) or not title.strip():
        raise LongFormMetadataError(
            "title must be a non-empty string",
            code=LongFormMetadataErrorCode.INVALID_METADATA,
        )
    if len(title) > MAX_TITLE_LENGTH:
        raise LongFormMetadataError(
            f"title length {len(title)} exceeds {MAX_TITLE_LENGTH}",
            code=LongFormMetadataErrorCode.INVALID_METADATA,
        )
    if _FORBIDDEN_CHARS.search(title):
        raise LongFormMetadataError(
            "title contains forbidden angle bracket characters",
            code=LongFormMetadataErrorCode.INVALID_METADATA,
        )


def _check_description(description: str) -> None:
    if not isinstance(description, str) or not description.strip():
        raise LongFormMetadataError(
            "description must be a non-empty string",
            code=LongFormMetadataErrorCode.INVALID_METADATA,
        )
    if len(description) > MAX_DESCRIPTION_LENGTH:
        raise LongFormMetadataError(
            f"description length {len(description)} exceeds {MAX_DESCRIPTION_LENGTH}",
            code=LongFormMetadataErrorCode.INVALID_METADATA,
        )
    if _FORBIDDEN_CHARS.search(description):
        raise LongFormMetadataError(
            "description contains forbidden angle bracket characters",
            code=LongFormMetadataErrorCode.INVALID_METADATA,
        )


def _check_tags(tags: Sequence[str]) -> None:
    if len(tags) > MAX_TAG_COUNT:
        raise LongFormMetadataError(
            f"tags count {len(tags)} exceeds {MAX_TAG_COUNT}",
            code=LongFormMetadataErrorCode.INVALID_METADATA,
        )
    total_len = 0
    seen = set()
    for tag in tags:
        if not tag.strip():
            raise LongFormMetadataError(
                "empty tag is not permitted",
                code=LongFormMetadataErrorCode.INVALID_METADATA,
            )
        if len(tag) > MAX_TAG_LENGTH:
            raise LongFormMetadataError(
                f"tag length {len(tag)} exceeds {MAX_TAG_LENGTH}",
                code=LongFormMetadataErrorCode.INVALID_METADATA,
            )
        if _FORBIDDEN_CHARS.search(tag):
            raise LongFormMetadataError(
                "tag contains forbidden angle bracket characters",
                code=LongFormMetadataErrorCode.INVALID_METADATA,
            )
        norm = tag.lower()
        if norm in seen:
            raise LongFormMetadataError(
                f"duplicate tag: {tag}",
                code=LongFormMetadataErrorCode.INVALID_METADATA,
            )
        seen.add(norm)
        total_len += len(tag)
    if total_len > MAX_TOTAL_TAGS_LENGTH:
        raise LongFormMetadataError(
            f"total tags length {total_len} exceeds {MAX_TOTAL_TAGS_LENGTH}",
            code=LongFormMetadataErrorCode.INVALID_METADATA,
        )


class LongFormMetadataRecorder:
    """Validates, policy-checks, and records the LongForm metadata package."""

    def __init__(
        self,
        database: Database,
        storage: ArtifactStorage,
        audit: AuditLog,
        policy_catalog: PolicyRuleSetCatalog | None = None,
        flags: FeatureFlags | None = None,
        *,
        clock: Clock | None = None,
    ) -> None:
        self._database = database
        self._storage = storage
        self._audit = audit
        self._policy_catalog = policy_catalog or default_catalog()
        self._policy_checker = PolicyChecker(self._policy_catalog)
        self._flags = (
            flags if flags is not None else FeatureFlags(longform_enabled=True)
        )
        self._clock = clock

    def _require_enabled(self) -> None:
        if not self._flags.longform_enabled:
            raise LongFormDisabledError("LONGFORM_ENABLED is off")

    def record(
        self,
        project_id: str,
        *,
        expected_version: int,
        actor: Actor,
        title: str | None = None,
        description: str | None = None,
        tags: Sequence[str] = (),
        made_for_kids: bool = False,
        append_attributions: bool = True,
        embed_chapters: bool = True,
        rule_set: RuleSet = POLICY_RULES,
    ) -> LongFormMetadataRecordResult:
        """Validate, policy-check and record the LongForm metadata package."""
        self._require_enabled()

        with self._database.transaction() as connection:
            projects = LongFormProjectRepository(connection)
            project = projects.get(project_id)
            if project is None:
                raise LongFormMetadataError(
                    f"project {project_id} not found",
                    code=LongFormMetadataErrorCode.PROJECT_NOT_FOUND,
                )
            if project.version != expected_version:
                raise LongFormMetadataError(
                    f"project version {project.version} != {expected_version}",
                    code=LongFormMetadataErrorCode.CONFLICT,
                )
            if not longform_project_is_current(connection, project):
                raise LongFormMetadataError(
                    f"project {project_id} is stale",
                    code=LongFormMetadataErrorCode.STALE_INPUT,
                )
            if project.status == LongFormProjectStatus.DRAFT:
                raise LongFormMetadataError(
                    f"project {project_id} is a draft",
                    code=LongFormMetadataErrorCode.NOT_READY,
                )

            item = ContentItemRepository(connection).get(project.content_item_id)
            if item is None:
                raise LongFormMetadataError(
                    f"content item {project.content_item_id} not found",
                    code=LongFormMetadataErrorCode.PROJECT_NOT_FOUND,
                )

            strategy = StrategyProfileRepository(connection).get_by_channel(
                item.channel_id
            )
            if strategy is None:
                raise LongFormMetadataError(
                    f"channel {item.channel_id} has no strategy profile",
                    code=LongFormMetadataErrorCode.STRATEGY_MISSING,
                )

            # AI disclosure check (#081)
            disclosures = DisclosureRepository(connection)
            latest_disclosure = disclosures.latest(item.id)
            if latest_disclosure is None:
                raise LongFormMetadataError(
                    f"content item {item.id} has no recorded AI disclosure decision",
                    code=LongFormMetadataErrorCode.DISCLOSURE_MISSING,
                )
            contains_synthetic = (
                latest_disclosure.decision == DisclosureDecision.REQUIRED
            )

            # Resolve title & description
            raw_title = title if title is not None else item.title
            raw_title = unicodedata.normalize("NFC", " ".join(raw_title.split()))
            _check_title(raw_title)

            raw_desc = (
                description
                if description is not None
                else f"LongForm video for {raw_title}."
            )
            raw_desc = unicodedata.normalize("NFC", raw_desc.strip())

            # Embed YouTube chapters if configured
            if embed_chapters and project.config.chapters and project.timeline.chapters:
                chapters_text = format_youtube_chapters(project.timeline.chapters)
                if chapters_text and chapters_text not in raw_desc:
                    raw_desc = f"{raw_desc}\n\nChapters:\n{chapters_text}"

            # Append asset attributions
            if append_attributions:
                assets_repo = AssetRepository(connection)
                attributions = []
                for scene in project.timeline.scenes:
                    for asset_ref in scene.assets:
                        a = assets_repo.get(asset_ref.asset_id)
                        if (
                            a is not None
                            and a.attribution
                            and a.attribution not in attributions
                        ):
                            attributions.append(a.attribution)
                if attributions:
                    attr_block = "\n".join(f"- {attr}" for attr in attributions)
                    if attr_block not in raw_desc:
                        raw_desc = f"{raw_desc}\n\nCredits:\n{attr_block}"

            _check_description(raw_desc)
            clean_tags = tuple(unicodedata.normalize("NFC", t.strip()) for t in tags)
            _check_tags(clean_tags)

            language = (
                strategy.languages.primary
                if strategy.languages
                else canonical_language_tag("en-US")
            )

            # Policy check (#080)
            forbidden = banned_phrases(strategy)
            policy_input = PolicyInput(
                content_item_id=item.id,
                channel_id=item.channel_id,
                title=raw_title,
                description=raw_desc,
                tags=clean_tags,
                banned_phrases=forbidden,
            )
            policy_result = self._policy_checker.check(policy_input, rule_set=rule_set)
            if policy_result.status == PolicyStatus.BLOCK:
                raise LongFormMetadataError(
                    f"metadata blocked by policy rule set {rule_set.id}",
                    code=LongFormMetadataErrorCode.POLICY_BLOCKED,
                )

            # Structured chapters
            chapters_meta = tuple(
                ChapterMetadata(ch.title, ch.start_ms, ch.end_ms)
                for ch in project.timeline.chapters
            )

            metadata = LongFormMetadata(
                project_id=project.id,
                project_version=project.version,
                content_item_id=item.id,
                channel_id=item.channel_id,
                script_id=project.script_id,
                script_version=project.script_version,
                strategy_version=project.strategy_version,
                title=raw_title,
                description=raw_desc,
                tags=clean_tags,
                chapters=chapters_meta,
                language=language,
                made_for_kids=made_for_kids,
                contains_synthetic_media=contains_synthetic,
                disclosure_decision_id=latest_disclosure.id,
                policy_rule_set_id=rule_set.id,
                policy_rule_set_version=rule_set.version,
                policy_status=policy_result.status,
            )
            raw_bytes = metadata.to_bytes()
            sha256 = hashlib.sha256(raw_bytes).hexdigest()

            # Check for replay
            artifacts_repo = ArtifactRepository(connection)
            latest_meta = artifacts_repo.latest(item.id, ArtifactKind.METADATA)
            if latest_meta is not None and latest_meta.sha256 == sha256:
                return LongFormMetadataRecordResult(
                    artifact=latest_meta,
                    metadata=metadata,
                    created=False,
                    invalidation=None,
                )
            next_version = 1 if latest_meta is None else latest_meta.version + 1

        # Write metadata file outside transaction
        written_file: StoredFile | None = None
        invalidation: InvalidationResult | None = None

        try:
            written_file = self._storage.write(
                item.id,
                ArtifactKind.METADATA,
                next_version,
                raw_bytes,
                media_type=METADATA_MEDIA_TYPE,
            )

            with self._database.transaction() as connection:
                # Re-verify currency
                p = LongFormProjectRepository(connection).get(project.id)
                if p is None or p.version != expected_version:
                    raise LongFormMetadataError(
                        "project changed concurrently",
                        code=LongFormMetadataErrorCode.CONFLICT,
                    )
                if not longform_project_is_current(connection, p):
                    raise LongFormMetadataError(
                        "project became stale concurrently",
                        code=LongFormMetadataErrorCode.STALE_INPUT,
                    )

                artifacts_repo = ArtifactRepository(connection)
                curr_meta = artifacts_repo.latest(item.id, ArtifactKind.METADATA)
                if curr_meta is not None and curr_meta.version >= next_version:
                    raise LongFormMetadataError(
                        "metadata version conflict",
                        code=LongFormMetadataErrorCode.CONFLICT,
                    )

                if curr_meta is None:
                    artifact = Artifact.create(
                        item.id,
                        ArtifactKind.METADATA,
                        uri=written_file.uri,
                        sha256=sha256,
                        size_bytes=len(raw_bytes),
                        media_type=METADATA_MEDIA_TYPE,
                        clock=self._clock,
                    )
                else:
                    artifact = curr_meta.next_version(
                        uri=written_file.uri,
                        sha256=sha256,
                        size_bytes=len(raw_bytes),
                        media_type=METADATA_MEDIA_TYPE,
                        clock=self._clock,
                    )
                artifacts_repo.add(artifact)

                # Invalidate stale approvals (#035)
                invalidation = invalidate_stale_approvals(
                    connection, item.id, clock=self._clock
                )

        except Exception:
            if written_file is not None:
                self._storage.delete(written_file.uri)
            raise

        if invalidation is not None:
            record_invalidations(self._audit, invalidation)
        self._audit.record(
            METADATA_RECORDED,
            actor,
            EntityRef("longform_project", project.id),
            AuditResult.SUCCESS,
            {
                "content_item_id": item.id,
                "artifact_id": artifact.id,
                "version": artifact.version,
                "size_bytes": artifact.size_bytes,
                "title_length": len(metadata.title),
                "description_length": len(metadata.description),
                "tags_count": len(metadata.tags),
                "chapters_count": len(metadata.chapters),
                "sha256": artifact.sha256,
            },
        )

        return LongFormMetadataRecordResult(
            artifact=artifact,
            metadata=metadata,
            created=True,
            invalidation=invalidation,
        )
