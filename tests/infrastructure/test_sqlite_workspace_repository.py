"""Tests for SQLite Workspace persistence."""

import asyncio
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from maestro.domain import ResourceSelector
from maestro.domain.exceptions import (
    ResourceConflictError,
    ResourceImmutableFieldError,
    ResourceTransitionError,
)
from maestro.domain.workspaces import (
    Workspace,
    WorkspaceExecutionReference,
    WorkspacePhase,
    WorkspaceProviderReference,
    WorkspaceSpec,
    WorkspaceStatus,
)
from maestro.infrastructure.persistence import SQLiteWorkspaceRepository


def valid_workspace_spec(
    execution_id: UUID | None = None,
    *,
    requested_path: Path | None = None,
) -> WorkspaceSpec:
    """Build a valid WorkspaceSpec for persistence tests."""

    return WorkspaceSpec(
        executionRef=WorkspaceExecutionReference(
            id=execution_id or uuid4(),
            name="implement-health",
        ),
        repositoryRef="backend",
        providerRef=WorkspaceProviderReference(name="local-git-worktree"),
        baseRevision="main",
        branchName="maestro/execution-123",
        requestedPath=requested_path,
    )


def valid_workspace(
    execution_id: UUID | None = None,
    *,
    name: str = "execution-backend",
) -> Workspace:
    """Build a valid Workspace resource."""

    return Workspace.new(name=name, spec=valid_workspace_spec(execution_id))


def test_workspace_persistence_round_trip(tmp_path) -> None:
    async def scenario() -> None:
        repository = SQLiteWorkspaceRepository(tmp_path / "maestro.db")
        workspace = await repository.create(valid_workspace())
        loaded = await repository.get(workspace.metadata.id)

        assert loaded == workspace
        repository.close()

    asyncio.run(scenario())


def test_workspace_persistence_survives_repository_restart(tmp_path) -> None:
    async def scenario() -> None:
        database_path = tmp_path / "maestro.db"
        first_repository = SQLiteWorkspaceRepository(database_path)
        workspace = await first_repository.create(valid_workspace())
        first_repository.close()

        second_repository = SQLiteWorkspaceRepository(database_path)
        loaded = await second_repository.get(workspace.metadata.id)

        assert loaded.metadata.id == workspace.metadata.id
        assert loaded.spec.repository_ref == "backend"
        second_repository.close()

    asyncio.run(scenario())


def test_workspace_repository_lists_by_execution_and_labels() -> None:
    async def scenario() -> None:
        repository = SQLiteWorkspaceRepository(":memory:")
        execution_id = uuid4()
        workspace = valid_workspace(execution_id)
        labeled_workspace = workspace.model_copy(
            update={
                "metadata": workspace.metadata.model_copy(
                    update={"labels": {"repo": "backend"}}
                )
            }
        )
        await repository.create(labeled_workspace)
        await repository.create(valid_workspace(name="other-workspace"))

        by_execution = await repository.list_by_execution(execution_id)
        by_label = await repository.list(ResourceSelector(labels={"repo": "backend"}))

        assert [workspace.metadata.name for workspace in by_execution] == [
            "execution-backend"
        ]
        assert [workspace.metadata.name for workspace in by_label] == [
            "execution-backend"
        ]
        repository.close()

    asyncio.run(scenario())


def test_workspace_update_status_validates_transition(tmp_path) -> None:
    async def scenario() -> None:
        repository = SQLiteWorkspaceRepository(":memory:")
        workspace = await repository.create(valid_workspace())

        preparing = await repository.update_status(
            workspace.metadata.id,
            WorkspaceStatus(phase=WorkspacePhase.PREPARING),
            expected_resource_version=workspace.metadata.resource_version,
        )
        ready = await repository.update_status(
            preparing.metadata.id,
            WorkspaceStatus(
                phase=WorkspacePhase.READY,
                path=tmp_path / "workspaces" / "execution-backend",
                observedRevision="abc123",
            ),
            expected_resource_version=preparing.metadata.resource_version,
        )

        assert ready.status.phase == WorkspacePhase.READY
        assert ready.metadata.generation == 1
        assert ready.metadata.resource_version == 3

        with pytest.raises(ResourceTransitionError):
            await repository.update_status(
                ready.metadata.id,
                WorkspaceStatus(phase=WorkspacePhase.RELEASED),
                expected_resource_version=ready.metadata.resource_version,
            )
        repository.close()

    asyncio.run(scenario())


def test_workspace_stale_status_update_returns_conflict() -> None:
    async def scenario() -> None:
        repository = SQLiteWorkspaceRepository(":memory:")
        workspace = await repository.create(valid_workspace())

        await repository.update_status(
            workspace.metadata.id,
            WorkspaceStatus(phase=WorkspacePhase.PREPARING),
            expected_resource_version=workspace.metadata.resource_version,
        )

        with pytest.raises(ResourceConflictError):
            await repository.update_status(
                workspace.metadata.id,
                WorkspaceStatus(phase=WorkspacePhase.PREPARING),
                expected_resource_version=workspace.metadata.resource_version,
            )
        repository.close()

    asyncio.run(scenario())


def test_workspace_spec_update_after_ready_is_rejected() -> None:
    async def scenario() -> None:
        repository = SQLiteWorkspaceRepository(":memory:")
        workspace = await repository.create(valid_workspace())
        preparing = await repository.update_status(
            workspace.metadata.id,
            WorkspaceStatus(phase=WorkspacePhase.PREPARING),
            expected_resource_version=workspace.metadata.resource_version,
        )
        ready = await repository.update_status(
            preparing.metadata.id,
            WorkspaceStatus(phase=WorkspacePhase.READY),
            expected_resource_version=preparing.metadata.resource_version,
        )

        with pytest.raises(ResourceImmutableFieldError):
            await repository.update_spec(
                ready.metadata.id,
                ready.spec.model_copy(update={"base_revision": "main~1"}),
                expected_resource_version=ready.metadata.resource_version,
            )
        repository.close()

    asyncio.run(scenario())
