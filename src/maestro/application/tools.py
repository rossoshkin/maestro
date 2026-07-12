"""Safe Coding Role tool runtime."""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any
from uuid import uuid4

from pydantic import Field, ValidationError

from maestro.application.artifacts import ArtifactService
from maestro.domain.artifacts import (
    Artifact,
    ArtifactExecutionReference,
    ArtifactProducer,
    ArtifactRoleInvocationReference,
    ArtifactType,
    ArtifactWorkItemReference,
)
from maestro.domain.capabilities import CapabilityName
from maestro.domain.events import (
    EventDraft,
    EventExecutionReference,
    EventPayload,
    EventPublisher,
)
from maestro.domain.exceptions import CapabilityPolicyDeniedError
from maestro.domain.providers import ProviderToolDefinition
from maestro.domain.resources import MaestroModel, ResourceName, ResourceReference
from maestro.domain.role_invocations import RoleInvocation
from maestro.domain.work_items import WorkItem
from maestro.domain.workspaces import (
    Workspace,
    WorkspaceCommandRequest,
    WorkspaceHandle,
    WorkspacePhase,
    WorkspaceProvider,
    WorkspaceProviderError,
    resolve_workspace_child,
)

TOOL_RUNTIME = "coding-tool-runtime"
DEFAULT_MAX_OUTPUT_BYTES = 64 * 1024
TRUNCATION_MARKER = "\n[truncated]"
SECRET_PATH_PARTS = frozenset(
    {
        ".aws",
        ".env",
        ".git",
        ".gnupg",
        ".ssh",
        "id_dsa",
        "id_ed25519",
        "id_rsa",
    }
)
DENIED_EXECUTABLES = frozenset(
    {
        "dd",
        "mkfs",
        "reboot",
        "shutdown",
        "su",
        "sudo",
    }
)
SHELL_EXECUTABLES = frozenset({"bash", "fish", "sh", "zsh"})


class ToolExecutionStatus(StrEnum):
    """Normalized Coding tool outcomes."""

    SUCCEEDED = "succeeded"
    FAILED = "failed"
    DENIED = "denied"


class ToolPathEntry(MaestroModel):
    """One list-files result entry."""

    path: str
    kind: str
    size_bytes: int | None = Field(default=None, alias="sizeBytes")


class ToolExecutionResult(MaestroModel):
    """Result returned to the model loop after one tool call."""

    tool_name: ResourceName = Field(alias="toolName")
    status: ToolExecutionStatus
    output: dict[str, Any] = Field(default_factory=dict)
    artifact_ref: ResourceReference = Field(alias="artifactRef")
    required_capability: CapabilityName = Field(alias="requiredCapability")
    message: str = ""
    truncated: bool = False


class ListFilesInput(MaestroModel):
    """Input for the list-files tool."""

    path: str = "."
    recursive: bool = False
    max_entries: int = Field(default=200, ge=1, le=1000, alias="maxEntries")


class ReadFileInput(MaestroModel):
    """Input for the read-file tool."""

    path: str = Field(min_length=1)
    max_bytes: int = Field(
        default=DEFAULT_MAX_OUTPUT_BYTES,
        ge=1,
        alias="maxBytes",
    )


class WriteFileInput(MaestroModel):
    """Input for the write-file tool."""

    path: str = Field(min_length=1)
    content: str


class EditFileInput(MaestroModel):
    """Input for the edit-file tool."""

    path: str = Field(min_length=1)
    old_text: str = Field(min_length=1, alias="oldText")
    new_text: str = Field(alias="newText")
    expected_occurrences: int = Field(default=1, ge=1, alias="expectedOccurrences")


class RunCommandInput(MaestroModel):
    """Input for the run-command tool."""

    command: tuple[str, ...] = Field(min_length=1)
    cwd: str | None = None
    timeout_seconds: int | None = Field(default=None, ge=1, alias="timeoutSeconds")
    capability: CapabilityName = "shell.execute.test"


class GitStatusInput(MaestroModel):
    """Input for the git-status tool."""

    porcelain: bool = True


class GitDiffInput(MaestroModel):
    """Input for the git-diff tool."""

    staged: bool = False


@dataclass(frozen=True, slots=True)
class ToolDefinition:
    """Static metadata for one Coding tool."""

    name: ResourceName
    description: str
    input_model: type[MaestroModel]
    required_capabilities: tuple[CapabilityName, ...]

    def provider_tool_definition(self) -> ProviderToolDefinition:
        """Return the provider-facing tool schema."""

        return ProviderToolDefinition(
            name=self.name,
            description=self.description,
            inputSchema=self.input_model.model_json_schema(by_alias=True),
        )


class CodingToolRegistry:
    """Registry of Coding tools exposed to provider tool loops."""

    def __init__(
        self,
        definitions: tuple[ToolDefinition, ...] | None = None,
    ) -> None:
        self._definitions = {
            definition.name: definition
            for definition in (definitions or default_tool_definitions())
        }

    def get(self, name: str) -> ToolDefinition:
        """Return a tool definition by name."""

        try:
            return self._definitions[name]
        except KeyError as error:
            raise ToolRuntimeError(f"Unknown tool: {name}") from error

    def list(self) -> tuple[ToolDefinition, ...]:
        """List registered tool definitions in deterministic order."""

        return tuple(self._definitions[name] for name in sorted(self._definitions))

    def provider_tool_definitions(self) -> tuple[ProviderToolDefinition, ...]:
        """Return provider tool definitions."""

        return tuple(
            definition.provider_tool_definition() for definition in self.list()
        )


class CodingToolRuntime:
    """Execute Coding Role tools inside a prepared Workspace."""

    def __init__(
        self,
        *,
        artifact_service: ArtifactService,
        registry: CodingToolRegistry | None = None,
        event_publisher: EventPublisher | None = None,
        max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES,
    ) -> None:
        if max_output_bytes < 1:
            raise ValueError("max_output_bytes must be at least 1")
        self._artifact_service = artifact_service
        self._registry = registry or CodingToolRegistry()
        self._event_publisher = event_publisher
        self._max_output_bytes = max_output_bytes

    @property
    def registry(self) -> CodingToolRegistry:
        """Return the configured tool registry."""

        return self._registry

    async def execute_tool(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        workspace: Workspace,
        work_item: WorkItem,
        granted_capabilities: tuple[CapabilityName, ...],
        workspace_provider: WorkspaceProvider | None = None,
        role_invocation: RoleInvocation | None = None,
    ) -> ToolExecutionResult:
        """Execute one tool call and persist an auditable result Artifact."""

        definition = self._registry.get(tool_name)
        required_capability = definition.required_capabilities[0]
        status = ToolExecutionStatus.SUCCEEDED
        output: dict[str, Any] = {}
        message = ""
        truncated = False
        artifact_type = ArtifactType.TOOL_LOG
        media_type = "application/json"
        raw_content: bytes | None = None

        try:
            tool_input = definition.input_model.model_validate(arguments)
            required_capability = _required_capability(definition, tool_input)
            _ensure_capability(required_capability, granted_capabilities)
            (
                output,
                truncated,
                artifact_type,
                media_type,
                raw_content,
            ) = await self._execute_validated_tool(
                definition,
                tool_input,
                workspace=workspace,
                workspace_provider=workspace_provider,
            )
        except CapabilityPolicyDeniedError as error:
            status = ToolExecutionStatus.DENIED
            message = error.message or str(error)
            output = {"error": message, "reason": error.reason}
        except ToolPolicyDeniedError as error:
            status = ToolExecutionStatus.DENIED
            message = error.message
            output = {"error": error.message, "reason": error.reason}
        except (ValidationError, ToolRuntimeError, WorkspaceProviderError) as error:
            status = ToolExecutionStatus.FAILED
            message = str(error)
            output = {"error": message}

        artifact = await self._persist_tool_artifact(
            tool_name=definition.name,
            arguments=arguments,
            work_item=work_item,
            role_invocation=role_invocation,
            status=status,
            output=output,
            required_capability=required_capability,
            message=message,
            truncated=truncated,
            artifact_type=(
                artifact_type
                if status == ToolExecutionStatus.SUCCEEDED
                else ArtifactType.TOOL_LOG
            ),
            media_type=(
                media_type
                if status == ToolExecutionStatus.SUCCEEDED
                else "application/json"
            ),
            raw_content=raw_content
            if status == ToolExecutionStatus.SUCCEEDED
            else None,
        )
        result = ToolExecutionResult(
            toolName=definition.name,
            status=status,
            output=output,
            artifactRef=_resource_ref(artifact),
            requiredCapability=required_capability,
            message=message,
            truncated=truncated,
        )
        await self._publish_tool_event(work_item, artifact, result)
        return result

    async def _execute_validated_tool(
        self,
        definition: ToolDefinition,
        tool_input: MaestroModel,
        *,
        workspace: Workspace,
        workspace_provider: WorkspaceProvider | None,
    ) -> tuple[dict[str, Any], bool, ArtifactType, str, bytes | None]:
        root = _workspace_root(workspace)
        match definition.name:
            case "list-files":
                assert isinstance(tool_input, ListFilesInput)
                output, truncated = _list_files(root, tool_input)
                return (
                    output,
                    truncated,
                    ArtifactType.TOOL_LOG,
                    "application/json",
                    None,
                )
            case "read-file":
                assert isinstance(tool_input, ReadFileInput)
                output, truncated = _read_file(root, tool_input, self._max_output_bytes)
                return (
                    output,
                    truncated,
                    ArtifactType.TOOL_LOG,
                    "application/json",
                    None,
                )
            case "write-file":
                assert isinstance(tool_input, WriteFileInput)
                output = _write_file(root, tool_input)
                return output, False, ArtifactType.TOOL_LOG, "application/json", None
            case "edit-file":
                assert isinstance(tool_input, EditFileInput)
                output = _edit_file(root, tool_input)
                return output, False, ArtifactType.TOOL_LOG, "application/json", None
            case "run-command":
                assert isinstance(tool_input, RunCommandInput)
                output, truncated = await _run_command(
                    workspace,
                    root,
                    tool_input,
                    workspace_provider,
                    max_output_bytes=self._max_output_bytes,
                )
                return (
                    output,
                    truncated,
                    ArtifactType.COMMAND_OUTPUT,
                    "application/json",
                    None,
                )
            case "git-status":
                assert isinstance(tool_input, GitStatusInput)
                output, truncated = await _git_status(
                    workspace,
                    root,
                    tool_input,
                    workspace_provider,
                    max_output_bytes=self._max_output_bytes,
                )
                return (
                    output,
                    truncated,
                    ArtifactType.COMMAND_OUTPUT,
                    "application/json",
                    None,
                )
            case "git-diff":
                assert isinstance(tool_input, GitDiffInput)
                output, truncated = await _git_diff(
                    workspace,
                    root,
                    tool_input,
                    workspace_provider,
                    max_output_bytes=self._max_output_bytes,
                )
                return (
                    output,
                    truncated,
                    ArtifactType.GIT_DIFF,
                    "text/x-diff",
                    output["diff"].encode("utf-8"),
                )
            case _:
                raise ToolRuntimeError(f"Unhandled tool: {definition.name}")

    async def _persist_tool_artifact(
        self,
        *,
        tool_name: ResourceName,
        arguments: dict[str, Any],
        work_item: WorkItem,
        role_invocation: RoleInvocation | None,
        status: ToolExecutionStatus,
        output: dict[str, Any],
        required_capability: CapabilityName,
        message: str,
        truncated: bool,
        artifact_type: ArtifactType,
        media_type: str,
        raw_content: bytes | None,
    ) -> Artifact:
        content = raw_content or _json_bytes(
            {
                "toolName": tool_name,
                "status": status,
                "requiredCapability": required_capability,
                "arguments": _audit_arguments(arguments),
                "output": output,
                "message": message,
                "truncated": truncated,
            }
        )
        artifact = await self._artifact_service.create_bytes_artifact(
            name=_artifact_name(tool_name),
            execution_ref=ArtifactExecutionReference(
                id=work_item.spec.execution_ref.id,
                name=work_item.spec.execution_ref.name,
            ),
            work_item_ref=ArtifactWorkItemReference(
                id=work_item.metadata.id,
                name=work_item.metadata.name,
            ),
            artifact_type=artifact_type,
            media_type=media_type,
            content=content,
            producer=ArtifactProducer(
                subsystem=TOOL_RUNTIME,
                roleInvocationRef=(
                    ArtifactRoleInvocationReference(
                        id=role_invocation.metadata.id,
                        name=role_invocation.metadata.name,
                    )
                    if role_invocation is not None
                    else None
                ),
            ),
        )
        return await self._artifact_service.verify_artifact(
            artifact,
            expected_resource_version=artifact.metadata.resource_version,
        )

    async def _publish_tool_event(
        self,
        work_item: WorkItem,
        artifact: Artifact,
        result: ToolExecutionResult,
    ) -> None:
        if self._event_publisher is None:
            return
        await self._event_publisher.publish(
            EventDraft(
                type="ToolCallRecorded",
                producer=TOOL_RUNTIME,
                correlationId=(f"tool:{work_item.metadata.id}:{artifact.metadata.id}"),
                executionRef=EventExecutionReference(
                    id=work_item.spec.execution_ref.id,
                    name=work_item.spec.execution_ref.name,
                ),
                subjectRef=_resource_ref(artifact),
                payload=_event_payload(result),
            )
        )


class ToolRuntimeError(Exception):
    """Raised for non-policy tool runtime failures."""


class ToolPolicyDeniedError(ToolRuntimeError):
    """Raised when a tool request violates Workspace or command policy."""

    def __init__(self, reason: str, message: str) -> None:
        self.reason = reason
        self.message = message
        super().__init__(message)


def default_tool_definitions() -> tuple[ToolDefinition, ...]:
    """Return the default Coding tool definitions."""

    return (
        ToolDefinition(
            name="list-files",
            description="List files and directories inside the Workspace.",
            input_model=ListFilesInput,
            required_capabilities=("filesystem.read",),
        ),
        ToolDefinition(
            name="read-file",
            description="Read a text file inside the Workspace.",
            input_model=ReadFileInput,
            required_capabilities=("filesystem.read",),
        ),
        ToolDefinition(
            name="write-file",
            description="Write a text file inside the Workspace.",
            input_model=WriteFileInput,
            required_capabilities=("filesystem.write",),
        ),
        ToolDefinition(
            name="edit-file",
            description="Replace exact text in a file inside the Workspace.",
            input_model=EditFileInput,
            required_capabilities=("filesystem.edit",),
        ),
        ToolDefinition(
            name="run-command",
            description="Run a non-destructive command inside the Workspace.",
            input_model=RunCommandInput,
            required_capabilities=("shell.execute.test",),
        ),
        ToolDefinition(
            name="git-status",
            description="Return Git status for the Workspace.",
            input_model=GitStatusInput,
            required_capabilities=("git.status",),
        ),
        ToolDefinition(
            name="git-diff",
            description="Return Git diff for the Workspace.",
            input_model=GitDiffInput,
            required_capabilities=("git.diff",),
        ),
    )


def _required_capability(
    definition: ToolDefinition,
    tool_input: MaestroModel,
) -> CapabilityName:
    if definition.name == "run-command":
        assert isinstance(tool_input, RunCommandInput)
        capability = tool_input.capability
        if not (
            capability == "shell.execute" or capability.startswith("shell.execute.")
        ):
            raise ToolPolicyDeniedError(
                "InvalidCommandCapability",
                "run-command capability must be shell.execute or a child capability",
            )
        return capability
    return definition.required_capabilities[0]


def _ensure_capability(
    required_capability: CapabilityName,
    granted_capabilities: tuple[CapabilityName, ...],
) -> None:
    if required_capability not in granted_capabilities:
        raise CapabilityPolicyDeniedError(
            "ToolCapabilityDenied",
            f"{required_capability} is not granted for this invocation",
        )


def _workspace_root(workspace: Workspace) -> Path:
    if workspace.status.phase not in {
        WorkspacePhase.READY,
        WorkspacePhase.IN_USE,
        WorkspacePhase.DIRTY,
    }:
        raise ToolPolicyDeniedError(
            "WorkspaceNotReady",
            f"Workspace is {workspace.status.phase}",
        )
    if workspace.status.path is None:
        raise ToolPolicyDeniedError("WorkspaceMissingPath", "Workspace has no path")
    if workspace.status.path.is_symlink():
        raise ToolPolicyDeniedError(
            "WorkspaceSymlinkDenied",
            "Workspace path must not be a symlink",
        )
    try:
        root = workspace.status.path.resolve(strict=True)
    except OSError as error:
        raise ToolPolicyDeniedError("WorkspaceMissing", str(error)) from error
    if not root.is_dir():
        raise ToolPolicyDeniedError(
            "WorkspaceInvalidPath",
            "Workspace path must be a directory",
        )
    return root


def _list_files(
    root: Path,
    tool_input: ListFilesInput,
) -> tuple[dict[str, Any], bool]:
    directory = _resolve_path(
        root,
        tool_input.path,
        must_exist=True,
        must_be_dir=True,
        capability="filesystem.read",
    )
    paths = directory.rglob("*") if tool_input.recursive else directory.iterdir()
    entries: list[ToolPathEntry] = []
    truncated = False
    for child in sorted(paths, key=lambda path: _relative_path(root, path)):
        if _has_denied_path_part(child, root):
            continue
        kind = "directory" if child.is_dir() else "file" if child.is_file() else None
        if kind is None:
            continue
        entries.append(
            ToolPathEntry(
                path=_relative_path(root, child),
                kind=kind,
                sizeBytes=child.stat().st_size if kind == "file" else None,
            )
        )
        if len(entries) >= tool_input.max_entries:
            truncated = True
            break
    return {
        "path": _relative_path(root, directory),
        "entries": tuple(
            entry.model_dump(mode="json", by_alias=True) for entry in entries
        ),
    }, truncated


def _read_file(
    root: Path,
    tool_input: ReadFileInput,
    runtime_max_output_bytes: int,
) -> tuple[dict[str, Any], bool]:
    target = _resolve_path(
        root,
        tool_input.path,
        must_exist=True,
        must_be_file=True,
        capability="filesystem.read",
    )
    max_bytes = min(tool_input.max_bytes, runtime_max_output_bytes)
    content = target.read_bytes()
    text, truncated = _truncate_text(
        content.decode("utf-8", errors="replace"),
        max_bytes,
    )
    return {
        "path": _relative_path(root, target),
        "content": text,
        "sizeBytes": len(content),
    }, truncated


def _write_file(root: Path, tool_input: WriteFileInput) -> dict[str, Any]:
    target = _resolve_path(
        root,
        tool_input.path,
        must_exist=False,
        capability="filesystem.write",
    )
    if target.exists() and not target.is_file():
        raise ToolPolicyDeniedError(
            "InvalidFileTarget",
            "write-file target must be a regular file",
        )
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(tool_input.content)
    return {
        "path": _relative_path(root, target),
        "bytesWritten": len(tool_input.content.encode("utf-8")),
    }


def _edit_file(root: Path, tool_input: EditFileInput) -> dict[str, Any]:
    target = _resolve_path(
        root,
        tool_input.path,
        must_exist=True,
        must_be_file=True,
        capability="filesystem.edit",
    )
    current = target.read_text()
    occurrences = current.count(tool_input.old_text)
    if occurrences != tool_input.expected_occurrences:
        raise ToolRuntimeError(
            "edit-file expected "
            f"{tool_input.expected_occurrences} occurrence(s), found {occurrences}"
        )
    updated = current.replace(
        tool_input.old_text,
        tool_input.new_text,
        tool_input.expected_occurrences,
    )
    target.write_text(updated)
    return {
        "path": _relative_path(root, target),
        "occurrencesReplaced": occurrences,
        "bytesWritten": len(updated.encode("utf-8")),
    }


async def _run_command(
    workspace: Workspace,
    root: Path,
    tool_input: RunCommandInput,
    workspace_provider: WorkspaceProvider | None,
    *,
    max_output_bytes: int,
) -> tuple[dict[str, Any], bool]:
    provider = _require_workspace_provider(workspace_provider)
    _validate_command_policy(tool_input.command)
    _validate_command_path_arguments(root, tool_input.command, tool_input.capability)
    cwd = None
    if tool_input.cwd is not None:
        cwd = _resolve_path(
            root,
            tool_input.cwd,
            must_exist=True,
            must_be_dir=True,
            capability=tool_input.capability,
        )
    timeout_seconds = min(
        tool_input.timeout_seconds or workspace.spec.policy.command_timeout_seconds,
        workspace.spec.policy.command_timeout_seconds,
    )
    result = await provider.run_command(
        _workspace_handle(workspace, root),
        WorkspaceCommandRequest(
            command=tool_input.command,
            cwd=cwd,
            timeoutSeconds=timeout_seconds,
        ),
    )
    stdout, stdout_truncated = _truncate_text(result.stdout, max_output_bytes)
    stderr, stderr_truncated = _truncate_text(result.stderr, max_output_bytes)
    return {
        "command": tool_input.command,
        "cwd": _relative_path(root, cwd) if cwd is not None else ".",
        "exitCode": result.exit_code,
        "stdout": stdout,
        "stderr": stderr,
        "timeoutSeconds": timeout_seconds,
    }, stdout_truncated or stderr_truncated


async def _git_status(
    workspace: Workspace,
    root: Path,
    tool_input: GitStatusInput,
    workspace_provider: WorkspaceProvider | None,
    *,
    max_output_bytes: int,
) -> tuple[dict[str, Any], bool]:
    provider = _require_workspace_provider(workspace_provider)
    command = ("git", "status", "--porcelain" if tool_input.porcelain else "--short")
    result = await provider.run_command(
        _workspace_handle(workspace, root),
        WorkspaceCommandRequest(
            command=command,
            timeoutSeconds=min(30, workspace.spec.policy.command_timeout_seconds),
        ),
    )
    stdout, stdout_truncated = _truncate_text(result.stdout, max_output_bytes)
    stderr, stderr_truncated = _truncate_text(result.stderr, max_output_bytes)
    return {
        "command": command,
        "exitCode": result.exit_code,
        "stdout": stdout,
        "stderr": stderr,
    }, stdout_truncated or stderr_truncated


async def _git_diff(
    workspace: Workspace,
    root: Path,
    tool_input: GitDiffInput,
    workspace_provider: WorkspaceProvider | None,
    *,
    max_output_bytes: int,
) -> tuple[dict[str, Any], bool]:
    provider = _require_workspace_provider(workspace_provider)
    if tool_input.staged:
        result = await provider.run_command(
            _workspace_handle(workspace, root),
            WorkspaceCommandRequest(
                command=("git", "diff", "--cached", "--binary"),
                timeoutSeconds=min(30, workspace.spec.policy.command_timeout_seconds),
            ),
        )
        diff = result.stdout if result.exit_code == 0 else result.stderr
    else:
        diff = (await provider.collect_diff(_workspace_handle(workspace, root))).text
    text, truncated = _truncate_text(diff, max_output_bytes)
    return {"diff": text, "staged": tool_input.staged}, truncated


def _resolve_path(
    root: Path,
    requested_path: str | Path,
    *,
    capability: str,
    must_exist: bool,
    must_be_file: bool = False,
    must_be_dir: bool = False,
) -> Path:
    try:
        target = resolve_workspace_child(root, requested_path)
    except ValueError as error:
        raise ToolPolicyDeniedError(
            "WorkspacePathDenied",
            f"{capability} denied path outside Workspace",
        ) from error
    _reject_secret_path(target, root)
    if must_exist and not target.exists():
        raise ToolRuntimeError(f"Workspace path does not exist: {requested_path}")
    if must_be_file and not target.is_file():
        raise ToolRuntimeError(f"Workspace path is not a file: {requested_path}")
    if must_be_dir and not target.is_dir():
        raise ToolRuntimeError(f"Workspace path is not a directory: {requested_path}")
    return target


def _reject_secret_path(path: Path, root: Path) -> None:
    relative_parts = tuple(part.lower() for part in path.relative_to(root).parts)
    for part in relative_parts:
        if part in SECRET_PATH_PARTS or part.startswith(".env"):
            raise ToolPolicyDeniedError(
                "SecretPathDenied",
                f"Access to {part} is denied by Workspace policy",
            )


def _has_denied_path_part(path: Path, root: Path) -> bool:
    try:
        _reject_secret_path(path, root)
    except ToolPolicyDeniedError:
        return True
    return False


def _validate_command_policy(command: tuple[str, ...]) -> None:
    executable = Path(command[0]).name
    args = command[1:]
    if executable in DENIED_EXECUTABLES:
        raise ToolPolicyDeniedError(
            "CommandDenied",
            f"{executable} is denied by command policy",
        )
    if executable in SHELL_EXECUTABLES and args[:1] == ("-c",):
        raise ToolPolicyDeniedError(
            "CommandDenied",
            "shell -c commands are denied by command policy",
        )
    if executable == "rm" and _has_flag(args, "r") and _has_flag(args, "f"):
        raise ToolPolicyDeniedError(
            "CommandDenied",
            "rm -rf is denied by command policy",
        )
    if executable == "git" and args[:1] == ("push",):
        raise ToolPolicyDeniedError(
            "CommandDenied",
            "git push is denied by command policy",
        )
    if executable == "git" and args[:1] == ("reset",) and "--hard" in args:
        raise ToolPolicyDeniedError(
            "CommandDenied",
            "git reset --hard is denied by command policy",
        )
    if executable == "docker" and args[:2] == ("system", "prune"):
        raise ToolPolicyDeniedError(
            "CommandDenied",
            "docker system prune is denied by command policy",
        )


def _validate_command_path_arguments(
    root: Path,
    command: tuple[str, ...],
    capability: CapabilityName,
) -> None:
    for argument in command[1:]:
        for candidate in _path_candidates(argument):
            _resolve_path(
                root,
                candidate,
                must_exist=False,
                capability=capability,
            )


def _path_candidates(argument: str) -> tuple[str, ...]:
    candidates: list[str] = []
    if _looks_like_path(argument):
        candidates.append(argument)
    if argument.startswith("-") and "=" in argument:
        value = argument.split("=", 1)[1]
        if _looks_like_path(value):
            candidates.append(value)
    return tuple(candidates)


def _looks_like_path(value: str) -> bool:
    if "://" in value:
        return False
    return value.startswith(("/", "./", "../", "~")) or "/" in value or "\\" in value


def _has_flag(args: tuple[str, ...], flag: str) -> bool:
    return any(
        arg == f"-{flag}" or (arg.startswith("-") and flag in arg) for arg in args
    )


def _truncate_text(value: str, max_bytes: int) -> tuple[str, bool]:
    encoded = value.encode("utf-8")
    if len(encoded) <= max_bytes:
        return value, False
    marker = TRUNCATION_MARKER.encode("utf-8")
    if max_bytes <= len(marker):
        return encoded[:max_bytes].decode("utf-8", errors="ignore"), True
    budget = max_bytes - len(marker)
    text = encoded[:budget].decode("utf-8", errors="ignore")
    return f"{text}{TRUNCATION_MARKER}", True


def _require_workspace_provider(
    workspace_provider: WorkspaceProvider | None,
) -> WorkspaceProvider:
    if workspace_provider is None:
        raise ToolRuntimeError("WorkspaceProvider is required for this tool")
    return workspace_provider


def _workspace_handle(workspace: Workspace, root: Path) -> WorkspaceHandle:
    return WorkspaceHandle(
        path=root,
        observedRevision=workspace.status.observed_revision or "unknown",
    )


def _relative_path(root: Path, path: Path) -> str:
    if path == root:
        return "."
    return path.relative_to(root).as_posix()


def _audit_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    audited: dict[str, Any] = {}
    for key, value in arguments.items():
        if key in {"content", "newText", "oldText"} and isinstance(value, str):
            audited[key] = {"bytes": len(value.encode("utf-8"))}
        else:
            audited[key] = value
    return audited


def _event_payload(result: ToolExecutionResult) -> EventPayload:
    return {
        "toolName": result.tool_name,
        "status": result.status,
        "requiredCapability": result.required_capability,
        "message": result.message,
        "truncated": result.truncated,
        "artifactId": str(result.artifact_ref.id),
    }


def _artifact_name(tool_name: ResourceName) -> ResourceName:
    return f"tool-{tool_name}-{uuid4().hex[:12]}"


def _resource_ref(resource: Artifact) -> ResourceReference:
    return ResourceReference(
        kind=resource.kind,
        id=resource.metadata.id,
        name=resource.metadata.name,
    )


def _json_bytes(value: Any) -> bytes:
    return json.dumps(value, indent=2, sort_keys=True).encode("utf-8")
