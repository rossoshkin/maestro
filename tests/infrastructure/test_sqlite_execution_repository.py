"""Tests for SQLite Execution persistence."""

import asyncio
from uuid import uuid4

import pytest

from maestro.domain import ResourceSelector
from maestro.domain.exceptions import (
    ResourceAlreadyExistsError,
    ResourceConflictError,
    ResourceImmutableFieldError,
    ResourceTransitionError,
)
from maestro.domain.executions import (
    Execution,
    ExecutionPhase,
    ExecutionSpec,
    ExecutionStatus,
    ExecutionWorkflowReference,
    Goal,
    ProjectReference,
)
from maestro.infrastructure.persistence import SQLiteExecutionRepository


def valid_execution_spec(project_id=None) -> ExecutionSpec:
    """Build a valid ExecutionSpec for persistence tests."""

    return ExecutionSpec(
        projectRef=ProjectReference(
            id=project_id or uuid4(),
            name="tour-manager",
        ),
        goal=Goal(summary="Add health endpoint"),
        workflowRef=ExecutionWorkflowReference(
            name="software-delivery",
            version="v1alpha1",
        ),
        requestedRoles=("planner", "coding", "reviewer"),
    )


def valid_execution(project_id=None) -> Execution:
    """Build a valid Execution for persistence tests."""

    return Execution.new(
        name="add-health-endpoint",
        spec=valid_execution_spec(project_id),
    )


def test_execution_persistence_round_trip(tmp_path) -> None:
    async def scenario() -> None:
        repository = SQLiteExecutionRepository(tmp_path / "maestro.db")
        execution = await repository.create(valid_execution())
        loaded = await repository.get(execution.metadata.id)

        assert loaded == execution
        repository.close()

    asyncio.run(scenario())


def test_execution_persistence_survives_repository_restart(tmp_path) -> None:
    async def scenario() -> None:
        database_path = tmp_path / "maestro.db"
        first_repository = SQLiteExecutionRepository(database_path)
        execution = await first_repository.create(valid_execution())
        first_repository.close()

        second_repository = SQLiteExecutionRepository(database_path)
        loaded = await second_repository.get(execution.metadata.id)

        assert loaded.metadata.id == execution.metadata.id
        assert loaded.spec.workflow_ref.version == "v1alpha1"
        second_repository.close()

    asyncio.run(scenario())


def test_execution_update_status_validates_phase_transition() -> None:
    async def scenario() -> None:
        repository = SQLiteExecutionRepository(":memory:")
        execution = await repository.create(valid_execution())

        planning = await repository.update_status(
            execution.metadata.id,
            ExecutionStatus(phase=ExecutionPhase.PLANNING),
            expected_resource_version=execution.metadata.resource_version,
        )

        assert planning.status.phase == ExecutionPhase.PLANNING
        assert planning.metadata.generation == 1
        assert planning.metadata.resource_version == 2

        with pytest.raises(ResourceTransitionError):
            await repository.update_status(
                execution.metadata.id,
                ExecutionStatus(phase=ExecutionPhase.COMPLETED),
                expected_resource_version=planning.metadata.resource_version,
            )
        repository.close()

    asyncio.run(scenario())


def test_execution_update_spec_uses_optimistic_concurrency() -> None:
    async def scenario() -> None:
        repository = SQLiteExecutionRepository(":memory:")
        execution = await repository.create(valid_execution())
        changed_spec = execution.spec.model_copy(update={"suspended": True})

        updated = await repository.update_spec(
            execution.metadata.id,
            changed_spec,
            expected_resource_version=1,
        )

        assert updated.metadata.generation == 2
        assert updated.metadata.resource_version == 2

        with pytest.raises(ResourceConflictError):
            await repository.update_spec(
                execution.metadata.id,
                changed_spec,
                expected_resource_version=1,
            )
        repository.close()

    asyncio.run(scenario())


def test_goal_update_after_planning_is_rejected() -> None:
    async def scenario() -> None:
        repository = SQLiteExecutionRepository(":memory:")
        execution = await repository.create(valid_execution())
        planning = await repository.update_status(
            execution.metadata.id,
            ExecutionStatus(phase=ExecutionPhase.PLANNING),
            expected_resource_version=execution.metadata.resource_version,
        )
        changed_spec = planning.spec.model_copy(
            update={"goal": Goal(summary="Different goal")}
        )

        with pytest.raises(ResourceImmutableFieldError):
            await repository.update_spec(
                execution.metadata.id,
                changed_spec,
                expected_resource_version=planning.metadata.resource_version,
            )
        repository.close()

    asyncio.run(scenario())


def test_execution_repository_lists_by_project_and_labels() -> None:
    async def scenario() -> None:
        repository = SQLiteExecutionRepository(":memory:")
        project_id = uuid4()
        execution = valid_execution(project_id)
        labeled_execution = execution.model_copy(
            update={
                "metadata": execution.metadata.model_copy(
                    update={"labels": {"area": "backend"}}
                )
            }
        )
        await repository.create(labeled_execution)
        await repository.create(
            Execution.new(name="other", spec=valid_execution_spec())
        )

        by_project = await repository.list_by_project(project_id)
        by_label = await repository.list(ResourceSelector(labels={"area": "backend"}))

        assert [execution.metadata.name for execution in by_project] == [
            "add-health-endpoint"
        ]
        assert [execution.metadata.name for execution in by_label] == [
            "add-health-endpoint"
        ]
        repository.close()

    asyncio.run(scenario())


def test_duplicate_execution_names_are_rejected() -> None:
    async def scenario() -> None:
        repository = SQLiteExecutionRepository(":memory:")
        await repository.create(valid_execution())

        with pytest.raises(ResourceAlreadyExistsError):
            await repository.create(valid_execution())
        repository.close()

    asyncio.run(scenario())
