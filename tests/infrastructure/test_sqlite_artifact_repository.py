"""Tests for SQLite Artifact persistence."""

import asyncio
import hashlib
from uuid import UUID, uuid4

import pytest

from maestro.domain import ResourceSelector
from maestro.domain.artifacts import (
    Artifact,
    ArtifactExecutionReference,
    ArtifactIntegrityResult,
    ArtifactPhase,
    ArtifactProducer,
    ArtifactSpec,
    ArtifactStorageMetadata,
    ArtifactType,
    ArtifactWorkItemReference,
    apply_artifact_status_update,
    artifact_status_from_integrity,
)
from maestro.domain.exceptions import (
    ResourceConflictError,
    ResourceImmutableFieldError,
)
from maestro.infrastructure.persistence import SQLiteArtifactRepository


def checksum(content: bytes = b"diff\n") -> str:
    """Return a SHA-256 checksum for test content."""

    return hashlib.sha256(content).hexdigest()


def valid_artifact_spec(
    execution_id: UUID | None = None,
    *,
    work_item_id: UUID | None = None,
) -> ArtifactSpec:
    """Build a valid ArtifactSpec for persistence tests."""

    return ArtifactSpec(
        executionRef=ArtifactExecutionReference(
            id=execution_id or uuid4(),
            name="implement-health",
        ),
        workItemRef=(
            ArtifactWorkItemReference(id=work_item_id, name="add-health")
            if work_item_id is not None
            else None
        ),
        type=ArtifactType.GIT_DIFF,
        mediaType="text/x-diff",
        storage=ArtifactStorageMetadata(uri="file:///tmp/artifacts/diff.patch"),
        sha256=checksum(),
        sizeBytes=len(b"diff\n"),
        producer=ArtifactProducer(subsystem="workspace-controller"),
    )


def valid_artifact(
    execution_id: UUID | None = None,
    *,
    work_item_id: UUID | None = None,
    name: str = "execution-git-diff",
) -> Artifact:
    """Build a valid Artifact resource."""

    return Artifact.new(
        name=name,
        spec=valid_artifact_spec(execution_id, work_item_id=work_item_id),
    )


def test_artifact_persistence_round_trip(tmp_path) -> None:
    async def scenario() -> None:
        repository = SQLiteArtifactRepository(tmp_path / "maestro.db")
        artifact = await repository.create(valid_artifact())
        loaded = await repository.get(artifact.metadata.id)

        assert loaded == artifact
        repository.close()

    asyncio.run(scenario())


def test_artifact_persistence_survives_repository_restart(tmp_path) -> None:
    async def scenario() -> None:
        database_path = tmp_path / "maestro.db"
        first_repository = SQLiteArtifactRepository(database_path)
        artifact = await first_repository.create(valid_artifact())
        first_repository.close()

        second_repository = SQLiteArtifactRepository(database_path)
        loaded = await second_repository.get(artifact.metadata.id)

        assert loaded.metadata.id == artifact.metadata.id
        assert loaded.spec.sha256 == checksum()
        second_repository.close()

    asyncio.run(scenario())


def test_artifact_repository_lists_by_execution_work_item_and_labels() -> None:
    async def scenario() -> None:
        repository = SQLiteArtifactRepository(":memory:")
        execution_id = uuid4()
        work_item_id = uuid4()
        artifact = valid_artifact(execution_id, work_item_id=work_item_id)
        labeled_artifact = artifact.model_copy(
            update={
                "metadata": artifact.metadata.model_copy(
                    update={"labels": {"kind": "evidence"}}
                )
            }
        )
        await repository.create(labeled_artifact)
        await repository.create(valid_artifact(name="other-artifact"))

        by_execution = await repository.list_by_execution(execution_id)
        by_work_item = await repository.list_by_work_item(work_item_id)
        by_label = await repository.list(ResourceSelector(labels={"kind": "evidence"}))

        assert [artifact.metadata.name for artifact in by_execution] == [
            "execution-git-diff"
        ]
        assert [artifact.metadata.name for artifact in by_work_item] == [
            "execution-git-diff"
        ]
        assert [artifact.metadata.name for artifact in by_label] == [
            "execution-git-diff"
        ]
        repository.close()

    asyncio.run(scenario())


def test_artifact_update_status_records_integrity() -> None:
    async def scenario() -> None:
        repository = SQLiteArtifactRepository(":memory:")
        artifact = await repository.create(valid_artifact())
        status = artifact_status_from_integrity(
            artifact,
            ArtifactIntegrityResult(
                exists=True,
                sha256=artifact.spec.sha256,
                sizeBytes=artifact.spec.size_bytes,
            ),
        )

        updated = await repository.update_status(
            artifact.metadata.id,
            status,
            expected_resource_version=artifact.metadata.resource_version,
        )

        assert updated.status.phase == ArtifactPhase.AVAILABLE
        assert updated.status.verified_sha256 == artifact.spec.sha256
        assert updated.metadata.generation == 1
        assert updated.metadata.resource_version == 2
        repository.close()

    asyncio.run(scenario())


def test_artifact_spec_updates_are_rejected() -> None:
    async def scenario() -> None:
        repository = SQLiteArtifactRepository(":memory:")
        artifact = await repository.create(valid_artifact())
        changed_spec = artifact.spec.model_copy(update={"media_type": "text/plain"})

        with pytest.raises(ResourceImmutableFieldError):
            await repository.update_spec(
                artifact.metadata.id,
                changed_spec,
                expected_resource_version=artifact.metadata.resource_version,
            )
        repository.close()

    asyncio.run(scenario())


def test_artifact_stale_status_update_returns_conflict() -> None:
    async def scenario() -> None:
        repository = SQLiteArtifactRepository(":memory:")
        artifact = await repository.create(valid_artifact())
        status = apply_artifact_status_update(
            artifact,
            artifact_status_from_integrity(
                artifact,
                ArtifactIntegrityResult(
                    exists=True,
                    sha256=artifact.spec.sha256,
                    sizeBytes=artifact.spec.size_bytes,
                ),
            ),
            expected_resource_version=artifact.metadata.resource_version,
        ).status
        await repository.update_status(
            artifact.metadata.id,
            status,
            expected_resource_version=artifact.metadata.resource_version,
        )

        with pytest.raises(ResourceConflictError):
            await repository.update_status(
                artifact.metadata.id,
                status,
                expected_resource_version=artifact.metadata.resource_version,
            )
        repository.close()

    asyncio.run(scenario())
