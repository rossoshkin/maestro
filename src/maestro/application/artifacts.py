"""Application services for Artifact creation and integrity verification."""

from pathlib import Path

from maestro.domain.artifacts import (
    Artifact,
    ArtifactExecutionReference,
    ArtifactProducer,
    ArtifactRepository,
    ArtifactSpec,
    ArtifactStorage,
    ArtifactType,
    ArtifactWorkItemReference,
    artifact_status_from_integrity,
)
from maestro.domain.resources import ResourceName, ResourceReference


class ArtifactService:
    """Coordinate Artifact byte storage with Artifact metadata persistence."""

    def __init__(
        self,
        artifact_repository: ArtifactRepository,
        artifact_storage: ArtifactStorage,
    ) -> None:
        self._artifact_repository = artifact_repository
        self._artifact_storage = artifact_storage

    async def create_bytes_artifact(
        self,
        *,
        name: ResourceName,
        execution_ref: ArtifactExecutionReference,
        artifact_type: ArtifactType,
        media_type: str,
        content: bytes,
        producer: ArtifactProducer,
        work_item_ref: ArtifactWorkItemReference | None = None,
        source_refs: tuple[ResourceReference, ...] = (),
    ) -> Artifact:
        """Persist bytes and create immutable Artifact metadata."""

        write_result = await self._artifact_storage.write_bytes(
            Path(execution_ref.id.hex, name),
            content,
        )
        artifact = Artifact.new(
            name=name,
            spec=ArtifactSpec(
                executionRef=execution_ref,
                workItemRef=work_item_ref,
                type=artifact_type,
                mediaType=media_type,
                storage=write_result.storage,
                sha256=write_result.sha256,
                sizeBytes=write_result.size_bytes,
                producer=producer,
                sourceRefs=source_refs,
            ),
        )
        return await self._artifact_repository.create(artifact)

    async def verify_artifact(
        self,
        artifact: Artifact,
        *,
        expected_resource_version: int,
    ) -> Artifact:
        """Verify Artifact storage and persist integrity status."""

        integrity = await self._artifact_storage.verify(artifact)
        status = artifact_status_from_integrity(artifact, integrity)
        return await self._artifact_repository.update_status(
            artifact.metadata.id,
            status,
            expected_resource_version=expected_resource_version,
        )
