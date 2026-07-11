"""SQLite persistence for Provider resources."""

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
from maestro.domain.providers import (
    Provider,
    ProviderRepository,
    ProviderSpec,
    ProviderStatus,
    apply_provider_spec_update,
    apply_provider_status_update,
)
from maestro.domain.repositories import ResourceSelector


class SQLiteProviderRepository(ProviderRepository):
    """SQLite-backed Provider repository."""

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

    async def create(self, resource: Provider) -> Provider:
        """Persist a new Provider."""

        try:
            self._connection.execute(
                """
                INSERT INTO providers (
                    id,
                    namespace,
                    name,
                    provider_type,
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

    async def get(self, resource_id: UUID) -> Provider:
        """Load a Provider by ID."""

        row = self._connection.execute(
            "SELECT resource_json FROM providers WHERE id = ?",
            (str(resource_id),),
        ).fetchone()
        if row is None:
            raise ResourceNotFoundError(resource_id)
        return Provider.model_validate_json(row["resource_json"])

    async def get_by_name(self, namespace: str, name: str) -> Provider:
        """Load a Provider by namespace and name."""

        row = self._connection.execute(
            """
            SELECT resource_json FROM providers
            WHERE namespace = ? AND name = ?
            """,
            (namespace, name),
        ).fetchone()
        if row is None:
            raise ResourceNameNotFoundError("Provider", namespace, name)
        return Provider.model_validate_json(row["resource_json"])

    async def list(
        self,
        selector: ResourceSelector | None = None,
    ) -> tuple[Provider, ...]:
        """List Providers matching optional selection criteria."""

        rows = self._connection.execute(
            "SELECT resource_json FROM providers ORDER BY namespace, name"
        ).fetchall()
        providers = tuple(
            Provider.model_validate_json(row["resource_json"]) for row in rows
        )
        if selector is None:
            return providers
        return tuple(
            provider for provider in providers if self._matches(provider, selector)
        )

    async def update_spec(
        self,
        resource_id: UUID,
        spec: ProviderSpec,
        *,
        expected_resource_version: int,
    ) -> Provider:
        """Persist a Provider spec update."""

        provider = await self.get(resource_id)
        updated = apply_provider_spec_update(
            provider,
            spec,
            expected_resource_version=expected_resource_version,
        )
        self._replace(updated, expected_resource_version=expected_resource_version)
        return updated

    async def update_status(
        self,
        resource_id: UUID,
        status: ProviderStatus,
        *,
        expected_resource_version: int,
    ) -> Provider:
        """Persist a Provider status update."""

        provider = await self.get(resource_id)
        updated = apply_provider_status_update(
            provider,
            status,
            expected_resource_version=expected_resource_version,
        )
        self._replace(updated, expected_resource_version=expected_resource_version)
        return updated

    def _create_schema(self) -> None:
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS providers (
                id TEXT PRIMARY KEY,
                namespace TEXT NOT NULL,
                name TEXT NOT NULL,
                provider_type TEXT NOT NULL,
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
        provider: Provider,
        *,
        expected_resource_version: int,
    ) -> None:
        cursor = self._connection.execute(
            """
            UPDATE providers
            SET provider_type = ?,
                generation = ?,
                resource_version = ?,
                phase = ?,
                resource_json = ?
            WHERE id = ? AND resource_version = ?
            """,
            (
                provider.spec.provider_type,
                provider.metadata.generation,
                provider.metadata.resource_version,
                provider.status.phase,
                self._serialize(provider),
                str(provider.metadata.id),
                expected_resource_version,
            ),
        )
        if cursor.rowcount != 1:
            current = self._connection.execute(
                "SELECT resource_json FROM providers WHERE id = ?",
                (str(provider.metadata.id),),
            ).fetchone()
            if current is None:
                raise ResourceNotFoundError(provider.metadata.id)
            actual = Provider.model_validate_json(current["resource_json"])
            raise ResourceConflictError(
                provider.metadata.id,
                expected_resource_version,
                actual.metadata.resource_version,
            )
        self._connection.commit()

    def _row_values(
        self,
        provider: Provider,
    ) -> tuple[str, str, str, str, int, int, str, str]:
        return (
            str(provider.metadata.id),
            provider.metadata.namespace,
            provider.metadata.name,
            provider.spec.provider_type,
            provider.metadata.generation,
            provider.metadata.resource_version,
            provider.status.phase,
            self._serialize(provider),
        )

    @staticmethod
    def _serialize(provider: Provider) -> str:
        return provider.model_dump_json(by_alias=True)

    @staticmethod
    def _matches(provider: Provider, selector: ResourceSelector) -> bool:
        namespace_matches = (
            selector.namespace is None
            or provider.metadata.namespace == selector.namespace
        )
        labels_match = all(
            provider.metadata.labels.get(key) == value
            for key, value in selector.labels.items()
        )
        return namespace_matches and labels_match
