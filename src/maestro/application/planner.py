"""Planner Role runtime."""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID

from pydantic import Field, ValidationError

from maestro.application.artifacts import ArtifactService
from maestro.application.controllers import observe_generation, with_condition
from maestro.domain.agents import Agent
from maestro.domain.artifacts import (
    Artifact,
    ArtifactExecutionReference,
    ArtifactProducer,
    ArtifactRoleInvocationReference,
    ArtifactType,
)
from maestro.domain.capabilities import CapabilityName
from maestro.domain.events import (
    EventDraft,
    EventExecutionReference,
    EventPublisher,
)
from maestro.domain.exceptions import CapabilityPolicyDeniedError
from maestro.domain.executions import (
    Execution,
    ExecutionPhase,
    ExecutionRepository,
)
from maestro.domain.plans import (
    Plan,
    PlanExecutionReference,
    PlanRepository,
    PlanRisk,
    PlanRoleReference,
    PlanSpec,
    PlanWorkItemProposal,
    PlanWorkItemVerification,
)
from maestro.domain.projects import Project, ProjectRepository
from maestro.domain.providers import (
    ModelProvider,
    Provider,
    ProviderMessage,
    ProviderMessageRole,
    StructuredGenerationRequest,
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
)

PLANNER_RUNTIME = "planner-runtime"
PLANNER_FORBIDDEN_CAPABILITY_PREFIXES = (
    "filesystem.write",
    "filesystem.edit",
    "shell.execute",
)
PLANNER_PROMPT_TEMPLATE = """You are Maestro's Planner Role.

Produce a compact JSON object that conforms to the supplied schema.
Do not modify files, execute commands, approve plans, or schedule agents.
Create small independently verifiable work items.
Ask blocking questions only when the goal cannot safely proceed without them.
"""


class PlannerQuestion(MaestroModel):
    """Question produced by the Planner."""

    id: ResourceName
    question: str = Field(min_length=1)
    blocking: bool = False


class PlannerRoleRef(MaestroModel):
    """Role reference in Planner output."""

    name: ResourceName
    version: str = Field(min_length=1)


class PlannerRiskOutput(MaestroModel):
    """Risk produced by the Planner."""

    description: str = Field(min_length=1)
    mitigation: str = ""


class PlannerVerificationOutput(MaestroModel):
    """WorkItem verification commands in Planner output."""

    commands: tuple[str, ...] = Field(default_factory=tuple)


class PlannerWorkItemOutput(MaestroModel):
    """WorkItem proposal produced by the Planner."""

    id: ResourceName
    title: str = Field(min_length=1)
    role_ref: PlannerRoleRef = Field(alias="roleRef")
    repository_ref: ResourceName | None = Field(default=None, alias="repositoryRef")
    objective: str = Field(min_length=1)
    context_refs: tuple[ResourceReference, ...] = Field(
        default_factory=tuple,
        alias="contextRefs",
    )
    constraints: tuple[str, ...] = Field(default_factory=tuple)
    acceptance_criteria: tuple[str, ...] = Field(
        min_length=1,
        alias="acceptanceCriteria",
    )
    verification: PlannerVerificationOutput = Field(
        default_factory=PlannerVerificationOutput
    )
    depends_on: tuple[ResourceName, ...] = Field(
        default_factory=tuple,
        alias="dependsOn",
    )
    requested_capabilities: tuple[CapabilityName, ...] = Field(
        default_factory=tuple,
        alias="requestedCapabilities",
    )


class PlannerOutput(MaestroModel):
    """Structured Planner output."""

    summary: str = Field(min_length=1)
    assumptions: tuple[str, ...] = Field(default_factory=tuple)
    questions: tuple[PlannerQuestion, ...] = Field(default_factory=tuple)
    risks: tuple[PlannerRiskOutput, ...] = Field(default_factory=tuple)
    work_items: tuple[PlannerWorkItemOutput, ...] = Field(
        default_factory=tuple,
        alias="workItems",
    )


class PlannerInvocationResult(MaestroModel):
    """Result of invoking the Planner Role."""

    invocation_ref: ResourceReference = Field(alias="invocationRef")
    plan_ref: ResourceReference | None = Field(default=None, alias="planRef")
    plan_artifact_ref: ResourceReference | None = Field(
        default=None,
        alias="planArtifactRef",
    )
    questions: tuple[PlannerQuestion, ...] = Field(default_factory=tuple)
    repair_attempted: bool = Field(default=False, alias="repairAttempted")


class PlannerRuntime:
    """Invoke the Planner Role through a model Provider."""

    def __init__(
        self,
        *,
        execution_repository: ExecutionRepository,
        project_repository: ProjectRepository,
        plan_repository: PlanRepository,
        role_invocation_repository: RoleInvocationRepository,
        artifact_service: ArtifactService,
        event_publisher: EventPublisher | None = None,
    ) -> None:
        self._execution_repository = execution_repository
        self._project_repository = project_repository
        self._plan_repository = plan_repository
        self._role_invocation_repository = role_invocation_repository
        self._artifact_service = artifact_service
        self._event_publisher = event_publisher

    async def invoke_planner(
        self,
        execution_id: UUID,
        *,
        agent: Agent,
        provider: Provider,
        runtime: ModelProvider,
        granted_capabilities: tuple[CapabilityName, ...],
        repository_context: dict[str, Any] | None = None,
        knowledge_context: dict[str, Any] | None = None,
    ) -> PlannerInvocationResult:
        """Invoke the Planner and persist prompts, outputs, invocation and Plan."""

        _ensure_planner_capabilities(granted_capabilities)
        execution = await self._execution_repository.get(execution_id)
        project = await self._project_repository.get(execution.spec.project_ref.id)
        version = await self._next_plan_version(execution)
        invocation = await self._create_invocation(
            execution,
            agent,
            granted_capabilities=granted_capabilities,
            version=version,
        )
        running = await self._mark_invocation_running(
            invocation,
            provider=provider,
            model=agent.spec.model,
        )

        prompt_artifact: Artifact | None = None
        response_artifact: Artifact | None = None
        repair_attempted = False
        validation_error = ""
        for attempt in (1, 2):
            repair_attempted = attempt == 2
            prompt = _planner_prompt(
                execution,
                project,
                repository_context=repository_context or {},
                knowledge_context=knowledge_context or {},
                validation_error=validation_error,
            )
            prompt_artifact = await self._create_artifact(
                invocation=running,
                execution=execution,
                name=_artifact_name("planner-prompt", running, attempt),
                artifact_type=ArtifactType.PROMPT,
                media_type="text/markdown",
                content=prompt.encode("utf-8"),
            )
            response = await runtime.generate_structured(
                StructuredGenerationRequest(
                    model=agent.spec.model,
                    messages=(
                        ProviderMessage(
                            role=ProviderMessageRole.SYSTEM,
                            content=PLANNER_PROMPT_TEMPLATE,
                        ),
                        ProviderMessage(
                            role=ProviderMessageRole.USER,
                            content=prompt,
                        ),
                    ),
                    responseSchema=PlannerOutput.model_json_schema(by_alias=True),
                    timeoutSeconds=provider.spec.timeout_seconds,
                )
            )
            response_artifact = await self._create_artifact(
                invocation=running,
                execution=execution,
                name=_artifact_name("planner-response", running, attempt),
                artifact_type=ArtifactType.MODEL_RESPONSE,
                media_type="application/json",
                content=_json_bytes(
                    {
                        "output": response.output,
                        "rawText": response.raw_text,
                        "tokenUsage": response.token_usage.model_dump(
                            mode="json",
                            by_alias=True,
                        ),
                    }
                ),
                source_refs=(_resource_ref(prompt_artifact),),
            )
            try:
                planner_output = PlannerOutput.model_validate(response.output)
                result = await self._handle_valid_output(
                    execution,
                    project,
                    running,
                    provider,
                    agent,
                    planner_output,
                    version=version,
                    prompt_artifact=prompt_artifact,
                    response_artifact=response_artifact,
                    repair_attempted=repair_attempted,
                )
                return result
            except (ValidationError, ValueError) as error:
                validation_error = str(error)
                if attempt == 2:
                    failed = await self._mark_invocation_failed(
                        running,
                        provider=provider,
                        model=agent.spec.model,
                        prompt_artifact=prompt_artifact,
                        response_artifact=response_artifact,
                        reason="PlannerOutputInvalid",
                        message=validation_error,
                    )
                    raise PlannerOutputError(
                        failed.metadata.id,
                        validation_error,
                    ) from error

        raise AssertionError("planner repair loop exhausted unexpectedly")

    async def _handle_valid_output(
        self,
        execution: Execution,
        project: Project,
        invocation: RoleInvocation,
        provider: Provider,
        agent: Agent,
        planner_output: PlannerOutput,
        *,
        version: int,
        prompt_artifact: Artifact,
        response_artifact: Artifact,
        repair_attempted: bool,
    ) -> PlannerInvocationResult:
        blocking_questions = tuple(
            question for question in planner_output.questions if question.blocking
        )
        if blocking_questions:
            succeeded = await self._mark_invocation_succeeded(
                invocation,
                provider=provider,
                model=agent.spec.model,
                prompt_artifact=prompt_artifact,
                response_artifact=response_artifact,
                output_artifacts=(),
            )
            await self._move_execution_to_user_input(execution, blocking_questions)
            await self._publish_event(
                "PlannerQuestionsProduced",
                execution=execution,
                subject=_resource_ref(succeeded),
                payload={
                    "questions": tuple(
                        question.model_dump(mode="json", by_alias=True)
                        for question in blocking_questions
                    ),
                },
            )
            return PlannerInvocationResult(
                invocationRef=_resource_ref(succeeded),
                questions=blocking_questions,
                repairAttempted=repair_attempted,
            )

        plan_spec = _plan_spec_from_output(execution, planner_output, version=version)
        plan = await self._plan_repository.create(
            Plan.new(
                name=f"plan-{execution.metadata.id.hex[:12]}-v{version}",
                namespace=execution.metadata.namespace,
                spec=plan_spec,
            )
        )
        plan_artifact = await self._create_artifact(
            invocation=invocation,
            execution=execution,
            name=f"planner-plan-{invocation.metadata.id.hex[:12]}",
            artifact_type=ArtifactType.PLAN,
            media_type="application/json",
            content=_json_bytes(plan.model_dump(mode="json", by_alias=True)),
            source_refs=(_resource_ref(response_artifact),),
        )
        succeeded = await self._mark_invocation_succeeded(
            invocation,
            provider=provider,
            model=agent.spec.model,
            prompt_artifact=prompt_artifact,
            response_artifact=response_artifact,
            output_artifacts=(plan_artifact,),
        )
        await self._publish_event(
            "PlanProduced",
            execution=execution,
            subject=_resource_ref(plan),
            payload={
                "planVersion": plan.spec.version,
                "roleInvocationId": str(succeeded.metadata.id),
            },
        )
        return PlannerInvocationResult(
            invocationRef=_resource_ref(succeeded),
            planRef=_resource_ref(plan),
            planArtifactRef=_resource_ref(plan_artifact),
            questions=planner_output.questions,
            repairAttempted=repair_attempted,
        )

    async def _create_invocation(
        self,
        execution: Execution,
        agent: Agent,
        *,
        granted_capabilities: tuple[CapabilityName, ...],
        version: int,
    ) -> RoleInvocation:
        invocation = RoleInvocation.new(
            name=f"planner-{execution.metadata.id.hex[:12]}-v{version}",
            namespace=execution.metadata.namespace,
            spec=RoleInvocationSpec(
                executionRef=RoleInvocationExecutionReference(
                    id=execution.metadata.id,
                    name=execution.metadata.name,
                ),
                roleRef=RoleInvocationRoleReference(
                    name="planner",
                    version="v1alpha1",
                ),
                agentRef=RoleInvocationAgentReference(
                    id=agent.metadata.id,
                    name=agent.metadata.name,
                ),
                grantedCapabilities=granted_capabilities,
                limits=RoleInvocationLimits(
                    maxSteps=execution.spec.limits.max_tool_calls_per_invocation,
                    maxDurationSeconds=execution.spec.limits.max_duration_seconds,
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
        output_artifacts: tuple[Artifact, ...],
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
                "output_artifact_refs": tuple(
                    _resource_ref(artifact) for artifact in output_artifacts
                ),
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
        response_artifact: Artifact,
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
                "response_artifact_ref": _resource_ref(response_artifact),
                "completed_at": utc_now(),
                "failure": RoleInvocationFailure(reason=reason, message=message),
            }
        )
        return await self._role_invocation_repository.update_status(
            current.metadata.id,
            status,
            expected_resource_version=current.metadata.resource_version,
        )

    async def _create_artifact(
        self,
        *,
        invocation: RoleInvocation,
        execution: Execution,
        name: ResourceName,
        artifact_type: ArtifactType,
        media_type: str,
        content: bytes,
        source_refs: tuple[ResourceReference, ...] = (),
    ) -> Artifact:
        artifact = await self._artifact_service.create_bytes_artifact(
            name=name,
            execution_ref=ArtifactExecutionReference(
                id=execution.metadata.id,
                name=execution.metadata.name,
            ),
            artifact_type=artifact_type,
            media_type=media_type,
            content=content,
            producer=ArtifactProducer(
                subsystem=PLANNER_RUNTIME,
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

    async def _move_execution_to_user_input(
        self,
        execution: Execution,
        questions: tuple[PlannerQuestion, ...],
    ) -> Execution:
        current = await self._execution_repository.get(execution.metadata.id)
        status = current.status.model_copy(
            update={
                "observed_generation": current.metadata.generation,
                "phase": ExecutionPhase.WAITING_FOR_USER_INPUT,
                "current_step": "planner-questions",
            }
        )
        status = with_condition(
            current,
            observe_generation(current, status),
            condition_type="Reconciled",
            condition_status=ConditionStatus.UNKNOWN,
            reason="PlannerQuestionsNeedInput",
            message="; ".join(question.question for question in questions),
        )
        return await self._execution_repository.update_status(
            current.metadata.id,
            status,
            expected_resource_version=current.metadata.resource_version,
        )

    async def _next_plan_version(self, execution: Execution) -> int:
        plans = await self._plan_repository.list_by_execution(execution.metadata.id)
        if not plans:
            return 1
        return max(plan.spec.version for plan in plans) + 1

    async def _publish_event(
        self,
        event_type: str,
        *,
        execution: Execution,
        subject: ResourceReference,
        payload: dict[str, Any],
    ) -> None:
        if self._event_publisher is None:
            return
        await self._event_publisher.publish(
            EventDraft(
                type=event_type,
                producer=PLANNER_RUNTIME,
                correlationId=f"planner:{execution.metadata.id}:{event_type}",
                executionRef=EventExecutionReference(
                    id=execution.metadata.id,
                    name=execution.metadata.name,
                ),
                subjectRef=subject,
                payload=payload,
            )
        )


class PlannerOutputError(ValueError):
    """Raised when Planner output cannot be repaired into a valid Plan."""

    def __init__(self, invocation_id: UUID, message: str) -> None:
        self.invocation_id = invocation_id
        super().__init__(f"Invalid Planner output for {invocation_id}: {message}")


def build_planner_input(
    execution: Execution,
    project: Project,
    *,
    repository_context: dict[str, Any] | None = None,
    knowledge_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build provider-independent Planner input."""

    return {
        "goal": execution.spec.goal.model_dump(mode="json", by_alias=True),
        "project": {
            "name": project.metadata.name,
            "repositories": tuple(
                {
                    "id": repository.id,
                    "path": str(repository.path),
                    "defaultBranch": repository.default_branch,
                    "type": repository.type.value,
                }
                for repository in project.spec.repositories
            ),
        },
        "repositoryContext": repository_context or {},
        "knowledgeContext": knowledge_context or {},
        "workflowContext": {
            "workflowRef": execution.spec.workflow_ref.model_dump(
                mode="json",
                by_alias=True,
            ),
            "permittedRoleRefs": tuple(execution.spec.requested_roles),
            "policySummary": "Planner may only produce a Plan and questions.",
        },
    }


def _planner_prompt(
    execution: Execution,
    project: Project,
    *,
    repository_context: dict[str, Any],
    knowledge_context: dict[str, Any],
    validation_error: str = "",
) -> str:
    planner_input = build_planner_input(
        execution,
        project,
        repository_context=repository_context,
        knowledge_context=knowledge_context,
    )
    prompt = {
        "instructions": PLANNER_PROMPT_TEMPLATE,
        "input": planner_input,
        "outputSchema": PlannerOutput.model_json_schema(by_alias=True),
    }
    if validation_error:
        prompt["repairInstructions"] = (
            "The previous output failed validation. Return a corrected JSON object "
            "only, with no markdown."
        )
        prompt["validationError"] = validation_error
    return json.dumps(prompt, indent=2, sort_keys=True)


def _plan_spec_from_output(
    execution: Execution,
    output: PlannerOutput,
    *,
    version: int,
) -> PlanSpec:
    return PlanSpec(
        executionRef=PlanExecutionReference(
            id=execution.metadata.id,
            name=execution.metadata.name,
        ),
        version=version,
        summary=output.summary,
        assumptions=output.assumptions,
        questions=tuple(question.question for question in output.questions),
        risks=tuple(
            PlanRisk(description=risk.description, mitigation=risk.mitigation)
            for risk in output.risks
        ),
        workItems=tuple(
            PlanWorkItemProposal(
                id=work_item.id,
                title=work_item.title,
                roleRef=PlanRoleReference(
                    name=work_item.role_ref.name,
                    version=work_item.role_ref.version,
                ),
                repositoryRef=work_item.repository_ref,
                objective=work_item.objective,
                contextRefs=work_item.context_refs,
                constraints=work_item.constraints,
                acceptanceCriteria=work_item.acceptance_criteria,
                verification=PlanWorkItemVerification(
                    commands=work_item.verification.commands
                ),
                dependsOn=work_item.depends_on,
                requestedCapabilities=work_item.requested_capabilities,
            )
            for work_item in output.work_items
        ),
    )


def _ensure_planner_capabilities(
    granted_capabilities: tuple[CapabilityName, ...],
) -> None:
    forbidden = tuple(
        capability
        for capability in granted_capabilities
        if any(
            capability == prefix or capability.startswith(f"{prefix}.")
            for prefix in PLANNER_FORBIDDEN_CAPABILITY_PREFIXES
        )
    )
    if forbidden:
        raise CapabilityPolicyDeniedError(
            "PlannerForbiddenCapability",
            "Planner cannot receive: " + ", ".join(forbidden),
        )


def _artifact_name(
    prefix: str,
    invocation: RoleInvocation,
    attempt: int,
) -> ResourceName:
    return f"{prefix}-{invocation.metadata.id.hex[:12]}-{attempt}"


def _json_bytes(value: Any) -> bytes:
    return json.dumps(value, indent=2, sort_keys=True).encode("utf-8")


def _resource_ref(resource: Artifact | Plan | RoleInvocation) -> ResourceReference:
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
