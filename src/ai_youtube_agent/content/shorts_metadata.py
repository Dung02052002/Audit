"""Shorts Metadata (Prompt Pack v8, prompt I-102), context C8 Video Production.

``ShortsMetadata`` is the canonical YouTube metadata package for a Shorts
``ShortsProject`` (#094), and ``ShortsMetadataRecorder`` validates and stores it
as the item's ``ArtifactKind.METADATA`` artifact (``application/json``):

- Bound to the current ``ShortsProject`` (``project_id``, ``project_version``,
  ``content_item_id``, ``channel_id``, ``script_id``, ``script_version``,
  ``strategy_version``).
- Text fields:
  - ``title``: NFC normalized, single-space collapsed, 1..100 characters,
    no ``<`` or ``>`` angle brackets. Defaults to ``ContentItem.title`` when
    omitted in ``record``.
  - ``description``: NFC normalized, ``\\n`` line endings, outer whitespace
    stripped, 1..5000 characters, no ``<`` or ``>`` angle brackets. Any
    non-empty ``attribution`` lines from assets referenced in the project's
    timeline are appended if not already present, and ``#Shorts`` is ensured
    when ``ensure_shorts_marker=True`` (the default).
  - ``tags``: 0..30 unique (case-insensitive) NFC-normalized tags, each 1..100
    characters with no ``<`` or ``>``, total combined length at most 500
    characters.
  - Shorts marker rule: ``#shorts`` (case-insensitive) must appear as a hashtag
    token in ``title`` or ``description``, or ``"shorts"`` must appear in
    ``tags``.
- Compliance & Policy:
  - ``language``: canonical BCP-47 tag from ``StrategyProfile.languages.primary``
    (or explicit ``language``).
  - ``made_for_kids``: explicit boolean (defaults to ``False``).
  - AI disclosure (#081): requires a recorded ``DisclosureRecord`` on the item
    (refuses with ``DISCLOSURE_MISSING`` if none exists) and binds
    ``disclosure_decision_id`` and ``contains_synthetic_media``
    (``True`` when ``DisclosureDecision.REQUIRED``).
  - Policy check (#080): evaluates ``PolicyInput`` against the configured
    ``RuleSet`` and the channel strategy's ``banned_phrases``; refuses with
    ``POLICY_BLOCKED`` when the policy status is ``BLOCK``.
- Storage & Versioning:
  - Serialised as deterministic UTF-8 JSON (``METADATA_SCHEMA_VERSION = 1``)
    and stored via ``ArtifactStorage`` under ``ArtifactKind.METADATA``.
  - Replays with ``created=False`` when the latest ``METADATA`` artifact has
    the same ``sha256``.
  - Invalidates stale approvals (#035) in the same transaction on a new version,
    cleans up the written file on failure, and audits
    ``shorts.metadata_recorded`` with scalar metadata only (never title,
    description or tag text).
"""

import hashlib
import json
import re
import sqlite3
import unicodedata
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from http import HTTPStatus
from typing import Any, Final

from ai_youtube_agent.content.disclosure import DisclosureDecision, DisclosureRecord
from ai_youtube_agent.content.policy_check import (
    POLICY_RULES,
    PolicyChecker,
    PolicyInput,
    PolicyRuleSetCatalog,
    PolicyStatus,
    default_catalog,
)
from ai_youtube_agent.content.policy_rule import RuleSet
from ai_youtube_agent.content.shorts_project import ShortsProject
from ai_youtube_agent.content.shorts_service import (
    ShortsProjectNotFoundError,
    project_is_current,
)
from ai_youtube_agent.content.strategy import (
    LANGUAGE_TAG_PATTERN,
    StrategyProfile,
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
    MetadataValue,
)
from ai_youtube_agent.core.content_item import ContentItem
from ai_youtube_agent.core.db.database import Database
from ai_youtube_agent.core.db.repositories.asset import AssetRepository
from ai_youtube_agent.core.db.repositories.channel import StrategyProfileRepository
from ai_youtube_agent.core.db.repositories.content import (
    ArtifactRepository,
    ContentItemRepository,
)
from ai_youtube_agent.core.db.repositories.disclosure import DisclosureRepository
from ai_youtube_agent.core.db.repositories.shorts import ShortsProjectRepository
from ai_youtube_agent.core.errors import DomainError
from ai_youtube_agent.core.log import get_logger
from ai_youtube_agent.core.version_invalidation import (
    InvalidationResult,
    invalidate_stale_approvals,
    record_invalidations,
)

logger = get_logger(__name__)

METADATA_RECORDED: Final[str] = "shorts.metadata_recorded"
METADATA_MEDIA_TYPE: Final[str] = "application/json"
METADATA_SCHEMA_VERSION: Final[int] = 1
MAX_TITLE_CHARS: Final[int] = 100
MAX_DESCRIPTION_CHARS: Final[int] = 5000
MAX_TAG_CHARS: Final[int] = 100
MAX_TAGS_TOTAL_CHARS: Final[int] = 500
MAX_TAG_COUNT: Final[int] = 30
_SHORTS_HASHTAG_RE: Final[re.Pattern[str]] = re.compile(
    r"(?:^|\s)#shorts(?=$|\s|[.,!?;:])", re.IGNORECASE
)
_METADATA_KEYS: Final[frozenset[str]] = frozenset(
    {
        "schema_version",
        "project_id",
        "project_version",
        "content_item_id",
        "channel_id",
        "script_id",
        "script_version",
        "strategy_version",
        "title",
        "description",
        "tags",
        "language",
        "made_for_kids",
        "contains_synthetic_media",
        "disclosure_decision_id",
        "policy_rule_set_id",
        "policy_rule_set_version",
        "policy_status",
    }
)

Clock = Callable[[], datetime]


class ShortsMetadataErrorCode(StrEnum):
    METADATA = "domain.shorts_metadata"
    TIMELINE_MISSING = "domain.shorts_metadata_timeline_missing"
    STALE_INPUT = "domain.shorts_metadata_stale_input"
    STRATEGY_MISSING = "domain.shorts_metadata_strategy_missing"
    DISCLOSURE_MISSING = "domain.shorts_metadata_disclosure_missing"
    POLICY_BLOCKED = "domain.shorts_metadata_policy_blocked"
    INVALID_METADATA = "domain.shorts_metadata_invalid"


USER_MESSAGES: Final[dict[ShortsMetadataErrorCode, str]] = {
    ShortsMetadataErrorCode.METADATA: (
        "The Shorts metadata could not be recorded."
    ),
    ShortsMetadataErrorCode.TIMELINE_MISSING: (
        "This Shorts project does not have a timeline yet."
    ),
    ShortsMetadataErrorCode.STALE_INPUT: (
        "The Shorts project is out of date with its content item, script or audio."
    ),
    ShortsMetadataErrorCode.STRATEGY_MISSING: (
        "The channel does not have a strategy profile."
    ),
    ShortsMetadataErrorCode.DISCLOSURE_MISSING: (
        "An AI disclosure decision must be recorded before Shorts metadata."
    ),
    ShortsMetadataErrorCode.POLICY_BLOCKED: (
        "The Shorts metadata was blocked by a policy rule."
    ),
    ShortsMetadataErrorCode.INVALID_METADATA: (
        "The Shorts title, description, tags or language are invalid."
    ),
}


class ShortsMetadataError(DomainError):
    default_code = ShortsMetadataErrorCode.METADATA.value
    default_user_message = USER_MESSAGES[ShortsMetadataErrorCode.METADATA]
    default_http_status = HTTPStatus.UNPROCESSABLE_ENTITY

    def __init__(
        self,
        detail: str = "",
        *,
        code: ShortsMetadataErrorCode = ShortsMetadataErrorCode.METADATA,
        http_status: int | None = None,
    ) -> None:
        status = (
            http_status
            if http_status is not None
            else (
                HTTPStatus.CONFLICT
                if code
                in (
                    ShortsMetadataErrorCode.TIMELINE_MISSING,
                    ShortsMetadataErrorCode.STALE_INPUT,
                    ShortsMetadataErrorCode.STRATEGY_MISSING,
                    ShortsMetadataErrorCode.DISCLOSURE_MISSING,
                )
                else HTTPStatus.UNPROCESSABLE_ENTITY
            )
        )
        super().__init__(
            detail,
            code=code.value,
            user_message=USER_MESSAGES[code],
            http_status=status,
        )


class ShortsMetadataConflictError(DomainError):
    default_code = "domain.shorts_metadata_conflict"
    default_user_message = (
        "This Shorts project was updated by someone else; reload and try again."
    )
    default_http_status = HTTPStatus.CONFLICT


@dataclass(frozen=True)
class ShortsMetadata:
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
    language: str
    made_for_kids: bool
    contains_synthetic_media: bool
    disclosure_decision_id: str
    policy_rule_set_id: str
    policy_rule_set_version: int
    policy_status: PolicyStatus
    schema_version: int = METADATA_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if (
            isinstance(self.schema_version, bool)
            or self.schema_version != METADATA_SCHEMA_VERSION
        ):
            raise ValueError(
                f"schema_version must be {METADATA_SCHEMA_VERSION}"
            )
        for name in (
            "project_id",
            "content_item_id",
            "channel_id",
            "script_id",
            "disclosure_decision_id",
            "policy_rule_set_id",
        ):
            val = getattr(self, name)
            if not isinstance(val, str) or not val.strip():
                raise ValueError(f"{name} must not be empty")
        for int_name in (
            "project_version",
            "script_version",
            "strategy_version",
            "policy_rule_set_version",
        ):
            num = getattr(self, int_name)
            if isinstance(num, bool) or not isinstance(num, int) or num < 1:
                raise ValueError(f"{int_name} must be an integer >= 1")
        object.__setattr__(self, "title", _validate_title(self.title))
        object.__setattr__(
            self, "description", _validate_description(self.description)
        )
        object.__setattr__(self, "tags", _validate_tags(self.tags))
        if not has_shorts_marker(self.title, self.description, self.tags):
            raise ValueError(
                "Shorts metadata requires #Shorts in title/description or 'Shorts' in tags"
            )
        if not isinstance(self.language, str) or not LANGUAGE_TAG_PATTERN.match(
            self.language
        ):
            raise ValueError(
                f"language {self.language!r} must be a canonical BCP-47 tag"
            )
        if not isinstance(self.made_for_kids, bool):
            raise TypeError("made_for_kids must be a bool")
        if not isinstance(self.contains_synthetic_media, bool):
            raise TypeError("contains_synthetic_media must be a bool")
        if not isinstance(self.policy_status, PolicyStatus):
            raise TypeError("policy_status must be a PolicyStatus")
        if self.policy_status is PolicyStatus.BLOCK:
            raise ValueError("blocked policy_status cannot be stored in ShortsMetadata")

    def to_dict(self) -> dict[str, Any]:
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
            "language": self.language,
            "made_for_kids": self.made_for_kids,
            "contains_synthetic_media": self.contains_synthetic_media,
            "disclosure_decision_id": self.disclosure_decision_id,
            "policy_rule_set_id": self.policy_rule_set_id,
            "policy_rule_set_version": self.policy_rule_set_version,
            "policy_status": self.policy_status.value,
        }

    def to_json_bytes(self) -> bytes:
        text = (
            json.dumps(
                self.to_dict(),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        )
        return text.encode("utf-8")

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.to_json_bytes()).hexdigest()

    @classmethod
    def from_dict(cls, data: object) -> "ShortsMetadata":
        if not isinstance(data, Mapping):
            raise ValueError("ShortsMetadata must be a JSON object")
        unknown = sorted(str(k) for k in set(data) - _METADATA_KEYS)
        if unknown:
            raise ValueError(f"ShortsMetadata has unknown keys: {', '.join(unknown)}")
        missing = sorted(_METADATA_KEYS - set(data))
        if missing:
            raise ValueError(f"ShortsMetadata is missing keys: {', '.join(missing)}")
        raw_tags = data["tags"]
        if not isinstance(raw_tags, list | tuple):
            raise ValueError("tags must be a list")
        try:
            status = PolicyStatus(data["policy_status"])
        except (ValueError, TypeError) as error:
            raise ValueError(
                f"invalid policy_status: {data['policy_status']!r}"
            ) from error
        return cls(
            schema_version=data["schema_version"],
            project_id=data["project_id"],
            project_version=data["project_version"],
            content_item_id=data["content_item_id"],
            channel_id=data["channel_id"],
            script_id=data["script_id"],
            script_version=data["script_version"],
            strategy_version=data["strategy_version"],
            title=data["title"],
            description=data["description"],
            tags=tuple(raw_tags),
            language=data["language"],
            made_for_kids=data["made_for_kids"],
            contains_synthetic_media=data["contains_synthetic_media"],
            disclosure_decision_id=data["disclosure_decision_id"],
            policy_rule_set_id=data["policy_rule_set_id"],
            policy_rule_set_version=data["policy_rule_set_version"],
            policy_status=status,
        )

    @classmethod
    def from_json_bytes(cls, data: bytes) -> "ShortsMetadata":
        if not isinstance(data, bytes | bytearray):
            raise TypeError("data must be bytes")
        try:
            decoded = json.loads(bytes(data).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ValueError(f"invalid ShortsMetadata JSON: {error}") from error
        return cls.from_dict(decoded)


@dataclass(frozen=True)
class ShortsMetadataRecord:
    artifact: Artifact
    metadata: ShortsMetadata
    project: ShortsProject
    created: bool
    invalidation: InvalidationResult | None


def has_shorts_marker(
    title: str, description: str, tags: Sequence[str]
) -> bool:
    """True when ``#Shorts`` is in title/description or ``Shorts`` is in tags."""
    if _SHORTS_HASHTAG_RE.search(title) or _SHORTS_HASHTAG_RE.search(description):
        return True
    return any(tag.casefold() in ("shorts", "#shorts") for tag in tags)


class ShortsMetadataRecorder:
    def __init__(
        self,
        database: Database,
        storage: ArtifactStorage,
        audit: AuditLog,
        *,
        catalog: PolicyRuleSetCatalog | None = None,
        rule_set: RuleSet = POLICY_RULES,
        clock: Clock | None = None,
    ) -> None:
        self._database = database
        self._storage = storage
        self._audit = audit
        self._catalog = catalog if catalog is not None else default_catalog()
        self._checker = PolicyChecker(self._catalog)
        self._rule_set = rule_set
        self._clock = clock

    def record(
        self,
        project_id: str,
        *,
        title: str | None = None,
        description: str,
        tags: Sequence[str] = (),
        language: str | None = None,
        made_for_kids: bool = False,
        ensure_shorts_marker: bool = True,
        rule_set: RuleSet | None = None,
        actor: Actor,
        expected_version: int | None = None,
    ) -> ShortsMetadataRecord:
        if not isinstance(project_id, str):
            raise TypeError("project_id must be text")
        if title is not None and not isinstance(title, str):
            raise TypeError("title must be text or None")
        if not isinstance(description, str):
            raise TypeError("description must be text")
        if not isinstance(tags, tuple | list) or isinstance(tags, str | bytes):
            raise TypeError("tags must be a sequence of strings")
        if language is not None and not isinstance(language, str):
            raise TypeError("language must be text or None")
        if not isinstance(made_for_kids, bool):
            raise TypeError("made_for_kids must be a bool")
        if not isinstance(ensure_shorts_marker, bool):
            raise TypeError("ensure_shorts_marker must be a bool")
        if not isinstance(actor, Actor):
            raise TypeError("actor must be an Actor")
        if expected_version is not None and (
            isinstance(expected_version, bool)
            or not isinstance(expected_version, int)
            or expected_version < 1
        ):
            raise ValueError("expected_version must be an integer >= 1")
        active_rule_set = rule_set if rule_set is not None else self._rule_set
        if not isinstance(active_rule_set, RuleSet):
            raise TypeError("rule_set must be a RuleSet")
        self._catalog.rules_for(active_rule_set)

        written: StoredFile | None = None
        try:
            with self._database.transaction() as connection:
                project, item, strategy, disclosure = _load_inputs(
                    connection, project_id, expected_version
                )
                attributions = _timeline_attributions(connection, project)
                metadata = self._build_metadata(
                    project=project,
                    item=item,
                    strategy=strategy,
                    disclosure=disclosure,
                    attributions=attributions,
                    raw_title=title if title is not None else item.title,
                    raw_description=description,
                    raw_tags=tags,
                    raw_language=language,
                    made_for_kids=made_for_kids,
                    ensure_shorts_marker=ensure_shorts_marker,
                    rule_set=active_rule_set,
                )
                payload = metadata.to_json_bytes()
                checksum = metadata.sha256
                artifacts = ArtifactRepository(connection)
                latest = artifacts.latest(item.id, ArtifactKind.METADATA)
                if latest is not None and latest.sha256 == checksum:
                    return ShortsMetadataRecord(
                        latest, metadata, project, False, None
                    )
                version = latest.version + 1 if latest else 1
                written = self._storage.write(
                    item.id,
                    ArtifactKind.METADATA,
                    version,
                    payload,
                    media_type=METADATA_MEDIA_TYPE,
                )
                artifact = _artifact(latest, item.id, written, self._clock)
                artifacts.add(artifact)
                invalidation = invalidate_stale_approvals(
                    connection, item.id, clock=self._clock
                )
        except BaseException:
            if written is not None:
                self._discard(written.uri)
            raise
        record_invalidations(self._audit, invalidation)
        self._audit.record(
            METADATA_RECORDED,
            actor,
            EntityRef("artifact", artifact.id),
            AuditResult.SUCCESS,
            _event_metadata(artifact, project, metadata),
        )
        return ShortsMetadataRecord(artifact, metadata, project, True, invalidation)

    def _build_metadata(
        self,
        *,
        project: ShortsProject,
        item: ContentItem,
        strategy: StrategyProfile,
        disclosure: DisclosureRecord,
        attributions: tuple[str, ...],
        raw_title: str,
        raw_description: str,
        raw_tags: Sequence[str],
        raw_language: str | None,
        made_for_kids: bool,
        ensure_shorts_marker: bool,
        rule_set: RuleSet,
    ) -> ShortsMetadata:
        try:
            clean_title = _validate_title(raw_title)
            clean_tags = _validate_tags(raw_tags)
            desc_body = _compose_description(
                raw_description,
                attributions=attributions,
                title=clean_title,
                tags=clean_tags,
                ensure_shorts_marker=ensure_shorts_marker,
            )
            clean_description = _validate_description(desc_body)
            resolved_lang = _resolve_language(raw_language, strategy)
        except (ValueError, TypeError) as error:
            raise ShortsMetadataError(
                str(error),
                code=ShortsMetadataErrorCode.INVALID_METADATA,
            ) from error

        if not has_shorts_marker(clean_title, clean_description, clean_tags):
            raise ShortsMetadataError(
                "Shorts metadata requires #Shorts in title/description or 'Shorts' in tags",
                code=ShortsMetadataErrorCode.INVALID_METADATA,
            )

        policy_input = PolicyInput(
            content_item_id=item.id,
            channel_id=item.channel_id,
            title=clean_title,
            description=clean_description,
            tags=clean_tags,
            banned_phrases=banned_phrases(strategy),
        )
        policy_result = self._checker.check(policy_input, rule_set=rule_set)
        if policy_result.status is PolicyStatus.BLOCK:
            codes = ", ".join(
                o.code for o in policy_result.outcomes if o.status is PolicyStatus.BLOCK
            )
            raise ShortsMetadataError(
                f"policy check blocked Shorts metadata ({codes})",
                code=ShortsMetadataErrorCode.POLICY_BLOCKED,
            )

        return ShortsMetadata(
            project_id=project.id,
            project_version=project.version,
            content_item_id=item.id,
            channel_id=item.channel_id,
            script_id=project.script_id,
            script_version=project.script_version,
            strategy_version=project.strategy_version,
            title=clean_title,
            description=clean_description,
            tags=clean_tags,
            language=resolved_lang,
            made_for_kids=made_for_kids,
            contains_synthetic_media=(
                disclosure.decision is DisclosureDecision.REQUIRED
            ),
            disclosure_decision_id=disclosure.id,
            policy_rule_set_id=rule_set.id,
            policy_rule_set_version=rule_set.version,
            policy_status=policy_result.status,
        )

    def _discard(self, uri: str) -> None:
        try:
            self._storage.delete(uri)
        except Exception:
            logger.warning(
                "shorts metadata file could not be removed after a failed record",
                extra={"fields": {"uri": uri}},
            )


def _load_inputs(
    connection: sqlite3.Connection,
    project_id: str,
    expected_version: int | None,
) -> tuple[ShortsProject, ContentItem, StrategyProfile, DisclosureRecord]:
    project = ShortsProjectRepository(connection).get(project_id)
    if project is None:
        raise ShortsProjectNotFoundError(
            f"Shorts project {project_id} does not exist"
        )
    if expected_version is not None and project.version != expected_version:
        raise ShortsMetadataConflictError(
            f"Shorts project {project.id} is at version {project.version}, "
            f"expected {expected_version}"
        )
    item = ContentItemRepository(connection).get(project.content_item_id)
    if item is None or not project_is_current(connection, project):
        raise ShortsMetadataError(
            f"Shorts project {project.id} is no longer current for content item "
            f"{project.content_item_id}",
            code=ShortsMetadataErrorCode.STALE_INPUT,
        )
    if not project.timeline.scenes:
        raise ShortsMetadataError(
            f"Shorts project {project.id} has no timeline scenes",
            code=ShortsMetadataErrorCode.TIMELINE_MISSING,
        )
    strategy = StrategyProfileRepository(connection).get_by_channel(item.channel_id)
    if strategy is None:
        raise ShortsMetadataError(
            f"channel {item.channel_id} has no strategy profile",
            code=ShortsMetadataErrorCode.STRATEGY_MISSING,
        )
    disclosure = DisclosureRepository(connection).latest(item.id)
    if disclosure is None:
        raise ShortsMetadataError(
            f"content item {item.id} has no AI disclosure decision",
            code=ShortsMetadataErrorCode.DISCLOSURE_MISSING,
        )
    return project, item, strategy, disclosure


def _timeline_attributions(
    connection: sqlite3.Connection, project: ShortsProject
) -> tuple[str, ...]:
    asset_ids: list[str] = []
    seen_ids: set[str] = set()
    for scene in project.timeline.scenes:
        for placed in scene.assets:
            if placed.asset_id not in seen_ids:
                seen_ids.add(placed.asset_id)
                asset_ids.append(placed.asset_id)
    for music in project.timeline.music:
        if music.asset_id not in seen_ids:
            seen_ids.add(music.asset_id)
            asset_ids.append(music.asset_id)
    if not asset_ids:
        return ()
    repo = AssetRepository(connection)
    lines: list[str] = []
    seen_lines: set[str] = set()
    for aid in asset_ids:
        asset = repo.get(aid)
        if asset is not None and asset.attribution:
            line = " ".join(unicodedata.normalize("NFC", asset.attribution).split())
            key = line.casefold()
            if line and key not in seen_lines:
                seen_lines.add(key)
                lines.append(line)
    return tuple(lines)


def _compose_description(
    raw_description: str,
    *,
    attributions: tuple[str, ...],
    title: str,
    tags: tuple[str, ...],
    ensure_shorts_marker: bool,
) -> str:
    text = unicodedata.normalize("NFC", raw_description)
    text = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    missing_credits = [
        credit
        for credit in attributions
        if credit.casefold() not in text.casefold()
    ]
    if missing_credits:
        credits_block = "\n".join(f"Attribution: {c}" for c in missing_credits)
        text = f"{text}\n\n{credits_block}" if text else credits_block
    if ensure_shorts_marker and not has_shorts_marker(title, text, tags):
        text = f"{text}\n\n#Shorts" if text else "#Shorts"
    return text


def _validate_title(value: object) -> str:
    if not isinstance(value, str):
        raise TypeError("title must be text")
    cleaned = " ".join(unicodedata.normalize("NFC", value).split())
    if not cleaned:
        raise ValueError("title must not be empty")
    if len(cleaned) > MAX_TITLE_CHARS:
        raise ValueError(f"title must be at most {MAX_TITLE_CHARS} characters")
    if "<" in cleaned or ">" in cleaned:
        raise ValueError("title must not contain '<' or '>' characters")
    return cleaned


def _validate_description(value: object) -> str:
    if not isinstance(value, str):
        raise TypeError("description must be text")
    normalized = (
        unicodedata.normalize("NFC", value)
        .replace("\r\n", "\n")
        .replace("\r", "\n")
        .strip()
    )
    if not normalized:
        raise ValueError("description must not be empty")
    if len(normalized) > MAX_DESCRIPTION_CHARS:
        raise ValueError(
            f"description must be at most {MAX_DESCRIPTION_CHARS} characters"
        )
    if "<" in normalized or ">" in normalized:
        raise ValueError("description must not contain '<' or '>' characters")
    return normalized


def _validate_tags(values: object) -> tuple[str, ...]:
    if not isinstance(values, tuple | list) or isinstance(values, str | bytes):
        raise TypeError("tags must be a tuple or list of strings")
    cleaned: list[str] = []
    seen: set[str] = set()
    for raw in values:
        if not isinstance(raw, str):
            raise TypeError("every tag must be text")
        tag = " ".join(unicodedata.normalize("NFC", raw).split())
        if not tag:
            raise ValueError("tags must not contain empty strings")
        if len(tag) > MAX_TAG_CHARS:
            raise ValueError(f"each tag must be at most {MAX_TAG_CHARS} characters")
        if "<" in tag or ">" in tag:
            raise ValueError("tags must not contain '<' or '>' characters")
        key = tag.casefold()
        if key not in seen:
            seen.add(key)
            cleaned.append(tag)
    if len(cleaned) > MAX_TAG_COUNT:
        raise ValueError(f"at most {MAX_TAG_COUNT} tags are allowed")
    total_chars = sum(len(t) for t in cleaned)
    if total_chars > MAX_TAGS_TOTAL_CHARS:
        raise ValueError(
            f"tags total length ({total_chars}) must be at most "
            f"{MAX_TAGS_TOTAL_CHARS} characters"
        )
    return tuple(cleaned)


def _resolve_language(
    raw_language: str | None, strategy: StrategyProfile
) -> str:
    if raw_language is not None:
        if not raw_language.strip():
            raise ValueError("language must not be empty")
        tag = canonical_language_tag(raw_language)
    elif strategy.languages is not None:
        tag = strategy.languages.primary
    else:
        raise ValueError("language is required when strategy has no primary language")
    if not LANGUAGE_TAG_PATTERN.match(tag):
        raise ValueError(f"language {tag!r} must be a valid BCP-47 tag")
    return tag


def _artifact(
    latest: Artifact | None,
    item_id: str,
    stored: StoredFile,
    clock: Clock | None,
) -> Artifact:
    if latest is None:
        return Artifact.create(
            item_id,
            ArtifactKind.METADATA,
            uri=stored.uri,
            sha256=stored.sha256,
            size_bytes=stored.size_bytes,
            media_type=METADATA_MEDIA_TYPE,
            clock=clock,
        )
    return latest.next_version(
        uri=stored.uri,
        sha256=stored.sha256,
        size_bytes=stored.size_bytes,
        media_type=METADATA_MEDIA_TYPE,
        clock=clock,
    )


def _event_metadata(
    artifact: Artifact,
    project: ShortsProject,
    metadata: ShortsMetadata,
) -> Mapping[str, MetadataValue]:
    return {
        "content_item_id": artifact.content_item_id,
        "channel_id": metadata.channel_id,
        "project_id": project.id,
        "project_version": project.version,
        "version": artifact.version,
        "sha256": artifact.sha256,
        "size_bytes": artifact.size_bytes,
        "title_length": len(metadata.title),
        "description_length": len(metadata.description),
        "tag_count": len(metadata.tags),
        "language": metadata.language,
        "made_for_kids": metadata.made_for_kids,
        "contains_synthetic_media": metadata.contains_synthetic_media,
        "disclosure_decision_id": metadata.disclosure_decision_id,
        "policy_rule_set_id": metadata.policy_rule_set_id,
        "policy_rule_set_version": metadata.policy_rule_set_version,
        "policy_status": metadata.policy_status.value,
    }

