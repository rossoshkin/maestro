"""Application services for Workspace lifecycle operations."""

from pathlib import Path
from uuid import UUID

from maestro.domain.workspaces import (
    Workspace,
    WorkspaceCommandRequest,
    WorkspaceCommandResult,
    WorkspaceDiff,
    WorkspaceHandle,
    WorkspacePhase,
    WorkspacePrepareRequest,
    WorkspaceProvider,
    WorkspaceProviderError,
    WorkspaceRepository,
)


class WorkspaceLifecycleService:
    """Coordinate Workspace persistence with a Workspace provider."""

    def __init__(self, workspace_repository: WorkspaceRepository) -> None:
        self._workspace_repository = workspace_repository

    async def prepare_workspace(
        self,
        resource_id: UUID,
        provider: WorkspaceProvider,
        *,
        source_repository_path: Path,
        workspace_root: Path,
        expected_resource_version: int,
    ) -> Workspace:
        """Prepare a Workspace and persist lifecycle state."""

        workspace = await self._workspace_repository.get(resource_id)
        preparing = await self._workspace_repository.update_status(
            resource_id,
            workspace.status.model_copy(
                update={
                    "phase": WorkspacePhase.PREPARING,
                    "observed_generation": workspace.metadata.generation,
                    "failure_message": "",
                }
            ),
            expected_resource_version=expected_resource_version,
        )

        try:
            handle = await provider.prepare(
                WorkspacePrepareRequest(
                    workspace=preparing,
                    sourceRepositoryPath=source_repository_path,
                    workspaceRoot=workspace_root,
                )
            )
        except WorkspaceProviderError as error:
            return await self._mark_failed(preparing, str(error))

        return await self._workspace_repository.update_status(
            resource_id,
            preparing.status.model_copy(
                update={
                    "phase": WorkspacePhase.READY,
                    "path": handle.path,
                    "observed_revision": handle.observed_revision,
                    "dirty": False,
                    "lock_holder": None,
                    "failure_message": "",
                    "observed_generation": preparing.metadata.generation,
                }
            ),
            expected_resource_version=preparing.metadata.resource_version,
        )

    async def refresh_workspace_state(
        self,
        resource_id: UUID,
        provider: WorkspaceProvider,
        *,
        expected_resource_version: int,
    ) -> Workspace:
        """Collect status from a provider and persist it."""

        workspace = await self._workspace_repository.get(resource_id)
        state = await provider.collect_state(_workspace_handle(workspace))
        phase = _phase_for_state(workspace, dirty=state.dirty)
        return await self._workspace_repository.update_status(
            resource_id,
            workspace.status.model_copy(
                update={
                    "phase": phase,
                    "observed_revision": state.observed_revision,
                    "dirty": state.dirty,
                    "observed_generation": workspace.metadata.generation,
                }
            ),
            expected_resource_version=expected_resource_version,
        )

    async def collect_workspace_diff(
        self,
        resource_id: UUID,
        provider: WorkspaceProvider,
    ) -> WorkspaceDiff:
        """Collect a Workspace diff from the provider."""

        workspace = await self._workspace_repository.get(resource_id)
        return await provider.collect_diff(_workspace_handle(workspace))

    async def run_workspace_command(
        self,
        resource_id: UUID,
        provider: WorkspaceProvider,
        request: WorkspaceCommandRequest,
    ) -> WorkspaceCommandResult:
        """Run a command inside a persisted Workspace path."""

        workspace = await self._workspace_repository.get(resource_id)
        return await provider.run_command(_workspace_handle(workspace), request)

    async def cleanup_workspace(
        self,
        resource_id: UUID,
        provider: WorkspaceProvider,
        *,
        expected_resource_version: int,
    ) -> Workspace:
        """Clean up a Workspace and preserve diagnostics on cleanup failure."""

        workspace = await self._workspace_repository.get(resource_id)
        releasing = await self._workspace_repository.update_status(
            resource_id,
            workspace.status.model_copy(
                update={
                    "phase": WorkspacePhase.RELEASING,
                    "lock_holder": None,
                    "observed_generation": workspace.metadata.generation,
                }
            ),
            expected_resource_version=expected_resource_version,
        )

        if releasing.status.path is not None:
            try:
                await provider.cleanup(_workspace_handle(releasing))
            except WorkspaceProviderError as error:
                return await self._mark_failed(releasing, str(error))

        return await self._workspace_repository.update_status(
            resource_id,
            releasing.status.model_copy(
                update={
                    "phase": WorkspacePhase.RELEASED,
                    "path": None,
                    "dirty": False,
                    "lock_holder": None,
                    "failure_message": "",
                    "observed_generation": releasing.metadata.generation,
                }
            ),
            expected_resource_version=releasing.metadata.resource_version,
        )

    async def _mark_failed(self, workspace: Workspace, message: str) -> Workspace:
        return await self._workspace_repository.update_status(
            workspace.metadata.id,
            workspace.status.model_copy(
                update={
                    "phase": WorkspacePhase.FAILED,
                    "failure_message": message,
                    "observed_generation": workspace.metadata.generation,
                }
            ),
            expected_resource_version=workspace.metadata.resource_version,
        )


def _workspace_handle(workspace: Workspace) -> WorkspaceHandle:
    if workspace.status.path is None:
        raise WorkspaceProviderError("Workspace has no prepared path")
    return WorkspaceHandle(
        path=workspace.status.path,
        observedRevision=workspace.status.observed_revision or "unknown",
    )


def _phase_for_state(workspace: Workspace, *, dirty: bool) -> WorkspacePhase:
    if workspace.status.phase == WorkspacePhase.IN_USE:
        return WorkspacePhase.IN_USE
    if dirty:
        return WorkspacePhase.DIRTY
    return WorkspacePhase.READY
