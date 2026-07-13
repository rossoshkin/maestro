"""Tests for the safe Coding tool runtime."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID, uuid4

from maestro.application.artifacts import ArtifactService
from maestro.application.tools import CodingToolRuntime, ToolExecutionStatus
from maestro.domain.artifacts import Artifact, ArtifactPhase, ArtifactType
from maestro.domain.events import EventDraft
from maestro.domain.work_items import (
    WorkItem,
    WorkItemExecutionReference,
    WorkItemPlanReference,
    WorkItemRoleReference,
    WorkItemSpec,
    WorkItemVerificationSpec,
    WorkItemWorkspaceReference,
)
from maestro.domain.workspaces import (
    Workspace,
    WorkspaceCommandRequest,
    WorkspaceCommandResult,
    WorkspaceDiff,
    WorkspaceExecutionReference,
    WorkspaceHandle,
    WorkspaceNetworkPolicy,
    WorkspacePhase,
    WorkspacePolicy,
    WorkspacePrepareRequest,
    WorkspaceProviderReference,
    WorkspaceSpec,
    WorkspaceState,
    WorkspaceStatus,
)
from maestro.infrastructure.artifacts import LocalArtifactStorage
from maestro.infrastructure.persistence import SQLiteArtifactRepository


class RecordingPublisher:
    """Capture tool audit events."""

    def __init__(self) -> None:
        self.events: list[EventDraft] = []

    async def publish(self, draft: EventDraft) -> object:
        self.events.append(draft)
        return object()


class RecordingWorkspaceProvider:
    """Workspace provider test double for command and Git tools."""

    def __init__(
        self,
        *,
        command_result: WorkspaceCommandResult | None = None,
        diff: str = "diff --git a/app.py b/app.py\n",
    ) -> None:
        self.command_requests: list[WorkspaceCommandRequest] = []
        self._command_result = command_result or WorkspaceCommandResult(
            exitCode=0,
            stdout="ok\n",
        )
        self._diff = diff

    async def prepare(self, request: WorkspacePrepareRequest) -> WorkspaceHandle:
        raise NotImplementedError

    async def cleanup(self, handle: WorkspaceHandle) -> None:
        raise NotImplementedError

    async def collect_state(self, handle: WorkspaceHandle) -> WorkspaceState:
        return WorkspaceState(observedRevision="abc123", dirty=False)

    async def collect_diff(self, handle: WorkspaceHandle) -> WorkspaceDiff:
        return WorkspaceDiff(text=self._diff)

    async def run_command(
        self,
        handle: WorkspaceHandle,
        request: WorkspaceCommandRequest,
    ) -> WorkspaceCommandResult:
        self.command_requests.append(request)
        return self._command_result


@dataclass(slots=True)
class ToolHarness:
    """Test harness for one tool runtime."""

    runtime: CodingToolRuntime
    artifact_repository: SQLiteArtifactRepository
    artifact_storage: LocalArtifactStorage
    publisher: RecordingPublisher
    workspace: Workspace
    work_item: WorkItem

    async def artifacts(self) -> tuple[Artifact, ...]:
        return await self.artifact_repository.list_by_execution(
            self.work_item.spec.execution_ref.id
        )

    def close(self) -> None:
        self.artifact_repository.close()


def test_registry_exposes_provider_tool_schemas(tmp_path: Path) -> None:
    repository = SQLiteArtifactRepository(":memory:")
    runtime = CodingToolRuntime(
        artifact_service=ArtifactService(
            repository,
            LocalArtifactStorage(tmp_path / "artifacts"),
        )
    )

    tool_names = {
        definition.name for definition in runtime.registry.provider_tool_definitions()
    }

    assert {
        "list-files",
        "read-file",
        "write-file",
        "edit-file",
        "run-command",
        "git-status",
        "git-diff",
    } <= tool_names
    repository.close()


def test_filesystem_tools_persist_artifacts_and_events(tmp_path: Path) -> None:
    async def scenario() -> None:
        harness = make_harness(tmp_path)
        (tmp_path / "workspace" / "app.py").write_text("print('old')\n")

        listed = await harness.runtime.execute_tool(
            "list-files",
            {"path": ".", "recursive": True},
            workspace=harness.workspace,
            work_item=harness.work_item,
            granted_capabilities=("filesystem.read",),
        )
        read = await harness.runtime.execute_tool(
            "read-file",
            {"path": "app.py"},
            workspace=harness.workspace,
            work_item=harness.work_item,
            granted_capabilities=("filesystem.read",),
        )
        written = await harness.runtime.execute_tool(
            "write-file",
            {"path": "new.txt", "content": "hello\n", "executable": True},
            workspace=harness.workspace,
            work_item=harness.work_item,
            granted_capabilities=("filesystem.write",),
        )
        edited = await harness.runtime.execute_tool(
            "edit-file",
            {
                "path": "app.py",
                "oldText": "old",
                "newText": "new",
            },
            workspace=harness.workspace,
            work_item=harness.work_item,
            granted_capabilities=("filesystem.edit",),
        )
        artifacts = await harness.artifacts()

        assert listed.status == ToolExecutionStatus.SUCCEEDED
        assert read.output["content"] == "print('old')\n"
        assert written.output["bytesWritten"] == 6
        assert written.output["executable"] is True
        assert edited.output["occurrencesReplaced"] == 1
        assert (tmp_path / "workspace" / "new.txt").read_text() == "hello\n"
        assert (tmp_path / "workspace" / "new.txt").stat().st_mode & 0o111
        assert (tmp_path / "workspace" / "app.py").read_text() == "print('new')\n"
        assert len(artifacts) == 4
        assert all(
            artifact.status.phase == ArtifactPhase.AVAILABLE for artifact in artifacts
        )
        assert len(harness.publisher.events) == 4
        assert harness.publisher.events[0].event_type == "ToolCallRecorded"
        harness.close()

    asyncio.run(scenario())


def test_capability_denial_persists_without_writing(tmp_path: Path) -> None:
    async def scenario() -> None:
        harness = make_harness(tmp_path)

        result = await harness.runtime.execute_tool(
            "write-file",
            {"path": "blocked.txt", "content": "nope\n"},
            workspace=harness.workspace,
            work_item=harness.work_item,
            granted_capabilities=("filesystem.read",),
        )
        artifacts = await harness.artifacts()

        assert result.status == ToolExecutionStatus.DENIED
        assert not (tmp_path / "workspace" / "blocked.txt").exists()
        assert artifacts[0].spec.artifact_type == ArtifactType.TOOL_LOG
        assert harness.publisher.events[0].payload["status"] == "denied"
        harness.close()

    asyncio.run(scenario())


def test_path_traversal_and_symlink_escape_are_rejected(tmp_path: Path) -> None:
    async def scenario() -> None:
        harness = make_harness(tmp_path)
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "secret.txt").write_text("secret\n")
        (tmp_path / "workspace" / "link").symlink_to(outside, target_is_directory=True)

        traversal = await harness.runtime.execute_tool(
            "read-file",
            {"path": "../outside/secret.txt"},
            workspace=harness.workspace,
            work_item=harness.work_item,
            granted_capabilities=("filesystem.read",),
        )
        symlink = await harness.runtime.execute_tool(
            "read-file",
            {"path": "link/secret.txt"},
            workspace=harness.workspace,
            work_item=harness.work_item,
            granted_capabilities=("filesystem.read",),
        )

        assert traversal.status == ToolExecutionStatus.DENIED
        assert symlink.status == ToolExecutionStatus.DENIED
        assert len(await harness.artifacts()) == 2
        harness.close()

    asyncio.run(scenario())


def test_command_policy_denies_destructive_commands_before_provider_call(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        harness = make_harness(tmp_path)
        provider = RecordingWorkspaceProvider()

        result = await harness.runtime.execute_tool(
            "run-command",
            {"command": ("sudo", "whoami")},
            workspace=harness.workspace,
            work_item=harness.work_item,
            workspace_provider=provider,
            granted_capabilities=("shell.execute.test",),
        )

        assert result.status == ToolExecutionStatus.DENIED
        assert provider.command_requests == []
        harness.close()

    asyncio.run(scenario())


def test_command_arguments_cannot_target_paths_outside_workspace(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        harness = make_harness(tmp_path)
        outside = tmp_path / "outside.txt"
        outside.write_text("secret\n")
        provider = RecordingWorkspaceProvider()

        result = await harness.runtime.execute_tool(
            "run-command",
            {"command": ("cat", str(outside))},
            workspace=harness.workspace,
            work_item=harness.work_item,
            workspace_provider=provider,
            granted_capabilities=("shell.execute.test",),
        )

        assert result.status == ToolExecutionStatus.DENIED
        assert provider.command_requests == []
        harness.close()

    asyncio.run(scenario())


def test_run_command_rejects_shell_redirection_as_plain_argument(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        harness = make_harness(tmp_path)
        provider = RecordingWorkspaceProvider()

        result = await harness.runtime.execute_tool(
            "run-command",
            {"command": ("echo", "'hello world' > hello_world.sh")},
            workspace=harness.workspace,
            work_item=harness.work_item,
            workspace_provider=provider,
            granted_capabilities=("shell.execute.test",),
        )

        assert result.status == ToolExecutionStatus.DENIED
        assert result.message.startswith("run-command executes argv directly")
        assert provider.command_requests == []
        assert not (tmp_path / "workspace" / "hello_world.sh").exists()
        harness.close()

    asyncio.run(scenario())


def test_run_command_caps_timeout_and_truncates_output(tmp_path: Path) -> None:
    async def scenario() -> None:
        harness = make_harness(tmp_path, max_output_bytes=20)
        provider = RecordingWorkspaceProvider(
            command_result=WorkspaceCommandResult(
                exitCode=0,
                stdout="0123456789" * 5,
                stderr="",
            )
        )

        result = await harness.runtime.execute_tool(
            "run-command",
            {
                "command": ("pytest", "-q"),
                "timeoutSeconds": 999,
            },
            workspace=harness.workspace,
            work_item=harness.work_item,
            workspace_provider=provider,
            granted_capabilities=("shell.execute.test",),
        )

        assert result.status == ToolExecutionStatus.SUCCEEDED
        assert result.truncated is True
        assert len(result.output["stdout"].encode("utf-8")) <= 20
        assert provider.command_requests[0].timeout_seconds == 3
        harness.close()

    asyncio.run(scenario())


def test_git_tools_use_capabilities_and_persist_diff_artifact(tmp_path: Path) -> None:
    async def scenario() -> None:
        harness = make_harness(tmp_path)
        provider = RecordingWorkspaceProvider(
            command_result=WorkspaceCommandResult(
                exitCode=0,
                stdout=" M app.py\n",
                stderr="",
            ),
            diff="diff --git a/app.py b/app.py\n+changed\n",
        )

        status = await harness.runtime.execute_tool(
            "git-status",
            {},
            workspace=harness.workspace,
            work_item=harness.work_item,
            workspace_provider=provider,
            granted_capabilities=("git.status",),
        )
        diff = await harness.runtime.execute_tool(
            "git-diff",
            {},
            workspace=harness.workspace,
            work_item=harness.work_item,
            workspace_provider=provider,
            granted_capabilities=("git.diff",),
        )
        artifacts = await harness.artifacts()
        diff_artifact = await harness.artifact_repository.get(diff.artifact_ref.id)
        diff_bytes = await harness.artifact_storage.read_bytes(diff_artifact)

        assert status.output["stdout"] == " M app.py\n"
        assert diff.status == ToolExecutionStatus.SUCCEEDED
        assert diff.output["diff"].startswith("diff --git")
        assert diff_artifact.spec.artifact_type == ArtifactType.GIT_DIFF
        assert diff_bytes.decode("utf-8") == diff.output["diff"]
        assert len(artifacts) == 2
        harness.close()

    asyncio.run(scenario())


def make_harness(
    tmp_path: Path,
    *,
    max_output_bytes: int = 64 * 1024,
) -> ToolHarness:
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    execution_id = uuid4()
    workspace = ready_workspace(workspace_root, execution_id=execution_id)
    work_item = work_item_resource(execution_id, workspace)
    artifact_repository = SQLiteArtifactRepository(":memory:")
    artifact_storage = LocalArtifactStorage(tmp_path / "artifacts")
    publisher = RecordingPublisher()
    runtime = CodingToolRuntime(
        artifact_service=ArtifactService(artifact_repository, artifact_storage),
        event_publisher=publisher,
        max_output_bytes=max_output_bytes,
    )
    return ToolHarness(
        runtime=runtime,
        artifact_repository=artifact_repository,
        artifact_storage=artifact_storage,
        publisher=publisher,
        workspace=workspace,
        work_item=work_item,
    )


def ready_workspace(path: Path, *, execution_id: UUID) -> Workspace:
    workspace = Workspace.new(
        name="execution-backend",
        spec=WorkspaceSpec(
            executionRef=WorkspaceExecutionReference(
                id=execution_id,
                name="implement-health",
            ),
            repositoryRef="backend",
            providerRef=WorkspaceProviderReference(name="local-git-worktree"),
            baseRevision="main",
            branchName="maestro/execution-123",
            policy=WorkspacePolicy(
                network=WorkspaceNetworkPolicy.DENY,
                commandTimeoutSeconds=3,
            ),
        ),
    )
    return Workspace(
        metadata=workspace.metadata,
        spec=workspace.spec,
        status=WorkspaceStatus(
            observedGeneration=workspace.metadata.generation,
            phase=WorkspacePhase.READY,
            path=path,
            observedRevision="abc123",
        ),
    )


def work_item_resource(execution_id: UUID, workspace: Workspace) -> WorkItem:
    return WorkItem.new(
        name="add-health",
        spec=WorkItemSpec(
            executionRef=WorkItemExecutionReference(
                id=execution_id,
                name="implement-health",
            ),
            planRef=WorkItemPlanReference(
                id=uuid4(),
                name="plan-1",
                version=1,
            ),
            planWorkItemId="add-health",
            roleRef=WorkItemRoleReference(name="coding", version="v1alpha1"),
            repositoryRef="backend",
            workspaceRef=WorkItemWorkspaceReference(
                id=workspace.metadata.id,
                name=workspace.metadata.name,
            ),
            objective="Implement the assigned change",
            acceptanceCriteria=("Change satisfies the goal",),
            verification=WorkItemVerificationSpec(commands=("pytest -q",)),
            requestedCapabilities=(
                "filesystem.read",
                "filesystem.write",
                "filesystem.edit",
                "git.status",
                "git.diff",
                "shell.execute.test",
            ),
        ),
    )
