"""Tests for the Ollama Provider adapter."""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Mapping
from typing import Any

import pytest

from maestro.domain.providers import (
    Provider,
    ProviderDataPolicy,
    ProviderErrorCode,
    ProviderFailure,
    ProviderMessage,
    ProviderMessageRole,
    ProviderOperationError,
    ProviderPhase,
    ProviderSpec,
    ProviderToolDefinition,
    StructuredGenerationRequest,
    ToolLoopRequest,
)
from maestro.infrastructure.providers import OllamaProvider


class FakeOllamaTransport:
    """Queue JSON responses by method and path."""

    def __init__(self) -> None:
        self.responses: dict[tuple[str, str], deque[dict[str, Any] | Exception]] = {}
        self.requests: list[tuple[str, str, Mapping[str, Any] | None, int]] = []

    def queue_get(self, path: str, response: dict[str, Any] | Exception) -> None:
        self._queue("GET", path, response)

    def queue_post(self, path: str, response: dict[str, Any] | Exception) -> None:
        self._queue("POST", path, response)

    async def get_json(self, path: str, *, timeout_seconds: int) -> dict[str, Any]:
        self.requests.append(("GET", path, None, timeout_seconds))
        return self._pop("GET", path)

    async def post_json(
        self,
        path: str,
        payload: Mapping[str, Any],
        *,
        timeout_seconds: int,
    ) -> dict[str, Any]:
        self.requests.append(("POST", path, payload, timeout_seconds))
        return self._pop("POST", path)

    def _queue(
        self,
        method: str,
        path: str,
        response: dict[str, Any] | Exception,
    ) -> None:
        self.responses.setdefault((method, path), deque()).append(response)

    def _pop(self, method: str, path: str) -> dict[str, Any]:
        response = self.responses[(method, path)].popleft()
        if isinstance(response, Exception):
            raise response
        return response


def message(content: str = "Return JSON") -> ProviderMessage:
    return ProviderMessage(role=ProviderMessageRole.USER, content=content)


def tags_response(*models: str) -> dict[str, Any]:
    return {"models": [{"name": model} for model in models]}


def provider_resource(
    *,
    provider_type: str = "ollama",
    endpoint: str = "http://127.0.0.1:11434",
) -> Provider:
    return Provider.new(
        name="ollama-local",
        spec=ProviderSpec(
            type=provider_type,
            endpoint=endpoint,
            allowedModels=("qwen3:14b",),
            dataPolicy=ProviderDataPolicy(allowSourceCode=True),
        ),
    )


def test_ollama_provider_reports_health_and_models() -> None:
    async def scenario() -> None:
        transport = FakeOllamaTransport()
        transport.queue_get("/api/tags", tags_response("qwen3:14b", "coder:latest"))
        transport.queue_get("/api/tags", tags_response("qwen3:14b", "coder:latest"))
        provider = OllamaProvider(
            endpoint="http://127.0.0.1:11434",
            transport=transport,
        )

        health = await provider.health()
        models = await provider.list_models()

        assert health.phase == ProviderPhase.READY
        assert health.capabilities.structured_output is True
        assert health.capabilities.tool_calling is True
        assert health.available_models == ("qwen3:14b", "coder:latest")
        assert models.models == ("qwen3:14b", "coder:latest")

    asyncio.run(scenario())


def test_ollama_provider_generates_structured_output() -> None:
    async def scenario() -> None:
        transport = FakeOllamaTransport()
        transport.queue_get("/api/tags", tags_response("qwen3:14b"))
        transport.queue_post(
            "/api/chat",
            {
                "message": {"role": "assistant", "content": '{"summary": "ok"}'},
                "prompt_eval_count": 7,
                "eval_count": 3,
            },
        )
        provider = OllamaProvider(
            endpoint="http://127.0.0.1:11434",
            timeout_seconds=30,
            transport=transport,
        )

        result = await provider.generate_structured(
            StructuredGenerationRequest(
                model="qwen3:14b",
                messages=(message(),),
                responseSchema={"type": "object"},
                timeoutSeconds=10,
            )
        )

        method, path, payload, timeout_seconds = transport.requests[-1]
        assert (method, path) == ("POST", "/api/chat")
        assert payload is not None
        assert payload["stream"] is False
        assert payload["format"] == {"type": "object"}
        assert timeout_seconds == 10
        assert result.output == {"summary": "ok"}
        assert result.raw_text == '{"summary": "ok"}'
        assert result.token_usage.input_tokens == 7
        assert result.token_usage.output_tokens == 3

    asyncio.run(scenario())


def test_ollama_provider_rejects_missing_model_before_generation() -> None:
    async def scenario() -> None:
        transport = FakeOllamaTransport()
        transport.queue_get("/api/tags", tags_response("qwen3:14b"))
        provider = OllamaProvider(
            endpoint="http://127.0.0.1:11434",
            transport=transport,
        )

        with pytest.raises(ProviderOperationError) as error:
            await provider.generate_structured(
                StructuredGenerationRequest(
                    model="missing:latest",
                    messages=(message(),),
                )
            )

        assert error.value.failure.code == ProviderErrorCode.MODEL_UNAVAILABLE
        assert all(request[0] != "POST" for request in transport.requests)

    asyncio.run(scenario())


def test_ollama_provider_rejects_malformed_structured_response() -> None:
    async def scenario() -> None:
        transport = FakeOllamaTransport()
        transport.queue_get("/api/tags", tags_response("qwen3:14b"))
        transport.queue_post(
            "/api/chat",
            {"message": {"role": "assistant", "content": "not json"}},
        )
        provider = OllamaProvider(
            endpoint="http://127.0.0.1:11434",
            transport=transport,
        )

        with pytest.raises(ProviderOperationError) as error:
            await provider.generate_structured(
                StructuredGenerationRequest(
                    model="qwen3:14b",
                    messages=(message(),),
                )
            )

        assert error.value.failure.code == ProviderErrorCode.STRUCTURED_OUTPUT_ERROR

    asyncio.run(scenario())


def test_ollama_provider_exchanges_tool_calls() -> None:
    async def scenario() -> None:
        transport = FakeOllamaTransport()
        transport.queue_get("/api/tags", tags_response("qwen3:14b"))
        transport.queue_post(
            "/api/chat",
            {
                "message": {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "function": {
                                "name": "pytest",
                                "arguments": {"path": "tests"},
                            }
                        },
                    ],
                },
                "prompt_eval_count": 11,
                "eval_count": 5,
            },
        )
        provider = OllamaProvider(
            endpoint="http://127.0.0.1:11434",
            transport=transport,
        )

        result = await provider.run_tool_loop(
            ToolLoopRequest(
                model="qwen3:14b",
                messages=(message("Run tests"),),
                tools=(
                    ProviderToolDefinition(
                        name="pytest",
                        description="Run pytest",
                        inputSchema={"type": "object"},
                    ),
                ),
            )
        )

        payload = transport.requests[-1][2]
        assert payload is not None
        assert payload["tools"][0]["function"]["name"] == "pytest"
        assert result.tool_call_count == 1
        assert result.output["toolCalls"] == (
            {"name": "pytest", "arguments": {"path": "tests"}},
        )
        assert result.token_usage.output_tokens == 5

    asyncio.run(scenario())


def test_ollama_provider_normalizes_timeout() -> None:
    async def scenario() -> None:
        transport = FakeOllamaTransport()
        transport.queue_get("/api/tags", TimeoutError("slow"))
        provider = OllamaProvider(
            endpoint="http://127.0.0.1:11434",
            transport=transport,
        )

        with pytest.raises(ProviderOperationError) as error:
            await provider.list_models()

        assert error.value.failure.code == ProviderErrorCode.PROVIDER_TIMEOUT
        assert error.value.failure.retryable is True

    asyncio.run(scenario())


def test_ollama_provider_health_returns_unavailable_on_endpoint_failure() -> None:
    async def scenario() -> None:
        transport = FakeOllamaTransport()
        transport.queue_get(
            "/api/tags",
            ProviderOperationError(
                ProviderFailure(
                    code=ProviderErrorCode.PROVIDER_UNAVAILABLE,
                    message="offline",
                    retryable=True,
                )
            ),
        )
        provider = OllamaProvider(
            endpoint="http://127.0.0.1:11434",
            transport=transport,
        )

        health = await provider.health()

        assert health.phase == ProviderPhase.UNAVAILABLE
        assert health.failure is not None
        assert health.failure.code == ProviderErrorCode.PROVIDER_UNAVAILABLE

    asyncio.run(scenario())


def test_ollama_provider_from_resource_validates_configuration() -> None:
    with pytest.raises(ValueError):
        OllamaProvider.from_provider(provider_resource(provider_type="openai"))

    with pytest.raises(ValueError):
        OllamaProvider.from_provider(provider_resource(endpoint="not-a-url"))
