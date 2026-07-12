"""Tests for Approval application service."""

import asyncio
from uuid import uuid4

from maestro.application.approvals import ApprovalService
from maestro.domain.approvals import (
    Approval,
    ApprovalDecision,
    ApprovalDecisionValue,
    ApprovalExecutionReference,
    ApprovalPhase,
    ApprovalSpec,
    ApprovalSubjectReference,
    ApprovalType,
)
from maestro.domain.artifacts import (
    Artifact,
    ArtifactExecutionReference,
    ArtifactProducer,
    ArtifactSpec,
    ArtifactStorageMetadata,
    ArtifactType,
)
from maestro.infrastructure.persistence import SQLiteApprovalRepository


def valid_subject() -> Artifact:
    """Build an Artifact subject for Approval service tests."""

    execution_id = uuid4()
    return Artifact.new(
        name="approved-plan",
        spec=ArtifactSpec(
            executionRef=ArtifactExecutionReference(id=execution_id),
            type=ArtifactType.PLAN,
            mediaType="application/json",
            storage=ArtifactStorageMetadata(uri="file:///tmp/artifacts/plan.json"),
            sha256="0" * 64,
            sizeBytes=2,
            producer=ArtifactProducer(subsystem="planner"),
        ),
    )


def valid_approval(subject: Artifact) -> Approval:
    """Build a valid Approval resource."""

    return Approval.new(
        name="plan-approval",
        spec=ApprovalSpec(
            executionRef=ApprovalExecutionReference(id=subject.spec.execution_ref.id),
            subjectRef=ApprovalSubjectReference(
                kind=subject.kind,
                id=subject.metadata.id,
                name=subject.metadata.name,
                resourceVersion=subject.metadata.resource_version,
            ),
            type=ApprovalType.PLAN,
        ),
    )


def test_approval_service_records_decision() -> None:
    async def scenario() -> None:
        repository = SQLiteApprovalRepository(":memory:")
        subject = valid_subject()
        approval = await repository.create(valid_approval(subject))
        service = ApprovalService(repository)

        decided = await service.record_decision(
            approval.metadata.id,
            ApprovalDecision(
                actor="sashka",
                decision=ApprovalDecisionValue.APPROVE,
                requestSource="web-ui",
            ),
            expected_resource_version=approval.metadata.resource_version,
        )

        assert decided.status.phase == ApprovalPhase.APPROVED
        assert decided.status.decisions[0].actor == "sashka"
        repository.close()

    asyncio.run(scenario())


def test_approval_service_invalidates_changed_subject() -> None:
    async def scenario() -> None:
        repository = SQLiteApprovalRepository(":memory:")
        subject = valid_subject()
        approval = await repository.create(valid_approval(subject))
        service = ApprovalService(repository)
        changed_subject = subject.model_copy(
            update={
                "metadata": subject.metadata.model_copy(
                    update={"resource_version": subject.metadata.resource_version + 1}
                )
            }
        )

        invalidated = await service.invalidate_if_subject_changed(
            approval.metadata.id,
            changed_subject,
            expected_resource_version=approval.metadata.resource_version,
        )

        assert invalidated.status.phase == ApprovalPhase.INVALIDATED
        assert invalidated.status.invalidation_reason == "SubjectChanged"
        repository.close()

    asyncio.run(scenario())
