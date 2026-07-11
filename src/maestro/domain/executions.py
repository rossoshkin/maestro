"""Execution aggregate resource models and transition rules."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal, Protocol, Self
from uuid import UUID

from pydantic import Field, field_validator, model_validator

from maestro.domain.exceptions import (
    ResourceImmutableFieldError,
    ResourceTransitionError,
)
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


class ExecutionPhase(StrEnum):
    """Execution status phases."""

    DRAFT = "Draft"
    PLANNING = "Planning"
    WAITING_FOR_USER_INPUT = "WaitingForUserInput"
    WAITING_FOR_PLAN_APPROVAL = "WaitingForPlanApproval"
    PREPARING_WORKSPACE = "PreparingWorkspace"
    EXECUTING = "Executing"
    VERIFYING = "Verifying"
    REVIEWING = "Reviewing"
    WAITING_FOR_FINAL_APPROVAL = "WaitingForFinalApproval"
    COMPLETED = "Completed"
    FAILED = "Failed"
    CANCELLED = "Cancelled"
    ARCHIVED = "Archived"


TERMINAL_EXECUTION_PHASES = frozenset(
    {
        ExecutionPhase.COMPLETED,
        ExecutionPhase.FAILED,
        ExecutionPhase.CANCELLED,
        ExecutionPhase.ARCHIVED,
    }
)

VALID_EXECUTION_TRANSITIONS = frozenset(
    {
        (ExecutionPhase.DRAFT, ExecutionPhase.PLANNING),
        (ExecutionPhase.PLANNING, ExecutionPhase.WAITING_FOR_USER_INPUT),
        (ExecutionPhase.PLANNING, ExecutionPhase.WAITING_FOR_PLAN_APPROVAL),
        (ExecutionPhase.PLANNING, ExecutionPhase.FAILED),
        (ExecutionPhase.WAITING_FOR_USER_INPUT, ExecutionPhase.PLANNING),
        (ExecutionPhase.WAITING_FOR_USER_INPUT, ExecutionPhase.CANCELLED),
        (ExecutionPhase.WAITING_FOR_PLAN_APPROVAL, ExecutionPhase.PLANNING),
        (
            ExecutionPhase.WAITING_FOR_PLAN_APPROVAL,
            ExecutionPhase.PREPARING_WORKSPACE,
        ),
        (ExecutionPhase.WAITING_FOR_PLAN_APPROVAL, ExecutionPhase.CANCELLED),
        (ExecutionPhase.PREPARING_WORKSPACE, ExecutionPhase.EXECUTING),
        (ExecutionPhase.PREPARING_WORKSPACE, ExecutionPhase.FAILED),
        (ExecutionPhase.EXECUTING, ExecutionPhase.VERIFYING),
        (ExecutionPhase.EXECUTING, ExecutionPhase.FAILED),
        (ExecutionPhase.EXECUTING, ExecutionPhase.CANCELLED),
        (ExecutionPhase.VERIFYING, ExecutionPhase.REVIEWING),
        (ExecutionPhase.VERIFYING, ExecutionPhase.EXECUTING),
        (ExecutionPhase.VERIFYING, ExecutionPhase.FAILED),
        (ExecutionPhase.REVIEWING, ExecutionPhase.WAITING_FOR_FINAL_APPROVAL),
        (ExecutionPhase.REVIEWING, ExecutionPhase.EXECUTING),
        (ExecutionPhase.REVIEWING, ExecutionPhase.FAILED),
        (ExecutionPhase.WAITING_FOR_FINAL_APPROVAL, ExecutionPhase.COMPLETED),
        (ExecutionPhase.WAITING_FOR_FINAL_APPROVAL, ExecutionPhase.EXECUTING),
        (ExecutionPhase.WAITING_FOR_FINAL_APPROVAL, ExecutionPhase.CANCELLED),
        (ExecutionPhase.COMPLETED, ExecutionPhase.ARCHIVED),
        (ExecutionPhase.FAILED, ExecutionPhase.ARCHIVED),
        (ExecutionPhase.CANCELLED, ExecutionPhase.ARCHIVED),
    }
)


class ProjectReference(MaestroModel):
    """Reference to the owning Project."""

    kind: Literal["Project"] = "Project"
    id: UUID
    name: ResourceName | None = None


class ExecutionWorkflowReference(MaestroModel):
    """Workflow version pinned by an Execution."""

    kind: Literal["Workflow"] = "Workflow"
    name: ResourceName
    version: ReferenceVersion


class PolicyReference(MaestroModel):
    """Policy resource reference used by an Execution."""

    kind: Literal["Policy"] = "Policy"
    name: ResourceName


class Goal(MaestroModel):
    """Human-owned statement of intended Execution outcome."""

    summary: str = Field(min_length=1)
    description: str = ""
    constraints: tuple[str, ...] = Field(default_factory=tuple)
    acceptance_criteria: tuple[str, ...] = Field(
        default_factory=tuple,
        alias="acceptanceCriteria",
    )


class ExecutionLimits(MaestroModel):
    """Bounded Execution limits."""

    max_coding_iterations: int = Field(default=2, ge=1, alias="maxCodingIterations")
    max_review_iterations: int = Field(default=2, ge=1, alias="maxReviewIterations")
    max_duration_seconds: int = Field(default=3600, ge=1, alias="maxDurationSeconds")
    max_tool_calls_per_invocation: int = Field(
        default=40,
        ge=1,
        alias="maxToolCallsPerInvocation",
    )


class ExecutionSpec(Spec):
    """Desired state for one orchestration run."""

    project_ref: ProjectReference = Field(alias="projectRef")
    goal: Goal
    workflow_ref: ExecutionWorkflowReference = Field(alias="workflowRef")
    policy_ref: PolicyReference | None = Field(default=None, alias="policyRef")
    requested_roles: tuple[ResourceName, ...] = Field(
        default_factory=tuple,
        alias="requestedRoles",
    )
    limits: ExecutionLimits = Field(default_factory=ExecutionLimits)
    suspended: bool = False
    cancellation_requested: bool = Field(default=False, alias="cancellationRequested")

    @field_validator("requested_roles")
    @classmethod
    def reject_duplicate_requested_roles(
        cls,
        value: tuple[ResourceName, ...],
    ) -> tuple[ResourceName, ...]:
        """Reject duplicate requested Role names."""

        if len(set(value)) != len(value):
            raise ValueError("requestedRoles must be unique")
        return value


class ExecutionIterationStatus(MaestroModel):
    """Iteration counters for bounded Execution loops."""

    coding: int = Field(default=0, ge=0)
    review: int = Field(default=0, ge=0)


class ExecutionStatus(Status):
    """Observed state for an Execution."""

    phase: ExecutionPhase = ExecutionPhase.DRAFT
    current_step: str | None = Field(default=None, alias="currentStep")
    approved_plan_ref: ResourceReference | None = Field(
        default=None,
        alias="approvedPlanRef",
    )
    active_work_item_refs: tuple[ResourceReference, ...] = Field(
        default_factory=tuple,
        alias="activeWorkItemRefs",
    )
    workspace_refs: tuple[ResourceReference, ...] = Field(
        default_factory=tuple,
        alias="workspaceRefs",
    )
    artifact_refs: tuple[ResourceReference, ...] = Field(
        default_factory=tuple,
        alias="artifactRefs",
    )
    iteration: ExecutionIterationStatus = Field(
        default_factory=ExecutionIterationStatus
    )
    started_at: datetime | None = Field(default=None, alias="startedAt")
    completed_at: datetime | None = Field(default=None, alias="completedAt")


class Execution(BaseResource[ExecutionSpec, ExecutionStatus]):
    """Primary aggregate root for a Maestro orchestration run."""

    kind: Literal["Execution"] = "Execution"

    @model_validator(mode="after")
    def validate_owner_reference(self) -> Self:
        """Require exactly one matching Project controller owner reference."""

        project_owners = tuple(
            owner
            for owner in self.metadata.owner_references
            if owner.kind == "Project" and owner.controller
        )
        if len(project_owners) != 1:
            raise ValueError("Execution must have exactly one Project controller owner")

        project_owner = project_owners[0]
        if project_owner.id != self.spec.project_ref.id:
            raise ValueError("Execution Project owner must match spec.projectRef")

        return self

    @classmethod
    def new(
        cls,
        *,
        name: ResourceName,
        spec: ExecutionSpec,
        created_by: str = "local-user",
        namespace: ResourceName = "default",
    ) -> Self:
        """Create a new Execution resource with Project ownership metadata."""

        return cls(
            metadata=Metadata(
                name=name,
                namespace=namespace,
                createdBy=created_by,
                ownerReferences=(
                    OwnerReference(
                        kind="Project",
                        id=spec.project_ref.id,
                        name=spec.project_ref.name,
                        controller=True,
                        blockOwnerDeletion=True,
                    ),
                ),
            ),
            spec=spec,
            status=ExecutionStatus(),
        )


class ExecutionRepository(
    ResourceRepository[Execution, ExecutionSpec, ExecutionStatus],
    Protocol,
):
    """Persistence contract for Execution resources."""

    async def list_by_project(self, project_id: UUID) -> tuple[Execution, ...]:
        """List Executions belonging to one Project."""


def validate_execution_transition(
    resource_id: UUID,
    current_phase: ExecutionPhase,
    next_phase: ExecutionPhase,
) -> None:
    """Reject illegal Execution phase transitions."""

    if current_phase == next_phase:
        return

    if (current_phase, next_phase) not in VALID_EXECUTION_TRANSITIONS:
        raise ResourceTransitionError(resource_id, current_phase, next_phase)


def apply_execution_spec_update(
    execution: Execution,
    spec: ExecutionSpec,
    *,
    expected_resource_version: int,
) -> Execution:
    """Apply Execution spec updates while preserving Goal immutability."""

    goal_changed_after_draft = (
        execution.status.phase != ExecutionPhase.DRAFT
        and spec.goal != execution.spec.goal
    )
    if goal_changed_after_draft:
        raise ResourceImmutableFieldError(execution.metadata.id, "spec.goal")

    return apply_spec_update(
        execution,
        spec,
        expected_resource_version=expected_resource_version,
    )


def apply_execution_status_update(
    execution: Execution,
    status: ExecutionStatus,
    *,
    expected_resource_version: int,
) -> Execution:
    """Apply Execution status updates with phase transition validation."""

    validate_execution_transition(
        execution.metadata.id,
        execution.status.phase,
        status.phase,
    )
    return apply_status_update(
        execution,
        status,
        expected_resource_version=expected_resource_version,
    )
