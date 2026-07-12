"""Tests for SQLite Approval persistence."""

import asyncio
from uuid import UUID, uuid4

import pytest

from maestro.domain import ResourceSelector
from maestro.domain.approvals import (
    Approval,
    ApprovalDecision,
    ApprovalDecisionValue,
    ApprovalExecutionReference,
    ApprovalPhase,
    ApprovalSpec,
    ApprovalSubjectReference,
    ApprovalType,
    record_approval_decision,
)
from maestro.domain.exceptions import (
    ResourceConflictError,
    ResourceImmutableFieldError,
)
from maestro.infrastructure.persistence import SQLiteApprovalRepository


def valid_approval_spec(
    execution_id: UUID | None = None,
    *,
    subject_id: UUID | None = None,
    subject_version: int = 3,
    required_approvers: int = 1,
) -> ApprovalSpec:
    """Build a valid ApprovalSpec for persistence tests."""

    return ApprovalSpec(
        executionRef=ApprovalExecutionReference(
            id=execution_id or uuid4(),
            name="implement-health",
        ),
        subjectRef=ApprovalSubjectReference(
            kind="Plan",
            id=subject_id or uuid4(),
            name="plan-1",
            resourceVersion=subject_version,
        ),
        type=ApprovalType.PLAN,
        requiredApprovers=required_approvers,
    )


def valid_approval(
    execution_id: UUID | None = None,
    *,
    subject_id: UUID | None = None,
    name: str = "plan-approval",
) -> Approval:
    """Build a valid Approval resource."""

    return Approval.new(
        name=name,
        spec=valid_approval_spec(execution_id, subject_id=subject_id),
    )


def approve_decision(actor: str = "sashka") -> ApprovalDecision:
    """Build an approve decision."""

    return ApprovalDecision(
        actor=actor,
        decision=ApprovalDecisionValue.APPROVE,
        requestSource="web-ui",
    )


def test_approval_persistence_round_trip(tmp_path) -> None:
    async def scenario() -> None:
        repository = SQLiteApprovalRepository(tmp_path / "maestro.db")
        approval = await repository.create(valid_approval())
        loaded = await repository.get(approval.metadata.id)

        assert loaded == approval
        repository.close()

    asyncio.run(scenario())


def test_approval_persistence_survives_repository_restart(tmp_path) -> None:
    async def scenario() -> None:
        database_path = tmp_path / "maestro.db"
        first_repository = SQLiteApprovalRepository(database_path)
        approval = await first_repository.create(valid_approval())
        first_repository.close()

        second_repository = SQLiteApprovalRepository(database_path)
        loaded = await second_repository.get(approval.metadata.id)

        assert loaded.metadata.id == approval.metadata.id
        assert loaded.spec.subject_ref.resource_version == 3
        second_repository.close()

    asyncio.run(scenario())


def test_approval_repository_lists_by_execution_subject_and_labels() -> None:
    async def scenario() -> None:
        repository = SQLiteApprovalRepository(":memory:")
        execution_id = uuid4()
        subject_id = uuid4()
        approval = valid_approval(execution_id, subject_id=subject_id)
        labeled_approval = approval.model_copy(
            update={
                "metadata": approval.metadata.model_copy(
                    update={"labels": {"gate": "plan"}}
                )
            }
        )
        await repository.create(labeled_approval)
        await repository.create(valid_approval(name="other-approval"))

        by_execution = await repository.list_by_execution(execution_id)
        by_subject = await repository.list_by_subject("Plan", subject_id)
        by_label = await repository.list(ResourceSelector(labels={"gate": "plan"}))

        assert [approval.metadata.name for approval in by_execution] == [
            "plan-approval"
        ]
        assert [approval.metadata.name for approval in by_subject] == ["plan-approval"]
        assert [approval.metadata.name for approval in by_label] == ["plan-approval"]
        repository.close()

    asyncio.run(scenario())


def test_approval_update_status_records_decision() -> None:
    async def scenario() -> None:
        repository = SQLiteApprovalRepository(":memory:")
        approval = await repository.create(valid_approval())
        decided = record_approval_decision(
            approval,
            approve_decision(),
            expected_resource_version=approval.metadata.resource_version,
        )

        updated = await repository.update_status(
            approval.metadata.id,
            decided.status,
            expected_resource_version=approval.metadata.resource_version,
        )

        assert updated.status.phase == ApprovalPhase.APPROVED
        assert updated.status.decisions[0].actor == "sashka"
        assert updated.metadata.generation == 1
        assert updated.metadata.resource_version == 2
        repository.close()

    asyncio.run(scenario())


def test_approval_spec_updates_are_rejected() -> None:
    async def scenario() -> None:
        repository = SQLiteApprovalRepository(":memory:")
        approval = await repository.create(valid_approval())
        changed_spec = approval.spec.model_copy(update={"required_approvers": 2})

        with pytest.raises(ResourceImmutableFieldError):
            await repository.update_spec(
                approval.metadata.id,
                changed_spec,
                expected_resource_version=approval.metadata.resource_version,
            )
        repository.close()

    asyncio.run(scenario())


def test_approval_decision_history_mutation_is_rejected() -> None:
    async def scenario() -> None:
        repository = SQLiteApprovalRepository(":memory:")
        approval = await repository.create(valid_approval())
        decided = record_approval_decision(
            approval,
            approve_decision(),
            expected_resource_version=approval.metadata.resource_version,
        )
        updated = await repository.update_status(
            approval.metadata.id,
            decided.status,
            expected_resource_version=approval.metadata.resource_version,
        )
        changed_decision = updated.status.decisions[0].model_copy(
            update={"comment": "Changed"}
        )

        with pytest.raises(ResourceImmutableFieldError):
            await repository.update_status(
                updated.metadata.id,
                updated.status.model_copy(update={"decisions": (changed_decision,)}),
                expected_resource_version=updated.metadata.resource_version,
            )
        repository.close()

    asyncio.run(scenario())


def test_approval_stale_status_update_returns_conflict() -> None:
    async def scenario() -> None:
        repository = SQLiteApprovalRepository(":memory:")
        approval = await repository.create(valid_approval())
        decided = record_approval_decision(
            approval,
            approve_decision(),
            expected_resource_version=approval.metadata.resource_version,
        )
        await repository.update_status(
            approval.metadata.id,
            decided.status,
            expected_resource_version=approval.metadata.resource_version,
        )

        with pytest.raises(ResourceConflictError):
            await repository.update_status(
                approval.metadata.id,
                decided.status,
                expected_resource_version=approval.metadata.resource_version,
            )
        repository.close()

    asyncio.run(scenario())
