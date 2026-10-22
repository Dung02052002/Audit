"""Shorts audio mix (Prompt Pack v8, prompt #099), context C8 Shorts Production.

``ShortsAudioMixer.mix`` combines the voice of a Shorts project with the music
and sound effects of its timeline under ``MixConstraints`` (the engine is
``content/audio_mix.py``). The rules were decided with the user on 2026-10-17:

- The mix is not stored. ``mix`` returns a ``MixOutcome`` with the WAV bytes in
  memory for the render step (#100) and writes nothing to the database or the
  storage; only the event ``audio.mixed`` is audited after the work, with ids,
  checksums, counts, the constraint values and the numbers of the report, never
  any audio bytes.
- The project is read, and its assets are checked, in one read transaction; the
  files are read and the mixing is done outside any transaction. The caller
  passes the ``version`` it last read (``expected_version``). A ``draft`` project
  is ``not_ready`` and a project whose bound script or audio is no longer the
  latest of its item is ``stale_input`` (the same rule as ``is_current``).
- The tracks are the timeline's music (absolute times) and the ``sfx`` assets of
  its scenes (relative to the scene, turned into absolute times). Every asset
  passes the I-096 asset checks again (``check_audio_assets``: same channel,
  attached to the item, rights category known, a kind the role accepts), so an
  asset that was changed after the timeline was saved is refused now. Backgrounds,
  b-roll and overlays have no sound and are not read.
- The bytes come from an ``AssetSource`` (``read_audio(asset)``). The default,
  ``ArtifactAssetSource``, reads the ``Artifact`` of a generated asset
  (``artifact_id``) through the ``ArtifactStorage`` and checks its sha256 and
  size; an asset without a stored file is ``asset_unavailable``. The voice is
  read from the project's bound audio artifact and its sha256 and size are
  checked.
- The result is checked with ``audio_validation.analyze`` (a failing check is
  ``mix_invalid``; warnings are returned in ``qc_warnings`` and audited).
- The constraints are the module defaults of ``audio_mix`` (``DEFAULT_CONSTRAINTS``)
  and may be passed to the constructor; they are not part of ``ShortsConfig``.
"""

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from http import HTTPStatus
from typing import Protocol, runtime_checkable

from ai_youtube_agent.content.asset import Asset
from ai_youtube_agent.content.audio_mix import (
    DEFAULT_CONSTRAINTS,
    MixConstraints,
    MixInputError,
    MixReport,
    MixRole,
    MixTrack,
    PcmAudio,
    decode_wav,
    mix_audio,
)
from ai_youtube_agent.content.audio_validation import analyze
from ai_youtube_agent.content.qc import QCStatus
from ai_youtube_agent.content.shorts_project import ShortsProject, ShortsProjectStatus
from ai_youtube_agent.content.shorts_service import (
    ShortsProjectError,
    ShortsProjectErrorCode,
    check_audio_assets,
    project_is_current,
)
from ai_youtube_agent.content.shorts_timeline import (
    AssetPlacement,
    AssetRole,
    SceneAsset,
    Timeline,
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
from ai_youtube_agent.core.db.repositories.shorts import ShortsProjectRepository
from ai_youtube_agent.core.errors import DomainError

MIXED = "audio.mixed"


class ShortsMixErrorCode(StrEnum):
    MIX = "domain.shorts_mix"
    PROJECT_NOT_FOUND = "domain.shorts_mix_project_not_found"
    CONFLICT = "domain.shorts_mix_conflict"
    NOT_READY = "domain.shorts_mix_not_ready"
    STALE_INPUT = "domain.shorts_mix_stale_input"
    ASSET_UNAVAILABLE = "domain.shorts_mix_asset_unavailable"
    ASSET_NOT_AUDIO = "domain.shorts_mix_asset_not_audio"
    ASSET_RIGHTS = "domain.shorts_mix_asset_rights"
    FORMAT_UNSUPPORTED = "domain.shorts_mix_format_unsupported"
    MIX_INVALID = "domain.shorts_mix_invalid"


USER_MESSAGES = {
    ShortsMixErrorCode.MIX: "The audio could not be mixed.",
    ShortsMixErrorCode.PROJECT_NOT_FOUND: "This Shorts project does not exist.",
    ShortsMixErrorCode.CONFLICT: "The Shorts project changed in the meantime. "
    "Reload it and try again.",
    ShortsMixErrorCode.NOT_READY: "Plan the scenes of this Short before mixing "
    "its audio.",
    ShortsMixErrorCode.STALE_INPUT: "The script or audio of this project is out "
    "of date. Rebind the project first.",
    ShortsMixErrorCode.ASSET_UNAVAILABLE: "A sound file of this Short is missing "
    "or has changed.",
    ShortsMixErrorCode.ASSET_NOT_AUDIO: "A file used as music or sound effect is "
    "not an audio file.",
    ShortsMixErrorCode.ASSET_RIGHTS: "A sound file of this Short has no declared "
    "rights category yet.",
    ShortsMixErrorCode.FORMAT_UNSUPPORTED: "A sound file of this Short is not in "
    "a supported audio format.",
    ShortsMixErrorCode.MIX_INVALID: "The mixed audio did not pass its checks.",
}
HTTP_STATUS = {
    ShortsMixErrorCode.PROJECT_NOT_FOUND: HTTPStatus.NOT_FOUND,
    ShortsMixErrorCode.CONFLICT: HTTPStatus.CONFLICT,
    ShortsMixErrorCode.STALE_INPUT: HTTPStatus.CONFLICT,
}
# How the I-096 asset failures are reported by the mix.
_PROJECT_CODES = ShortsProjectErrorCode
ASSET_CODES = {
    _PROJECT_CODES.ASSET_NOT_FOUND.value: ShortsMixErrorCode.ASSET_UNAVAILABLE,
    _PROJECT_CODES.ASSET_OTHER_CHANNEL.value: ShortsMixErrorCode.ASSET_UNAVAILABLE,
    _PROJECT_CODES.ASSET_NOT_ATTACHED.value: ShortsMixErrorCode.ASSET_UNAVAILABLE,
    _PROJECT_CODES.ASSET_RIGHTS_UNKNOWN.value: ShortsMixErrorCode.ASSET_RIGHTS,
    _PROJECT_CODES.ASSET_KIND_MISMATCH.value: ShortsMixErrorCode.ASSET_NOT_AUDIO,
}


class ShortsMixError(DomainError):
    """A mix was refused; ``code`` is a ``ShortsMixErrorCode``."""

    default_code = ShortsMixErrorCode.MIX.value
    default_user_message = USER_MESSAGES[ShortsMixErrorCode.MIX]

    def __init__(
        self,
        detail: str = "",
        *,
        code: ShortsMixErrorCode = ShortsMixErrorCode.MIX,
    ) -> None:
        super().__init__(detail, code=code.value, user_message=USER_MESSAGES[code])
        if code in HTTP_STATUS:
            self.default_http_status = HTTP_STATUS[code]


@runtime_checkable
class AssetSource(Protocol):
    """Gives the audio bytes of an asset; raises OSError or ValueError if it can't."""

    def read_audio(self, asset: Asset) -> bytes: ...


class ArtifactAssetSource:
    """Reads the file of a generated asset through its ``Artifact``."""

    def __init__(self, database: Database, storage: ArtifactStorage) -> None:
        self._database = database
        self._storage = storage

    def read_audio(self, asset: Asset) -> bytes:
        if asset.artifact_id is None:
            raise ValueError(f"asset {asset.id} has no stored file")
        with self._database.transaction() as connection:
            artifact = ArtifactRepository(connection).get(asset.artifact_id)
        if artifact is None:
            raise ValueError(f"artifact {asset.artifact_id} does not exist")
        data = self._storage.read(artifact.uri)
        return _verified(data, artifact.sha256, artifact.size_bytes)


@dataclass(frozen=True)
class MixOutcome:
    wav: bytes = field(repr=False)
    sha256: str
    report: MixReport
    timeline_sha256: str
    voice_sha256: str
    asset_ids: tuple[str, ...]
    qc_warnings: tuple[str, ...] = ()


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


def _verified(data: bytes, sha256: str, size_bytes: int) -> bytes:
    if len(data) != size_bytes or hashlib.sha256(data).hexdigest() != sha256:
        raise ValueError("the file differs from the stored artifact")
    return data


class ShortsAudioMixer:
    def __init__(
        self,
        database: Database,
        storage: ArtifactStorage,
        audit: AuditLog,
        asset_source: AssetSource | None = None,
        *,
        constraints: MixConstraints | None = None,
    ) -> None:
        self._database = database
        self._storage = storage
        self._audit = audit
        self._assets = asset_source or ArtifactAssetSource(database, storage)
        self._constraints = constraints or DEFAULT_CONSTRAINTS

    def mix(
        self, project_id: str, *, expected_version: int, actor: Actor
    ) -> MixOutcome:
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
            raise ShortsMixError(
                str(error), code=ShortsMixErrorCode.MIX_INVALID
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
            EntityRef("shorts_project", project.id),
            AuditResult.SUCCESS,
            self._event_metadata(project, outcome, planned),
        )
        return outcome

    def _read(self, project_id: str, expected_version: int):
        """Everything the mix needs from the database, in one read transaction."""
        with self._database.transaction() as connection:
            project = ShortsProjectRepository(connection).get(project_id)
            if project is None:
                raise ShortsMixError(
                    f"Shorts project {project_id} does not exist",
                    code=ShortsMixErrorCode.PROJECT_NOT_FOUND,
                )
            if project.version != expected_version:
                raise ShortsMixError(
                    f"Shorts project {project.id} is version {project.version}, "
                    f"not {expected_version}",
                    code=ShortsMixErrorCode.CONFLICT,
                )
            if project.status is ShortsProjectStatus.DRAFT:
                raise ShortsMixError(
                    f"Shorts project {project.id} is a draft",
                    code=ShortsMixErrorCode.NOT_READY,
                )
            if not project_is_current(connection, project):
                raise ShortsMixError(
                    f"the script or audio bound to project {project.id} is no "
                    "longer the latest of its content item",
                    code=ShortsMixErrorCode.STALE_INPUT,
                )
            item = ContentItemRepository(connection).get(project.content_item_id)
            if item is None:
                raise ShortsMixError(
                    f"content item {project.content_item_id} does not exist",
                    code=ShortsMixErrorCode.PROJECT_NOT_FOUND,
                )
            planned = _planned(project.timeline)
            try:
                assets = check_audio_assets(
                    connection,
                    item,
                    [_placement(entry) for entry in planned],
                )
            except ShortsProjectError as error:
                raise ShortsMixError(
                    error.detail,
                    code=ASSET_CODES.get(error.code, ShortsMixErrorCode.MIX),
                ) from None
            voice_artifact = ArtifactRepository(connection).get(
                project.audio_artifact_id
            )
        if voice_artifact is None:
            raise ShortsMixError(
                f"audio artifact {project.audio_artifact_id} does not exist",
                code=ShortsMixErrorCode.ASSET_UNAVAILABLE,
            )
        return project, planned, assets, voice_artifact

    def _voice_bytes(self, project: ShortsProject, artifact) -> bytes:
        try:
            data = self._storage.read(artifact.uri)
            return _verified(data, project.audio_sha256, artifact.size_bytes)
        except (OSError, ValueError) as error:
            raise ShortsMixError(
                f"the voice of project {project.id} is unavailable: {error}",
                code=ShortsMixErrorCode.ASSET_UNAVAILABLE,
            ) from None

    def _decode_asset(self, asset: Asset) -> PcmAudio:
        try:
            data = self._assets.read_audio(asset)
        except (OSError, ValueError) as error:
            raise ShortsMixError(
                f"the file of asset {asset.id} is unavailable: {error}",
                code=ShortsMixErrorCode.ASSET_UNAVAILABLE,
            ) from None
        return _decode(data, f"asset {asset.id}")

    def _self_check(
        self, project: ShortsProject, wav: bytes, sha256: str
    ) -> tuple[str, ...]:
        checks, _ = analyze(
            wav,
            expected_sha256=sha256,
            expected_size=len(wav),
            expected_duration_ms=project.audio_duration_ms,
        )
        failed = [check for check in checks if check.status is QCStatus.FAIL]
        if failed:
            raise ShortsMixError(
                "; ".join(f"{check.name}: {check.detail}" for check in failed),
                code=ShortsMixErrorCode.MIX_INVALID,
            )
        return tuple(check.name for check in checks if check.status is QCStatus.WARN)

    def _event_metadata(
        self, project: ShortsProject, outcome: MixOutcome, planned: list[_Planned]
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
        raise ShortsMixError(
            f"{what} cannot be mixed: {error}",
            code=ShortsMixErrorCode.FORMAT_UNSUPPORTED,
        ) from None


def _planned(timeline: Timeline) -> list[_Planned]:
    """The music and the scene sound effects with absolute times."""
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
                )
            )
    return planned


def _placement(entry: _Planned) -> SceneAsset | AssetPlacement:
    """A placement of the entry's asset and role, for the asset checks."""
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
        raise ShortsMixError(str(error), code=ShortsMixErrorCode.MIX_INVALID) from None
