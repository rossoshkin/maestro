"""Tests for the deterministic mock Provider adapter."""

import asyncio

import pytest

from maestro.domain.providers import (
    ProviderErrorCode,
    ProviderFeatureSet,
    ProviderMessage,
    ProviderMessageRole,
    ProviderOperationError,
    ProviderPhase,
    ProviderToolDefinition,
    StructuredGenerationRequest,
    ToolLoopRequest,
)
from maestro.infrastructure.providers import MockProvider


def message() -> ProviderMessage:
    """Build a simple user message."""

    return ProviderMessage(role=ProviderMessageRole.USER, content="Return JSON")


def test_mock_provider_reports_health_and_models() -> None:
    async def scenario() -> None:
        provider = MockProvider(models=("mock-a", "mock-b"))

        health = await provider.health()
        models = await provider.list_models()

        assert health.phase == ProviderPhase.READY
        assert health.capabilities.structured_output is True
        assert models.models == ("mock-a", "mock-b")

    asyncio.run(scenario())


def test_mock_provider_generates_deterministic_structured_output() -> None:
    async def scenario() -> None:
        provider = MockProvider(
            models=("mock-model",),
            structured_outputs=({"summary": "ok"},),
        )

        result = await provider.generate_structured(
            StructuredGenerationRequest(
                model="mock-model",
                messages=(message(),),
                responseSchema={"type": "object"},
            )
        )

        assert result.output == {"summary": "ok"}
        assert result.token_usage.input_tokens == 2

    asyncio.run(scenario())


def test_mock_provider_rejects_unavailable_model() -> None:
    async def scenario() -> None:
        provider = MockProvider(models=("mock-model",))

        with pytest.raises(ProviderOperationError) as error:
            await provider.generate_structured(
                StructuredGenerationRequest(
                    model="other-model",
                    messages=(message(),),
                )
            )

        assert error.value.failure.code == ProviderErrorCode.MODEL_UNAVAILABLE

    asyncio.run(scenario())


def test_mock_provider_normalizes_timeout() -> None:
    async def scenario() -> None:
        provider = MockProvider(operation_delay_seconds=10)

        with pytest.raises(ProviderOperationError) as error:
            await provider.generate_structured(
                StructuredGenerationRequest(
                    model="mock-model",
                    messages=(message(),),
                    timeoutSeconds=1,
                )
            )

        assert error.value.failure.code == ProviderErrorCode.PROVIDER_TIMEOUT
        assert error.value.failure.retryable is True

    asyncio.run(scenario())


def test_mock_provider_tool_loop_result_is_deterministic() -> None:
    async def scenario() -> None:
        provider = MockProvider(tool_loop_outputs=({"done": True},))

        result = await provider.run_tool_loop(
            ToolLoopRequest(
                model="mock-model",
                messages=(message(),),
                tools=(ProviderToolDefinition(name="pytest"),),
            )
        )

        assert result.output == {"done": True}
        assert result.tool_call_count == 1

    asyncio.run(scenario())


def test_mock_provider_rejects_tool_loop_when_unsupported() -> None:
    async def scenario() -> None:
        provider = MockProvider(
            capabilities=ProviderFeatureSet(structuredOutput=True, toolCalling=False)
        )

        with pytest.raises(ProviderOperationError) as error:
            await provider.run_tool_loop(
                ToolLoopRequest(
                    model="mock-model",
                    messages=(message(),),
                    tools=(ProviderToolDefinition(name="pytest"),),
                )
            )

        assert error.value.failure.code == ProviderErrorCode.TOOL_LOOP_ERROR

    asyncio.run(scenario())
