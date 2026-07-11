"""Tests for SQLite Project persistence."""

import asyncio
from pathlib import Path

import pytest

from maestro.domain import ResourceSelector
from maestro.domain.exceptions import ResourceAlreadyExistsError, ResourceConflictError
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
from maestro.infrastructure.persistence import SQLiteProjectRepository


def valid_project_spec(repository_path: Path) -> ProjectSpec:
    """Build a valid ProjectSpec for persistence tests."""

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


def test_project_persistence_round_trip(tmp_path: Path) -> None:
    async def scenario() -> None:
        repository = SQLiteProjectRepository(tmp_path / "maestro.db")
        project = Project.new(
            name="tour-manager",
            spec=valid_project_spec(tmp_path / "backend"),
        )
        created = await repository.create(project)
        loaded = await repository.get(created.metadata.id)

        assert loaded == created
        repository.close()

    asyncio.run(scenario())


def test_project_persistence_survives_repository_restart(tmp_path: Path) -> None:
    async def scenario() -> None:
        database_path = tmp_path / "maestro.db"
        first_repository = SQLiteProjectRepository(database_path)
        project = await first_repository.create(
            Project.new(
                name="tour-manager",
                spec=valid_project_spec(tmp_path / "backend"),
            )
        )
        first_repository.close()

        second_repository = SQLiteProjectRepository(database_path)
        loaded = await second_repository.get(project.metadata.id)

        assert loaded.metadata.id == project.metadata.id
        assert loaded.spec.workflow_ref.name == "software-delivery"
        second_repository.close()

    asyncio.run(scenario())


def test_project_update_spec_uses_optimistic_concurrency(tmp_path: Path) -> None:
    async def scenario() -> None:
        repository = SQLiteProjectRepository(":memory:")
        project = await repository.create(
            Project.new(
                name="tour-manager",
                spec=valid_project_spec(tmp_path / "backend"),
            )
        )
        updated_spec = project.spec.model_copy(update={"description": "Changed"})

        updated = await repository.update_spec(
            project.metadata.id,
            updated_spec,
            expected_resource_version=1,
        )

        assert updated.metadata.generation == 2
        assert updated.metadata.resource_version == 2

        with pytest.raises(ResourceConflictError):
            await repository.update_spec(
                project.metadata.id,
                updated_spec,
                expected_resource_version=1,
            )
        repository.close()

    asyncio.run(scenario())


def test_project_update_status_preserves_generation(tmp_path: Path) -> None:
    async def scenario() -> None:
        repository = SQLiteProjectRepository(":memory:")
        project = await repository.create(
            Project.new(
                name="tour-manager",
                spec=valid_project_spec(tmp_path / "backend"),
            )
        )
        status = ProjectStatus(observedGeneration=1, phase=ProjectPhase.READY)

        updated = await repository.update_status(
            project.metadata.id,
            status,
            expected_resource_version=1,
        )

        assert updated.metadata.generation == 1
        assert updated.metadata.resource_version == 2
        assert updated.status.phase == ProjectPhase.READY
        repository.close()

    asyncio.run(scenario())


def test_project_repository_lists_by_namespace_and_labels(tmp_path: Path) -> None:
    async def scenario() -> None:
        repository = SQLiteProjectRepository(":memory:")
        project = Project.new(
            name="tour-manager",
            spec=valid_project_spec(tmp_path / "backend"),
        )
        labeled_project = project.model_copy(
            update={
                "metadata": project.metadata.model_copy(
                    update={"labels": {"domain": "backend"}}
                )
            }
        )
        await repository.create(labeled_project)

        selected = await repository.list(ResourceSelector(labels={"domain": "backend"}))

        assert [project.metadata.name for project in selected] == ["tour-manager"]
        repository.close()

    asyncio.run(scenario())


def test_duplicate_project_names_are_rejected(tmp_path: Path) -> None:
    async def scenario() -> None:
        repository = SQLiteProjectRepository(":memory:")
        await repository.create(
            Project.new(
                name="tour-manager",
                spec=valid_project_spec(tmp_path / "backend"),
            )
        )

        with pytest.raises(ResourceAlreadyExistsError):
            await repository.create(
                Project.new(
                    name="tour-manager",
                    spec=valid_project_spec(tmp_path / "frontend"),
                )
            )
        repository.close()

    asyncio.run(scenario())


def test_project_mark_deleted_sets_deletion_timestamp(tmp_path: Path) -> None:
    async def scenario() -> None:
        repository = SQLiteProjectRepository(":memory:")
        project = await repository.create(
            Project.new(
                name="tour-manager",
                spec=valid_project_spec(tmp_path / "backend"),
            )
        )

        deleted = await repository.mark_deleted(
            project.metadata.id,
            expected_resource_version=project.metadata.resource_version,
        )

        assert deleted.metadata.deletion_timestamp is not None
        assert deleted.metadata.generation == 1
        assert deleted.metadata.resource_version == 2
        repository.close()

    asyncio.run(scenario())
