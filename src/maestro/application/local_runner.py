"""Local autonomous Execution runner used by the browser MVP."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from collections.abc import Iterable
from typing import Any
from uuid import UUID, uuid4

from pydantic import Field, ValidationError

from maestro.application.artifacts import ArtifactService
from maestro.application.coding import CodingRuntime
from maestro.application.controllers import (
    ReconcileKey,
    ReconciliationContext,
    RetryPolicy,
    observe_generation,
    with_condition,
)
from maestro.application.planner import PlannerRuntime
from maestro.application.resource_controllers import (
    ExecutionController,
    PlanController,
    WorkItemController,
)
from maestro.application.reviewer import ReviewerRuntime
from maestro.application.scheduler import WorkItemScheduler
from maestro.application.tools import CodingToolRuntime
from maestro.application.verification import VerificationController
from maestro.application.workspaces import WorkspaceLifecycleService
from maestro.config import Settings
from maestro.domain.agents import (
    Agent,
    AgentCapabilityBindingReference,
    AgentCapacity,
    AgentPhase,
    AgentProviderReference,
    AgentRepository,
    AgentScheduling,
    AgentSpec,
    AgentStatus,
    AgentSupportedRole,
)
from maestro.domain.approvals import (
    Approval,
    ApprovalExecutionReference,
    ApprovalPhase,
    ApprovalRepository,
    ApprovalSpec,
    ApprovalSubjectReference,
    ApprovalType,
)
from maestro.domain.artifacts import ArtifactRepository
from maestro.domain.capabilities import (
    Capability,
    CapabilityApprovalPolicy,
    CapabilityBinding,
    CapabilityBindingPhase,
    CapabilityBindingRepository,
    CapabilityBindingSpec,
    CapabilityBindingStatus,
    CapabilityName,
    CapabilityPhase,
    CapabilityRepository,
    CapabilityScope,
    CapabilitySideEffectLevel,
    CapabilitySpec,
    CapabilityStatus,
)
from maestro.domain.events import (
    EventDraft,
    EventExecutionReference,
    EventStore,
)
from maestro.domain.exceptions import ResourceNameNotFoundError
from maestro.domain.executions import (
    TERMINAL_EXECUTION_PHASES,
    Execution,
    ExecutionPhase,
    ExecutionRepository,
)
from maestro.domain.plans import Plan, PlanPhase, PlanRepository
from maestro.domain.projects import Project, ProjectRepository
from maestro.domain.providers import (
    Provider,
    ProviderDataPolicy,
    ProviderFailure,
    ProviderFeatureSet,
    ProviderMessage,
    ProviderMessageRole,
    ProviderOperationError,
    ProviderPhase,
    ProviderRepository,
    ProviderSpec,
    ProviderStatus,
    StructuredGenerationRequest,
)
from maestro.domain.repositories import ResourceSelector
from maestro.domain.resources import (
    BaseResource,
    ConditionStatus,
    MaestroModel,
    ResourceName,
    ResourceReference,
    utc_now,
)
from maestro.domain.reviews import ReviewPhase, ReviewRepository
from maestro.domain.role_invocations import (
    RoleInvocation,
    RoleInvocationPhase,
    RoleInvocationRepository,
)
from maestro.domain.roles import (
    Role,
    RoleExecutionPolicy,
    RolePhase,
    RoleRepository,
    RoleSpec,
    RoleStatus,
    RoleValidationResult,
)
from maestro.domain.work_items import (
    WorkItem,
    WorkItemAgentReference,
    WorkItemPhase,
    WorkItemRepository,
    WorkItemWorkspaceReference,
)
from maestro.domain.workspaces import (
    Workspace,
    WorkspaceExecutionReference,
    WorkspacePhase,
    WorkspaceProviderReference,
    WorkspaceRepository,
    WorkspaceSpec,
)
from maestro.infrastructure.artifacts import LocalArtifactStorage
from maestro.infrastructure.providers.codex import (
    DEFAULT_CODEX_MODEL,
    CodexProvider,
)
from maestro.infrastructure.providers.ollama import OllamaProvider
from maestro.infrastructure.workspaces import LocalGitWorktreeProvider

DEFAULT_OLLAMA_HOST = "http://127.0.0.1:11434"
DEFAULT_OLLAMA_PROVIDER_NAME = "ollama-local"
DEFAULT_CODEX_PROVIDER_NAME = "codex-local"
DEFAULT_WORKSPACE_PROVIDER_NAME = "local-git"
DEFAULT_LOCAL_ROLE_VERSION = "v1alpha1"
DEFAULT_LOCAL_ROLE_VERSIONS = ("v1alpha1", "v1")
DEFAULT_CODING_CAPABILITIES = (
    "filesystem.read",
    "filesystem.write",
    "shell.execute.test",
    "git.status",
    "git.diff",
)
RUNNER_MAX_STEPS = 40
RUNNER_FAILURE_PHASES = frozenset(
    {
        ExecutionPhase.PLANNING,
        ExecutionPhase.PREPARING_WORKSPACE,
        ExecutionPhase.EXECUTING,
        ExecutionPhase.VERIFYING,
        ExecutionPhase.REVIEWING,
    }
)
PLANNER_REPAIR_ADVISOR_PROMPT = (
    "You are Maestro's Planner in advisor mode. Diagnose why the Coding "
    "Role failed one approved WorkItem and provide concrete instructions for "
    "retrying the same WorkItem. Do not rewrite the plan, add work items, ask "
    "for user approval, or suggest manual shell commands for the user."
)


class PlannerRepairAdvice(MaestroModel):
    """Planner guidance for retrying a failed Coding WorkItem."""

    summary: str = Field(min_length=1)
    instructions: tuple[str, ...] = Field(default_factory=tuple)


def _effective_coding_capabilities(
    requested: tuple[CapabilityName, ...],
) -> tuple[CapabilityName, ...]:
    """Grant the local safe coding tool set plus any scheduler-approved extras."""

    granted: list[CapabilityName] = list(DEFAULT_CODING_CAPABILITIES)
    for capability in requested:
        if capability not in granted:
            granted.append(capability)
    return tuple(granted)


class LocalExecutionRunner:
    """Drive one local Execution until it needs a human or reaches a terminal state."""

    def __init__(
        self,
        *,
        settings: Settings,
        project_repository: ProjectRepository,
        execution_repository: ExecutionRepository,
        plan_repository: PlanRepository,
        work_item_repository: WorkItemRepository,
        workspace_repository: WorkspaceRepository,
        artifact_repository: ArtifactRepository,
        artifact_storage: LocalArtifactStorage,
        approval_repository: ApprovalRepository,
        review_repository: ReviewRepository,
        provider_repository: ProviderRepository,
        agent_repository: AgentRepository,
        role_repository: RoleRepository,
        capability_repository: CapabilityRepository,
        capability_binding_repository: CapabilityBindingRepository,
        role_invocation_repository: RoleInvocationRepository,
        event_publisher: EventStore,
    ) -> None:
        self._settings = settings
        self._projects = project_repository
        self._executions = execution_repository
        self._plans = plan_repository
        self._work_items = work_item_repository
        self._workspaces = workspace_repository
        self._artifacts = artifact_repository
        self._artifact_storage = artifact_storage
        self._approvals = approval_repository
        self._reviews = review_repository
        self._providers = provider_repository
        self._agents = agent_repository
        self._roles = role_repository
        self._capabilities = capability_repository
        self._bindings = capability_binding_repository
        self._role_invocations = role_invocation_repository
        self._events = event_publisher
        self._artifact_service = ArtifactService(
            artifact_repository,
            artifact_storage,
        )

    async def run(self, execution_id: UUID) -> None:
        """Run backend orchestration for one Execution."""

        try:
            execution = await self._executions.get(execution_id)
            await self._publish_runner_event(
                execution,
                "ExecutionRunStarted",
                {"phase": execution.status.phase},
            )
            await self._ensure_runtime_catalog(execution.metadata.namespace)

            for _ in range(RUNNER_MAX_STEPS):
                execution = await self._executions.get(execution_id)
                if execution.status.phase in TERMINAL_EXECUTION_PHASES:
                    await self._publish_runner_event(
                        execution,
                        "ExecutionRunCompleted",
                        {"phase": execution.status.phase},
                    )
                    return

                match execution.status.phase:
                    case ExecutionPhase.DRAFT:
                        await self._reconcile_execution(execution)
                    case ExecutionPhase.PLANNING:
                        await self._run_planning(execution)
                    case ExecutionPhase.WAITING_FOR_PLAN_APPROVAL:
                        if not await self._advance_approved_plan(execution):
                            await self._publish_runner_event(
                                execution,
                                "ExecutionRunWaiting",
                                {"reason": "WaitingForPlanApproval"},
                            )
                            return
                    case ExecutionPhase.PREPARING_WORKSPACE:
                        await self._prepare_workspace(execution)
                    case ExecutionPhase.EXECUTING:
                        await self._run_execution_work_items(execution)
                    case ExecutionPhase.VERIFYING:
                        await self._verify_work_items(execution)
                    case ExecutionPhase.REVIEWING:
                        await self._run_reviews(execution)
                    case ExecutionPhase.WAITING_FOR_FINAL_APPROVAL:
                        if not await self._has_approved_final_approval(execution):
                            await self._publish_runner_event(
                                execution,
                                "ExecutionRunWaiting",
                                {"reason": "WaitingForFinalApproval"},
                            )
                            return
                        await self._publish_final_workspace(execution)
                        await self._reconcile_execution(execution)
                    case ExecutionPhase.WAITING_FOR_USER_INPUT:
                        await self._publish_runner_event(
                            execution,
                            "ExecutionRunWaiting",
                            {"reason": "WaitingForUserInput"},
                        )
                        return
                    case _:
                        await self._publish_runner_event(
                            execution,
                            "ExecutionRunWaiting",
                            {"reason": f"WaitingIn{execution.status.phase}"},
                        )
                        return

            execution = await self._executions.get(execution_id)
            await self._publish_runner_event(
                execution,
                "ExecutionRunWaiting",
                {"reason": "StepBudgetExhausted"},
            )
        except Exception as error:  # noqa: BLE001 - runner failures must become evidence.
            await self._publish_runner_failure(execution_id, error)

    async def _run_planning(self, execution: Execution) -> None:
        plans = await self._plans.list_by_execution(execution.metadata.id)
        reusable_plan = _latest_reusable_plan(plans)
        if reusable_plan is not None:
            await self._prepare_plan_for_approval(execution, reusable_plan)
            await self._reconcile_execution(execution)
            return

        await self._publish_runner_event(
            execution,
            "PlannerRunStarted",
            {"provider": DEFAULT_OLLAMA_PROVIDER_NAME},
        )
        provider = await self._ensure_ollama_provider(execution.metadata.namespace)
        agent = await self._ensure_agent(
            execution.metadata.namespace,
            name="planner-local",
            role_name="planner",
            provider_name=provider.metadata.name,
            model=_planner_model(provider),
        )
        result = await self._planner_runtime().invoke_planner(
            execution.metadata.id,
            agent=agent,
            provider=provider,
            runtime=OllamaProvider.from_provider(provider),
            granted_capabilities=("filesystem.read",),
            repository_context=await self._repository_context(execution),
            knowledge_context=await self._user_input_context(execution),
        )
        if result.plan_ref is not None:
            plan = await self._plans.get(result.plan_ref.id)
            await self._prepare_plan_for_approval(execution, plan)
        await self._publish_runner_event(
            execution,
            "PlannerRunCompleted",
            {"planId": str(result.plan_ref.id) if result.plan_ref else None},
        )
        await self._reconcile_execution(execution)

    async def _prepare_plan_for_approval(
        self,
        execution: Execution,
        plan: Plan,
    ) -> None:
        await self._plan_controller().reconcile(_context_for(plan))
        plan = await self._plans.get(plan.metadata.id)
        if plan.status.phase == PlanPhase.WAITING_FOR_APPROVAL:
            await self._ensure_approval(execution, plan, ApprovalType.PLAN)

    async def _advance_approved_plan(self, execution: Execution) -> bool:
        plans = await self._plans.list_by_execution(execution.metadata.id)
        advanced = False
        for plan in plans:
            await self._plan_controller().reconcile(_context_for(plan))
            updated = await self._plans.get(plan.metadata.id)
            if updated.status.phase in {PlanPhase.APPROVED, PlanPhase.REJECTED}:
                await self._plan_controller().reconcile(_context_for(updated))
                advanced = True
        if advanced:
            await self._reconcile_execution(execution)
        return advanced

    async def _prepare_workspace(self, execution: Execution) -> None:
        project = await self._projects.get(execution.spec.project_ref.id)
        repository = _primary_repository(project)
        source_path = repository.path
        if source_path is None:
            raise ValueError("Project repository path is required for local runner")

        workspace = await self._ensure_workspace(execution, project)
        if workspace.status.path is None:
            await self._publish_runner_event(
                execution,
                "WorkspacePreparationStarted",
                {"repository": repository.id},
            )
            workspace_provider = LocalGitWorktreeProvider(_git_executable())
            workspace = await WorkspaceLifecycleService(
                self._workspaces
            ).prepare_workspace(
                workspace.metadata.id,
                workspace_provider,
                source_repository_path=source_path,
                workspace_root=self._settings.workspace_root,
                expected_resource_version=workspace.metadata.resource_version,
            )
            if workspace.status.phase == WorkspacePhase.READY:
                await self._publish_runner_event(
                    execution,
                    "WorkspacePrepared",
                    {"workspaceId": str(workspace.metadata.id)},
                    subject=workspace,
                )
            else:
                await self._publish_runner_event(
                    execution,
                    "WorkspacePreparationFailed",
                    {
                        "workspaceId": str(workspace.metadata.id),
                        "message": workspace.status.failure_message,
                    },
                    subject=workspace,
                )

        if workspace.status.phase not in {
            WorkspacePhase.READY,
            WorkspacePhase.IN_USE,
            WorkspacePhase.DIRTY,
        }:
            await self._reconcile_execution(execution)
            return
        await self._attach_workspace_to_work_items(execution, workspace)
        await self._reconcile_execution(execution)
        updated = await self._executions.get(execution.metadata.id)
        if updated.status.phase == ExecutionPhase.EXECUTING:
            await self._run_execution_work_items(updated)

    async def _run_execution_work_items(self, execution: Execution) -> None:
        work_items = await self._work_items.list_by_execution(execution.metadata.id)
        for work_item in work_items:
            await self._work_item_controller().reconcile(_context_for(work_item))

        await self._retry_role_catalog_blocked_work_items(execution)
        work_items = await self._work_items.list_by_execution(execution.metadata.id)
        for work_item in work_items:
            if work_item.status.phase == WorkItemPhase.READY:
                decision = await self._scheduler().schedule_work_item(
                    work_item.metadata.id
                )
                if not decision.scheduled:
                    continue
                work_item = await self._work_items.get(work_item.metadata.id)

            if work_item.status.phase == WorkItemPhase.SCHEDULED:
                await self._run_coding_work_item(work_item)

        await self._verify_pending_work_items(execution)
        execution = await self._executions.get(execution.metadata.id)
        await self._reconcile_execution(execution)

    async def _run_coding_work_item(self, work_item: WorkItem) -> None:
        if work_item.spec.workspace_ref is None:
            raise ValueError(f"WorkItem {work_item.metadata.name} has no Workspace")
        if work_item.status.assigned_agent_ref is None:
            raise ValueError(f"WorkItem {work_item.metadata.name} has no Agent")

        execution = await self._executions.get(work_item.spec.execution_ref.id)
        workspace = await self._workspaces.get(work_item.spec.workspace_ref.id)
        agent = await self._agents.get(work_item.status.assigned_agent_ref.id)
        provider = await self._ensure_ollama_provider(work_item.metadata.namespace)
        coding_context = await self._coding_context(execution, work_item, provider)
        await self._publish_runner_event(
            execution,
            "CodingRunStarted",
            {
                "workItemId": str(work_item.metadata.id),
                "agent": agent.metadata.name,
                "attempt": work_item.status.attempt + 1,
            },
            subject=work_item,
        )
        try:
            await self._coding_runtime().invoke_coding(
                work_item.metadata.id,
                workspace=workspace,
                workspace_provider=LocalGitWorktreeProvider(_git_executable()),
                agent=agent,
                provider=provider,
                runtime=OllamaProvider.from_provider(provider),
                granted_capabilities=_effective_coding_capabilities(
                    work_item.spec.requested_capabilities
                ),
                max_steps=execution.spec.limits.max_tool_calls_per_invocation,
                max_duration_seconds=execution.spec.limits.max_duration_seconds,
                context=coding_context,
            )
        finally:
            await self._release_agent_assignment(work_item.status.assigned_agent_ref)

    async def _coding_context(
        self,
        execution: Execution,
        work_item: WorkItem,
        provider: Provider,
    ) -> dict[str, Any]:
        failure_context = await self._previous_coding_failure_context(
            execution,
            work_item,
        )
        if failure_context is None:
            return {}

        context: dict[str, Any] = {"previousCodingFailure": failure_context}
        advice = await self._planner_repair_advice(
            execution,
            work_item,
            provider,
            failure_context,
        )
        if advice is not None:
            context["plannerRepairAdvice"] = advice.model_dump(
                mode="json",
                by_alias=True,
            )
        return context

    async def _planner_repair_advice(
        self,
        execution: Execution,
        work_item: WorkItem,
        provider: Provider,
        failure_context: dict[str, Any],
    ) -> PlannerRepairAdvice | None:
        await self._publish_runner_event(
            execution,
            "PlannerRepairAdviceStarted",
            {
                "workItemId": str(work_item.metadata.id),
                "attempt": work_item.status.attempt + 1,
            },
            subject=work_item,
        )
        runtime = OllamaProvider.from_provider(provider)
        try:
            response = await runtime.generate_structured(
                StructuredGenerationRequest(
                    model=_planner_model(provider),
                    messages=(
                        ProviderMessage(
                            role=ProviderMessageRole.SYSTEM,
                            content=PLANNER_REPAIR_ADVISOR_PROMPT,
                        ),
                        ProviderMessage(
                            role=ProviderMessageRole.USER,
                            content=_planner_repair_advice_prompt(
                                execution,
                                work_item,
                                failure_context,
                            ),
                        ),
                    ),
                    responseSchema=PlannerRepairAdvice.model_json_schema(by_alias=True),
                    timeoutSeconds=provider.spec.timeout_seconds,
                )
            )
            advice = PlannerRepairAdvice.model_validate(response.output)
        except (ProviderOperationError, ValidationError, ValueError) as error:
            await self._publish_runner_event(
                execution,
                "PlannerRepairAdviceFailed",
                {
                    "workItemId": str(work_item.metadata.id),
                    "message": str(error),
                },
                subject=work_item,
            )
            return None

        await self._publish_runner_event(
            execution,
            "PlannerRepairAdviceProduced",
            {
                "workItemId": str(work_item.metadata.id),
                "summary": advice.summary,
                "instructions": advice.instructions,
            },
            subject=work_item,
        )
        return advice

    async def _verify_work_items(self, execution: Execution) -> None:
        await self._verify_pending_work_items(execution)
        await self._reconcile_execution(execution)

    async def _verify_pending_work_items(self, execution: Execution) -> None:
        work_items = await self._work_items.list_by_execution(execution.metadata.id)
        verifier = VerificationController(
            work_item_repository=self._work_items,
            workspace_repository=self._workspaces,
            workspace_provider=LocalGitWorktreeProvider(_git_executable()),
            artifact_service=self._artifact_service,
            event_publisher=self._events,
        )
        for work_item in work_items:
            if work_item.status.phase == WorkItemPhase.VERIFYING:
                await verifier.verify_work_item(work_item.metadata.id)

    async def _run_reviews(self, execution: Execution) -> None:
        await self._reconcile_execution(execution)
        reviews = await self._reviews.list_by_execution(execution.metadata.id)
        pending = tuple(
            review for review in reviews if review.status.phase == ReviewPhase.PENDING
        )
        if not pending:
            await self._reconcile_execution(execution)
            return

        provider = await self._ensure_codex_provider(execution.metadata.namespace)
        runtime = CodexProvider.from_provider(
            provider,
            working_directory=self._settings.workspace_root,
        )
        for review in pending:
            await self._publish_runner_event(
                execution,
                "ReviewerRunStarted",
                {"reviewId": str(review.metadata.id)},
                subject=review,
            )
            await self._reviewer_runtime().invoke_review(
                review.metadata.id,
                provider=provider,
                runtime=runtime,
                model=_codex_model(provider),
            )
        execution = await self._executions.get(execution.metadata.id)
        await self._reconcile_execution(execution)

    async def _publish_final_workspace(self, execution: Execution) -> None:
        project = await self._projects.get(execution.spec.project_ref.id)
        workspaces = await self._workspaces.list_by_execution(execution.metadata.id)
        workspace_service = WorkspaceLifecycleService(self._workspaces)
        workspace_provider = LocalGitWorktreeProvider(_git_executable())
        for workspace in workspaces:
            if await self._workspace_already_published(execution, workspace):
                continue
            repository = _repository_for_workspace(project, workspace)
            await self._publish_runner_event(
                execution,
                "WorkspacePublishStarted",
                {
                    "workspaceId": str(workspace.metadata.id),
                    "repository": repository.id,
                    "targetPath": str(repository.path),
                },
                subject=workspace,
            )
            diff = await workspace_service.collect_workspace_diff(
                workspace.metadata.id,
                workspace_provider,
            )
            _apply_patch_to_repository(repository.path, diff.text)
            await self._publish_runner_event(
                execution,
                "WorkspacePublished",
                {
                    "workspaceId": str(workspace.metadata.id),
                    "repository": repository.id,
                    "targetPath": str(repository.path),
                    "bytes": len(diff.text.encode("utf-8")),
                },
                subject=workspace,
            )

    async def _workspace_already_published(
        self,
        execution: Execution,
        workspace: Workspace,
    ) -> bool:
        events = await self._events.list_by_execution(execution.metadata.id)
        return any(
            event.spec.event_type == "WorkspacePublished"
            and event.spec.payload.get("workspaceId") == str(workspace.metadata.id)
            for event in events
        )

    async def _ensure_runtime_catalog(self, namespace: str) -> None:
        for version in DEFAULT_LOCAL_ROLE_VERSIONS:
            await self._ensure_role(namespace, "coding", version=version)
        for capability in DEFAULT_CODING_CAPABILITIES:
            await self._ensure_capability(namespace, capability)
        await self._ensure_binding(namespace, DEFAULT_CODING_CAPABILITIES)
        provider = await self._ensure_ollama_provider(namespace)
        await self._ensure_agent(
            namespace,
            name="coder-local",
            role_name="coding",
            role_versions=DEFAULT_LOCAL_ROLE_VERSIONS,
            provider_name=provider.metadata.name,
            model=_coder_model(provider),
            capability_bindings=("local-workspace-safe",),
        )

    async def _ensure_ollama_provider(self, namespace: str) -> Provider:
        endpoint = os.environ.get("OLLAMA_HOST", DEFAULT_OLLAMA_HOST)
        runtime = OllamaProvider(endpoint=endpoint)
        health = await runtime.health()
        if health.phase != ProviderPhase.READY or not health.available_models:
            provider = await self._upsert_provider(
                namespace,
                name=DEFAULT_OLLAMA_PROVIDER_NAME,
                spec=ProviderSpec(
                    type="ollama",
                    endpoint=endpoint,
                    dataPolicy=ProviderDataPolicy(allowSourceCode=True),
                    timeoutSeconds=120,
                ),
            )
            await self._update_provider_status(provider, health.phase, health.failure)
            message = (
                health.failure.message if health.failure else "Ollama is not ready"
            )
            raise ValueError(message)

        planner_model = _env_model("MAESTRO_PLANNER_MODEL") or _env_model(
            "OLLAMA_MODEL"
        )
        coder_model = _env_model("MAESTRO_CODER_MODEL") or _env_model("OLLAMA_MODEL")
        planner_model = planner_model or health.available_models[0]
        coder_model = coder_model or planner_model
        missing = tuple(
            model
            for model in (planner_model, coder_model)
            if model not in health.available_models
        )
        if missing:
            raise ValueError(
                "Configured Ollama model is unavailable: " + ", ".join(missing)
            )

        provider = await self._upsert_provider(
            namespace,
            name=DEFAULT_OLLAMA_PROVIDER_NAME,
            spec=ProviderSpec(
                type="ollama",
                endpoint=endpoint,
                allowedModels=_unique((planner_model, coder_model)),
                dataPolicy=ProviderDataPolicy(allowSourceCode=True),
                timeoutSeconds=120,
            ),
        )
        status = ProviderStatus(
            observedGeneration=provider.metadata.generation,
            phase=ProviderPhase.READY,
            capabilities=health.capabilities,
            availableModels=_unique((planner_model, coder_model)),
            lastHealthCheckAt=provider.metadata.updated_at,
        )
        return await self._providers.update_status(
            provider.metadata.id,
            status,
            expected_resource_version=provider.metadata.resource_version,
        )

    async def _ensure_codex_provider(self, namespace: str) -> Provider:
        executable = os.environ.get("CODEX", "codex")
        model = (
            _env_model("MAESTRO_CODEX_MODEL")
            or _env_model("CODEX_MODEL")
            or DEFAULT_CODEX_MODEL
        )
        provider = await self._upsert_provider(
            namespace,
            name=DEFAULT_CODEX_PROVIDER_NAME,
            spec=ProviderSpec(
                type="codex",
                endpoint=executable,
                allowedModels=(model,),
                timeoutSeconds=120,
            ),
        )
        runtime = CodexProvider.from_provider(
            provider,
            working_directory=self._settings.workspace_root,
        )
        health = await runtime.health()
        status = ProviderStatus(
            observedGeneration=provider.metadata.generation,
            phase=health.phase,
            capabilities=health.capabilities,
            availableModels=(model,) if health.phase == ProviderPhase.READY else (),
            failure=health.failure,
            lastHealthCheckAt=provider.metadata.updated_at,
        )
        provider = await self._providers.update_status(
            provider.metadata.id,
            status,
            expected_resource_version=provider.metadata.resource_version,
        )
        if provider.status.phase != ProviderPhase.READY:
            raise ValueError(
                provider.status.failure.message
                if provider.status.failure
                else "Codex is not ready"
            )
        return provider

    async def _upsert_provider(
        self,
        namespace: str,
        *,
        name: ResourceName,
        spec: ProviderSpec,
    ) -> Provider:
        try:
            provider = await self._providers.get_by_name(namespace, name)
        except ResourceNameNotFoundError:
            return await self._providers.create(
                Provider.new(name=name, namespace=namespace, spec=spec)
            )
        if provider.spec == spec:
            return provider
        return await self._providers.update_spec(
            provider.metadata.id,
            spec,
            expected_resource_version=provider.metadata.resource_version,
        )

    async def _update_provider_status(
        self,
        provider: Provider,
        phase: ProviderPhase,
        failure: ProviderFailure | None,
    ) -> Provider:
        return await self._providers.update_status(
            provider.metadata.id,
            ProviderStatus(
                observedGeneration=provider.metadata.generation,
                phase=phase,
                capabilities=ProviderFeatureSet(
                    structuredOutput=True,
                    toolCalling=True,
                ),
                failure=failure,
            ),
            expected_resource_version=provider.metadata.resource_version,
        )

    async def _ensure_role(
        self,
        namespace: str,
        name: ResourceName,
        *,
        version: str = DEFAULT_LOCAL_ROLE_VERSION,
    ) -> Role:
        try:
            return await self._roles.get_by_name_version(namespace, name, version)
        except ResourceNameNotFoundError:
            role = Role.new(
                name=name,
                namespace=namespace,
                spec=RoleSpec(
                    version=version,
                    purpose=f"{name} role",
                    inputSchemaRef=f"{name.title()}Input/v1",
                    outputSchemaRef=f"{name.title()}Output/v1",
                    requiredCapabilities=("filesystem.read",),
                    executionPolicy=RoleExecutionPolicy(maxSteps=20),
                ),
            )
            return await self._roles.create(
                Role(
                    metadata=role.metadata,
                    spec=role.spec,
                    status=RoleStatus(
                        observedGeneration=role.metadata.generation,
                        phase=RolePhase.READY,
                        validation=RoleValidationResult(valid=True),
                    ),
                )
            )

    async def _ensure_capability(
        self,
        namespace: str,
        canonical_name: str,
    ) -> Capability:
        try:
            return await self._capabilities.get_by_canonical_name(
                namespace,
                canonical_name,
            )
        except ResourceNameNotFoundError:
            schema_name = "".join(
                part.capitalize() for part in canonical_name.split(".")
            )
            capability = Capability.new(
                name=canonical_name.replace(".", "-"),
                namespace=namespace,
                spec=CapabilitySpec(
                    canonicalName=canonical_name,
                    description=f"Local workspace capability for {canonical_name}",
                    sideEffectLevel=_capability_side_effect(canonical_name),
                    approvalPolicy=CapabilityApprovalPolicy.NONE,
                    scopes=(CapabilityScope.WORKSPACE,),
                    inputSchemaRef=f"{schema_name}Input/v1",
                    outputSchemaRef=f"{schema_name}Output/v1",
                ),
            )
            return await self._capabilities.create(
                Capability(
                    metadata=capability.metadata,
                    spec=capability.spec,
                    status=CapabilityStatus(
                        observedGeneration=capability.metadata.generation,
                        phase=CapabilityPhase.READY,
                        toolImplementations=("local-tool",),
                    ),
                )
            )

    async def _ensure_binding(
        self,
        namespace: str,
        grants: tuple[str, ...],
    ) -> CapabilityBinding:
        bindings = await self._bindings.list(ResourceSelector(namespace=namespace))
        for binding in bindings:
            if binding.metadata.name == "local-workspace-safe":
                return binding
        binding = CapabilityBinding.new(
            name="local-workspace-safe",
            namespace=namespace,
            spec=CapabilityBindingSpec(grants=grants),
        )
        return await self._bindings.create(
            CapabilityBinding(
                metadata=binding.metadata,
                spec=binding.spec,
                status=CapabilityBindingStatus(
                    observedGeneration=binding.metadata.generation,
                    phase=CapabilityBindingPhase.READY,
                ),
            )
        )

    async def _ensure_agent(
        self,
        namespace: str,
        *,
        name: ResourceName,
        role_name: ResourceName,
        role_versions: tuple[str, ...] = (DEFAULT_LOCAL_ROLE_VERSION,),
        provider_name: ResourceName,
        model: str,
        capability_bindings: tuple[ResourceName, ...] = (),
    ) -> Agent:
        desired_spec = AgentSpec(
            providerRef=AgentProviderReference(name=provider_name),
            model=model,
            supportedRoles=(
                AgentSupportedRole(name=role_name, versions=role_versions),
            ),
            capabilityBindings=tuple(
                AgentCapabilityBindingReference(name=binding)
                for binding in capability_bindings
            ),
            capacity=AgentCapacity(maxConcurrentAssignments=2),
            scheduling=AgentScheduling(priority=100),
        )
        agents = await self._agents.list(ResourceSelector(namespace=namespace))
        for agent in agents:
            if agent.metadata.name == name:
                if agent.spec != desired_spec:
                    agent = await self._agents.update_spec(
                        agent.metadata.id,
                        desired_spec,
                        expected_resource_version=agent.metadata.resource_version,
                    )
                if agent.status.phase == AgentPhase.READY:
                    return agent
                return await self._agents.update_status(
                    agent.metadata.id,
                    AgentStatus(
                        observedGeneration=agent.metadata.generation,
                        phase=AgentPhase.READY,
                        modelAvailable=True,
                    ),
                    expected_resource_version=agent.metadata.resource_version,
                )

        agent = Agent.new(
            name=name,
            namespace=namespace,
            spec=desired_spec,
        )
        return await self._agents.create(
            Agent(
                metadata=agent.metadata,
                spec=agent.spec,
                status=AgentStatus(
                    observedGeneration=agent.metadata.generation,
                    phase=AgentPhase.READY,
                    modelAvailable=True,
                ),
            )
        )

    async def _retry_role_catalog_blocked_work_items(
        self,
        execution: Execution,
    ) -> None:
        work_items = await self._work_items.list_by_execution(execution.metadata.id)
        for work_item in work_items:
            if not _is_retryable_role_catalog_block(work_item):
                continue
            try:
                await self._roles.get_by_name_version(
                    work_item.metadata.namespace,
                    work_item.spec.role_ref.name,
                    work_item.spec.role_ref.version,
                )
            except ResourceNameNotFoundError:
                continue

            status = observe_generation(
                work_item,
                work_item.status.model_copy(update={"phase": WorkItemPhase.READY}),
            )
            status = with_condition(
                work_item,
                status,
                condition_type="Ready",
                condition_status=ConditionStatus.TRUE,
                reason="Ready",
                message="WorkItem recovered after local Role catalog refresh",
            )
            status = with_condition(
                work_item,
                status,
                condition_type="Scheduled",
                condition_status=ConditionStatus.UNKNOWN,
                reason="SchedulerRetry",
                message="Retrying after local Role catalog refresh",
            )
            updated = await self._work_items.update_status(
                work_item.metadata.id,
                status,
                expected_resource_version=work_item.metadata.resource_version,
            )
            await self._publish_runner_event(
                execution,
                "WorkItemSchedulingRetry",
                {
                    "workItemId": str(updated.metadata.id),
                    "role": (
                        f"{updated.spec.role_ref.name}/{updated.spec.role_ref.version}"
                    ),
                },
                subject=updated,
            )

    async def _ensure_workspace(
        self,
        execution: Execution,
        project: Project,
    ) -> Workspace:
        existing = await self._workspaces.list_by_execution(execution.metadata.id)
        if existing:
            return existing[0]
        repository = _primary_repository(project)
        base_revision = _repository_revision(project, repository.id)
        return await self._workspaces.create(
            Workspace.new(
                name=f"execution-{execution.metadata.id.hex[:12]}-{repository.id}",
                namespace=execution.metadata.namespace,
                spec=WorkspaceSpec(
                    executionRef=WorkspaceExecutionReference(
                        id=execution.metadata.id,
                        name=execution.metadata.name,
                    ),
                    repositoryRef=repository.id,
                    providerRef=WorkspaceProviderReference(
                        name=DEFAULT_WORKSPACE_PROVIDER_NAME
                    ),
                    baseRevision=base_revision,
                    branchName=f"maestro/{execution.metadata.id.hex[:12]}",
                ),
            )
        )

    async def _attach_workspace_to_work_items(
        self,
        execution: Execution,
        workspace: Workspace,
    ) -> None:
        work_items = await self._work_items.list_by_execution(execution.metadata.id)
        for work_item in work_items:
            updates: dict[str, Any] = {}
            if work_item.spec.workspace_ref is None:
                updates["workspace_ref"] = WorkItemWorkspaceReference(
                    id=workspace.metadata.id,
                    name=workspace.metadata.name,
                )
            if (
                work_item.spec.retry_policy.max_attempts
                < execution.spec.limits.max_coding_iterations
            ):
                updates["retry_policy"] = work_item.spec.retry_policy.model_copy(
                    update={"max_attempts": execution.spec.limits.max_coding_iterations}
                )
            if not updates:
                continue
            spec = work_item.spec.model_copy(update=updates)
            await self._work_items.update_spec(
                work_item.metadata.id,
                spec,
                expected_resource_version=work_item.metadata.resource_version,
            )

    async def _release_agent_assignment(
        self,
        agent_ref: WorkItemAgentReference,
    ) -> None:
        agent = await self._agents.get(agent_ref.id)
        current_assignments = max(0, agent.status.current_assignments - 1)
        phase = AgentPhase.READY if current_assignments == 0 else agent.status.phase
        await self._agents.update_status(
            agent.metadata.id,
            observe_generation(
                agent,
                agent.status.model_copy(
                    update={
                        "phase": phase,
                        "current_assignments": current_assignments,
                    }
                ),
            ),
            expected_resource_version=agent.metadata.resource_version,
        )

    async def _has_approved_final_approval(self, execution: Execution) -> bool:
        approvals = await self._approvals.list_by_execution(execution.metadata.id)
        return any(
            approval.spec.approval_type == ApprovalType.FINAL
            and approval.status.phase == ApprovalPhase.APPROVED
            for approval in approvals
        )

    async def _ensure_approval(
        self,
        execution: Execution,
        subject: BaseResource[Any, Any],
        approval_type: ApprovalType,
    ) -> Approval:
        approvals = await self._approvals.list_by_subject(
            subject.kind,
            subject.metadata.id,
        )
        for approval in approvals:
            if (
                approval.spec.subject_ref.resource_version
                == subject.metadata.resource_version
            ):
                return approval
        approval = await self._approvals.create(
            Approval.new(
                name=f"{approval_type.value.lower()}-{subject.metadata.name}",
                namespace=execution.metadata.namespace,
                spec=ApprovalSpec(
                    executionRef=ApprovalExecutionReference(
                        id=execution.metadata.id,
                        name=execution.metadata.name,
                    ),
                    subjectRef=ApprovalSubjectReference(
                        kind=subject.kind,
                        id=subject.metadata.id,
                        name=subject.metadata.name,
                        resourceVersion=subject.metadata.resource_version,
                    ),
                    type=approval_type,
                ),
            )
        )
        await self._publish_runner_event(
            execution,
            "ApprovalRequested",
            {
                "approvalId": str(approval.metadata.id),
                "type": approval_type,
                "subjectKind": subject.kind,
            },
            subject=approval,
        )
        return approval

    async def _repository_context(self, execution: Execution) -> dict[str, Any]:
        project = await self._projects.get(execution.spec.project_ref.id)
        return {
            "repositories": tuple(
                {
                    "id": repository.id,
                    "path": str(repository.path),
                    "defaultBranch": repository.default_branch,
                    "headRevision": _repository_revision(project, repository.id),
                }
                for repository in project.spec.repositories
            )
        }

    async def _user_input_context(self, execution: Execution) -> dict[str, Any]:
        events = await self._events.list_by_execution(execution.metadata.id)
        questions: list[Any] = []
        answers: list[Any] = []
        for event in events:
            if event.spec.event_type == "PlannerQuestionsProduced":
                payload_questions = event.spec.payload.get("questions")
                if isinstance(payload_questions, list | tuple):
                    questions.extend(payload_questions)
            if event.spec.event_type == "UserInputProvided":
                payload_answers = event.spec.payload.get("answers")
                if isinstance(payload_answers, list | tuple):
                    answers.extend(payload_answers)
        if not questions and not answers:
            return {}
        return {
            "plannerQuestions": tuple(questions),
            "userAnswers": tuple(answers),
        }

    async def _previous_coding_failure_context(
        self,
        execution: Execution,
        work_item: WorkItem,
    ) -> dict[str, Any] | None:
        if work_item.status.attempt == 0:
            return None
        invocations = await self._role_invocations.list_by_execution(
            execution.metadata.id
        )
        failed_invocations = tuple(
            invocation
            for invocation in invocations
            if invocation.spec.work_item_ref is not None
            and invocation.spec.work_item_ref.id == work_item.metadata.id
            and invocation.status.phase
            in {
                RoleInvocationPhase.FAILED,
                RoleInvocationPhase.TIMED_OUT,
            }
        )
        if not failed_invocations:
            return None

        invocation = _latest_role_invocation(failed_invocations)
        condition = _latest_condition(work_item, "Coding")
        invocation_failure = invocation.status.failure
        context = {
            "workItemId": str(work_item.metadata.id),
            "name": work_item.metadata.name,
            "planWorkItemId": work_item.spec.plan_work_item_id,
            "objective": work_item.spec.objective,
            "attemptsCompleted": work_item.status.attempt,
            "conditionReason": condition.reason if condition else "",
            "conditionMessage": condition.message if condition else "",
            "invocationId": str(invocation.metadata.id),
            "invocationPhase": invocation.status.phase,
            "invocationFailureReason": (
                invocation_failure.reason if invocation_failure else ""
            ),
            "invocationFailureMessage": (
                invocation_failure.message if invocation_failure else ""
            ),
        }
        tool_failures = await self._tool_failure_context(invocation)
        if tool_failures:
            context["toolFailures"] = tool_failures
        return context

    async def _tool_failure_context(
        self,
        invocation: RoleInvocation,
    ) -> tuple[dict[str, Any], ...]:
        failures: list[dict[str, Any]] = []
        for artifact_ref in invocation.status.output_artifact_refs:
            try:
                artifact = await self._artifacts.get(artifact_ref.id)
                raw_content = await self._artifact_storage.read_bytes(artifact)
                payload = json.loads(raw_content.decode("utf-8"))
            except Exception:  # noqa: BLE001 - retry context is best-effort evidence.
                continue
            if not isinstance(payload, dict):
                continue
            status = payload.get("status")
            if status not in {"denied", "failed"}:
                continue
            failures.append(
                {
                    "toolName": payload.get("toolName", ""),
                    "status": status,
                    "message": payload.get("message", ""),
                    "arguments": payload.get("arguments", {}),
                    "output": payload.get("output", {}),
                }
            )
        return tuple(failures[-3:])

    def _execution_controller(self) -> ExecutionController:
        return ExecutionController(
            self._executions,
            plan_repository=self._plans,
            workspace_repository=self._workspaces,
            work_item_repository=self._work_items,
            artifact_repository=self._artifacts,
            review_repository=self._reviews,
            approval_repository=self._approvals,
            event_publisher=self._events,
        )

    def _plan_controller(self) -> PlanController:
        return PlanController(
            self._plans,
            approval_repository=self._approvals,
            work_item_repository=self._work_items,
            event_publisher=self._events,
        )

    def _work_item_controller(self) -> WorkItemController:
        return WorkItemController(self._work_items, event_publisher=self._events)

    def _scheduler(self) -> WorkItemScheduler:
        return WorkItemScheduler(
            work_item_repository=self._work_items,
            agent_repository=self._agents,
            role_repository=self._roles,
            provider_repository=self._providers,
            capability_repository=self._capabilities,
            capability_binding_repository=self._bindings,
            event_publisher=self._events,
        )

    def _planner_runtime(self) -> PlannerRuntime:
        return PlannerRuntime(
            execution_repository=self._executions,
            project_repository=self._projects,
            plan_repository=self._plans,
            role_invocation_repository=self._role_invocations,
            artifact_service=self._artifact_service,
            event_publisher=self._events,
        )

    def _coding_runtime(self) -> CodingRuntime:
        return CodingRuntime(
            work_item_repository=self._work_items,
            role_invocation_repository=self._role_invocations,
            artifact_service=self._artifact_service,
            tool_runtime=CodingToolRuntime(
                artifact_service=self._artifact_service,
                event_publisher=self._events,
            ),
            event_publisher=self._events,
        )

    def _reviewer_runtime(self) -> ReviewerRuntime:
        return ReviewerRuntime(
            review_repository=self._reviews,
            artifact_repository=self._artifacts,
            artifact_storage=self._artifact_storage,
            artifact_service=self._artifact_service,
            event_publisher=self._events,
        )

    async def _reconcile_execution(self, execution: Execution) -> None:
        await self._execution_controller().reconcile(_context_for(execution))

    async def _publish_runner_failure(
        self,
        execution_id: UUID,
        error: Exception,
    ) -> None:
        try:
            execution = await self._executions.get(execution_id)
        except Exception:  # noqa: BLE001 - no resource means no event can be owned.
            return
        await self._publish_runner_event(
            execution,
            "ExecutionRunFailed",
            {
                "error": type(error).__name__,
                "message": str(error),
            },
        )
        if execution.status.phase in RUNNER_FAILURE_PHASES:
            status = execution.status.model_copy(
                update={
                    "observed_generation": execution.metadata.generation,
                    "phase": ExecutionPhase.FAILED,
                    "completed_at": utc_now(),
                }
            )
            status = with_condition(
                execution,
                status,
                condition_type="Reconciled",
                condition_status=ConditionStatus.FALSE,
                reason="RunnerFailed",
                message=str(error),
            )
            await self._executions.update_status(
                execution.metadata.id,
                status,
                expected_resource_version=execution.metadata.resource_version,
            )

    async def _publish_runner_event(
        self,
        execution: Execution,
        event_type: str,
        payload: dict[str, Any],
        *,
        subject: BaseResource[Any, Any] | None = None,
    ) -> None:
        subject_ref = _resource_ref(subject or execution)
        await self._events.publish(
            EventDraft(
                type=event_type,
                producer="local-execution-runner",
                correlationId=f"runner:{event_type}:{uuid4().hex[:16]}",
                executionRef=EventExecutionReference(
                    id=execution.metadata.id,
                    name=execution.metadata.name,
                ),
                subjectRef=subject_ref,
                payload=payload,
            )
        )


def _context_for(resource: BaseResource[Any, Any]) -> ReconciliationContext:
    return ReconciliationContext(
        key=ReconcileKey(kind=resource.kind, resource_id=resource.metadata.id),
        controller_name="local-execution-runner",
        attempt=1,
        retry_policy=RetryPolicy(),
    )


def _resource_ref(resource: BaseResource[Any, Any]) -> ResourceReference:
    return ResourceReference(
        kind=resource.kind,
        id=resource.metadata.id,
        name=resource.metadata.name,
    )


def _is_retryable_role_catalog_block(work_item: WorkItem) -> bool:
    if work_item.status.phase != WorkItemPhase.BLOCKED:
        return False
    return any(
        condition.type == "Scheduled" and condition.reason == "RoleNotFound"
        for condition in work_item.status.conditions
    )


def _latest_reusable_plan(plans: Iterable[Plan]) -> Plan | None:
    plan_versions = tuple(plans)
    if not plan_versions:
        return None
    latest = max(plan_versions, key=lambda plan: plan.spec.version)
    if latest.status.phase not in {PlanPhase.WAITING_FOR_APPROVAL, PlanPhase.APPROVED}:
        return None
    return latest


def _latest_role_invocation(invocations: Iterable[RoleInvocation]) -> RoleInvocation:
    return max(
        invocations,
        key=lambda invocation: (
            invocation.status.completed_at
            or invocation.status.started_at
            or invocation.metadata.updated_at
        ),
    )


def _latest_condition(resource: BaseResource[Any, Any], condition_type: str) -> Any:
    return next(
        (
            condition
            for condition in resource.status.conditions
            if condition.type == condition_type
        ),
        None,
    )


def _planner_repair_advice_prompt(
    execution: Execution,
    work_item: WorkItem,
    failure_context: dict[str, Any],
) -> str:
    prompt = {
        "mode": "coding-repair-advice",
        "instructions": (
            "Advise the next Coding Role attempt for this same WorkItem. "
            "Preserve the approved plan and focus on the smallest concrete "
            "change that should unblock coding."
        ),
        "executionGoal": execution.spec.goal.model_dump(mode="json", by_alias=True),
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
        },
        "previousCodingFailure": failure_context,
        "outputContract": {
            "summary": "Brief diagnosis of why the previous attempt failed.",
            "instructions": (
                "Ordered concrete instructions for the next coding attempt. "
                "Do not include manual steps for the human user."
            ),
        },
    }
    return json.dumps(prompt, indent=2, sort_keys=True)


def _primary_repository(project: Project) -> Any:
    if not project.spec.repositories:
        raise ValueError("Project must define at least one repository")
    return project.spec.repositories[0]


def _repository_for_workspace(project: Project, workspace: Workspace) -> Any:
    for repository in project.spec.repositories:
        if repository.id == workspace.spec.repository_ref:
            return repository
    raise ValueError(
        f"Project repository {workspace.spec.repository_ref!r} was not found"
    )


def _repository_revision(project: Project, repository_id: str) -> str:
    for repository_status in project.status.repositories:
        if repository_status.id != repository_id:
            continue
        if repository_status.head_revision is None:
            raise ValueError(
                f"Project repository {repository_id!r} has no committed HEAD. "
                "Create an initial commit and refresh the Project before running."
            )
        return repository_status.head_revision
    raise ValueError(f"Project repository {repository_id!r} has no observed revision")


def _planner_model(provider: Provider) -> str:
    return provider.spec.allowed_models[0]


def _coder_model(provider: Provider) -> str:
    return provider.spec.allowed_models[-1]


def _codex_model(provider: Provider) -> str:
    if provider.spec.allowed_models:
        return provider.spec.allowed_models[0]
    return DEFAULT_CODEX_MODEL


def _env_model(name: str) -> str | None:
    value = os.environ.get(name)
    return value.strip() if value and value.strip() else None


def _unique(values: Iterable[str]) -> tuple[str, ...]:
    unique: list[str] = []
    for value in values:
        if value not in unique:
            unique.append(value)
    return tuple(unique)


def _git_executable() -> str:
    return shutil.which("git") or "git"


def _apply_patch_to_repository(repository_path: os.PathLike[str], patch: str) -> None:
    if not patch.strip():
        return
    if not patch.endswith("\n"):
        patch = patch + "\n"
    path = os.fspath(repository_path)
    status = _run_git(path, "status", "--porcelain")
    if status.strip():
        raise ValueError(
            f"Target repository {path} has uncommitted changes; "
            "publish refused to avoid overwriting local work"
        )
    _run_git(path, "apply", "--binary", "--whitespace=nowarn", stdin=patch)


def _run_git(
    repository_path: str,
    *args: str,
    stdin: str | None = None,
) -> str:
    completed = subprocess.run(
        (_git_executable(), "-C", repository_path, *args),
        input=stdin,
        capture_output=True,
        check=False,
        text=True,
        timeout=30,
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise ValueError(detail or "Git command failed")
    return completed.stdout


def _capability_side_effect(canonical_name: str) -> CapabilitySideEffectLevel:
    if canonical_name == "filesystem.read" or canonical_name in {
        "git.status",
        "git.diff",
    }:
        return CapabilitySideEffectLevel.READ_ONLY
    return CapabilitySideEffectLevel.MUTATING
