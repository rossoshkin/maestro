"""Application service for Project resource use cases."""

from pathlib import Path
from uuid import UUID

from maestro.domain.projects import Project, ProjectRepository, ProjectSpec


class ProjectService:
    """Coordinate Project resource operations."""

    def __init__(
        self,
        repository: ProjectRepository,
        *,
        forbidden_repository_roots: tuple[Path, ...] = (),
    ) -> None:
        self._repository = repository
        self._forbidden_repository_roots = tuple(
            root.resolve() for root in forbidden_repository_roots
        )

    async def create_project(
        self,
        *,
        name: str,
        spec: ProjectSpec,
        created_by: str = "local-user",
        namespace: str = "default",
    ) -> Project:
        """Create a Project after admission checks."""

        self._validate_repository_paths(spec)
        project = Project.new(
            name=name,
            namespace=namespace,
            spec=spec,
            created_by=created_by,
        )
        return await self._repository.create(project)

    async def update_project_spec(
        self,
        resource_id: UUID,
        spec: ProjectSpec,
        *,
        expected_resource_version: int,
    ) -> Project:
        """Update Project desired state using optimistic concurrency."""

        self._validate_repository_paths(spec)
        return await self._repository.update_spec(
            resource_id,
            spec,
            expected_resource_version=expected_resource_version,
        )

    async def archive_project(
        self,
        resource_id: UUID,
        *,
        expected_resource_version: int,
    ) -> Project:
        """Request Project archival without touching source repositories."""

        project = await self._repository.get(resource_id)
        archived_spec = project.spec.model_copy(update={"archived": True})
        return await self.update_project_spec(
            resource_id,
            archived_spec,
            expected_resource_version=expected_resource_version,
        )

    async def request_project_deletion(
        self,
        resource_id: UUID,
        *,
        expected_resource_version: int,
    ) -> Project:
        """Mark a Project for deletion without deleting source repositories."""

        return await self._repository.mark_deleted(
            resource_id,
            expected_resource_version=expected_resource_version,
        )

    def _validate_repository_paths(self, spec: ProjectSpec) -> None:
        for repository in spec.repositories:
            repository_path = repository.path.resolve()
            for forbidden_root in self._forbidden_repository_roots:
                if repository_path == forbidden_root or repository_path.is_relative_to(
                    forbidden_root
                ):
                    raise ValueError(
                        "repository path must not be nested inside Maestro data roots"
                    )
