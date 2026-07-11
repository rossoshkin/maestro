"""Tests for SQLite Provider persistence."""

import asyncio

import pytest

from maestro.domain import ResourceSelector
from maestro.domain.exceptions import ResourceAlreadyExistsError, ResourceConflictError
from maestro.domain.providers import (
    Provider,
    ProviderDataPolicy,
    ProviderFeatureSet,
    ProviderPhase,
    ProviderSpec,
    ProviderStatus,
)
from maestro.infrastructure.persistence import SQLiteProviderRepository


def valid_provider_spec() -> ProviderSpec:
    """Build a valid ProviderSpec for persistence tests."""

    return ProviderSpec(
        type="ollama",
        endpoint="http://127.0.0.1:11434",
        allowedModels=("qwen3:14b", "qwen2.5-coder:14b"),
        dataPolicy=ProviderDataPolicy(allowSourceCode=True),
        timeoutSeconds=120,
    )


def valid_provider() -> Provider:
    """Build a valid Provider resource."""

    return Provider.new(name="ollama-local", spec=valid_provider_spec())


def test_provider_persistence_round_trip(tmp_path) -> None:
    async def scenario() -> None:
        repository = SQLiteProviderRepository(tmp_path / "maestro.db")
        provider = await repository.create(valid_provider())
        loaded = await repository.get(provider.metadata.id)

        assert loaded == provider
        repository.close()

    asyncio.run(scenario())


def test_provider_persistence_survives_repository_restart(tmp_path) -> None:
    async def scenario() -> None:
        database_path = tmp_path / "maestro.db"
        first_repository = SQLiteProviderRepository(database_path)
        provider = await first_repository.create(valid_provider())
        first_repository.close()

        second_repository = SQLiteProviderRepository(database_path)
        loaded = await second_repository.get(provider.metadata.id)

        assert loaded.metadata.id == provider.metadata.id
        assert loaded.spec.provider_type == "ollama"
        second_repository.close()

    asyncio.run(scenario())


def test_provider_lookup_by_name() -> None:
    async def scenario() -> None:
        repository = SQLiteProviderRepository(":memory:")
        provider = await repository.create(valid_provider())

        loaded = await repository.get_by_name("default", "ollama-local")

        assert loaded == provider
        repository.close()

    asyncio.run(scenario())


def test_duplicate_provider_names_are_rejected() -> None:
    async def scenario() -> None:
        repository = SQLiteProviderRepository(":memory:")
        await repository.create(valid_provider())

        with pytest.raises(ResourceAlreadyExistsError):
            await repository.create(valid_provider())
        repository.close()

    asyncio.run(scenario())


def test_provider_update_status_preserves_generation() -> None:
    async def scenario() -> None:
        repository = SQLiteProviderRepository(":memory:")
        provider = await repository.create(valid_provider())

        updated = await repository.update_status(
            provider.metadata.id,
            ProviderStatus(
                observedGeneration=1,
                phase=ProviderPhase.READY,
                capabilities=ProviderFeatureSet(structuredOutput=True),
                availableModels=("qwen3:14b",),
            ),
            expected_resource_version=provider.metadata.resource_version,
        )

        assert updated.metadata.generation == 1
        assert updated.metadata.resource_version == 2
        assert updated.status.phase == ProviderPhase.READY
        repository.close()

    asyncio.run(scenario())


def test_provider_update_spec_uses_optimistic_concurrency() -> None:
    async def scenario() -> None:
        repository = SQLiteProviderRepository(":memory:")
        provider = await repository.create(valid_provider())
        changed_spec = provider.spec.model_copy(update={"timeout_seconds": 60})

        updated = await repository.update_spec(
            provider.metadata.id,
            changed_spec,
            expected_resource_version=provider.metadata.resource_version,
        )

        assert updated.metadata.generation == 2
        assert updated.metadata.resource_version == 2

        with pytest.raises(ResourceConflictError):
            await repository.update_spec(
                provider.metadata.id,
                changed_spec,
                expected_resource_version=provider.metadata.resource_version,
            )
        repository.close()

    asyncio.run(scenario())


def test_provider_repository_lists_by_labels() -> None:
    async def scenario() -> None:
        repository = SQLiteProviderRepository(":memory:")
        provider = valid_provider()
        labeled = provider.model_copy(
            update={
                "metadata": provider.metadata.model_copy(
                    update={"labels": {"locality": "local"}}
                )
            }
        )
        await repository.create(labeled)

        selected = await repository.list(ResourceSelector(labels={"locality": "local"}))

        assert [provider.metadata.name for provider in selected] == ["ollama-local"]
        repository.close()

    asyncio.run(scenario())
