"""Local filesystem Artifact storage."""

from __future__ import annotations

import hashlib
from pathlib import Path
from urllib.parse import unquote, urlparse

from maestro.domain.artifacts import (
    Artifact,
    ArtifactIntegrityResult,
    ArtifactStorageError,
    ArtifactStorageMetadata,
    ArtifactStorageWriteResult,
)

CHUNK_SIZE = 1024 * 1024


class LocalArtifactStorage:
    """Store Artifact bytes under a local filesystem root."""

    def __init__(self, root: Path | str) -> None:
        self._root = Path(root)
        self._root.mkdir(parents=True, exist_ok=True)

    async def write_bytes(
        self,
        relative_path: Path,
        content: bytes,
    ) -> ArtifactStorageWriteResult:
        """Persist Artifact bytes and return storage metadata."""

        target = self._resolve_child(relative_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.is_symlink():
            raise ArtifactStorageError("artifact target must not be a symlink")
        if target.exists():
            existing = target.read_bytes()
            if existing != content:
                raise ArtifactStorageError(
                    "artifact storage path already contains different bytes"
                )
        else:
            target.write_bytes(content)

        sha256 = hashlib.sha256(content).hexdigest()
        return ArtifactStorageWriteResult(
            storage=ArtifactStorageMetadata(uri=target.resolve(strict=True).as_uri()),
            sha256=sha256,
            sizeBytes=len(content),
        )

    async def read_bytes(self, artifact: Artifact) -> bytes:
        """Read Artifact bytes from local storage."""

        path = self._path_from_uri(artifact.spec.storage.uri)
        if not path.exists() or not path.is_file():
            raise ArtifactStorageError("artifact content is missing")
        return path.read_bytes()

    async def verify(self, artifact: Artifact) -> ArtifactIntegrityResult:
        """Compute Artifact content integrity evidence."""

        path = self._path_from_uri(artifact.spec.storage.uri, require_exists=False)
        if not path.exists() or not path.is_file():
            return ArtifactIntegrityResult(exists=False)

        sha256 = hashlib.sha256()
        size_bytes = 0
        with path.open("rb") as handle:
            while chunk := handle.read(CHUNK_SIZE):
                size_bytes += len(chunk)
                sha256.update(chunk)

        return ArtifactIntegrityResult(
            exists=True,
            sha256=sha256.hexdigest(),
            sizeBytes=size_bytes,
        )

    def _resolve_child(self, relative_path: Path) -> Path:
        if relative_path.is_absolute():
            raise ArtifactStorageError("artifact path must be relative")

        root = self._root.resolve(strict=True)
        target = (root / relative_path).resolve(strict=False)
        try:
            target.relative_to(root)
        except ValueError as error:
            raise ArtifactStorageError("artifact path escapes storage root") from error
        return target

    def _path_from_uri(
        self,
        uri: str,
        *,
        require_exists: bool = True,
    ) -> Path:
        parsed = urlparse(uri)
        if parsed.scheme != "file":
            raise ArtifactStorageError("local artifact storage only supports file URIs")
        if parsed.netloc not in {"", "localhost"}:
            raise ArtifactStorageError("file URI host must be local")

        raw_path = Path(unquote(parsed.path))
        if not raw_path.is_absolute():
            raise ArtifactStorageError("file URI path must be absolute")

        root = self._root.resolve(strict=True)
        resolved = raw_path.resolve(strict=require_exists)
        try:
            resolved.relative_to(root)
        except ValueError as error:
            raise ArtifactStorageError("artifact URI escapes storage root") from error
        return resolved
