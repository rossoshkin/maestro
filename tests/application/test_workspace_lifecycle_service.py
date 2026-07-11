"""Tests for Workspace lifecycle service."""

import asyncio
from pathlib import Path
from uuid import UUID, uuid4

from maestro.application.workspaces import WorkspaceLifecycleService
from maestro.domain.workspaces import (
    Workspace,
    WorkspaceCommandRequest,
    WorkspaceCommandResult,
    WorkspaceDiff,
    WorkspaceExecutionReference,
    WorkspaceHandle,
    WorkspacePhase,
    WorkspacePrepareRequest,
    WorkspaceProviderError,
    WorkspaceProviderReference,
    WorkspaceSpec,
    WorkspaceState,
)
from maestro.infrastructure.persistence import SQLiteWorkspaceRepository


class FakeWorkspaceProvider:
    """Fake Workspace provider for lifecycle service tests."""

    def __init__(
        self,
        path: Path,
        *,
        dirty: bool = False,
        fail_cleanup: bool = False,
    ) -> None:
        self._path = path
        self._dirty = dirty
        self._fail_cleanup = fail_cleanup

    async def prepare(self, request: WorkspacePrepareRequest) -> WorkspaceHandle:
        return WorkspaceHandle(path=self._path, observedRevision="abc123")

    async def cleanup(self, handle: WorkspaceHandle) -> None:
        if self._fail_cleanup:
            raise WorkspaceProviderError("cleanup failed")

    async def collect_state(self, handle: WorkspaceHandle) -> WorkspaceState:
        return WorkspaceState(observedRevision="def456", dirty=self._dirty)

    async def collect_diff(self, handle: WorkspaceHandle) -> WorkspaceDiff:
        return WorkspaceDiff(text="diff --git a/README.md b/README.md")

    async def run_command(
        self,
        handle: WorkspaceHandle,
        request: WorkspaceCommandRequest,
    ) -> WorkspaceCommandResult:
        return WorkspaceCommandResult(exitCode=0, stdout="ok\n")


def valid_workspace_spec(execution_id: UUID | None = None) -> WorkspaceSpec:
    """Build a valid WorkspaceSpec for lifecycle tests."""

    return WorkspaceSpec(
        executionRef=WorkspaceExecutionReference(
            id=execution_id or uuid4(),
            name="implement-health",
        ),
        repositoryRef="backend",
        providerRef=WorkspaceProviderReference(name="local-git-worktree"),
        baseRevision="main",
        branchName="maestro/execution-123",
    )


def valid_workspace() -> Workspace:
    """Build a valid Workspace resource."""

    return Workspace.new(name="execution-backend", spec=valid_workspace_spec())


def test_workspace_prepare_persists_ready_state(tmp_path) -> None:
    async def scenario() -> None:
        repository = SQLiteWorkspaceRepository(":memory:")
        workspace = await repository.create(valid_workspace())
        prepared_path = tmp_path / "workspaces" / "execution-backend"
        service = WorkspaceLifecycleService(repository)

        prepared = await service.prepare_workspace(
            workspace.metadata.id,
            FakeWorkspaceProvider(prepared_path),
            source_repository_path=tmp_path / "source",
            workspace_root=tmp_path / "workspaces",
            expected_resource_version=workspace.metadata.resource_version,
        )
        loaded = await repository.get(workspace.metadata.id)

        assert prepared.status.phase == WorkspacePhase.READY
        assert prepared.status.path == prepared_path
        assert prepared.status.observed_revision == "abc123"
        assert loaded == prepared
        repository.close()

    asyncio.run(scenario())


def test_workspace_refresh_persists_dirty_status(tmp_path) -> None:
    async def scenario() -> None:
        repository = SQLiteWorkspaceRepository(":memory:")
        workspace = await repository.create(valid_workspace())
        service = WorkspaceLifecycleService(repository)
        prepared = await service.prepare_workspace(
            workspace.metadata.id,
            FakeWorkspaceProvider(tmp_path / "workspaces" / "execution-backend"),
            source_repository_path=tmp_path / "source",
            workspace_root=tmp_path / "workspaces",
            expected_resource_version=workspace.metadata.resource_version,
        )

        refreshed = await service.refresh_workspace_state(
            prepared.metadata.id,
            FakeWorkspaceProvider(
                tmp_path / "workspaces" / "execution-backend", dirty=True
            ),
            expected_resource_version=prepared.metadata.resource_version,
        )

        assert refreshed.status.phase == WorkspacePhase.DIRTY
        assert refreshed.status.dirty is True
        assert refreshed.status.observed_revision == "def456"
        repository.close()

    asyncio.run(scenario())


def test_failed_cleanup_preserves_diagnostic_state(tmp_path) -> None:
    async def scenario() -> None:
        repository = SQLiteWorkspaceRepository(":memory:")
        workspace = await repository.create(valid_workspace())
        prepared_path = tmp_path / "workspaces" / "execution-backend"
        service = WorkspaceLifecycleService(repository)
        prepared = await service.prepare_workspace(
            workspace.metadata.id,
            FakeWorkspaceProvider(prepared_path),
            source_repository_path=tmp_path / "source",
            workspace_root=tmp_path / "workspaces",
            expected_resource_version=workspace.metadata.resource_version,
        )

        failed = await service.cleanup_workspace(
            prepared.metadata.id,
            FakeWorkspaceProvider(prepared_path, fail_cleanup=True),
            expected_resource_version=prepared.metadata.resource_version,
        )

        assert failed.status.phase == WorkspacePhase.FAILED
        assert failed.status.path == prepared_path
        assert failed.status.failure_message == "cleanup failed"
        repository.close()

    asyncio.run(scenario())


def test_workspace_lifecycle_survives_repository_restart(tmp_path) -> None:
    async def scenario() -> None:
        database_path = tmp_path / "maestro.db"
        first_repository = SQLiteWorkspaceRepository(database_path)
        workspace = await first_repository.create(valid_workspace())
        prepared_path = tmp_path / "workspaces" / "execution-backend"
        service = WorkspaceLifecycleService(first_repository)
        prepared = await service.prepare_workspace(
            workspace.metadata.id,
            FakeWorkspaceProvider(prepared_path),
            source_repository_path=tmp_path / "source",
            workspace_root=tmp_path / "workspaces",
            expected_resource_version=workspace.metadata.resource_version,
        )
        first_repository.close()

        second_repository = SQLiteWorkspaceRepository(database_path)
        loaded = await second_repository.get(prepared.metadata.id)

        assert loaded.status.phase == WorkspacePhase.READY
        assert loaded.status.path == prepared_path
        assert loaded.status.observed_revision == "abc123"
        second_repository.close()

    asyncio.run(scenario())
