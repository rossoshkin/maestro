"""Tests for the Coding Role runtime."""

from __future__ import annotations

import asyncio
import json
from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from maestro.application.artifacts import ArtifactService
from maestro.application.coding import (
    CodingOutputStatus,
    CodingRuntime,
    LiteralRuntimeStatus,
    build_coding_input,
)
from maestro.application.tools import CodingToolRuntime
from maestro.domain.agents import (
    Agent,
    AgentCapacity,
    AgentProviderReference,
    AgentSpec,
    AgentSupportedRole,
)
from maestro.domain.artifacts import Artifact, ArtifactType
from maestro.domain.events import EventDraft
from maestro.domain.providers import (
    Provider,
    ProviderFeatureSet,
    ProviderHealth,
    ProviderModelList,
    ProviderPhase,
    ProviderSpec,
    ProviderTokenUsage,
    StructuredGenerationRequest,
    StructuredGenerationResult,
    ToolLoopRequest,
    ToolLoopResult,
)
from maestro.domain.role_invocations import RoleInvocationPhase
from maestro.domain.work_items import (
    WorkItem,
    WorkItemExecutionReference,
    WorkItemPhase,
    WorkItemPlanReference,
    WorkItemRoleReference,
    WorkItemSpec,
    WorkItemStatus,
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
    WorkspacePhase,
    WorkspacePolicy,
    WorkspacePrepareRequest,
    WorkspaceProviderReference,
    WorkspaceSpec,
    WorkspaceState,
    WorkspaceStatus,
)
from maestro.infrastructure.artifacts import LocalArtifactStorage
from maestro.infrastructure.persistence import (
    SQLiteArtifactRepository,
    SQLiteRoleInvocationRepository,
    SQLiteWorkItemRepository,
)


class RecordingModelProvider:
    """Capture tool-loop requests and return queued outputs."""

    def __init__(
        self,
        outputs: Iterable[dict[str, Any]],
        *,
        model: str = "mock-coder",
    ) -> None:
        self.calls: list[ToolLoopRequest] = []
        self._outputs = deque(outputs)
        self._model = model

    async def health(self) -> ProviderHealth:
        return ProviderHealth(
            phase=ProviderPhase.READY,
            capabilities=ProviderFeatureSet(toolCalling=True),
            availableModels=(self._model,),
        )

    async def list_models(self) -> ProviderModelList:
        return ProviderModelList(models=(self._model,))

    async def generate_structured(
        self,
        request: StructuredGenerationRequest,
    ) -> StructuredGenerationResult:
        return StructuredGenerationResult(model=request.model, output={})

    async def run_tool_loop(self, request: ToolLoopRequest) -> ToolLoopResult:
        self.calls.append(request)
        if not self._outputs:
            raise AssertionError("RecordingModelProvider has no queued output")
        output = self._outputs.popleft()
        return ToolLoopResult(
            model=request.model,
            output=output,
            toolCallCount=len(output.get("toolCalls", ())),
            tokenUsage=ProviderTokenUsage(inputTokens=1, outputTokens=1),
        )


class RecordingWorkspaceProvider:
    """Workspace provider test double for Coding runtime tests."""

    def __init__(
        self,
        *,
        status_stdout: str = "",
        diff: str = "",
    ) -> None:
        self.command_requests: list[WorkspaceCommandRequest] = []
        self._status_stdout = status_stdout
        self._diff = diff

    async def prepare(self, request: WorkspacePrepareRequest) -> WorkspaceHandle:
        raise NotImplementedError

    async def cleanup(self, handle: WorkspaceHandle) -> None:
        raise NotImplementedError

    async def collect_state(self, handle: WorkspaceHandle) -> WorkspaceState:
        return WorkspaceState(observedRevision="abc123", dirty=bool(self._diff))

    async def collect_diff(self, handle: WorkspaceHandle) -> WorkspaceDiff:
        return WorkspaceDiff(text=self._diff)

    async def run_command(
        self,
        handle: WorkspaceHandle,
        request: WorkspaceCommandRequest,
    ) -> WorkspaceCommandResult:
        self.command_requests.append(request)
        if request.command == ("git", "status", "--porcelain"):
            return WorkspaceCommandResult(exitCode=0, stdout=self._status_stdout)
        return WorkspaceCommandResult(exitCode=0, stdout="ok\n")


class RecordingPublisher:
    """Capture runtime audit events."""

    def __init__(self) -> None:
        self.events: list[EventDraft] = []

    async def publish(self, draft: EventDraft) -> object:
        self.events.append(draft)
        return object()


class ManualClock:
    """Deterministic monotonic clock for timeout tests."""

    def __init__(self, values: Iterable[float]) -> None:
        self._values = deque(values)
        self._last = 0.0

    def __call__(self) -> float:
        if self._values:
            self._last = self._values.popleft()
        return self._last


@dataclass(slots=True)
class CodingHarness:
    """Repositories, runtime and resources for one Coding runtime test."""

    runtime: CodingRuntime
    work_items: SQLiteWorkItemRepository
    role_invocations: SQLiteRoleInvocationRepository
    artifacts: SQLiteArtifactRepository
    artifact_storage: LocalArtifactStorage
    publisher: RecordingPublisher
    workspace: Workspace
    work_item: WorkItem

    async def artifact_list(self) -> tuple[Artifact, ...]:
        return await self.artifacts.list_by_execution(
            self.work_item.spec.execution_ref.id
        )

    def close(self) -> None:
        self.work_items.close()
        self.role_invocations.close()
        self.artifacts.close()


def completed_output(
    *,
    summary: str = "Implemented the Work Item.",
    changed_path: str = "model-claim.txt",
) -> dict[str, Any]:
    return {
        "status": "completed",
        "summary": summary,
        "changedFiles": ({"path": changed_path, "changeType": "deleted"},),
        "commandsRequested": (),
        "remainingIssues": (),
        "questions": (),
    }


def blocked_output() -> dict[str, Any]:
    return {
        "status": "blocked",
        "summary": "Need product input.",
        "changedFiles": (),
        "commandsRequested": (),
        "remainingIssues": ("Missing target behavior.",),
        "questions": ("Which endpoint should be changed?",),
    }


def tool_call(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    return {"toolCalls": ({"name": name, "arguments": arguments},)}


def test_build_coding_input_includes_workspace_capabilities_and_limits(
    tmp_path: Path,
) -> None:
    workspace = workspace_resource(tmp_path / "workspace")
    item = work_item_resource(uuid4(), workspace)

    coding_input = build_coding_input(
        item,
        workspace,
        granted_capabilities=("filesystem.read", "filesystem.write"),
        max_steps=7,
        max_duration_seconds=30,
        max_command_output_bytes=1024,
    )

    assert coding_input["workItem"]["objective"] == "Implement the assigned change"
    assert coding_input["workspace"]["root"] == str(tmp_path / "workspace")
    assert coding_input["capabilities"]["granted"] == (
        "filesystem.read",
        "filesystem.write",
    )
    assert coding_input["limits"]["maxSteps"] == 7


def test_coding_runtime_creates_file_and_persists_evidence(tmp_path: Path) -> None:
    async def scenario() -> None:
        harness = await make_harness(tmp_path)
        model = RecordingModelProvider(
            (
                tool_call(
                    "write-file",
                    {"path": "hello.txt", "content": "hello\n"},
                ),
                completed_output(summary="Created hello file."),
            )
        )
        workspace_provider = RecordingWorkspaceProvider(
            status_stdout="?? hello.txt\n",
            diff="diff --git a/hello.txt b/hello.txt\n+hello\n",
        )

        result = await harness.runtime.invoke_coding(
            harness.work_item.metadata.id,
            workspace=harness.workspace,
            workspace_provider=workspace_provider,
            agent=agent_resource(),
            provider=provider_resource(),
            runtime=model,
            granted_capabilities=granted_capabilities(),
            max_steps=3,
            max_duration_seconds=60,
        )

        updated_item = await harness.work_items.get(harness.work_item.metadata.id)
        invocation = (
            await harness.role_invocations.list_by_work_item(
                harness.work_item.metadata.id
            )
        )[0]
        artifacts = await harness.artifact_list()
        summary_artifact = await harness.artifacts.get(result.summary_artifact_ref.id)
        summary = json.loads(
            (await harness.artifact_storage.read_bytes(summary_artifact)).decode()
        )

        assert result.status == CodingOutputStatus.COMPLETED
        assert (tmp_path / "workspace" / "hello.txt").read_text() == "hello\n"
        assert result.observed_changed_files[0].path == "hello.txt"
        assert result.output is not None
        assert result.output.changed_files[0].path == "model-claim.txt"
        assert summary["observedChangedFiles"][0]["path"] == "hello.txt"
        assert updated_item.status.phase == WorkItemPhase.VERIFYING
        assert updated_item.status.phase != WorkItemPhase.SUCCEEDED
        assert len(updated_item.status.result_artifact_refs) == 2
        assert invocation.status.phase == RoleInvocationPhase.SUCCEEDED
        assert invocation.spec.granted_capabilities == granted_capabilities()
        assert invocation.status.tool_call_count == 1
        assert {artifact.spec.artifact_type for artifact in artifacts} >= {
            ArtifactType.PROMPT,
            ArtifactType.MODEL_RESPONSE,
            ArtifactType.TOOL_LOG,
            ArtifactType.SUMMARY,
            ArtifactType.GIT_DIFF,
        }
        assert {event.event_type for event in harness.publisher.events} >= {
            "ToolCallRecorded",
            "CodingImplementationProduced",
        }
        harness.close()

    asyncio.run(scenario())


def test_endpoint_fixture_edits_existing_file(tmp_path: Path) -> None:
    async def scenario() -> None:
        harness = await make_harness(tmp_path)
        app = tmp_path / "workspace" / "app.py"
        app.write_text("def health():\n    return 'old'\n")
        model = RecordingModelProvider(
            (
                tool_call("read-file", {"path": "app.py"}),
                tool_call(
                    "edit-file",
                    {
                        "path": "app.py",
                        "oldText": "return 'old'",
                        "newText": "return {'status': 'ok'}",
                    },
                ),
                completed_output(
                    summary="Updated health endpoint.", changed_path="app.py"
                ),
            )
        )
        workspace_provider = RecordingWorkspaceProvider(
            status_stdout=" M app.py\n",
            diff=(
                "diff --git a/app.py b/app.py\n"
                "-return 'old'\n"
                "+return {'status': 'ok'}\n"
            ),
        )

        result = await harness.runtime.invoke_coding(
            harness.work_item.metadata.id,
            workspace=harness.workspace,
            workspace_provider=workspace_provider,
            agent=agent_resource(),
            provider=provider_resource(),
            runtime=model,
            granted_capabilities=granted_capabilities(),
            max_steps=5,
        )

        assert result.status == CodingOutputStatus.COMPLETED
        assert "status" in app.read_text()
        assert result.observed_changed_files[0].change_type == "modified"
        assert len(result.tool_artifact_refs) == 2
        harness.close()

    asyncio.run(scenario())


def test_coding_runtime_enforces_max_steps_before_executing_extra_tools(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        harness = await make_harness(tmp_path)
        model = RecordingModelProvider(
            (
                {
                    "toolCalls": (
                        {
                            "name": "write-file",
                            "arguments": {"path": "a.txt", "content": "a"},
                        },
                        {
                            "name": "write-file",
                            "arguments": {"path": "b.txt", "content": "b"},
                        },
                    )
                },
            )
        )

        result = await harness.runtime.invoke_coding(
            harness.work_item.metadata.id,
            workspace=harness.workspace,
            workspace_provider=RecordingWorkspaceProvider(),
            agent=agent_resource(),
            provider=provider_resource(),
            runtime=model,
            granted_capabilities=granted_capabilities(),
            max_steps=1,
        )

        updated_item = await harness.work_items.get(harness.work_item.metadata.id)
        invocation = (
            await harness.role_invocations.list_by_work_item(
                harness.work_item.metadata.id
            )
        )[0]

        assert result.status == LiteralRuntimeStatus.STEP_LIMIT_EXCEEDED
        assert not (tmp_path / "workspace" / "a.txt").exists()
        assert updated_item.status.phase == WorkItemPhase.FAILED
        assert invocation.status.phase == RoleInvocationPhase.FAILED
        assert invocation.status.failure is not None
        assert invocation.status.failure.reason == "CodingStepLimitExceeded"
        harness.close()

    asyncio.run(scenario())


def test_coding_runtime_rejects_invalid_final_output(tmp_path: Path) -> None:
    async def scenario() -> None:
        harness = await make_harness(tmp_path)
        model = RecordingModelProvider(({"status": "completed"},))

        result = await harness.runtime.invoke_coding(
            harness.work_item.metadata.id,
            workspace=harness.workspace,
            workspace_provider=RecordingWorkspaceProvider(),
            agent=agent_resource(),
            provider=provider_resource(),
            runtime=model,
            granted_capabilities=granted_capabilities(),
        )

        artifacts = await harness.artifact_list()
        invocation = (
            await harness.role_invocations.list_by_work_item(
                harness.work_item.metadata.id
            )
        )[0]

        assert result.status == LiteralRuntimeStatus.INVALID_OUTPUT
        assert invocation.status.phase == RoleInvocationPhase.FAILED
        assert invocation.status.failure is not None
        assert invocation.status.failure.reason == "CodingOutputInvalid"
        assert {artifact.spec.artifact_type for artifact in artifacts} == {
            ArtifactType.PROMPT,
            ArtifactType.MODEL_RESPONSE,
        }
        harness.close()

    asyncio.run(scenario())


def test_coding_runtime_rejects_invalid_tool_call_output(tmp_path: Path) -> None:
    async def scenario() -> None:
        harness = await make_harness(tmp_path)
        model = RecordingModelProvider(({"toolCalls": {"name": "write-file"}},))

        result = await harness.runtime.invoke_coding(
            harness.work_item.metadata.id,
            workspace=harness.workspace,
            workspace_provider=RecordingWorkspaceProvider(),
            agent=agent_resource(),
            provider=provider_resource(),
            runtime=model,
            granted_capabilities=granted_capabilities(),
        )

        updated_item = await harness.work_items.get(harness.work_item.metadata.id)
        invocation = (
            await harness.role_invocations.list_by_work_item(
                harness.work_item.metadata.id
            )
        )[0]

        assert result.status == LiteralRuntimeStatus.INVALID_OUTPUT
        assert updated_item.status.phase == WorkItemPhase.FAILED
        assert invocation.status.phase == RoleInvocationPhase.FAILED
        assert invocation.status.failure is not None
        assert invocation.status.failure.reason == "CodingToolCallsInvalid"
        assert result.tool_artifact_refs == ()
        harness.close()

    asyncio.run(scenario())


def test_coding_runtime_records_blocked_task_without_succeeding_work_item(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        harness = await make_harness(tmp_path)
        model = RecordingModelProvider((blocked_output(),))

        result = await harness.runtime.invoke_coding(
            harness.work_item.metadata.id,
            workspace=harness.workspace,
            workspace_provider=RecordingWorkspaceProvider(),
            agent=agent_resource(),
            provider=provider_resource(),
            runtime=model,
            granted_capabilities=granted_capabilities(),
        )

        updated_item = await harness.work_items.get(harness.work_item.metadata.id)
        invocation = (
            await harness.role_invocations.list_by_work_item(
                harness.work_item.metadata.id
            )
        )[0]

        assert result.status == CodingOutputStatus.BLOCKED
        assert updated_item.status.phase == WorkItemPhase.FAILED
        assert updated_item.status.conditions[0].reason == "BlockedByMissingContext"
        assert invocation.status.phase == RoleInvocationPhase.FAILED
        assert invocation.status.failure is not None
        assert invocation.status.failure.reason == "CodingBlocked"
        assert result.summary_artifact_ref is not None
        assert result.diff_artifact_ref is not None
        harness.close()

    asyncio.run(scenario())


def test_coding_runtime_preserves_workspace_isolation_for_denied_tool_call(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        harness = await make_harness(tmp_path)
        model = RecordingModelProvider(
            (
                tool_call(
                    "write-file",
                    {"path": "../escape.txt", "content": "nope\n"},
                ),
                blocked_output(),
            )
        )

        result = await harness.runtime.invoke_coding(
            harness.work_item.metadata.id,
            workspace=harness.workspace,
            workspace_provider=RecordingWorkspaceProvider(),
            agent=agent_resource(),
            provider=provider_resource(),
            runtime=model,
            granted_capabilities=granted_capabilities(),
        )

        assert result.status == CodingOutputStatus.BLOCKED
        assert not (tmp_path / "escape.txt").exists()
        assert len(result.tool_artifact_refs) == 1
        harness.close()

    asyncio.run(scenario())


def test_coding_runtime_exposes_only_granted_tool_schemas(tmp_path: Path) -> None:
    async def scenario() -> None:
        harness = await make_harness(tmp_path)
        model = RecordingModelProvider((blocked_output(),))

        await harness.runtime.invoke_coding(
            harness.work_item.metadata.id,
            workspace=harness.workspace,
            workspace_provider=RecordingWorkspaceProvider(),
            agent=agent_resource(),
            provider=provider_resource(),
            runtime=model,
            granted_capabilities=("filesystem.read",),
        )

        tool_names = {tool.name for tool in model.calls[0].tools}

        assert tool_names == {"list-files", "read-file"}
        harness.close()

    asyncio.run(scenario())


def test_coding_runtime_enforces_duration_limit(tmp_path: Path) -> None:
    async def scenario() -> None:
        harness = await make_harness(tmp_path, clock=ManualClock((0, 0, 0, 2)))
        model = RecordingModelProvider((completed_output(),))

        result = await harness.runtime.invoke_coding(
            harness.work_item.metadata.id,
            workspace=harness.workspace,
            workspace_provider=RecordingWorkspaceProvider(),
            agent=agent_resource(),
            provider=provider_resource(),
            runtime=model,
            granted_capabilities=granted_capabilities(),
            max_duration_seconds=1,
        )

        updated_item = await harness.work_items.get(harness.work_item.metadata.id)
        invocation = (
            await harness.role_invocations.list_by_work_item(
                harness.work_item.metadata.id
            )
        )[0]

        assert result.status == LiteralRuntimeStatus.DURATION_LIMIT_EXCEEDED
        assert updated_item.status.phase == WorkItemPhase.FAILED
        assert invocation.status.phase == RoleInvocationPhase.TIMED_OUT
        assert result.summary_artifact_ref is None
        harness.close()

    asyncio.run(scenario())


async def make_harness(
    tmp_path: Path,
    *,
    clock: ManualClock | None = None,
) -> CodingHarness:
    workspace_path = tmp_path / "workspace"
    workspace_path.mkdir()
    execution_id = uuid4()
    workspace = workspace_resource(workspace_path, execution_id=execution_id)
    item = work_item_resource(execution_id, workspace)
    work_items = SQLiteWorkItemRepository(":memory:")
    role_invocations = SQLiteRoleInvocationRepository(":memory:")
    artifacts = SQLiteArtifactRepository(":memory:")
    artifact_storage = LocalArtifactStorage(tmp_path / "artifacts")
    artifact_service = ArtifactService(artifacts, artifact_storage)
    publisher = RecordingPublisher()
    tool_runtime = CodingToolRuntime(
        artifact_service=artifact_service,
        event_publisher=publisher,
    )
    runtime = CodingRuntime(
        work_item_repository=work_items,
        role_invocation_repository=role_invocations,
        artifact_service=artifact_service,
        tool_runtime=tool_runtime,
        event_publisher=publisher,
        clock=clock or ManualClock((0,)),
    )
    created = await work_items.create(item)
    ready = await work_items.update_status(
        created.metadata.id,
        WorkItemStatus(
            observedGeneration=created.metadata.generation,
            phase=WorkItemPhase.READY,
        ),
        expected_resource_version=created.metadata.resource_version,
    )
    scheduled = await work_items.update_status(
        ready.metadata.id,
        ready.status.model_copy(update={"phase": WorkItemPhase.SCHEDULED}),
        expected_resource_version=ready.metadata.resource_version,
    )
    return CodingHarness(
        runtime=runtime,
        work_items=work_items,
        role_invocations=role_invocations,
        artifacts=artifacts,
        artifact_storage=artifact_storage,
        publisher=publisher,
        workspace=workspace,
        work_item=scheduled,
    )


def workspace_resource(
    path: Path,
    *,
    execution_id: UUID | None = None,
) -> Workspace:
    execution_id = execution_id or uuid4()
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
            policy=WorkspacePolicy(commandTimeoutSeconds=30),
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
            requestedCapabilities=granted_capabilities(),
        ),
    )


def agent_resource(*, model: str = "mock-coder") -> Agent:
    return Agent.new(
        name="coder-local",
        spec=AgentSpec(
            providerRef=AgentProviderReference(name="ollama-local"),
            model=model,
            supportedRoles=(AgentSupportedRole(name="coding", versions=("v1alpha1",)),),
            capacity=AgentCapacity(maxConcurrentAssignments=1),
        ),
    )


def provider_resource(*, model: str = "mock-coder") -> Provider:
    return Provider.new(
        name="ollama-local",
        spec=ProviderSpec(
            type="ollama",
            endpoint="http://127.0.0.1:11434",
            allowedModels=(model,),
            timeoutSeconds=30,
        ),
    )


def granted_capabilities() -> tuple[str, ...]:
    return (
        "filesystem.read",
        "filesystem.write",
        "filesystem.edit",
        "git.status",
        "git.diff",
        "shell.execute.test",
    )
