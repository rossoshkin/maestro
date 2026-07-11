"""SQLite persistence for Workspace resources."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from uuid import UUID

from maestro.domain.exceptions import (
    ResourceAlreadyExistsError,
    ResourceConflictError,
    ResourceNotFoundError,
)
from maestro.domain.repositories import ResourceSelector
from maestro.domain.workspaces import (
    Workspace,
    WorkspaceRepository,
    WorkspaceSpec,
    WorkspaceStatus,
    apply_workspace_spec_update,
    apply_workspace_status_update,
)


class SQLiteWorkspaceRepository(WorkspaceRepository):
    """SQLite-backed Workspace repository."""

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

    async def create(self, resource: Workspace) -> Workspace:
        """Persist a new Workspace."""

        try:
            self._connection.execute(
                """
                INSERT INTO workspaces (
                    id,
                    execution_id,
                    namespace,
                    name,
                    repository_ref,
                    generation,
                    resource_version,
                    phase,
                    resource_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
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

    async def get(self, resource_id: UUID) -> Workspace:
        """Load a Workspace by ID."""

        row = self._connection.execute(
            "SELECT resource_json FROM workspaces WHERE id = ?",
            (str(resource_id),),
        ).fetchone()
        if row is None:
            raise ResourceNotFoundError(resource_id)
        return Workspace.model_validate_json(row["resource_json"])

    async def list(
        self,
        selector: ResourceSelector | None = None,
    ) -> tuple[Workspace, ...]:
        """List Workspaces matching optional selection criteria."""

        rows = self._connection.execute(
            "SELECT resource_json FROM workspaces ORDER BY namespace, name"
        ).fetchall()
        workspaces = tuple(
            Workspace.model_validate_json(row["resource_json"]) for row in rows
        )
        if selector is None:
            return workspaces
        return tuple(
            workspace for workspace in workspaces if self._matches(workspace, selector)
        )

    async def list_by_execution(self, execution_id: UUID) -> tuple[Workspace, ...]:
        """List Workspaces belonging to one Execution."""

        rows = self._connection.execute(
            """
            SELECT resource_json FROM workspaces
            WHERE execution_id = ?
            ORDER BY name
            """,
            (str(execution_id),),
        ).fetchall()
        return tuple(
            Workspace.model_validate_json(row["resource_json"]) for row in rows
        )

    async def update_spec(
        self,
        resource_id: UUID,
        spec: WorkspaceSpec,
        *,
        expected_resource_version: int,
    ) -> Workspace:
        """Persist a limited Workspace spec update."""

        workspace = await self.get(resource_id)
        updated = apply_workspace_spec_update(
            workspace,
            spec,
            expected_resource_version=expected_resource_version,
        )
        self._replace(updated, expected_resource_version=expected_resource_version)
        return updated

    async def update_status(
        self,
        resource_id: UUID,
        status: WorkspaceStatus,
        *,
        expected_resource_version: int,
    ) -> Workspace:
        """Persist a Workspace status update."""

        workspace = await self.get(resource_id)
        updated = apply_workspace_status_update(
            workspace,
            status,
            expected_resource_version=expected_resource_version,
        )
        self._replace(updated, expected_resource_version=expected_resource_version)
        return updated

    def _create_schema(self) -> None:
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS workspaces (
                id TEXT PRIMARY KEY,
                execution_id TEXT NOT NULL,
                namespace TEXT NOT NULL,
                name TEXT NOT NULL,
                repository_ref TEXT NOT NULL,
                generation INTEGER NOT NULL,
                resource_version INTEGER NOT NULL,
                phase TEXT NOT NULL,
                resource_json TEXT NOT NULL,
                UNIQUE(namespace, name)
            )
            """
        )
        self._connection.commit()

    def _replace(
        self,
        workspace: Workspace,
        *,
        expected_resource_version: int,
    ) -> None:
        cursor = self._connection.execute(
            """
            UPDATE workspaces
            SET repository_ref = ?,
                generation = ?,
                resource_version = ?,
                phase = ?,
                resource_json = ?
            WHERE id = ? AND resource_version = ?
            """,
            (
                workspace.spec.repository_ref,
                workspace.metadata.generation,
                workspace.metadata.resource_version,
                workspace.status.phase,
                self._serialize(workspace),
                str(workspace.metadata.id),
                expected_resource_version,
            ),
        )
        if cursor.rowcount != 1:
            current = self._connection.execute(
                "SELECT resource_json FROM workspaces WHERE id = ?",
                (str(workspace.metadata.id),),
            ).fetchone()
            if current is None:
                raise ResourceNotFoundError(workspace.metadata.id)
            actual = Workspace.model_validate_json(current["resource_json"])
            raise ResourceConflictError(
                workspace.metadata.id,
                expected_resource_version,
                actual.metadata.resource_version,
            )
        self._connection.commit()

    def _row_values(
        self,
        workspace: Workspace,
    ) -> tuple[str, str, str, str, str, int, int, str, str]:
        return (
            str(workspace.metadata.id),
            str(workspace.spec.execution_ref.id),
            workspace.metadata.namespace,
            workspace.metadata.name,
            workspace.spec.repository_ref,
            workspace.metadata.generation,
            workspace.metadata.resource_version,
            workspace.status.phase,
            self._serialize(workspace),
        )

    @staticmethod
    def _serialize(workspace: Workspace) -> str:
        return workspace.model_dump_json(by_alias=True)

    @staticmethod
    def _matches(workspace: Workspace, selector: ResourceSelector) -> bool:
        namespace_matches = (
            selector.namespace is None
            or workspace.metadata.namespace == selector.namespace
        )
        labels_match = all(
            workspace.metadata.labels.get(key) == value
            for key, value in selector.labels.items()
        )
        return namespace_matches and labels_match
