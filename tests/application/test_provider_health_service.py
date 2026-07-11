"""Tests for Provider health refresh service."""

import asyncio

from maestro.application.providers import ProviderHealthService
from maestro.domain.providers import (
    Provider,
    ProviderDataPolicy,
    ProviderErrorCode,
    ProviderFailure,
    ProviderFeatureSet,
    ProviderPhase,
    ProviderSpec,
)
from maestro.infrastructure.persistence import SQLiteProviderRepository
from maestro.infrastructure.providers import MockProvider


def valid_provider() -> Provider:
    """Build a valid Provider resource."""

    return Provider.new(
        name="ollama-local",
        spec=ProviderSpec(
            type="ollama",
            endpoint="http://127.0.0.1:11434",
            allowedModels=("qwen3:14b", "qwen2.5-coder:14b"),
            dataPolicy=ProviderDataPolicy(allowSourceCode=True),
        ),
    )


def test_provider_health_service_updates_ready_status() -> None:
    async def scenario() -> None:
        repository = SQLiteProviderRepository(":memory:")
        provider = await repository.create(valid_provider())
        service = ProviderHealthService(repository)

        updated = await service.refresh_provider_health(
            provider.metadata.id,
            MockProvider(
                capabilities=ProviderFeatureSet(
                    structuredOutput=True,
                    toolCalling=True,
                    streaming=True,
                ),
                models=("qwen3:14b", "not-allowed:latest"),
            ),
            expected_resource_version=provider.metadata.resource_version,
        )

        assert updated.status.phase == ProviderPhase.READY
        assert updated.status.capabilities.structured_output is True
        assert updated.status.available_models == ("qwen3:14b",)
        assert updated.status.last_health_check_at is not None
        repository.close()

    asyncio.run(scenario())


def test_provider_health_service_normalizes_runtime_failure() -> None:
    async def scenario() -> None:
        repository = SQLiteProviderRepository(":memory:")
        provider = await repository.create(valid_provider())
        service = ProviderHealthService(repository)

        updated = await service.refresh_provider_health(
            provider.metadata.id,
            MockProvider(
                failure=ProviderFailure(
                    code=ProviderErrorCode.PROVIDER_UNAVAILABLE,
                    message="offline",
                    retryable=True,
                )
            ),
            expected_resource_version=provider.metadata.resource_version,
        )

        assert updated.status.phase == ProviderPhase.UNAVAILABLE
        assert updated.status.failure is not None
        assert updated.status.failure.code == ProviderErrorCode.PROVIDER_UNAVAILABLE
        repository.close()

    asyncio.run(scenario())
