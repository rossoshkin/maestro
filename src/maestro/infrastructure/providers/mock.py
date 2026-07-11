"""Deterministic mock Provider adapter for tests."""

from collections import deque
from collections.abc import Iterable
from typing import Any

from maestro.domain.modeling import ModelIdentifier
from maestro.domain.providers import (
    ModelProvider,
    ProviderErrorCode,
    ProviderFailure,
    ProviderFeatureSet,
    ProviderHealth,
    ProviderMessage,
    ProviderModelList,
    ProviderOperationError,
    ProviderPhase,
    ProviderTokenUsage,
    StructuredGenerationRequest,
    StructuredGenerationResult,
    ToolLoopRequest,
    ToolLoopResult,
)


class MockProvider(ModelProvider):
    """Deterministic in-process Provider implementation."""

    def __init__(
        self,
        *,
        phase: ProviderPhase = ProviderPhase.READY,
        capabilities: ProviderFeatureSet | None = None,
        models: Iterable[ModelIdentifier] = ("mock-model",),
        structured_outputs: Iterable[dict[str, Any]] = (),
        tool_loop_outputs: Iterable[dict[str, Any]] = (),
        operation_delay_seconds: int = 0,
        failure: ProviderFailure | None = None,
    ) -> None:
        self._phase = phase
        self._capabilities = capabilities or ProviderFeatureSet(
            structuredOutput=True,
            toolCalling=True,
        )
        self._models = tuple(models)
        self._structured_outputs = deque(structured_outputs)
        self._tool_loop_outputs = deque(tool_loop_outputs)
        self._operation_delay_seconds = operation_delay_seconds
        self._failure = failure

    async def health(self) -> ProviderHealth:
        """Return configured mock health."""

        if self._failure is not None:
            raise ProviderOperationError(self._failure)

        return ProviderHealth(
            phase=self._phase,
            capabilities=self._capabilities,
            availableModels=self._models,
            failure=self._failure,
        )

    async def list_models(self) -> ProviderModelList:
        """Return configured mock models."""

        return ProviderModelList(models=self._models)

    async def generate_structured(
        self,
        request: StructuredGenerationRequest,
    ) -> StructuredGenerationResult:
        """Return the next configured structured output."""

        self._ensure_ready_for_model(request.model)
        self._ensure_timeout_not_exceeded(request.timeout_seconds)
        if not self._capabilities.structured_output:
            raise ProviderOperationError(
                ProviderFailure(
                    code=ProviderErrorCode.STRUCTURED_OUTPUT_ERROR,
                    message="Provider does not support structured output",
                    retryable=False,
                )
            )

        output = (
            self._structured_outputs.popleft()
            if self._structured_outputs
            else {"ok": True}
        )
        return StructuredGenerationResult(
            model=request.model,
            output=output,
            rawText=str(output),
            tokenUsage=_token_usage_for_messages(request.messages),
        )

    async def run_tool_loop(self, request: ToolLoopRequest) -> ToolLoopResult:
        """Return the next configured tool-loop output."""

        self._ensure_ready_for_model(request.model)
        self._ensure_timeout_not_exceeded(request.timeout_seconds)
        if not self._capabilities.tool_calling:
            raise ProviderOperationError(
                ProviderFailure(
                    code=ProviderErrorCode.TOOL_LOOP_ERROR,
                    message="Provider does not support tool calling",
                    retryable=False,
                )
            )

        output = (
            self._tool_loop_outputs.popleft()
            if self._tool_loop_outputs
            else {"ok": True}
        )
        return ToolLoopResult(
            model=request.model,
            output=output,
            toolCallCount=min(len(request.tools), request.max_tool_calls),
            tokenUsage=_token_usage_for_messages(request.messages),
        )

    def _ensure_ready_for_model(self, model: str) -> None:
        if self._failure is not None:
            raise ProviderOperationError(self._failure)

        if self._phase not in {ProviderPhase.READY, ProviderPhase.DEGRADED}:
            raise ProviderOperationError(
                ProviderFailure(
                    code=ProviderErrorCode.PROVIDER_UNAVAILABLE,
                    message=f"Provider is {self._phase}",
                    retryable=True,
                )
            )

        if model not in self._models:
            raise ProviderOperationError(
                ProviderFailure(
                    code=ProviderErrorCode.MODEL_UNAVAILABLE,
                    message=f"Model {model} is not available",
                    retryable=False,
                )
            )

    def _ensure_timeout_not_exceeded(self, timeout_seconds: int) -> None:
        if self._operation_delay_seconds > timeout_seconds:
            raise ProviderOperationError(
                ProviderFailure(
                    code=ProviderErrorCode.PROVIDER_TIMEOUT,
                    message="Provider operation timed out",
                    retryable=True,
                )
            )


def _token_usage_for_messages(
    messages: tuple[ProviderMessage, ...],
) -> ProviderTokenUsage:
    approximate_input_tokens = sum(len(message.content.split()) for message in messages)
    return ProviderTokenUsage(
        inputTokens=approximate_input_tokens,
        outputTokens=1,
    )
