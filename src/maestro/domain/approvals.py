"""Approval resources, immutable decisions and invalidation rules."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Any, Literal, Protocol, Self
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
    MaestroModel,
    Metadata,
    OwnerReference,
    ResourceKind,
    ResourceName,
    Spec,
    Status,
    utc_now,
)

ApprovalComment = Annotated[str, Field(max_length=4000)]
ApprovalActor = Annotated[str, Field(min_length=1, max_length=128)]
RequestSource = Annotated[str, Field(min_length=1, max_length=128)]


class ApprovalType(StrEnum):
    """Known approval gate types."""

    PLAN = "plan"
    FINAL = "final"
    POLICY = "policy"
    MANUAL = "manual"


class ApprovalPhase(StrEnum):
    """Approval lifecycle phases."""

    PENDING = "Pending"
    APPROVED = "Approved"
    REJECTED = "Rejected"
    EXPIRED = "Expired"
    INVALIDATED = "Invalidated"
    CANCELLED = "Cancelled"


VALID_APPROVAL_TRANSITIONS = frozenset(
    {
        (ApprovalPhase.PENDING, ApprovalPhase.APPROVED),
        (ApprovalPhase.PENDING, ApprovalPhase.REJECTED),
        (ApprovalPhase.PENDING, ApprovalPhase.EXPIRED),
        (ApprovalPhase.PENDING, ApprovalPhase.INVALIDATED),
        (ApprovalPhase.PENDING, ApprovalPhase.CANCELLED),
        (ApprovalPhase.APPROVED, ApprovalPhase.INVALIDATED),
        (ApprovalPhase.APPROVED, ApprovalPhase.EXPIRED),
        (ApprovalPhase.REJECTED, ApprovalPhase.INVALIDATED),
    }
)


class ApprovalDecisionValue(StrEnum):
    """Decision values recorded by an approver."""

    APPROVE = "approve"
    REJECT = "reject"


class ApprovalActorKind(StrEnum):
    """Actor categories allowed to make MVP approval decisions."""

    HUMAN = "human"
    POLICY = "policy"


class ApprovalExecutionReference(MaestroModel):
    """Reference to the owning Execution."""

    kind: Literal["Execution"] = "Execution"
    id: UUID
    name: ResourceName | None = None


class ApprovalSubjectReference(MaestroModel):
    """Exact resource version under approval."""

    kind: ResourceKind
    id: UUID
    name: ResourceName | None = None
    resource_version: int = Field(ge=1, alias="resourceVersion")


class ApprovalSpec(Spec):
    """Immutable approval request."""

    execution_ref: ApprovalExecutionReference = Field(alias="executionRef")
    subject_ref: ApprovalSubjectReference = Field(alias="subjectRef")
    approval_type: ApprovalType = Field(alias="type")
    required_approvers: int = Field(default=1, ge=1, alias="requiredApprovers")
    expires_at: datetime | None = Field(default=None, alias="expiresAt")


class ApprovalDecision(MaestroModel):
    """Attributable immutable approval decision."""

    actor: ApprovalActor
    actor_kind: ApprovalActorKind = Field(
        default=ApprovalActorKind.HUMAN,
        alias="actorKind",
    )
    decision: ApprovalDecisionValue
    comment: ApprovalComment = ""
    decided_at: datetime = Field(default_factory=utc_now, alias="decidedAt")
    request_source: RequestSource = Field(alias="requestSource")


class ApprovalStatus(Status):
    """Observed approval state and append-only decisions."""

    phase: ApprovalPhase = ApprovalPhase.PENDING
    decisions: tuple[ApprovalDecision, ...] = Field(default_factory=tuple)
    invalidation_reason: str = Field(default="", alias="invalidationReason")

    @field_validator("decisions")
    @classmethod
    def reject_duplicate_actors(
        cls,
        value: tuple[ApprovalDecision, ...],
    ) -> tuple[ApprovalDecision, ...]:
        """Ensure each actor can decide at most once."""

        actors = [decision.actor for decision in value]
        if len(set(actors)) != len(actors):
            raise ValueError("approval decisions must be unique by actor")
        return value

    @model_validator(mode="after")
    def validate_phase_metadata(self) -> Self:
        """Ensure terminal phases carry their required metadata."""

        if self.phase == ApprovalPhase.APPROVED and not any(
            decision.decision == ApprovalDecisionValue.APPROVE
            for decision in self.decisions
        ):
            raise ValueError("Approved approvals require an approve decision")

        if self.phase == ApprovalPhase.REJECTED and not any(
            decision.decision == ApprovalDecisionValue.REJECT
            for decision in self.decisions
        ):
            raise ValueError("Rejected approvals require a reject decision")

        if self.phase == ApprovalPhase.INVALIDATED and not self.invalidation_reason:
            raise ValueError("Invalidated approvals require invalidationReason")

        return self


class Approval(BaseResource[ApprovalSpec, ApprovalStatus]):
    """Human or policy decision over an exact immutable subject version."""

    kind: Literal["Approval"] = "Approval"

    @model_validator(mode="after")
    def validate_approval_metadata(self) -> Self:
        """Require matching Execution ownership and decision quorum."""

        execution_owners = tuple(
            owner
            for owner in self.metadata.owner_references
            if owner.kind == "Execution" and owner.controller
        )
        if len(execution_owners) != 1:
            raise ValueError(
                "Approval must have exactly one Execution controller owner"
            )

        execution_owner = execution_owners[0]
        if execution_owner.id != self.spec.execution_ref.id:
            raise ValueError("Approval Execution owner must match spec.executionRef")

        if self.status.phase == ApprovalPhase.APPROVED:
            approvals = {
                decision.actor
                for decision in self.status.decisions
                if decision.decision == ApprovalDecisionValue.APPROVE
            }
            if len(approvals) < self.spec.required_approvers:
                raise ValueError("Approved approvals require requiredApprovers quorum")

        return self

    @classmethod
    def new(
        cls,
        *,
        name: ResourceName,
        spec: ApprovalSpec,
        created_by: str = "local-user",
        namespace: ResourceName = "default",
    ) -> Self:
        """Create a new Approval resource with Execution ownership metadata."""

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
            status=ApprovalStatus(),
        )


class ApprovalRepository(
    ResourceRepository[Approval, ApprovalSpec, ApprovalStatus],
    Protocol,
):
    """Persistence contract for Approval resources."""

    async def list_by_execution(self, execution_id: UUID) -> tuple[Approval, ...]:
        """List Approvals belonging to one Execution."""

    async def list_by_subject(
        self,
        subject_kind: str,
        subject_id: UUID,
    ) -> tuple[Approval, ...]:
        """List Approvals for one subject."""


def validate_approval_transition(
    resource_id: UUID,
    current_phase: ApprovalPhase,
    next_phase: ApprovalPhase,
) -> None:
    """Reject illegal Approval phase transitions."""

    if current_phase == next_phase:
        return

    if (current_phase, next_phase) not in VALID_APPROVAL_TRANSITIONS:
        raise ResourceTransitionError(resource_id, current_phase, next_phase)


def record_approval_decision(
    approval: Approval,
    decision: ApprovalDecision,
    *,
    expected_resource_version: int,
) -> Approval:
    """Append an approval decision and update phase when quorum is reached."""

    decisions = (*approval.status.decisions, decision)
    next_phase = _phase_for_decisions(approval, decisions)
    status = ApprovalStatus(
        observedGeneration=approval.metadata.generation,
        phase=next_phase,
        decisions=decisions,
    )
    return apply_approval_status_update(
        approval,
        status,
        expected_resource_version=expected_resource_version,
    )


def invalidated_approval_status_for_subject(
    approval: Approval,
    subject: BaseResource[Any, Any],
) -> ApprovalStatus | None:
    """Return an Invalidated status when a subject no longer matches approval."""

    if subject.kind != approval.spec.subject_ref.kind:
        return _invalidated_status(approval, "SubjectKindChanged")
    if subject.metadata.id != approval.spec.subject_ref.id:
        return _invalidated_status(approval, "SubjectChanged")
    if subject.metadata.deletion_timestamp is not None:
        return _invalidated_status(approval, "SubjectDeleted")
    if subject.metadata.resource_version != approval.spec.subject_ref.resource_version:
        return _invalidated_status(approval, "SubjectChanged")
    return None


def apply_approval_spec_update(
    approval: Approval,
    spec: ApprovalSpec,
    *,
    expected_resource_version: int,
) -> Approval:
    """Reject Approval spec changes because approval subjects are immutable."""

    if spec != approval.spec:
        raise ResourceImmutableFieldError(approval.metadata.id, "spec")

    return apply_spec_update(
        approval,
        spec,
        expected_resource_version=expected_resource_version,
    )


def apply_approval_status_update(
    approval: Approval,
    status: ApprovalStatus,
    *,
    expected_resource_version: int,
) -> Approval:
    """Apply Approval status updates with decision-history validation."""

    validate_approval_transition(
        approval.metadata.id,
        approval.status.phase,
        status.phase,
    )
    _validate_decision_history_update(approval, status)
    return apply_status_update(
        approval,
        status,
        expected_resource_version=expected_resource_version,
    )


def _phase_for_decisions(
    approval: Approval,
    decisions: tuple[ApprovalDecision, ...],
) -> ApprovalPhase:
    if any(decision.decision == ApprovalDecisionValue.REJECT for decision in decisions):
        return ApprovalPhase.REJECTED

    approvals = {
        decision.actor
        for decision in decisions
        if decision.decision == ApprovalDecisionValue.APPROVE
    }
    if len(approvals) >= approval.spec.required_approvers:
        return ApprovalPhase.APPROVED
    return ApprovalPhase.PENDING


def _invalidated_status(approval: Approval, reason: str) -> ApprovalStatus:
    return ApprovalStatus(
        observedGeneration=approval.metadata.generation,
        phase=ApprovalPhase.INVALIDATED,
        decisions=approval.status.decisions,
        invalidationReason=reason,
    )


def _validate_decision_history_update(
    approval: Approval,
    status: ApprovalStatus,
) -> None:
    current_decisions = approval.status.decisions
    new_decisions = status.decisions
    if len(new_decisions) < len(current_decisions):
        raise ResourceImmutableFieldError(approval.metadata.id, "status.decisions")
    if new_decisions[: len(current_decisions)] != current_decisions:
        raise ResourceImmutableFieldError(approval.metadata.id, "status.decisions")
    if (
        len(new_decisions) > len(current_decisions)
        and approval.status.phase != ApprovalPhase.PENDING
    ):
        raise ResourceImmutableFieldError(approval.metadata.id, "status.decisions")
