"""SQLite persistence for Project resources."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from uuid import UUID

from maestro.domain.exceptions import (
    ResourceAlreadyExistsError,
    ResourceConflictError,
    ResourceNameNotFoundError,
    ResourceNotFoundError,
)
from maestro.domain.projects import (
    Project,
    ProjectRepository,
    ProjectSpec,
    ProjectStatus,
)
from maestro.domain.repositories import (
    ResourceSelector,
    apply_deletion_mark,
    apply_spec_update,
    apply_status_update,
)


class SQLiteProjectRepository(ProjectRepository):
    """SQLite-backed Project repository."""

    def __init__(self, database_path: Path | str) -> None:
        self._database_path = database_path
        if database_path != ":memory:":
            Path(database_path).parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(str(database_path))
        self._connection.row_factory = sqlite3.Row
        self._create_schema()

    def close(self) -> None:
        """Close the SQLite connection."""

        self._connection.close()

    async def create(self, resource: Project) -> Project:
        """Persist a new Project."""

        try:
            self._connection.execute(
                """
                INSERT INTO projects (
                    id,
                    namespace,
                    name,
                    generation,
                    resource_version,
                    resource_json,
                    archived,
                    deletion_timestamp
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                self._row_values(resource),
            )
            self._connection.commit()
        except sqlite3.IntegrityError as error:
            raise ResourceAlreadyExistsError(
                resource.kind,
                resource.metadata.namespace,
                resource.metadata.name,
            ) from error
        return resource

    async def get(self, resource_id: UUID) -> Project:
        """Load a Project by ID."""

        row = self._connection.execute(
            "SELECT resource_json FROM projects WHERE id = ?",
            (str(resource_id),),
        ).fetchone()
        if row is None:
            raise ResourceNotFoundError(resource_id)
        return Project.model_validate_json(row["resource_json"])

    async def get_by_name(self, namespace: str, name: str) -> Project:
        """Load a Project by namespace and name."""

        row = self._connection.execute(
            """
            SELECT resource_json FROM projects
            WHERE namespace = ? AND name = ?
            """,
            (namespace, name),
        ).fetchone()
        if row is None:
            raise ResourceNameNotFoundError("Project", namespace, name)
        return Project.model_validate_json(row["resource_json"])

    async def list(
        self,
        selector: ResourceSelector | None = None,
    ) -> tuple[Project, ...]:
        """List Projects matching optional selection criteria."""

        rows = self._connection.execute("SELECT resource_json FROM projects").fetchall()
        projects = tuple(
            Project.model_validate_json(row["resource_json"]) for row in rows
        )
        if selector is None:
            return projects
        return tuple(
            project for project in projects if self._matches(project, selector)
        )

    async def update_spec(
        self,
        resource_id: UUID,
        spec: ProjectSpec,
        *,
        expected_resource_version: int,
    ) -> Project:
        """Persist a Project spec update."""

        project = await self.get(resource_id)
        updated = apply_spec_update(
            project,
            spec,
            expected_resource_version=expected_resource_version,
        )
        self._replace(updated, expected_resource_version=expected_resource_version)
        return updated

    async def update_status(
        self,
        resource_id: UUID,
        status: ProjectStatus,
        *,
        expected_resource_version: int,
    ) -> Project:
        """Persist a Project status update."""

        project = await self.get(resource_id)
        updated = apply_status_update(
            project,
            status,
            expected_resource_version=expected_resource_version,
        )
        self._replace(updated, expected_resource_version=expected_resource_version)
        return updated

    async def mark_deleted(
        self,
        resource_id: UUID,
        *,
        expected_resource_version: int,
    ) -> Project:
        """Set deletionTimestamp without deleting repository contents."""

        project = await self.get(resource_id)
        updated = apply_deletion_mark(
            project,
            expected_resource_version=expected_resource_version,
        )
        self._replace(updated, expected_resource_version=expected_resource_version)
        return updated

    def _create_schema(self) -> None:
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS projects (
                id TEXT PRIMARY KEY,
                namespace TEXT NOT NULL,
                name TEXT NOT NULL,
                generation INTEGER NOT NULL,
                resource_version INTEGER NOT NULL,
                resource_json TEXT NOT NULL,
                archived INTEGER NOT NULL,
                deletion_timestamp TEXT,
                UNIQUE(namespace, name)
            )
            """
        )
        self._connection.commit()

    def _replace(
        self,
        project: Project,
        *,
        expected_resource_version: int,
    ) -> None:
        cursor = self._connection.execute(
            """
            UPDATE projects
            SET generation = ?,
                resource_version = ?,
                resource_json = ?,
                archived = ?,
                deletion_timestamp = ?
            WHERE id = ? AND resource_version = ?
            """,
            (
                project.metadata.generation,
                project.metadata.resource_version,
                self._serialize(project),
                int(project.spec.archived),
                (
                    project.metadata.deletion_timestamp.isoformat()
                    if project.metadata.deletion_timestamp is not None
                    else None
                ),
                str(project.metadata.id),
                expected_resource_version,
            ),
        )
        if cursor.rowcount != 1:
            current = self._connection.execute(
                "SELECT resource_json FROM projects WHERE id = ?",
                (str(project.metadata.id),),
            ).fetchone()
            if current is None:
                raise ResourceNotFoundError(project.metadata.id)
            actual = Project.model_validate_json(current["resource_json"])
            raise ResourceConflictError(
                project.metadata.id,
                expected_resource_version,
                actual.metadata.resource_version,
            )
        self._connection.commit()

    def _row_values(
        self,
        project: Project,
    ) -> tuple[str, str, str, int, int, str, int, str | None]:
        return (
            str(project.metadata.id),
            project.metadata.namespace,
            project.metadata.name,
            project.metadata.generation,
            project.metadata.resource_version,
            self._serialize(project),
            int(project.spec.archived),
            (
                project.metadata.deletion_timestamp.isoformat()
                if project.metadata.deletion_timestamp is not None
                else None
            ),
        )

    @staticmethod
    def _serialize(project: Project) -> str:
        return project.model_dump_json(by_alias=True)

    @staticmethod
    def _matches(project: Project, selector: ResourceSelector) -> bool:
        namespace_matches = (
            selector.namespace is None
            or project.metadata.namespace == selector.namespace
        )
        labels_match = all(
            project.metadata.labels.get(key) == value
            for key, value in selector.labels.items()
        )
        return namespace_matches and labels_match
