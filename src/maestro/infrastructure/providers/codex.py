"""Codex Provider adapter."""

from __future__ import annotations

import asyncio
import json
import subprocess
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, cast

from maestro.domain.modeling import ModelIdentifier
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
    StructuredGenerationRequest,
    StructuredGenerationResult,
    ToolLoopRequest,
    ToolLoopResult,
)

DEFAULT_CODEX_MODEL = "codex-default"
CODEX_UNSUPPORTED_SCHEMA_KEYS = {
    "default",
    "exclusiveMaximum",
    "exclusiveMinimum",
    "format",
    "maxItems",
    "maxLength",
    "maximum",
    "minItems",
    "minLength",
    "minimum",
    "multipleOf",
    "pattern",
    "title",
    "uniqueItems",
}


@dataclass(frozen=True, slots=True)
class CodexRunResult:
    """Completed Codex CLI invocation."""

    return_code: int
    stdout: str = ""
    stderr: str = ""


class CodexRunner(Protocol):
    """Async command runner used by the Codex adapter."""

    async def run(
        self,
        command: tuple[str, ...],
        *,
        stdin: str,
        cwd: Path | None,
        timeout_seconds: int,
    ) -> CodexRunResult:
        """Run Codex and return captured output."""


class SubprocessCodexRunner:
    """Run Codex through the local CLI."""

    async def run(
        self,
        command: tuple[str, ...],
        *,
        stdin: str,
        cwd: Path | None,
        timeout_seconds: int,
    ) -> CodexRunResult:
        """Run a subprocess with captured stdout/stderr."""

        try:
            completed = await asyncio.to_thread(
                subprocess.run,
                command,
                input=stdin,
                cwd=cwd,
                timeout=timeout_seconds,
                text=True,
                capture_output=True,
                check=False,
            )
        except subprocess.TimeoutExpired as error:
            raise TimeoutError("Codex operation timed out") from error
        except OSError as error:
            raise _provider_error(
                ProviderErrorCode.PROVIDER_UNAVAILABLE,
                str(error),
                retryable=True,
            ) from error
        return CodexRunResult(
            return_code=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
        )


class CodexProvider(ModelProvider):
    """ModelProvider implementation backed by non-interactive Codex CLI."""

    capabilities = ProviderFeatureSet(structuredOutput=True, toolCalling=False)

    def __init__(
        self,
        *,
        executable: str = "codex",
        models: tuple[ModelIdentifier, ...] = (DEFAULT_CODEX_MODEL,),
        timeout_seconds: int = 120,
        working_directory: Path | None = None,
        runner: CodexRunner | None = None,
    ) -> None:
        if timeout_seconds < 1:
            raise ValueError("timeout_seconds must be at least 1")
        if not executable:
            raise ValueError("executable must not be empty")
        if not models:
            raise ValueError("models must not be empty")
        self._executable = executable
        self._models = models
        self._timeout_seconds = timeout_seconds
        self._working_directory = working_directory
        self._runner = runner or SubprocessCodexRunner()

    @classmethod
    def from_provider(
        cls,
        provider: Provider,
        *,
        runner: CodexRunner | None = None,
        working_directory: Path | None = None,
    ) -> CodexProvider:
        """Create a Codex adapter from a Provider resource."""

        if provider.spec.provider_type != "codex":
            raise ValueError("Provider spec.type must be 'codex'")
        return cls(
            executable=provider.spec.endpoint,
            models=provider.spec.allowed_models or (DEFAULT_CODEX_MODEL,),
            timeout_seconds=provider.spec.timeout_seconds,
            working_directory=working_directory,
            runner=runner,
        )

    async def health(self) -> ProviderHealth:
        """Return Codex CLI availability."""

        try:
            result = await self._runner.run(
                (self._executable, "--version"),
                stdin="",
                cwd=self._working_directory,
                timeout_seconds=min(10, self._timeout_seconds),
            )
        except ProviderOperationError as error:
            return ProviderHealth(
                phase=ProviderPhase.UNAVAILABLE,
                capabilities=self.capabilities,
                failure=error.failure,
            )
        except TimeoutError:
            failure = ProviderFailure(
                code=ProviderErrorCode.PROVIDER_TIMEOUT,
                message="Codex health check timed out",
                retryable=True,
            )
            return ProviderHealth(
                phase=ProviderPhase.UNAVAILABLE,
                capabilities=self.capabilities,
                failure=failure,
            )
        if result.return_code != 0:
            return ProviderHealth(
                phase=ProviderPhase.UNAVAILABLE,
                capabilities=self.capabilities,
                failure=ProviderFailure(
                    code=ProviderErrorCode.PROVIDER_UNAVAILABLE,
                    message=result.stderr or result.stdout or "Codex is unavailable",
                    retryable=True,
                ),
            )
        return ProviderHealth(
            phase=ProviderPhase.READY,
            capabilities=self.capabilities,
            availableModels=self._models,
        )

    async def list_models(self) -> ProviderModelList:
        """Return configured Codex model names."""

        return ProviderModelList(models=self._models)

    async def generate_structured(
        self,
        request: StructuredGenerationRequest,
    ) -> StructuredGenerationResult:
        """Run `codex exec` in read-only mode and parse the final JSON message."""

        self._ensure_model(request.model)
        prompt = _prompt_from_messages(request.messages)
        with tempfile.TemporaryDirectory(prefix="maestro-codex-") as temp_dir_name:
            temp_dir = Path(temp_dir_name)
            schema_path = temp_dir / "schema.json"
            output_path = temp_dir / "output.json"
            schema_path.write_text(
                json.dumps(_schema_for_codex(request.response_schema or {}))
            )
            command = _exec_command(
                self._executable,
                model=request.model,
                schema_path=schema_path,
                output_path=output_path,
            )
            try:
                result = await self._runner.run(
                    command,
                    stdin=prompt,
                    cwd=self._working_directory,
                    timeout_seconds=request.timeout_seconds,
                )
            except TimeoutError as error:
                raise _provider_error(
                    ProviderErrorCode.PROVIDER_TIMEOUT,
                    "Codex operation timed out",
                    retryable=True,
                ) from error

            if result.return_code != 0:
                raise _provider_error(
                    ProviderErrorCode.UNKNOWN,
                    result.stderr or result.stdout or "Codex exited unsuccessfully",
                )
            raw_text = _read_output(output_path, result)
        output = _parse_json_object(raw_text)
        return StructuredGenerationResult(
            model=request.model,
            output=output,
            rawText=raw_text,
            tokenUsage=_token_usage(request.messages, raw_text),
        )

    async def run_tool_loop(self, request: ToolLoopRequest) -> ToolLoopResult:
        """Reject tool-loop use because Reviewer Codex runs read-only."""

        del request
        raise _provider_error(
            ProviderErrorCode.TOOL_LOOP_ERROR,
            "Codex Reviewer provider does not support tool calls",
        )

    def _ensure_model(self, model: str) -> None:
        if model not in self._models:
            raise _provider_error(
                ProviderErrorCode.MODEL_UNAVAILABLE,
                f"Model {model} is not configured for Codex",
            )


def _exec_command(
    executable: str,
    *,
    model: str,
    schema_path: Path,
    output_path: Path,
) -> tuple[str, ...]:
    command: tuple[str, ...] = (
        executable,
        "exec",
        "--sandbox",
        "read-only",
        "--skip-git-repo-check",
        "--ephemeral",
        "--ignore-rules",
        "--color",
        "never",
        "--output-schema",
        str(schema_path),
        "--output-last-message",
        str(output_path),
    )
    if model != DEFAULT_CODEX_MODEL:
        command = (*command, "-m", model)
    return (*command, "-")


def _schema_for_codex(schema: Mapping[str, Any]) -> dict[str, Any]:
    return cast(dict[str, Any], _sanitize_schema_for_codex(schema))


def _sanitize_schema_for_codex(value: Any) -> Any:
    if isinstance(value, Mapping):
        sanitized = {
            str(key): _sanitize_schema_for_codex(item)
            for key, item in value.items()
            if key not in CODEX_UNSUPPORTED_SCHEMA_KEYS
        }
        properties = sanitized.get("properties")
        if isinstance(properties, dict):
            sanitized["required"] = list(properties)
            sanitized.setdefault("additionalProperties", False)
        return sanitized
    if isinstance(value, list):
        return [_sanitize_schema_for_codex(item) for item in value]
    return value


def _prompt_from_messages(messages: tuple[ProviderMessage, ...]) -> str:
    return "\n\n".join(f"{message.role}: {message.content}" for message in messages)


def _read_output(output_path: Path, result: CodexRunResult) -> str:
    if output_path.exists():
        return output_path.read_text()
    if result.stdout:
        return result.stdout
    raise _provider_error(
        ProviderErrorCode.STRUCTURED_OUTPUT_ERROR,
        "Codex did not write a final message",
    )


def _parse_json_object(raw_text: str) -> dict[str, object]:
    try:
        decoded = json.loads(raw_text)
    except json.JSONDecodeError as error:
        raise _provider_error(
            ProviderErrorCode.STRUCTURED_OUTPUT_ERROR,
            "Codex final message was not valid JSON",
        ) from error
    if not isinstance(decoded, dict):
        raise _provider_error(
            ProviderErrorCode.STRUCTURED_OUTPUT_ERROR,
            "Codex final message was not a JSON object",
        )
    return decoded


def _token_usage(
    messages: tuple[ProviderMessage, ...],
    output: str,
) -> ProviderTokenUsage:
    return ProviderTokenUsage(
        inputTokens=sum(len(message.content.split()) for message in messages),
        outputTokens=len(output.split()),
    )


def _provider_error(
    code: ProviderErrorCode,
    message: str,
    *,
    retryable: bool = False,
) -> ProviderOperationError:
    return ProviderOperationError(
        ProviderFailure(code=code, message=message, retryable=retryable)
    )
