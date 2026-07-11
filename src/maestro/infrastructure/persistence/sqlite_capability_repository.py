"""SQLite persistence for Capability resources."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from uuid import UUID

from maestro.domain.capabilities import (
    Capability,
    CapabilityBinding,
    CapabilityBindingPhase,
    CapabilityBindingRepository,
    CapabilityBindingSpec,
    CapabilityBindingStatus,
    CapabilityRepository,
    CapabilitySpec,
    CapabilityStatus,
    apply_capability_binding_spec_update,
    apply_capability_binding_status_update,
    apply_capability_spec_update,
    apply_capability_status_update,
)
from maestro.domain.exceptions import (
    ResourceAlreadyExistsError,
    ResourceConflictError,
    ResourceNameNotFoundError,
    ResourceNotFoundError,
)
from maestro.domain.repositories import ResourceSelector


class SQLiteCapabilityRepository(CapabilityRepository):
    """SQLite-backed Capability repository."""

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

    async def create(self, resource: Capability) -> Capability:
        """Persist a new Capability."""

        try:
            self._connection.execute(
                """
                INSERT INTO capabilities (
                    id,
                    namespace,
                    name,
                    canonical_name,
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
                resource.spec.canonical_name,
            ) from error
        return resource

    async def get(self, resource_id: UUID) -> Capability:
        """Load a Capability by ID."""

        row = self._connection.execute(
            "SELECT resource_json FROM capabilities WHERE id = ?",
            (str(resource_id),),
        ).fetchone()
        if row is None:
            raise ResourceNotFoundError(resource_id)
        return Capability.model_validate_json(row["resource_json"])

    async def get_by_canonical_name(
        self,
        namespace: str,
        canonical_name: str,
    ) -> Capability:
        """Load a Capability by namespace and canonical name."""

        row = self._connection.execute(
            """
            SELECT resource_json FROM capabilities
            WHERE namespace = ? AND canonical_name = ?
            """,
            (namespace, canonical_name),
        ).fetchone()
        if row is None:
            raise ResourceNameNotFoundError("Capability", namespace, canonical_name)
        return Capability.model_validate_json(row["resource_json"])

    async def list(
        self,
        selector: ResourceSelector | None = None,
    ) -> tuple[Capability, ...]:
        """List Capabilities matching optional selection criteria."""

        rows = self._connection.execute(
            "SELECT resource_json FROM capabilities ORDER BY namespace, name"
        ).fetchall()
        capabilities = tuple(
            Capability.model_validate_json(row["resource_json"]) for row in rows
        )
        if selector is None:
            return capabilities
        return tuple(
            capability
            for capability in capabilities
            if self._matches(capability, selector)
        )

    async def update_spec(
        self,
        resource_id: UUID,
        spec: CapabilitySpec,
        *,
        expected_resource_version: int,
    ) -> Capability:
        """Persist a Capability spec update."""

        capability = await self.get(resource_id)
        updated = apply_capability_spec_update(
            capability,
            spec,
            expected_resource_version=expected_resource_version,
        )
        self._replace(updated, expected_resource_version=expected_resource_version)
        return updated

    async def update_status(
        self,
        resource_id: UUID,
        status: CapabilityStatus,
        *,
        expected_resource_version: int,
    ) -> Capability:
        """Persist a Capability status update."""

        capability = await self.get(resource_id)
        updated = apply_capability_status_update(
            capability,
            status,
            expected_resource_version=expected_resource_version,
        )
        self._replace(updated, expected_resource_version=expected_resource_version)
        return updated

    def _create_schema(self) -> None:
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS capabilities (
                id TEXT PRIMARY KEY,
                namespace TEXT NOT NULL,
                name TEXT NOT NULL,
                canonical_name TEXT NOT NULL,
                generation INTEGER NOT NULL,
                resource_version INTEGER NOT NULL,
                phase TEXT NOT NULL,
                resource_json TEXT NOT NULL,
                UNIQUE(namespace, name),
                UNIQUE(namespace, canonical_name)
            )
            """
        )
        self._connection.commit()

    def _replace(
        self,
        capability: Capability,
        *,
        expected_resource_version: int,
    ) -> None:
        cursor = self._connection.execute(
            """
            UPDATE capabilities
            SET canonical_name = ?,
                generation = ?,
                resource_version = ?,
                phase = ?,
                resource_json = ?
            WHERE id = ? AND resource_version = ?
            """,
            (
                capability.spec.canonical_name,
                capability.metadata.generation,
                capability.metadata.resource_version,
                capability.status.phase,
                self._serialize(capability),
                str(capability.metadata.id),
                expected_resource_version,
            ),
        )
        if cursor.rowcount != 1:
            current = self._connection.execute(
                "SELECT resource_json FROM capabilities WHERE id = ?",
                (str(capability.metadata.id),),
            ).fetchone()
            if current is None:
                raise ResourceNotFoundError(capability.metadata.id)
            actual = Capability.model_validate_json(current["resource_json"])
            raise ResourceConflictError(
                capability.metadata.id,
                expected_resource_version,
                actual.metadata.resource_version,
            )
        self._connection.commit()

    def _row_values(
        self,
        capability: Capability,
    ) -> tuple[str, str, str, str, int, int, str, str]:
        return (
            str(capability.metadata.id),
            capability.metadata.namespace,
            capability.metadata.name,
            capability.spec.canonical_name,
            capability.metadata.generation,
            capability.metadata.resource_version,
            capability.status.phase,
            self._serialize(capability),
        )

    @staticmethod
    def _serialize(capability: Capability) -> str:
        return capability.model_dump_json(by_alias=True)

    @staticmethod
    def _matches(capability: Capability, selector: ResourceSelector) -> bool:
        namespace_matches = (
            selector.namespace is None
            or capability.metadata.namespace == selector.namespace
        )
        labels_match = all(
            capability.metadata.labels.get(key) == value
            for key, value in selector.labels.items()
        )
        return namespace_matches and labels_match


class SQLiteCapabilityBindingRepository(CapabilityBindingRepository):
    """SQLite-backed CapabilityBinding repository."""

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

    async def create(self, resource: CapabilityBinding) -> CapabilityBinding:
        """Persist a new CapabilityBinding."""

        try:
            self._connection.execute(
                """
                INSERT INTO capability_bindings (
                    id,
                    namespace,
                    name,
                    generation,
                    resource_version,
                    phase,
                    resource_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
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

    async def get(self, resource_id: UUID) -> CapabilityBinding:
        """Load a CapabilityBinding by ID."""

        row = self._connection.execute(
            "SELECT resource_json FROM capability_bindings WHERE id = ?",
            (str(resource_id),),
        ).fetchone()
        if row is None:
            raise ResourceNotFoundError(resource_id)
        return CapabilityBinding.model_validate_json(row["resource_json"])

    async def list(
        self,
        selector: ResourceSelector | None = None,
    ) -> tuple[CapabilityBinding, ...]:
        """List CapabilityBindings matching optional selection criteria."""

        rows = self._connection.execute(
            "SELECT resource_json FROM capability_bindings ORDER BY namespace, name"
        ).fetchall()
        bindings = tuple(
            CapabilityBinding.model_validate_json(row["resource_json"]) for row in rows
        )
        if selector is None:
            return bindings
        return tuple(
            binding for binding in bindings if self._matches(binding, selector)
        )

    async def list_ready(self, namespace: str) -> tuple[CapabilityBinding, ...]:
        """List Ready CapabilityBindings in one namespace."""

        rows = self._connection.execute(
            """
            SELECT resource_json FROM capability_bindings
            WHERE namespace = ? AND phase = ?
            ORDER BY name
            """,
            (namespace, CapabilityBindingPhase.READY),
        ).fetchall()
        return tuple(
            CapabilityBinding.model_validate_json(row["resource_json"]) for row in rows
        )

    async def update_spec(
        self,
        resource_id: UUID,
        spec: CapabilityBindingSpec,
        *,
        expected_resource_version: int,
    ) -> CapabilityBinding:
        """Persist a CapabilityBinding spec update."""

        binding = await self.get(resource_id)
        updated = apply_capability_binding_spec_update(
            binding,
            spec,
            expected_resource_version=expected_resource_version,
        )
        self._replace(updated, expected_resource_version=expected_resource_version)
        return updated

    async def update_status(
        self,
        resource_id: UUID,
        status: CapabilityBindingStatus,
        *,
        expected_resource_version: int,
    ) -> CapabilityBinding:
        """Persist a CapabilityBinding status update."""

        binding = await self.get(resource_id)
        updated = apply_capability_binding_status_update(
            binding,
            status,
            expected_resource_version=expected_resource_version,
        )
        self._replace(updated, expected_resource_version=expected_resource_version)
        return updated

    def _create_schema(self) -> None:
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS capability_bindings (
                id TEXT PRIMARY KEY,
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
        binding: CapabilityBinding,
        *,
        expected_resource_version: int,
    ) -> None:
        cursor = self._connection.execute(
            """
            UPDATE capability_bindings
            SET generation = ?,
                resource_version = ?,
                phase = ?,
                resource_json = ?
            WHERE id = ? AND resource_version = ?
            """,
            (
                binding.metadata.generation,
                binding.metadata.resource_version,
                binding.status.phase,
                self._serialize(binding),
                str(binding.metadata.id),
                expected_resource_version,
            ),
        )
        if cursor.rowcount != 1:
            current = self._connection.execute(
                "SELECT resource_json FROM capability_bindings WHERE id = ?",
                (str(binding.metadata.id),),
            ).fetchone()
            if current is None:
                raise ResourceNotFoundError(binding.metadata.id)
            actual = CapabilityBinding.model_validate_json(current["resource_json"])
            raise ResourceConflictError(
                binding.metadata.id,
                expected_resource_version,
                actual.metadata.resource_version,
            )
        self._connection.commit()

    def _row_values(
        self,
        binding: CapabilityBinding,
    ) -> tuple[str, str, str, int, int, str, str]:
        return (
            str(binding.metadata.id),
            binding.metadata.namespace,
            binding.metadata.name,
            binding.metadata.generation,
            binding.metadata.resource_version,
            binding.status.phase,
            self._serialize(binding),
        )

    @staticmethod
    def _serialize(binding: CapabilityBinding) -> str:
        return binding.model_dump_json(by_alias=True)

    @staticmethod
    def _matches(binding: CapabilityBinding, selector: ResourceSelector) -> bool:
        namespace_matches = (
            selector.namespace is None
            or binding.metadata.namespace == selector.namespace
        )
        labels_match = all(
            binding.metadata.labels.get(key) == value
            for key, value in selector.labels.items()
        )
        return namespace_matches and labels_match
