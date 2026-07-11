"""WorkItem resource models, readiness and transition rules."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal, Protocol, Self
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

PlanWorkItemId = ResourceName
NonEmptyText = Annotated[str, Field(min_length=1)]
CapabilityName = Annotated[
    str,
    Field(min_length=1, max_length=128, pattern=r"^[a-z0-9][a-z0-9.\-]*$"),
]


class WorkItemPhase(StrEnum):
    """WorkItem status phases."""

    PENDING = "Pending"
    BLOCKED = "Blocked"
    READY = "Ready"
    SCHEDULED = "Scheduled"
    RUNNING = "Running"
    WAITING_FOR_TOOL = "WaitingForTool"
    WAITING_FOR_APPROVAL = "WaitingForApproval"
    VERIFYING = "Verifying"
    REVIEWING = "Reviewing"
    SUCCEEDED = "Succeeded"
    FAILED = "Failed"
    CANCELLED = "Cancelled"


TERMINAL_WORK_ITEM_PHASES = frozenset(
    {
        WorkItemPhase.SUCCEEDED,
        WorkItemPhase.CANCELLED,
    }
)

VALID_WORK_ITEM_TRANSITIONS = frozenset(
    {
        (WorkItemPhase.PENDING, WorkItemPhase.BLOCKED),
        (WorkItemPhase.PENDING, WorkItemPhase.READY),
        (WorkItemPhase.PENDING, WorkItemPhase.CANCELLED),
        (WorkItemPhase.BLOCKED, WorkItemPhase.PENDING),
        (WorkItemPhase.BLOCKED, WorkItemPhase.READY),
        (WorkItemPhase.BLOCKED, WorkItemPhase.CANCELLED),
        (WorkItemPhase.READY, WorkItemPhase.BLOCKED),
        (WorkItemPhase.READY, WorkItemPhase.SCHEDULED),
        (WorkItemPhase.READY, WorkItemPhase.CANCELLED),
        (WorkItemPhase.SCHEDULED, WorkItemPhase.READY),
        (WorkItemPhase.SCHEDULED, WorkItemPhase.RUNNING),
        (WorkItemPhase.SCHEDULED, WorkItemPhase.CANCELLED),
        (WorkItemPhase.RUNNING, WorkItemPhase.WAITING_FOR_TOOL),
        (WorkItemPhase.RUNNING, WorkItemPhase.WAITING_FOR_APPROVAL),
        (WorkItemPhase.RUNNING, WorkItemPhase.VERIFYING),
        (WorkItemPhase.RUNNING, WorkItemPhase.FAILED),
        (WorkItemPhase.RUNNING, WorkItemPhase.CANCELLED),
        (WorkItemPhase.WAITING_FOR_TOOL, WorkItemPhase.RUNNING),
        (WorkItemPhase.WAITING_FOR_TOOL, WorkItemPhase.FAILED),
        (WorkItemPhase.WAITING_FOR_TOOL, WorkItemPhase.CANCELLED),
        (WorkItemPhase.WAITING_FOR_APPROVAL, WorkItemPhase.RUNNING),
        (WorkItemPhase.WAITING_FOR_APPROVAL, WorkItemPhase.FAILED),
        (WorkItemPhase.WAITING_FOR_APPROVAL, WorkItemPhase.CANCELLED),
        (WorkItemPhase.VERIFYING, WorkItemPhase.RUNNING),
        (WorkItemPhase.VERIFYING, WorkItemPhase.REVIEWING),
        (WorkItemPhase.VERIFYING, WorkItemPhase.SUCCEEDED),
        (WorkItemPhase.VERIFYING, WorkItemPhase.FAILED),
        (WorkItemPhase.VERIFYING, WorkItemPhase.CANCELLED),
        (WorkItemPhase.REVIEWING, WorkItemPhase.RUNNING),
        (WorkItemPhase.REVIEWING, WorkItemPhase.WAITING_FOR_APPROVAL),
        (WorkItemPhase.REVIEWING, WorkItemPhase.SUCCEEDED),
        (WorkItemPhase.REVIEWING, WorkItemPhase.FAILED),
        (WorkItemPhase.REVIEWING, WorkItemPhase.CANCELLED),
        (WorkItemPhase.FAILED, WorkItemPhase.READY),
        (WorkItemPhase.FAILED, WorkItemPhase.CANCELLED),
    }
)

MUTABLE_SPEC_PHASES = frozenset(
    {
        WorkItemPhase.PENDING,
        WorkItemPhase.BLOCKED,
        WorkItemPhase.READY,
    }
)


class WorkItemExecutionReference(MaestroModel):
    """Reference to the owning Execution."""

    kind: Literal["Execution"] = "Execution"
    id: UUID
    name: ResourceName | None = None


class WorkItemPlanReference(MaestroModel):
    """Reference to the Plan revision that produced this WorkItem."""

    kind: Literal["Plan"] = "Plan"
    id: UUID
    name: ResourceName | None = None
    version: int = Field(ge=1)


class WorkItemRoleReference(MaestroModel):
    """Role version assigned to this WorkItem."""

    name: ResourceName
    version: ReferenceVersion


class WorkItemWorkspaceReference(MaestroModel):
    """Workspace reference available to a WorkItem."""

    kind: Literal["Workspace"] = "Workspace"
    id: UUID
    name: ResourceName | None = None


class WorkItemDependencyReference(MaestroModel):
    """Dependency on another WorkItem resource."""

    kind: Literal["WorkItem"] = "WorkItem"
    id: UUID
    name: ResourceName | None = None


class WorkItemAgentReference(MaestroModel):
    """Agent currently assigned to a WorkItem."""

    kind: Literal["Agent"] = "Agent"
    id: UUID
    name: ResourceName | None = None


class WorkItemRetryPolicy(MaestroModel):
    """Finite retry policy for a WorkItem."""

    max_attempts: int = Field(default=1, ge=1, alias="maxAttempts")


class WorkItemVerificationSpec(MaestroModel):
    """Verification commands configured for a WorkItem."""

    commands: tuple[NonEmptyText, ...] = Field(default_factory=tuple)

    @field_validator("commands")
    @classmethod
    def reject_duplicate_commands(
        cls,
        value: tuple[NonEmptyText, ...],
    ) -> tuple[NonEmptyText, ...]:
        """Reject duplicate verification commands."""

        if len(set(value)) != len(value):
            raise ValueError("verification commands must be unique")
        return value


class WorkItemVerificationCommandResult(MaestroModel):
    """Observed result for one verification command."""

    command: NonEmptyText
    exit_code: int = Field(alias="exitCode")
    output_artifact_ref: ResourceReference | None = Field(
        default=None,
        alias="outputArtifactRef",
    )


class WorkItemVerificationStatus(MaestroModel):
    """Observed verification evidence for a WorkItem."""

    command_results: tuple[WorkItemVerificationCommandResult, ...] = Field(
        default_factory=tuple,
        alias="commandResults",
    )
    evidence_refs: tuple[ResourceReference, ...] = Field(
        default_factory=tuple,
        alias="evidenceRefs",
    )

    @field_validator("command_results")
    @classmethod
    def reject_duplicate_command_results(
        cls,
        value: tuple[WorkItemVerificationCommandResult, ...],
    ) -> tuple[WorkItemVerificationCommandResult, ...]:
        """Reject duplicate command results."""

        commands = [result.command for result in value]
        if len(set(commands)) != len(commands):
            raise ValueError("verification command results must be unique by command")
        return value

    def successful_commands(self) -> set[str]:
        """Return commands observed with successful exit codes."""

        return {
            result.command for result in self.command_results if result.exit_code == 0
        }


class WorkItemSpec(Spec):
    """Desired state for one schedulable WorkItem."""

    execution_ref: WorkItemExecutionReference = Field(alias="executionRef")
    plan_ref: WorkItemPlanReference = Field(alias="planRef")
    plan_work_item_id: PlanWorkItemId = Field(alias="planWorkItemId")
    role_ref: WorkItemRoleReference = Field(alias="roleRef")
    repository_ref: ResourceName | None = Field(default=None, alias="repositoryRef")
    workspace_ref: WorkItemWorkspaceReference | None = Field(
        default=None,
        alias="workspaceRef",
    )
    objective: NonEmptyText
    context_refs: tuple[ResourceReference, ...] = Field(
        default_factory=tuple,
        alias="contextRefs",
    )
    constraints: tuple[NonEmptyText, ...] = Field(default_factory=tuple)
    acceptance_criteria: tuple[NonEmptyText, ...] = Field(
        min_length=1,
        alias="acceptanceCriteria",
    )
    verification: WorkItemVerificationSpec = Field(
        default_factory=WorkItemVerificationSpec
    )
    depends_on: tuple[WorkItemDependencyReference, ...] = Field(
        default_factory=tuple,
        alias="dependsOn",
    )
    requested_capabilities: tuple[CapabilityName, ...] = Field(
        default_factory=tuple,
        alias="requestedCapabilities",
    )
    retry_policy: WorkItemRetryPolicy = Field(
        default_factory=WorkItemRetryPolicy,
        alias="retryPolicy",
    )

    @field_validator("depends_on")
    @classmethod
    def reject_duplicate_dependencies(
        cls,
        value: tuple[WorkItemDependencyReference, ...],
    ) -> tuple[WorkItemDependencyReference, ...]:
        """Reject duplicate WorkItem dependency references."""

        dependency_ids = [dependency.id for dependency in value]
        if len(set(dependency_ids)) != len(dependency_ids):
            raise ValueError("WorkItem dependencies must be unique")
        return value

    @field_validator("requested_capabilities")
    @classmethod
    def reject_duplicate_capabilities(
        cls,
        value: tuple[CapabilityName, ...],
    ) -> tuple[CapabilityName, ...]:
        """Reject duplicate requested Capability names."""

        if len(set(value)) != len(value):
            raise ValueError("requestedCapabilities must be unique")
        return value


class WorkItemStatus(Status):
    """Observed state for a WorkItem."""

    phase: WorkItemPhase = WorkItemPhase.PENDING
    assigned_agent_ref: WorkItemAgentReference | None = Field(
        default=None,
        alias="assignedAgentRef",
    )
    invocation_refs: tuple[ResourceReference, ...] = Field(
        default_factory=tuple,
        alias="invocationRefs",
    )
    attempt: int = Field(default=0, ge=0)
    result_artifact_refs: tuple[ResourceReference, ...] = Field(
        default_factory=tuple,
        alias="resultArtifactRefs",
    )
    verification: WorkItemVerificationStatus = Field(
        default_factory=WorkItemVerificationStatus
    )
    started_at: datetime | None = Field(default=None, alias="startedAt")
    completed_at: datetime | None = Field(default=None, alias="completedAt")

    @model_validator(mode="after")
    def validate_timestamps(self) -> Self:
        """Ensure completion timestamps do not precede start timestamps."""

        if (
            self.started_at is not None
            and self.completed_at is not None
            and self.completed_at < self.started_at
        ):
            raise ValueError("completedAt must not be earlier than startedAt")
        return self


class WorkItemReadinessReason(StrEnum):
    """Deterministic WorkItem readiness outcomes."""

    READY = "Ready"
    NOT_PENDING = "NotPending"
    MISSING_DEPENDENCY = "MissingDependency"
    WAITING_FOR_DEPENDENCIES = "WaitingForDependencies"
    DEPENDENCY_BLOCKED = "DependencyBlocked"
    DEPENDENCY_SCOPE_MISMATCH = "DependencyScopeMismatch"


class WorkItemReadinessDecision(MaestroModel):
    """Readiness decision for a WorkItem."""

    ready: bool
    blocked: bool = False
    reason: WorkItemReadinessReason
    message: str = ""


class WorkItem(BaseResource[WorkItemSpec, WorkItemStatus]):
    """Smallest schedulable unit of work."""

    kind: Literal["WorkItem"] = "WorkItem"

    @model_validator(mode="after")
    def validate_owner_reference(self) -> Self:
        """Require exactly one matching Execution controller owner reference."""

        execution_owners = tuple(
            owner
            for owner in self.metadata.owner_references
            if owner.kind == "Execution" and owner.controller
        )
        if len(execution_owners) != 1:
            raise ValueError(
                "WorkItem must have exactly one Execution controller owner"
            )

        execution_owner = execution_owners[0]
        if execution_owner.id != self.spec.execution_ref.id:
            raise ValueError("WorkItem Execution owner must match spec.executionRef")

        return self

    @model_validator(mode="after")
    def validate_dependency_scope_and_attempts(self) -> Self:
        """Validate dependency identity and bounded attempt state."""

        for dependency in self.spec.depends_on:
            if dependency.id == self.metadata.id:
                raise ValueError("WorkItem cannot depend on itself")

        if self.status.attempt > self.spec.retry_policy.max_attempts:
            raise ValueError("WorkItem attempt cannot exceed retryPolicy.maxAttempts")

        if (
            self.status.phase == WorkItemPhase.SUCCEEDED
            and self.spec.verification.commands
            and not _has_successful_verification_evidence(self.spec, self.status)
        ):
            raise ValueError(
                "Succeeded WorkItems with verification commands require "
                "successful verification evidence"
            )

        return self

    @classmethod
    def new(
        cls,
        *,
        name: ResourceName,
        spec: WorkItemSpec,
        created_by: str = "local-user",
        namespace: ResourceName = "default",
    ) -> Self:
        """Create a new WorkItem resource with Execution ownership metadata."""

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
            status=WorkItemStatus(),
        )


class WorkItemRepository(
    ResourceRepository[WorkItem, WorkItemSpec, WorkItemStatus],
    Protocol,
):
    """Persistence contract for WorkItem resources."""

    async def list_by_execution(self, execution_id: UUID) -> tuple[WorkItem, ...]:
        """List WorkItems belonging to one Execution."""

    async def list_by_plan(self, plan_id: UUID) -> tuple[WorkItem, ...]:
        """List WorkItems produced by one Plan revision."""

    async def get_by_plan_work_item_id(
        self,
        plan_id: UUID,
        plan_work_item_id: str,
    ) -> WorkItem:
        """Load a WorkItem by Plan ID and planner-provided Work Item ID."""


def evaluate_work_item_readiness(
    work_item: WorkItem,
    dependencies: Iterable[WorkItem],
) -> WorkItemReadinessDecision:
    """Evaluate whether a WorkItem can move from Pending to Ready."""

    if work_item.status.phase != WorkItemPhase.PENDING:
        return WorkItemReadinessDecision(
            ready=False,
            reason=WorkItemReadinessReason.NOT_PENDING,
            message="WorkItem is not Pending",
        )

    dependency_by_id = {
        dependency.metadata.id: dependency for dependency in dependencies
    }
    for dependency_ref in work_item.spec.depends_on:
        dependency = dependency_by_id.get(dependency_ref.id)
        if dependency is None:
            return WorkItemReadinessDecision(
                ready=False,
                blocked=True,
                reason=WorkItemReadinessReason.MISSING_DEPENDENCY,
                message=f"Missing dependency {dependency_ref.id}",
            )

        if dependency.spec.execution_ref.id != work_item.spec.execution_ref.id:
            return WorkItemReadinessDecision(
                ready=False,
                blocked=True,
                reason=WorkItemReadinessReason.DEPENDENCY_SCOPE_MISMATCH,
                message=(
                    f"Dependency {dependency.metadata.id} belongs to another Execution"
                ),
            )

        if dependency.spec.plan_ref.id != work_item.spec.plan_ref.id:
            return WorkItemReadinessDecision(
                ready=False,
                blocked=True,
                reason=WorkItemReadinessReason.DEPENDENCY_SCOPE_MISMATCH,
                message=f"Dependency {dependency.metadata.id} belongs to another Plan",
            )

        if dependency.status.phase in {
            WorkItemPhase.BLOCKED,
            WorkItemPhase.FAILED,
            WorkItemPhase.CANCELLED,
        }:
            return WorkItemReadinessDecision(
                ready=False,
                blocked=True,
                reason=WorkItemReadinessReason.DEPENDENCY_BLOCKED,
                message=(
                    f"Dependency {dependency.metadata.id} is {dependency.status.phase}"
                ),
            )

        if dependency.status.phase != WorkItemPhase.SUCCEEDED:
            return WorkItemReadinessDecision(
                ready=False,
                reason=WorkItemReadinessReason.WAITING_FOR_DEPENDENCIES,
                message=f"Dependency {dependency.metadata.id} has not succeeded",
            )

    return WorkItemReadinessDecision(
        ready=True,
        reason=WorkItemReadinessReason.READY,
    )


def validate_work_item_transition(
    resource_id: UUID,
    current_phase: WorkItemPhase,
    next_phase: WorkItemPhase,
    *,
    attempt: int,
    max_attempts: int,
) -> None:
    """Reject illegal WorkItem phase transitions."""

    if current_phase == next_phase:
        return

    if current_phase in TERMINAL_WORK_ITEM_PHASES:
        raise ResourceTransitionError(resource_id, current_phase, next_phase)

    if (current_phase, next_phase) not in VALID_WORK_ITEM_TRANSITIONS:
        raise ResourceTransitionError(resource_id, current_phase, next_phase)

    if (
        current_phase == WorkItemPhase.FAILED
        and next_phase == WorkItemPhase.READY
        and attempt >= max_attempts
    ):
        raise ResourceTransitionError(resource_id, current_phase, next_phase)


def apply_work_item_spec_update(
    work_item: WorkItem,
    spec: WorkItemSpec,
    *,
    expected_resource_version: int,
) -> WorkItem:
    """Apply limited WorkItem spec updates before execution starts."""

    immutable_fields = (
        "execution_ref",
        "plan_ref",
        "plan_work_item_id",
        "role_ref",
    )
    for field_name in immutable_fields:
        if getattr(spec, field_name) != getattr(work_item.spec, field_name):
            raise ResourceImmutableFieldError(
                work_item.metadata.id,
                f"spec.{field_name}",
            )

    if work_item.status.phase not in MUTABLE_SPEC_PHASES and spec != work_item.spec:
        raise ResourceImmutableFieldError(work_item.metadata.id, "spec")

    return apply_spec_update(
        work_item,
        spec,
        expected_resource_version=expected_resource_version,
    )


def apply_work_item_status_update(
    work_item: WorkItem,
    status: WorkItemStatus,
    *,
    expected_resource_version: int,
) -> WorkItem:
    """Apply WorkItem status updates with phase transition validation."""

    validate_work_item_transition(
        work_item.metadata.id,
        work_item.status.phase,
        status.phase,
        attempt=work_item.status.attempt,
        max_attempts=work_item.spec.retry_policy.max_attempts,
    )
    return apply_status_update(
        work_item,
        status,
        expected_resource_version=expected_resource_version,
    )


def _has_successful_verification_evidence(
    spec: WorkItemSpec,
    status: WorkItemStatus,
) -> bool:
    expected_commands = set(spec.verification.commands)
    successful_commands = status.verification.successful_commands()
    return expected_commands <= successful_commands
