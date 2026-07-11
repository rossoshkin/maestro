"""SQLite persistence for Agent resources."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from uuid import UUID

from maestro.domain.agents import (
    Agent,
    AgentRepository,
    AgentSpec,
    AgentStatus,
    apply_agent_spec_update,
    apply_agent_status_update,
)
from maestro.domain.exceptions import (
    ResourceAlreadyExistsError,
    ResourceConflictError,
    ResourceNotFoundError,
)
from maestro.domain.repositories import ResourceSelector


class SQLiteAgentRepository(AgentRepository):
    """SQLite-backed Agent repository."""

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

    async def create(self, resource: Agent) -> Agent:
        """Persist a new Agent."""

        try:
            self._connection.execute(
                """
                INSERT INTO agents (
                    id,
                    provider_name,
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

    async def get(self, resource_id: UUID) -> Agent:
        """Load an Agent by ID."""

        row = self._connection.execute(
            "SELECT resource_json FROM agents WHERE id = ?",
            (str(resource_id),),
        ).fetchone()
        if row is None:
            raise ResourceNotFoundError(resource_id)
        return Agent.model_validate_json(row["resource_json"])

    async def list(
        self,
        selector: ResourceSelector | None = None,
    ) -> tuple[Agent, ...]:
        """List Agents matching optional selection criteria."""

        rows = self._connection.execute(
            "SELECT resource_json FROM agents ORDER BY namespace, name"
        ).fetchall()
        agents = tuple(Agent.model_validate_json(row["resource_json"]) for row in rows)
        if selector is None:
            return agents
        return tuple(agent for agent in agents if self._matches(agent, selector))

    async def list_by_provider(
        self,
        namespace: str,
        provider_name: str,
    ) -> tuple[Agent, ...]:
        """List Agents bound to a Provider."""

        rows = self._connection.execute(
            """
            SELECT resource_json FROM agents
            WHERE namespace = ? AND provider_name = ?
            ORDER BY name
            """,
            (namespace, provider_name),
        ).fetchall()
        return tuple(Agent.model_validate_json(row["resource_json"]) for row in rows)

    async def list_compatible_with_role(
        self,
        namespace: str,
        role_name: str,
        role_version: str,
    ) -> tuple[Agent, ...]:
        """List Agents that declare support for a Role version."""

        agents = await self.list(ResourceSelector(namespace=namespace))
        return tuple(
            agent
            for agent in agents
            if any(
                supported_role.name == role_name
                and role_version in supported_role.versions
                for supported_role in agent.spec.supported_roles
            )
        )

    async def update_spec(
        self,
        resource_id: UUID,
        spec: AgentSpec,
        *,
        expected_resource_version: int,
    ) -> Agent:
        """Persist an Agent spec update."""

        agent = await self.get(resource_id)
        updated = apply_agent_spec_update(
            agent,
            spec,
            expected_resource_version=expected_resource_version,
        )
        self._replace(updated, expected_resource_version=expected_resource_version)
        return updated

    async def update_status(
        self,
        resource_id: UUID,
        status: AgentStatus,
        *,
        expected_resource_version: int,
    ) -> Agent:
        """Persist an Agent status update."""

        agent = await self.get(resource_id)
        updated = apply_agent_status_update(
            agent,
            status,
            expected_resource_version=expected_resource_version,
        )
        self._replace(updated, expected_resource_version=expected_resource_version)
        return updated

    def _create_schema(self) -> None:
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS agents (
                id TEXT PRIMARY KEY,
                provider_name TEXT NOT NULL,
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
        agent: Agent,
        *,
        expected_resource_version: int,
    ) -> None:
        cursor = self._connection.execute(
            """
            UPDATE agents
            SET provider_name = ?,
                generation = ?,
                resource_version = ?,
                phase = ?,
                resource_json = ?
            WHERE id = ? AND resource_version = ?
            """,
            (
                agent.spec.provider_ref.name,
                agent.metadata.generation,
                agent.metadata.resource_version,
                agent.status.phase,
                self._serialize(agent),
                str(agent.metadata.id),
                expected_resource_version,
            ),
        )
        if cursor.rowcount != 1:
            current = self._connection.execute(
                "SELECT resource_json FROM agents WHERE id = ?",
                (str(agent.metadata.id),),
            ).fetchone()
            if current is None:
                raise ResourceNotFoundError(agent.metadata.id)
            actual = Agent.model_validate_json(current["resource_json"])
            raise ResourceConflictError(
                agent.metadata.id,
                expected_resource_version,
                actual.metadata.resource_version,
            )
        self._connection.commit()

    def _row_values(
        self,
        agent: Agent,
    ) -> tuple[str, str, str, str, int, int, str, str]:
        return (
            str(agent.metadata.id),
            agent.spec.provider_ref.name,
            agent.metadata.namespace,
            agent.metadata.name,
            agent.metadata.generation,
            agent.metadata.resource_version,
            agent.status.phase,
            self._serialize(agent),
        )

    @staticmethod
    def _serialize(agent: Agent) -> str:
        return agent.model_dump_json(by_alias=True)

    @staticmethod
    def _matches(agent: Agent, selector: ResourceSelector) -> bool:
        namespace_matches = (
            selector.namespace is None or agent.metadata.namespace == selector.namespace
        )
        labels_match = all(
            agent.metadata.labels.get(key) == value
            for key, value in selector.labels.items()
        )
        return namespace_matches and labels_match
