"""Tests for independent WorkItem verification."""

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
from maestro.application.controllers import (
    ControllerRegistry,
    ControllerRuntime,
    ReconcileKey,
    ReconcileQueue,
    ReconciliationContext,
    RetryPolicy,
)
from maestro.application.resource_controllers import WorkItemController
from maestro.application.verification import (
    VerificationController,
    VerificationFailureCategory,
    VerificationStatus,
    is_verification_unfinished,
)
from maestro.domain.artifacts import Artifact, ArtifactPhase, ArtifactType
from maestro.domain.events import EventDraft
from maestro.domain.resources import BaseResource, Condition, utc_now
from maestro.domain.work_items import (
    WorkItem,
    WorkItemExecutionReference,
    WorkItemPhase,
    WorkItemPlanReference,
    WorkItemRetryPolicy,
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
    SQLiteWorkItemRepository,
    SQLiteWorkspaceRepository,
)


class RecordingWorkspaceProvider:
    """Workspace provider test double for verification commands."""

    def __init__(
        self,
        results: Iterable[WorkspaceCommandResult] = (),
    ) -> None:
        self.command_requests: list[WorkspaceCommandRequest] = []
        self._results = deque(results)

    async def prepare(self, request: WorkspacePrepareRequest) -> WorkspaceHandle:
        raise NotImplementedError

    async def cleanup(self, handle: WorkspaceHandle) -> None:
        raise NotImplementedError

    async def collect_state(self, handle: WorkspaceHandle) -> WorkspaceState:
        return WorkspaceState(observedRevision="abc123", dirty=False)

    async def collect_diff(self, handle: WorkspaceHandle) -> WorkspaceDiff:
        return WorkspaceDiff()

    async def run_command(
        self,
        handle: WorkspaceHandle,
        request: WorkspaceCommandRequest,
    ) -> WorkspaceCommandResult:
        self.command_requests.append(request)
        if self._results:
            return self._results.popleft()
        return WorkspaceCommandResult(exitCode=0, stdout="ok\n")


class RecordingPublisher:
    """Capture verification events."""

    def __init__(self) -> None:
        self.events: list[EventDraft] = []

    async def publish(self, draft: EventDraft) -> object:
        self.events.append(draft)
        return object()


@dataclass(slots=True)
class VerificationHarness:
    """Test harness for one verification controller."""

    controller: VerificationController
    work_items: SQLiteWorkItemRepository
    workspaces: SQLiteWorkspaceRepository
    artifacts: SQLiteArtifactRepository
    artifact_storage: LocalArtifactStorage
    workspace_provider: RecordingWorkspaceProvider
    publisher: RecordingPublisher
    workspace: Workspace
    work_item: WorkItem

    async def artifact_list(self) -> tuple[Artifact, ...]:
        return await self.artifacts.list_by_work_item(self.work_item.metadata.id)

    async def report_payload(self, artifact_id: UUID) -> dict[str, Any]:
        artifact = await self.artifacts.get(artifact_id)
        return json.loads((await self.artifact_storage.read_bytes(artifact)).decode())

    def close(self) -> None:
        self.work_items.close()
        self.workspaces.close()
        self.artifacts.close()


def context_for(resource: BaseResource[Any, Any]) -> ReconciliationContext:
    """Build a minimal reconciliation context for a resource."""

    return ReconciliationContext(
        key=ReconcileKey(kind=resource.kind, resource_id=resource.metadata.id),
        controller_name="test-controller",
        attempt=1,
        retry_policy=RetryPolicy(),
    )


def condition(resource: BaseResource[Any, Any], condition_type: str) -> Condition:
    """Return one condition from a resource status."""

    return next(
        item for item in resource.status.conditions if item.type == condition_type
    )


def test_verification_controller_marks_work_item_succeeded_from_exit_codes(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        harness = await make_harness(
            tmp_path,
            commands=("pytest -q",),
            command_results=(WorkspaceCommandResult(exitCode=0, stdout="passed\n"),),
        )

        result = await harness.controller.verify_work_item(
            harness.work_item.metadata.id
        )

        updated = await harness.work_items.get(harness.work_item.metadata.id)
        report = await harness.report_payload(result.report_artifact_ref.id)
        artifacts = await harness.artifact_list()

        assert result.status == VerificationStatus.PASSED
        assert updated.status.phase == WorkItemPhase.SUCCEEDED
        assert updated.status.verification.command_results[0].exit_code == 0
        assert updated.status.verification.command_results[0].command == "pytest -q"
        assert condition(updated, "Verification").reason == "VerificationPassed"
        assert harness.workspace_provider.command_requests[0].command == (
            "pytest",
            "-q",
        )
        assert report["status"] == "passed"
        assert report["allCommandsPassed"] is True
        assert report["request"]["commands"] == ["pytest -q"]
        assert {artifact.spec.artifact_type for artifact in artifacts} == {
            ArtifactType.COMMAND_OUTPUT,
            ArtifactType.VERIFICATION_REPORT,
        }
        assert all(
            artifact.status.phase == ArtifactPhase.AVAILABLE for artifact in artifacts
        )
        assert {event.event_type for event in harness.publisher.events} == {
            "VerificationCompleted"
        }
        harness.close()

    asyncio.run(scenario())


def test_verification_controller_records_failed_tests(tmp_path: Path) -> None:
    async def scenario() -> None:
        harness = await make_harness(
            tmp_path,
            commands=("pytest -q",),
            command_results=(
                WorkspaceCommandResult(
                    exitCode=1,
                    stdout="1 failed\n",
                    stderr="assert False\n",
                ),
            ),
        )

        result = await harness.controller.verify_work_item(
            harness.work_item.metadata.id
        )

        updated = await harness.work_items.get(harness.work_item.metadata.id)
        report = await harness.report_payload(result.report_artifact_ref.id)

        assert result.status == VerificationStatus.FAILED
        assert updated.status.phase == WorkItemPhase.FAILED
        assert updated.status.verification.command_results[0].exit_code == 1
        assert condition(updated, "Verification").reason == "VerificationFailed"
        assert report["failureCategory"] == VerificationFailureCategory.COMMAND_FAILED
        assert report["commandResults"][0]["stdout"] == "1 failed\n"
        assert report["commandResults"][0]["stderr"] == "assert False\n"
        assert {event.event_type for event in harness.publisher.events} == {
            "VerificationFailed"
        }
        harness.close()

    asyncio.run(scenario())


def test_verification_controller_enforces_workspace_timeout(tmp_path: Path) -> None:
    async def scenario() -> None:
        harness = await make_harness(
            tmp_path,
            commands=("pytest -q",),
            command_results=(
                WorkspaceCommandResult(
                    exitCode=124,
                    stderr="Command timed out after 5s",
                ),
            ),
            command_timeout_seconds=5,
        )

        result = await harness.controller.verify_work_item(
            harness.work_item.metadata.id
        )

        updated = await harness.work_items.get(harness.work_item.metadata.id)
        report = await harness.report_payload(result.report_artifact_ref.id)

        assert result.status == VerificationStatus.FAILED
        assert harness.workspace_provider.command_requests[0].timeout_seconds == 5
        assert condition(updated, "Verification").reason == "VerificationTimedOut"
        assert (
            report["failureCategory"] == VerificationFailureCategory.COMMAND_TIMED_OUT
        )
        assert report["commandResults"][0]["timedOut"] is True
        harness.close()

    asyncio.run(scenario())


def test_verification_controller_skips_missing_commands(tmp_path: Path) -> None:
    async def scenario() -> None:
        harness = await make_harness(tmp_path, commands=())

        result = await harness.controller.verify_work_item(
            harness.work_item.metadata.id
        )

        updated = await harness.work_items.get(harness.work_item.metadata.id)
        report = await harness.report_payload(result.report_artifact_ref.id)
        artifacts = await harness.artifact_list()

        assert result.status == VerificationStatus.SKIPPED
        assert result.command_artifact_refs == ()
        assert updated.status.phase == WorkItemPhase.SUCCEEDED
        assert condition(updated, "Verification").reason == "VerificationSkipped"
        assert report["failureCategory"] == VerificationFailureCategory.MISSING_COMMANDS
        assert report["status"] == "skipped"
        assert report["repairAllowed"] is False
        assert harness.workspace_provider.command_requests == []
        assert {artifact.spec.artifact_type for artifact in artifacts} == {
            ArtifactType.VERIFICATION_REPORT
        }
        assert {event.event_type for event in harness.publisher.events} == {
            "VerificationSkipped"
        }
        harness.close()

    asyncio.run(scenario())


def test_verification_failure_routes_to_repair_when_attempts_remain(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        harness = await make_harness(
            tmp_path,
            commands=("pytest -q",),
            command_results=(WorkspaceCommandResult(exitCode=1),),
            max_attempts=2,
        )

        result = await harness.controller.verify_work_item(
            harness.work_item.metadata.id
        )
        failed = await harness.work_items.get(harness.work_item.metadata.id)
        await WorkItemController(harness.work_items).reconcile(context_for(failed))
        ready = await harness.work_items.get(harness.work_item.metadata.id)

        assert result.repair_allowed is True
        assert failed.status.phase == WorkItemPhase.FAILED
        assert ready.status.phase == WorkItemPhase.READY
        harness.close()

    asyncio.run(scenario())


def test_verification_controller_recovers_verifying_work_items_after_restart(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        harness = await make_harness(
            tmp_path,
            commands=("pytest -q",),
            command_results=(WorkspaceCommandResult(exitCode=0),),
        )
        registry = ControllerRegistry()
        queue = ReconcileQueue()
        registry.register(harness.controller)
        runtime = ControllerRuntime(registry, queue)
        runtime.start()

        recovered = await runtime.recover(
            "WorkItem",
            harness.work_items,
            is_verification_unfinished,
        )
        run = await runtime.run_once()
        updated = await harness.work_items.get(harness.work_item.metadata.id)

        assert recovered == 1
        assert run.published_events == 0
        assert updated.status.phase == WorkItemPhase.SUCCEEDED
        harness.close()

    asyncio.run(scenario())


async def make_harness(
    tmp_path: Path,
    *,
    commands: tuple[str, ...],
    command_results: tuple[WorkspaceCommandResult, ...] = (),
    command_timeout_seconds: int = 30,
    max_attempts: int = 1,
) -> VerificationHarness:
    work_items = SQLiteWorkItemRepository(":memory:")
    workspaces = SQLiteWorkspaceRepository(":memory:")
    artifacts = SQLiteArtifactRepository(":memory:")
    artifact_storage = LocalArtifactStorage(tmp_path / "artifacts")
    artifact_service = ArtifactService(artifacts, artifact_storage)
    workspace_provider = RecordingWorkspaceProvider(command_results)
    publisher = RecordingPublisher()
    execution_id = uuid4()
    workspace_path = tmp_path / "workspace"
    workspace_path.mkdir()
    workspace = await workspaces.create(
        workspace_resource(
            execution_id,
            workspace_path,
            command_timeout_seconds=command_timeout_seconds,
        )
    )
    work_item = await work_items.create(
        work_item_resource(
            execution_id,
            workspace,
            commands=commands,
            max_attempts=max_attempts,
        )
    )
    ready = await work_items.update_status(
        work_item.metadata.id,
        WorkItemStatus(
            observedGeneration=work_item.metadata.generation,
            phase=WorkItemPhase.READY,
        ),
        expected_resource_version=work_item.metadata.resource_version,
    )
    scheduled = await work_items.update_status(
        ready.metadata.id,
        ready.status.model_copy(update={"phase": WorkItemPhase.SCHEDULED}),
        expected_resource_version=ready.metadata.resource_version,
    )
    running = await work_items.update_status(
        scheduled.metadata.id,
        scheduled.status.model_copy(
            update={
                "phase": WorkItemPhase.RUNNING,
                "attempt": 1,
                "started_at": utc_now(),
            }
        ),
        expected_resource_version=scheduled.metadata.resource_version,
    )
    verifying = await work_items.update_status(
        running.metadata.id,
        running.status.model_copy(update={"phase": WorkItemPhase.VERIFYING}),
        expected_resource_version=running.metadata.resource_version,
    )
    controller = VerificationController(
        work_item_repository=work_items,
        workspace_repository=workspaces,
        workspace_provider=workspace_provider,
        artifact_service=artifact_service,
        event_publisher=publisher,
    )
    return VerificationHarness(
        controller=controller,
        work_items=work_items,
        workspaces=workspaces,
        artifacts=artifacts,
        artifact_storage=artifact_storage,
        workspace_provider=workspace_provider,
        publisher=publisher,
        workspace=workspace,
        work_item=verifying,
    )


def workspace_resource(
    execution_id: UUID,
    path: Path,
    *,
    command_timeout_seconds: int,
) -> Workspace:
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
            policy=WorkspacePolicy(commandTimeoutSeconds=command_timeout_seconds),
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


def work_item_resource(
    execution_id: UUID,
    workspace: Workspace,
    *,
    commands: tuple[str, ...],
    max_attempts: int,
) -> WorkItem:
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
            verification=WorkItemVerificationSpec(commands=commands),
            retryPolicy=WorkItemRetryPolicy(maxAttempts=max_attempts),
            requestedCapabilities=("shell.execute.test",),
        ),
    )
