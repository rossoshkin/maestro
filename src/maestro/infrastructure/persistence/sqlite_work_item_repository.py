"""SQLite persistence for WorkItem resources."""

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
from maestro.domain.repositories import ResourceSelector
from maestro.domain.work_items import (
    WorkItem,
    WorkItemRepository,
    WorkItemSpec,
    WorkItemStatus,
    apply_work_item_spec_update,
    apply_work_item_status_update,
)


class SQLiteWorkItemRepository(WorkItemRepository):
    """SQLite-backed WorkItem repository."""

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

    async def create(self, resource: WorkItem) -> WorkItem:
        """Persist a new WorkItem."""

        try:
            self._connection.execute(
                """
                INSERT INTO work_items (
                    id,
                    execution_id,
                    plan_id,
                    plan_work_item_id,
                    namespace,
                    name,
                    generation,
                    resource_version,
                    phase,
                    resource_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                self._row_values(resource),
            )
            self._connection.commit()
        except sqlite3.IntegrityError as error:
            raise ResourceAlreadyExistsError(
                resource.kind,
                resource.metadata.namespace,
                f"{resource.metadata.name}/{resource.spec.plan_work_item_id}",
            ) from error
        return resource

    async def get(self, resource_id: UUID) -> WorkItem:
        """Load a WorkItem by ID."""

        row = self._connection.execute(
            "SELECT resource_json FROM work_items WHERE id = ?",
            (str(resource_id),),
        ).fetchone()
        if row is None:
            raise ResourceNotFoundError(resource_id)
        return WorkItem.model_validate_json(row["resource_json"])

    async def get_by_plan_work_item_id(
        self,
        plan_id: UUID,
        plan_work_item_id: str,
    ) -> WorkItem:
        """Load a WorkItem by Plan ID and planner-provided Work Item ID."""

        row = self._connection.execute(
            """
            SELECT resource_json FROM work_items
            WHERE plan_id = ? AND plan_work_item_id = ?
            """,
            (str(plan_id), plan_work_item_id),
        ).fetchone()
        if row is None:
            raise ResourceNameNotFoundError(
                "WorkItem",
                "plan",
                f"{plan_id}/{plan_work_item_id}",
            )
        return WorkItem.model_validate_json(row["resource_json"])

    async def list(
        self,
        selector: ResourceSelector | None = None,
    ) -> tuple[WorkItem, ...]:
        """List WorkItems matching optional selection criteria."""

        rows = self._connection.execute(
            "SELECT resource_json FROM work_items ORDER BY namespace, name"
        ).fetchall()
        work_items = tuple(
            WorkItem.model_validate_json(row["resource_json"]) for row in rows
        )
        if selector is None:
            return work_items
        return tuple(
            work_item for work_item in work_items if self._matches(work_item, selector)
        )

    async def list_by_execution(self, execution_id: UUID) -> tuple[WorkItem, ...]:
        """List WorkItems belonging to one Execution."""

        rows = self._connection.execute(
            """
            SELECT resource_json FROM work_items
            WHERE execution_id = ?
            ORDER BY name
            """,
            (str(execution_id),),
        ).fetchall()
        return tuple(WorkItem.model_validate_json(row["resource_json"]) for row in rows)

    async def list_by_plan(self, plan_id: UUID) -> tuple[WorkItem, ...]:
        """List WorkItems produced by one Plan revision."""

        rows = self._connection.execute(
            """
            SELECT resource_json FROM work_items
            WHERE plan_id = ?
            ORDER BY plan_work_item_id
            """,
            (str(plan_id),),
        ).fetchall()
        return tuple(WorkItem.model_validate_json(row["resource_json"]) for row in rows)

    async def update_spec(
        self,
        resource_id: UUID,
        spec: WorkItemSpec,
        *,
        expected_resource_version: int,
    ) -> WorkItem:
        """Persist a limited WorkItem spec update."""

        work_item = await self.get(resource_id)
        updated = apply_work_item_spec_update(
            work_item,
            spec,
            expected_resource_version=expected_resource_version,
        )
        self._replace(updated, expected_resource_version=expected_resource_version)
        return updated

    async def update_status(
        self,
        resource_id: UUID,
        status: WorkItemStatus,
        *,
        expected_resource_version: int,
    ) -> WorkItem:
        """Persist a WorkItem status update."""

        work_item = await self.get(resource_id)
        updated = apply_work_item_status_update(
            work_item,
            status,
            expected_resource_version=expected_resource_version,
        )
        self._replace(updated, expected_resource_version=expected_resource_version)
        return updated

    def _create_schema(self) -> None:
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS work_items (
                id TEXT PRIMARY KEY,
                execution_id TEXT NOT NULL,
                plan_id TEXT NOT NULL,
                plan_work_item_id TEXT NOT NULL,
                namespace TEXT NOT NULL,
                name TEXT NOT NULL,
                generation INTEGER NOT NULL,
                resource_version INTEGER NOT NULL,
                phase TEXT NOT NULL,
                resource_json TEXT NOT NULL,
                UNIQUE(namespace, name),
                UNIQUE(plan_id, plan_work_item_id)
            )
            """
        )
        self._connection.commit()

    def _replace(
        self,
        work_item: WorkItem,
        *,
        expected_resource_version: int,
    ) -> None:
        cursor = self._connection.execute(
            """
            UPDATE work_items
            SET generation = ?,
                resource_version = ?,
                phase = ?,
                resource_json = ?
            WHERE id = ? AND resource_version = ?
            """,
            (
                work_item.metadata.generation,
                work_item.metadata.resource_version,
                work_item.status.phase,
                self._serialize(work_item),
                str(work_item.metadata.id),
                expected_resource_version,
            ),
        )
        if cursor.rowcount != 1:
            current = self._connection.execute(
                "SELECT resource_json FROM work_items WHERE id = ?",
                (str(work_item.metadata.id),),
            ).fetchone()
            if current is None:
                raise ResourceNotFoundError(work_item.metadata.id)
            actual = WorkItem.model_validate_json(current["resource_json"])
            raise ResourceConflictError(
                work_item.metadata.id,
                expected_resource_version,
                actual.metadata.resource_version,
            )
        self._connection.commit()

    def _row_values(
        self,
        work_item: WorkItem,
    ) -> tuple[str, str, str, str, str, str, int, int, str, str]:
        return (
            str(work_item.metadata.id),
            str(work_item.spec.execution_ref.id),
            str(work_item.spec.plan_ref.id),
            work_item.spec.plan_work_item_id,
            work_item.metadata.namespace,
            work_item.metadata.name,
            work_item.metadata.generation,
            work_item.metadata.resource_version,
            work_item.status.phase,
            self._serialize(work_item),
        )

    @staticmethod
    def _serialize(work_item: WorkItem) -> str:
        return work_item.model_dump_json(by_alias=True)

    @staticmethod
    def _matches(work_item: WorkItem, selector: ResourceSelector) -> bool:
        namespace_matches = (
            selector.namespace is None
            or work_item.metadata.namespace == selector.namespace
        )
        labels_match = all(
            work_item.metadata.labels.get(key) == value
            for key, value in selector.labels.items()
        )
        return namespace_matches and labels_match
