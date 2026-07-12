"""SQLite persistence for Workflow resources."""

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
from maestro.domain.repositories import ResourceSelector, apply_status_update
from maestro.domain.workflows import (
    Workflow,
    WorkflowRepository,
    WorkflowSpec,
    WorkflowStatus,
    apply_workflow_spec_update,
)


class SQLiteWorkflowRepository(WorkflowRepository):
    """SQLite-backed Workflow repository."""

    def __init__(self, database_path: Path | str) -> None:
        self._database_path = database_path
        if database_path != ":memory:":
            Path(database_path).parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(
            str(database_path),
            check_same_thread=False,
        )
        self._connection.row_factory = sqlite3.Row
        self._create_schema()

    def close(self) -> None:
        """Close the SQLite connection."""

        self._connection.close()

    async def create(self, resource: Workflow) -> Workflow:
        """Persist a new Workflow version."""

        try:
            self._connection.execute(
                """
                INSERT INTO workflows (
                    id,
                    namespace,
                    name,
                    version,
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
                f"{resource.metadata.name}/{resource.spec.version}",
            ) from error
        return resource

    async def get(self, resource_id: UUID) -> Workflow:
        """Load a Workflow by ID."""

        row = self._connection.execute(
            "SELECT resource_json FROM workflows WHERE id = ?",
            (str(resource_id),),
        ).fetchone()
        if row is None:
            raise ResourceNotFoundError(resource_id)
        return Workflow.model_validate_json(row["resource_json"])

    async def get_by_name_version(
        self,
        namespace: str,
        name: str,
        version: str,
    ) -> Workflow:
        """Load a Workflow by namespace, name and version."""

        row = self._connection.execute(
            """
            SELECT resource_json FROM workflows
            WHERE namespace = ? AND name = ? AND version = ?
            """,
            (namespace, name, version),
        ).fetchone()
        if row is None:
            raise ResourceNameNotFoundError("Workflow", namespace, f"{name}/{version}")
        return Workflow.model_validate_json(row["resource_json"])

    async def list(
        self,
        selector: ResourceSelector | None = None,
    ) -> tuple[Workflow, ...]:
        """List Workflows matching optional selection criteria."""

        rows = self._connection.execute(
            "SELECT resource_json FROM workflows"
        ).fetchall()
        workflows = tuple(
            Workflow.model_validate_json(row["resource_json"]) for row in rows
        )
        if selector is None:
            return workflows
        return tuple(
            workflow for workflow in workflows if self._matches(workflow, selector)
        )

    async def update_spec(
        self,
        resource_id: UUID,
        spec: WorkflowSpec,
        *,
        expected_resource_version: int,
    ) -> Workflow:
        """Reject actual Workflow spec changes and persist no-op updates."""

        workflow = await self.get(resource_id)
        updated = apply_workflow_spec_update(
            workflow,
            spec,
            expected_resource_version=expected_resource_version,
        )
        self._replace(updated, expected_resource_version=expected_resource_version)
        return updated

    async def update_status(
        self,
        resource_id: UUID,
        status: WorkflowStatus,
        *,
        expected_resource_version: int,
    ) -> Workflow:
        """Persist a Workflow status update."""

        workflow = await self.get(resource_id)
        updated = apply_status_update(
            workflow,
            status,
            expected_resource_version=expected_resource_version,
        )
        self._replace(updated, expected_resource_version=expected_resource_version)
        return updated

    def _create_schema(self) -> None:
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS workflows (
                id TEXT PRIMARY KEY,
                namespace TEXT NOT NULL,
                name TEXT NOT NULL,
                version TEXT NOT NULL,
                generation INTEGER NOT NULL,
                resource_version INTEGER NOT NULL,
                phase TEXT NOT NULL,
                resource_json TEXT NOT NULL,
                UNIQUE(namespace, name, version)
            )
            """
        )
        self._connection.commit()

    def _replace(
        self,
        workflow: Workflow,
        *,
        expected_resource_version: int,
    ) -> None:
        cursor = self._connection.execute(
            """
            UPDATE workflows
            SET generation = ?,
                resource_version = ?,
                phase = ?,
                resource_json = ?
            WHERE id = ? AND resource_version = ?
            """,
            (
                workflow.metadata.generation,
                workflow.metadata.resource_version,
                workflow.status.phase,
                self._serialize(workflow),
                str(workflow.metadata.id),
                expected_resource_version,
            ),
        )
        if cursor.rowcount != 1:
            current = self._connection.execute(
                "SELECT resource_json FROM workflows WHERE id = ?",
                (str(workflow.metadata.id),),
            ).fetchone()
            if current is None:
                raise ResourceNotFoundError(workflow.metadata.id)
            actual = Workflow.model_validate_json(current["resource_json"])
            raise ResourceConflictError(
                workflow.metadata.id,
                expected_resource_version,
                actual.metadata.resource_version,
            )
        self._connection.commit()

    def _row_values(
        self,
        workflow: Workflow,
    ) -> tuple[str, str, str, str, int, int, str, str]:
        return (
            str(workflow.metadata.id),
            workflow.metadata.namespace,
            workflow.metadata.name,
            workflow.spec.version,
            workflow.metadata.generation,
            workflow.metadata.resource_version,
            workflow.status.phase,
            self._serialize(workflow),
        )

    @staticmethod
    def _serialize(workflow: Workflow) -> str:
        return workflow.model_dump_json(by_alias=True)

    @staticmethod
    def _matches(workflow: Workflow, selector: ResourceSelector) -> bool:
        namespace_matches = (
            selector.namespace is None
            or workflow.metadata.namespace == selector.namespace
        )
        labels_match = all(
            workflow.metadata.labels.get(key) == value
            for key, value in selector.labels.items()
        )
        return namespace_matches and labels_match
