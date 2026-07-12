"""Artifact resources, integrity metadata and storage contracts."""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from typing import Annotated, Literal, Protocol, Self
from uuid import UUID

from pydantic import Field, field_validator, model_validator

from maestro.domain.exceptions import (
    ResourceImmutableFieldError,
    ResourceTransitionError,
)
from maestro.domain.repositories import (
    ResourceRepository,
    apply_spec_update,
    apply_status_update,
)
from maestro.domain.resources import (
    BaseResource,
    Condition,
    ConditionStatus,
    MaestroModel,
    Metadata,
    OwnerReference,
    ResourceName,
    ResourceReference,
    Spec,
    Status,
)

Checksum = Annotated[str, Field(min_length=64, max_length=64)]
MediaType = Annotated[str, Field(min_length=1)]
StorageUri = Annotated[str, Field(min_length=1)]
ProducerName = Annotated[str, Field(min_length=1, max_length=128)]


class ArtifactType(StrEnum):
    """Known Artifact kinds produced by Maestro."""

    GOAL = "goal"
    PLAN = "plan"
    PROMPT = "prompt"
    MODEL_RESPONSE = "model-response"
    TOOL_LOG = "tool-log"
    COMMAND_OUTPUT = "command-output"
    VERIFICATION_REPORT = "verification-report"
    TEST_REPORT = "test-report"
    GIT_DIFF = "git-diff"
    PATCH = "patch"
    REVIEW = "review"
    SUMMARY = "summary"
    KNOWLEDGE_RESULT = "knowledge-result"


class ArtifactPhase(StrEnum):
    """Artifact integrity phases."""

    PENDING = "Pending"
    AVAILABLE = "Available"
    CORRUPT = "Corrupt"
    MISSING = "Missing"
    ARCHIVED = "Archived"


VALID_ARTIFACT_TRANSITIONS = frozenset(
    {
        (ArtifactPhase.PENDING, ArtifactPhase.AVAILABLE),
        (ArtifactPhase.PENDING, ArtifactPhase.CORRUPT),
        (ArtifactPhase.PENDING, ArtifactPhase.MISSING),
        (ArtifactPhase.PENDING, ArtifactPhase.ARCHIVED),
        (ArtifactPhase.AVAILABLE, ArtifactPhase.CORRUPT),
        (ArtifactPhase.AVAILABLE, ArtifactPhase.MISSING),
        (ArtifactPhase.AVAILABLE, ArtifactPhase.ARCHIVED),
        (ArtifactPhase.CORRUPT, ArtifactPhase.AVAILABLE),
        (ArtifactPhase.CORRUPT, ArtifactPhase.MISSING),
        (ArtifactPhase.CORRUPT, ArtifactPhase.ARCHIVED),
        (ArtifactPhase.MISSING, ArtifactPhase.AVAILABLE),
        (ArtifactPhase.MISSING, ArtifactPhase.CORRUPT),
        (ArtifactPhase.MISSING, ArtifactPhase.ARCHIVED),
    }
)


class ArtifactExecutionReference(MaestroModel):
    """Reference to the owning Execution."""

    kind: Literal["Execution"] = "Execution"
    id: UUID
    name: ResourceName | None = None


class ArtifactWorkItemReference(MaestroModel):
    """Optional WorkItem provenance reference."""

    kind: Literal["WorkItem"] = "WorkItem"
    id: UUID
    name: ResourceName | None = None


class ArtifactRoleInvocationReference(MaestroModel):
    """Optional RoleInvocation provenance reference."""

    kind: Literal["RoleInvocation"] = "RoleInvocation"
    id: UUID
    name: ResourceName | None = None


class ArtifactStorageMetadata(MaestroModel):
    """Immutable Artifact storage descriptor."""

    uri: StorageUri


class ArtifactProducer(MaestroModel):
    """Provenance for the subsystem or RoleInvocation that produced an Artifact."""

    subsystem: ProducerName
    role_invocation_ref: ArtifactRoleInvocationReference | None = Field(
        default=None,
        alias="roleInvocationRef",
    )


class ArtifactSpec(Spec):
    """Immutable Artifact metadata."""

    execution_ref: ArtifactExecutionReference = Field(alias="executionRef")
    work_item_ref: ArtifactWorkItemReference | None = Field(
        default=None,
        alias="workItemRef",
    )
    artifact_type: ArtifactType = Field(alias="type")
    media_type: MediaType = Field(alias="mediaType")
    storage: ArtifactStorageMetadata
    sha256: Checksum
    size_bytes: int = Field(ge=0, alias="sizeBytes")
    producer: ArtifactProducer
    source_refs: tuple[ResourceReference, ...] = Field(
        default_factory=tuple,
        alias="sourceRefs",
    )

    @field_validator("sha256")
    @classmethod
    def normalize_sha256(cls, value: Checksum) -> Checksum:
        """Require a hexadecimal SHA-256 checksum and normalize to lowercase."""

        normalized = value.lower()
        if normalized != value or any(
            character not in "0123456789abcdef" for character in value
        ):
            raise ValueError("sha256 must be lowercase hexadecimal")
        return normalized

    @field_validator("source_refs")
    @classmethod
    def reject_duplicate_source_refs(
        cls,
        value: tuple[ResourceReference, ...],
    ) -> tuple[ResourceReference, ...]:
        """Reject duplicate source references."""

        keys = [(resource.kind, resource.id) for resource in value]
        if len(set(keys)) != len(keys):
            raise ValueError("sourceRefs must be unique by kind and id")
        return value


class ArtifactStatus(Status):
    """Observed Artifact integrity state."""

    phase: ArtifactPhase = ArtifactPhase.PENDING
    verified_sha256: Checksum | None = Field(default=None, alias="verifiedSha256")

    @model_validator(mode="after")
    def validate_phase_metadata(self) -> Self:
        """Ensure verified phases carry checksum evidence."""

        if self.phase == ArtifactPhase.AVAILABLE and self.verified_sha256 is None:
            raise ValueError("Available Artifacts require verifiedSha256")
        return self


class Artifact(BaseResource[ArtifactSpec, ArtifactStatus]):
    """Immutable durable output produced during an Execution."""

    kind: Literal["Artifact"] = "Artifact"

    @model_validator(mode="after")
    def validate_artifact_metadata(self) -> Self:
        """Require matching Execution ownership and consistent integrity status."""

        execution_owners = tuple(
            owner
            for owner in self.metadata.owner_references
            if owner.kind == "Execution" and owner.controller
        )
        if len(execution_owners) != 1:
            raise ValueError(
                "Artifact must have exactly one Execution controller owner"
            )

        execution_owner = execution_owners[0]
        if execution_owner.id != self.spec.execution_ref.id:
            raise ValueError("Artifact Execution owner must match spec.executionRef")

        if (
            self.status.phase == ArtifactPhase.AVAILABLE
            and self.status.verified_sha256 != self.spec.sha256
        ):
            raise ValueError("Available Artifact checksum must match spec.sha256")

        return self

    @classmethod
    def new(
        cls,
        *,
        name: ResourceName,
        spec: ArtifactSpec,
        created_by: str = "local-user",
        namespace: ResourceName = "default",
    ) -> Self:
        """Create a new Artifact resource with Execution ownership metadata."""

        return cls(
            metadata=Metadata(
                name=name,
                namespace=namespace,
                createdBy=created_by,
                ownerReferences=(
                    OwnerReference(
                        kind="Execution",
                        id=spec.execution_ref.id,
                        name=spec.execution_ref.name,
                        controller=True,
                        blockOwnerDeletion=True,
                    ),
                ),
            ),
            spec=spec,
            status=ArtifactStatus(),
        )


class ArtifactIntegrityResult(MaestroModel):
    """Observed bytes and checksum for an Artifact storage object."""

    exists: bool
    sha256: Checksum | None = None
    size_bytes: int = Field(default=0, ge=0, alias="sizeBytes")


class ArtifactStorageWriteResult(MaestroModel):
    """Result of writing content to Artifact storage."""

    storage: ArtifactStorageMetadata
    sha256: Checksum
    size_bytes: int = Field(ge=0, alias="sizeBytes")


class ArtifactStorageError(Exception):
    """Raised by Artifact storage adapters with user-safe diagnostics."""


class ArtifactStorage(Protocol):
    """Storage adapter contract for Artifact bytes."""

    async def write_bytes(
        self,
        relative_path: Path,
        content: bytes,
    ) -> ArtifactStorageWriteResult:
        """Persist Artifact bytes and return immutable metadata."""

    async def read_bytes(self, artifact: Artifact) -> bytes:
        """Read Artifact bytes."""

    async def verify(self, artifact: Artifact) -> ArtifactIntegrityResult:
        """Verify Artifact bytes against stored metadata."""


class ArtifactRepository(
    ResourceRepository[Artifact, ArtifactSpec, ArtifactStatus],
    Protocol,
):
    """Persistence contract for Artifact resources."""

    async def list_by_execution(self, execution_id: UUID) -> tuple[Artifact, ...]:
        """List Artifacts belonging to one Execution."""

    async def list_by_work_item(self, work_item_id: UUID) -> tuple[Artifact, ...]:
        """List Artifacts belonging to one WorkItem."""


def validate_artifact_transition(
    resource_id: UUID,
    current_phase: ArtifactPhase,
    next_phase: ArtifactPhase,
) -> None:
    """Reject illegal Artifact phase transitions."""

    if current_phase == next_phase:
        return

    if (current_phase, next_phase) not in VALID_ARTIFACT_TRANSITIONS:
        raise ResourceTransitionError(resource_id, current_phase, next_phase)


def artifact_status_from_integrity(
    artifact: Artifact,
    integrity: ArtifactIntegrityResult,
) -> ArtifactStatus:
    """Build an Artifact status from storage integrity evidence."""

    if not integrity.exists:
        return ArtifactStatus(
            observedGeneration=artifact.metadata.generation,
            phase=ArtifactPhase.MISSING,
            conditions=(
                Condition(
                    type="IntegrityVerified",
                    status=ConditionStatus.FALSE,
                    reason="ArtifactMissing",
                    message="Artifact content is missing from storage",
                    observedGeneration=artifact.metadata.generation,
                ),
            ),
        )

    checksum_matches = (
        integrity.sha256 == artifact.spec.sha256
        and integrity.size_bytes == artifact.spec.size_bytes
    )
    return ArtifactStatus(
        observedGeneration=artifact.metadata.generation,
        phase=ArtifactPhase.AVAILABLE if checksum_matches else ArtifactPhase.CORRUPT,
        verifiedSha256=integrity.sha256,
        conditions=(
            Condition(
                type="IntegrityVerified",
                status=(
                    ConditionStatus.TRUE if checksum_matches else ConditionStatus.FALSE
                ),
                reason="ChecksumMatched" if checksum_matches else "ChecksumMismatched",
                message="" if checksum_matches else "Artifact checksum mismatch",
                observedGeneration=artifact.metadata.generation,
            ),
        ),
    )


def apply_artifact_spec_update(
    artifact: Artifact,
    spec: ArtifactSpec,
    *,
    expected_resource_version: int,
) -> Artifact:
    """Reject Artifact spec changes because Artifacts are immutable."""

    if spec != artifact.spec:
        raise ResourceImmutableFieldError(artifact.metadata.id, "spec")

    return apply_spec_update(
        artifact,
        spec,
        expected_resource_version=expected_resource_version,
    )


def apply_artifact_status_update(
    artifact: Artifact,
    status: ArtifactStatus,
    *,
    expected_resource_version: int,
) -> Artifact:
    """Apply Artifact status updates with phase transition validation."""

    validate_artifact_transition(
        artifact.metadata.id, artifact.status.phase, status.phase
    )
    return apply_status_update(
        artifact,
        status,
        expected_resource_version=expected_resource_version,
    )
