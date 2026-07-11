"""Application services for Provider resources."""

from uuid import UUID

from maestro.domain.providers import (
    ModelProvider,
    Provider,
    ProviderErrorCode,
    ProviderFailure,
    ProviderHealth,
    ProviderPhase,
    ProviderRepository,
    normalize_provider_error,
    provider_status_from_health,
)


class ProviderHealthService:
    """Refresh Provider health using a model-provider adapter."""

    def __init__(self, provider_repository: ProviderRepository) -> None:
        self._provider_repository = provider_repository

    async def refresh_provider_health(
        self,
        resource_id: UUID,
        runtime: ModelProvider,
        *,
        expected_resource_version: int,
    ) -> Provider:
        """Update Provider status from runtime health and model discovery."""

        provider = await self._provider_repository.get(resource_id)
        try:
            health = await runtime.health()
            models = await runtime.list_models()
            health = ProviderHealth(
                phase=health.phase,
                capabilities=health.capabilities,
                availableModels=models.models,
                failure=health.failure,
            )
        except Exception as error:  # noqa: BLE001 - normalize adapter boundary errors.
            failure = normalize_provider_error(error)
            health = ProviderHealth(
                phase=_phase_for_failure(failure),
                failure=failure,
            )

        status = provider_status_from_health(provider, health)
        return await self._provider_repository.update_status(
            resource_id,
            status,
            expected_resource_version=expected_resource_version,
        )


def _phase_for_failure(failure: ProviderFailure) -> ProviderPhase:
    if failure.code == ProviderErrorCode.PROVIDER_TIMEOUT:
        return ProviderPhase.DEGRADED
    return ProviderPhase.UNAVAILABLE
