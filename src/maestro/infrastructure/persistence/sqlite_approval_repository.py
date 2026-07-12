"""SQLite persistence for Approval resources."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from uuid import UUID

from maestro.domain.approvals import (
    Approval,
    ApprovalRepository,
    ApprovalSpec,
    ApprovalStatus,
    apply_approval_spec_update,
    apply_approval_status_update,
)
from maestro.domain.exceptions import (
    ResourceAlreadyExistsError,
    ResourceConflictError,
    ResourceNotFoundError,
)
from maestro.domain.repositories import ResourceSelector


class SQLiteApprovalRepository(ApprovalRepository):
    """SQLite-backed Approval repository."""

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

    async def create(self, resource: Approval) -> Approval:
        """Persist a new Approval."""

        try:
            self._connection.execute(
                """
                INSERT INTO approvals (
                    id,
                    execution_id,
                    subject_kind,
                    subject_id,
                    subject_resource_version,
                    namespace,
                    name,
                    approval_type,
                    generation,
                    resource_version,
                    phase,
                    resource_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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

    async def get(self, resource_id: UUID) -> Approval:
        """Load an Approval by ID."""

        row = self._connection.execute(
            "SELECT resource_json FROM approvals WHERE id = ?",
            (str(resource_id),),
        ).fetchone()
        if row is None:
            raise ResourceNotFoundError(resource_id)
        return Approval.model_validate_json(row["resource_json"])

    async def list(
        self,
        selector: ResourceSelector | None = None,
    ) -> tuple[Approval, ...]:
        """List Approvals matching optional selection criteria."""

        rows = self._connection.execute(
            "SELECT resource_json FROM approvals ORDER BY namespace, name"
        ).fetchall()
        approvals = tuple(
            Approval.model_validate_json(row["resource_json"]) for row in rows
        )
        if selector is None:
            return approvals
        return tuple(
            approval for approval in approvals if self._matches(approval, selector)
        )

    async def list_by_execution(self, execution_id: UUID) -> tuple[Approval, ...]:
        """List Approvals belonging to one Execution."""

        rows = self._connection.execute(
            """
            SELECT resource_json FROM approvals
            WHERE execution_id = ?
            ORDER BY namespace, name
            """,
            (str(execution_id),),
        ).fetchall()
        return tuple(Approval.model_validate_json(row["resource_json"]) for row in rows)

    async def list_by_subject(
        self,
        subject_kind: str,
        subject_id: UUID,
    ) -> tuple[Approval, ...]:
        """List Approvals for one subject."""

        rows = self._connection.execute(
            """
            SELECT resource_json FROM approvals
            WHERE subject_kind = ? AND subject_id = ?
            ORDER BY namespace, name
            """,
            (subject_kind, str(subject_id)),
        ).fetchall()
        return tuple(Approval.model_validate_json(row["resource_json"]) for row in rows)

    async def update_spec(
        self,
        resource_id: UUID,
        spec: ApprovalSpec,
        *,
        expected_resource_version: int,
    ) -> Approval:
        """Reject Approval spec changes and persist no-op updates."""

        approval = await self.get(resource_id)
        updated = apply_approval_spec_update(
            approval,
            spec,
            expected_resource_version=expected_resource_version,
        )
        self._replace(updated, expected_resource_version=expected_resource_version)
        return updated

    async def update_status(
        self,
        resource_id: UUID,
        status: ApprovalStatus,
        *,
        expected_resource_version: int,
    ) -> Approval:
        """Persist an Approval status update."""

        approval = await self.get(resource_id)
        updated = apply_approval_status_update(
            approval,
            status,
            expected_resource_version=expected_resource_version,
        )
        self._replace(updated, expected_resource_version=expected_resource_version)
        return updated

    def _create_schema(self) -> None:
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS approvals (
                id TEXT PRIMARY KEY,
                execution_id TEXT NOT NULL,
                subject_kind TEXT NOT NULL,
                subject_id TEXT NOT NULL,
                subject_resource_version INTEGER NOT NULL,
                namespace TEXT NOT NULL,
                name TEXT NOT NULL,
                approval_type TEXT NOT NULL,
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
        approval: Approval,
        *,
        expected_resource_version: int,
    ) -> None:
        cursor = self._connection.execute(
            """
            UPDATE approvals
            SET resource_version = ?,
                phase = ?,
                resource_json = ?
            WHERE id = ? AND resource_version = ?
            """,
            (
                approval.metadata.resource_version,
                approval.status.phase,
                self._serialize(approval),
                str(approval.metadata.id),
                expected_resource_version,
            ),
        )
        if cursor.rowcount != 1:
            current = self._connection.execute(
                "SELECT resource_json FROM approvals WHERE id = ?",
                (str(approval.metadata.id),),
            ).fetchone()
            if current is None:
                raise ResourceNotFoundError(approval.metadata.id)
            actual = Approval.model_validate_json(current["resource_json"])
            raise ResourceConflictError(
                approval.metadata.id,
                expected_resource_version,
                actual.metadata.resource_version,
            )
        self._connection.commit()

    def _row_values(
        self,
        approval: Approval,
    ) -> tuple[str, str, str, str, int, str, str, str, int, int, str, str]:
        return (
            str(approval.metadata.id),
            str(approval.spec.execution_ref.id),
            approval.spec.subject_ref.kind,
            str(approval.spec.subject_ref.id),
            approval.spec.subject_ref.resource_version,
            approval.metadata.namespace,
            approval.metadata.name,
            approval.spec.approval_type,
            approval.metadata.generation,
            approval.metadata.resource_version,
            approval.status.phase,
            self._serialize(approval),
        )

    @staticmethod
    def _serialize(approval: Approval) -> str:
        return approval.model_dump_json(by_alias=True)

    @staticmethod
    def _matches(approval: Approval, selector: ResourceSelector) -> bool:
        namespace_matches = (
            selector.namespace is None
            or approval.metadata.namespace == selector.namespace
        )
        labels_match = all(
            approval.metadata.labels.get(key) == value
            for key, value in selector.labels.items()
        )
        return namespace_matches and labels_match
