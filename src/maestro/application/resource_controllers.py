"""MVP resource-specific controllers."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Any, Final

from maestro.application.controllers import (
    ReconcileResult,
    ReconciliationContext,
    StatusWriter,
    observe_generation,
    with_condition,
)
from maestro.domain.agents import (
    Agent,
    AgentRepository,
    AgentStatus,
    ProviderReadinessPhase,
    ProviderReadinessSnapshot,
    evaluate_agent_readiness,
)
from maestro.domain.approvals import (
    Approval,
    ApprovalDecision,
    ApprovalPhase,
    ApprovalRepository,
    ApprovalStatus,
    ApprovalType,
)
from maestro.domain.artifacts import (
    Artifact,
    ArtifactRepository,
    ArtifactStatus,
    ArtifactStorage,
    artifact_status_from_integrity,
)
from maestro.domain.events import EventPublisher
from maestro.domain.exceptions import ResourceNameNotFoundError
from maestro.domain.executions import (
    TERMINAL_EXECUTION_PHASES,
    Execution,
    ExecutionPhase,
    ExecutionRepository,
    ExecutionStatus,
)
from maestro.domain.plans import (
    Plan,
    PlanPhase,
    PlanRepository,
    PlanStatus,
    PlanValidationResult,
    PlanWorkItemProposal,
)
from maestro.domain.projects import (
    Project,
    ProjectPhase,
    ProjectRepository,
    ProjectStatus,
)
from maestro.domain.providers import (
    ModelProvider,
    Provider,
    ProviderHealth,
    ProviderPhase,
    ProviderRepository,
    ProviderStatus,
    normalize_provider_error,
    provider_status_from_health,
)
from maestro.domain.resources import (
    BaseResource,
    ConditionStatus,
    ResourceReference,
    Status,
    utc_now,
)
from maestro.domain.reviews import (
    Review,
    ReviewPhase,
    ReviewRepository,
    ReviewStatus,
    ReviewVerdict,
)
from maestro.domain.work_items import (
    WorkItem,
    WorkItemDependencyReference,
    WorkItemExecutionReference,
    WorkItemPhase,
    WorkItemPlanReference,
    WorkItemRepository,
    WorkItemRoleReference,
    WorkItemSpec,
    WorkItemStatus,
    WorkItemVerificationSpec,
    evaluate_work_item_readiness,
)
from maestro.domain.workflows import (
    Workflow,
    WorkflowPhase,
    WorkflowRepository,
    WorkflowStatus,
    WorkflowValidationResult,
)
from maestro.domain.workspaces import (
    Workspace,
    WorkspacePhase,
    WorkspaceRepository,
    WorkspaceStatus,
)


class _Unset:
    pass


_UNSET: Final = _Unset()


class ProjectController:
    """Reconcile Project status from admitted Project configuration."""

    name = "project-controller"
    kind = "Project"

    def __init__(
        self,
        repository: ProjectRepository,
        *,
        event_publisher: EventPublisher | None = None,
    ) -> None:
        self._repository = repository
        self._writer = StatusWriter(
            repository,
            event_publisher=event_publisher,
            producer=self.name,
        )

    async def reconcile(self, context: ReconciliationContext) -> ReconcileResult:
        project = await self._repository.get(context.resource_id)

        def build(current: Project) -> ProjectStatus:
            phase = _project_phase(current)
            status = observe_generation(
                current,
                current.status.model_copy(update={"phase": phase}),
            )
            return with_condition(
                current,
                status,
                condition_type="Ready",
                condition_status=(
                    ConditionStatus.TRUE
                    if phase == ProjectPhase.READY
                    else ConditionStatus.FALSE
                ),
                reason="ProjectReady" if phase == ProjectPhase.READY else str(phase),
                message="" if phase == ProjectPhase.READY else "Project is not Ready",
            )

        await _write_if_changed(
            project,
            self._writer,
            build,
            event_type="ProjectPhaseChanged",
        )
        return ReconcileResult()


class WorkflowController:
    """Reconcile immutable Workflow definitions into Ready or Invalid status."""

    name = "workflow-controller"
    kind = "Workflow"

    def __init__(
        self,
        repository: WorkflowRepository,
        *,
        event_publisher: EventPublisher | None = None,
    ) -> None:
        self._repository = repository
        self._writer = StatusWriter(
            repository,
            event_publisher=event_publisher,
            producer=self.name,
        )

    async def reconcile(self, context: ReconciliationContext) -> ReconcileResult:
        workflow = await self._repository.get(context.resource_id)
        if workflow.status.phase == WorkflowPhase.DEPRECATED:
            return ReconcileResult()

        def build(current: Workflow) -> WorkflowStatus:
            status = WorkflowStatus(
                observedGeneration=current.metadata.generation,
                phase=WorkflowPhase.READY,
                validation=WorkflowValidationResult(valid=True),
            )
            return with_condition(
                current,
                status,
                condition_type="Ready",
                condition_status=ConditionStatus.TRUE,
                reason="ValidationPassed",
            )

        await _write_if_changed(
            workflow,
            self._writer,
            build,
            event_type="WorkflowPhaseChanged",
        )
        return ReconcileResult()


class PlanController:
    """Reconcile Plan approval state and WorkItem materialization."""

    name = "plan-controller"
    kind = "Plan"

    def __init__(
        self,
        plan_repository: PlanRepository,
        *,
        approval_repository: ApprovalRepository | None = None,
        work_item_repository: WorkItemRepository | None = None,
        event_publisher: EventPublisher | None = None,
    ) -> None:
        self._plan_repository = plan_repository
        self._approval_repository = approval_repository
        self._work_item_repository = work_item_repository
        self._writer = StatusWriter(
            plan_repository,
            event_publisher=event_publisher,
            producer=self.name,
        )

    async def reconcile(self, context: ReconciliationContext) -> ReconcileResult:
        plan = await self._plan_repository.get(context.resource_id)

        if plan.status.phase == PlanPhase.DRAFT:
            await self._mark_waiting_for_approval(plan)
            return ReconcileResult(requeue=True)

        if plan.status.phase == PlanPhase.WAITING_FOR_APPROVAL:
            await self._reconcile_approval_decision(plan)
            return ReconcileResult()

        if (
            plan.status.phase == PlanPhase.APPROVED
            and self._work_item_repository is not None
        ):
            await self._ensure_work_items(plan)

        return ReconcileResult()

    async def _mark_waiting_for_approval(self, plan: Plan) -> None:
        def build(current: Plan) -> PlanStatus:
            status = current.status.model_copy(
                update={
                    "observed_generation": current.metadata.generation,
                    "phase": PlanPhase.WAITING_FOR_APPROVAL,
                    "validation": PlanValidationResult(valid=True),
                }
            )
            return with_condition(
                current,
                status,
                condition_type="ReadyForApproval",
                condition_status=ConditionStatus.TRUE,
                reason="ValidationPassed",
            )

        await _write_if_changed(
            plan,
            self._writer,
            build,
            event_type="PlanPhaseChanged",
        )

    async def _reconcile_approval_decision(self, plan: Plan) -> None:
        if self._approval_repository is None:
            return

        approvals = await self._approval_repository.list_by_subject(
            plan.kind,
            plan.metadata.id,
        )
        decision = _approval_for_resource_version(approvals, plan)
        if decision is None:
            return

        def build(current: Plan) -> PlanStatus:
            current_decision = _approval_for_resource_version(approvals, current)
            if current_decision is None:
                return current.status

            if current_decision.status.phase == ApprovalPhase.APPROVED:
                approval_decision = _last_approval_decision(current_decision)
                return current.status.model_copy(
                    update={
                        "observed_generation": current.metadata.generation,
                        "phase": PlanPhase.APPROVED,
                        "approved_by": approval_decision.actor,
                        "approved_at": approval_decision.decided_at,
                    }
                )

            if current_decision.status.phase == ApprovalPhase.REJECTED:
                rejection_decision = _last_approval_decision(current_decision)
                return current.status.model_copy(
                    update={
                        "observed_generation": current.metadata.generation,
                        "phase": PlanPhase.REJECTED,
                        "rejected_by": rejection_decision.actor,
                        "rejected_at": rejection_decision.decided_at,
                        "rejection_reason": rejection_decision.comment,
                    }
                )

            return current.status

        await _write_if_changed(
            plan,
            self._writer,
            build,
            event_type="PlanPhaseChanged",
        )

    async def _ensure_work_items(self, plan: Plan) -> None:
        assert self._work_item_repository is not None
        existing = await self._list_or_create_plan_work_items(plan)
        by_plan_id = {
            work_item.spec.plan_work_item_id: work_item for work_item in existing
        }
        for proposal in plan.spec.work_items:
            await self._ensure_work_item_dependencies(
                plan,
                proposal,
                by_plan_id,
            )

    async def _list_or_create_plan_work_items(self, plan: Plan) -> tuple[WorkItem, ...]:
        assert self._work_item_repository is not None
        work_items: list[WorkItem] = []
        for proposal in plan.spec.work_items:
            try:
                work_item = await self._work_item_repository.get_by_plan_work_item_id(
                    plan.metadata.id,
                    proposal.id,
                )
            except ResourceNameNotFoundError:
                work_item = await self._work_item_repository.create(
                    WorkItem.new(
                        name=proposal.id,
                        namespace=plan.metadata.namespace,
                        spec=_work_item_spec_from_plan(plan, proposal),
                    )
                )
            work_items.append(work_item)
        return tuple(work_items)

    async def _ensure_work_item_dependencies(
        self,
        plan: Plan,
        proposal: PlanWorkItemProposal,
        by_plan_id: dict[str, WorkItem],
    ) -> None:
        assert self._work_item_repository is not None
        work_item = by_plan_id[proposal.id]
        dependency_refs = tuple(
            WorkItemDependencyReference(
                id=by_plan_id[dependency_id].metadata.id,
                name=by_plan_id[dependency_id].metadata.name,
            )
            for dependency_id in proposal.depends_on
        )
        if work_item.spec.depends_on == dependency_refs:
            return
        changed_spec = work_item.spec.model_copy(update={"depends_on": dependency_refs})
        updated = await self._work_item_repository.update_spec(
            work_item.metadata.id,
            changed_spec,
            expected_resource_version=work_item.metadata.resource_version,
        )
        by_plan_id[proposal.id] = updated


class WorkItemController:
    """Reconcile WorkItem readiness from dependencies and retry policy."""

    name = "work-item-controller"
    kind = "WorkItem"

    def __init__(
        self,
        repository: WorkItemRepository,
        *,
        event_publisher: EventPublisher | None = None,
    ) -> None:
        self._repository = repository
        self._writer = StatusWriter(
            repository,
            event_publisher=event_publisher,
            producer=self.name,
        )

    async def reconcile(self, context: ReconciliationContext) -> ReconcileResult:
        work_item = await self._repository.get(context.resource_id)
        if work_item.status.phase in {
            WorkItemPhase.SCHEDULED,
            WorkItemPhase.RUNNING,
            WorkItemPhase.WAITING_FOR_TOOL,
            WorkItemPhase.WAITING_FOR_APPROVAL,
            WorkItemPhase.VERIFYING,
            WorkItemPhase.REVIEWING,
            WorkItemPhase.SUCCEEDED,
            WorkItemPhase.CANCELLED,
        }:
            return ReconcileResult()

        dependencies = await self._repository.list_by_plan(work_item.spec.plan_ref.id)

        def build(current: WorkItem) -> WorkItemStatus:
            if current.status.phase == WorkItemPhase.FAILED:
                if current.status.attempt >= current.spec.retry_policy.max_attempts:
                    return with_condition(
                        current,
                        observe_generation(current, current.status),
                        condition_type="Ready",
                        condition_status=ConditionStatus.FALSE,
                        reason="RetryAttemptsExhausted",
                        message="WorkItem retry attempts are exhausted",
                    )
                return observe_generation(
                    current,
                    current.status.model_copy(update={"phase": WorkItemPhase.READY}),
                )

            decision = evaluate_work_item_readiness(current, dependencies)
            next_phase = current.status.phase
            if decision.ready:
                next_phase = WorkItemPhase.READY
            elif decision.blocked:
                next_phase = WorkItemPhase.BLOCKED

            status = observe_generation(
                current,
                current.status.model_copy(update={"phase": next_phase}),
            )
            return with_condition(
                current,
                status,
                condition_type="Ready",
                condition_status=(
                    ConditionStatus.TRUE if decision.ready else ConditionStatus.FALSE
                ),
                reason=decision.reason,
                message=decision.message,
            )

        await _write_if_changed(
            work_item,
            self._writer,
            build,
            event_type="WorkItemPhaseChanged",
        )
        return ReconcileResult()


class WorkspaceController:
    """Reconcile Workspace status when no workspace provider action is supplied."""

    name = "workspace-controller"
    kind = "Workspace"

    def __init__(
        self,
        repository: WorkspaceRepository,
        *,
        event_publisher: EventPublisher | None = None,
    ) -> None:
        self._repository = repository
        self._writer = StatusWriter(
            repository,
            event_publisher=event_publisher,
            producer=self.name,
        )

    async def reconcile(self, context: ReconciliationContext) -> ReconcileResult:
        workspace = await self._repository.get(context.resource_id)
        if workspace.status.phase != WorkspacePhase.PENDING:
            return ReconcileResult()

        def build(current: Workspace) -> WorkspaceStatus:
            return with_condition(
                current,
                observe_generation(current, current.status),
                condition_type="Ready",
                condition_status=ConditionStatus.UNKNOWN,
                reason="WaitingForWorkspaceProvider",
                message="Workspace provider action is required",
            )

        await _write_if_changed(workspace, self._writer, build)
        return ReconcileResult()


class ApprovalController:
    """Reconcile Approval expiry and pending decision Conditions."""

    name = "approval-controller"
    kind = "Approval"

    def __init__(
        self,
        repository: ApprovalRepository,
        *,
        now: Callable[[], datetime] = utc_now,
        event_publisher: EventPublisher | None = None,
    ) -> None:
        self._repository = repository
        self._now = now
        self._writer = StatusWriter(
            repository,
            event_publisher=event_publisher,
            producer=self.name,
        )

    async def reconcile(self, context: ReconciliationContext) -> ReconcileResult:
        approval = await self._repository.get(context.resource_id)
        if approval.status.phase != ApprovalPhase.PENDING:
            return ReconcileResult()

        def build(current: Approval) -> ApprovalStatus:
            if (
                current.spec.expires_at is not None
                and current.spec.expires_at <= self._now()
            ):
                return current.status.model_copy(
                    update={
                        "observed_generation": current.metadata.generation,
                        "phase": ApprovalPhase.EXPIRED,
                    }
                )
            return with_condition(
                current,
                observe_generation(current, current.status),
                condition_type="Decision",
                condition_status=ConditionStatus.UNKNOWN,
                reason="AwaitingDecision",
                message="Approval is waiting for a decision",
            )

        await _write_if_changed(
            approval,
            self._writer,
            build,
            event_type="ApprovalPhaseChanged",
        )
        return ReconcileResult()


class ReviewController:
    """Reconcile Review scheduling state without invoking a reviewer."""

    name = "review-controller"
    kind = "Review"

    def __init__(
        self,
        repository: ReviewRepository,
        *,
        event_publisher: EventPublisher | None = None,
    ) -> None:
        self._repository = repository
        self._writer = StatusWriter(
            repository,
            event_publisher=event_publisher,
            producer=self.name,
        )

    async def reconcile(self, context: ReconciliationContext) -> ReconcileResult:
        review = await self._repository.get(context.resource_id)
        if review.status.phase != ReviewPhase.PENDING:
            return ReconcileResult()

        def build(current: Review) -> ReviewStatus:
            status = current.status.model_copy(
                update={
                    "observed_generation": current.metadata.generation,
                    "phase": ReviewPhase.SCHEDULED,
                }
            )
            return with_condition(
                current,
                status,
                condition_type="Scheduled",
                condition_status=ConditionStatus.TRUE,
                reason="ReviewScheduled",
            )

        await _write_if_changed(
            review,
            self._writer,
            build,
            event_type="ReviewPhaseChanged",
        )
        return ReconcileResult()


class ArtifactController:
    """Reconcile Artifact integrity through optional storage verification."""

    name = "artifact-controller"
    kind = "Artifact"

    def __init__(
        self,
        repository: ArtifactRepository,
        *,
        storage: ArtifactStorage | None = None,
        event_publisher: EventPublisher | None = None,
    ) -> None:
        self._repository = repository
        self._storage = storage
        self._writer = StatusWriter(
            repository,
            event_publisher=event_publisher,
            producer=self.name,
        )

    async def reconcile(self, context: ReconciliationContext) -> ReconcileResult:
        artifact = await self._repository.get(context.resource_id)
        if self._storage is None:
            await self._mark_unknown(artifact)
            return ReconcileResult()

        integrity = await self._storage.verify(artifact)

        def build(current: Artifact) -> ArtifactStatus:
            return artifact_status_from_integrity(current, integrity)

        await _write_if_changed(
            artifact,
            self._writer,
            build,
            event_type="ArtifactPhaseChanged",
        )
        return ReconcileResult()

    async def _mark_unknown(self, artifact: Artifact) -> None:
        def build(current: Artifact) -> ArtifactStatus:
            return with_condition(
                current,
                observe_generation(current, current.status),
                condition_type="IntegrityVerified",
                condition_status=ConditionStatus.UNKNOWN,
                reason="StorageUnavailable",
                message="Artifact storage verifier is not configured",
            )

        await _write_if_changed(artifact, self._writer, build)


class ProviderController:
    """Reconcile Provider health when a runtime resolver is available."""

    name = "provider-controller"
    kind = "Provider"

    def __init__(
        self,
        repository: ProviderRepository,
        *,
        runtime_resolver: Callable[[Provider], ModelProvider | None] | None = None,
        event_publisher: EventPublisher | None = None,
    ) -> None:
        self._repository = repository
        self._runtime_resolver = runtime_resolver
        self._writer = StatusWriter(
            repository,
            event_publisher=event_publisher,
            producer=self.name,
        )

    async def reconcile(self, context: ReconciliationContext) -> ReconcileResult:
        provider = await self._repository.get(context.resource_id)
        runtime = self._runtime_resolver(provider) if self._runtime_resolver else None
        if runtime is None:
            await self._mark_unknown(provider)
            return ReconcileResult()

        try:
            health = await runtime.health()
            models = await runtime.list_models()
            observed = ProviderHealth(
                phase=health.phase,
                capabilities=health.capabilities,
                availableModels=models.models,
                failure=health.failure,
            )
        except Exception as error:  # noqa: BLE001 - provider boundary.
            failure = normalize_provider_error(error)
            observed = ProviderHealth(
                phase=ProviderPhase.UNAVAILABLE,
                failure=failure,
            )

        def build(current: Provider) -> ProviderStatus:
            status = provider_status_from_health(current, observed)
            return with_condition(
                current,
                status,
                condition_type="Ready",
                condition_status=(
                    ConditionStatus.TRUE
                    if status.phase == ProviderPhase.READY
                    else ConditionStatus.FALSE
                ),
                reason=str(status.phase),
                message=status.failure.message if status.failure else "",
            )

        await _write_if_changed(
            provider,
            self._writer,
            build,
            event_type="ProviderPhaseChanged",
        )
        return ReconcileResult()

    async def _mark_unknown(self, provider: Provider) -> None:
        def build(current: Provider) -> ProviderStatus:
            return with_condition(
                current,
                observe_generation(current, current.status),
                condition_type="Ready",
                condition_status=ConditionStatus.UNKNOWN,
                reason="RuntimeUnavailable",
                message="Provider runtime is not configured",
            )

        await _write_if_changed(provider, self._writer, build)


class AgentController:
    """Reconcile Agent readiness from Provider status evidence."""

    name = "agent-controller"
    kind = "Agent"

    def __init__(
        self,
        repository: AgentRepository,
        *,
        provider_repository: ProviderRepository | None = None,
        event_publisher: EventPublisher | None = None,
    ) -> None:
        self._repository = repository
        self._provider_repository = provider_repository
        self._writer = StatusWriter(
            repository,
            event_publisher=event_publisher,
            producer=self.name,
        )

    async def reconcile(self, context: ReconciliationContext) -> ReconcileResult:
        agent = await self._repository.get(context.resource_id)
        if self._provider_repository is None:
            await self._mark_unknown(agent)
            return ReconcileResult()

        provider = await self._provider_repository.get_by_name(
            agent.metadata.namespace,
            agent.spec.provider_ref.name,
        )
        decision = evaluate_agent_readiness(
            agent,
            ProviderReadinessSnapshot(
                providerRef=agent.spec.provider_ref,
                phase=ProviderReadinessPhase(provider.status.phase),
                availableModels=provider.status.available_models,
            ),
        )

        def build(current: Agent) -> AgentStatus:
            status = current.status.model_copy(
                update={
                    "observed_generation": current.metadata.generation,
                    "phase": decision.phase,
                    "model_available": decision.model_available,
                }
            )
            return with_condition(
                current,
                status,
                condition_type="Ready",
                condition_status=(
                    ConditionStatus.TRUE if decision.ready else ConditionStatus.FALSE
                ),
                reason=decision.reason,
                message=decision.message,
            )

        await _write_if_changed(
            agent,
            self._writer,
            build,
            event_type="AgentPhaseChanged",
        )
        return ReconcileResult()

    async def _mark_unknown(self, agent: Agent) -> None:
        def build(current: Agent) -> AgentStatus:
            return with_condition(
                current,
                observe_generation(current, current.status),
                condition_type="Ready",
                condition_status=ConditionStatus.UNKNOWN,
                reason="ProviderStatusUnavailable",
                message="Provider status is not configured",
            )

        await _write_if_changed(agent, self._writer, build)


class ExecutionController:
    """Reconcile Execution phase from persisted resource evidence."""

    name = "execution-controller"
    kind = "Execution"

    def __init__(
        self,
        execution_repository: ExecutionRepository,
        *,
        plan_repository: PlanRepository | None = None,
        workspace_repository: WorkspaceRepository | None = None,
        work_item_repository: WorkItemRepository | None = None,
        review_repository: ReviewRepository | None = None,
        approval_repository: ApprovalRepository | None = None,
        event_publisher: EventPublisher | None = None,
    ) -> None:
        self._execution_repository = execution_repository
        self._plan_repository = plan_repository
        self._workspace_repository = workspace_repository
        self._work_item_repository = work_item_repository
        self._review_repository = review_repository
        self._approval_repository = approval_repository
        self._writer = StatusWriter(
            execution_repository,
            event_publisher=event_publisher,
            producer=self.name,
        )

    async def reconcile(self, context: ReconciliationContext) -> ReconcileResult:
        execution = await self._execution_repository.get(context.resource_id)
        if execution.status.phase in TERMINAL_EXECUTION_PHASES:
            return ReconcileResult()
        if execution.spec.suspended:
            await self._mark_waiting(execution, "ExecutionSuspended")
            return ReconcileResult()
        if execution.spec.cancellation_requested and _can_cancel(execution):
            await self._transition_execution(
                execution,
                ExecutionPhase.CANCELLED,
                current_step="cancelled",
                completed_at=utc_now(),
                condition_reason="CancellationRequested",
            )
            return ReconcileResult()

        match execution.status.phase:
            case ExecutionPhase.DRAFT:
                await self._transition_execution(
                    execution,
                    ExecutionPhase.PLANNING,
                    current_step="planning",
                    started_at=execution.status.started_at or utc_now(),
                    condition_reason="GoalAccepted",
                )
            case ExecutionPhase.PLANNING:
                await self._reconcile_planning(execution)
            case ExecutionPhase.WAITING_FOR_PLAN_APPROVAL:
                await self._reconcile_plan_approval(execution)
            case ExecutionPhase.PREPARING_WORKSPACE:
                await self._reconcile_workspace_preparation(execution)
            case ExecutionPhase.EXECUTING:
                await self._reconcile_execution_work_items(execution)
            case ExecutionPhase.VERIFYING:
                await self._reconcile_verification(execution)
            case ExecutionPhase.REVIEWING:
                await self._reconcile_reviews(execution)
            case ExecutionPhase.WAITING_FOR_FINAL_APPROVAL:
                await self._reconcile_final_approval(execution)
            case _:
                await self._mark_waiting(execution, "AwaitingEvidence")

        return ReconcileResult()

    async def _reconcile_planning(self, execution: Execution) -> None:
        plan = await self._approved_plan(execution)
        if plan is not None:
            await self._transition_execution(
                execution,
                ExecutionPhase.WAITING_FOR_PLAN_APPROVAL,
                current_step="plan-approval",
                approved_plan_ref=_ref(plan),
                condition_reason="PlanAvailable",
            )
            return
        if await self._has_plan_waiting_for_approval(execution):
            await self._transition_execution(
                execution,
                ExecutionPhase.WAITING_FOR_PLAN_APPROVAL,
                current_step="plan-approval",
                condition_reason="PlanReadyForApproval",
            )
            return
        await self._mark_waiting(execution, "WaitingForPlan")

    async def _reconcile_plan_approval(self, execution: Execution) -> None:
        approved_plan = await self._approved_plan(execution)
        if approved_plan is None:
            await self._mark_waiting(execution, "WaitingForPlanApproval")
            return
        await self._transition_execution(
            execution,
            ExecutionPhase.PREPARING_WORKSPACE,
            current_step="prepare-workspace",
            approved_plan_ref=_ref(approved_plan),
            condition_reason="PlanApproved",
        )

    async def _reconcile_workspace_preparation(self, execution: Execution) -> None:
        if self._workspace_repository is None:
            await self._mark_waiting(execution, "WaitingForWorkspace")
            return
        workspaces = await self._workspace_repository.list_by_execution(
            execution.metadata.id
        )
        if any(
            workspace.status.phase == WorkspacePhase.FAILED for workspace in workspaces
        ):
            await self._transition_execution(
                execution,
                ExecutionPhase.FAILED,
                current_step="prepare-workspace",
                completed_at=utc_now(),
                condition_reason="WorkspaceFailed",
            )
            return
        ready = tuple(
            workspace
            for workspace in workspaces
            if workspace.status.phase
            in {WorkspacePhase.READY, WorkspacePhase.IN_USE, WorkspacePhase.DIRTY}
        )
        if ready:
            await self._transition_execution(
                execution,
                ExecutionPhase.EXECUTING,
                current_step="execute-work-items",
                workspace_refs=tuple(_ref(workspace) for workspace in ready),
                condition_reason="WorkspaceReady",
            )
            return
        await self._mark_waiting(execution, "WaitingForWorkspace")

    async def _reconcile_execution_work_items(self, execution: Execution) -> None:
        if self._work_item_repository is None:
            await self._mark_waiting(execution, "WaitingForWorkItems")
            return
        work_items = await self._work_item_repository.list_by_execution(
            execution.metadata.id
        )
        if not work_items:
            await self._mark_waiting(execution, "WaitingForWorkItems")
            return
        if any(
            work_item.status.phase == WorkItemPhase.FAILED for work_item in work_items
        ):
            await self._transition_execution(
                execution,
                ExecutionPhase.FAILED,
                current_step="execute-work-items",
                completed_at=utc_now(),
                active_work_item_refs=tuple(
                    _ref(work_item) for work_item in work_items
                ),
                condition_reason="WorkItemFailed",
            )
            return
        if all(
            work_item.status.phase == WorkItemPhase.SUCCEEDED
            for work_item in work_items
        ):
            await self._transition_execution(
                execution,
                ExecutionPhase.VERIFYING,
                current_step="verify",
                active_work_item_refs=tuple(
                    _ref(work_item) for work_item in work_items
                ),
                condition_reason="WorkItemsSucceeded",
            )
            return
        await self._mark_waiting(execution, "WaitingForWorkItems")

    async def _reconcile_verification(self, execution: Execution) -> None:
        if self._work_item_repository is None:
            await self._mark_waiting(execution, "WaitingForVerification")
            return
        work_items = await self._work_item_repository.list_by_execution(
            execution.metadata.id
        )
        if work_items and all(
            work_item.status.phase == WorkItemPhase.SUCCEEDED
            for work_item in work_items
        ):
            await self._transition_execution(
                execution,
                ExecutionPhase.REVIEWING,
                current_step="review",
                active_work_item_refs=tuple(
                    _ref(work_item) for work_item in work_items
                ),
                condition_reason="VerificationEvidenceReady",
            )
            return
        await self._mark_waiting(execution, "WaitingForVerification")

    async def _reconcile_reviews(self, execution: Execution) -> None:
        if self._review_repository is None:
            await self._mark_waiting(execution, "WaitingForReview")
            return
        reviews = await self._review_repository.list_by_execution(execution.metadata.id)
        completed = tuple(
            review for review in reviews if review.status.phase == ReviewPhase.COMPLETED
        )
        if not completed:
            await self._mark_waiting(execution, "WaitingForReview")
            return
        latest = completed[-1]
        if latest.status.verdict in {
            ReviewVerdict.APPROVE,
            ReviewVerdict.NEEDS_HUMAN_DECISION,
        }:
            await self._transition_execution(
                execution,
                ExecutionPhase.WAITING_FOR_FINAL_APPROVAL,
                current_step="final-approval",
                condition_reason="ReviewCompleted",
            )
            return
        if latest.status.verdict == ReviewVerdict.REQUEST_CHANGES:
            await self._transition_execution(
                execution,
                ExecutionPhase.EXECUTING,
                current_step="execute-work-items",
                condition_reason="ReviewRequestedChanges",
            )
            return
        await self._transition_execution(
            execution,
            ExecutionPhase.FAILED,
            current_step="review",
            completed_at=utc_now(),
            condition_reason="ReviewUnableToReview",
        )

    async def _reconcile_final_approval(self, execution: Execution) -> None:
        if self._approval_repository is None:
            await self._mark_waiting(execution, "WaitingForFinalApproval")
            return
        approvals = await self._approval_repository.list_by_execution(
            execution.metadata.id
        )
        final_approvals = tuple(
            approval
            for approval in approvals
            if approval.spec.approval_type == ApprovalType.FINAL
        )
        if any(
            approval.status.phase == ApprovalPhase.APPROVED
            for approval in final_approvals
        ):
            await self._transition_execution(
                execution,
                ExecutionPhase.COMPLETED,
                current_step="completed",
                completed_at=utc_now(),
                condition_reason="FinalApprovalGranted",
            )
            return
        if any(
            approval.status.phase == ApprovalPhase.REJECTED
            for approval in final_approvals
        ):
            await self._transition_execution(
                execution,
                ExecutionPhase.EXECUTING,
                current_step="execute-work-items",
                condition_reason="FinalApprovalRejected",
            )
            return
        await self._mark_waiting(execution, "WaitingForFinalApproval")

    async def _approved_plan(self, execution: Execution) -> Plan | None:
        if self._plan_repository is None:
            return None
        return await self._plan_repository.get_approved_for_execution(
            execution.metadata.id
        )

    async def _has_plan_waiting_for_approval(self, execution: Execution) -> bool:
        if self._plan_repository is None:
            return False
        plans = await self._plan_repository.list_by_execution(execution.metadata.id)
        return any(
            plan.status.phase == PlanPhase.WAITING_FOR_APPROVAL for plan in plans
        )

    async def _transition_execution(
        self,
        execution: Execution,
        phase: ExecutionPhase,
        *,
        current_step: str | None,
        condition_reason: str,
        approved_plan_ref: ResourceReference | None | _Unset = _UNSET,
        active_work_item_refs: tuple[ResourceReference, ...] | _Unset = _UNSET,
        workspace_refs: tuple[ResourceReference, ...] | _Unset = _UNSET,
        started_at: datetime | None | _Unset = _UNSET,
        completed_at: datetime | None | _Unset = _UNSET,
    ) -> None:
        def build(current: Execution) -> ExecutionStatus:
            updates: dict[str, Any] = {
                "observed_generation": current.metadata.generation,
                "phase": phase,
                "current_step": current_step,
            }
            if approved_plan_ref is not _UNSET:
                updates["approved_plan_ref"] = approved_plan_ref
            if active_work_item_refs is not _UNSET:
                updates["active_work_item_refs"] = active_work_item_refs
            if workspace_refs is not _UNSET:
                updates["workspace_refs"] = workspace_refs
            if started_at is not _UNSET:
                updates["started_at"] = started_at
            if completed_at is not _UNSET:
                updates["completed_at"] = completed_at

            status = current.status.model_copy(update=updates)
            return with_condition(
                current,
                status,
                condition_type="Reconciled",
                condition_status=ConditionStatus.TRUE,
                reason=condition_reason,
            )

        await _write_if_changed(
            execution,
            self._writer,
            build,
            event_type="ExecutionPhaseChanged",
        )

    async def _mark_waiting(self, execution: Execution, reason: str) -> None:
        def build(current: Execution) -> ExecutionStatus:
            return with_condition(
                current,
                observe_generation(current, current.status),
                condition_type="Reconciled",
                condition_status=ConditionStatus.UNKNOWN,
                reason=reason,
            )

        await _write_if_changed(execution, self._writer, build)


def _project_phase(project: Project) -> ProjectPhase:
    if project.spec.archived:
        return ProjectPhase.ARCHIVED
    if not project.spec.repositories:
        return ProjectPhase.READY
    if len(project.status.repositories) < len(project.spec.repositories):
        return ProjectPhase.VALIDATING
    if all(
        status.reachable and status.git_repository
        for status in project.status.repositories
    ):
        return ProjectPhase.READY
    return ProjectPhase.ERROR


def _work_item_spec_from_plan(
    plan: Plan,
    proposal: PlanWorkItemProposal,
) -> WorkItemSpec:
    return WorkItemSpec(
        executionRef=WorkItemExecutionReference(
            id=plan.spec.execution_ref.id,
            name=plan.spec.execution_ref.name,
        ),
        planRef=WorkItemPlanReference(
            id=plan.metadata.id,
            name=plan.metadata.name,
            version=plan.spec.version,
        ),
        planWorkItemId=proposal.id,
        roleRef=WorkItemRoleReference(
            name=proposal.role_ref.name,
            version=proposal.role_ref.version,
        ),
        repositoryRef=proposal.repository_ref,
        objective=proposal.objective,
        contextRefs=proposal.context_refs,
        constraints=proposal.constraints,
        acceptanceCriteria=proposal.acceptance_criteria,
        verification=WorkItemVerificationSpec(commands=proposal.verification.commands),
        requestedCapabilities=proposal.requested_capabilities,
    )


async def _write_if_changed[
    ResourceT: BaseResource[Any, Any],
    StatusT: Status,
](
    resource: ResourceT,
    writer: StatusWriter[ResourceT, Any, StatusT],
    build: Callable[[ResourceT], StatusT],
    *,
    event_type: str | None = None,
) -> ResourceT:
    desired = build(resource)
    if desired == resource.status:
        return resource
    return await writer.update_status(
        resource.metadata.id,
        build,
        event_type=event_type,
    )


def _approval_for_resource_version(
    approvals: tuple[Approval, ...],
    resource: BaseResource[Any, Any],
) -> Approval | None:
    for approval in approvals:
        if (
            approval.spec.subject_ref.kind == resource.kind
            and approval.spec.subject_ref.id == resource.metadata.id
            and approval.spec.subject_ref.resource_version
            == resource.metadata.resource_version
        ):
            return approval
    return None


def _last_approval_decision(approval: Approval) -> ApprovalDecision:
    return approval.status.decisions[-1]


def _ref(resource: BaseResource[Any, Any]) -> ResourceReference:
    return ResourceReference(
        kind=resource.kind,
        id=resource.metadata.id,
        name=resource.metadata.name,
    )


def _can_cancel(execution: Execution) -> bool:
    return execution.status.phase in {
        ExecutionPhase.WAITING_FOR_USER_INPUT,
        ExecutionPhase.WAITING_FOR_PLAN_APPROVAL,
        ExecutionPhase.EXECUTING,
        ExecutionPhase.WAITING_FOR_FINAL_APPROVAL,
    }
