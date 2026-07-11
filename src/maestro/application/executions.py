"""Application service for Execution resource use cases."""

from uuid import UUID

from maestro.domain.exceptions import ResourceTransitionError
from maestro.domain.executions import (
    TERMINAL_EXECUTION_PHASES,
    Execution,
    ExecutionPhase,
    ExecutionRepository,
    ExecutionSpec,
)
from maestro.domain.projects import ProjectPhase, ProjectRepository


class ExecutionService:
    """Coordinate Execution resource operations."""

    def __init__(
        self,
        execution_repository: ExecutionRepository,
        project_repository: ProjectRepository,
    ) -> None:
        self._execution_repository = execution_repository
        self._project_repository = project_repository

    async def create_execution(
        self,
        *,
        name: str,
        spec: ExecutionSpec,
        created_by: str = "local-user",
        namespace: str = "default",
    ) -> Execution:
        """Create an Execution after validating the owning Project."""

        await self._validate_project_ready(spec.project_ref.id)
        execution = Execution.new(
            name=name,
            namespace=namespace,
            spec=spec,
            created_by=created_by,
        )
        return await self._execution_repository.create(execution)

    async def update_execution_spec(
        self,
        resource_id: UUID,
        spec: ExecutionSpec,
        *,
        expected_resource_version: int,
    ) -> Execution:
        """Update Execution desired state using optimistic concurrency."""

        await self._validate_project_ready(spec.project_ref.id)
        return await self._execution_repository.update_spec(
            resource_id,
            spec,
            expected_resource_version=expected_resource_version,
        )

    async def request_cancellation(
        self,
        resource_id: UUID,
        *,
        expected_resource_version: int,
    ) -> Execution:
        """Request cancellation through desired state."""

        execution = await self._execution_repository.get(resource_id)
        if execution.status.phase in TERMINAL_EXECUTION_PHASES:
            raise ResourceTransitionError(
                resource_id,
                execution.status.phase,
                ExecutionPhase.CANCELLED,
            )

        spec = execution.spec.model_copy(update={"cancellation_requested": True})
        return await self._execution_repository.update_spec(
            resource_id,
            spec,
            expected_resource_version=expected_resource_version,
        )

    async def set_suspended(
        self,
        resource_id: UUID,
        suspended: bool,
        *,
        expected_resource_version: int,
    ) -> Execution:
        """Set the Execution suspended desired-state flag."""

        execution = await self._execution_repository.get(resource_id)
        spec = execution.spec.model_copy(update={"suspended": suspended})
        return await self._execution_repository.update_spec(
            resource_id,
            spec,
            expected_resource_version=expected_resource_version,
        )

    async def _validate_project_ready(self, project_id: UUID) -> None:
        project = await self._project_repository.get(project_id)
        if project.status.phase not in {ProjectPhase.READY, ProjectPhase.DEGRADED}:
            raise ResourceTransitionError(
                project.metadata.id,
                project.status.phase,
                "ExecutionCreation",
            )
