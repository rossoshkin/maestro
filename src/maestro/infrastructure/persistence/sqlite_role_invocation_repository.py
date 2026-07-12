"""SQLite persistence for RoleInvocation resources."""

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
from maestro.domain.role_invocations import (
    RoleInvocation,
    RoleInvocationRepository,
    RoleInvocationSpec,
    RoleInvocationStatus,
    apply_role_invocation_spec_update,
    apply_role_invocation_status_update,
)


class SQLiteRoleInvocationRepository(RoleInvocationRepository):
    """SQLite-backed RoleInvocation repository."""

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

    async def create(self, resource: RoleInvocation) -> RoleInvocation:
        """Persist a new RoleInvocation."""

        try:
            self._connection.execute(
                """
                INSERT INTO role_invocations (
                    id,
                    execution_id,
                    work_item_id,
                    namespace,
                    name,
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

    async def get(self, resource_id: UUID) -> RoleInvocation:
        """Load a RoleInvocation by ID."""

        row = self._connection.execute(
            "SELECT resource_json FROM role_invocations WHERE id = ?",
            (str(resource_id),),
        ).fetchone()
        if row is None:
            raise ResourceNotFoundError(resource_id)
        return RoleInvocation.model_validate_json(row["resource_json"])

    async def list(
        self,
        selector: ResourceSelector | None = None,
    ) -> tuple[RoleInvocation, ...]:
        """List RoleInvocations matching optional selection criteria."""

        rows = self._connection.execute(
            "SELECT resource_json FROM role_invocations ORDER BY namespace, name"
        ).fetchall()
        invocations = tuple(
            RoleInvocation.model_validate_json(row["resource_json"]) for row in rows
        )
        if selector is None:
            return invocations
        return tuple(
            invocation
            for invocation in invocations
            if self._matches(invocation, selector)
        )

    async def list_by_execution(
        self,
        execution_id: UUID,
    ) -> tuple[RoleInvocation, ...]:
        """List RoleInvocations belonging to one Execution."""

        rows = self._connection.execute(
            """
            SELECT resource_json FROM role_invocations
            WHERE execution_id = ?
            ORDER BY namespace, name
            """,
            (str(execution_id),),
        ).fetchall()
        return tuple(
            RoleInvocation.model_validate_json(row["resource_json"]) for row in rows
        )

    async def list_by_work_item(
        self,
        work_item_id: UUID,
    ) -> tuple[RoleInvocation, ...]:
        """List RoleInvocations associated with one WorkItem."""

        rows = self._connection.execute(
            """
            SELECT resource_json FROM role_invocations
            WHERE work_item_id = ?
            ORDER BY namespace, name
            """,
            (str(work_item_id),),
        ).fetchall()
        return tuple(
            RoleInvocation.model_validate_json(row["resource_json"]) for row in rows
        )

    async def update_spec(
        self,
        resource_id: UUID,
        spec: RoleInvocationSpec,
        *,
        expected_resource_version: int,
    ) -> RoleInvocation:
        """Persist a RoleInvocation spec update."""

        invocation = await self.get(resource_id)
        updated = apply_role_invocation_spec_update(
            invocation,
            spec,
            expected_resource_version=expected_resource_version,
        )
        self._replace(updated, expected_resource_version=expected_resource_version)
        return updated

    async def update_status(
        self,
        resource_id: UUID,
        status: RoleInvocationStatus,
        *,
        expected_resource_version: int,
    ) -> RoleInvocation:
        """Persist a RoleInvocation status update."""

        invocation = await self.get(resource_id)
        updated = apply_role_invocation_status_update(
            invocation,
            status,
            expected_resource_version=expected_resource_version,
        )
        self._replace(updated, expected_resource_version=expected_resource_version)
        return updated

    def _create_schema(self) -> None:
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS role_invocations (
                id TEXT PRIMARY KEY,
                execution_id TEXT NOT NULL,
                work_item_id TEXT,
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
        invocation: RoleInvocation,
        *,
        expected_resource_version: int,
    ) -> None:
        cursor = self._connection.execute(
            """
            UPDATE role_invocations
            SET execution_id = ?,
                work_item_id = ?,
                generation = ?,
                resource_version = ?,
                phase = ?,
                resource_json = ?
            WHERE id = ? AND resource_version = ?
            """,
            (
                str(invocation.spec.execution_ref.id),
                (
                    str(invocation.spec.work_item_ref.id)
                    if invocation.spec.work_item_ref is not None
                    else None
                ),
                invocation.metadata.generation,
                invocation.metadata.resource_version,
                invocation.status.phase,
                self._serialize(invocation),
                str(invocation.metadata.id),
                expected_resource_version,
            ),
        )
        if cursor.rowcount != 1:
            current = self._connection.execute(
                "SELECT resource_json FROM role_invocations WHERE id = ?",
                (str(invocation.metadata.id),),
            ).fetchone()
            if current is None:
                raise ResourceNotFoundError(invocation.metadata.id)
            actual = RoleInvocation.model_validate_json(current["resource_json"])
            raise ResourceConflictError(
                invocation.metadata.id,
                expected_resource_version,
                actual.metadata.resource_version,
            )
        self._connection.commit()

    def _row_values(
        self,
        invocation: RoleInvocation,
    ) -> tuple[str, str, str | None, str, str, int, int, str, str]:
        return (
            str(invocation.metadata.id),
            str(invocation.spec.execution_ref.id),
            (
                str(invocation.spec.work_item_ref.id)
                if invocation.spec.work_item_ref is not None
                else None
            ),
            invocation.metadata.namespace,
            invocation.metadata.name,
            invocation.metadata.generation,
            invocation.metadata.resource_version,
            invocation.status.phase,
            self._serialize(invocation),
        )

    @staticmethod
    def _serialize(invocation: RoleInvocation) -> str:
        return invocation.model_dump_json(by_alias=True)

    @staticmethod
    def _matches(
        invocation: RoleInvocation,
        selector: ResourceSelector,
    ) -> bool:
        namespace_matches = (
            selector.namespace is None
            or invocation.metadata.namespace == selector.namespace
        )
        labels_match = all(
            invocation.metadata.labels.get(key) == value
            for key, value in selector.labels.items()
        )
        return namespace_matches and labels_match
