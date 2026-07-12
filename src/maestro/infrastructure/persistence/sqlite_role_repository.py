"""SQLite persistence for Role resources."""

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
from maestro.domain.roles import (
    Role,
    RoleRepository,
    RoleSpec,
    RoleStatus,
    apply_role_spec_update,
)


class SQLiteRoleRepository(RoleRepository):
    """SQLite-backed Role repository."""

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

    async def create(self, resource: Role) -> Role:
        """Persist a new Role version."""

        try:
            self._connection.execute(
                """
                INSERT INTO roles (
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

    async def get(self, resource_id: UUID) -> Role:
        """Load a Role by ID."""

        row = self._connection.execute(
            "SELECT resource_json FROM roles WHERE id = ?",
            (str(resource_id),),
        ).fetchone()
        if row is None:
            raise ResourceNotFoundError(resource_id)
        return Role.model_validate_json(row["resource_json"])

    async def get_by_name_version(
        self,
        namespace: str,
        name: str,
        version: str,
    ) -> Role:
        """Load a Role by namespace, name and version."""

        row = self._connection.execute(
            """
            SELECT resource_json FROM roles
            WHERE namespace = ? AND name = ? AND version = ?
            """,
            (namespace, name, version),
        ).fetchone()
        if row is None:
            raise ResourceNameNotFoundError("Role", namespace, f"{name}/{version}")
        return Role.model_validate_json(row["resource_json"])

    async def list(
        self,
        selector: ResourceSelector | None = None,
    ) -> tuple[Role, ...]:
        """List Roles matching optional selection criteria."""

        rows = self._connection.execute("SELECT resource_json FROM roles").fetchall()
        roles = tuple(Role.model_validate_json(row["resource_json"]) for row in rows)
        if selector is None:
            return roles
        return tuple(role for role in roles if self._matches(role, selector))

    async def update_spec(
        self,
        resource_id: UUID,
        spec: RoleSpec,
        *,
        expected_resource_version: int,
    ) -> Role:
        """Reject actual Role spec changes and persist no-op updates."""

        role = await self.get(resource_id)
        updated = apply_role_spec_update(
            role,
            spec,
            expected_resource_version=expected_resource_version,
        )
        self._replace(updated, expected_resource_version=expected_resource_version)
        return updated

    async def update_status(
        self,
        resource_id: UUID,
        status: RoleStatus,
        *,
        expected_resource_version: int,
    ) -> Role:
        """Persist a Role status update."""

        role = await self.get(resource_id)
        updated = apply_status_update(
            role,
            status,
            expected_resource_version=expected_resource_version,
        )
        self._replace(updated, expected_resource_version=expected_resource_version)
        return updated

    def _create_schema(self) -> None:
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS roles (
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
        role: Role,
        *,
        expected_resource_version: int,
    ) -> None:
        cursor = self._connection.execute(
            """
            UPDATE roles
            SET generation = ?,
                resource_version = ?,
                phase = ?,
                resource_json = ?
            WHERE id = ? AND resource_version = ?
            """,
            (
                role.metadata.generation,
                role.metadata.resource_version,
                role.status.phase,
                self._serialize(role),
                str(role.metadata.id),
                expected_resource_version,
            ),
        )
        if cursor.rowcount != 1:
            current = self._connection.execute(
                "SELECT resource_json FROM roles WHERE id = ?",
                (str(role.metadata.id),),
            ).fetchone()
            if current is None:
                raise ResourceNotFoundError(role.metadata.id)
            actual = Role.model_validate_json(current["resource_json"])
            raise ResourceConflictError(
                role.metadata.id,
                expected_resource_version,
                actual.metadata.resource_version,
            )
        self._connection.commit()

    def _row_values(
        self,
        role: Role,
    ) -> tuple[str, str, str, str, int, int, str, str]:
        return (
            str(role.metadata.id),
            role.metadata.namespace,
            role.metadata.name,
            role.spec.version,
            role.metadata.generation,
            role.metadata.resource_version,
            role.status.phase,
            self._serialize(role),
        )

    @staticmethod
    def _serialize(role: Role) -> str:
        return role.model_dump_json(by_alias=True)

    @staticmethod
    def _matches(role: Role, selector: ResourceSelector) -> bool:
        namespace_matches = (
            selector.namespace is None or role.metadata.namespace == selector.namespace
        )
        labels_match = all(
            role.metadata.labels.get(key) == value
            for key, value in selector.labels.items()
        )
        return namespace_matches and labels_match
