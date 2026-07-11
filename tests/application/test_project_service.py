"""Tests for Project application service behavior."""

import asyncio
from pathlib import Path

import pytest

from maestro.application.projects import ProjectService
from maestro.domain.projects import (
    AgentReference,
    Project,
    ProjectRepositoryBinding,
    ProjectRoleBinding,
    ProjectSpec,
    WorkflowReference,
)
from maestro.infrastructure.persistence import SQLiteProjectRepository


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


def test_create_project_rejects_repository_inside_data_roots(tmp_path: Path) -> None:
    async def scenario() -> None:
        data_root = tmp_path / "data"
        repository = SQLiteProjectRepository(":memory:")
        service = ProjectService(
            repository,
            forbidden_repository_roots=(data_root,),
        )

        with pytest.raises(ValueError):
            await service.create_project(
                name="tour-manager",
                spec=valid_project_spec(data_root / "repositories" / "backend"),
            )

        repository.close()

    asyncio.run(scenario())


def test_archive_project_never_deletes_source_repository(tmp_path: Path) -> None:
    async def scenario() -> None:
        source_repository = tmp_path / "source" / "backend"
        source_repository.mkdir(parents=True)
        repository = SQLiteProjectRepository(":memory:")
        service = ProjectService(repository)
        project = await service.create_project(
            name="tour-manager",
            spec=valid_project_spec(source_repository),
        )

        archived = await service.archive_project(
            project.metadata.id,
            expected_resource_version=project.metadata.resource_version,
        )

        assert archived.spec.archived is True
        assert source_repository.exists()
        repository.close()

    asyncio.run(scenario())


def test_project_deletion_request_preserves_source_repository(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        source_repository = tmp_path / "source" / "backend"
        source_repository.mkdir(parents=True)
        repository = SQLiteProjectRepository(":memory:")
        service = ProjectService(repository)
        project = await service.create_project(
            name="tour-manager",
            spec=valid_project_spec(source_repository),
        )

        deleted = await service.request_project_deletion(
            project.metadata.id,
            expected_resource_version=project.metadata.resource_version,
        )

        assert deleted.metadata.deletion_timestamp is not None
        assert source_repository.exists()
        repository.close()

    asyncio.run(scenario())


def test_project_service_create_returns_project_resource(tmp_path: Path) -> None:
    async def scenario() -> Project:
        repository = SQLiteProjectRepository(":memory:")
        service = ProjectService(repository)
        project = await service.create_project(
            name="tour-manager",
            spec=valid_project_spec(tmp_path / "backend"),
            created_by="tester",
        )
        repository.close()
        return project

    project = asyncio.run(scenario())

    assert project.metadata.name == "tour-manager"
    assert project.metadata.created_by == "tester"
