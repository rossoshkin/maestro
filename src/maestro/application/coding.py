"""Coding Role runtime."""

from __future__ import annotations

import json
from collections.abc import Callable
from enum import StrEnum
from time import monotonic
from typing import Any
from uuid import UUID

from pydantic import Field, ValidationError

from maestro.application.artifacts import ArtifactService
from maestro.application.controllers import observe_generation, with_condition
from maestro.application.tools import CodingToolRuntime
from maestro.domain.agents import Agent
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
    EventPublisher,
)
from maestro.domain.providers import (
    ModelProvider,
    Provider,
    ProviderMessage,
    ProviderMessageRole,
    ProviderOperationError,
    ToolLoopRequest,
    ToolLoopResult,
)
from maestro.domain.resources import (
    ConditionStatus,
    MaestroModel,
    ResourceName,
    ResourceReference,
    utc_now,
)
from maestro.domain.role_invocations import (
    RoleInvocation,
    RoleInvocationAgentReference,
    RoleInvocationExecutionReference,
    RoleInvocationFailure,
    RoleInvocationLimits,
    RoleInvocationPhase,
    RoleInvocationProviderReference,
    RoleInvocationRepository,
    RoleInvocationRoleReference,
    RoleInvocationSpec,
    RoleInvocationWorkItemReference,
)
from maestro.domain.work_items import (
    WorkItem,
    WorkItemPhase,
    WorkItemRepository,
)
from maestro.domain.workspaces import (
    Workspace,
    WorkspaceCommandRequest,
    WorkspaceHandle,
    WorkspaceProvider,
)

CODING_RUNTIME = "coding-runtime"
CODING_PROMPT_TEMPLATE = """You are Maestro's Coding Role.

Work only inside the assigned Workspace using the provided tools.
Use exactly the granted Capabilities and do not request prohibited operations.
Inspect relevant files before editing. Keep changes focused on the Work Item.
When no more tools are needed, return only a compact JSON object matching the
supplied Coding output schema.
Do not claim tests passed unless you observed command output through tools.
Do not mark the Work Item verified; Maestro verifies independently.
"""
DEFAULT_CODING_MAX_STEPS = 20
DEFAULT_CODING_MAX_DURATION_SECONDS = 900
DEFAULT_CODING_MAX_OUTPUT_BYTES = 64 * 1024
CODING_DENIED_CAPABILITIES = (
    "filesystem.read.outside-workspace",
    "git.push",
    "git.merge",
    "deployment.execute",
    "secrets.read",
    "sudo",
    "network.unrestricted",
    "workflow.transition",
    "approval.decide",
)


class CodingOutputStatus(StrEnum):
    """Model-reported Coding Role completion status."""

    COMPLETED = "completed"
    BLOCKED = "blocked"
    FAILED = "failed"


class CodingChangeType(StrEnum):
    """File change types reported or observed for Coding output."""

    ADDED = "added"
    MODIFIED = "modified"
    DELETED = "deleted"


class CodingChangedFile(MaestroModel):
    """Changed file entry in Coding output."""

    path: str = Field(min_length=1)
    change_type: CodingChangeType = Field(alias="changeType")


class CodingCommandRequest(MaestroModel):
    """Command the model says it requested."""

    command: str = Field(min_length=1)
    purpose: str = ""


class LiteralRuntimeStatus(StrEnum):
    """Runtime failure statuses that are not model-authored Coding output."""

    INVALID_OUTPUT = "invalid-output"
    STEP_LIMIT_EXCEEDED = "step-limit-exceeded"
    DURATION_LIMIT_EXCEEDED = "duration-limit-exceeded"
    PROVIDER_FAILED = "provider-failed"


class CodingOutput(MaestroModel):
    """Structured Coding Role output."""

    status: CodingOutputStatus
    summary: str = Field(min_length=1)
    changed_files: tuple[CodingChangedFile, ...] = Field(
        default_factory=tuple,
        alias="changedFiles",
    )
    commands_requested: tuple[CodingCommandRequest, ...] = Field(
        default_factory=tuple,
        alias="commandsRequested",
    )
    remaining_issues: tuple[str, ...] = Field(
        default_factory=tuple,
        alias="remainingIssues",
    )
    questions: tuple[str, ...] = Field(default_factory=tuple)


class CodingInvocationResult(MaestroModel):
    """Result of one Coding Role invocation."""

    invocation_ref: ResourceReference = Field(alias="invocationRef")
    status: CodingOutputStatus | LiteralRuntimeStatus
    output: CodingOutput | None = None
    summary_artifact_ref: ResourceReference | None = Field(
        default=None,
        alias="summaryArtifactRef",
    )
    diff_artifact_ref: ResourceReference | None = Field(
        default=None,
        alias="diffArtifactRef",
    )
    observed_changed_files: tuple[CodingChangedFile, ...] = Field(
        default_factory=tuple,
        alias="observedChangedFiles",
    )
    tool_artifact_refs: tuple[ResourceReference, ...] = Field(
        default_factory=tuple,
        alias="toolArtifactRefs",
    )


class CodingToolCall(MaestroModel):
    """Tool call requested by the model provider."""

    name: ResourceName
    arguments: dict[str, Any] = Field(default_factory=dict)


class CodingRuntime:
    """Run the Coding Role through a Provider and the safe tool runtime."""

    def __init__(
        self,
        *,
        work_item_repository: WorkItemRepository,
        role_invocation_repository: RoleInvocationRepository,
        artifact_service: ArtifactService,
        tool_runtime: CodingToolRuntime,
        event_publisher: EventPublisher | None = None,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        self._work_item_repository = work_item_repository
        self._role_invocation_repository = role_invocation_repository
        self._artifact_service = artifact_service
        self._tool_runtime = tool_runtime
        self._event_publisher = event_publisher
        self._clock = clock

    async def invoke_coding(
        self,
        work_item_id: UUID,
        *,
        workspace: Workspace,
        workspace_provider: WorkspaceProvider,
        agent: Agent,
        provider: Provider,
        runtime: ModelProvider,
        granted_capabilities: tuple[CapabilityName, ...],
        max_steps: int = DEFAULT_CODING_MAX_STEPS,
        max_duration_seconds: int = DEFAULT_CODING_MAX_DURATION_SECONDS,
        max_command_output_bytes: int = DEFAULT_CODING_MAX_OUTPUT_BYTES,
        context: dict[str, Any] | None = None,
    ) -> CodingInvocationResult:
        """Invoke the Coding Role and persist all prompts, tool calls and output."""

        if max_steps < 1:
            raise ValueError("max_steps must be at least 1")
        if max_duration_seconds < 1:
            raise ValueError("max_duration_seconds must be at least 1")

        work_item = await self._work_item_repository.get(work_item_id)
        running_work_item = await self._mark_work_item_running(work_item)
        invocation = await self._create_invocation(
            running_work_item,
            agent,
            granted_capabilities=granted_capabilities,
            max_steps=max_steps,
            max_duration_seconds=max_duration_seconds,
        )
        running_invocation = await self._mark_invocation_running(
            invocation,
            provider=provider,
            model=agent.spec.model,
        )
        running_work_item = await self._record_work_item_invocation(
            running_work_item,
            running_invocation,
        )

        prompt = _coding_prompt(
            running_work_item,
            workspace,
            granted_capabilities=granted_capabilities,
            denied_capabilities=CODING_DENIED_CAPABILITIES,
            max_steps=max_steps,
            max_duration_seconds=max_duration_seconds,
            max_command_output_bytes=max_command_output_bytes,
            context=context or {},
        )
        prompt_artifact = await self._create_artifact(
            invocation=running_invocation,
            work_item=running_work_item,
            name=f"coding-prompt-{running_invocation.metadata.id.hex[:12]}",
            artifact_type=ArtifactType.PROMPT,
            media_type="text/markdown",
            content=prompt.encode("utf-8"),
        )
        messages: list[ProviderMessage] = [
            ProviderMessage(
                role=ProviderMessageRole.SYSTEM,
                content=CODING_PROMPT_TEMPLATE,
            ),
            ProviderMessage(role=ProviderMessageRole.USER, content=prompt),
        ]
        response_artifacts: list[Artifact] = []
        tool_artifact_refs: list[ResourceReference] = []
        tool_call_count = 0
        started = self._clock()

        while True:
            if self._elapsed(started) > max_duration_seconds:
                return await self._finish_timed_out(
                    running_work_item,
                    running_invocation,
                    provider=provider,
                    model=agent.spec.model,
                    prompt_artifact=prompt_artifact,
                    response_artifact=_last_or_none(response_artifacts),
                    tool_artifact_refs=tuple(tool_artifact_refs),
                    tool_call_count=tool_call_count,
                    message="Coding invocation exceeded maxDurationSeconds",
                )

            try:
                loop_result = await runtime.run_tool_loop(
                    ToolLoopRequest(
                        model=agent.spec.model,
                        messages=tuple(messages),
                        tools=self._tool_runtime.registry.provider_tool_definitions(
                            granted_capabilities=granted_capabilities,
                        ),
                        maxToolCalls=max(0, max_steps - tool_call_count),
                        timeoutSeconds=min(
                            provider.spec.timeout_seconds,
                            max(1, max_duration_seconds - int(self._elapsed(started))),
                        ),
                    )
                )
            except ProviderOperationError as error:
                return await self._finish_failed(
                    running_work_item,
                    running_invocation,
                    provider=provider,
                    model=agent.spec.model,
                    prompt_artifact=prompt_artifact,
                    response_artifact=_last_or_none(response_artifacts),
                    tool_artifact_refs=tuple(tool_artifact_refs),
                    reason="CodingProviderFailed",
                    message=str(error),
                    status=LiteralRuntimeStatus.PROVIDER_FAILED,
                )

            response_artifact = await self._persist_response_artifact(
                running_invocation,
                running_work_item,
                loop_result,
                len(response_artifacts) + 1,
                prompt_artifact=prompt_artifact,
            )
            response_artifacts.append(response_artifact)

            if self._elapsed(started) > max_duration_seconds:
                return await self._finish_timed_out(
                    running_work_item,
                    running_invocation,
                    provider=provider,
                    model=agent.spec.model,
                    prompt_artifact=prompt_artifact,
                    response_artifact=response_artifact,
                    tool_artifact_refs=tuple(tool_artifact_refs),
                    tool_call_count=tool_call_count,
                    message="Coding invocation exceeded maxDurationSeconds",
                )

            try:
                tool_calls = _tool_calls_from_output(loop_result.output)
            except (ValidationError, ValueError) as error:
                return await self._finish_failed(
                    running_work_item,
                    running_invocation,
                    provider=provider,
                    model=agent.spec.model,
                    prompt_artifact=prompt_artifact,
                    response_artifact=response_artifact,
                    tool_artifact_refs=tuple(tool_artifact_refs),
                    reason="CodingToolCallsInvalid",
                    message=str(error),
                    status=LiteralRuntimeStatus.INVALID_OUTPUT,
                )
            if tool_calls:
                if tool_call_count + len(tool_calls) > max_steps:
                    return await self._finish_failed(
                        running_work_item,
                        running_invocation,
                        provider=provider,
                        model=agent.spec.model,
                        prompt_artifact=prompt_artifact,
                        response_artifact=response_artifact,
                        tool_artifact_refs=tuple(tool_artifact_refs),
                        reason="CodingStepLimitExceeded",
                        message="Coding Agent requested more tool calls than allowed",
                        status=LiteralRuntimeStatus.STEP_LIMIT_EXCEEDED,
                    )
                messages.append(
                    ProviderMessage(
                        role=ProviderMessageRole.ASSISTANT,
                        content=_json_text(
                            {
                                "toolCalls": tuple(
                                    call.model_dump(mode="json", by_alias=True)
                                    for call in tool_calls
                                )
                            }
                        ),
                    )
                )
                for tool_call in tool_calls:
                    tool_result = await self._tool_runtime.execute_tool(
                        tool_call.name,
                        tool_call.arguments,
                        workspace=workspace,
                        work_item=running_work_item,
                        granted_capabilities=granted_capabilities,
                        workspace_provider=workspace_provider,
                        role_invocation=running_invocation,
                    )
                    tool_call_count += 1
                    tool_artifact_refs.append(tool_result.artifact_ref)
                    messages.append(
                        ProviderMessage(
                            role=ProviderMessageRole.TOOL,
                            content=tool_result.model_dump_json(by_alias=True),
                        )
                    )
                continue

            try:
                coding_output = CodingOutput.model_validate(
                    _coding_output_candidate(loop_result.output)
                )
            except ValidationError as error:
                return await self._finish_failed(
                    running_work_item,
                    running_invocation,
                    provider=provider,
                    model=agent.spec.model,
                    prompt_artifact=prompt_artifact,
                    response_artifact=response_artifact,
                    tool_artifact_refs=tuple(tool_artifact_refs),
                    reason="CodingOutputInvalid",
                    message=str(error),
                    status=LiteralRuntimeStatus.INVALID_OUTPUT,
                )

            observed_changes = await _collect_changed_files(
                workspace,
                workspace_provider,
            )
            diff_artifact = await self._create_diff_artifact(
                running_invocation,
                running_work_item,
                workspace,
                workspace_provider,
                source_refs=(_resource_ref(response_artifact),),
            )
            summary_artifact = await self._create_summary_artifact(
                running_invocation,
                running_work_item,
                coding_output,
                observed_changes,
                tool_artifact_refs=tuple(tool_artifact_refs),
                diff_artifact=diff_artifact,
                source_refs=(_resource_ref(response_artifact),),
            )
            output_artifacts = (
                *tool_artifact_refs,
                _resource_ref(summary_artifact),
                _resource_ref(diff_artifact),
            )

            if coding_output.status == CodingOutputStatus.COMPLETED:
                updated_invocation = await self._mark_invocation_succeeded(
                    running_invocation,
                    provider=provider,
                    model=agent.spec.model,
                    prompt_artifact=prompt_artifact,
                    response_artifact=response_artifact,
                    output_artifact_refs=output_artifacts,
                    tool_call_count=tool_call_count,
                )
                await self._mark_work_item_verifying(
                    running_work_item,
                    output_artifact_refs=(
                        _resource_ref(summary_artifact),
                        _resource_ref(diff_artifact),
                    ),
                    reason="ImplementationProduced",
                    message=coding_output.summary,
                )
                await self._publish_event(
                    "CodingImplementationProduced",
                    running_work_item,
                    _resource_ref(summary_artifact),
                    {
                        "status": coding_output.status,
                        "observedChangedFiles": tuple(
                            change.model_dump(mode="json", by_alias=True)
                            for change in observed_changes
                        ),
                    },
                )
                return CodingInvocationResult(
                    invocationRef=_resource_ref(updated_invocation),
                    status=coding_output.status,
                    output=coding_output,
                    summaryArtifactRef=_resource_ref(summary_artifact),
                    diffArtifactRef=_resource_ref(diff_artifact),
                    observedChangedFiles=observed_changes,
                    toolArtifactRefs=tuple(tool_artifact_refs),
                )

            reason = (
                "CodingBlocked"
                if coding_output.status == CodingOutputStatus.BLOCKED
                else "CodingFailed"
            )
            updated_invocation = await self._mark_invocation_failed(
                running_invocation,
                provider=provider,
                model=agent.spec.model,
                prompt_artifact=prompt_artifact,
                response_artifact=response_artifact,
                output_artifact_refs=output_artifacts,
                tool_call_count=tool_call_count,
                reason=reason,
                message=coding_output.summary,
            )
            await self._mark_work_item_failed(
                running_work_item,
                reason=(
                    "BlockedByMissingContext"
                    if coding_output.status == CodingOutputStatus.BLOCKED
                    else "UnableToComplete"
                ),
                message=coding_output.summary,
                output_artifact_refs=(
                    _resource_ref(summary_artifact),
                    _resource_ref(diff_artifact),
                ),
            )
            return CodingInvocationResult(
                invocationRef=_resource_ref(updated_invocation),
                status=coding_output.status,
                output=coding_output,
                summaryArtifactRef=_resource_ref(summary_artifact),
                diffArtifactRef=_resource_ref(diff_artifact),
                observedChangedFiles=observed_changes,
                toolArtifactRefs=tuple(tool_artifact_refs),
            )

    def _elapsed(self, started: float) -> int:
        return int(self._clock() - started)

    async def _create_invocation(
        self,
        work_item: WorkItem,
        agent: Agent,
        *,
        granted_capabilities: tuple[CapabilityName, ...],
        max_steps: int,
        max_duration_seconds: int,
    ) -> RoleInvocation:
        invocation = RoleInvocation.new(
            name=f"coding-{work_item.metadata.id.hex[:12]}-a{work_item.status.attempt}",
            namespace=work_item.metadata.namespace,
            spec=RoleInvocationSpec(
                executionRef=RoleInvocationExecutionReference(
                    id=work_item.spec.execution_ref.id,
                    name=work_item.spec.execution_ref.name,
                ),
                workItemRef=RoleInvocationWorkItemReference(
                    id=work_item.metadata.id,
                    name=work_item.metadata.name,
                ),
                roleRef=RoleInvocationRoleReference(
                    name=work_item.spec.role_ref.name,
                    version=work_item.spec.role_ref.version,
                ),
                agentRef=RoleInvocationAgentReference(
                    id=agent.metadata.id,
                    name=agent.metadata.name,
                ),
                grantedCapabilities=granted_capabilities,
                limits=RoleInvocationLimits(
                    maxSteps=max_steps,
                    maxDurationSeconds=max_duration_seconds,
                ),
            ),
        )
        return await self._role_invocation_repository.create(invocation)

    async def _mark_invocation_running(
        self,
        invocation: RoleInvocation,
        *,
        provider: Provider,
        model: str,
    ) -> RoleInvocation:
        status = invocation.status.model_copy(
            update={
                "observed_generation": invocation.metadata.generation,
                "phase": RoleInvocationPhase.RUNNING,
                "provider_ref": _provider_ref(provider),
                "model": model,
                "started_at": utc_now(),
            }
        )
        return await self._role_invocation_repository.update_status(
            invocation.metadata.id,
            status,
            expected_resource_version=invocation.metadata.resource_version,
        )

    async def _mark_invocation_succeeded(
        self,
        invocation: RoleInvocation,
        *,
        provider: Provider,
        model: str,
        prompt_artifact: Artifact,
        response_artifact: Artifact,
        output_artifact_refs: tuple[ResourceReference, ...],
        tool_call_count: int,
    ) -> RoleInvocation:
        current = await self._role_invocation_repository.get(invocation.metadata.id)
        status = current.status.model_copy(
            update={
                "observed_generation": current.metadata.generation,
                "phase": RoleInvocationPhase.SUCCEEDED,
                "provider_ref": _provider_ref(provider),
                "model": model,
                "prompt_artifact_ref": _resource_ref(prompt_artifact),
                "response_artifact_ref": _resource_ref(response_artifact),
                "output_artifact_refs": output_artifact_refs,
                "tool_call_count": tool_call_count,
                "completed_at": utc_now(),
            }
        )
        return await self._role_invocation_repository.update_status(
            current.metadata.id,
            status,
            expected_resource_version=current.metadata.resource_version,
        )

    async def _mark_invocation_failed(
        self,
        invocation: RoleInvocation,
        *,
        provider: Provider,
        model: str,
        prompt_artifact: Artifact,
        response_artifact: Artifact | None,
        output_artifact_refs: tuple[ResourceReference, ...],
        tool_call_count: int,
        reason: str,
        message: str,
    ) -> RoleInvocation:
        current = await self._role_invocation_repository.get(invocation.metadata.id)
        status = current.status.model_copy(
            update={
                "observed_generation": current.metadata.generation,
                "phase": RoleInvocationPhase.FAILED,
                "provider_ref": _provider_ref(provider),
                "model": model,
                "prompt_artifact_ref": _resource_ref(prompt_artifact),
                "response_artifact_ref": (
                    _resource_ref(response_artifact)
                    if response_artifact is not None
                    else None
                ),
                "output_artifact_refs": output_artifact_refs,
                "tool_call_count": tool_call_count,
                "completed_at": utc_now(),
                "failure": RoleInvocationFailure(reason=reason, message=message),
            }
        )
        return await self._role_invocation_repository.update_status(
            current.metadata.id,
            status,
            expected_resource_version=current.metadata.resource_version,
        )

    async def _mark_invocation_timed_out(
        self,
        invocation: RoleInvocation,
        *,
        provider: Provider,
        model: str,
        prompt_artifact: Artifact,
        response_artifact: Artifact | None,
        output_artifact_refs: tuple[ResourceReference, ...],
        tool_call_count: int,
        message: str,
    ) -> RoleInvocation:
        current = await self._role_invocation_repository.get(invocation.metadata.id)
        status = current.status.model_copy(
            update={
                "observed_generation": current.metadata.generation,
                "phase": RoleInvocationPhase.TIMED_OUT,
                "provider_ref": _provider_ref(provider),
                "model": model,
                "prompt_artifact_ref": _resource_ref(prompt_artifact),
                "response_artifact_ref": (
                    _resource_ref(response_artifact)
                    if response_artifact is not None
                    else None
                ),
                "output_artifact_refs": output_artifact_refs,
                "tool_call_count": tool_call_count,
                "completed_at": utc_now(),
                "failure": RoleInvocationFailure(
                    reason="CodingDurationLimitExceeded",
                    message=message,
                ),
            }
        )
        return await self._role_invocation_repository.update_status(
            current.metadata.id,
            status,
            expected_resource_version=current.metadata.resource_version,
        )

    async def _mark_work_item_running(self, work_item: WorkItem) -> WorkItem:
        if work_item.status.phase == WorkItemPhase.RUNNING:
            return work_item
        if work_item.status.phase != WorkItemPhase.SCHEDULED:
            raise ValueError(f"WorkItem is {work_item.status.phase}, not Scheduled")
        status = work_item.status.model_copy(
            update={
                "phase": WorkItemPhase.RUNNING,
                "attempt": work_item.status.attempt + 1,
                "started_at": work_item.status.started_at or utc_now(),
            }
        )
        status = with_condition(
            work_item,
            observe_generation(work_item, status),
            condition_type="Coding",
            condition_status=ConditionStatus.UNKNOWN,
            reason="CodingStarted",
            message="Coding Role invocation started",
        )
        return await self._work_item_repository.update_status(
            work_item.metadata.id,
            status,
            expected_resource_version=work_item.metadata.resource_version,
        )

    async def _record_work_item_invocation(
        self,
        work_item: WorkItem,
        invocation: RoleInvocation,
    ) -> WorkItem:
        current = await self._work_item_repository.get(work_item.metadata.id)
        status = current.status.model_copy(
            update={
                "invocation_refs": _append_refs(
                    current.status.invocation_refs,
                    (_resource_ref(invocation),),
                )
            }
        )
        return await self._work_item_repository.update_status(
            current.metadata.id,
            observe_generation(current, status),
            expected_resource_version=current.metadata.resource_version,
        )

    async def _mark_work_item_verifying(
        self,
        work_item: WorkItem,
        *,
        output_artifact_refs: tuple[ResourceReference, ...],
        reason: str,
        message: str,
    ) -> WorkItem:
        current = await self._work_item_repository.get(work_item.metadata.id)
        status = current.status.model_copy(
            update={
                "phase": WorkItemPhase.VERIFYING,
                "result_artifact_refs": _append_refs(
                    current.status.result_artifact_refs,
                    output_artifact_refs,
                ),
            }
        )
        status = with_condition(
            current,
            observe_generation(current, status),
            condition_type="Coding",
            condition_status=ConditionStatus.TRUE,
            reason=reason,
            message=message,
        )
        return await self._work_item_repository.update_status(
            current.metadata.id,
            status,
            expected_resource_version=current.metadata.resource_version,
        )

    async def _mark_work_item_failed(
        self,
        work_item: WorkItem,
        *,
        reason: str,
        message: str,
        output_artifact_refs: tuple[ResourceReference, ...],
    ) -> WorkItem:
        current = await self._work_item_repository.get(work_item.metadata.id)
        status = current.status.model_copy(
            update={
                "phase": WorkItemPhase.FAILED,
                "result_artifact_refs": _append_refs(
                    current.status.result_artifact_refs,
                    output_artifact_refs,
                ),
                "completed_at": utc_now(),
            }
        )
        status = with_condition(
            current,
            observe_generation(current, status),
            condition_type="Coding",
            condition_status=ConditionStatus.FALSE,
            reason=reason,
            message=message,
        )
        return await self._work_item_repository.update_status(
            current.metadata.id,
            status,
            expected_resource_version=current.metadata.resource_version,
        )

    async def _persist_response_artifact(
        self,
        invocation: RoleInvocation,
        work_item: WorkItem,
        loop_result: ToolLoopResult,
        index: int,
        *,
        prompt_artifact: Artifact,
    ) -> Artifact:
        return await self._create_artifact(
            invocation=invocation,
            work_item=work_item,
            name=f"coding-response-{invocation.metadata.id.hex[:12]}-{index}",
            artifact_type=ArtifactType.MODEL_RESPONSE,
            media_type="application/json",
            content=_json_bytes(
                {
                    "model": loop_result.model,
                    "output": loop_result.output,
                    "toolCallCount": loop_result.tool_call_count,
                    "tokenUsage": loop_result.token_usage.model_dump(
                        mode="json",
                        by_alias=True,
                    ),
                }
            ),
            source_refs=(_resource_ref(prompt_artifact),),
        )

    async def _create_summary_artifact(
        self,
        invocation: RoleInvocation,
        work_item: WorkItem,
        output: CodingOutput,
        observed_changed_files: tuple[CodingChangedFile, ...],
        *,
        tool_artifact_refs: tuple[ResourceReference, ...],
        diff_artifact: Artifact,
        source_refs: tuple[ResourceReference, ...],
    ) -> Artifact:
        return await self._create_artifact(
            invocation=invocation,
            work_item=work_item,
            name=f"coding-summary-{invocation.metadata.id.hex[:12]}",
            artifact_type=ArtifactType.SUMMARY,
            media_type="application/json",
            content=_json_bytes(
                {
                    "modelOutput": output.model_dump(mode="json", by_alias=True),
                    "observedChangedFiles": tuple(
                        change.model_dump(mode="json", by_alias=True)
                        for change in observed_changed_files
                    ),
                    "toolArtifactRefs": tuple(
                        ref.model_dump(mode="json", by_alias=True)
                        for ref in tool_artifact_refs
                    ),
                    "diffArtifactRef": _resource_ref(diff_artifact).model_dump(
                        mode="json",
                        by_alias=True,
                    ),
                    "verificationTrusted": False,
                }
            ),
            source_refs=(
                *source_refs,
                *tool_artifact_refs,
                _resource_ref(diff_artifact),
            ),
        )

    async def _create_diff_artifact(
        self,
        invocation: RoleInvocation,
        work_item: WorkItem,
        workspace: Workspace,
        workspace_provider: WorkspaceProvider,
        *,
        source_refs: tuple[ResourceReference, ...],
    ) -> Artifact:
        diff = await workspace_provider.collect_diff(_workspace_handle(workspace))
        return await self._create_artifact(
            invocation=invocation,
            work_item=work_item,
            name=f"coding-diff-{invocation.metadata.id.hex[:12]}",
            artifact_type=ArtifactType.GIT_DIFF,
            media_type="text/x-diff",
            content=diff.text.encode("utf-8"),
            source_refs=source_refs,
        )

    async def _create_artifact(
        self,
        *,
        invocation: RoleInvocation,
        work_item: WorkItem,
        name: ResourceName,
        artifact_type: ArtifactType,
        media_type: str,
        content: bytes,
        source_refs: tuple[ResourceReference, ...] = (),
    ) -> Artifact:
        artifact = await self._artifact_service.create_bytes_artifact(
            name=name,
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
                subsystem=CODING_RUNTIME,
                roleInvocationRef=ArtifactRoleInvocationReference(
                    id=invocation.metadata.id,
                    name=invocation.metadata.name,
                ),
            ),
            source_refs=source_refs,
        )
        return await self._artifact_service.verify_artifact(
            artifact,
            expected_resource_version=artifact.metadata.resource_version,
        )

    async def _finish_failed(
        self,
        work_item: WorkItem,
        invocation: RoleInvocation,
        *,
        provider: Provider,
        model: str,
        prompt_artifact: Artifact,
        response_artifact: Artifact | None,
        tool_artifact_refs: tuple[ResourceReference, ...],
        reason: str,
        message: str,
        status: LiteralRuntimeStatus,
    ) -> CodingInvocationResult:
        updated_invocation = await self._mark_invocation_failed(
            invocation,
            provider=provider,
            model=model,
            prompt_artifact=prompt_artifact,
            response_artifact=response_artifact,
            output_artifact_refs=tool_artifact_refs,
            tool_call_count=len(tool_artifact_refs),
            reason=reason,
            message=message,
        )
        await self._mark_work_item_failed(
            work_item,
            reason=reason,
            message=message,
            output_artifact_refs=tool_artifact_refs,
        )
        await self._publish_event(
            "CodingInvocationFailed",
            work_item,
            _resource_ref(updated_invocation),
            {"status": status, "reason": reason, "message": message},
        )
        return CodingInvocationResult(
            invocationRef=_resource_ref(updated_invocation),
            status=status,
            toolArtifactRefs=tool_artifact_refs,
        )

    async def _finish_timed_out(
        self,
        work_item: WorkItem,
        invocation: RoleInvocation,
        *,
        provider: Provider,
        model: str,
        prompt_artifact: Artifact,
        response_artifact: Artifact | None,
        tool_artifact_refs: tuple[ResourceReference, ...],
        tool_call_count: int,
        message: str,
    ) -> CodingInvocationResult:
        updated_invocation = await self._mark_invocation_timed_out(
            invocation,
            provider=provider,
            model=model,
            prompt_artifact=prompt_artifact,
            response_artifact=response_artifact,
            output_artifact_refs=tool_artifact_refs,
            tool_call_count=tool_call_count,
            message=message,
        )
        await self._mark_work_item_failed(
            work_item,
            reason="CodingDurationLimitExceeded",
            message=message,
            output_artifact_refs=tool_artifact_refs,
        )
        await self._publish_event(
            "CodingInvocationTimedOut",
            work_item,
            _resource_ref(updated_invocation),
            {
                "status": LiteralRuntimeStatus.DURATION_LIMIT_EXCEEDED,
                "message": message,
            },
        )
        return CodingInvocationResult(
            invocationRef=_resource_ref(updated_invocation),
            status=LiteralRuntimeStatus.DURATION_LIMIT_EXCEEDED,
            toolArtifactRefs=tool_artifact_refs,
        )

    async def _publish_event(
        self,
        event_type: str,
        work_item: WorkItem,
        subject: ResourceReference,
        payload: dict[str, Any],
    ) -> None:
        if self._event_publisher is None:
            return
        await self._event_publisher.publish(
            EventDraft(
                type=event_type,
                producer=CODING_RUNTIME,
                correlationId=f"coding:{work_item.metadata.id}:{event_type}",
                executionRef=EventExecutionReference(
                    id=work_item.spec.execution_ref.id,
                    name=work_item.spec.execution_ref.name,
                ),
                subjectRef=subject,
                payload=payload,
            )
        )


def build_coding_input(
    work_item: WorkItem,
    workspace: Workspace,
    *,
    granted_capabilities: tuple[CapabilityName, ...],
    denied_capabilities: tuple[str, ...] = CODING_DENIED_CAPABILITIES,
    max_steps: int = DEFAULT_CODING_MAX_STEPS,
    max_duration_seconds: int = DEFAULT_CODING_MAX_DURATION_SECONDS,
    max_command_output_bytes: int = DEFAULT_CODING_MAX_OUTPUT_BYTES,
    context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build provider-independent Coding input."""

    return {
        "executionRef": work_item.spec.execution_ref.model_dump(
            mode="json",
            by_alias=True,
        ),
        "workItem": {
            "id": str(work_item.metadata.id),
            "name": work_item.metadata.name,
            "objective": work_item.spec.objective,
            "constraints": work_item.spec.constraints,
            "acceptanceCriteria": work_item.spec.acceptance_criteria,
            "verification": work_item.spec.verification.model_dump(
                mode="json",
                by_alias=True,
            ),
            "contextRefs": tuple(
                ref.model_dump(mode="json", by_alias=True)
                for ref in work_item.spec.context_refs
            ),
        },
        "workspace": {
            "id": str(workspace.metadata.id),
            "name": workspace.metadata.name,
            "root": str(workspace.status.path) if workspace.status.path else "",
            "repositoryRef": workspace.spec.repository_ref,
            "baseRevision": workspace.spec.base_revision,
        },
        "context": context or {},
        "capabilities": {
            "granted": granted_capabilities,
            "denied": denied_capabilities,
        },
        "limits": {
            "maxSteps": max_steps,
            "maxDurationSeconds": max_duration_seconds,
            "maxCommandOutputBytes": max_command_output_bytes,
        },
    }


def _coding_prompt(
    work_item: WorkItem,
    workspace: Workspace,
    *,
    granted_capabilities: tuple[CapabilityName, ...],
    denied_capabilities: tuple[str, ...],
    max_steps: int,
    max_duration_seconds: int,
    max_command_output_bytes: int,
    context: dict[str, Any],
) -> str:
    prompt = {
        "instructions": CODING_PROMPT_TEMPLATE,
        "input": build_coding_input(
            work_item,
            workspace,
            granted_capabilities=granted_capabilities,
            denied_capabilities=denied_capabilities,
            max_steps=max_steps,
            max_duration_seconds=max_duration_seconds,
            max_command_output_bytes=max_command_output_bytes,
            context=context,
        ),
        "outputSchema": CodingOutput.model_json_schema(by_alias=True),
    }
    return _json_text(prompt)


def _tool_calls_from_output(output: dict[str, Any]) -> tuple[CodingToolCall, ...]:
    raw_calls = output.get("toolCalls", ())
    if raw_calls in (None, ()):
        return ()
    if not isinstance(raw_calls, (list, tuple)):
        raise ValueError("toolCalls must be a list")
    return tuple(CodingToolCall.model_validate(call) for call in raw_calls)


def _coding_output_candidate(output: dict[str, Any]) -> dict[str, Any]:
    content = output.get("content")
    if isinstance(content, dict) and set(output) == {"content"}:
        return content
    return output


async def _collect_changed_files(
    workspace: Workspace,
    workspace_provider: WorkspaceProvider,
) -> tuple[CodingChangedFile, ...]:
    result = await workspace_provider.run_command(
        _workspace_handle(workspace),
        WorkspaceCommandRequest(
            command=("git", "status", "--porcelain"),
            timeoutSeconds=min(30, workspace.spec.policy.command_timeout_seconds),
        ),
    )
    if result.exit_code != 0:
        return ()
    return _parse_git_status_porcelain(result.stdout)


def _parse_git_status_porcelain(output: str) -> tuple[CodingChangedFile, ...]:
    changes: list[CodingChangedFile] = []
    for line in output.splitlines():
        if not line:
            continue
        status = line[:2]
        path = line[3:] if len(line) > 3 else ""
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        if not path:
            continue
        if "D" in status:
            change_type = CodingChangeType.DELETED
        elif status == "??" or "A" in status:
            change_type = CodingChangeType.ADDED
        else:
            change_type = CodingChangeType.MODIFIED
        changes.append(CodingChangedFile(path=path, changeType=change_type))
    return tuple(changes)


def _workspace_handle(workspace: Workspace) -> WorkspaceHandle:
    if workspace.status.path is None:
        raise ValueError("Workspace has no prepared path")
    return WorkspaceHandle(
        path=workspace.status.path,
        observedRevision=workspace.status.observed_revision or "unknown",
    )


def _append_refs(
    existing: tuple[ResourceReference, ...],
    additions: tuple[ResourceReference, ...],
) -> tuple[ResourceReference, ...]:
    by_key = {(ref.kind, ref.id): ref for ref in existing}
    for ref in additions:
        by_key[(ref.kind, ref.id)] = ref
    return tuple(by_key.values())


def _last_or_none(values: list[Artifact]) -> Artifact | None:
    if not values:
        return None
    return values[-1]


def _resource_ref(resource: Artifact | RoleInvocation) -> ResourceReference:
    return ResourceReference(
        kind=resource.kind,
        id=resource.metadata.id,
        name=resource.metadata.name,
    )


def _provider_ref(provider: Provider) -> RoleInvocationProviderReference:
    return RoleInvocationProviderReference(
        id=provider.metadata.id,
        name=provider.metadata.name,
    )


def _json_text(value: Any) -> str:
    return json.dumps(value, indent=2, sort_keys=True)


def _json_bytes(value: Any) -> bytes:
    return _json_text(value).encode("utf-8")
