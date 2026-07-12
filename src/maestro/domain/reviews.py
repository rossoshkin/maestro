"""Review resources, verdicts and structured finding rules."""

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
    Spec,
    Status,
)

ReviewText = Annotated[str, Field(min_length=1)]
ReviewSummary = Annotated[str, Field(max_length=8000)]
ReviewFindingId = Annotated[str, Field(min_length=1, max_length=128)]
ReviewPath = Annotated[str, Field(min_length=1, max_length=4096)]


class ReviewPhase(StrEnum):
    """Review lifecycle phases."""

    PENDING = "Pending"
    SCHEDULED = "Scheduled"
    RUNNING = "Running"
    COMPLETED = "Completed"
    FAILED = "Failed"
    CANCELLED = "Cancelled"


VALID_REVIEW_TRANSITIONS = frozenset(
    {
        (ReviewPhase.PENDING, ReviewPhase.SCHEDULED),
        (ReviewPhase.PENDING, ReviewPhase.RUNNING),
        (ReviewPhase.PENDING, ReviewPhase.COMPLETED),
        (ReviewPhase.PENDING, ReviewPhase.FAILED),
        (ReviewPhase.PENDING, ReviewPhase.CANCELLED),
        (ReviewPhase.SCHEDULED, ReviewPhase.RUNNING),
        (ReviewPhase.SCHEDULED, ReviewPhase.FAILED),
        (ReviewPhase.SCHEDULED, ReviewPhase.CANCELLED),
        (ReviewPhase.RUNNING, ReviewPhase.COMPLETED),
        (ReviewPhase.RUNNING, ReviewPhase.FAILED),
        (ReviewPhase.RUNNING, ReviewPhase.CANCELLED),
    }
)


class ReviewVerdict(StrEnum):
    """Review verdict values."""

    APPROVE = "Approve"
    REQUEST_CHANGES = "RequestChanges"
    NEEDS_HUMAN_DECISION = "NeedsHumanDecision"
    UNABLE_TO_REVIEW = "UnableToReview"


class ReviewFindingSeverity(StrEnum):
    """Finding severities."""

    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class ReviewFindingCategory(StrEnum):
    """Finding categories."""

    CORRECTNESS = "correctness"
    SECURITY = "security"
    MAINTAINABILITY = "maintainability"
    TESTS = "tests"
    SCOPE = "scope"


class ReviewExecutionReference(MaestroModel):
    """Reference to the owning Execution."""

    kind: Literal["Execution"] = "Execution"
    id: UUID
    name: ResourceName | None = None


class ReviewWorkItemReference(MaestroModel):
    """Reference to the reviewed WorkItem."""

    kind: Literal["WorkItem"] = "WorkItem"
    id: UUID
    name: ResourceName | None = None


class ReviewRoleReference(MaestroModel):
    """Reviewer Role version."""

    name: ResourceName
    version: ReferenceVersion


class ReviewArtifactReference(MaestroModel):
    """Exact immutable Artifact subject version."""

    kind: Literal["Artifact"] = "Artifact"
    id: UUID
    name: ResourceName | None = None
    resource_version: int = Field(ge=1, alias="resourceVersion")


class ReviewPolicy(MaestroModel):
    """Read-only Reviewer policy."""

    require_tests: bool = Field(default=True, alias="requireTests")
    security_checks: bool = Field(default=True, alias="securityChecks")
    allow_workspace_mutation: Literal[False] = Field(
        default=False,
        alias="allowWorkspaceMutation",
    )


class ReviewSpec(Spec):
    """Immutable Review request over Artifact subjects."""

    execution_ref: ReviewExecutionReference = Field(alias="executionRef")
    work_item_ref: ReviewWorkItemReference = Field(alias="workItemRef")
    reviewer_role_ref: ReviewRoleReference = Field(alias="reviewerRoleRef")
    subject_refs: tuple[ReviewArtifactReference, ...] = Field(
        min_length=1,
        alias="subjectRefs",
    )
    acceptance_criteria: tuple[ReviewText, ...] = Field(
        min_length=1,
        alias="acceptanceCriteria",
    )
    policy: ReviewPolicy = Field(default_factory=ReviewPolicy)

    @field_validator("subject_refs")
    @classmethod
    def reject_duplicate_subjects(
        cls,
        value: tuple[ReviewArtifactReference, ...],
    ) -> tuple[ReviewArtifactReference, ...]:
        """Reject duplicate Artifact subjects."""

        subject_ids = [subject.id for subject in value]
        if len(set(subject_ids)) != len(subject_ids):
            raise ValueError("subjectRefs must be unique by Artifact id")
        return value


class ReviewFinding(MaestroModel):
    """Structured review finding."""

    id: ReviewFindingId
    severity: ReviewFindingSeverity
    category: ReviewFindingCategory
    file: ReviewPath | None = None
    line: int | None = Field(default=None, ge=1)
    issue: ReviewText
    evidence: ReviewText
    suggested_fix: str = Field(default="", alias="suggestedFix")


class ReviewStatus(Status):
    """Observed Review output."""

    phase: ReviewPhase = ReviewPhase.PENDING
    verdict: ReviewVerdict | None = None
    summary: ReviewSummary = ""
    blocking_findings: tuple[ReviewFinding, ...] = Field(
        default_factory=tuple,
        alias="blockingFindings",
    )
    non_blocking_findings: tuple[ReviewFinding, ...] = Field(
        default_factory=tuple,
        alias="nonBlockingFindings",
    )
    missing_evidence: tuple[ReviewText, ...] = Field(
        default_factory=tuple,
        alias="missingEvidence",
    )
    completed_at: datetime | None = Field(default=None, alias="completedAt")
    failure_message: str = Field(default="", alias="failureMessage")

    @model_validator(mode="after")
    def validate_review_output(self) -> Self:
        """Validate verdict semantics and structured output."""

        finding_ids = [
            finding.id
            for finding in (*self.blocking_findings, *self.non_blocking_findings)
        ]
        if len(set(finding_ids)) != len(finding_ids):
            raise ValueError("Review findings must be unique by id")

        if self.phase != ReviewPhase.COMPLETED:
            if self.verdict is not None:
                raise ValueError("Only completed Reviews can carry a verdict")
            if self.completed_at is not None:
                raise ValueError("completedAt is only valid for completed Reviews")

        if self.phase == ReviewPhase.COMPLETED:
            if self.verdict is None or self.completed_at is None:
                raise ValueError("Completed Reviews require verdict and completedAt")
            self._validate_completed_verdict()

        if self.phase == ReviewPhase.FAILED and not self.failure_message:
            raise ValueError("Failed Reviews require failureMessage")

        return self

    def _validate_completed_verdict(self) -> None:
        if self.verdict == ReviewVerdict.APPROVE:
            if self.blocking_findings:
                raise ValueError("Approve verdict cannot include blocking findings")
            if self.missing_evidence:
                raise ValueError("Approve verdict requires sufficient evidence")

        if self.verdict == ReviewVerdict.REQUEST_CHANGES and not self.blocking_findings:
            raise ValueError("RequestChanges verdict requires blocking findings")

        if self.verdict == ReviewVerdict.UNABLE_TO_REVIEW and not self.missing_evidence:
            raise ValueError("UnableToReview verdict requires missingEvidence")


class Review(BaseResource[ReviewSpec, ReviewStatus]):
    """Structured evaluation of immutable Artifacts."""

    kind: Literal["Review"] = "Review"

    @model_validator(mode="after")
    def validate_review_metadata(self) -> Self:
        """Require matching Execution ownership."""

        execution_owners = tuple(
            owner
            for owner in self.metadata.owner_references
            if owner.kind == "Execution" and owner.controller
        )
        if len(execution_owners) != 1:
            raise ValueError("Review must have exactly one Execution controller owner")

        execution_owner = execution_owners[0]
        if execution_owner.id != self.spec.execution_ref.id:
            raise ValueError("Review Execution owner must match spec.executionRef")

        return self

    @classmethod
    def new(
        cls,
        *,
        name: ResourceName,
        spec: ReviewSpec,
        created_by: str = "local-user",
        namespace: ResourceName = "default",
    ) -> Self:
        """Create a new Review resource with Execution ownership metadata."""

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
            status=ReviewStatus(),
        )


class ReviewRepository(
    ResourceRepository[Review, ReviewSpec, ReviewStatus],
    Protocol,
):
    """Persistence contract for Review resources."""

    async def list_by_execution(self, execution_id: UUID) -> tuple[Review, ...]:
        """List Reviews belonging to one Execution."""

    async def list_by_work_item(self, work_item_id: UUID) -> tuple[Review, ...]:
        """List Reviews for one WorkItem."""


def validate_review_transition(
    resource_id: UUID,
    current_phase: ReviewPhase,
    next_phase: ReviewPhase,
) -> None:
    """Reject illegal Review phase transitions."""

    if current_phase == next_phase:
        return

    if (current_phase, next_phase) not in VALID_REVIEW_TRANSITIONS:
        raise ResourceTransitionError(resource_id, current_phase, next_phase)


def apply_review_spec_update(
    review: Review,
    spec: ReviewSpec,
    *,
    expected_resource_version: int,
) -> Review:
    """Reject Review spec changes because Review subjects are immutable."""

    if spec != review.spec:
        raise ResourceImmutableFieldError(review.metadata.id, "spec")

    return apply_spec_update(
        review,
        spec,
        expected_resource_version=expected_resource_version,
    )


def apply_review_status_update(
    review: Review,
    status: ReviewStatus,
    *,
    expected_resource_version: int,
) -> Review:
    """Apply Review status updates with phase transition validation."""

    validate_review_transition(review.metadata.id, review.status.phase, status.phase)
    return apply_status_update(
        review,
        status,
        expected_resource_version=expected_resource_version,
    )
