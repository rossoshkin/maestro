"""Tests for local Artifact storage."""

import asyncio
import hashlib
from pathlib import Path
from uuid import uuid4

import pytest

from maestro.domain.artifacts import (
    Artifact,
    ArtifactExecutionReference,
    ArtifactPhase,
    ArtifactProducer,
    ArtifactSpec,
    ArtifactStorageError,
    ArtifactType,
    artifact_status_from_integrity,
)
from maestro.infrastructure.artifacts import LocalArtifactStorage


def checksum(content: bytes) -> str:
    """Return a SHA-256 checksum for test content."""

    return hashlib.sha256(content).hexdigest()


def artifact_from_write_result(
    write_result,
    *,
    content: bytes,
) -> Artifact:
    """Build an Artifact resource for a stored object."""

    execution_id = uuid4()
    return Artifact.new(
        name="command-output",
        spec=ArtifactSpec(
            executionRef=ArtifactExecutionReference(id=execution_id),
            type=ArtifactType.COMMAND_OUTPUT,
            mediaType="text/plain",
            storage=write_result.storage,
            sha256=checksum(content),
            sizeBytes=len(content),
            producer=ArtifactProducer(subsystem="workspace-controller"),
        ),
    )


def test_local_artifact_storage_writes_reads_and_verifies_bytes(tmp_path) -> None:
    async def scenario() -> None:
        storage = LocalArtifactStorage(tmp_path / "artifacts")
        content = b"pytest output\n"

        write_result = await storage.write_bytes(Path("execution/output.txt"), content)
        artifact = artifact_from_write_result(write_result, content=content)
        loaded = await storage.read_bytes(artifact)
        integrity = await storage.verify(artifact)

        assert loaded == content
        assert integrity.exists is True
        assert integrity.sha256 == checksum(content)
        assert integrity.size_bytes == len(content)

    asyncio.run(scenario())


def test_local_artifact_storage_detects_tampering(tmp_path) -> None:
    async def scenario() -> None:
        storage = LocalArtifactStorage(tmp_path / "artifacts")
        content = b"pytest output\n"
        write_result = await storage.write_bytes(Path("execution/output.txt"), content)
        artifact = artifact_from_write_result(write_result, content=content)
        stored_path = Path(write_result.storage.uri.removeprefix("file://"))
        stored_path.write_bytes(b"changed\n")

        integrity = await storage.verify(artifact)

        assert integrity.exists is True
        assert integrity.sha256 != artifact.spec.sha256
        assert integrity.size_bytes == len(b"changed\n")

    asyncio.run(scenario())


def test_local_artifact_storage_reports_missing_content(tmp_path) -> None:
    async def scenario() -> None:
        storage = LocalArtifactStorage(tmp_path / "artifacts")
        content = b"pytest output\n"
        write_result = await storage.write_bytes(Path("execution/output.txt"), content)
        artifact = artifact_from_write_result(write_result, content=content)
        stored_path = Path(write_result.storage.uri.removeprefix("file://"))
        stored_path.unlink()

        integrity = await storage.verify(artifact)

        assert integrity.exists is False

    asyncio.run(scenario())


def test_local_artifact_storage_rejects_path_traversal(tmp_path) -> None:
    async def scenario() -> None:
        storage = LocalArtifactStorage(tmp_path / "artifacts")

        with pytest.raises(ArtifactStorageError):
            await storage.write_bytes(Path("../escape.txt"), b"bad")

    asyncio.run(scenario())


def test_local_artifact_storage_rejects_overwrite_with_different_bytes(
    tmp_path,
) -> None:
    async def scenario() -> None:
        storage = LocalArtifactStorage(tmp_path / "artifacts")
        await storage.write_bytes(Path("execution/output.txt"), b"first")

        with pytest.raises(ArtifactStorageError):
            await storage.write_bytes(Path("execution/output.txt"), b"second")

    asyncio.run(scenario())


def test_local_artifact_storage_integrity_status_marks_corrupt(tmp_path) -> None:
    async def scenario() -> None:
        storage = LocalArtifactStorage(tmp_path / "artifacts")
        content = b"pytest output\n"
        write_result = await storage.write_bytes(Path("execution/output.txt"), content)
        artifact = artifact_from_write_result(write_result, content=content)
        stored_path = Path(write_result.storage.uri.removeprefix("file://"))
        stored_path.write_bytes(b"changed\n")

        integrity = await storage.verify(artifact)
        status = artifact_status_from_integrity(artifact, integrity)

        assert status.phase == ArtifactPhase.CORRUPT

    asyncio.run(scenario())
