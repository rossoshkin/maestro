"""Tests for Approval resources."""

from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from maestro.domain.approvals import (
    Approval,
    ApprovalDecision,
    ApprovalDecisionValue,
    ApprovalExecutionReference,
    ApprovalPhase,
    ApprovalSpec,
    ApprovalStatus,
    ApprovalSubjectReference,
    ApprovalType,
    apply_approval_spec_update,
    apply_approval_status_update,
    invalidated_approval_status_for_subject,
    record_approval_decision,
)
from maestro.domain.artifacts import (
    Artifact,
    ArtifactExecutionReference,
    ArtifactProducer,
    ArtifactSpec,
    ArtifactStorageMetadata,
    ArtifactType,
)
from maestro.domain.exceptions import ResourceImmutableFieldError
from maestro.domain.resources import Metadata, OwnerReference


def valid_subject(execution_id: UUID | None = None) -> Artifact:
    """Build an Artifact subject for Approval tests."""

    owner_id = execution_id or uuid4()
    return Artifact.new(
        name="approved-plan",
        spec=ArtifactSpec(
            executionRef=ArtifactExecutionReference(id=owner_id, name="execution-1"),
            type=ArtifactType.PLAN,
            mediaType="application/json",
            storage=ArtifactStorageMetadata(uri="file:///tmp/artifacts/plan.json"),
            sha256="0" * 64,
            sizeBytes=2,
            producer=ArtifactProducer(subsystem="planner"),
        ),
    )


def valid_approval_spec(
    subject: Artifact, *, required_approvers: int = 1
) -> ApprovalSpec:
    """Build a valid ApprovalSpec for tests."""

    return ApprovalSpec(
        executionRef=ApprovalExecutionReference(
            id=subject.spec.execution_ref.id,
            name="execution-1",
        ),
        subjectRef=ApprovalSubjectReference(
            kind=subject.kind,
            id=subject.metadata.id,
            name=subject.metadata.name,
            resourceVersion=subject.metadata.resource_version,
        ),
        type=ApprovalType.PLAN,
        requiredApprovers=required_approvers,
    )


def valid_approval(
    subject: Artifact | None = None,
    *,
    required_approvers: int = 1,
) -> Approval:
    """Build a valid Approval resource."""

    subject = subject or valid_subject()
    return Approval.new(
        name="plan-approval",
        spec=valid_approval_spec(subject, required_approvers=required_approvers),
    )


def approve_decision(actor: str = "sashka") -> ApprovalDecision:
    """Build an approve decision."""

    return ApprovalDecision(
        actor=actor,
        decision=ApprovalDecisionValue.APPROVE,
        comment="Proceed",
        requestSource="web-ui",
    )


def test_approval_serializes_and_deserializes() -> None:
    approval = valid_approval()

    payload = approval.model_dump(mode="json", by_alias=True)
    round_tripped = Approval.model_validate(payload)

    assert payload["kind"] == "Approval"
    assert payload["spec"]["subjectRef"]["resourceVersion"] == 1
    assert payload["spec"]["type"] == "plan"
    assert round_tripped == approval


def test_approval_requires_matching_execution_owner() -> None:
    subject = valid_subject()
    spec = valid_approval_spec(subject)

    with pytest.raises(ValidationError):
        Approval(
            metadata=Metadata(
                name="plan-approval",
                ownerReferences=(
                    OwnerReference(
                        kind="Execution",
                        id=uuid4(),
                        controller=True,
                    ),
                ),
            ),
            spec=spec,
            status=ApprovalStatus(),
        )


def test_approval_subject_requires_exact_resource_version() -> None:
    subject = valid_subject()

    with pytest.raises(ValidationError):
        ApprovalSubjectReference(
            kind=subject.kind,
            id=subject.metadata.id,
            resourceVersion=0,
        )


def test_models_cannot_be_approval_actor_kind() -> None:
    with pytest.raises(ValidationError):
        ApprovalDecision(
            actor="llm-reviewer",
            actorKind="model",
            decision=ApprovalDecisionValue.APPROVE,
            requestSource="provider",
        )


def test_record_approval_decision_reaches_quorum() -> None:
    approval = valid_approval(required_approvers=2)
    first = record_approval_decision(
        approval,
        approve_decision("sashka"),
        expected_resource_version=approval.metadata.resource_version,
    )
    second = record_approval_decision(
        first,
        approve_decision("owner"),
        expected_resource_version=first.metadata.resource_version,
    )

    assert first.status.phase == ApprovalPhase.PENDING
    assert second.status.phase == ApprovalPhase.APPROVED
    assert [decision.actor for decision in second.status.decisions] == [
        "sashka",
        "owner",
    ]


def test_approval_decision_history_is_immutable() -> None:
    approval = valid_approval()
    approved = record_approval_decision(
        approval,
        approve_decision(),
        expected_resource_version=approval.metadata.resource_version,
    )
    changed_decision = approved.status.decisions[0].model_copy(
        update={"comment": "Changed"}
    )

    with pytest.raises(ResourceImmutableFieldError):
        apply_approval_status_update(
            approved,
            approved.status.model_copy(update={"decisions": (changed_decision,)}),
            expected_resource_version=approved.metadata.resource_version,
        )


def test_changed_subject_invalidates_approval() -> None:
    subject = valid_subject()
    approval = record_approval_decision(
        valid_approval(subject),
        approve_decision(),
        expected_resource_version=1,
    )
    changed_subject = subject.model_copy(
        update={
            "metadata": subject.metadata.model_copy(
                update={"resource_version": subject.metadata.resource_version + 1}
            )
        }
    )

    invalidated = invalidated_approval_status_for_subject(approval, changed_subject)

    assert invalidated is not None
    assert invalidated.phase == ApprovalPhase.INVALIDATED
    assert invalidated.invalidation_reason == "SubjectChanged"


def test_approval_spec_updates_are_rejected() -> None:
    approval = valid_approval()
    changed_spec = approval.spec.model_copy(update={"required_approvers": 2})

    with pytest.raises(ResourceImmutableFieldError):
        apply_approval_spec_update(
            approval,
            changed_spec,
            expected_resource_version=approval.metadata.resource_version,
        )
