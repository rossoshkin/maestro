"""SQLite persistence for Execution resources."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from uuid import UUID

from maestro.domain.exceptions import (
    ResourceAlreadyExistsError,
    ResourceConflictError,
    ResourceNotFoundError,
)
from maestro.domain.executions import (
    Execution,
    ExecutionRepository,
    ExecutionSpec,
    ExecutionStatus,
    apply_execution_spec_update,
    apply_execution_status_update,
)
from maestro.domain.repositories import ResourceSelector


class SQLiteExecutionRepository(ExecutionRepository):
    """SQLite-backed Execution repository."""

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

    async def create(self, resource: Execution) -> Execution:
        """Persist a new Execution."""

        try:
            self._connection.execute(
                """
                INSERT INTO executions (
                    id,
                    project_id,
                    namespace,
                    name,
                    generation,
                    resource_version,
                    phase,
                    resource_json
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

    async def get(self, resource_id: UUID) -> Execution:
        """Load an Execution by ID."""

        row = self._connection.execute(
            "SELECT resource_json FROM executions WHERE id = ?",
            (str(resource_id),),
        ).fetchone()
        if row is None:
            raise ResourceNotFoundError(resource_id)
        return Execution.model_validate_json(row["resource_json"])

    async def list(
        self,
        selector: ResourceSelector | None = None,
    ) -> tuple[Execution, ...]:
        """List Executions matching optional selection criteria."""

        rows = self._connection.execute(
            "SELECT resource_json FROM executions"
        ).fetchall()
        executions = tuple(
            Execution.model_validate_json(row["resource_json"]) for row in rows
        )
        if selector is None:
            return executions
        return tuple(
            execution for execution in executions if self._matches(execution, selector)
        )

    async def list_by_project(self, project_id: UUID) -> tuple[Execution, ...]:
        """List Executions belonging to one Project."""

        rows = self._connection.execute(
            "SELECT resource_json FROM executions WHERE project_id = ?",
            (str(project_id),),
        ).fetchall()
        return tuple(
            Execution.model_validate_json(row["resource_json"]) for row in rows
        )

    async def update_spec(
        self,
        resource_id: UUID,
        spec: ExecutionSpec,
        *,
        expected_resource_version: int,
    ) -> Execution:
        """Persist an Execution spec update."""

        execution = await self.get(resource_id)
        updated = apply_execution_spec_update(
            execution,
            spec,
            expected_resource_version=expected_resource_version,
        )
        self._replace(updated, expected_resource_version=expected_resource_version)
        return updated

    async def update_status(
        self,
        resource_id: UUID,
        status: ExecutionStatus,
        *,
        expected_resource_version: int,
    ) -> Execution:
        """Persist an Execution status update."""

        execution = await self.get(resource_id)
        updated = apply_execution_status_update(
            execution,
            status,
            expected_resource_version=expected_resource_version,
        )
        self._replace(updated, expected_resource_version=expected_resource_version)
        return updated

    def _create_schema(self) -> None:
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS executions (
                id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL,
                namespace TEXT NOT NULL,
                name TEXT NOT NULL,
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
        execution: Execution,
        *,
        expected_resource_version: int,
    ) -> None:
        cursor = self._connection.execute(
            """
            UPDATE executions
            SET generation = ?,
                resource_version = ?,
                phase = ?,
                resource_json = ?
            WHERE id = ? AND resource_version = ?
            """,
            (
                execution.metadata.generation,
                execution.metadata.resource_version,
                execution.status.phase,
                self._serialize(execution),
                str(execution.metadata.id),
                expected_resource_version,
            ),
        )
        if cursor.rowcount != 1:
            current = self._connection.execute(
                "SELECT resource_json FROM executions WHERE id = ?",
                (str(execution.metadata.id),),
            ).fetchone()
            if current is None:
                raise ResourceNotFoundError(execution.metadata.id)
            actual = Execution.model_validate_json(current["resource_json"])
            raise ResourceConflictError(
                execution.metadata.id,
                expected_resource_version,
                actual.metadata.resource_version,
            )
        self._connection.commit()

    def _row_values(
        self,
        execution: Execution,
    ) -> tuple[str, str, str, str, int, int, str, str]:
        return (
            str(execution.metadata.id),
            str(execution.spec.project_ref.id),
            execution.metadata.namespace,
            execution.metadata.name,
            execution.metadata.generation,
            execution.metadata.resource_version,
            execution.status.phase,
            self._serialize(execution),
        )

    @staticmethod
    def _serialize(execution: Execution) -> str:
        return execution.model_dump_json(by_alias=True)

    @staticmethod
    def _matches(execution: Execution, selector: ResourceSelector) -> bool:
        namespace_matches = (
            selector.namespace is None
            or execution.metadata.namespace == selector.namespace
        )
        labels_match = all(
            execution.metadata.labels.get(key) == value
            for key, value in selector.labels.items()
        )
        return namespace_matches and labels_match
