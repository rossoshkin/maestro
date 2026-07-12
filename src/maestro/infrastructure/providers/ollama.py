"""Ollama Provider adapter."""

from __future__ import annotations

import asyncio
import json
import socket
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping
from typing import Any, Protocol

from maestro.domain.providers import (
    ModelProvider,
    Provider,
    ProviderErrorCode,
    ProviderFailure,
    ProviderFeatureSet,
    ProviderHealth,
    ProviderMessage,
    ProviderModelList,
    ProviderOperationError,
    ProviderPhase,
    ProviderTokenUsage,
    ProviderToolDefinition,
    StructuredGenerationRequest,
    StructuredGenerationResult,
    ToolLoopRequest,
    ToolLoopResult,
)

type JsonObject = dict[str, Any]


class OllamaTransport(Protocol):
    """Minimal async JSON transport used by the Ollama adapter."""

    async def get_json(self, path: str, *, timeout_seconds: int) -> JsonObject:
        """Return JSON from an Ollama GET endpoint."""

    async def post_json(
        self,
        path: str,
        payload: Mapping[str, Any],
        *,
        timeout_seconds: int,
    ) -> JsonObject:
        """Return JSON from an Ollama POST endpoint."""


class UrllibOllamaTransport:
    """Standard-library HTTP transport for local Ollama endpoints."""

    def __init__(self, endpoint: str) -> None:
        self._endpoint = endpoint.rstrip("/")

    async def get_json(self, path: str, *, timeout_seconds: int) -> JsonObject:
        """Return JSON from an Ollama GET endpoint."""

        return await asyncio.to_thread(
            self._request_json,
            "GET",
            path,
            None,
            timeout_seconds,
        )

    async def post_json(
        self,
        path: str,
        payload: Mapping[str, Any],
        *,
        timeout_seconds: int,
    ) -> JsonObject:
        """Return JSON from an Ollama POST endpoint."""

        return await asyncio.to_thread(
            self._request_json,
            "POST",
            path,
            payload,
            timeout_seconds,
        )

    def _request_json(
        self,
        method: str,
        path: str,
        payload: Mapping[str, Any] | None,
        timeout_seconds: int,
    ) -> JsonObject:
        url = self._url(path)
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=data,
            headers={"Content-Type": "application/json"},
            method=method,
        )
        try:
            with urllib.request.urlopen(
                request,
                timeout=timeout_seconds,
            ) as response:
                raw = response.read().decode("utf-8")
        except TimeoutError as error:
            raise _provider_error(
                ProviderErrorCode.PROVIDER_TIMEOUT,
                "Ollama operation timed out",
                retryable=True,
            ) from error
        except urllib.error.HTTPError as error:
            raise _http_error(error) from error
        except urllib.error.URLError as error:
            if isinstance(error.reason, socket.timeout):
                raise _provider_error(
                    ProviderErrorCode.PROVIDER_TIMEOUT,
                    "Ollama operation timed out",
                    retryable=True,
                ) from error
            raise _provider_error(
                ProviderErrorCode.PROVIDER_UNAVAILABLE,
                str(error.reason) or "Ollama endpoint is unavailable",
                retryable=True,
            ) from error

        try:
            decoded = json.loads(raw)
        except json.JSONDecodeError as error:
            raise _provider_error(
                ProviderErrorCode.UNKNOWN,
                "Ollama returned non-JSON response",
            ) from error
        if not isinstance(decoded, dict):
            raise _provider_error(
                ProviderErrorCode.UNKNOWN,
                "Ollama returned a non-object JSON response",
            )
        return decoded

    def _url(self, path: str) -> str:
        return self._endpoint + "/" + path.lstrip("/")


class OllamaProvider(ModelProvider):
    """ModelProvider implementation backed by Ollama's HTTP API."""

    capabilities = ProviderFeatureSet(
        structuredOutput=True,
        toolCalling=True,
        streaming=True,
    )

    def __init__(
        self,
        *,
        endpoint: str,
        timeout_seconds: int = 120,
        transport: OllamaTransport | None = None,
    ) -> None:
        self._endpoint = _validate_endpoint(endpoint)
        if timeout_seconds < 1:
            raise ValueError("timeout_seconds must be at least 1")
        self._timeout_seconds = timeout_seconds
        self._transport = transport or UrllibOllamaTransport(self._endpoint)

    @classmethod
    def from_provider(
        cls,
        provider: Provider,
        *,
        transport: OllamaTransport | None = None,
    ) -> OllamaProvider:
        """Create an adapter from a Provider resource."""

        if provider.spec.provider_type != "ollama":
            raise ValueError("Provider spec.type must be 'ollama'")
        if provider.spec.auth_ref is not None:
            raise ValueError("Ollama Provider does not support authRef in the MVP")
        return cls(
            endpoint=provider.spec.endpoint,
            timeout_seconds=provider.spec.timeout_seconds,
            transport=transport,
        )

    async def health(self) -> ProviderHealth:
        """Return endpoint health and discovered feature support."""

        try:
            models = await self.list_models()
        except ProviderOperationError as error:
            return ProviderHealth(
                phase=_phase_for_failure(error.failure),
                capabilities=self.capabilities,
                failure=error.failure,
            )
        return ProviderHealth(
            phase=ProviderPhase.READY,
            capabilities=self.capabilities,
            availableModels=models.models,
        )

    async def list_models(self) -> ProviderModelList:
        """Return models discoverable from `/api/tags`."""

        payload = await self._get_json("/api/tags")
        models = payload.get("models")
        if not isinstance(models, list):
            raise _provider_error(
                ProviderErrorCode.UNKNOWN,
                "Ollama /api/tags response is missing models",
            )
        names: list[str] = []
        for item in models:
            if not isinstance(item, dict):
                continue
            name = item.get("name") or item.get("model")
            if isinstance(name, str) and name:
                names.append(name)
        return ProviderModelList(models=tuple(names))

    async def generate_structured(
        self,
        request: StructuredGenerationRequest,
    ) -> StructuredGenerationResult:
        """Generate and parse structured JSON output through `/api/chat`."""

        await self._ensure_model_available(request.model)
        payload: JsonObject = {
            "model": request.model,
            "messages": _messages_payload(request.messages),
            "stream": False,
            "format": request.response_schema or "json",
        }
        response = await self._post_json(
            "/api/chat",
            payload,
            timeout_seconds=request.timeout_seconds,
        )
        content = _message_content(response)
        output = _parse_json_object_content(
            content,
            error_code=ProviderErrorCode.STRUCTURED_OUTPUT_ERROR,
            error_message="Ollama structured output was not valid JSON object",
        )
        return StructuredGenerationResult(
            model=request.model,
            output=output,
            rawText=content,
            tokenUsage=_token_usage(response),
        )

    async def run_tool_loop(self, request: ToolLoopRequest) -> ToolLoopResult:
        """Exchange a tool-capable chat request with Ollama."""

        await self._ensure_model_available(request.model)
        payload: JsonObject = {
            "model": request.model,
            "messages": _messages_payload(request.messages),
            "stream": False,
            "tools": tuple(_tool_payload(tool) for tool in request.tools),
        }
        response = await self._post_json(
            "/api/chat",
            payload,
            timeout_seconds=request.timeout_seconds,
        )
        tool_calls = _tool_calls(response, request.tools)
        if len(tool_calls) > request.max_tool_calls:
            raise _provider_error(
                ProviderErrorCode.TOOL_LOOP_ERROR,
                "Ollama returned more tool calls than allowed",
            )

        content = _message_content(response, allow_empty=True)
        parsed_content = _parse_optional_json_content(content)
        output: JsonObject
        if tool_calls:
            output = {
                "content": parsed_content,
                "toolCalls": tool_calls,
            }
        elif isinstance(parsed_content, dict):
            output = parsed_content
        else:
            output = {"content": parsed_content}

        return ToolLoopResult(
            model=request.model,
            output=output,
            toolCallCount=len(tool_calls),
            tokenUsage=_token_usage(response),
        )

    async def _ensure_model_available(self, model: str) -> None:
        models = await self.list_models()
        if model not in models.models:
            raise _provider_error(
                ProviderErrorCode.MODEL_UNAVAILABLE,
                f"Model {model} is not available from Ollama",
            )

    async def _get_json(self, path: str) -> JsonObject:
        try:
            return await self._transport.get_json(
                path,
                timeout_seconds=self._timeout_seconds,
            )
        except TimeoutError as error:
            raise _provider_error(
                ProviderErrorCode.PROVIDER_TIMEOUT,
                "Ollama operation timed out",
                retryable=True,
            ) from error

    async def _post_json(
        self,
        path: str,
        payload: Mapping[str, Any],
        *,
        timeout_seconds: int,
    ) -> JsonObject:
        try:
            return await self._transport.post_json(
                path,
                payload,
                timeout_seconds=min(timeout_seconds, self._timeout_seconds),
            )
        except TimeoutError as error:
            raise _provider_error(
                ProviderErrorCode.PROVIDER_TIMEOUT,
                "Ollama operation timed out",
                retryable=True,
            ) from error


def _validate_endpoint(endpoint: str) -> str:
    parsed = urllib.parse.urlparse(endpoint)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("Ollama endpoint must be an HTTP(S) URL")
    return endpoint.rstrip("/")


def _http_error(error: urllib.error.HTTPError) -> ProviderOperationError:
    try:
        detail = error.read().decode("utf-8")
    except Exception:  # noqa: BLE001 - error bodies are best-effort diagnostics.
        detail = ""
    message = detail or f"Ollama returned HTTP {error.code}"
    if 400 <= error.code < 500:
        return _provider_error(ProviderErrorCode.INVALID_REQUEST, message)
    return _provider_error(
        ProviderErrorCode.PROVIDER_UNAVAILABLE,
        message,
        retryable=True,
    )


def _messages_payload(messages: tuple[ProviderMessage, ...]) -> tuple[JsonObject, ...]:
    return tuple(
        {
            "role": message.role.value,
            "content": message.content,
        }
        for message in messages
    )


def _tool_payload(tool: ProviderToolDefinition) -> JsonObject:
    return {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": tool.description,
            "parameters": tool.input_schema,
        },
    }


def _message_content(response: JsonObject, *, allow_empty: bool = False) -> str:
    message = response.get("message")
    content: Any
    if isinstance(message, dict):
        content = message.get("content", "")
    else:
        content = response.get("response", "")

    if isinstance(content, str) and (content or allow_empty):
        return content
    raise _provider_error(
        ProviderErrorCode.STRUCTURED_OUTPUT_ERROR,
        "Ollama response is missing assistant content",
    )


def _parse_json_object_content(
    content: str,
    *,
    error_code: ProviderErrorCode,
    error_message: str,
) -> JsonObject:
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError as error:
        raise _provider_error(error_code, error_message) from error
    if not isinstance(parsed, dict):
        raise _provider_error(error_code, error_message)
    return parsed


def _parse_optional_json_content(content: str) -> JsonObject | str:
    if not content:
        return {}
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError:
        return content
    if isinstance(parsed, dict):
        return parsed
    return content


def _tool_calls(
    response: JsonObject,
    tools: tuple[ProviderToolDefinition, ...],
) -> tuple[JsonObject, ...]:
    message = response.get("message")
    if not isinstance(message, dict):
        return ()
    raw_calls = message.get("tool_calls", ())
    if raw_calls in (None, ()):
        return ()
    if not isinstance(raw_calls, list):
        raise _provider_error(
            ProviderErrorCode.TOOL_LOOP_ERROR,
            "Ollama tool_calls must be a list",
        )

    known_tools = {tool.name for tool in tools}
    calls: list[JsonObject] = []
    for raw_call in raw_calls:
        if not isinstance(raw_call, dict):
            raise _provider_error(
                ProviderErrorCode.TOOL_LOOP_ERROR,
                "Ollama tool call must be an object",
            )
        function = raw_call.get("function")
        if not isinstance(function, dict):
            raise _provider_error(
                ProviderErrorCode.TOOL_LOOP_ERROR,
                "Ollama tool call is missing function data",
            )
        name = function.get("name")
        if not isinstance(name, str) or name not in known_tools:
            raise _provider_error(
                ProviderErrorCode.TOOL_LOOP_ERROR,
                f"Ollama requested unknown tool {name!r}",
            )
        calls.append(
            {
                "name": name,
                "arguments": _tool_arguments(function.get("arguments", {})),
            }
        )
    return tuple(calls)


def _tool_arguments(value: Any) -> JsonObject:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError as error:
            raise _provider_error(
                ProviderErrorCode.TOOL_LOOP_ERROR,
                "Ollama tool arguments were not valid JSON",
            ) from error
        if isinstance(parsed, dict):
            return parsed
    raise _provider_error(
        ProviderErrorCode.TOOL_LOOP_ERROR,
        "Ollama tool arguments must be an object",
    )


def _token_usage(response: JsonObject) -> ProviderTokenUsage:
    return ProviderTokenUsage(
        inputTokens=_non_negative_int(response.get("prompt_eval_count")),
        outputTokens=_non_negative_int(response.get("eval_count")),
    )


def _non_negative_int(value: Any) -> int:
    if isinstance(value, int) and value >= 0:
        return value
    return 0


def _phase_for_failure(failure: ProviderFailure) -> ProviderPhase:
    if failure.code == ProviderErrorCode.PROVIDER_TIMEOUT:
        return ProviderPhase.DEGRADED
    return ProviderPhase.UNAVAILABLE


def _provider_error(
    code: ProviderErrorCode,
    message: str,
    *,
    retryable: bool = False,
) -> ProviderOperationError:
    return ProviderOperationError(
        ProviderFailure(code=code, message=message, retryable=retryable)
    )
