"""RoleInvocation resources and lifecycle validation."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal, Protocol, Self
from uuid import UUID

from pydantic import Field, model_validator

from maestro.domain.capabilities import CapabilityName
from maestro.domain.modeling import ModelIdentifier
from maestro.domain.projects import ReferenceVersion
from maestro.domain.repositories import (
    ResourceRepository,
    apply_spec_update,
    apply_status_update,
)
from maestro.domain.resources import (
    BaseResource,
    MaestroModel,
    Metadata,
    OwnerReference,
    ResourceName,
    ResourceReference,
    Spec,
    Status,
)


class RoleInvocationPhase(StrEnum):
    """RoleInvocation lifecycle phases."""

    PENDING = "Pending"
    ASSIGNED = "Assigned"
    RUNNING = "Running"
    WAITING_FOR_TOOL = "WaitingForTool"
    SUCCEEDED = "Succeeded"
    FAILED = "Failed"
    CANCELLED = "Cancelled"
    TIMED_OUT = "TimedOut"


TERMINAL_ROLE_INVOCATION_PHASES = frozenset(
    {
        RoleInvocationPhase.SUCCEEDED,
        RoleInvocationPhase.FAILED,
        RoleInvocationPhase.CANCELLED,
        RoleInvocationPhase.TIMED_OUT,
    }
)

VALID_ROLE_INVOCATION_TRANSITIONS = frozenset(
    {
        (RoleInvocationPhase.PENDING, RoleInvocationPhase.ASSIGNED),
        (RoleInvocationPhase.PENDING, RoleInvocationPhase.RUNNING),
        (RoleInvocationPhase.PENDING, RoleInvocationPhase.FAILED),
        (RoleInvocationPhase.ASSIGNED, RoleInvocationPhase.RUNNING),
        (RoleInvocationPhase.ASSIGNED, RoleInvocationPhase.CANCELLED),
        (RoleInvocationPhase.RUNNING, RoleInvocationPhase.WAITING_FOR_TOOL),
        (RoleInvocationPhase.RUNNING, RoleInvocationPhase.SUCCEEDED),
        (RoleInvocationPhase.RUNNING, RoleInvocationPhase.FAILED),
        (RoleInvocationPhase.RUNNING, RoleInvocationPhase.CANCELLED),
        (RoleInvocationPhase.RUNNING, RoleInvocationPhase.TIMED_OUT),
        (RoleInvocationPhase.WAITING_FOR_TOOL, RoleInvocationPhase.RUNNING),
        (RoleInvocationPhase.WAITING_FOR_TOOL, RoleInvocationPhase.FAILED),
        (RoleInvocationPhase.WAITING_FOR_TOOL, RoleInvocationPhase.CANCELLED),
        (RoleInvocationPhase.WAITING_FOR_TOOL, RoleInvocationPhase.TIMED_OUT),
    }
)


class RoleInvocationExecutionReference(MaestroModel):
    """Reference to the owning Execution."""

    kind: Literal["Execution"] = "Execution"
    id: UUID
    name: ResourceName | None = None


class RoleInvocationWorkItemReference(MaestroModel):
    """Optional WorkItem reference for WorkItem-backed Role invocations."""

    kind: Literal["WorkItem"] = "WorkItem"
    id: UUID
    name: ResourceName | None = None


class RoleInvocationRoleReference(MaestroModel):
    """Role version fulfilled by this invocation."""

    name: ResourceName
    version: ReferenceVersion


class RoleInvocationAgentReference(MaestroModel):
    """Agent assigned to this invocation."""

    kind: Literal["Agent"] = "Agent"
    id: UUID
    name: ResourceName | None = None


class RoleInvocationProviderReference(MaestroModel):
    """Provider used by this invocation."""

    kind: Literal["Provider"] = "Provider"
    id: UUID | None = None
    name: ResourceName


class RoleInvocationLimits(MaestroModel):
    """Bounded limits copied from the Role execution policy."""

    max_steps: int = Field(ge=1, alias="maxSteps")
    max_duration_seconds: int = Field(ge=1, alias="maxDurationSeconds")


class RoleInvocationFailure(MaestroModel):
    """Failure details recorded for unsuccessful invocations."""

    reason: str = Field(min_length=1)
    message: str = ""


class RoleInvocationSpec(Spec):
    """Immutable input for one Role invocation."""

    execution_ref: RoleInvocationExecutionReference = Field(alias="executionRef")
    work_item_ref: RoleInvocationWorkItemReference | None = Field(
        default=None,
        alias="workItemRef",
    )
    role_ref: RoleInvocationRoleReference = Field(alias="roleRef")
    agent_ref: RoleInvocationAgentReference = Field(alias="agentRef")
    input_artifact_refs: tuple[ResourceReference, ...] = Field(
        default_factory=tuple,
        alias="inputArtifactRefs",
    )
    granted_capabilities: tuple[CapabilityName, ...] = Field(
        default_factory=tuple,
        alias="grantedCapabilities",
    )
    limits: RoleInvocationLimits


class RoleInvocationStatus(Status):
    """Observed Role invocation state."""

    phase: RoleInvocationPhase = RoleInvocationPhase.PENDING
    provider_ref: RoleInvocationProviderReference | None = Field(
        default=None,
        alias="providerRef",
    )
    model: ModelIdentifier | None = None
    prompt_artifact_ref: ResourceReference | None = Field(
        default=None,
        alias="promptArtifactRef",
    )
    response_artifact_ref: ResourceReference | None = Field(
        default=None,
        alias="responseArtifactRef",
    )
    output_artifact_refs: tuple[ResourceReference, ...] = Field(
        default_factory=tuple,
        alias="outputArtifactRefs",
    )
    tool_call_count: int = Field(default=0, ge=0, alias="toolCallCount")
    started_at: datetime | None = Field(default=None, alias="startedAt")
    completed_at: datetime | None = Field(default=None, alias="completedAt")
    failure: RoleInvocationFailure | None = None

    @model_validator(mode="after")
    def validate_phase_metadata(self) -> Self:
        """Validate terminal metadata and timestamp ordering."""

        if (
            self.started_at is not None
            and self.completed_at is not None
            and self.completed_at < self.started_at
        ):
            raise ValueError("completedAt must not be earlier than startedAt")

        if self.phase == RoleInvocationPhase.SUCCEEDED:
            if self.completed_at is None:
                raise ValueError("Succeeded RoleInvocations require completedAt")
            if self.provider_ref is None or self.model is None:
                raise ValueError(
                    "Succeeded RoleInvocations require providerRef and model"
                )

        if self.phase == RoleInvocationPhase.FAILED and self.failure is None:
            raise ValueError("Failed RoleInvocations require failure")

        return self


class RoleInvocation(BaseResource[RoleInvocationSpec, RoleInvocationStatus]):
    """Durable record of one Agent attempt to fulfill one Role."""

    kind: Literal["RoleInvocation"] = "RoleInvocation"

    @model_validator(mode="after")
    def validate_role_invocation_metadata(self) -> Self:
        """Require matching Execution ownership."""

        execution_owners = tuple(
            owner
            for owner in self.metadata.owner_references
            if owner.kind == "Execution" and owner.controller
        )
        if len(execution_owners) != 1:
            raise ValueError(
                "RoleInvocation must have exactly one Execution controller owner"
            )

        execution_owner = execution_owners[0]
        if execution_owner.id != self.spec.execution_ref.id:
            raise ValueError(
                "RoleInvocation Execution owner must match spec.executionRef"
            )

        return self

    @classmethod
    def new(
        cls,
        *,
        name: ResourceName,
        spec: RoleInvocationSpec,
        created_by: str = "local-user",
        namespace: ResourceName = "default",
    ) -> Self:
        """Create a new RoleInvocation resource."""

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
            status=RoleInvocationStatus(),
        )


class RoleInvocationRepository(
    ResourceRepository[
        RoleInvocation,
        RoleInvocationSpec,
        RoleInvocationStatus,
    ],
    Protocol,
):
    """Persistence contract for RoleInvocation resources."""

    async def list_by_execution(
        self,
        execution_id: UUID,
    ) -> tuple[RoleInvocation, ...]:
        """List RoleInvocations belonging to one Execution."""

    async def list_by_work_item(
        self,
        work_item_id: UUID,
    ) -> tuple[RoleInvocation, ...]:
        """List RoleInvocations associated with one WorkItem."""


def validate_role_invocation_transition(
    resource_id: UUID,
    current_phase: RoleInvocationPhase,
    next_phase: RoleInvocationPhase,
) -> None:
    """Reject illegal RoleInvocation phase transitions."""

    from maestro.domain.exceptions import ResourceTransitionError

    if current_phase == next_phase:
        return
    if current_phase in TERMINAL_ROLE_INVOCATION_PHASES:
        raise ResourceTransitionError(resource_id, current_phase, next_phase)
    if (current_phase, next_phase) not in VALID_ROLE_INVOCATION_TRANSITIONS:
        raise ResourceTransitionError(resource_id, current_phase, next_phase)


def apply_role_invocation_spec_update(
    invocation: RoleInvocation,
    spec: RoleInvocationSpec,
    *,
    expected_resource_version: int,
) -> RoleInvocation:
    """Reject actual RoleInvocation spec changes after creation."""

    from maestro.domain.exceptions import ResourceImmutableFieldError

    if spec != invocation.spec:
        raise ResourceImmutableFieldError(invocation.metadata.id, "spec")
    return apply_spec_update(
        invocation,
        spec,
        expected_resource_version=expected_resource_version,
    )


def apply_role_invocation_status_update(
    invocation: RoleInvocation,
    status: RoleInvocationStatus,
    *,
    expected_resource_version: int,
) -> RoleInvocation:
    """Apply RoleInvocation status updates with transition validation."""

    validate_role_invocation_transition(
        invocation.metadata.id,
        invocation.status.phase,
        status.phase,
    )
    return apply_status_update(
        invocation,
        status,
        expected_resource_version=expected_resource_version,
    )
