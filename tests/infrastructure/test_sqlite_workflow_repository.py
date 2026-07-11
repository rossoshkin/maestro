"""Tests for SQLite Workflow persistence."""

import asyncio

import pytest

from maestro.domain import ResourceSelector
from maestro.domain.exceptions import (
    ResourceAlreadyExistsError,
    ResourceConflictError,
    ResourceImmutableFieldError,
)
from maestro.domain.workflows import (
    TerminalOutcome,
    Workflow,
    WorkflowApprovalStep,
    WorkflowFanOutStep,
    WorkflowPhase,
    WorkflowRetryPolicy,
    WorkflowRoleReference,
    WorkflowRoleStep,
    WorkflowSpec,
    WorkflowStatus,
    WorkflowSystemStep,
    WorkflowTerminalStep,
    WorkflowValidationResult,
)
from maestro.infrastructure.persistence import SQLiteWorkflowRepository


def valid_workflow_spec() -> WorkflowSpec:
    """Build the default MVP Workflow shape for persistence tests."""

    return WorkflowSpec(
        version="v1alpha1",
        entrypoint="planning",
        steps=(
            WorkflowRoleStep(
                id="planning",
                roleRef=WorkflowRoleReference(name="planner", version="v1alpha1"),
                onSuccess="plan-approval",
            ),
            WorkflowApprovalStep(
                id="plan-approval",
                subjectRef="latestPlan",
                retryPolicy=WorkflowRetryPolicy(maxAttempts=2),
                onApproved="prepare-workspace",
                onRejected="planning",
            ),
            WorkflowSystemStep(
                id="prepare-workspace",
                controller="workspace",
                onSuccess="execute-work-items",
            ),
            WorkflowFanOutStep(
                id="execute-work-items",
                source="approvedPlan.workItems",
                onSuccess="completed",
            ),
            WorkflowTerminalStep(
                id="completed",
                outcome=TerminalOutcome.SUCCESS,
            ),
        ),
    )


def test_workflow_persistence_round_trip(tmp_path) -> None:
    async def scenario() -> None:
        repository = SQLiteWorkflowRepository(tmp_path / "maestro.db")
        workflow = await repository.create(
            Workflow.new(name="software-delivery", spec=valid_workflow_spec())
        )
        loaded = await repository.get(workflow.metadata.id)

        assert loaded == workflow
        repository.close()

    asyncio.run(scenario())


def test_workflow_persistence_survives_repository_restart(tmp_path) -> None:
    async def scenario() -> None:
        database_path = tmp_path / "maestro.db"
        first_repository = SQLiteWorkflowRepository(database_path)
        workflow = await first_repository.create(
            Workflow.new(name="software-delivery", spec=valid_workflow_spec())
        )
        first_repository.close()

        second_repository = SQLiteWorkflowRepository(database_path)
        loaded = await second_repository.get(workflow.metadata.id)

        assert loaded.metadata.id == workflow.metadata.id
        assert loaded.spec.version == "v1alpha1"
        second_repository.close()

    asyncio.run(scenario())


def test_workflow_lookup_by_exact_name_and_version(tmp_path) -> None:
    async def scenario() -> None:
        repository = SQLiteWorkflowRepository(":memory:")
        workflow = await repository.create(
            Workflow.new(name="software-delivery", spec=valid_workflow_spec())
        )

        loaded = await repository.get_by_name_version(
            workflow.metadata.namespace,
            workflow.metadata.name,
            workflow.spec.version,
        )

        assert loaded == workflow
        repository.close()

    asyncio.run(scenario())


def test_workflow_versions_are_unique_by_name_and_version() -> None:
    async def scenario() -> None:
        repository = SQLiteWorkflowRepository(":memory:")
        await repository.create(
            Workflow.new(name="software-delivery", spec=valid_workflow_spec())
        )

        with pytest.raises(ResourceAlreadyExistsError):
            await repository.create(
                Workflow.new(name="software-delivery", spec=valid_workflow_spec())
            )
        repository.close()

    asyncio.run(scenario())


def test_workflow_new_version_can_be_registered() -> None:
    async def scenario() -> None:
        repository = SQLiteWorkflowRepository(":memory:")
        await repository.create(
            Workflow.new(name="software-delivery", spec=valid_workflow_spec())
        )
        next_spec = valid_workflow_spec().model_copy(update={"version": "v1alpha2"})
        next_workflow = await repository.create(
            Workflow.new(name="software-delivery", spec=next_spec)
        )

        assert next_workflow.spec.version == "v1alpha2"
        repository.close()

    asyncio.run(scenario())


def test_workflow_spec_updates_are_rejected() -> None:
    async def scenario() -> None:
        repository = SQLiteWorkflowRepository(":memory:")
        workflow = await repository.create(
            Workflow.new(name="software-delivery", spec=valid_workflow_spec())
        )
        changed_spec = workflow.spec.model_copy(update={"description": "changed"})

        with pytest.raises(ResourceImmutableFieldError):
            await repository.update_spec(
                workflow.metadata.id,
                changed_spec,
                expected_resource_version=workflow.metadata.resource_version,
            )
        repository.close()

    asyncio.run(scenario())


def test_workflow_status_update_preserves_generation() -> None:
    async def scenario() -> None:
        repository = SQLiteWorkflowRepository(":memory:")
        workflow = await repository.create(
            Workflow.new(name="software-delivery", spec=valid_workflow_spec())
        )
        status = WorkflowStatus(
            observedGeneration=1,
            phase=WorkflowPhase.READY,
            validation=WorkflowValidationResult(valid=True),
        )

        updated = await repository.update_status(
            workflow.metadata.id,
            status,
            expected_resource_version=workflow.metadata.resource_version,
        )

        assert updated.metadata.generation == 1
        assert updated.metadata.resource_version == 2
        assert updated.status.phase == WorkflowPhase.READY
        repository.close()

    asyncio.run(scenario())


def test_workflow_stale_update_returns_conflict() -> None:
    async def scenario() -> None:
        repository = SQLiteWorkflowRepository(":memory:")
        workflow = await repository.create(
            Workflow.new(name="software-delivery", spec=valid_workflow_spec())
        )
        status = WorkflowStatus(phase=WorkflowPhase.READY)

        await repository.update_status(
            workflow.metadata.id,
            status,
            expected_resource_version=workflow.metadata.resource_version,
        )

        with pytest.raises(ResourceConflictError):
            await repository.update_status(
                workflow.metadata.id,
                status,
                expected_resource_version=workflow.metadata.resource_version,
            )
        repository.close()

    asyncio.run(scenario())


def test_workflow_repository_lists_by_namespace_and_labels() -> None:
    async def scenario() -> None:
        repository = SQLiteWorkflowRepository(":memory:")
        workflow = Workflow.new(name="software-delivery", spec=valid_workflow_spec())
        labeled_workflow = workflow.model_copy(
            update={
                "metadata": workflow.metadata.model_copy(
                    update={"labels": {"domain": "software"}}
                )
            }
        )
        await repository.create(labeled_workflow)

        selected = await repository.list(
            ResourceSelector(labels={"domain": "software"})
        )

        assert [workflow.metadata.name for workflow in selected] == [
            "software-delivery"
        ]
        repository.close()

    asyncio.run(scenario())
