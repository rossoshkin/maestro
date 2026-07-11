"""Plan resource models and validation rules."""

from __future__ import annotations

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


class PlanPhase(StrEnum):
    """Plan status phases."""

    DRAFT = "Draft"
    VALIDATING = "Validating"
    WAITING_FOR_INPUT = "WaitingForInput"
    WAITING_FOR_APPROVAL = "WaitingForApproval"
    APPROVED = "Approved"
    REJECTED = "Rejected"
    SUPERSEDED = "Superseded"
    INVALID = "Invalid"


VALID_PLAN_TRANSITIONS = frozenset(
    {
        (PlanPhase.DRAFT, PlanPhase.VALIDATING),
        (PlanPhase.DRAFT, PlanPhase.WAITING_FOR_INPUT),
        (PlanPhase.DRAFT, PlanPhase.WAITING_FOR_APPROVAL),
        (PlanPhase.DRAFT, PlanPhase.REJECTED),
        (PlanPhase.DRAFT, PlanPhase.INVALID),
        (PlanPhase.VALIDATING, PlanPhase.WAITING_FOR_INPUT),
        (PlanPhase.VALIDATING, PlanPhase.WAITING_FOR_APPROVAL),
        (PlanPhase.VALIDATING, PlanPhase.REJECTED),
        (PlanPhase.VALIDATING, PlanPhase.INVALID),
        (PlanPhase.WAITING_FOR_INPUT, PlanPhase.VALIDATING),
        (PlanPhase.WAITING_FOR_INPUT, PlanPhase.REJECTED),
        (PlanPhase.WAITING_FOR_INPUT, PlanPhase.SUPERSEDED),
        (PlanPhase.WAITING_FOR_APPROVAL, PlanPhase.VALIDATING),
        (PlanPhase.WAITING_FOR_APPROVAL, PlanPhase.APPROVED),
        (PlanPhase.WAITING_FOR_APPROVAL, PlanPhase.REJECTED),
        (PlanPhase.WAITING_FOR_APPROVAL, PlanPhase.SUPERSEDED),
        (PlanPhase.APPROVED, PlanPhase.SUPERSEDED),
        (PlanPhase.REJECTED, PlanPhase.SUPERSEDED),
        (PlanPhase.INVALID, PlanPhase.SUPERSEDED),
    }
)


class PlanValidationResult(MaestroModel):
    """Plan validation result stored in status."""

    valid: bool = False
    errors: tuple[str, ...] = Field(default_factory=tuple)


class PlanExecutionReference(MaestroModel):
    """Reference to the owning Execution."""

    kind: Literal["Execution"] = "Execution"
    id: UUID
    name: ResourceName | None = None


class PlanRoleReference(MaestroModel):
    """Role version proposed for a Work Item."""

    name: ResourceName
    version: ReferenceVersion


class PlanRisk(MaestroModel):
    """Material risk identified by the Planner."""

    description: NonEmptyText
    mitigation: str = ""


class PlanWorkItemVerification(MaestroModel):
    """Verification commands proposed for a Work Item."""

    commands: tuple[NonEmptyText, ...] = Field(default_factory=tuple)


class PlanWorkItemProposal(MaestroModel):
    """Proposed Work Item contained inside a Plan revision."""

    id: PlanWorkItemId
    title: NonEmptyText
    role_ref: PlanRoleReference = Field(alias="roleRef")
    repository_ref: ResourceName | None = Field(default=None, alias="repositoryRef")
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
    verification: PlanWorkItemVerification = Field(
        default_factory=PlanWorkItemVerification
    )
    depends_on: tuple[PlanWorkItemId, ...] = Field(
        default_factory=tuple,
        alias="dependsOn",
    )
    requested_capabilities: tuple[CapabilityName, ...] = Field(
        default_factory=tuple,
        alias="requestedCapabilities",
    )

    @field_validator("depends_on")
    @classmethod
    def reject_duplicate_dependencies(
        cls,
        value: tuple[PlanWorkItemId, ...],
    ) -> tuple[PlanWorkItemId, ...]:
        """Reject duplicate dependency IDs on one proposed Work Item."""

        if len(set(value)) != len(value):
            raise ValueError("Work Item dependencies must be unique")
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


class PlanSpec(Spec):
    """Immutable Plan proposal for one Execution."""

    execution_ref: PlanExecutionReference = Field(alias="executionRef")
    version: int = Field(ge=1)
    summary: NonEmptyText
    assumptions: tuple[NonEmptyText, ...] = Field(default_factory=tuple)
    questions: tuple[NonEmptyText, ...] = Field(default_factory=tuple)
    risks: tuple[PlanRisk, ...] = Field(default_factory=tuple)
    work_items: tuple[PlanWorkItemProposal, ...] = Field(
        min_length=1,
        alias="workItems",
    )
    supersedes_plan_ref: ResourceReference | None = Field(
        default=None,
        alias="supersedesPlanRef",
    )

    @field_validator("work_items")
    @classmethod
    def reject_duplicate_work_item_ids(
        cls,
        value: tuple[PlanWorkItemProposal, ...],
    ) -> tuple[PlanWorkItemProposal, ...]:
        """Reject duplicate Work Item IDs within a Plan."""

        work_item_ids = [work_item.id for work_item in value]
        if len(set(work_item_ids)) != len(work_item_ids):
            raise ValueError("Work Item IDs must be unique within a Plan")
        return value

    @model_validator(mode="after")
    def validate_dependency_graph(self) -> Self:
        """Validate proposed Work Item dependency references and cycles."""

        validate_plan_dependency_graph(self)
        return self


class PlanStatus(Status):
    """Observed state for a Plan revision."""

    phase: PlanPhase = PlanPhase.DRAFT
    validation: PlanValidationResult = Field(default_factory=PlanValidationResult)
    approval_ref: ResourceReference | None = Field(default=None, alias="approvalRef")
    approved_by: str | None = Field(default=None, min_length=1, alias="approvedBy")
    approved_at: datetime | None = Field(default=None, alias="approvedAt")
    rejected_by: str | None = Field(default=None, min_length=1, alias="rejectedBy")
    rejected_at: datetime | None = Field(default=None, alias="rejectedAt")
    rejection_reason: str = Field(default="", alias="rejectionReason")
    superseded_by_ref: ResourceReference | None = Field(
        default=None,
        alias="supersededByRef",
    )

    @property
    def approval_ready(self) -> bool:
        """Return whether this status is ready for plan approval."""

        return (
            self.phase == PlanPhase.WAITING_FOR_APPROVAL
            and self.validation.valid
            and not self.validation.errors
        )

    @model_validator(mode="after")
    def validate_phase_metadata(self) -> Self:
        """Ensure lifecycle phases carry their required audit metadata."""

        if (self.approved_by is None) != (self.approved_at is None):
            raise ValueError("approvedBy and approvedAt must be set together")

        if (self.rejected_by is None) != (self.rejected_at is None):
            raise ValueError("rejectedBy and rejectedAt must be set together")

        if self.phase == PlanPhase.APPROVED and (
            self.approved_by is None or self.approved_at is None
        ):
            raise ValueError("Approved Plans require approvedBy and approvedAt")

        if self.phase == PlanPhase.REJECTED and (
            self.rejected_by is None or self.rejected_at is None
        ):
            raise ValueError("Rejected Plans require rejectedBy and rejectedAt")

        if self.phase == PlanPhase.SUPERSEDED and self.superseded_by_ref is None:
            raise ValueError("Superseded Plans require supersededByRef")

        return self


class Plan(BaseResource[PlanSpec, PlanStatus]):
    """Immutable, versioned Plan proposal for an Execution."""

    kind: Literal["Plan"] = "Plan"

    @model_validator(mode="after")
    def validate_owner_reference(self) -> Self:
        """Require exactly one matching Execution controller owner reference."""

        execution_owners = tuple(
            owner
            for owner in self.metadata.owner_references
            if owner.kind == "Execution" and owner.controller
        )
        if len(execution_owners) != 1:
            raise ValueError("Plan must have exactly one Execution controller owner")

        execution_owner = execution_owners[0]
        if execution_owner.id != self.spec.execution_ref.id:
            raise ValueError("Plan Execution owner must match spec.executionRef")

        return self

    @classmethod
    def new(
        cls,
        *,
        name: ResourceName,
        spec: PlanSpec,
        created_by: str = "local-user",
        namespace: ResourceName = "default",
    ) -> Self:
        """Create a new Plan resource with Execution ownership metadata."""

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
            status=PlanStatus(),
        )


class PlanRepository(ResourceRepository[Plan, PlanSpec, PlanStatus], Protocol):
    """Persistence contract for Plan resources."""

    async def list_by_execution(self, execution_id: UUID) -> tuple[Plan, ...]:
        """List Plan revisions belonging to one Execution."""

    async def get_by_execution_version(
        self,
        execution_id: UUID,
        version: int,
    ) -> Plan:
        """Load a Plan by Execution ID and Plan version."""

    async def get_approved_for_execution(self, execution_id: UUID) -> Plan | None:
        """Load the approved Plan for an Execution, if one exists."""


def validate_plan_transition(
    resource_id: UUID,
    current_phase: PlanPhase,
    next_phase: PlanPhase,
) -> None:
    """Reject illegal Plan phase transitions."""

    if current_phase == next_phase:
        return

    if (current_phase, next_phase) not in VALID_PLAN_TRANSITIONS:
        raise ResourceTransitionError(resource_id, current_phase, next_phase)


def validate_plan_dependency_graph(spec: PlanSpec) -> None:
    """Validate Work Item dependency targets and reject cycles."""

    work_item_ids = {work_item.id for work_item in spec.work_items}
    graph = {work_item.id: tuple(work_item.depends_on) for work_item in spec.work_items}

    for work_item_id, dependency_ids in graph.items():
        for dependency_id in dependency_ids:
            if dependency_id not in work_item_ids:
                raise ValueError(
                    f"Work Item {work_item_id!r} depends on missing Work Item "
                    f"{dependency_id!r}"
                )

    visiting: set[PlanWorkItemId] = set()
    visited: set[PlanWorkItemId] = set()

    def visit(work_item_id: PlanWorkItemId, path: tuple[PlanWorkItemId, ...]) -> None:
        if work_item_id in visited:
            return
        if work_item_id in visiting:
            cycle = (*path, work_item_id)
            raise ValueError(
                "Plan dependency graph contains a cycle: " + " -> ".join(cycle)
            )

        visiting.add(work_item_id)
        for dependency_id in graph[work_item_id]:
            visit(dependency_id, (*path, work_item_id))
        visiting.remove(work_item_id)
        visited.add(work_item_id)

    for work_item_id in graph:
        visit(work_item_id, ())


def apply_plan_spec_update(
    plan: Plan,
    spec: PlanSpec,
    *,
    expected_resource_version: int,
) -> Plan:
    """Reject actual Plan spec changes because Plan versions are immutable."""

    if spec != plan.spec:
        raise ResourceImmutableFieldError(plan.metadata.id, "spec")

    return apply_spec_update(
        plan,
        spec,
        expected_resource_version=expected_resource_version,
    )


def apply_plan_status_update(
    plan: Plan,
    status: PlanStatus,
    *,
    expected_resource_version: int,
) -> Plan:
    """Apply Plan status updates with phase transition validation."""

    validate_plan_transition(plan.metadata.id, plan.status.phase, status.phase)
    return apply_status_update(
        plan,
        status,
        expected_resource_version=expected_resource_version,
    )
