"""Tests for SQLite Agent persistence."""

import asyncio

import pytest

from maestro.domain import ResourceSelector
from maestro.domain.agents import (
    Agent,
    AgentCapacity,
    AgentPhase,
    AgentProviderReference,
    AgentScheduling,
    AgentSpec,
    AgentStatus,
    AgentSupportedRole,
)
from maestro.domain.exceptions import ResourceAlreadyExistsError, ResourceConflictError
from maestro.infrastructure.persistence import SQLiteAgentRepository


def valid_agent_spec() -> AgentSpec:
    """Build a valid AgentSpec for persistence tests."""

    return AgentSpec(
        providerRef=AgentProviderReference(name="ollama-local"),
        model="qwen2.5-coder:14b",
        supportedRoles=(
            AgentSupportedRole(name="coding", versions=("v1alpha1", "v1alpha2")),
            AgentSupportedRole(name="reviewer", versions=("v1alpha1",)),
        ),
        capacity=AgentCapacity(maxConcurrentAssignments=2),
        scheduling=AgentScheduling(priority=100),
    )


def valid_agent(*, name: str = "coder-local") -> Agent:
    """Build a valid Agent resource."""

    return Agent.new(name=name, spec=valid_agent_spec())


def test_agent_persistence_round_trip(tmp_path) -> None:
    async def scenario() -> None:
        repository = SQLiteAgentRepository(tmp_path / "maestro.db")
        agent = await repository.create(valid_agent())
        loaded = await repository.get(agent.metadata.id)

        assert loaded == agent
        repository.close()

    asyncio.run(scenario())


def test_agent_persistence_survives_repository_restart(tmp_path) -> None:
    async def scenario() -> None:
        database_path = tmp_path / "maestro.db"
        first_repository = SQLiteAgentRepository(database_path)
        agent = await first_repository.create(valid_agent())
        first_repository.close()

        second_repository = SQLiteAgentRepository(database_path)
        loaded = await second_repository.get(agent.metadata.id)

        assert loaded.metadata.id == agent.metadata.id
        assert loaded.spec.model == "qwen2.5-coder:14b"
        second_repository.close()

    asyncio.run(scenario())


def test_duplicate_agent_names_are_rejected() -> None:
    async def scenario() -> None:
        repository = SQLiteAgentRepository(":memory:")
        await repository.create(valid_agent())

        with pytest.raises(ResourceAlreadyExistsError):
            await repository.create(valid_agent())
        repository.close()

    asyncio.run(scenario())


def test_agent_repository_lists_by_provider_role_and_labels() -> None:
    async def scenario() -> None:
        repository = SQLiteAgentRepository(":memory:")
        agent = valid_agent()
        labeled_agent = agent.model_copy(
            update={
                "metadata": agent.metadata.model_copy(
                    update={"labels": {"locality": "macbook"}}
                )
            }
        )
        await repository.create(labeled_agent)
        await repository.create(
            Agent.new(
                name="reviewer-local",
                spec=valid_agent_spec().model_copy(
                    update={
                        "model": "qwen3:14b",
                        "supported_roles": (
                            AgentSupportedRole(
                                name="reviewer",
                                versions=("v1alpha1",),
                            ),
                        ),
                    }
                ),
            )
        )

        by_provider = await repository.list_by_provider("default", "ollama-local")
        by_role = await repository.list_compatible_with_role(
            "default",
            "coding",
            "v1alpha1",
        )
        by_label = await repository.list(
            ResourceSelector(labels={"locality": "macbook"})
        )

        assert [agent.metadata.name for agent in by_provider] == [
            "coder-local",
            "reviewer-local",
        ]
        assert [agent.metadata.name for agent in by_role] == ["coder-local"]
        assert [agent.metadata.name for agent in by_label] == ["coder-local"]
        repository.close()

    asyncio.run(scenario())


def test_agent_update_spec_uses_optimistic_concurrency() -> None:
    async def scenario() -> None:
        repository = SQLiteAgentRepository(":memory:")
        agent = await repository.create(valid_agent())
        changed_spec = agent.spec.model_copy(
            update={"scheduling": AgentScheduling(priority=200)}
        )

        updated = await repository.update_spec(
            agent.metadata.id,
            changed_spec,
            expected_resource_version=agent.metadata.resource_version,
        )

        assert updated.metadata.generation == 2
        assert updated.metadata.resource_version == 2

        with pytest.raises(ResourceConflictError):
            await repository.update_spec(
                agent.metadata.id,
                changed_spec,
                expected_resource_version=agent.metadata.resource_version,
            )
        repository.close()

    asyncio.run(scenario())


def test_agent_update_status_preserves_generation() -> None:
    async def scenario() -> None:
        repository = SQLiteAgentRepository(":memory:")
        agent = await repository.create(valid_agent())
        status = AgentStatus(
            observedGeneration=1,
            phase=AgentPhase.READY,
            currentAssignments=0,
            modelAvailable=True,
        )

        updated = await repository.update_status(
            agent.metadata.id,
            status,
            expected_resource_version=agent.metadata.resource_version,
        )

        assert updated.metadata.generation == 1
        assert updated.metadata.resource_version == 2
        assert updated.status.phase == AgentPhase.READY
        repository.close()

    asyncio.run(scenario())


def test_agent_stale_status_update_returns_conflict() -> None:
    async def scenario() -> None:
        repository = SQLiteAgentRepository(":memory:")
        agent = await repository.create(valid_agent())
        status = AgentStatus(phase=AgentPhase.READY, modelAvailable=True)

        await repository.update_status(
            agent.metadata.id,
            status,
            expected_resource_version=agent.metadata.resource_version,
        )

        with pytest.raises(ResourceConflictError):
            await repository.update_status(
                agent.metadata.id,
                status,
                expected_resource_version=agent.metadata.resource_version,
            )
        repository.close()

    asyncio.run(scenario())
