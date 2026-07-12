"""Tests for Artifact resources and integrity rules."""

import hashlib
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from maestro.domain.artifacts import (
    Artifact,
    ArtifactExecutionReference,
    ArtifactIntegrityResult,
    ArtifactPhase,
    ArtifactProducer,
    ArtifactSpec,
    ArtifactStatus,
    ArtifactStorageMetadata,
    ArtifactType,
    ArtifactWorkItemReference,
    apply_artifact_spec_update,
    artifact_status_from_integrity,
)
from maestro.domain.exceptions import ResourceImmutableFieldError
from maestro.domain.resources import Metadata, OwnerReference


def checksum(content: bytes = b"diff\n") -> str:
    """Return a SHA-256 checksum for test content."""

    return hashlib.sha256(content).hexdigest()


def valid_artifact_spec(
    execution_id: UUID | None = None,
    *,
    work_item_id: UUID | None = None,
    sha256: str | None = None,
) -> ArtifactSpec:
    """Build a valid ArtifactSpec for tests."""

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
        sha256=sha256 or checksum(),
        sizeBytes=len(b"diff\n"),
        producer=ArtifactProducer(subsystem="workspace-controller"),
    )


def valid_artifact(execution_id: UUID | None = None) -> Artifact:
    """Build a valid Artifact resource."""

    return Artifact.new(
        name="execution-git-diff", spec=valid_artifact_spec(execution_id)
    )


def test_artifact_serializes_and_deserializes() -> None:
    artifact = valid_artifact()

    payload = artifact.model_dump(mode="json", by_alias=True)
    round_tripped = Artifact.model_validate(payload)

    assert payload["kind"] == "Artifact"
    assert payload["spec"]["type"] == "git-diff"
    assert payload["spec"]["producer"]["subsystem"] == "workspace-controller"
    assert round_tripped == artifact


def test_artifact_requires_matching_execution_owner() -> None:
    spec = valid_artifact_spec()

    with pytest.raises(ValidationError):
        Artifact(
            metadata=Metadata(
                name="execution-git-diff",
                ownerReferences=(
                    OwnerReference(kind="Execution", id=uuid4(), controller=True),
                ),
            ),
            spec=spec,
            status=ArtifactStatus(),
        )


def test_artifact_requires_provenance() -> None:
    with pytest.raises(ValidationError):
        ArtifactProducer(subsystem="")


def test_sha256_must_be_lowercase_hex() -> None:
    with pytest.raises(ValidationError):
        valid_artifact_spec(sha256="A" * 64)

    with pytest.raises(ValidationError):
        valid_artifact_spec(sha256="z" * 64)


def test_available_artifact_requires_matching_checksum() -> None:
    spec = valid_artifact_spec()

    with pytest.raises(ValidationError):
        Artifact(
            metadata=Metadata(
                name="execution-git-diff",
                ownerReferences=(
                    OwnerReference(
                        kind="Execution",
                        id=spec.execution_ref.id,
                        controller=True,
                    ),
                ),
            ),
            spec=spec,
            status=ArtifactStatus(
                phase=ArtifactPhase.AVAILABLE,
                verifiedSha256=checksum(b"other\n"),
            ),
        )


def test_artifact_spec_updates_are_rejected() -> None:
    artifact = valid_artifact()
    changed_spec = artifact.spec.model_copy(update={"media_type": "text/plain"})

    with pytest.raises(ResourceImmutableFieldError):
        apply_artifact_spec_update(
            artifact,
            changed_spec,
            expected_resource_version=artifact.metadata.resource_version,
        )


def test_artifact_status_from_integrity_detects_available_corrupt_and_missing() -> None:
    artifact = valid_artifact()

    available = artifact_status_from_integrity(
        artifact,
        ArtifactIntegrityResult(
            exists=True,
            sha256=artifact.spec.sha256,
            sizeBytes=artifact.spec.size_bytes,
        ),
    )
    corrupt = artifact_status_from_integrity(
        artifact,
        ArtifactIntegrityResult(exists=True, sha256=checksum(b"other"), sizeBytes=5),
    )
    missing = artifact_status_from_integrity(
        artifact,
        ArtifactIntegrityResult(exists=False),
    )

    assert available.phase == ArtifactPhase.AVAILABLE
    assert available.verified_sha256 == artifact.spec.sha256
    assert corrupt.phase == ArtifactPhase.CORRUPT
    assert missing.phase == ArtifactPhase.MISSING
