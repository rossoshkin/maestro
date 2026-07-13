"""Independent WorkItem verification controller."""

from __future__ import annotations

import json
import shlex
from collections.abc import Callable
from enum import StrEnum
from pathlib import Path
from time import monotonic
from typing import Any, cast
from uuid import UUID, uuid4

from pydantic import Field

from maestro.application.artifacts import ArtifactService
from maestro.application.controllers import (
    ReconcileResult,
    ReconciliationContext,
    observe_generation,
    with_condition,
)
from maestro.application.tools import (
    ToolPolicyDeniedError,
    truncate_text,
    validate_workspace_command,
)
from maestro.domain.artifacts import (
    Artifact,
    ArtifactExecutionReference,
    ArtifactProducer,
    ArtifactType,
    ArtifactWorkItemReference,
)
from maestro.domain.events import (
    EventDraft,
    EventExecutionReference,
    EventPayload,
    EventPublisher,
)
from maestro.domain.exceptions import ResourceNotFoundError
from maestro.domain.resources import (
    ConditionStatus,
    MaestroModel,
    ResourceName,
    ResourceReference,
    utc_now,
)
from maestro.domain.work_items import (
    WorkItem,
    WorkItemPhase,
    WorkItemRepository,
    WorkItemVerificationCommandResult,
    WorkItemVerificationStatus,
)
from maestro.domain.workspaces import (
    Workspace,
    WorkspaceCommandRequest,
    WorkspaceCommandResult,
    WorkspaceHandle,
    WorkspacePhase,
    WorkspaceProvider,
    WorkspaceProviderError,
    WorkspaceRepository,
)

VERIFICATION_CONTROLLER = "verification-controller"
DEFAULT_VERIFICATION_MAX_OUTPUT_BYTES = 64 * 1024
COMMAND_TIMEOUT_EXIT_CODE = 124
COMMAND_DENIED_EXIT_CODE = 126
COMMAND_INVALID_EXIT_CODE = 2


class VerificationStatus(StrEnum):
    """Independent verification outcome."""

    PASSED = "passed"
    FAILED = "failed"
    SKIPPED = "skipped"


class VerificationFailureCategory(StrEnum):
    """Structured failure categories for verification evidence."""

    MISSING_COMMANDS = "missing-commands"
    WORKSPACE_UNAVAILABLE = "workspace-unavailable"
    INVALID_COMMAND = "invalid-command"
    POLICY_DENIED = "policy-denied"
    COMMAND_FAILED = "command-failed"
    COMMAND_TIMED_OUT = "command-timed-out"


class VerificationCommandEvidence(MaestroModel):
    """Observed output for one verification command."""

    command: str = Field(min_length=1)
    argv: tuple[str, ...] = Field(default_factory=tuple)
    exit_code: int = Field(alias="exitCode")
    stdout: str = ""
    stderr: str = ""
    timeout_seconds: int = Field(ge=1, alias="timeoutSeconds")
    duration_seconds: float = Field(ge=0, alias="durationSeconds")
    timed_out: bool = Field(default=False, alias="timedOut")
    truncated: bool = False
    failure_category: VerificationFailureCategory | None = Field(
        default=None,
        alias="failureCategory",
    )
    output_artifact_ref: ResourceReference | None = Field(
        default=None,
        alias="outputArtifactRef",
    )


class VerificationRequest(MaestroModel):
    """Provider-independent request for WorkItem verification."""

    work_item_ref: ResourceReference = Field(alias="workItemRef")
    workspace_ref: ResourceReference | None = Field(default=None, alias="workspaceRef")
    commands: tuple[str, ...]
    command_source: str = Field(
        default="WorkItem.spec.verification.commands",
        alias="commandSource",
    )
    timeout_seconds: int = Field(ge=1, alias="timeoutSeconds")
    max_output_bytes: int = Field(ge=1, alias="maxOutputBytes")


class VerificationReport(MaestroModel):
    """Structured verification report persisted as an Artifact."""

    status: VerificationStatus
    failure_category: VerificationFailureCategory | None = Field(
        default=None,
        alias="failureCategory",
    )
    request: VerificationRequest
    command_results: tuple[VerificationCommandEvidence, ...] = Field(
        default_factory=tuple,
        alias="commandResults",
    )
    all_commands_passed: bool = Field(default=False, alias="allCommandsPassed")
    repair_allowed: bool = Field(default=False, alias="repairAllowed")
    message: str = ""


class VerificationControllerResult(MaestroModel):
    """Result returned by the verification controller."""

    work_item_ref: ResourceReference = Field(alias="workItemRef")
    status: VerificationStatus
    report_artifact_ref: ResourceReference = Field(alias="reportArtifactRef")
    command_artifact_refs: tuple[ResourceReference, ...] = Field(
        default_factory=tuple,
        alias="commandArtifactRefs",
    )
    repair_allowed: bool = Field(default=False, alias="repairAllowed")


class VerificationController:
    """Reconcile Verifying WorkItems from observed command results."""

    name = VERIFICATION_CONTROLLER
    kind = "WorkItem"

    def __init__(
        self,
        *,
        work_item_repository: WorkItemRepository,
        workspace_repository: WorkspaceRepository,
        workspace_provider: WorkspaceProvider,
        artifact_service: ArtifactService,
        event_publisher: EventPublisher | None = None,
        max_output_bytes: int = DEFAULT_VERIFICATION_MAX_OUTPUT_BYTES,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        if max_output_bytes < 1:
            raise ValueError("max_output_bytes must be at least 1")
        self._work_item_repository = work_item_repository
        self._workspace_repository = workspace_repository
        self._workspace_provider = workspace_provider
        self._artifact_service = artifact_service
        self._event_publisher = event_publisher
        self._max_output_bytes = max_output_bytes
        self._clock = clock

    async def reconcile(self, context: ReconciliationContext) -> ReconcileResult:
        """Run verification for a WorkItem that is awaiting verification."""

        work_item = await self._work_item_repository.get(context.resource_id)
        if work_item.status.phase != WorkItemPhase.VERIFYING:
            return ReconcileResult()

        await self.verify_work_item(work_item.metadata.id)
        return ReconcileResult()

    async def verify_work_item(
        self, work_item_id: UUID
    ) -> VerificationControllerResult:
        """Verify one WorkItem using its approved verification commands."""

        work_item = await self._work_item_repository.get(work_item_id)
        if work_item.status.phase != WorkItemPhase.VERIFYING:
            raise ValueError(f"WorkItem is {work_item.status.phase}, not Verifying")

        timeout_seconds = self._verification_timeout(work_item, workspace=None)
        request = build_verification_request(
            work_item,
            workspace=None,
            timeout_seconds=timeout_seconds,
            max_output_bytes=self._max_output_bytes,
        )
        commands = work_item.spec.verification.commands
        if not commands:
            return await self._finish_without_commands(work_item, request)

        try:
            workspace = await self._load_workspace(work_item)
        except (ResourceNotFoundError, ValueError) as error:
            return await self._finish_workspace_unavailable(
                work_item,
                request,
                str(error),
            )
        timeout_seconds = self._verification_timeout(work_item, workspace=workspace)
        request = build_verification_request(
            work_item,
            workspace=workspace,
            timeout_seconds=timeout_seconds,
            max_output_bytes=self._max_output_bytes,
        )

        if not _workspace_is_ready(workspace):
            return await self._finish_workspace_unavailable(
                work_item,
                request,
                "Workspace is not ready for verification",
            )

        command_evidence: list[VerificationCommandEvidence] = []
        for index, command in enumerate(commands, start=1):
            evidence = await self._run_command(
                work_item,
                workspace,
                command,
                index=index,
                timeout_seconds=timeout_seconds,
            )
            command_evidence.append(evidence)

        return await self._finish_with_command_results(
            work_item,
            request,
            tuple(command_evidence),
        )

    async def _run_command(
        self,
        work_item: WorkItem,
        workspace: Workspace,
        command: str,
        *,
        index: int,
        timeout_seconds: int,
    ) -> VerificationCommandEvidence:
        started = self._clock()
        argv: tuple[str, ...] = ()
        try:
            argv = tuple(shlex.split(command))
            if not argv:
                raise ValueError("verification command is empty")
            validate_workspace_command(
                _workspace_root(workspace),
                argv,
                capability="shell.execute.test",
            )
            result = await self._workspace_provider.run_command(
                _workspace_handle(workspace),
                WorkspaceCommandRequest(
                    command=argv,
                    timeoutSeconds=timeout_seconds,
                ),
            )
            evidence = self._evidence_from_command_result(
                command,
                argv,
                result,
                timeout_seconds=timeout_seconds,
                duration_seconds=self._duration(started),
            )
        except ValueError as error:
            evidence = VerificationCommandEvidence(
                command=command,
                argv=argv,
                exitCode=COMMAND_INVALID_EXIT_CODE,
                stderr=str(error),
                timeoutSeconds=timeout_seconds,
                durationSeconds=self._duration(started),
                failureCategory=VerificationFailureCategory.INVALID_COMMAND,
            )
        except ToolPolicyDeniedError as error:
            evidence = VerificationCommandEvidence(
                command=command,
                argv=argv,
                exitCode=COMMAND_DENIED_EXIT_CODE,
                stderr=error.message,
                timeoutSeconds=timeout_seconds,
                durationSeconds=self._duration(started),
                failureCategory=VerificationFailureCategory.POLICY_DENIED,
            )
        except WorkspaceProviderError as error:
            evidence = VerificationCommandEvidence(
                command=command,
                argv=argv,
                exitCode=1,
                stderr=str(error),
                timeoutSeconds=timeout_seconds,
                durationSeconds=self._duration(started),
                failureCategory=VerificationFailureCategory.WORKSPACE_UNAVAILABLE,
            )

        artifact = await self._create_command_artifact(
            work_item,
            index=index,
            evidence=evidence,
        )
        return evidence.model_copy(
            update={"output_artifact_ref": _resource_ref(artifact)}
        )

    def _evidence_from_command_result(
        self,
        command: str,
        argv: tuple[str, ...],
        result: WorkspaceCommandResult,
        *,
        timeout_seconds: int,
        duration_seconds: float,
    ) -> VerificationCommandEvidence:
        stdout, stdout_truncated = truncate_text(result.stdout, self._max_output_bytes)
        stderr, stderr_truncated = truncate_text(result.stderr, self._max_output_bytes)
        timed_out = result.exit_code == COMMAND_TIMEOUT_EXIT_CODE
        failure_category = _failure_category_for_exit_code(result.exit_code)
        return VerificationCommandEvidence(
            command=command,
            argv=argv,
            exitCode=result.exit_code,
            stdout=stdout,
            stderr=stderr,
            timeoutSeconds=timeout_seconds,
            durationSeconds=duration_seconds,
            timedOut=timed_out,
            truncated=stdout_truncated or stderr_truncated,
            failureCategory=failure_category,
        )

    async def _finish_without_commands(
        self,
        work_item: WorkItem,
        request: VerificationRequest,
    ) -> VerificationControllerResult:
        report = VerificationReport(
            status=VerificationStatus.SKIPPED,
            failureCategory=VerificationFailureCategory.MISSING_COMMANDS,
            request=request,
            message=(
                "No verification commands are configured for this WorkItem; "
                "command verification was skipped"
            ),
            repairAllowed=False,
        )
        return await self._finish(work_item, report)

    async def _finish_workspace_unavailable(
        self,
        work_item: WorkItem,
        request: VerificationRequest,
        message: str,
    ) -> VerificationControllerResult:
        report = VerificationReport(
            status=VerificationStatus.FAILED,
            failureCategory=VerificationFailureCategory.WORKSPACE_UNAVAILABLE,
            request=request,
            message=message,
            repairAllowed=_repair_allowed(work_item),
        )
        return await self._finish(work_item, report)

    async def _finish_with_command_results(
        self,
        work_item: WorkItem,
        request: VerificationRequest,
        command_evidence: tuple[VerificationCommandEvidence, ...],
    ) -> VerificationControllerResult:
        failure_category = _first_failure_category(command_evidence)
        passed = failure_category is None
        report = VerificationReport(
            status=VerificationStatus.PASSED if passed else VerificationStatus.FAILED,
            failureCategory=failure_category,
            request=request,
            commandResults=command_evidence,
            allCommandsPassed=passed,
            repairAllowed=not passed and _repair_allowed(work_item),
            message=(
                "All verification commands passed"
                if passed
                else "One or more verification commands failed"
            ),
        )
        return await self._finish(work_item, report)

    async def _finish(
        self,
        work_item: WorkItem,
        report: VerificationReport,
    ) -> VerificationControllerResult:
        command_artifact_refs = tuple(
            evidence.output_artifact_ref
            for evidence in report.command_results
            if evidence.output_artifact_ref is not None
        )
        report_artifact = await self._create_report_artifact(
            work_item,
            report,
            source_refs=(
                *work_item.status.result_artifact_refs,
                *command_artifact_refs,
            ),
        )
        report_ref = _resource_ref(report_artifact)
        await self._mark_work_item_verified(
            work_item,
            report,
            report_ref=report_ref,
            command_artifact_refs=command_artifact_refs,
        )
        await self._publish_event(
            work_item,
            report=report,
            report_ref=report_ref,
        )
        return VerificationControllerResult(
            workItemRef=_resource_ref(work_item),
            status=report.status,
            reportArtifactRef=report_ref,
            commandArtifactRefs=command_artifact_refs,
            repairAllowed=report.repair_allowed,
        )

    async def _mark_work_item_verified(
        self,
        work_item: WorkItem,
        report: VerificationReport,
        *,
        report_ref: ResourceReference,
        command_artifact_refs: tuple[ResourceReference, ...],
    ) -> WorkItem:
        current = await self._work_item_repository.get(work_item.metadata.id)
        command_results = tuple(
            WorkItemVerificationCommandResult(
                command=evidence.command,
                exitCode=evidence.exit_code,
                outputArtifactRef=evidence.output_artifact_ref,
            )
            for evidence in report.command_results
        )
        evidence_refs = _append_refs(command_artifact_refs, (report_ref,))
        phase = (
            WorkItemPhase.SUCCEEDED
            if report.status in {VerificationStatus.PASSED, VerificationStatus.SKIPPED}
            else WorkItemPhase.FAILED
        )
        status = current.status.model_copy(
            update={
                "phase": phase,
                "verification": WorkItemVerificationStatus(
                    commandResults=command_results,
                    evidenceRefs=evidence_refs,
                ),
                "result_artifact_refs": _append_refs(
                    current.status.result_artifact_refs,
                    (report_ref,),
                ),
                "completed_at": utc_now(),
            }
        )
        status = with_condition(
            current,
            observe_generation(current, status),
            condition_type="Verification",
            condition_status=(
                ConditionStatus.TRUE
                if report.status == VerificationStatus.PASSED
                else ConditionStatus.UNKNOWN
                if report.status == VerificationStatus.SKIPPED
                else ConditionStatus.FALSE
            ),
            reason=_condition_reason(report),
            message=report.message,
        )
        return await self._work_item_repository.update_status(
            current.metadata.id,
            status,
            expected_resource_version=current.metadata.resource_version,
        )

    async def _create_command_artifact(
        self,
        work_item: WorkItem,
        *,
        index: int,
        evidence: VerificationCommandEvidence,
    ) -> Artifact:
        artifact = await self._artifact_service.create_bytes_artifact(
            name=_artifact_name("verification-command", work_item, index),
            execution_ref=ArtifactExecutionReference(
                id=work_item.spec.execution_ref.id,
                name=work_item.spec.execution_ref.name,
            ),
            work_item_ref=ArtifactWorkItemReference(
                id=work_item.metadata.id,
                name=work_item.metadata.name,
            ),
            artifact_type=ArtifactType.COMMAND_OUTPUT,
            media_type="application/json",
            content=_json_bytes(evidence.model_dump(mode="json", by_alias=True)),
            producer=ArtifactProducer(subsystem=VERIFICATION_CONTROLLER),
            source_refs=work_item.status.result_artifact_refs,
        )
        return await self._artifact_service.verify_artifact(
            artifact,
            expected_resource_version=artifact.metadata.resource_version,
        )

    async def _create_report_artifact(
        self,
        work_item: WorkItem,
        report: VerificationReport,
        *,
        source_refs: tuple[ResourceReference, ...],
    ) -> Artifact:
        artifact = await self._artifact_service.create_bytes_artifact(
            name=_artifact_name("verification-report", work_item, 0),
            execution_ref=ArtifactExecutionReference(
                id=work_item.spec.execution_ref.id,
                name=work_item.spec.execution_ref.name,
            ),
            work_item_ref=ArtifactWorkItemReference(
                id=work_item.metadata.id,
                name=work_item.metadata.name,
            ),
            artifact_type=ArtifactType.VERIFICATION_REPORT,
            media_type="application/json",
            content=_json_bytes(report.model_dump(mode="json", by_alias=True)),
            producer=ArtifactProducer(subsystem=VERIFICATION_CONTROLLER),
            source_refs=_unique_refs(source_refs),
        )
        return await self._artifact_service.verify_artifact(
            artifact,
            expected_resource_version=artifact.metadata.resource_version,
        )

    async def _load_workspace(self, work_item: WorkItem) -> Workspace:
        if work_item.spec.workspace_ref is None:
            raise ValueError("WorkItem has no Workspace reference")
        return await self._workspace_repository.get(work_item.spec.workspace_ref.id)

    def _verification_timeout(
        self,
        work_item: WorkItem,
        *,
        workspace: Workspace | None,
    ) -> int:
        del work_item
        if workspace is None:
            return 300
        return workspace.spec.policy.command_timeout_seconds

    def _duration(self, started: float) -> float:
        return max(0.0, self._clock() - started)

    async def _publish_event(
        self,
        work_item: WorkItem,
        *,
        report: VerificationReport,
        report_ref: ResourceReference,
    ) -> None:
        if self._event_publisher is None:
            return
        payload = report.model_dump(mode="json", by_alias=True)
        await self._event_publisher.publish(
            EventDraft(
                type=_event_type(report),
                producer=VERIFICATION_CONTROLLER,
                correlationId=(
                    f"verification:{work_item.metadata.id}:{work_item.status.attempt}"
                ),
                executionRef=EventExecutionReference(
                    id=work_item.spec.execution_ref.id,
                    name=work_item.spec.execution_ref.name,
                ),
                subjectRef=report_ref,
                payload=_event_payload(payload),
            )
        )


def build_verification_request(
    work_item: WorkItem,
    workspace: Workspace | None,
    *,
    timeout_seconds: int,
    max_output_bytes: int,
) -> VerificationRequest:
    """Build a durable request record from persisted WorkItem state."""

    return VerificationRequest(
        workItemRef=_resource_ref(work_item),
        workspaceRef=_resource_ref(workspace) if workspace is not None else None,
        commands=work_item.spec.verification.commands,
        timeoutSeconds=timeout_seconds,
        maxOutputBytes=max_output_bytes,
    )


def is_verification_unfinished(work_item: WorkItem) -> bool:
    """Return whether a WorkItem should be requeued after controller restart."""

    return work_item.status.phase == WorkItemPhase.VERIFYING


def _failure_category_for_exit_code(
    exit_code: int,
) -> VerificationFailureCategory | None:
    if exit_code == 0:
        return None
    if exit_code == COMMAND_TIMEOUT_EXIT_CODE:
        return VerificationFailureCategory.COMMAND_TIMED_OUT
    return VerificationFailureCategory.COMMAND_FAILED


def _first_failure_category(
    evidence: tuple[VerificationCommandEvidence, ...],
) -> VerificationFailureCategory | None:
    for item in evidence:
        if item.failure_category is not None:
            return item.failure_category
    return None


def _condition_reason(report: VerificationReport) -> str:
    if report.status == VerificationStatus.PASSED:
        return "VerificationPassed"
    if report.status == VerificationStatus.SKIPPED:
        return "VerificationSkipped"
    if report.failure_category == VerificationFailureCategory.MISSING_COMMANDS:
        return "VerificationCommandsMissing"
    if report.failure_category == VerificationFailureCategory.COMMAND_TIMED_OUT:
        return "VerificationTimedOut"
    if report.failure_category == VerificationFailureCategory.POLICY_DENIED:
        return "VerificationPolicyDenied"
    if report.failure_category == VerificationFailureCategory.WORKSPACE_UNAVAILABLE:
        return "VerificationWorkspaceUnavailable"
    if report.failure_category == VerificationFailureCategory.INVALID_COMMAND:
        return "VerificationCommandInvalid"
    return "VerificationFailed"


def _event_type(report: VerificationReport) -> str:
    if report.status == VerificationStatus.PASSED:
        return "VerificationCompleted"
    if report.status == VerificationStatus.SKIPPED:
        return "VerificationSkipped"
    return "VerificationFailed"


def _repair_allowed(work_item: WorkItem) -> bool:
    return work_item.status.attempt < work_item.spec.retry_policy.max_attempts


def _workspace_is_ready(workspace: Workspace) -> bool:
    return (
        workspace.status.phase
        in {WorkspacePhase.READY, WorkspacePhase.IN_USE, WorkspacePhase.DIRTY}
        and workspace.status.path is not None
    )


def _workspace_root(workspace: Workspace) -> Path:
    if workspace.status.path is None:
        raise WorkspaceProviderError("Workspace has no prepared path")
    if workspace.status.path.is_symlink():
        raise WorkspaceProviderError("Workspace path must not be a symlink")
    try:
        return workspace.status.path.resolve(strict=True)
    except OSError as error:
        raise WorkspaceProviderError(str(error)) from error


def _workspace_handle(workspace: Workspace) -> WorkspaceHandle:
    if workspace.status.path is None:
        raise WorkspaceProviderError("Workspace has no prepared path")
    return WorkspaceHandle(
        path=workspace.status.path,
        observedRevision=workspace.status.observed_revision or "unknown",
    )


def _artifact_name(
    prefix: str,
    work_item: WorkItem,
    index: int,
) -> ResourceName:
    return (
        f"{prefix}-"
        f"{work_item.metadata.id.hex[:10]}-"
        f"{work_item.status.attempt}-"
        f"{index}-"
        f"{uuid4().hex[:8]}"
    )


def _append_refs(
    existing: tuple[ResourceReference, ...],
    additions: tuple[ResourceReference, ...],
) -> tuple[ResourceReference, ...]:
    by_key = {(ref.kind, ref.id): ref for ref in existing}
    for ref in additions:
        by_key[(ref.kind, ref.id)] = ref
    return tuple(by_key.values())


def _unique_refs(refs: tuple[ResourceReference, ...]) -> tuple[ResourceReference, ...]:
    return _append_refs((), refs)


def _resource_ref(resource: WorkItem | Workspace | Artifact) -> ResourceReference:
    return ResourceReference(
        kind=resource.kind,
        id=resource.metadata.id,
        name=resource.metadata.name,
    )


def _event_payload(payload: dict[str, Any]) -> EventPayload:
    return cast(EventPayload, json.loads(json.dumps(payload)))


def _json_bytes(value: Any) -> bytes:
    return json.dumps(value, indent=2, sort_keys=True).encode("utf-8")
