"""H-089 Artifact storage (Prompt Pack v8, prompt #089).

Rules the user approved on 2026-10-16:

- ``LocalFileStorage`` keeps files under one root at
  ``<root>/<content_item_id>/<kind>/v<version>.<ext>`` with an absolute
  ``file://`` uri; the root is created on the first write;
- ids, kinds, versions and media types are checked before the disk is
  touched, and every path stays inside the root;
- a write goes through a temporary file that is renamed over the final name
  and removed when anything fails;
- ``read`` and ``delete`` accept only ``file://`` uris inside the root, and
  deleting a missing file does nothing; a uri with a host is accepted only for
  a UNC root on that server.
"""

import hashlib
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
from urllib.parse import urlsplit

import pytest

from ai_youtube_agent.core import artifact_storage
from ai_youtube_agent.core.artifact import Artifact, ArtifactKind
from ai_youtube_agent.core.artifact_storage import (
    MEDIA_EXTENSIONS,
    ArtifactStorage,
    LocalFileStorage,
    StoredFile,
    unc_host,
)

AUDIO = ArtifactKind.AUDIO
WAV = "audio/wav"
ITEM = "0123456789abcdef0123456789abcdef"


@pytest.fixture
def root(tmp_path: Path) -> Path:
    return tmp_path / "artifacts"


@pytest.fixture
def storage(root: Path) -> LocalFileStorage:
    return LocalFileStorage(root)


def files_under(path: Path) -> list[Path]:
    return sorted(p for p in path.rglob("*") if p.is_file()) if path.exists() else []


# Writing


def test_a_write_stores_the_bytes_at_the_versioned_path(
    storage: LocalFileStorage, root: Path
) -> None:
    stored = storage.write(ITEM, AUDIO, 1, b"RIFF-audio", media_type=WAV)

    path = root.resolve() / ITEM / "audio" / "v1.wav"
    assert path.read_bytes() == b"RIFF-audio"
    assert stored == StoredFile(
        uri=path.as_uri(),
        sha256=hashlib.sha256(b"RIFF-audio").hexdigest(),
        size_bytes=len(b"RIFF-audio"),
    )
    assert stored.uri.startswith("file://")
    assert files_under(root) == [path]


def test_versions_and_kinds_get_their_own_files(
    storage: LocalFileStorage, root: Path
) -> None:
    first = storage.write(ITEM, AUDIO, 1, b"one", media_type=WAV)
    second = storage.write(ITEM, AUDIO, 2, b"two", media_type=WAV)
    video = storage.write(ITEM, ArtifactKind.VIDEO, 1, b"three", media_type=WAV)

    assert len({first.uri, second.uri, video.uri}) == 3
    assert storage.read(first.uri) == b"one"
    assert storage.read(second.uri) == b"two"
    assert storage.read(video.uri) == b"three"
    assert (root.resolve() / ITEM / "video" / "v1.wav").is_file()


def test_a_root_with_a_space_gives_a_percent_encoded_uri(tmp_path: Path) -> None:
    storage = LocalFileStorage(tmp_path / "my artifacts")

    stored = storage.write(ITEM, AUDIO, 1, b"spaced", media_type=WAV)

    assert " " not in stored.uri
    assert "%20" in stored.uri
    assert storage.read(stored.uri) == b"spaced"
    artifact = Artifact.create(
        ITEM,
        AUDIO,
        uri=stored.uri,
        sha256=stored.sha256,
        size_bytes=stored.size_bytes,
        media_type=WAV,
    )
    assert artifact.uri == stored.uri


def test_read_returns_what_was_written(storage: LocalFileStorage) -> None:
    data = bytes(range(256)) * 10

    stored = storage.write(ITEM, AUDIO, 3, data, media_type=WAV)

    assert storage.read(stored.uri) == data


def test_a_bytearray_is_accepted(storage: LocalFileStorage) -> None:
    stored = storage.write(ITEM, AUDIO, 1, bytearray(b"abc"), media_type=WAV)

    assert storage.read(stored.uri) == b"abc"


def test_an_orphan_file_is_overwritten(storage: LocalFileStorage, root: Path) -> None:
    path = root.resolve() / ITEM / "audio" / "v1.wav"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"left by a failed run")

    stored = storage.write(ITEM, AUDIO, 1, b"fresh", media_type=WAV)

    assert path.read_bytes() == b"fresh"
    assert stored.size_bytes == len(b"fresh")
    assert files_under(root) == [path]


def test_no_temporary_file_is_left_after_a_write(
    storage: LocalFileStorage, root: Path
) -> None:
    storage.write(ITEM, AUDIO, 1, b"data", media_type=WAV)
    storage.write(ITEM, AUDIO, 1, b"again", media_type=WAV)

    assert [p.name for p in files_under(root)] == ["v1.wav"]


def test_a_failed_rename_removes_the_temporary_file(
    storage: LocalFileStorage, root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class BrokenOs:
        def __getattr__(self, name):
            return getattr(os, name)

        @staticmethod
        def replace(source, target):
            raise OSError("disk full")

    monkeypatch.setattr(artifact_storage, "os", BrokenOs())

    with pytest.raises(OSError, match="disk full"):
        storage.write(ITEM, AUDIO, 1, b"data", media_type=WAV)

    assert files_under(root) == []


def test_a_failed_rename_keeps_an_older_file(
    storage: LocalFileStorage, root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage.write(ITEM, AUDIO, 1, b"old", media_type=WAV)

    class BrokenOs:
        def __getattr__(self, name):
            return getattr(os, name)

        @staticmethod
        def replace(source, target):
            raise OSError("disk full")

    monkeypatch.setattr(artifact_storage, "os", BrokenOs())

    with pytest.raises(OSError):
        storage.write(ITEM, AUDIO, 1, b"new", media_type=WAV)

    path = root.resolve() / ITEM / "audio" / "v1.wav"
    assert files_under(root) == [path]
    assert path.read_bytes() == b"old"


# Refused writes


@pytest.mark.parametrize(
    "item_id",
    [
        "",
        "../escape",
        "a/b",
        "a\\b",
        ".hidden",
        "-dash",
        "_under",
        "a b",
        "a\n",
        "é",
        "a" * 129,
        "C:",
        None,
        5,
    ],
    ids=repr,
)
def test_a_bad_content_item_id_is_refused_before_the_disk(
    storage: LocalFileStorage, root: Path, item_id
) -> None:
    with pytest.raises(ValueError, match="content_item_id"):
        storage.write(item_id, AUDIO, 1, b"data", media_type=WAV)

    assert not root.exists()


def test_the_longest_content_item_id_is_accepted(storage: LocalFileStorage) -> None:
    stored = storage.write("a" * 128, AUDIO, 1, b"data", media_type=WAV)

    assert storage.read(stored.uri) == b"data"


@pytest.mark.parametrize("version", [0, -1, True, False, 1.0, "1", None])
def test_a_bad_version_is_refused_before_the_disk(
    storage: LocalFileStorage, root: Path, version
) -> None:
    with pytest.raises(ValueError, match="version"):
        storage.write(ITEM, AUDIO, version, b"data", media_type=WAV)

    assert not root.exists()


@pytest.mark.parametrize("kind", ["audio", None, 1])
def test_a_kind_that_is_not_an_artifact_kind_is_refused(
    storage: LocalFileStorage, root: Path, kind
) -> None:
    with pytest.raises(TypeError, match="kind"):
        storage.write(ITEM, kind, 1, b"data", media_type=WAV)

    assert not root.exists()


@pytest.mark.parametrize(
    "media_type", ["audio/mpeg", "video/quicktime", "AUDIO/WAV", "audio/wav ", "", None]
)
def test_an_unknown_media_type_is_refused(
    storage: LocalFileStorage, root: Path, media_type
) -> None:
    with pytest.raises(ValueError, match="media type"):
        storage.write(ITEM, AUDIO, 1, b"data", media_type=media_type)

    assert not root.exists()


@pytest.mark.parametrize("data", [b"", bytearray(), "text", None, 5])
def test_empty_or_non_bytes_data_is_refused(
    storage: LocalFileStorage, root: Path, data
) -> None:
    with pytest.raises(ValueError, match="data"):
        storage.write(ITEM, AUDIO, 1, data, media_type=WAV)

    assert not root.exists()


def test_supported_media_extensions() -> None:
    assert MEDIA_EXTENSIONS == {
        "audio/wav": "wav",
        "application/x-subrip": "srt",
        "text/vtt": "vtt",
        "video/mp4": "mp4",
        "image/png": "png",
        "image/jpeg": "jpg",
        "image/webp": "webp",
        "application/json": "json",
    }


def test_an_mp4_video_is_stored_as_v1_mp4(
    storage: LocalFileStorage, root: Path
) -> None:
    stored = storage.write(
        ITEM, ArtifactKind.VIDEO, 1, b"mp4 bytes", media_type="video/mp4"
    )

    path = root.resolve() / ITEM / "video" / "v1.mp4"
    assert path.read_bytes() == b"mp4 bytes"
    assert stored.uri == path.as_uri()
    assert storage.read(stored.uri) == b"mp4 bytes"


@pytest.mark.parametrize(
    ("kind", "media_type", "ext"),
    [
        (ArtifactKind.THUMBNAIL, "image/png", "png"),
        (ArtifactKind.THUMBNAIL, "image/jpeg", "jpg"),
        (ArtifactKind.THUMBNAIL, "image/webp", "webp"),
        (ArtifactKind.METADATA, "application/json", "json"),
    ],
)
def test_thumbnail_and_metadata_extensions_are_stored(
    storage: LocalFileStorage,
    root: Path,
    kind: ArtifactKind,
    media_type: str,
    ext: str,
) -> None:
    stored = storage.write(ITEM, kind, 1, b"payload", media_type=media_type)

    path = root.resolve() / ITEM / kind.value / f"v1.{ext}"
    assert path.read_bytes() == b"payload"
    assert stored.uri == path.as_uri()
    assert storage.read(stored.uri) == b"payload"


# Reading and deleting


def test_delete_removes_the_file_and_a_missing_file_is_a_no_op(
    storage: LocalFileStorage, root: Path
) -> None:
    stored = storage.write(ITEM, AUDIO, 1, b"data", media_type=WAV)

    storage.delete(stored.uri)
    storage.delete(stored.uri)

    assert files_under(root) == []
    with pytest.raises(FileNotFoundError):
        storage.read(stored.uri)


def test_deleting_a_never_written_file_inside_the_root_is_a_no_op(
    storage: LocalFileStorage, root: Path
) -> None:
    storage.delete((root.resolve() / ITEM / "audio" / "v9.wav").as_uri())

    assert not root.exists()


def outside_uris(root: Path, outside: Path) -> list[str]:
    base = root.resolve().as_uri()
    return [
        outside.as_uri(),
        f"{base}/%2e%2e/{outside.name}",
        f"{base}/{ITEM}/%2E%2E/%2E%2E/{outside.name}",
        f"{base}/../{outside.name}",
        base,
        "http://example.com/v1.wav",
        "https://example.com/v1.wav",
        f"file://server/share/{outside.name}",
        f"{base}/{ITEM}/audio/v1.wav?x=1",
        f"{base}/{ITEM}/audio/v1.wav#part",
        "v1.wav",
        "",
    ]


@pytest.mark.parametrize("index", range(12))
def test_read_and_delete_refuse_uris_outside_the_root(
    tmp_path: Path, storage: LocalFileStorage, root: Path, index: int
) -> None:
    storage.write(ITEM, AUDIO, 1, b"inside", media_type=WAV)
    outside = tmp_path / "outside.wav"
    outside.write_bytes(b"secret")
    uri = outside_uris(root, outside)[index]

    with pytest.raises(ValueError):
        storage.read(uri)
    with pytest.raises(ValueError):
        storage.delete(uri)

    assert outside.read_bytes() == b"secret"
    assert storage.read((root.resolve() / ITEM / "audio" / "v1.wav").as_uri()) == (
        b"inside"
    )


def test_there_are_twelve_outside_uris(tmp_path: Path, root: Path) -> None:
    # Keeps the parametrised range above in step with the list.
    assert len(outside_uris(root, tmp_path / "outside.wav")) == 12


@pytest.mark.parametrize("host", ["localhost", "server", "127.0.0.1"])
def test_a_uri_with_a_host_is_refused_for_a_local_root(
    storage: LocalFileStorage, root: Path, host: str
) -> None:
    stored = storage.write(ITEM, AUDIO, 1, b"inside", media_type=WAV)
    uri = stored.uri.replace("file://", f"file://{host}", 1)

    with pytest.raises(ValueError, match="only file://"):
        storage.read(uri)
    with pytest.raises(ValueError, match="only file://"):
        storage.delete(uri)

    assert storage.read(stored.uri) == b"inside"


def test_a_unc_root_refuses_a_uri_on_another_server(tmp_path: Path) -> None:
    storage = LocalFileStorage(tmp_path)
    # Pure path: the host is checked before anything touches the network.
    storage.root = PureWindowsPath(r"\\server\share\artifacts")
    uri = f"file://other/share/artifacts/{ITEM}/audio/v1.wav"

    with pytest.raises(ValueError, match="only file://"):
        storage.read(uri)
    with pytest.raises(ValueError, match="only file://"):
        storage.delete(uri)


@pytest.mark.parametrize(
    ("path", "host"),
    [
        (PureWindowsPath(r"\\server\share\artifacts"), "server"),
        (PureWindowsPath("//Server/share/artifacts"), "Server"),
        (PureWindowsPath(r"C:\artifacts"), ""),
        (PureWindowsPath(r"\\?\C:\artifacts"), ""),
        (PureWindowsPath(r"\\.\C:\artifacts"), ""),
        (PureWindowsPath("artifacts"), ""),
        (PurePosixPath("/srv/artifacts"), ""),
        (PurePosixPath("//server/share/artifacts"), ""),
    ],
    ids=str,
)
def test_unc_host_names_the_server_of_a_unc_root(path, host: str) -> None:
    assert unc_host(path) == host


def test_the_uri_of_a_unc_root_names_its_server() -> None:
    # A UNC root gives file://server/... uris, whose host matches unc_host.
    root = PureWindowsPath(r"\\server\share\artifacts")

    uri = (root / ITEM / "audio" / "v1.wav").as_uri()

    assert uri == f"file://server/share/artifacts/{ITEM}/audio/v1.wav"
    assert urlsplit(uri).netloc == unc_host(root)


# Construction


def test_the_root_is_not_created_when_the_storage_is_built(tmp_path: Path) -> None:
    root = tmp_path / "never" / "made"

    storage = LocalFileStorage(root)

    assert storage.root == root.resolve()
    assert not root.exists()
    assert not (tmp_path / "never").exists()


def test_a_relative_root_is_resolved_against_the_working_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)

    storage = LocalFileStorage("data/artifacts")

    assert storage.root == (tmp_path / "data" / "artifacts").resolve()
    assert storage.root.is_absolute()


def test_the_local_storage_is_an_artifact_storage(storage: LocalFileStorage) -> None:
    assert isinstance(storage, ArtifactStorage)
