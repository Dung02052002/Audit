"""Version Comparison service (Prompt Pack v8, prompt L-135), context C11 Review.

Compares current and previous artifact versions for a ``ContentItem`` so the
human reviewer can inspect what changed after a re-render or metadata edit:
- Compares ``VIDEO``, ``AUDIO``, ``SUBTITLES``, ``THUMBNAIL``, and ``METADATA``
  artifacts between the target (default latest) version and the preceding version.
- Computes kind-specific diffs:
  - ``VIDEO``: container duration delta via ``probe_mp4``.
  - ``AUDIO``: audio duration delta from ``AudioMetadataRepository``.
  - ``SUBTITLES``: cue count delta and modified cue indices.
  - ``THUMBNAIL``: image dimensions and media type changes via ``probe_image``.
  - ``METADATA``: title/description/tags/chapters/project_version/script_version/
    disclosure changes.
- Reports ``invalidated_approval_ids`` and ``stale_approval_ids`` caused by
  artifact version changes.
"""

import json
from dataclasses import dataclass
from typing import Any

from ai_youtube_agent.content.approval import ApprovalStatus, stale_kinds
from ai_youtube_agent.content.preview_artifact import (
    PreviewArtifactDescriptor,
    PreviewError,
    PreviewErrorCode,
    _validate_positive_version,
)
from ai_youtube_agent.content.qc_video_checks import _parse_subtitle_cues
from ai_youtube_agent.content.shorts_thumbnail import probe_image
from ai_youtube_agent.core.artifact import Artifact, ArtifactKind
from ai_youtube_agent.core.artifact_storage import ArtifactStorage
from ai_youtube_agent.core.content_item import ContentStatus, ContentType
from ai_youtube_agent.core.db.database import Database
from ai_youtube_agent.core.db.repositories.content import (
    ArtifactRepository,
    AudioMetadataRepository,
    ContentItemRepository,
)
from ai_youtube_agent.core.db.repositories.review import ApprovalRequestRepository
from ai_youtube_agent.providers.video_render import probe_mp4


@dataclass(frozen=True)
class ArtifactKindComparison:
    """Comparison between previous and current versions of one ``ArtifactKind``
    (#135).
    """

    kind: ArtifactKind
    change_status: str  # "modified" | "added" | "unchanged" | "missing"
    previous: PreviewArtifactDescriptor | None
    current: PreviewArtifactDescriptor | None
    version_delta: int
    size_delta_bytes: int
    sha256_changed: bool
    details: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "change_status": self.change_status,
            "previous": self.previous.as_dict() if self.previous is not None else None,
            "current": self.current.as_dict() if self.current is not None else None,
            "version_delta": self.version_delta,
            "size_delta_bytes": self.size_delta_bytes,
            "sha256_changed": self.sha256_changed,
            "details": dict(self.details),
        }


@dataclass(frozen=True)
class VersionComparisonResult:
    """Full comparison across all artifact kinds for a ``ContentItem`` (#135)."""

    content_item_id: str
    channel_id: str
    content_type: ContentType
    status: ContentStatus
    has_previous_version: bool
    changed_kinds: tuple[ArtifactKind, ...]
    comparisons: tuple[ArtifactKindComparison, ...]
    invalidated_approval_ids: tuple[str, ...]
    stale_approval_ids: tuple[str, ...]

    def by_kind(self, kind: ArtifactKind) -> ArtifactKindComparison:
        for comp in self.comparisons:
            if comp.kind is kind:
                return comp
        raise KeyError(kind)

    def as_dict(self) -> dict[str, Any]:
        return {
            "content_item_id": self.content_item_id,
            "channel_id": self.channel_id,
            "content_type": self.content_type.value,
            "status": self.status.value,
            "has_previous_version": self.has_previous_version,
            "changed_kinds": [k.value for k in self.changed_kinds],
            "comparisons": [c.as_dict() for c in self.comparisons],
            "invalidated_approval_ids": list(self.invalidated_approval_ids),
            "stale_approval_ids": list(self.stale_approval_ids),
        }


class VersionComparisonService:
    """Compares current and previous artifact versions for a content item (#135)."""

    def __init__(self, database: Database, storage: ArtifactStorage) -> None:
        self._database = database
        self._storage = storage

    def compare(
        self,
        content_item_id: str,
        *,
        base_version: int | None = None,
        target_version: int | None = None,
    ) -> VersionComparisonResult:
        """Compare ``target_version`` (default latest) against ``base_version``
        (default preceding).
        """
        if base_version is not None:
            _validate_positive_version(base_version, "base_version")
        if target_version is not None:
            _validate_positive_version(target_version, "target_version")
        if (
            base_version is not None
            and target_version is not None
            and base_version >= target_version
        ):
            raise PreviewError(
                f"base_version ({base_version}) must be strictly less than "
                f"target_version ({target_version})",
                code=PreviewErrorCode.VERSION_INVALID,
            )

        with self._database.transaction() as connection:
            item = ContentItemRepository(connection).get(content_item_id)
            if item is None:
                raise PreviewError(
                    f"content item {content_item_id} not found",
                    code=PreviewErrorCode.ITEM_NOT_FOUND,
                )
            all_artifacts = ArtifactRepository(connection).list_by_content_item(
                content_item_id
            )
            audio_meta_repo = AudioMetadataRepository(connection)
            audio_durations: dict[str, int] = {}
            for art in all_artifacts:
                if art.kind is ArtifactKind.AUDIO:
                    am = audio_meta_repo.get_by_artifact(art.id)
                    if am is not None:
                        audio_durations[art.id] = am.duration_ms
            approvals = ApprovalRequestRepository(connection).list_by_content_item(
                content_item_id
            )

        by_kind: dict[ArtifactKind, list[Artifact]] = {}
        for art in all_artifacts:
            by_kind.setdefault(art.kind, []).append(art)
        for kind_list in by_kind.values():
            kind_list.sort(key=lambda a: a.version)

        if not by_kind.get(ArtifactKind.VIDEO):
            raise PreviewError(
                f"no rendered video artifact exists for item {content_item_id}",
                code=PreviewErrorCode.ARTIFACT_NOT_FOUND,
            )

        comparisons: list[ArtifactKindComparison] = []
        changed_kinds: list[ArtifactKind] = []
        has_previous = False

        for kind in (
            ArtifactKind.VIDEO,
            ArtifactKind.AUDIO,
            ArtifactKind.SUBTITLES,
            ArtifactKind.THUMBNAIL,
            ArtifactKind.METADATA,
        ):
            versions = by_kind.get(kind, [])
            if not versions:
                comparisons.append(
                    ArtifactKindComparison(
                        kind=kind,
                        change_status="missing",
                        previous=None,
                        current=None,
                        version_delta=0,
                        size_delta_bytes=0,
                        sha256_changed=False,
                        details={},
                    )
                )
                continue

            latest_ver = versions[-1].version
            curr_art = self._select_version(
                versions,
                target_version,
                kind=kind,
                require_exact=(kind is ArtifactKind.VIDEO),
            )
            prev_art = self._select_previous(
                versions,
                curr_art,
                base_version,
                kind=kind,
                require_exact=(kind is ArtifactKind.VIDEO),
            )

            curr_desc = PreviewArtifactDescriptor.from_artifact(
                curr_art, is_latest=(curr_art.version == latest_ver)
            )
            if prev_art is None:
                comparisons.append(
                    ArtifactKindComparison(
                        kind=kind,
                        change_status="added",
                        previous=None,
                        current=curr_desc,
                        version_delta=curr_art.version,
                        size_delta_bytes=curr_art.size_bytes,
                        sha256_changed=True,
                        details=self._single_version_details(
                            kind, curr_art, audio_durations
                        ),
                    )
                )
                continue

            has_previous = True
            prev_desc = PreviewArtifactDescriptor.from_artifact(
                prev_art, is_latest=(prev_art.version == latest_ver)
            )
            sha_changed = prev_art.sha256 != curr_art.sha256
            status_str = "modified" if sha_changed else "unchanged"
            if sha_changed:
                changed_kinds.append(kind)

            details = self._diff_details(kind, prev_art, curr_art, audio_durations)
            comparisons.append(
                ArtifactKindComparison(
                    kind=kind,
                    change_status=status_str,
                    previous=prev_desc,
                    current=curr_desc,
                    version_delta=curr_art.version - prev_art.version,
                    size_delta_bytes=curr_art.size_bytes - prev_art.size_bytes,
                    sha256_changed=sha_changed,
                    details=details,
                )
            )

        invalidated_ids = tuple(
            req.id for req in approvals if req.status is ApprovalStatus.INVALIDATED
        )
        stale_ids = tuple(
            req.id for req in approvals if bool(stale_kinds(req, all_artifacts))
        )

        return VersionComparisonResult(
            content_item_id=item.id,
            channel_id=item.channel_id,
            content_type=item.content_type,
            status=item.status,
            has_previous_version=has_previous,
            changed_kinds=tuple(changed_kinds),
            comparisons=tuple(comparisons),
            invalidated_approval_ids=invalidated_ids,
            stale_approval_ids=stale_ids,
        )

    def _select_version(
        self,
        versions: list[Artifact],
        target_version: int | None,
        *,
        kind: ArtifactKind,
        require_exact: bool,
    ) -> Artifact:
        if target_version is None:
            return versions[-1]
        exact = [v for v in versions if v.version == target_version]
        if exact:
            return exact[0]
        if require_exact:
            raise PreviewError(
                f"{kind.value} version {target_version} does not exist",
                code=PreviewErrorCode.ARTIFACT_NOT_FOUND,
            )
        le_versions = [v for v in versions if v.version <= target_version]
        return le_versions[-1] if le_versions else versions[-1]

    def _select_previous(
        self,
        versions: list[Artifact],
        current: Artifact,
        base_version: int | None,
        *,
        kind: ArtifactKind,
        require_exact: bool,
    ) -> Artifact | None:
        if base_version is not None:
            exact = [v for v in versions if v.version == base_version]
            if exact:
                return exact[0]
            if require_exact:
                raise PreviewError(
                    f"{kind.value} base version {base_version} does not exist",
                    code=PreviewErrorCode.ARTIFACT_NOT_FOUND,
                )
            le_versions = [v for v in versions if v.version <= base_version]
            return le_versions[-1] if le_versions else None

        older = [v for v in versions if v.version < current.version]
        return older[-1] if older else None

    def _single_version_details(
        self,
        kind: ArtifactKind,
        current: Artifact,
        audio_durations: dict[str, int],
    ) -> dict[str, Any]:
        if kind is ArtifactKind.VIDEO:
            try:
                info = probe_mp4(self._storage.read(current.uri))
                return {"current_duration_ms": info.duration_ms}
            except Exception:
                return {}
        if kind is ArtifactKind.AUDIO:
            dur = audio_durations.get(current.id)
            return {"current_duration_ms": dur} if dur is not None else {}
        return {}

    def _diff_details(
        self,
        kind: ArtifactKind,
        previous: Artifact,
        current: Artifact,
        audio_durations: dict[str, int],
    ) -> dict[str, Any]:
        if kind is ArtifactKind.VIDEO:
            try:
                prev_info = probe_mp4(self._storage.read(previous.uri))
                curr_info = probe_mp4(self._storage.read(current.uri))
                return {
                    "previous_duration_ms": prev_info.duration_ms,
                    "current_duration_ms": curr_info.duration_ms,
                    "duration_delta_ms": curr_info.duration_ms - prev_info.duration_ms,
                }
            except Exception:
                return {}

        if kind is ArtifactKind.AUDIO:
            prev_dur = audio_durations.get(previous.id)
            curr_dur = audio_durations.get(current.id)
            if prev_dur is not None and curr_dur is not None:
                return {
                    "previous_duration_ms": prev_dur,
                    "current_duration_ms": curr_dur,
                    "duration_delta_ms": curr_dur - prev_dur,
                }
            return {}

        if kind is ArtifactKind.SUBTITLES:
            try:
                prev_text = self._storage.read(previous.uri).decode("utf-8")
                curr_text = self._storage.read(current.uri).decode("utf-8")
                _, prev_cues = _parse_subtitle_cues(prev_text, previous.media_type)
                _, curr_cues = _parse_subtitle_cues(curr_text, current.media_type)
                prev_map = {c.index: (c.start_ms, c.end_ms, c.text) for c in prev_cues}
                curr_map = {c.index: (c.start_ms, c.end_ms, c.text) for c in curr_cues}
                all_indices = sorted(set(prev_map) | set(curr_map))
                modified_indices = [
                    idx for idx in all_indices if prev_map.get(idx) != curr_map.get(idx)
                ]
                return {
                    "previous_cue_count": len(prev_cues),
                    "current_cue_count": len(curr_cues),
                    "cue_count_delta": len(curr_cues) - len(prev_cues),
                    "modified_cue_indices": modified_indices,
                }
            except Exception:
                return {}

        if kind is ArtifactKind.THUMBNAIL:
            try:
                prev_img = probe_image(self._storage.read(previous.uri))
                curr_img = probe_image(self._storage.read(current.uri))
                return {
                    "previous_dimensions": [prev_img.width, prev_img.height],
                    "current_dimensions": [curr_img.width, curr_img.height],
                    "dimensions_changed": (
                        (prev_img.width, prev_img.height)
                        != (curr_img.width, curr_img.height)
                    ),
                    "media_type_changed": previous.media_type != current.media_type,
                }
            except Exception:
                return {
                    "media_type_changed": previous.media_type != current.media_type,
                }

        if kind is ArtifactKind.METADATA:
            try:
                prev_json = json.loads(self._storage.read(previous.uri).decode("utf-8"))
                curr_json = json.loads(self._storage.read(current.uri).decode("utf-8"))
                prev_tags = list(prev_json.get("tags", []))
                curr_tags = list(curr_json.get("tags", []))
                tags_added = [t for t in curr_tags if t not in prev_tags]
                tags_removed = [t for t in prev_tags if t not in curr_tags]
                return {
                    "title_changed": prev_json.get("title") != curr_json.get("title"),
                    "description_changed": (
                        prev_json.get("description") != curr_json.get("description")
                    ),
                    "tags_added": tags_added,
                    "tags_removed": tags_removed,
                    "chapters_changed": (
                        prev_json.get("chapters") != curr_json.get("chapters")
                    ),
                    "project_version_delta": int(curr_json.get("project_version", 0))
                    - int(prev_json.get("project_version", 0)),
                    "script_version_delta": int(curr_json.get("script_version", 0))
                    - int(prev_json.get("script_version", 0)),
                    "contains_synthetic_media_changed": (
                        prev_json.get("contains_synthetic_media")
                        != curr_json.get("contains_synthetic_media")
                    ),
                }
            except Exception:
                return {}

        return {}
