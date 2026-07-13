"""Tests for Codex Provider adapter."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from maestro.domain.providers import (
    ProviderErrorCode,
    ProviderMessage,
    ProviderMessageRole,
    ProviderOperationError,
    StructuredGenerationRequest,
    ToolLoopRequest,
)
from maestro.infrastructure.providers.codex import (
    DEFAULT_CODEX_MODEL,
    CodexProvider,
    CodexRunResult,
)


class RecordingCodexRunner:
    """Capture Codex commands and write configured final output."""

    def __init__(
        self,
        *,
        output: dict[str, object] | None = None,
        return_code: int = 0,
        timeout: bool = False,
    ) -> None:
        self.commands: list[tuple[str, ...]] = []
        self.stdin: list[str] = []
        self.schema_payloads: list[dict[str, object]] = []
        self._output = output or {"verdict": "Approve", "summary": "ok"}
        self._return_code = return_code
        self._timeout = timeout

    async def run(
        self,
        command: tuple[str, ...],
        *,
        stdin: str,
        cwd: Path | None,
        timeout_seconds: int,
    ) -> CodexRunResult:
        del cwd, timeout_seconds
        self.commands.append(command)
        self.stdin.append(stdin)
        if self._timeout:
            raise TimeoutError("timeout")
        if "--output-schema" in command:
            schema_path = Path(command[command.index("--output-schema") + 1])
            self.schema_payloads.append(json.loads(schema_path.read_text()))
        if "--output-last-message" in command:
            output_path = Path(command[command.index("--output-last-message") + 1])
            output_path.write_text(json.dumps(self._output))
        return CodexRunResult(
            return_code=self._return_code,
            stdout="",
            stderr="boom" if self._return_code else "",
        )


def test_codex_provider_invokes_exec_read_only_with_schema(tmp_path: Path) -> None:
    async def scenario() -> None:
        runner = RecordingCodexRunner(output={"verdict": "Approve", "summary": "ok"})
        provider = CodexProvider(
            executable="codex",
            models=("codex-reviewer",),
            working_directory=tmp_path,
            runner=runner,
        )

        result = await provider.generate_structured(
            StructuredGenerationRequest(
                model="codex-reviewer",
                messages=(
                    ProviderMessage(
                        role=ProviderMessageRole.SYSTEM,
                        content="review only",
                    ),
                    ProviderMessage(
                        role=ProviderMessageRole.USER,
                        content="Return JSON",
                    ),
                ),
                responseSchema={"type": "object"},
                timeoutSeconds=7,
            )
        )

        command = runner.commands[0]

        assert result.output == {"verdict": "Approve", "summary": "ok"}
        assert command[:2] == ("codex", "exec")
        assert command[command.index("--sandbox") + 1] == "read-only"
        assert "--ask-for-approval" not in command
        assert "--output-schema" in command
        assert "--output-last-message" in command
        assert command[-1] == "-"
        assert "-m" in command
        assert command[command.index("-m") + 1] == "codex-reviewer"
        assert runner.schema_payloads == [{"type": "object"}]
        assert "review only" in runner.stdin[0]

    import asyncio

    asyncio.run(scenario())


def test_codex_provider_omits_model_flag_for_cli_default(tmp_path: Path) -> None:
    async def scenario() -> None:
        runner = RecordingCodexRunner(output={"verdict": "Approve", "summary": "ok"})
        provider = CodexProvider(
            executable="codex",
            models=(DEFAULT_CODEX_MODEL,),
            working_directory=tmp_path,
            runner=runner,
        )

        await provider.generate_structured(
            StructuredGenerationRequest(
                model=DEFAULT_CODEX_MODEL,
                messages=(
                    ProviderMessage(
                        role=ProviderMessageRole.USER,
                        content="Return JSON",
                    ),
                ),
                responseSchema={"type": "object"},
                timeoutSeconds=7,
            )
        )

        command = runner.commands[0]

        assert "-m" not in command
        assert command[-1] == "-"

    import asyncio

    asyncio.run(scenario())


def test_codex_provider_sanitizes_schema_for_structured_outputs(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        runner = RecordingCodexRunner(output={"verdict": "Approve", "findings": []})
        provider = CodexProvider(
            executable="codex",
            models=("codex-reviewer",),
            working_directory=tmp_path,
            runner=runner,
        )
        schema = {
            "type": "object",
            "title": "ReviewOutput",
            "properties": {
                "verdict": {"type": "string", "enum": ["Approve"]},
                "findings": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": {"type": "string", "minLength": 1},
                            "file": {
                                "anyOf": [{"type": "string"}, {"type": "null"}],
                                "default": None,
                            },
                            "line": {
                                "anyOf": [{"type": "integer"}, {"type": "null"}],
                                "default": None,
                                "minimum": 1,
                            },
                        },
                        "required": ["id"],
                    },
                },
            },
            "required": ["verdict"],
        }

        await provider.generate_structured(
            StructuredGenerationRequest(
                model="codex-reviewer",
                messages=(
                    ProviderMessage(
                        role=ProviderMessageRole.USER,
                        content="Return JSON",
                    ),
                ),
                responseSchema=schema,
                timeoutSeconds=7,
            )
        )

        sanitized = runner.schema_payloads[0]
        finding = sanitized["properties"]["findings"]["items"]

        assert sanitized["required"] == ["verdict", "findings"]
        assert sanitized["additionalProperties"] is False
        assert "title" not in sanitized
        assert finding["required"] == ["id", "file", "line"]
        assert finding["additionalProperties"] is False
        assert "minLength" not in finding["properties"]["id"]
        assert "default" not in finding["properties"]["file"]
        assert "minimum" not in finding["properties"]["line"]

    import asyncio

    asyncio.run(scenario())


def test_codex_provider_normalizes_timeout() -> None:
    async def scenario() -> None:
        provider = CodexProvider(
            executable="codex",
            models=("codex-reviewer",),
            runner=RecordingCodexRunner(timeout=True),
        )

        with pytest.raises(ProviderOperationError) as error:
            await provider.generate_structured(
                StructuredGenerationRequest(
                    model="codex-reviewer",
                    messages=(
                        ProviderMessage(
                            role=ProviderMessageRole.USER,
                            content="review",
                        ),
                    ),
                    timeoutSeconds=1,
                )
            )

        assert error.value.failure.code == ProviderErrorCode.PROVIDER_TIMEOUT
        assert error.value.failure.retryable is True

    import asyncio

    asyncio.run(scenario())


def test_codex_provider_rejects_tool_loop() -> None:
    async def scenario() -> None:
        provider = CodexProvider(
            executable="codex",
            models=("codex-reviewer",),
            runner=RecordingCodexRunner(),
        )

        with pytest.raises(ProviderOperationError) as error:
            await provider.run_tool_loop(
                ToolLoopRequest(
                    model="codex-reviewer",
                    messages=(
                        ProviderMessage(
                            role=ProviderMessageRole.USER,
                            content="review",
                        ),
                    ),
                )
            )

        assert error.value.failure.code == ProviderErrorCode.TOOL_LOOP_ERROR

    import asyncio

    asyncio.run(scenario())
