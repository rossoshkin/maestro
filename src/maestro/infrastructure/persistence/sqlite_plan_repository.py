"""SQLite persistence for Plan resources."""

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
from maestro.domain.plans import (
    Plan,
    PlanPhase,
    PlanRepository,
    PlanSpec,
    PlanStatus,
    apply_plan_spec_update,
    apply_plan_status_update,
)
from maestro.domain.repositories import ResourceSelector


class SQLitePlanRepository(PlanRepository):
    """SQLite-backed Plan repository."""

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

    async def create(self, resource: Plan) -> Plan:
        """Persist a new Plan revision."""

        try:
            self._connection.execute(
                """
                INSERT INTO plans (
                    id,
                    execution_id,
                    namespace,
                    name,
                    version,
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
                f"{resource.metadata.name}/v{resource.spec.version}",
            ) from error
        return resource

    async def get(self, resource_id: UUID) -> Plan:
        """Load a Plan by ID."""

        row = self._connection.execute(
            "SELECT resource_json FROM plans WHERE id = ?",
            (str(resource_id),),
        ).fetchone()
        if row is None:
            raise ResourceNotFoundError(resource_id)
        return Plan.model_validate_json(row["resource_json"])

    async def get_by_execution_version(
        self,
        execution_id: UUID,
        version: int,
    ) -> Plan:
        """Load a Plan by Execution ID and Plan version."""

        row = self._connection.execute(
            """
            SELECT resource_json FROM plans
            WHERE execution_id = ? AND version = ?
            """,
            (str(execution_id), version),
        ).fetchone()
        if row is None:
            raise ResourceNameNotFoundError(
                "Plan",
                "execution",
                f"{execution_id}/v{version}",
            )
        return Plan.model_validate_json(row["resource_json"])

    async def get_approved_for_execution(self, execution_id: UUID) -> Plan | None:
        """Load the approved Plan for an Execution, if one exists."""

        row = self._connection.execute(
            """
            SELECT resource_json FROM plans
            WHERE execution_id = ? AND phase = ?
            """,
            (str(execution_id), PlanPhase.APPROVED),
        ).fetchone()
        if row is None:
            return None
        return Plan.model_validate_json(row["resource_json"])

    async def list(
        self,
        selector: ResourceSelector | None = None,
    ) -> tuple[Plan, ...]:
        """List Plans matching optional selection criteria."""

        rows = self._connection.execute(
            "SELECT resource_json FROM plans ORDER BY namespace, name"
        ).fetchall()
        plans = tuple(Plan.model_validate_json(row["resource_json"]) for row in rows)
        if selector is None:
            return plans
        return tuple(plan for plan in plans if self._matches(plan, selector))

    async def list_by_execution(self, execution_id: UUID) -> tuple[Plan, ...]:
        """List Plan revisions belonging to one Execution."""

        rows = self._connection.execute(
            """
            SELECT resource_json FROM plans
            WHERE execution_id = ?
            ORDER BY version
            """,
            (str(execution_id),),
        ).fetchall()
        return tuple(Plan.model_validate_json(row["resource_json"]) for row in rows)

    async def update_spec(
        self,
        resource_id: UUID,
        spec: PlanSpec,
        *,
        expected_resource_version: int,
    ) -> Plan:
        """Reject actual Plan spec changes and persist no-op updates."""

        plan = await self.get(resource_id)
        updated = apply_plan_spec_update(
            plan,
            spec,
            expected_resource_version=expected_resource_version,
        )
        self._replace(updated, expected_resource_version=expected_resource_version)
        return updated

    async def update_status(
        self,
        resource_id: UUID,
        status: PlanStatus,
        *,
        expected_resource_version: int,
    ) -> Plan:
        """Persist a Plan status update."""

        plan = await self.get(resource_id)
        updated = apply_plan_status_update(
            plan,
            status,
            expected_resource_version=expected_resource_version,
        )
        self._replace(updated, expected_resource_version=expected_resource_version)
        return updated

    def _create_schema(self) -> None:
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS plans (
                id TEXT PRIMARY KEY,
                execution_id TEXT NOT NULL,
                namespace TEXT NOT NULL,
                name TEXT NOT NULL,
                version INTEGER NOT NULL,
                generation INTEGER NOT NULL,
                resource_version INTEGER NOT NULL,
                phase TEXT NOT NULL,
                resource_json TEXT NOT NULL,
                UNIQUE(namespace, name),
                UNIQUE(execution_id, version)
            )
            """
        )
        self._connection.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS
                plans_one_approved_per_execution
            ON plans(execution_id)
            WHERE phase = 'Approved'
            """
        )
        self._connection.commit()

    def _replace(
        self,
        plan: Plan,
        *,
        expected_resource_version: int,
    ) -> None:
        try:
            cursor = self._connection.execute(
                """
                UPDATE plans
                SET generation = ?,
                    resource_version = ?,
                    phase = ?,
                    resource_json = ?
                WHERE id = ? AND resource_version = ?
                """,
                (
                    plan.metadata.generation,
                    plan.metadata.resource_version,
                    plan.status.phase,
                    self._serialize(plan),
                    str(plan.metadata.id),
                    expected_resource_version,
                ),
            )
        except sqlite3.IntegrityError as error:
            raise ResourceAlreadyExistsError(
                "Plan",
                plan.metadata.namespace,
                f"approved/{plan.spec.execution_ref.id}",
            ) from error

        if cursor.rowcount != 1:
            current = self._connection.execute(
                "SELECT resource_json FROM plans WHERE id = ?",
                (str(plan.metadata.id),),
            ).fetchone()
            if current is None:
                raise ResourceNotFoundError(plan.metadata.id)
            actual = Plan.model_validate_json(current["resource_json"])
            raise ResourceConflictError(
                plan.metadata.id,
                expected_resource_version,
                actual.metadata.resource_version,
            )
        self._connection.commit()

    def _row_values(
        self,
        plan: Plan,
    ) -> tuple[str, str, str, str, int, int, int, str, str]:
        return (
            str(plan.metadata.id),
            str(plan.spec.execution_ref.id),
            plan.metadata.namespace,
            plan.metadata.name,
            plan.spec.version,
            plan.metadata.generation,
            plan.metadata.resource_version,
            plan.status.phase,
            self._serialize(plan),
        )

    @staticmethod
    def _serialize(plan: Plan) -> str:
        return plan.model_dump_json(by_alias=True)

    @staticmethod
    def _matches(plan: Plan, selector: ResourceSelector) -> bool:
        namespace_matches = (
            selector.namespace is None or plan.metadata.namespace == selector.namespace
        )
        labels_match = all(
            plan.metadata.labels.get(key) == value
            for key, value in selector.labels.items()
        )
        return namespace_matches and labels_match
