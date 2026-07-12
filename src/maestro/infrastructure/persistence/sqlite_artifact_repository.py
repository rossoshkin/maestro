"""SQLite persistence for Artifact resources."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from uuid import UUID

from maestro.domain.artifacts import (
    Artifact,
    ArtifactRepository,
    ArtifactSpec,
    ArtifactStatus,
    apply_artifact_spec_update,
    apply_artifact_status_update,
)
from maestro.domain.exceptions import (
    ResourceAlreadyExistsError,
    ResourceConflictError,
    ResourceNotFoundError,
)
from maestro.domain.repositories import ResourceSelector


class SQLiteArtifactRepository(ArtifactRepository):
    """SQLite-backed Artifact repository."""

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

    async def create(self, resource: Artifact) -> Artifact:
        """Persist a new Artifact."""

        try:
            self._connection.execute(
                """
                INSERT INTO artifacts (
                    id,
                    execution_id,
                    work_item_id,
                    namespace,
                    name,
                    artifact_type,
                    sha256,
                    generation,
                    resource_version,
                    phase,
                    resource_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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

    async def get(self, resource_id: UUID) -> Artifact:
        """Load an Artifact by ID."""

        row = self._connection.execute(
            "SELECT resource_json FROM artifacts WHERE id = ?",
            (str(resource_id),),
        ).fetchone()
        if row is None:
            raise ResourceNotFoundError(resource_id)
        return Artifact.model_validate_json(row["resource_json"])

    async def list(
        self,
        selector: ResourceSelector | None = None,
    ) -> tuple[Artifact, ...]:
        """List Artifacts matching optional selection criteria."""

        rows = self._connection.execute(
            "SELECT resource_json FROM artifacts ORDER BY namespace, name"
        ).fetchall()
        artifacts = tuple(
            Artifact.model_validate_json(row["resource_json"]) for row in rows
        )
        if selector is None:
            return artifacts
        return tuple(
            artifact for artifact in artifacts if self._matches(artifact, selector)
        )

    async def list_by_execution(self, execution_id: UUID) -> tuple[Artifact, ...]:
        """List Artifacts belonging to one Execution."""

        rows = self._connection.execute(
            """
            SELECT resource_json FROM artifacts
            WHERE execution_id = ?
            ORDER BY namespace, name
            """,
            (str(execution_id),),
        ).fetchall()
        return tuple(Artifact.model_validate_json(row["resource_json"]) for row in rows)

    async def list_by_work_item(self, work_item_id: UUID) -> tuple[Artifact, ...]:
        """List Artifacts belonging to one WorkItem."""

        rows = self._connection.execute(
            """
            SELECT resource_json FROM artifacts
            WHERE work_item_id = ?
            ORDER BY namespace, name
            """,
            (str(work_item_id),),
        ).fetchall()
        return tuple(Artifact.model_validate_json(row["resource_json"]) for row in rows)

    async def update_spec(
        self,
        resource_id: UUID,
        spec: ArtifactSpec,
        *,
        expected_resource_version: int,
    ) -> Artifact:
        """Reject Artifact spec changes and persist no-op updates."""

        artifact = await self.get(resource_id)
        updated = apply_artifact_spec_update(
            artifact,
            spec,
            expected_resource_version=expected_resource_version,
        )
        self._replace(updated, expected_resource_version=expected_resource_version)
        return updated

    async def update_status(
        self,
        resource_id: UUID,
        status: ArtifactStatus,
        *,
        expected_resource_version: int,
    ) -> Artifact:
        """Persist an Artifact status update."""

        artifact = await self.get(resource_id)
        updated = apply_artifact_status_update(
            artifact,
            status,
            expected_resource_version=expected_resource_version,
        )
        self._replace(updated, expected_resource_version=expected_resource_version)
        return updated

    def _create_schema(self) -> None:
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS artifacts (
                id TEXT PRIMARY KEY,
                execution_id TEXT NOT NULL,
                work_item_id TEXT,
                namespace TEXT NOT NULL,
                name TEXT NOT NULL,
                artifact_type TEXT NOT NULL,
                sha256 TEXT NOT NULL,
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
        artifact: Artifact,
        *,
        expected_resource_version: int,
    ) -> None:
        cursor = self._connection.execute(
            """
            UPDATE artifacts
            SET generation = ?,
                resource_version = ?,
                phase = ?,
                resource_json = ?
            WHERE id = ? AND resource_version = ?
            """,
            (
                artifact.metadata.generation,
                artifact.metadata.resource_version,
                artifact.status.phase,
                self._serialize(artifact),
                str(artifact.metadata.id),
                expected_resource_version,
            ),
        )
        if cursor.rowcount != 1:
            current = self._connection.execute(
                "SELECT resource_json FROM artifacts WHERE id = ?",
                (str(artifact.metadata.id),),
            ).fetchone()
            if current is None:
                raise ResourceNotFoundError(artifact.metadata.id)
            actual = Artifact.model_validate_json(current["resource_json"])
            raise ResourceConflictError(
                artifact.metadata.id,
                expected_resource_version,
                actual.metadata.resource_version,
            )
        self._connection.commit()

    def _row_values(
        self,
        artifact: Artifact,
    ) -> tuple[str, str, str | None, str, str, str, str, int, int, str, str]:
        return (
            str(artifact.metadata.id),
            str(artifact.spec.execution_ref.id),
            (
                str(artifact.spec.work_item_ref.id)
                if artifact.spec.work_item_ref is not None
                else None
            ),
            artifact.metadata.namespace,
            artifact.metadata.name,
            artifact.spec.artifact_type,
            artifact.spec.sha256,
            artifact.metadata.generation,
            artifact.metadata.resource_version,
            artifact.status.phase,
            self._serialize(artifact),
        )

    @staticmethod
    def _serialize(artifact: Artifact) -> str:
        return artifact.model_dump_json(by_alias=True)

    @staticmethod
    def _matches(artifact: Artifact, selector: ResourceSelector) -> bool:
        namespace_matches = (
            selector.namespace is None
            or artifact.metadata.namespace == selector.namespace
        )
        labels_match = all(
            artifact.metadata.labels.get(key) == value
            for key, value in selector.labels.items()
        )
        return namespace_matches and labels_match
