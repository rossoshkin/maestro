"""Tests for Artifact application service."""

import asyncio
from pathlib import Path
from urllib.parse import unquote, urlparse
from uuid import uuid4

from maestro.application.artifacts import ArtifactService
from maestro.domain.artifacts import (
    ArtifactExecutionReference,
    ArtifactPhase,
    ArtifactProducer,
    ArtifactType,
)
from maestro.infrastructure.artifacts import LocalArtifactStorage
from maestro.infrastructure.persistence import SQLiteArtifactRepository


def stored_path(uri: str) -> Path:
    """Convert a file URI to a Path for test tampering."""

    parsed = urlparse(uri)
    return Path(unquote(parsed.path))


def test_artifact_service_creates_and_verifies_artifact(tmp_path) -> None:
    async def scenario() -> None:
        repository = SQLiteArtifactRepository(":memory:")
        storage = LocalArtifactStorage(tmp_path / "artifacts")
        service = ArtifactService(repository, storage)
        execution_ref = ArtifactExecutionReference(
            id=uuid4(),
            name="implement-health",
        )

        artifact = await service.create_bytes_artifact(
            name="command-output",
            execution_ref=execution_ref,
            artifact_type=ArtifactType.COMMAND_OUTPUT,
            media_type="text/plain",
            content=b"pytest passed\n",
            producer=ArtifactProducer(subsystem="verification-controller"),
        )
        verified = await service.verify_artifact(
            artifact,
            expected_resource_version=artifact.metadata.resource_version,
        )

        assert artifact.spec.execution_ref == execution_ref
        assert artifact.spec.producer.subsystem == "verification-controller"
        assert verified.status.phase == ArtifactPhase.AVAILABLE
        assert verified.status.verified_sha256 == artifact.spec.sha256
        repository.close()

    asyncio.run(scenario())


def test_artifact_service_detects_tampered_content(tmp_path) -> None:
    async def scenario() -> None:
        repository = SQLiteArtifactRepository(":memory:")
        storage = LocalArtifactStorage(tmp_path / "artifacts")
        service = ArtifactService(repository, storage)
        artifact = await service.create_bytes_artifact(
            name="command-output",
            execution_ref=ArtifactExecutionReference(id=uuid4()),
            artifact_type=ArtifactType.COMMAND_OUTPUT,
            media_type="text/plain",
            content=b"pytest passed\n",
            producer=ArtifactProducer(subsystem="verification-controller"),
        )
        stored_path(artifact.spec.storage.uri).write_bytes(b"tampered\n")

        verified = await service.verify_artifact(
            artifact,
            expected_resource_version=artifact.metadata.resource_version,
        )

        assert verified.status.phase == ArtifactPhase.CORRUPT
        repository.close()

    asyncio.run(scenario())
