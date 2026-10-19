"""Publish Metadata Mapper (Prompt Pack v8, prompt #148), context C12 Publishing.

Maps internal ``ShortsMetadata`` (#102), ``LongFormMetadata`` (#113) or stored
``ArtifactKind.METADATA`` JSON artifacts, together with the approved
``ApprovalRequest``, ``Channel`` and bound ``Artifact`` records, into a
validated ``YouTubeUploadPayload`` (#147).
"""

import json
from datetime import datetime
from enum import StrEnum
from http import HTTPStatus
from typing import Any, Final

from ai_youtube_agent.content.approval import ApprovalRequest, ApprovalStatus
from ai_youtube_agent.content.channel import Channel
from ai_youtube_agent.content.disclosure import DisclosureDecision, DisclosureRecord
from ai_youtube_agent.content.longform_metadata import LongFormMetadata
from ai_youtube_agent.content.shorts_metadata import ShortsMetadata, has_shorts_marker
from ai_youtube_agent.core.artifact import Artifact, ArtifactKind
from ai_youtube_agent.core.content_item import ContentType
from ai_youtube_agent.core.errors import DomainError
from ai_youtube_agent.pipeline.idempotency import publish_key
from ai_youtube_agent.pipeline.publish import PublishNotApprovedError
from ai_youtube_agent.providers.youtube_upload import (
    YouTubePrivacyStatus,
    YouTubeUploadPayload,
    YouTubeVideoSnippet,
    YouTubeVideoStatus,
)

DEFAULT_YOUTUBE_CATEGORY_ID: Final[str] = "22"


class PublishMetadataErrorCode(StrEnum):
    MAPPING = "domain.publish_metadata_mapping"
    BINDING_MISMATCH = "domain.publish_metadata_binding_mismatch"
    DISCLOSURE_MISMATCH = "domain.publish_metadata_disclosure_mismatch"
    SHORTS_MARKER_MISSING = "domain.publish_metadata_shorts_marker_missing"
    CHAPTERS_MISSING = "domain.publish_metadata_chapters_missing"
    INVALID_JSON = "domain.publish_metadata_invalid_json"


USER_MESSAGES: Final[dict[PublishMetadataErrorCode, str]] = {
    PublishMetadataErrorCode.MAPPING: (
        "The internal publish metadata could not be mapped to a YouTube payload."
    ),
    PublishMetadataErrorCode.BINDING_MISMATCH: (
        "The artifacts to publish do not match the approved artifact bindings."
    ),
    PublishMetadataErrorCode.DISCLOSURE_MISMATCH: (
        "The publish metadata does not satisfy the required AI disclosure decision."
    ),
    PublishMetadataErrorCode.SHORTS_MARKER_MISSING: (
        "Shorts publish metadata must include #Shorts in the title, description or tags."
    ),
    PublishMetadataErrorCode.CHAPTERS_MISSING: (
        "LongForm publish metadata with chapters must include 00:00 chapter timestamps in the description."
    ),
    PublishMetadataErrorCode.INVALID_JSON: (
        "The stored metadata artifact is not valid publish metadata JSON."
    ),
}


class PublishMetadataMappingError(DomainError):
    default_code = PublishMetadataErrorCode.MAPPING.value
    default_user_message = USER_MESSAGES[PublishMetadataErrorCode.MAPPING]
    default_http_status = HTTPStatus.UNPROCESSABLE_ENTITY

    def __init__(
        self,
        code: PublishMetadataErrorCode,
        detail: str,
        *,
        http_status: HTTPStatus = HTTPStatus.UNPROCESSABLE_ENTITY,
    ) -> None:
        if not isinstance(code, PublishMetadataErrorCode):
            raise TypeError("code must be a PublishMetadataErrorCode")
        self.mapping_code = code
        super().__init__(
            detail,
            code=code.value,
            user_message=USER_MESSAGES[code],
        )
        self.default_http_status = http_status


def _verify_binding(
    approval: ApprovalRequest, artifact: Artifact, kind: ArtifactKind
) -> None:
    if artifact.kind is not kind:
        raise PublishMetadataMappingError(
            PublishMetadataErrorCode.BINDING_MISMATCH,
            f"expected {kind.value} artifact, got {artifact.kind.value}",
        )
    if artifact.content_item_id != approval.content_item_id:
        raise PublishMetadataMappingError(
            PublishMetadataErrorCode.BINDING_MISMATCH,
            f"artifact {artifact.id} belongs to another content item",
        )
    binding = approval.binding_for(kind)
    if binding is None:
        raise PublishMetadataMappingError(
            PublishMetadataErrorCode.BINDING_MISMATCH,
            f"approval {approval.id} does not bind any {kind.value} artifact",
            http_status=HTTPStatus.CONFLICT,
        )
    if (
        binding.artifact_id != artifact.id
        or binding.version != artifact.version
        or binding.sha256 != artifact.sha256
    ):
        raise PublishMetadataMappingError(
            PublishMetadataErrorCode.BINDING_MISMATCH,
            f"{kind.value} artifact {artifact.id} (v{artifact.version}) does not match "
            f"approved binding {binding.artifact_id} (v{binding.version})",
            http_status=HTTPStatus.CONFLICT,
        )


def _extract_metadata_fields(
    content_type: ContentType,
    metadata_source: ShortsMetadata | LongFormMetadata | bytes | dict[str, Any],
) -> dict[str, Any]:
    if isinstance(metadata_source, ShortsMetadata):
        return {
            "content_item_id": metadata_source.content_item_id,
            "channel_id": metadata_source.channel_id,
            "title": metadata_source.title,
            "description": metadata_source.description,
            "tags": metadata_source.tags,
            "language": metadata_source.language,
            "made_for_kids": metadata_source.made_for_kids,
            "contains_synthetic_media": metadata_source.contains_synthetic_media,
            "disclosure_decision_id": metadata_source.disclosure_decision_id,
            "chapters": (),
        }
    if isinstance(metadata_source, LongFormMetadata):
        return {
            "content_item_id": metadata_source.content_item_id,
            "channel_id": metadata_source.channel_id,
            "title": metadata_source.title,
            "description": metadata_source.description,
            "tags": metadata_source.tags,
            "language": metadata_source.language,
            "made_for_kids": metadata_source.made_for_kids,
            "contains_synthetic_media": metadata_source.contains_synthetic_media,
            "disclosure_decision_id": metadata_source.disclosure_decision_id,
            "chapters": metadata_source.chapters,
        }
    if isinstance(metadata_source, bytes):
        try:
            if content_type is ContentType.SHORTS:
                try:
                    sm = ShortsMetadata.from_json_bytes(metadata_source)
                    return _extract_metadata_fields(content_type, sm)
                except Exception:
                    raw_dict = json.loads(metadata_source.decode("utf-8"))
            else:
                try:
                    lm = LongFormMetadata.from_bytes(metadata_source)
                    return _extract_metadata_fields(content_type, lm)
                except Exception:
                    raw_dict = json.loads(metadata_source.decode("utf-8"))
        except Exception as exc:
            raise PublishMetadataMappingError(
                PublishMetadataErrorCode.INVALID_JSON,
                f"failed to decode metadata JSON: {exc}",
            ) from exc
    elif isinstance(metadata_source, dict):
        raw_dict = metadata_source
    else:
        raise TypeError("unsupported metadata_source type")

    if not isinstance(raw_dict, dict):
        raise PublishMetadataMappingError(
            PublishMetadataErrorCode.INVALID_JSON,
            "metadata JSON root must be an object",
        )
    tags_raw = raw_dict.get("tags", ())
    if not isinstance(tags_raw, list | tuple):
        raise PublishMetadataMappingError(
            PublishMetadataErrorCode.INVALID_JSON,
            "tags must be a list or tuple",
        )
    return {
        "content_item_id": raw_dict.get("content_item_id"),
        "channel_id": raw_dict.get("channel_id"),
        "title": raw_dict.get("title", ""),
        "description": raw_dict.get("description", ""),
        "tags": tuple(str(t) for t in tags_raw),
        "language": raw_dict.get("language", "en"),
        "made_for_kids": bool(raw_dict.get("made_for_kids", False)),
        "contains_synthetic_media": bool(
            raw_dict.get(
                "contains_synthetic_media",
                raw_dict.get("ai_disclosure_required", False),
            )
        ),
        "disclosure_decision_id": raw_dict.get("disclosure_decision_id"),
        "chapters": tuple(raw_dict.get("chapters", ())),
    }


class PublishMetadataMapper:
    """Maps internal Shorts or LongForm metadata and approved artifacts to ``YouTubeUploadPayload`` (#148)."""

    def map_to_payload(
        self,
        *,
        channel: Channel,
        content_type: ContentType,
        approval: ApprovalRequest,
        video_artifact: Artifact,
        metadata_artifact: Artifact,
        metadata_source: ShortsMetadata | LongFormMetadata | bytes | dict[str, Any],
        thumbnail_artifact: Artifact | None = None,
        subtitles_artifact: Artifact | None = None,
        disclosure_record: DisclosureRecord | None = None,
        privacy_status: YouTubePrivacyStatus = YouTubePrivacyStatus.PRIVATE,
        category_id: str = DEFAULT_YOUTUBE_CATEGORY_ID,
        publish_at: datetime | None = None,
        idempotency_key: str | None = None,
    ) -> YouTubeUploadPayload:
        if approval.status is not ApprovalStatus.APPROVED:
            raise PublishNotApprovedError(
                f"approval {approval.id} is {approval.status.value}, not approved"
            )

        _verify_binding(approval, video_artifact, ArtifactKind.VIDEO)
        _verify_binding(approval, metadata_artifact, ArtifactKind.METADATA)
        if thumbnail_artifact is not None:
            _verify_binding(approval, thumbnail_artifact, ArtifactKind.THUMBNAIL)
        if subtitles_artifact is not None:
            _verify_binding(approval, subtitles_artifact, ArtifactKind.SUBTITLES)

        fields = _extract_metadata_fields(content_type, metadata_source)
        if (
            fields["content_item_id"] is not None
            and fields["content_item_id"] != approval.content_item_id
        ):
            raise PublishMetadataMappingError(
                PublishMetadataErrorCode.BINDING_MISMATCH,
                "metadata content_item_id does not match approval.content_item_id",
            )
        if (
            fields["channel_id"] is not None
            and fields["channel_id"] != channel.id
        ):
            raise PublishMetadataMappingError(
                PublishMetadataErrorCode.BINDING_MISMATCH,
                "metadata channel_id does not match channel.id",
            )

        title: str = fields["title"]
        description: str = fields["description"]
        tags: tuple[str, ...] = fields["tags"]
        language: str = fields["language"]
        made_for_kids: bool = fields["made_for_kids"]
        contains_synthetic_media: bool = fields["contains_synthetic_media"]
        disclosure_decision_id: str | None = fields["disclosure_decision_id"]
        chapters = fields["chapters"]

        if content_type is ContentType.SHORTS:
            if not has_shorts_marker(title, description, tags):
                raise PublishMetadataMappingError(
                    PublishMetadataErrorCode.SHORTS_MARKER_MISSING,
                    "Shorts publish metadata must contain #Shorts in title/description or 'Shorts' in tags",
                )
        elif content_type is ContentType.LONGFORM:
            if chapters and "00:00" not in description:
                raise PublishMetadataMappingError(
                    PublishMetadataErrorCode.CHAPTERS_MISSING,
                    "LongForm publish metadata with chapters must embed 00:00 timestamps in description",
                )

        if disclosure_record is not None:
            if (
                disclosure_decision_id is not None
                and disclosure_decision_id != disclosure_record.id
            ):
                raise PublishMetadataMappingError(
                    PublishMetadataErrorCode.DISCLOSURE_MISMATCH,
                    f"metadata disclosure_decision_id {disclosure_decision_id} does not match "
                    f"latest disclosure decision {disclosure_record.id}",
                )
            if (
                disclosure_record.decision is DisclosureDecision.REQUIRED
                and not contains_synthetic_media
            ):
                raise PublishMetadataMappingError(
                    PublishMetadataErrorCode.DISCLOSURE_MISMATCH,
                    "AI disclosure decision is REQUIRED but contains_synthetic_media is False",
                )
            disclosure_decision_id = disclosure_record.id

        try:
            snippet = YouTubeVideoSnippet(
                title=title,
                description=description,
                tags=tags,
                category_id=category_id,
                default_language=language,
                default_audio_language=language,
            )
            status = YouTubeVideoStatus(
                privacy_status=privacy_status,
                self_declared_made_for_kids=made_for_kids,
                contains_synthetic_media=contains_synthetic_media,
                publish_at=publish_at,
            )
            key = idempotency_key or publish_key(
                approval.content_item_id, approval.id
            )
            return YouTubeUploadPayload(
                channel_id=channel.id,
                youtube_channel_id=channel.youtube.channel_id,
                content_item_id=approval.content_item_id,
                content_type=content_type,
                approval_request_id=approval.id,
                idempotency_key=key,
                video_artifact_id=video_artifact.id,
                video_version=video_artifact.version,
                video_sha256=video_artifact.sha256,
                video_size_bytes=video_artifact.size_bytes,
                video_media_type=video_artifact.media_type,
                snippet=snippet,
                status=status,
                metadata_artifact_id=metadata_artifact.id,
                metadata_version=metadata_artifact.version,
                metadata_sha256=metadata_artifact.sha256,
                thumbnail_artifact_id=(
                    thumbnail_artifact.id if thumbnail_artifact else None
                ),
                thumbnail_sha256=(
                    thumbnail_artifact.sha256 if thumbnail_artifact else None
                ),
                thumbnail_media_type=(
                    thumbnail_artifact.media_type if thumbnail_artifact else None
                ),
                subtitles_artifact_id=(
                    subtitles_artifact.id if subtitles_artifact else None
                ),
                subtitles_sha256=(
                    subtitles_artifact.sha256 if subtitles_artifact else None
                ),
                subtitles_language=language if subtitles_artifact else None,
                disclosure_decision_id=disclosure_decision_id,
            )
        except (ValueError, TypeError) as exc:
            raise PublishMetadataMappingError(
                PublishMetadataErrorCode.MAPPING,
                str(exc),
            ) from exc
