"""Artifact storage (Prompt Pack v8, prompt #089), context C2 Content Lifecycle.

Where the file of an ``Artifact`` version lives. The rules were approved by the
user on 2026-10-16:

- ``ArtifactStorage`` writes the bytes of one version and returns a
  ``StoredFile`` (uri, sha256, size); it reads and deletes by uri. Deleting a
  file that is already gone does nothing.
- ``LocalFileStorage`` is the only backend: local files under one root folder
  (``Settings.artifact_dir``), at ``<root>/<content_item_id>/<kind>/
  v<version>.<ext>``, with an absolute ``file://`` uri. One root belongs to one
  database. The root is created on the first write, not when the storage is
  built.
- The extension comes from the media type (``MEDIA_EXTENSIONS``: WAV audio,
  SRT subtitles (#098), MP4 video (#100), PNG/JPEG/WebP images (#101) and JSON
  metadata (#102)); an unknown media type is
  refused. The item id, kind and version are checked before anything touches
  the disk, and every path must stay inside the root.
- A write goes to a temporary file in the same folder, is flushed to disk and
  then renamed over the final name, so a reader never sees half a file and an
  orphan left by an earlier failed run is replaced. The temporary file is
  removed when anything fails.
- ``read`` and ``delete`` accept only ``file://`` uris that point inside the
  root. A uri with a host (``file://server/share/...``) is accepted only when
  the root is a UNC path on that server.
"""

import contextlib
import hashlib
import os
import re
import uuid
from dataclasses import dataclass
from pathlib import Path, PurePath
from typing import Protocol, runtime_checkable
from urllib.parse import urlsplit
from urllib.request import url2pathname

from ai_youtube_agent.core.artifact import ArtifactKind

MEDIA_EXTENSIONS = {
    "audio/wav": "wav",
    "application/x-subrip": "srt",
    "video/mp4": "mp4",
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/webp": "webp",
    "application/json": "json",
}
CONTENT_ITEM_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")


@dataclass(frozen=True)
class StoredFile:
    uri: str
    sha256: str
    size_bytes: int


@runtime_checkable
class ArtifactStorage(Protocol):
    """Keeps the files of artifact versions."""

    def write(
        self,
        content_item_id: str,
        kind: ArtifactKind,
        version: int,
        data: bytes,
        *,
        media_type: str,
    ) -> StoredFile: ...

    def read(self, uri: str) -> bytes: ...

    def delete(self, uri: str) -> None: ...


class LocalFileStorage:
    def __init__(self, root: Path | str) -> None:
        self.root = Path(root).resolve()

    def write(
        self,
        content_item_id: str,
        kind: ArtifactKind,
        version: int,
        data: bytes,
        *,
        media_type: str,
    ) -> StoredFile:
        final = self._path_for(content_item_id, kind, version, media_type)
        if not isinstance(data, bytes | bytearray) or not data:
            raise ValueError("data must be non-empty bytes")
        content = bytes(data)
        final.parent.mkdir(parents=True, exist_ok=True)
        temporary = final.with_name(f".{final.name}.{uuid.uuid4().hex}.tmp")
        try:
            with open(temporary, "xb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, final)
        except BaseException:
            with contextlib.suppress(OSError):
                temporary.unlink(missing_ok=True)
            raise
        return StoredFile(
            uri=final.as_uri(),
            sha256=hashlib.sha256(content).hexdigest(),
            size_bytes=len(content),
        )

    def read(self, uri: str) -> bytes:
        return self._path_of(uri).read_bytes()

    def delete(self, uri: str) -> None:
        self._path_of(uri).unlink(missing_ok=True)

    def _path_for(
        self, content_item_id: str, kind: ArtifactKind, version: int, media_type: str
    ) -> Path:
        if not isinstance(content_item_id, str) or not (
            CONTENT_ITEM_ID_PATTERN.fullmatch(content_item_id)
        ):
            raise ValueError(
                "content_item_id must be 1 to 128 letters, digits, '_' or '-'"
            )
        if not isinstance(kind, ArtifactKind):
            raise TypeError("kind must be an ArtifactKind")
        if isinstance(version, bool) or not isinstance(version, int) or version < 1:
            raise ValueError("version must be a whole number of 1 or more")
        extension = MEDIA_EXTENSIONS.get(media_type)
        if extension is None:
            raise ValueError(f"media type {media_type!r} cannot be stored")
        final = (
            self.root / content_item_id / kind.value / f"v{version}.{extension}"
        ).resolve()
        return self._inside(final)

    def _path_of(self, uri: str) -> Path:
        parts = urlsplit(uri)
        if parts.scheme != "file" or parts.query or parts.fragment:
            raise ValueError("only file:// uris of this storage can be used")
        location = parts.path
        if parts.netloc:
            # A UNC root gives file://server/share/... uris; any other host is
            # refused.
            host = unc_host(self.root)
            if not host or parts.netloc.casefold() != host.casefold():
                raise ValueError("only file:// uris of this storage can be used")
            location = f"//{parts.netloc}{parts.path}"
        try:
            path = Path(url2pathname(location)).resolve()
        except (OSError, ValueError) as error:
            raise ValueError("the uri does not name a local file") from error
        return self._inside(path)

    def _inside(self, path: Path) -> Path:
        if path == self.root or not path.is_relative_to(self.root):
            raise ValueError("the path is outside the artifact storage root")
        return path


def unc_host(path: PurePath) -> str:
    """The server of a UNC path such as ``//server/share/...``, or ``""``."""
    drive = path.drive.replace("\\", "/")
    if not drive.startswith("//"):
        return ""
    host = drive[2:].split("/", 1)[0]
    return "" if host in {".", "?"} else host
