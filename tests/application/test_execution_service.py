"""Tests for Execution application service behavior."""

import asyncio
from pathlib import Path

import pytest

from maestro.application.executions import ExecutionService
from maestro.domain.exceptions import ResourceTransitionError
from maestro.domain.executions import (
    ExecutionPhase,
    ExecutionSpec,
    ExecutionStatus,
    ExecutionWorkflowReference,
    Goal,
    ProjectReference,
)
from maestro.domain.projects import (
    AgentReference,
    Project,
    ProjectPhase,
    ProjectRepositoryBinding,
    ProjectRoleBinding,
    ProjectSpec,
    ProjectStatus,
    WorkflowReference,
)
from maestro.infrastructure.persistence import (
    SQLiteExecutionRepository,
    SQLiteProjectRepository,
)


def valid_project_spec(repository_path: Path) -> ProjectSpec:
    """Build a valid ProjectSpec for service tests."""

    return ProjectSpec(
        description="Test project",
        repositories=(
            ProjectRepositoryBinding(
                id="backend",
                path=repository_path,
                defaultBranch="main",
            ),
        ),
        workflowRef=WorkflowReference(name="software-delivery", version="v1alpha1"),
        roleBindings={
            "planner": ProjectRoleBinding(
                agentRef=AgentReference(name="planner-local")
            ),
        },
    )


def valid_execution_spec(project: Project) -> ExecutionSpec:
    """Build a valid ExecutionSpec for service tests."""

    return ExecutionSpec(
        projectRef=ProjectReference(
            id=project.metadata.id,
            name=project.metadata.name,
        ),
        goal=Goal(summary="Add health endpoint"),
        workflowRef=ExecutionWorkflowReference(
            name=project.spec.workflow_ref.name,
            version=project.spec.workflow_ref.version,
        ),
        requestedRoles=("planner", "coding", "reviewer"),
    )


async def create_ready_project(
    repository: SQLiteProjectRepository,
    tmp_path: Path,
) -> Project:
    """Create a Project and mark it Ready for Execution admission."""

    project = await repository.create(
        Project.new(
            name="tour-manager",
            spec=valid_project_spec(tmp_path / "backend"),
        )
    )
    return await repository.update_status(
        project.metadata.id,
        ProjectStatus(observedGeneration=1, phase=ProjectPhase.READY),
        expected_resource_version=project.metadata.resource_version,
    )


def test_create_execution_requires_ready_project(tmp_path: Path) -> None:
    async def scenario() -> None:
        project_repository = SQLiteProjectRepository(":memory:")
        execution_repository = SQLiteExecutionRepository(":memory:")
        service = ExecutionService(execution_repository, project_repository)
        project = await project_repository.create(
            Project.new(
                name="tour-manager",
                spec=valid_project_spec(tmp_path / "backend"),
            )
        )

        with pytest.raises(ResourceTransitionError):
            await service.create_execution(
                name="add-health-endpoint",
                spec=valid_execution_spec(project),
            )

        project_repository.close()
        execution_repository.close()

    asyncio.run(scenario())


def test_create_execution_persists_project_owner(tmp_path: Path) -> None:
    async def scenario() -> None:
        project_repository = SQLiteProjectRepository(":memory:")
        execution_repository = SQLiteExecutionRepository(":memory:")
        service = ExecutionService(execution_repository, project_repository)
        project = await create_ready_project(project_repository, tmp_path)

        execution = await service.create_execution(
            name="add-health-endpoint",
            spec=valid_execution_spec(project),
            created_by="tester",
        )

        assert execution.spec.project_ref.id == project.metadata.id
        assert execution.metadata.owner_references[0].id == project.metadata.id
        assert execution.metadata.created_by == "tester"
        project_repository.close()
        execution_repository.close()

    asyncio.run(scenario())


def test_request_cancellation_sets_desired_state(tmp_path: Path) -> None:
    async def scenario() -> None:
        project_repository = SQLiteProjectRepository(":memory:")
        execution_repository = SQLiteExecutionRepository(":memory:")
        service = ExecutionService(execution_repository, project_repository)
        project = await create_ready_project(project_repository, tmp_path)
        execution = await service.create_execution(
            name="add-health-endpoint",
            spec=valid_execution_spec(project),
        )

        cancelled = await service.request_cancellation(
            execution.metadata.id,
            expected_resource_version=execution.metadata.resource_version,
        )

        assert cancelled.spec.cancellation_requested is True
        project_repository.close()
        execution_repository.close()

    asyncio.run(scenario())


def test_terminal_execution_rejects_cancellation_request(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        project_repository = SQLiteProjectRepository(":memory:")
        execution_repository = SQLiteExecutionRepository(":memory:")
        service = ExecutionService(execution_repository, project_repository)
        project = await create_ready_project(project_repository, tmp_path)
        execution = await service.create_execution(
            name="add-health-endpoint",
            spec=valid_execution_spec(project),
        )
        planning = await execution_repository.update_status(
            execution.metadata.id,
            ExecutionStatus(phase=ExecutionPhase.PLANNING),
            expected_resource_version=execution.metadata.resource_version,
        )
        failed = await execution_repository.update_status(
            execution.metadata.id,
            ExecutionStatus(phase=ExecutionPhase.FAILED),
            expected_resource_version=planning.metadata.resource_version,
        )

        with pytest.raises(ResourceTransitionError):
            await service.request_cancellation(
                execution.metadata.id,
                expected_resource_version=failed.metadata.resource_version,
            )

        project_repository.close()
        execution_repository.close()

    asyncio.run(scenario())
