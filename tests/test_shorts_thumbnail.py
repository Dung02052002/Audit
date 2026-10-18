"""I-101 Shorts thumbnail (Prompt Pack v8, prompt #101).

Tests for:
- ``probe_image`` across PNG, JPEG (SOF0/SOF2) and WebP (VP8, VP8L, VP8X),
  refusing empty bytes, files above 2 MiB, unknown headers, dimensions below
  640x640, and non-9:16 aspect ratios;
- ``generate_thumbnail_png`` determinism, canvas dimensions and valid PNG
  structure;
- ``ThumbnailMetadata`` invariants for ``SELECTED`` and ``GENERATED`` sources;
- ``ShortsThumbnailRecorder.select`` and ``ShortsThumbnailRecorder.generate``:
  storing ``ArtifactKind.THUMBNAIL``, replay on identical sha256, approval
  invalidation, cleanup on transaction failure, D5 asset and provenance checks,
  stale input/version conflict refusals, and scalar-only audit metadata.
"""

import hashlib
import struct
import unicodedata
from datetime import timedelta
from pathlib import Path

import pytest
from factories import T0, USER, at, make_approval_request, make_channel
from test_shorts_scene import Stage

from ai_youtube_agent.content.approval import ApprovalStatus
from ai_youtube_agent.content.asset import Asset, AssetCategory, AssetKind
from ai_youtube_agent.content.shorts_service import ShortsProjectNotFoundError
from ai_youtube_agent.content.shorts_thumbnail import (
    JPEG_MEDIA_TYPE,
    MAX_THUMBNAIL_BYTES,
    PNG_MEDIA_TYPE,
    THUMBNAIL_RECORDED,
    WEBP_MEDIA_TYPE,
    ImageInfo,
    ShortsThumbnailConflictError,
    ShortsThumbnailError,
    ShortsThumbnailErrorCode,
    ShortsThumbnailRecorder,
    ThumbnailMetadata,
    ThumbnailSourceKind,
    generate_thumbnail_png,
    probe_image,
)
from ai_youtube_agent.content.shorts_timeline import AssetRole, SceneAsset
from ai_youtube_agent.core.artifact import ArtifactKind
from ai_youtube_agent.core.artifact_storage import LocalFileStorage
from ai_youtube_agent.core.audit import AuditLog, AuditResult, InMemoryAuditSink
from ai_youtube_agent.core.db.database import Database
from ai_youtube_agent.core.db.repositories.channel import ChannelRepository
from ai_youtube_agent.core.db.repositories.content import (
    ArtifactRepository,
    ContentItemRepository,
)
from ai_youtube_agent.core.db.repositories.review import ApprovalRequestRepository

Code = ShortsThumbnailErrorCode
T1 = T0 + timedelta(hours=1)
SECRET_TITLE = "Secret-Falcon-9912"


def make_jpeg(width: int = 1080, height: int = 1920, marker: int = 0xC0) -> bytes:
    app0 = b"\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
    sof_payload = struct.pack(">BHHB", 8, height, width, 3) + b"\x01\x11\x00\x02\x11\x00\x03\x11\x00"
    sof = bytes([0xFF, marker]) + struct.pack(">H", len(sof_payload) + 2) + sof_payload
    return b"\xff\xd8" + app0 + sof + b"\xff\xd9"


def make_webp_vp8x(width: int = 1080, height: int = 1920) -> bytes:
    w_minus_1 = (width - 1).to_bytes(3, "little")
    h_minus_1 = (height - 1).to_bytes(3, "little")
    vp8x = b"VP8X" + struct.pack("<I", 10) + b"\x00\x00\x00\x00" + w_minus_1 + h_minus_1
    body = b"WEBP" + vp8x
    return b"RIFF" + struct.pack("<I", len(body)) + body


def make_webp_vp8(width: int = 720, height: int = 1280) -> bytes:
    frame = b"\x00\x00\x00\x9d\x01\x2a" + struct.pack("<HH", width, height)
    vp8 = b"VP8 " + struct.pack("<I", len(frame)) + frame
    body = b"WEBP" + vp8
    return b"RIFF" + struct.pack("<I", len(body)) + body


def make_webp_vp8l(width: int = 720, height: int = 1280) -> bytes:
    bits = ((width - 1) & 0x3FFF) | (((height - 1) & 0x3FFF) << 14)
    payload = b"\x2f" + struct.pack("<I", bits) + b"\x00\x00\x00\x00"
    vp8l = b"VP8L" + struct.pack("<I", len(payload)) + payload
    body = b"WEBP" + vp8l
    return b"RIFF" + struct.pack("<I", len(body)) + body


class FakeVisuals:
    def __init__(self, payloads: dict[str, bytes]) -> None:
        self._payloads = dict(payloads)

    def read_bytes(self, asset: Asset) -> bytes:
        return self._payloads[asset.id]


# Image probing and generation


def test_probe_png_jpeg_and_webp_variants() -> None:
    png = generate_thumbnail_png(
        width=720,
        height=1280,
        project_id="p1",
        script_id="s1",
        title_text="Quick Hook",
    )
    assert probe_image(png) == ImageInfo(PNG_MEDIA_TYPE, 720, 1280, len(png))

    jpg = make_jpeg(1080, 1920, marker=0xC2)
    assert probe_image(jpg) == ImageInfo(JPEG_MEDIA_TYPE, 1080, 1920, len(jpg))

    webp_x = make_webp_vp8x(1080, 1920)
    assert probe_image(webp_x) == ImageInfo(WEBP_MEDIA_TYPE, 1080, 1920, len(webp_x))

    webp_lossy = make_webp_vp8(720, 1280)
    assert probe_image(webp_lossy) == ImageInfo(
        WEBP_MEDIA_TYPE, 720, 1280, len(webp_lossy)
    )

    webp_lossless = make_webp_vp8l(720, 1280)
    assert probe_image(webp_lossless) == ImageInfo(
        WEBP_MEDIA_TYPE, 720, 1280, len(webp_lossless)
    )


@pytest.mark.parametrize(
    "bad_bytes",
    [
        b"",
        b"not-an-image",
        b"\x89PNG\r\n\x1a\nshort",
        b"\xff\xd8\xff\xd9",
        b"RIFF\x10\x00\x00\x00WEBPXXXX" + b"\x00" * 20,
        make_jpeg(360, 640),  # width < 640
        make_jpeg(1920, 1080),  # 16:9 horizontal, not 9:16 vertical
        b"\x89PNG\r\n\x1a\n" + b"\x00" * (MAX_THUMBNAIL_BYTES + 1),
    ],
)
def test_probe_image_refuses_invalid_images(bad_bytes: bytes) -> None:
    with pytest.raises(ShortsThumbnailError) as exc_info:
        probe_image(bad_bytes)
    assert exc_info.value.code == Code.INVALID_IMAGE.value


def test_generate_thumbnail_png_is_deterministic_and_sensitive_to_inputs() -> None:
    first = generate_thumbnail_png(
        width=1080,
        height=1920,
        project_id="proj-1",
        script_id="script-1",
        title_text="  Xin   chào  ",
    )
    second = generate_thumbnail_png(
        width=1080,
        height=1920,
        project_id="proj-1",
        script_id="script-1",
        title_text=unicodedata.normalize("NFD", "Xin chào"),
    )
    different = generate_thumbnail_png(
        width=1080,
        height=1920,
        project_id="proj-1",
        script_id="script-1",
        title_text="Other Title",
    )

    assert first == second
    assert first != different


def test_thumbnail_metadata_enforces_source_specific_invariants() -> None:
    sha = "a" * 64
    selected = ThumbnailMetadata(
        project_id="p1",
        project_version=1,
        content_item_id="ci1",
        source_kind=ThumbnailSourceKind.SELECTED,
        media_type=PNG_MEDIA_TYPE,
        width=1080,
        height=1920,
        size_bytes=1024,
        sha256=sha,
        asset_id="as1",
        provenance_id="prov1",
    )
    assert selected.as_dict()["source_kind"] == "selected"

    with pytest.raises(ValueError, match="provenance_id"):
        ThumbnailMetadata(
            project_id="p1",
            project_version=1,
            content_item_id="ci1",
            source_kind=ThumbnailSourceKind.SELECTED,
            media_type=PNG_MEDIA_TYPE,
            width=1080,
            height=1920,
            size_bytes=1024,
            sha256=sha,
            asset_id="as1",
        )

    with pytest.raises(ValueError, match="asset_id"):
        ThumbnailMetadata(
            project_id="p1",
            project_version=1,
            content_item_id="ci1",
            source_kind=ThumbnailSourceKind.GENERATED,
            media_type=PNG_MEDIA_TYPE,
            width=1080,
            height=1920,
            size_bytes=1024,
            sha256=sha,
            asset_id="as1",
            title_text="Hook",
        )


# Recorder service


class ThumbnailRig:
    def __init__(self, database: Database, tmp_path: Path) -> None:
        self.database = database
        self.stage = Stage(database)
        self.service = self.stage.world.service
        self.item = self.stage.world.item
        self.image_asset = self.stage.add_asset(AssetKind.IMAGE, provenance=True)
        self.project = self.stage.save(
            self.stage.timeline(SceneAsset(self.image_asset.id, AssetRole.BACKGROUND))
        )
        self.root = tmp_path / "artifacts"
        self.storage = LocalFileStorage(self.root)
        self.sink = InMemoryAuditSink()
        self.audit = AuditLog(self.sink, clock=at(T1))
        self.jpg_bytes = make_jpeg(1080, 1920)
        self.visuals = FakeVisuals({self.image_asset.id: self.jpg_bytes})

    def recorder(self, visuals=None, storage=None) -> ShortsThumbnailRecorder:
        return ShortsThumbnailRecorder(
            self.database,
            storage or self.storage,
            self.audit,
            visuals=visuals or self.visuals,
            clock=at(T1),
        )


@pytest.fixture
def rig(database: Database, tmp_path: Path) -> ThumbnailRig:
    return ThumbnailRig(database, tmp_path)


def test_generate_records_a_png_thumbnail_and_audits_scalars_only(
    rig: ThumbnailRig,
) -> None:
    record = rig.recorder().generate(
        rig.project.id,
        title_text=f"  {SECRET_TITLE}  ",
        actor=USER,
        expected_version=rig.project.version,
    )

    assert record.created is True
    assert record.artifact.kind is ArtifactKind.THUMBNAIL
    assert record.artifact.version == 1
    assert record.artifact.media_type == PNG_MEDIA_TYPE
    assert record.metadata.source_kind is ThumbnailSourceKind.GENERATED
    assert record.metadata.width == 1080
    assert record.metadata.height == 1920
    assert record.metadata.title_text == SECRET_TITLE
    stored_bytes = rig.storage.read(record.artifact.uri)
    assert hashlib.sha256(stored_bytes).hexdigest() == record.artifact.sha256

    assert len(rig.sink.events) == 1
    event = rig.sink.events[0]
    assert event.action == THUMBNAIL_RECORDED
    assert event.result is AuditResult.SUCCESS
    assert event.metadata["source_kind"] == "generated"
    assert SECRET_TITLE not in str(event.metadata)


def test_select_defaults_to_first_scene_image_background_and_replays_when_unchanged(
    rig: ThumbnailRig,
) -> None:
    recorder = rig.recorder()
    first = recorder.select(rig.project.id, actor=USER)

    assert first.created is True
    assert first.artifact.version == 1
    assert first.artifact.media_type == JPEG_MEDIA_TYPE
    assert first.metadata.source_kind is ThumbnailSourceKind.SELECTED
    assert first.metadata.asset_id == rig.image_asset.id
    assert first.metadata.provenance_id is not None

    replay = recorder.select(rig.project.id, actor=USER)
    assert replay.created is False
    assert replay.artifact == first.artifact
    assert replay.invalidation is None
    assert len(rig.sink.events) == 1


def test_recording_a_new_thumbnail_version_invalidates_pending_approvals(
    rig: ThumbnailRig,
) -> None:
    recorder = rig.recorder()
    first = recorder.generate(rig.project.id, title_text="First", actor=USER)
    approval = make_approval_request(
        content_item_id=rig.item.id,
        artifacts=(first.artifact,),
    )
    with rig.database.transaction() as connection:
        ApprovalRequestRepository(connection).add(approval)

    second = recorder.generate(rig.project.id, title_text="Second", actor=USER)

    assert second.created is True
    assert second.artifact.version == 2
    assert second.invalidation is not None
    assert second.invalidation.invalidated_ids == (approval.id,)
    with rig.database.transaction() as connection:
        updated = ApprovalRequestRepository(connection).get(approval.id)
        assert updated is not None
        assert updated.status is ApprovalStatus.INVALIDATED
    assert [e.action for e in rig.sink.events] == [
        THUMBNAIL_RECORDED,
        "approval.invalidated",
        THUMBNAIL_RECORDED,
    ]


def test_select_enforces_d5_asset_and_provenance_rules(rig: ThumbnailRig) -> None:
    recorder = rig.recorder()

    # Unknown asset
    with pytest.raises(ShortsThumbnailError) as exc_info:
        recorder.select(rig.project.id, asset_id="missing-asset", actor=USER)
    assert exc_info.value.code == Code.ASSET_NOT_FOUND.value

    # Other channel
    other_channel = make_channel()
    with rig.database.transaction() as connection:
        ChannelRepository(connection).add(other_channel)
    foreign = rig.stage.add_asset(
        AssetKind.IMAGE, channel_id=other_channel.id, attach=False
    )
    with pytest.raises(ShortsThumbnailError) as exc_info:
        recorder.select(rig.project.id, asset_id=foreign.id, actor=USER)
    assert exc_info.value.code == Code.ASSET_CHANNEL_MISMATCH.value

    # Unattached asset
    unattached = rig.stage.add_asset(AssetKind.IMAGE, attach=False)
    with pytest.raises(ShortsThumbnailError) as exc_info:
        recorder.select(rig.project.id, asset_id=unattached.id, actor=USER)
    assert exc_info.value.code == Code.ASSET_NOT_ATTACHED.value

    # Non-image asset
    clip = rig.stage.add_asset(AssetKind.VIDEO_CLIP)
    with pytest.raises(ShortsThumbnailError) as exc_info:
        recorder.select(rig.project.id, asset_id=clip.id, actor=USER)
    assert exc_info.value.code == Code.ASSET_KIND_INVALID.value

    # Unknown category
    unknown_cat = rig.stage.add_asset(
        AssetKind.IMAGE, category=AssetCategory.UNKNOWN, provenance=False
    )
    with pytest.raises(ShortsThumbnailError) as exc_info:
        recorder.select(rig.project.id, asset_id=unknown_cat.id, actor=USER)
    assert exc_info.value.code == Code.ASSET_CATEGORY_UNKNOWN.value

    # Missing provenance
    no_prov = rig.stage.add_asset(AssetKind.IMAGE, provenance=False)
    with pytest.raises(ShortsThumbnailError) as exc_info:
        recorder.select(rig.project.id, asset_id=no_prov.id, actor=USER)
    assert exc_info.value.code == Code.PROVENANCE_MISSING.value


def test_version_conflict_missing_project_and_stale_inputs_are_refused(
    rig: ThumbnailRig,
) -> None:
    recorder = rig.recorder()

    with pytest.raises(ShortsProjectNotFoundError):
        recorder.generate("missing-project", actor=USER)

    with pytest.raises(ShortsThumbnailConflictError):
        recorder.generate(
            rig.project.id,
            actor=USER,
            expected_version=rig.project.version + 9,
        )

    # Make the bound audio stale by adding a newer audio artifact
    with rig.database.transaction() as connection:
        artifacts = ArtifactRepository(connection)
        latest_audio = artifacts.latest(rig.item.id, ArtifactKind.AUDIO)
        assert latest_audio is not None
        artifacts.add(
            latest_audio.next_version(
                uri="file:///tmp/newer.wav",
                sha256="b" * 64,
                size_bytes=128,
                media_type="audio/wav",
                clock=at(T1),
            )
        )

    with pytest.raises(ShortsThumbnailError) as exc_info:
        recorder.generate(rig.project.id, actor=USER)
    assert exc_info.value.code == Code.STALE_INPUT.value


def test_written_file_is_cleaned_up_when_database_insert_fails(
    rig: ThumbnailRig, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(self, artifact):
        raise RuntimeError("db write failed")

    monkeypatch.setattr(ArtifactRepository, "add", boom)

    with pytest.raises(RuntimeError, match="db write failed"):
        rig.recorder().generate(rig.project.id, title_text="Fail", actor=USER)

    assert not rig.root.exists() or list(rig.root.rglob("*.png")) == []
    with rig.database.transaction() as connection:
        assert (
            ArtifactRepository(connection).latest(
                rig.item.id, ArtifactKind.THUMBNAIL
            )
            is None
        )

