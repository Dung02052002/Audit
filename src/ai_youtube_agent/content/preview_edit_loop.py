"""Preview Edit Loop service (Prompt Pack v8, prompt L-136), context C11 Review.

Implements the ``change -> rerender -> retest -> new preview`` workflow (#136):
1. **Change**: Applies requested edits (title, description, tags, subtitle/thumbnail
   regeneration, or project rebind) and moves the ``ContentItem`` back to
   ``ContentStatus.GENERATING`` (#032).
2. **Rerender**: Produces the updated artifact versions (``VIDEO``, ``METADATA``,
   ``SUBTITLES``, ``THUMBNAIL``) and invalidates any stale ``PENDING`` or
   ``APPROVED`` approval requests in the same transaction (#035). Moves the item
   to ``ContentStatus.TESTING``.
3. **Retest**: Runs ``QCReportBuilder.evaluate`` across all 12 Phase K technical
   and compliance checks (``video.*`` + ``qc.rights``, ``qc.policy``,
   ``qc.ai_disclosure``) bound to the new artifact versions.
4. **New Preview**: When QC passes or warns (``qc_report.status != QCStatus.FAIL``),
   transitions the item from ``TESTING -> PREVIEW_READY`` and returns the new
   ``PreviewSnapshot``, ``QCReport``, and ``VersionComparisonResult``.
"""

import contextlib
import hashlib
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum
from http import HTTPStatus
from typing import Any, Final

from ai_youtube_agent.content.longform_metadata import LongFormMetadata
from ai_youtube_agent.content.longform_thumbnail import (
    generate_longform_thumbnail_png,
)
from ai_youtube_agent.content.policy_check import (
    PolicyChecker,
    PolicyInput,
    PolicyStatus,
)
from ai_youtube_agent.content.policy_rule import RuleSet
from ai_youtube_agent.content.preview_artifact import (
    PreviewArtifactService,
    PreviewSnapshot,
)
from ai_youtube_agent.content.qc import QCStatus
from ai_youtube_agent.content.qc_report import QCReport, QCReportBuilder
from ai_youtube_agent.content.shorts_metadata import ShortsMetadata
from ai_youtube_agent.content.shorts_thumbnail import generate_thumbnail_png
from ai_youtube_agent.content.version_comparison import (
    VersionComparisonResult,
    VersionComparisonService,
)
from ai_youtube_agent.core.artifact import Artifact, ArtifactKind
from ai_youtube_agent.core.artifact_storage import ArtifactStorage
from ai_youtube_agent.core.audit import (
    Actor,
    ActorKind,
    AuditLog,
    AuditResult,
    EntityRef,
)
from ai_youtube_agent.core.config import Settings
from ai_youtube_agent.core.content_item import (
    ContentItem,
    ContentStatus,
    ContentType,
)
from ai_youtube_agent.core.db.database import Database
from ai_youtube_agent.core.db.repositories.channel import StrategyProfileRepository
from ai_youtube_agent.core.db.repositories.content import (
    ArtifactRepository,
    ContentItemRepository,
)
from ai_youtube_agent.core.db.repositories.jobs import AIJobRepository
from ai_youtube_agent.core.errors import DomainError
from ai_youtube_agent.core.version_invalidation import (
    InvalidationResult,
    invalidate_stale_approvals,
    record_invalidations,
)
from ai_youtube_agent.pipeline.idempotency import render_key
from ai_youtube_agent.pipeline.job import AIJob
from ai_youtube_agent.providers.mock_video_render import stub_mp4
from ai_youtube_agent.providers.video_render import probe_mp4

Clock = Callable[[], datetime]

PREVIEW_EDIT_LOOP_COMPLETED: Final[str] = "preview.edit_loop_completed"

EDITABLE_ITEM_STATUSES: Final[frozenset[ContentStatus]] = frozenset(
    {
        ContentStatus.GENERATING,
        ContentStatus.TESTING,
        ContentStatus.PREVIEW_READY,
        ContentStatus.AWAITING_APPROVAL,
        ContentStatus.APPROVED,
    }
)


class PreviewEditErrorCode(StrEnum):
    EDIT_LOOP = "domain.preview_edit_loop"
    ITEM_NOT_FOUND = "domain.preview_edit_item_not_found"
    INVALID_STATUS = "domain.preview_edit_invalid_status"
    NOT_ALLOWED = "domain.preview_edit_not_allowed"
    ARTIFACT_MISSING = "domain.preview_edit_artifact_missing"
    POLICY_BLOCKED = "domain.preview_edit_policy_blocked"
    RENDER_FAILED = "domain.preview_edit_render_failed"


USER_MESSAGES: Final[dict[PreviewEditErrorCode, str]] = {
    PreviewEditErrorCode.EDIT_LOOP: "The preview edit loop could not be completed.",
    PreviewEditErrorCode.ITEM_NOT_FOUND: "This content item does not exist.",
    PreviewEditErrorCode.INVALID_STATUS: (
        "This content item cannot enter the preview edit loop in its current status."
    ),
    PreviewEditErrorCode.NOT_ALLOWED: (
        "Only a user or the system pipeline may run the preview edit loop."
    ),
    PreviewEditErrorCode.ARTIFACT_MISSING: (
        "Required artifacts are missing to re-render the preview."
    ),
    PreviewEditErrorCode.POLICY_BLOCKED: (
        "The edited metadata violates channel or platform policy rules."
    ),
    PreviewEditErrorCode.RENDER_FAILED: "Re-rendering the video artifact failed.",
}


class PreviewEditError(DomainError):
    default_code = PreviewEditErrorCode.EDIT_LOOP.value
    default_user_message = USER_MESSAGES[PreviewEditErrorCode.EDIT_LOOP]
    default_http_status = HTTPStatus.UNPROCESSABLE_ENTITY

    def __init__(
        self,
        detail: str = "",
        *,
        code: PreviewEditErrorCode = PreviewEditErrorCode.EDIT_LOOP,
        http_status: int | None = None,
    ) -> None:
        status = (
            http_status
            if http_status is not None
            else (
                HTTPStatus.NOT_FOUND
                if code is PreviewEditErrorCode.ITEM_NOT_FOUND
                else (
                    HTTPStatus.FORBIDDEN
                    if code is PreviewEditErrorCode.NOT_ALLOWED
                    else HTTPStatus.UNPROCESSABLE_ENTITY
                )
            )
        )
        super().__init__(
            detail,
            code=code.value,
            user_message=USER_MESSAGES.get(
                code, USER_MESSAGES[PreviewEditErrorCode.EDIT_LOOP]
            ),
        )
        self.default_http_status = HTTPStatus(status)


@dataclass(frozen=True)
class PreviewEditChanges:
    """Changes requested for a ``change -> rerender -> retest -> new preview`` cycle."""

    title: str | None = None
    description: str | None = None
    tags: tuple[str, ...] | None = None
    rerender_video: bool = True
    regenerate_thumbnail: bool = False
    rendered_video_bytes: bytes | None = None
    subtitle_bytes: bytes | None = None
    subtitle_media_type: str | None = None

    def __post_init__(self) -> None:
        if self.title is not None and not self.title.strip():
            raise ValueError("title must not be blank when provided")
        if self.description is not None and not self.description.strip():
            raise ValueError("description must not be blank when provided")
        if self.tags is not None:
            if not isinstance(self.tags, tuple):
                raise TypeError("tags must be a tuple of strings")
            if any(not isinstance(t, str) or not t.strip() for t in self.tags):
                raise ValueError("tags must be non-empty strings")


@dataclass(frozen=True)
class PreviewEditLoopResult:
    """Outcome of a completed ``change -> rerender -> retest -> new preview`` cycle
    (#136)."""

    item: ContentItem
    previous_video_version: int
    new_video_version: int
    qc_report: QCReport
    preview: PreviewSnapshot
    comparison: VersionComparisonResult
    invalidated_approval_ids: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "content_item_id": self.item.id,
            "item_status": self.item.status.value,
            "previous_video_version": self.previous_video_version,
            "new_video_version": self.new_video_version,
            "qc_report": self.qc_report.as_dict(),
            "preview": self.preview.as_dict(),
            "comparison": self.comparison.as_dict(),
            "invalidated_approval_ids": list(self.invalidated_approval_ids),
        }


class PreviewEditLoopService:
    """Coordinates ``change -> rerender -> retest -> new preview`` (#136)."""

    def __init__(
        self,
        database: Database,
        storage: ArtifactStorage,
        audit: AuditLog,
        settings: Settings,
        *,
        qc_builder: QCReportBuilder | None = None,
        preview_service: PreviewArtifactService | None = None,
        comparison_service: VersionComparisonService | None = None,
        policy_checker: PolicyChecker | None = None,
        clock: Clock | None = None,
    ) -> None:
        self._database = database
        self._storage = storage
        self._audit = audit
        self._settings = settings
        self._clock = clock or (lambda: datetime.now(UTC))
        self._qc_builder = qc_builder or QCReportBuilder(
            database, storage, audit, settings, clock=self._clock
        )
        self._preview_service = preview_service or PreviewArtifactService(
            database, storage
        )
        self._comparison_service = comparison_service or VersionComparisonService(
            database, storage
        )
        self._policy_checker = policy_checker or PolicyChecker()

    def run_edit_loop(
        self,
        content_item_id: str,
        changes: PreviewEditChanges,
        *,
        actor: Actor,
    ) -> PreviewEditLoopResult:
        """Execute ``change -> rerender -> retest -> new preview`` for
        ``content_item_id``."""
        if not isinstance(actor, Actor):
            raise TypeError("actor must be an Actor")
        if actor.kind is ActorKind.AI:
            raise PreviewEditError(
                "AI actor cannot trigger the preview edit loop",
                code=PreviewEditErrorCode.NOT_ALLOWED,
            )

        invalidations: list[InvalidationResult] = []
        written_uris: list[str] = []

        try:
            with self._database.transaction() as connection:
                items_repo = ContentItemRepository(connection)
                artifacts_repo = ArtifactRepository(connection)
                item = items_repo.get(content_item_id)
                if item is None:
                    raise PreviewEditError(
                        f"content item {content_item_id} not found",
                        code=PreviewEditErrorCode.ITEM_NOT_FOUND,
                    )
                if item.status not in EDITABLE_ITEM_STATUSES:
                    raise PreviewEditError(
                        f"item {item.id} is in {item.status.value} and cannot enter "
                        f"edit loop",
                        code=PreviewEditErrorCode.INVALID_STATUS,
                    )

                latest_video = artifacts_repo.latest(item.id, ArtifactKind.VIDEO)
                if latest_video is None:
                    raise PreviewEditError(
                        f"item {item.id} has no existing video artifact to edit",
                        code=PreviewEditErrorCode.ARTIFACT_MISSING,
                    )
                previous_video_version = latest_video.version

                # 1. Move item to GENERATING (if not already GENERATING) and apply title
                # change
                current_item = item
                if current_item.status is not ContentStatus.GENERATING:
                    moved = current_item.with_status(
                        ContentStatus.GENERATING, clock=self._clock
                    )
                    items_repo.update(
                        moved, expected_updated_at=current_item.updated_at
                    )
                    current_item = moved

                if (
                    changes.title is not None
                    and changes.title.strip() != current_item.title
                ):
                    renamed = current_item.rename(
                        changes.title.strip(), clock=self._clock
                    )
                    items_repo.update(
                        renamed, expected_updated_at=current_item.updated_at
                    )
                    current_item = renamed

                # 2a. Update SUBTITLES if requested
                if changes.subtitle_bytes is not None:
                    latest_sub = artifacts_repo.latest(item.id, ArtifactKind.SUBTITLES)
                    sub_media = changes.subtitle_media_type or (
                        latest_sub.media_type if latest_sub else "application/x-subrip"
                    )
                    sub_sha = hashlib.sha256(changes.subtitle_bytes).hexdigest()
                    if latest_sub is None or latest_sub.sha256 != sub_sha:
                        next_sub_ver = latest_sub.version + 1 if latest_sub else 1
                        stored_sub = self._storage.write(
                            item.id,
                            ArtifactKind.SUBTITLES,
                            next_sub_ver,
                            changes.subtitle_bytes,
                            media_type=sub_media,
                        )
                        written_uris.append(stored_sub.uri)
                        sub_art = (
                            latest_sub.next_version(
                                uri=stored_sub.uri,
                                sha256=stored_sub.sha256,
                                size_bytes=stored_sub.size_bytes,
                                media_type=sub_media,
                                clock=self._clock,
                            )
                            if latest_sub is not None
                            else Artifact.create(
                                item.id,
                                ArtifactKind.SUBTITLES,
                                uri=stored_sub.uri,
                                sha256=stored_sub.sha256,
                                size_bytes=stored_sub.size_bytes,
                                media_type=sub_media,
                                clock=self._clock,
                            )
                        )
                        artifacts_repo.add(sub_art)
                        invalidations.append(
                            invalidate_stale_approvals(
                                connection, item.id, clock=self._clock
                            )
                        )
                        current_item = items_repo.get(item.id) or current_item

                # 2b. Regenerate THUMBNAIL if requested
                if changes.regenerate_thumbnail:
                    latest_thumb = artifacts_repo.latest(
                        item.id, ArtifactKind.THUMBNAIL
                    )
                    next_thumb_ver = latest_thumb.version + 1 if latest_thumb else 1
                    w, h = (
                        (1080, 1920)
                        if item.content_type is ContentType.SHORTS
                        else (1920, 1080)
                    )
                    thumb_meta = artifacts_repo.latest(item.id, ArtifactKind.METADATA)
                    if thumb_meta is None:
                        raise PreviewEditError(
                            f"item {item.id} has no metadata artifact for the "
                            f"thumbnail",
                            code=PreviewEditErrorCode.ARTIFACT_MISSING,
                        )
                    raw_thumb_meta = self._storage.read(thumb_meta.uri)
                    if item.content_type is ContentType.SHORTS:
                        parsed_thumb_meta = ShortsMetadata.from_json_bytes(
                            raw_thumb_meta
                        )
                        thumbnail_png = generate_thumbnail_png
                    else:
                        parsed_thumb_meta = LongFormMetadata.from_bytes(raw_thumb_meta)
                        thumbnail_png = generate_longform_thumbnail_png
                    png_bytes = thumbnail_png(
                        width=w,
                        height=h,
                        project_id=parsed_thumb_meta.project_id,
                        script_id=parsed_thumb_meta.script_id,
                        title_text=changes.title.strip()
                        if changes.title is not None
                        else current_item.title,
                    )
                    stored_thumb = self._storage.write(
                        item.id,
                        ArtifactKind.THUMBNAIL,
                        next_thumb_ver,
                        png_bytes,
                        media_type="image/png",
                    )
                    written_uris.append(stored_thumb.uri)
                    thumb_art = (
                        latest_thumb.next_version(
                            uri=stored_thumb.uri,
                            sha256=stored_thumb.sha256,
                            size_bytes=stored_thumb.size_bytes,
                            media_type="image/png",
                            clock=self._clock,
                        )
                        if latest_thumb is not None
                        else Artifact.create(
                            item.id,
                            ArtifactKind.THUMBNAIL,
                            uri=stored_thumb.uri,
                            sha256=stored_thumb.sha256,
                            size_bytes=stored_thumb.size_bytes,
                            media_type="image/png",
                            clock=self._clock,
                        )
                    )
                    artifacts_repo.add(thumb_art)
                    invalidations.append(
                        invalidate_stale_approvals(
                            connection, item.id, clock=self._clock
                        )
                    )
                    current_item = items_repo.get(item.id) or current_item

                # 2c. Update METADATA if title, description, or tags changed
                latest_meta = artifacts_repo.latest(item.id, ArtifactKind.METADATA)
                if latest_meta is not None and (
                    changes.title is not None
                    or changes.description is not None
                    or changes.tags is not None
                ):
                    raw_meta = self._storage.read(latest_meta.uri)
                    strategy = StrategyProfileRepository(connection).get_by_channel(
                        item.channel_id
                    )
                    banned_phrases = (
                        strategy.brand.banned_phrases
                        if strategy is not None and strategy.brand is not None
                        else ()
                    )
                    if item.content_type is ContentType.SHORTS:
                        s_meta = ShortsMetadata.from_json_bytes(raw_meta)
                        new_title = (
                            changes.title.strip()
                            if changes.title is not None
                            else s_meta.title
                        )
                        new_desc = (
                            changes.description.strip()
                            if changes.description is not None
                            else s_meta.description
                        )
                        if (
                            "#shorts" not in new_title.lower()
                            and "#shorts" not in new_desc.lower()
                        ):
                            new_desc = f"{new_desc}\n\n#Shorts"
                        new_tags = (
                            changes.tags if changes.tags is not None else s_meta.tags
                        )
                        policy_eval = self._policy_checker.check(
                            PolicyInput(
                                content_item_id=item.id,
                                channel_id=item.channel_id,
                                title=new_title,
                                description=new_desc,
                                tags=new_tags,
                                banned_phrases=banned_phrases,
                            ),
                            rule_set=RuleSet(
                                "policy", self._settings.policy_rule_set_version
                            ),
                        )
                        if policy_eval.status is PolicyStatus.BLOCK:
                            raise PreviewEditError(
                                "edited Shorts metadata blocked by policy check",
                                code=PreviewEditErrorCode.POLICY_BLOCKED,
                            )
                        updated_s_meta = replace(
                            s_meta,
                            title=new_title,
                            description=new_desc,
                            tags=new_tags,
                            policy_status=policy_eval.status,
                        )
                        new_meta_bytes = updated_s_meta.to_json_bytes()
                    else:
                        l_meta = LongFormMetadata.from_bytes(raw_meta)
                        new_title = (
                            changes.title.strip()
                            if changes.title is not None
                            else l_meta.title
                        )
                        new_desc = (
                            changes.description.strip()
                            if changes.description is not None
                            else l_meta.description
                        )
                        new_tags = (
                            changes.tags if changes.tags is not None else l_meta.tags
                        )
                        policy_eval = self._policy_checker.check(
                            PolicyInput(
                                content_item_id=item.id,
                                channel_id=item.channel_id,
                                title=new_title,
                                description=new_desc,
                                tags=new_tags,
                                banned_phrases=banned_phrases,
                            ),
                            rule_set=RuleSet(
                                "policy", self._settings.policy_rule_set_version
                            ),
                        )
                        if policy_eval.status is PolicyStatus.BLOCK:
                            raise PreviewEditError(
                                "edited LongForm metadata blocked by policy check",
                                code=PreviewEditErrorCode.POLICY_BLOCKED,
                            )
                        updated_l_meta = replace(
                            l_meta,
                            title=new_title,
                            description=new_desc,
                            tags=new_tags,
                            policy_status=policy_eval.status,
                        )
                        new_meta_bytes = updated_l_meta.to_bytes()

                    new_meta_sha = hashlib.sha256(new_meta_bytes).hexdigest()
                    if new_meta_sha != latest_meta.sha256:
                        next_meta_ver = latest_meta.version + 1
                        stored_meta = self._storage.write(
                            item.id,
                            ArtifactKind.METADATA,
                            next_meta_ver,
                            new_meta_bytes,
                            media_type="application/json",
                        )
                        written_uris.append(stored_meta.uri)
                        meta_art = latest_meta.next_version(
                            uri=stored_meta.uri,
                            sha256=stored_meta.sha256,
                            size_bytes=stored_meta.size_bytes,
                            media_type="application/json",
                            clock=self._clock,
                        )
                        artifacts_repo.add(meta_art)
                        invalidations.append(
                            invalidate_stale_approvals(
                                connection, item.id, clock=self._clock
                            )
                        )
                        current_item = items_repo.get(item.id) or current_item

                # 2d. Rerender VIDEO artifact
                new_video_art = latest_video
                if changes.rerender_video or changes.rendered_video_bytes is not None:
                    old_video_bytes = self._storage.read(latest_video.uri)
                    old_info = probe_mp4(old_video_bytes)
                    next_vid_ver = latest_video.version + 1
                    if changes.rendered_video_bytes is not None:
                        new_video_bytes = changes.rendered_video_bytes
                    else:
                        payload_seed = (
                            f"rerender:{item.id}:v{next_vid_ver}:{current_item.title}".encode()
                            * 16
                        )
                        new_video_bytes = stub_mp4(old_info.duration_ms, payload_seed)

                    new_vid_sha = hashlib.sha256(new_video_bytes).hexdigest()
                    if new_vid_sha == latest_video.sha256:
                        extra_tag = f":v{next_vid_ver}".encode()
                        new_video_bytes = stub_mp4(
                            old_info.duration_ms, new_video_bytes[-64:] + extra_tag
                        )
                        new_vid_sha = hashlib.sha256(new_video_bytes).hexdigest()

                    stored_vid = self._storage.write(
                        item.id,
                        ArtifactKind.VIDEO,
                        next_vid_ver,
                        new_video_bytes,
                        media_type="video/mp4",
                    )
                    written_uris.append(stored_vid.uri)
                    new_video_art = latest_video.next_version(
                        uri=stored_vid.uri,
                        sha256=stored_vid.sha256,
                        size_bytes=stored_vid.size_bytes,
                        media_type="video/mp4",
                        clock=self._clock,
                    )
                    artifacts_repo.add(new_video_art)

                    # Record corresponding succeeded video.render AIJob checkpoint
                    jobs_repo = AIJobRepository(connection)
                    existing_jobs = jobs_repo.list_by_content_item(item.id)
                    timeline_sha: str | None = None
                    for j in reversed(existing_jobs):
                        if j.kind == "video.render":
                            for cp in reversed(j.checkpoints):
                                if cp.step == "video.rendered":
                                    t_sha = cp.data.get("timeline_sha256")
                                    if isinstance(t_sha, str):
                                        timeline_sha = t_sha
                                    break
                            if timeline_sha is not None:
                                break

                    job_key = render_key(item.id, new_vid_sha, next_vid_ver)
                    render_job = AIJob.create(
                        "video.render",
                        job_key,
                        content_item_id=item.id,
                        clock=self._clock,
                    )
                    started_job = render_job.start(clock=self._clock)
                    cp_data: dict[str, Any] = {
                        "artifact_id": new_video_art.id,
                        "version": next_vid_ver,
                        "video_sha256": new_vid_sha,
                    }
                    if timeline_sha is not None:
                        cp_data["timeline_sha256"] = timeline_sha
                    checkpointed_job = started_job.checkpoint(
                        "video.rendered", cp_data, clock=self._clock
                    )
                    succeeded_job = checkpointed_job.succeed(clock=self._clock)
                    jobs_repo.add(succeeded_job)

                    invalidations.append(
                        invalidate_stale_approvals(
                            connection, item.id, clock=self._clock
                        )
                    )
                    current_item = items_repo.get(item.id) or current_item

                # Move item from GENERATING -> TESTING
                testing_item = current_item.with_status(
                    ContentStatus.TESTING, clock=self._clock
                )
                items_repo.update(
                    testing_item, expected_updated_at=current_item.updated_at
                )
                current_item = testing_item
        except BaseException:
            for uri in written_uris:
                with contextlib.suppress(Exception):
                    self._storage.delete(uri)
            raise

        invalidated_ids: list[str] = []
        for inv in invalidations:
            record_invalidations(self._audit, inv)
            for entry in inv.invalidated:
                if entry.approval.id not in invalidated_ids:
                    invalidated_ids.append(entry.approval.id)

        # 3. Retest: evaluate full Phase K QC report on the new artifacts
        qc_report = self._qc_builder.generate(content_item_id, actor=actor)

        # 4. Move TESTING -> PREVIEW_READY when QC passes or warns
        final_item = current_item
        if qc_report.status is not QCStatus.FAIL:
            with self._database.transaction() as connection:
                items_repo = ContentItemRepository(connection)
                stored_item = items_repo.get(content_item_id)
                assert stored_item is not None
                if stored_item.status is ContentStatus.TESTING:
                    ready_item = stored_item.with_status(
                        ContentStatus.PREVIEW_READY, clock=self._clock
                    )
                    items_repo.update(
                        ready_item, expected_updated_at=stored_item.updated_at
                    )
                    final_item = ready_item
                else:
                    final_item = stored_item

        preview_snapshot = self._preview_service.get_snapshot(content_item_id)
        comparison = self._comparison_service.compare(
            content_item_id,
            base_version=previous_video_version,
            target_version=new_video_art.version,
        )

        self._audit.record(
            PREVIEW_EDIT_LOOP_COMPLETED,
            actor,
            EntityRef("content_item", final_item.id),
            AuditResult.SUCCESS,
            {
                "previous_video_version": previous_video_version,
                "new_video_version": new_video_art.version,
                "qc_result_id": qc_report.qc_result.id,
                "qc_status": qc_report.status.value,
                "item_status": final_item.status.value,
                "invalidated_approvals_count": len(invalidated_ids),
            },
        )

        return PreviewEditLoopResult(
            item=final_item,
            previous_video_version=previous_video_version,
            new_video_version=new_video_art.version,
            qc_report=qc_report,
            preview=preview_snapshot,
            comparison=comparison,
            invalidated_approval_ids=tuple(invalidated_ids),
        )
