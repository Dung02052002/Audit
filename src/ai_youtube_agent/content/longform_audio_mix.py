"""LongForm audio mix (Prompt Pack v8, prompt #110), context C9 LongForm Production.

``LongFormAudioMixer.mix`` combines the voice of a ``LongFormProject`` with the
music and scene sound effects of its ``LongFormTimeline`` under
``MixConstraints`` (using the ``content/audio_mix.py`` engine).

Rules:
- While ``LONGFORM_ENABLED`` is off (``FeatureFlags.longform_enabled`` is
  ``False``), ``mix`` is refused with ``LongFormDisabledError`` before reading
  or mixing.
- ``mix`` returns a ``MixOutcome`` with the mixed WAV bytes in memory for the
  LongForm render step (#111) and writes nothing to the database or storage;
  only ``audio.mixed`` is audited after the work with scalar IDs, checksums,
  counts, constraint values and report numbers.
- In one read transaction, the project is loaded, ``expected_version`` is
  checked, ``draft`` projects are refused with ``not_ready``, stale script/audio
  bindings are refused with ``stale_input``, and every music and ``sfx`` asset
  passes ``check_longform_audio_assets`` (D5) plus provenance snapshot
  verification.
- Audio bytes are read through ``AssetSource`` (default ``ArtifactAssetSource``)
  outside the transaction, mixed via ``mix_audio``, self-checked with
  ``audio_validation.analyze``, and audited as ``audio.mixed``.
"""

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from http import HTTPStatus

from ai_youtube_agent.content.asset import Asset
from ai_youtube_agent.content.audio_mix import (
    DEFAULT_CONSTRAINTS,
    MixConstraints,
    MixInputError,
    MixRole,
    MixTrack,
    PcmAudio,
    decode_wav,
    mix_audio,
)
from ai_youtube_agent.content.audio_validation import analyze
from ai_youtube_agent.content.longform_project import (
    LongFormProject,
    LongFormProjectStatus,
)
from ai_youtube_agent.content.longform_script_generator import LongFormDisabledError
from ai_youtube_agent.content.longform_service import (
    LongFormProjectError,
    LongFormProjectErrorCode,
    check_longform_audio_assets,
    longform_project_is_current,
)
from ai_youtube_agent.content.longform_timeline import LongFormTimeline
from ai_youtube_agent.content.qc import QCStatus
from ai_youtube_agent.content.shorts_audio_mix import (
    MIXED,
    ArtifactAssetSource,
    AssetSource,
    MixOutcome,
)
from ai_youtube_agent.content.shorts_timeline import (
    AssetPlacement,
    AssetRole,
    SceneAsset,
)
from ai_youtube_agent.core.artifact_storage import ArtifactStorage
from ai_youtube_agent.core.audit import (
    Actor,
    AuditLog,
    AuditResult,
    EntityRef,
    MetadataValue,
)
from ai_youtube_agent.core.db.database import Database
from ai_youtube_agent.core.db.repositories.content import (
    ArtifactRepository,
    ContentItemRepository,
)
from ai_youtube_agent.core.db.repositories.longform import LongFormProjectRepository
from ai_youtube_agent.core.db.repositories.provenance import ProvenanceRepository
from ai_youtube_agent.core.errors import DomainError
from ai_youtube_agent.core.flags import FeatureFlags


class LongFormMixErrorCode(StrEnum):
    MIX = "domain.longform_mix"
    PROJECT_NOT_FOUND = "domain.longform_mix_project_not_found"
    CONFLICT = "domain.longform_mix_conflict"
    NOT_READY = "domain.longform_mix_not_ready"
    STALE_INPUT = "domain.longform_mix_stale_input"
    ASSET_UNAVAILABLE = "domain.longform_mix_asset_unavailable"
    ASSET_NOT_AUDIO = "domain.longform_mix_asset_not_audio"
    ASSET_RIGHTS = "domain.longform_mix_asset_rights"
    FORMAT_UNSUPPORTED = "domain.longform_mix_format_unsupported"
    MIX_INVALID = "domain.longform_mix_invalid"


USER_MESSAGES = {
    LongFormMixErrorCode.MIX: "The audio could not be mixed.",
    LongFormMixErrorCode.PROJECT_NOT_FOUND: ("This LongForm project does not exist."),
    LongFormMixErrorCode.CONFLICT: (
        "The LongForm project changed in the meantime. Reload it and try again."
    ),
    LongFormMixErrorCode.NOT_READY: (
        "Plan the scenes of this LongForm video before mixing its audio."
    ),
    LongFormMixErrorCode.STALE_INPUT: (
        "The script or audio of this project is out of date. Rebind the project first."
    ),
    LongFormMixErrorCode.ASSET_UNAVAILABLE: (
        "A sound file of this LongForm video is missing or has changed."
    ),
    LongFormMixErrorCode.ASSET_NOT_AUDIO: (
        "A file used as music or sound effect is not an audio file."
    ),
    LongFormMixErrorCode.ASSET_RIGHTS: (
        "A sound file of this LongForm video has no declared rights category yet."
    ),
    LongFormMixErrorCode.FORMAT_UNSUPPORTED: (
        "A sound file of this LongForm video is not in a supported audio format."
    ),
    LongFormMixErrorCode.MIX_INVALID: ("The mixed audio did not pass its checks."),
}

HTTP_STATUS = {
    LongFormMixErrorCode.PROJECT_NOT_FOUND: HTTPStatus.NOT_FOUND,
    LongFormMixErrorCode.CONFLICT: HTTPStatus.CONFLICT,
    LongFormMixErrorCode.STALE_INPUT: HTTPStatus.CONFLICT,
}

_PROJECT_CODES = LongFormProjectErrorCode
ASSET_CODES = {
    _PROJECT_CODES.ASSET_NOT_FOUND.value: LongFormMixErrorCode.ASSET_UNAVAILABLE,
    _PROJECT_CODES.ASSET_OTHER_CHANNEL.value: LongFormMixErrorCode.ASSET_UNAVAILABLE,
    _PROJECT_CODES.ASSET_NOT_ATTACHED.value: LongFormMixErrorCode.ASSET_UNAVAILABLE,
    _PROJECT_CODES.ASSET_RIGHTS_UNKNOWN.value: LongFormMixErrorCode.ASSET_RIGHTS,
    _PROJECT_CODES.ASSET_KIND_MISMATCH.value: LongFormMixErrorCode.ASSET_NOT_AUDIO,
}


class LongFormMixError(DomainError):
    """A LongForm mix was refused; ``code`` is a ``LongFormMixErrorCode``."""

    default_code = LongFormMixErrorCode.MIX.value
    default_user_message = USER_MESSAGES[LongFormMixErrorCode.MIX]

    def __init__(
        self,
        detail: str = "",
        *,
        code: LongFormMixErrorCode = LongFormMixErrorCode.MIX,
    ) -> None:
        super().__init__(detail, code=code.value, user_message=USER_MESSAGES[code])
        if code in HTTP_STATUS:
            self.default_http_status = HTTP_STATUS[code]


@dataclass(frozen=True)
class _Planned:
    """A music or sound effect placement with absolute times."""

    asset_id: str
    role: MixRole
    start_ms: int
    end_ms: int | None
    source_in_ms: int | None
    source_out_ms: int | None
    gain_db: int | float | None
    fade_in_ms: int
    fade_out_ms: int
    provenance_id: str | None


def _verified(data: bytes, sha256: str, size_bytes: int) -> bytes:
    if len(data) != size_bytes or hashlib.sha256(data).hexdigest() != sha256:
        raise ValueError("the file differs from the stored artifact")
    return data


class LongFormAudioMixer:
    def __init__(
        self,
        database: Database,
        storage: ArtifactStorage,
        audit: AuditLog,
        flags: FeatureFlags | None = None,
        asset_source: AssetSource | None = None,
        *,
        constraints: MixConstraints | None = None,
    ) -> None:
        self._database = database
        self._storage = storage
        self._audit = audit
        self._flags = (
            flags if flags is not None else FeatureFlags(longform_enabled=True)
        )
        self._assets = asset_source or ArtifactAssetSource(database, storage)
        self._constraints = constraints or DEFAULT_CONSTRAINTS

    def mix(
        self, project_id: str, *, expected_version: int, actor: Actor
    ) -> MixOutcome:
        if not self._flags.longform_enabled:
            raise LongFormDisabledError("LONGFORM_ENABLED is off")
        project, planned, assets, voice_artifact = self._read(
            project_id, expected_version
        )
        voice_bytes = self._voice_bytes(project, voice_artifact)
        voice = _decode(voice_bytes, f"the voice of project {project.id}")
        decoded: dict[str, PcmAudio] = {}
        for asset_id, asset in assets.items():
            decoded[asset_id] = self._decode_asset(asset)
        tracks = [_track(item, decoded[item.asset_id]) for item in planned]
        try:
            result = mix_audio(voice, tracks, self._constraints)
        except MixInputError as error:
            raise LongFormMixError(
                str(error), code=LongFormMixErrorCode.MIX_INVALID
            ) from None
        sha256 = hashlib.sha256(result.wav).hexdigest()
        warned = self._self_check(project, result.wav, sha256)
        outcome = MixOutcome(
            wav=result.wav,
            sha256=sha256,
            report=result.report,
            timeline_sha256=project.timeline.sha256,
            voice_sha256=project.audio_sha256,
            asset_ids=tuple(sorted(assets)),
            qc_warnings=warned,
        )
        self._audit.record(
            MIXED,
            actor,
            EntityRef("longform_project", project.id),
            AuditResult.SUCCESS,
            self._event_metadata(project, outcome, planned),
        )
        return outcome

    def _read(self, project_id: str, expected_version: int):
        with self._database.transaction() as connection:
            project = LongFormProjectRepository(connection).get(project_id)
            if project is None:
                raise LongFormMixError(
                    f"LongForm project {project_id} does not exist",
                    code=LongFormMixErrorCode.PROJECT_NOT_FOUND,
                )
            if project.version != expected_version:
                raise LongFormMixError(
                    f"LongForm project {project.id} is version "
                    f"{project.version}, not {expected_version}",
                    code=LongFormMixErrorCode.CONFLICT,
                )
            if project.status is LongFormProjectStatus.DRAFT:
                raise LongFormMixError(
                    f"LongForm project {project.id} is a draft",
                    code=LongFormMixErrorCode.NOT_READY,
                )
            if not longform_project_is_current(connection, project):
                raise LongFormMixError(
                    f"the script or audio bound to project {project.id} is no "
                    "longer the latest of its content item",
                    code=LongFormMixErrorCode.STALE_INPUT,
                )
            item = ContentItemRepository(connection).get(project.content_item_id)
            if item is None:
                raise LongFormMixError(
                    f"content item {project.content_item_id} does not exist",
                    code=LongFormMixErrorCode.PROJECT_NOT_FOUND,
                )
            planned = _planned(project.timeline)
            try:
                assets = check_longform_audio_assets(
                    connection,
                    item,
                    [_placement(entry) for entry in planned],
                )
            except LongFormProjectError as error:
                raise LongFormMixError(
                    error.detail,
                    code=ASSET_CODES.get(error.code, LongFormMixErrorCode.MIX),
                ) from None
            provenance_repo = ProvenanceRepository(connection)
            for entry in planned:
                latest_prov = provenance_repo.latest(entry.asset_id)
                current_prov = latest_prov.id if latest_prov is not None else None
                if (
                    entry.provenance_id is not None
                    and entry.provenance_id != current_prov
                ):
                    raise LongFormMixError(
                        f"asset {entry.asset_id} provenance changed from "
                        f"{entry.provenance_id} to {current_prov}",
                        code=LongFormMixErrorCode.ASSET_UNAVAILABLE,
                    )
            voice_artifact = ArtifactRepository(connection).get(
                project.audio_artifact_id
            )
        if voice_artifact is None:
            raise LongFormMixError(
                f"audio artifact {project.audio_artifact_id} does not exist",
                code=LongFormMixErrorCode.ASSET_UNAVAILABLE,
            )
        return project, planned, assets, voice_artifact

    def _voice_bytes(self, project: LongFormProject, artifact) -> bytes:
        try:
            data = self._storage.read(artifact.uri)
            return _verified(data, project.audio_sha256, artifact.size_bytes)
        except (OSError, ValueError) as error:
            raise LongFormMixError(
                f"the voice of project {project.id} is unavailable: {error}",
                code=LongFormMixErrorCode.ASSET_UNAVAILABLE,
            ) from None

    def _decode_asset(self, asset: Asset) -> PcmAudio:
        try:
            data = self._assets.read_audio(asset)
        except (OSError, ValueError) as error:
            raise LongFormMixError(
                f"the file of asset {asset.id} is unavailable: {error}",
                code=LongFormMixErrorCode.ASSET_UNAVAILABLE,
            ) from None
        return _decode(data, f"asset {asset.id}")

    def _self_check(
        self, project: LongFormProject, wav: bytes, sha256: str
    ) -> tuple[str, ...]:
        checks, _ = analyze(
            wav,
            expected_sha256=sha256,
            expected_size=len(wav),
            expected_duration_ms=project.audio_duration_ms,
        )
        failed = [check for check in checks if check.status is QCStatus.FAIL]
        if failed:
            raise LongFormMixError(
                "; ".join(f"{check.name}: {check.detail}" for check in failed),
                code=LongFormMixErrorCode.MIX_INVALID,
            )
        return tuple(check.name for check in checks if check.status is QCStatus.WARN)

    def _event_metadata(
        self,
        project: LongFormProject,
        outcome: MixOutcome,
        planned: list[_Planned],
    ) -> Mapping[str, MetadataValue]:
        return {
            "project_id": project.id,
            "content_item_id": project.content_item_id,
            "project_version": project.version,
            "timeline_sha256": outcome.timeline_sha256,
            "voice_sha256": outcome.voice_sha256,
            "sha256": outcome.sha256,
            "size_bytes": len(outcome.wav),
            "asset_ids": ",".join(outcome.asset_ids),
            "asset_count": len(outcome.asset_ids),
            "music_count": sum(entry.role is MixRole.MUSIC for entry in planned),
            "sfx_count": sum(entry.role is MixRole.SFX for entry in planned),
            "qc_warned": ",".join(outcome.qc_warnings),
            **self._constraints.to_dict(),
            **outcome.report.metadata(),
        }


def _decode(data: bytes, what: str) -> PcmAudio:
    try:
        return decode_wav(data)
    except MixInputError as error:
        raise LongFormMixError(
            f"{what} cannot be mixed: {error}",
            code=LongFormMixErrorCode.FORMAT_UNSUPPORTED,
        ) from None


def _planned(timeline: LongFormTimeline) -> list[_Planned]:
    planned = [
        _Planned(
            music.asset_id,
            MixRole.MUSIC,
            music.start_ms,
            music.end_ms,
            music.source_in_ms,
            music.source_out_ms,
            music.gain_db,
            music.fade_in_ms,
            music.fade_out_ms,
            music.provenance_id,
        )
        for music in timeline.music
    ]
    for scene in timeline.scenes:
        for effect in scene.assets:
            if effect.role is not AssetRole.SFX:
                continue
            length = scene.duration_ms if effect.end_ms is None else effect.end_ms
            planned.append(
                _Planned(
                    effect.asset_id,
                    MixRole.SFX,
                    scene.start_ms + effect.start_ms,
                    scene.start_ms + length,
                    effect.source_in_ms,
                    effect.source_out_ms,
                    effect.gain_db,
                    effect.fade_in_ms,
                    effect.fade_out_ms,
                    effect.provenance_id,
                )
            )
    return planned


def _placement(entry: _Planned) -> SceneAsset | AssetPlacement:
    if entry.role is MixRole.MUSIC:
        return AssetPlacement(entry.asset_id)
    return SceneAsset(entry.asset_id, AssetRole.SFX)


def _track(entry: _Planned, pcm: PcmAudio) -> MixTrack:
    try:
        return MixTrack(
            entry.role,
            pcm,
            entry.start_ms,
            entry.end_ms,
            entry.source_in_ms,
            entry.source_out_ms,
            entry.gain_db,
            entry.fade_in_ms,
            entry.fade_out_ms,
            label=entry.asset_id,
        )
    except MixInputError as error:
        raise LongFormMixError(
            str(error), code=LongFormMixErrorCode.MIX_INVALID
        ) from None
