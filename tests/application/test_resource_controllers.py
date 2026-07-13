"""Tests for MVP resource-specific controllers."""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from maestro.application.controllers import (
    ReconcileKey,
    ReconciliationContext,
    RetryPolicy,
)
from maestro.application.resource_controllers import (
    AgentController,
    ApprovalController,
    ArtifactController,
    ExecutionController,
    PlanController,
    ProjectController,
    ProviderController,
    ReviewController,
    WorkflowController,
    WorkItemController,
    WorkspaceController,
)
from maestro.domain.agents import (
    Agent,
    AgentCapacity,
    AgentPhase,
    AgentProviderReference,
    AgentScheduling,
    AgentSpec,
    AgentSupportedRole,
)
from maestro.domain.approvals import (
    Approval,
    ApprovalDecision,
    ApprovalDecisionValue,
    ApprovalExecutionReference,
    ApprovalPhase,
    ApprovalSpec,
    ApprovalSubjectReference,
    ApprovalType,
    record_approval_decision,
)
from maestro.domain.artifacts import (
    Artifact,
    ArtifactExecutionReference,
    ArtifactIntegrityResult,
    ArtifactPhase,
    ArtifactProducer,
    ArtifactSpec,
    ArtifactStorageMetadata,
    ArtifactStorageWriteResult,
    ArtifactType,
    ArtifactWorkItemReference,
)
from maestro.domain.executions import (
    Execution,
    ExecutionLimits,
    ExecutionPhase,
    ExecutionSpec,
    ExecutionStatus,
    ExecutionWorkflowReference,
    Goal,
    ProjectReference,
)
from maestro.domain.plans import (
    Plan,
    PlanExecutionReference,
    PlanPhase,
    PlanRoleReference,
    PlanSpec,
    PlanStatus,
    PlanValidationResult,
    PlanWorkItemProposal,
)
from maestro.domain.projects import (
    AgentReference,
    Project,
    ProjectPhase,
    ProjectRoleBinding,
    ProjectSpec,
    WorkflowReference,
)
from maestro.domain.providers import (
    Provider,
    ProviderDataPolicy,
    ProviderFeatureSet,
    ProviderHealth,
    ProviderModelList,
    ProviderPhase,
    ProviderSpec,
    StructuredGenerationRequest,
    StructuredGenerationResult,
    ToolLoopRequest,
    ToolLoopResult,
)
from maestro.domain.resources import BaseResource, Condition, ConditionStatus, utc_now
from maestro.domain.reviews import (
    Review,
    ReviewArtifactReference,
    ReviewExecutionReference,
    ReviewFinding,
    ReviewFindingCategory,
    ReviewFindingSeverity,
    ReviewPhase,
    ReviewRoleReference,
    ReviewSpec,
    ReviewStatus,
    ReviewVerdict,
    ReviewWorkItemReference,
)
from maestro.domain.work_items import (
    WorkItem,
    WorkItemDependencyReference,
    WorkItemExecutionReference,
    WorkItemPhase,
    WorkItemPlanReference,
    WorkItemRetryPolicy,
    WorkItemRoleReference,
    WorkItemSpec,
    WorkItemStatus,
    WorkItemVerificationSpec,
)
from maestro.domain.workflows import (
    TerminalOutcome,
    Workflow,
    WorkflowPhase,
    WorkflowRoleReference,
    WorkflowRoleStep,
    WorkflowSpec,
    WorkflowTerminalStep,
)
from maestro.domain.workspaces import (
    Workspace,
    WorkspaceExecutionReference,
    WorkspacePhase,
    WorkspaceProviderReference,
    WorkspaceSpec,
    WorkspaceStatus,
)
from maestro.infrastructure.persistence import (
    SQLiteAgentRepository,
    SQLiteApprovalRepository,
    SQLiteArtifactRepository,
    SQLiteExecutionRepository,
    SQLitePlanRepository,
    SQLiteProjectRepository,
    SQLiteProviderRepository,
    SQLiteReviewRepository,
    SQLiteWorkflowRepository,
    SQLiteWorkItemRepository,
    SQLiteWorkspaceRepository,
)


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


def execution_spec(project_id: UUID | None = None) -> ExecutionSpec:
    return ExecutionSpec(
        projectRef=ProjectReference(id=project_id or uuid4(), name="tour-manager"),
        goal=Goal(summary="Add health endpoint"),
        workflowRef=ExecutionWorkflowReference(
            name="software-delivery",
            version="v1alpha1",
        ),
        requestedRoles=("planner", "coding", "reviewer"),
    )


def execution(project_id: UUID | None = None) -> Execution:
    return Execution.new(name="add-health-endpoint", spec=execution_spec(project_id))


def plan_spec(
    execution_id: UUID,
    *,
    execution_name: str = "add-health-endpoint",
) -> PlanSpec:
    return PlanSpec(
        executionRef=PlanExecutionReference(id=execution_id, name=execution_name),
        version=1,
        summary="Implement health endpoint",
        workItems=(
            PlanWorkItemProposal(
                id="add-health",
                title="Add health endpoint",
                roleRef=PlanRoleReference(name="coding", version="v1alpha1"),
                repositoryRef="backend",
                objective="Implement GET /health",
                acceptanceCriteria=("GET /health returns 200",),
                requestedCapabilities=("filesystem.read", "filesystem.write"),
            ),
            PlanWorkItemProposal(
                id="update-tests",
                title="Update tests",
                roleRef=PlanRoleReference(name="coding", version="v1alpha1"),
                repositoryRef="backend",
                objective="Add tests for GET /health",
                acceptanceCriteria=("Tests cover GET /health",),
                dependsOn=("add-health",),
                requestedCapabilities=("filesystem.read", "filesystem.write"),
            ),
        ),
    )


def work_item_spec(
    execution_id: UUID,
    plan_id: UUID,
    *,
    plan_work_item_id: str = "add-health",
    depends_on: tuple[WorkItemDependencyReference, ...] = (),
    max_attempts: int = 2,
) -> WorkItemSpec:
    return WorkItemSpec(
        executionRef=WorkItemExecutionReference(
            id=execution_id,
            name="add-health-endpoint",
        ),
        planRef=WorkItemPlanReference(
            id=plan_id,
            name="plan-1",
            version=1,
        ),
        planWorkItemId=plan_work_item_id,
        roleRef=WorkItemRoleReference(name="coding", version="v1alpha1"),
        repositoryRef="backend",
        objective="Implement GET /health",
        dependsOn=depends_on,
        acceptanceCriteria=("GET /health returns 200",),
        verification=WorkItemVerificationSpec(commands=()),
        requestedCapabilities=("filesystem.read", "filesystem.write"),
        retryPolicy=WorkItemRetryPolicy(maxAttempts=max_attempts),
    )


def approval_spec(
    execution_id: UUID,
    subject: BaseResource[Any, Any],
    *,
    approval_type: ApprovalType = ApprovalType.PLAN,
    expires_at: datetime | None = None,
) -> ApprovalSpec:
    return ApprovalSpec(
        executionRef=ApprovalExecutionReference(
            id=execution_id,
            name="add-health-endpoint",
        ),
        subjectRef=ApprovalSubjectReference(
            kind=subject.kind,
            id=subject.metadata.id,
            name=subject.metadata.name,
            resourceVersion=subject.metadata.resource_version,
        ),
        type=approval_type,
        expiresAt=expires_at,
    )


def approve_decision(actor: str = "sashka") -> ApprovalDecision:
    return ApprovalDecision(
        actor=actor,
        decision=ApprovalDecisionValue.APPROVE,
        requestSource="test",
    )


async def approve(
    repository: SQLiteApprovalRepository,
    approval: Approval,
) -> Approval:
    decided = record_approval_decision(
        approval,
        approve_decision(),
        expected_resource_version=approval.metadata.resource_version,
    )
    return await repository.update_status(
        approval.metadata.id,
        decided.status,
        expected_resource_version=approval.metadata.resource_version,
    )


async def approve_plan_status(repository: SQLitePlanRepository, plan: Plan) -> Plan:
    waiting = await repository.update_status(
        plan.metadata.id,
        PlanStatus(
            observedGeneration=plan.metadata.generation,
            phase=PlanPhase.WAITING_FOR_APPROVAL,
            validation=PlanValidationResult(valid=True),
        ),
        expected_resource_version=plan.metadata.resource_version,
    )
    return await repository.update_status(
        waiting.metadata.id,
        PlanStatus(
            observedGeneration=waiting.metadata.generation,
            phase=PlanPhase.APPROVED,
            validation=PlanValidationResult(valid=True),
            approvedBy="sashka",
            approvedAt=utc_now(),
        ),
        expected_resource_version=waiting.metadata.resource_version,
    )


async def move_work_item(
    repository: SQLiteWorkItemRepository,
    work_item: WorkItem,
    phase: WorkItemPhase,
    *,
    attempt: int = 0,
) -> WorkItem:
    return await repository.update_status(
        work_item.metadata.id,
        WorkItemStatus(
            observedGeneration=work_item.metadata.generation,
            phase=phase,
            attempt=attempt,
        ),
        expected_resource_version=work_item.metadata.resource_version,
    )


async def succeed_work_item(
    repository: SQLiteWorkItemRepository,
    work_item: WorkItem,
) -> WorkItem:
    ready = await move_work_item(repository, work_item, WorkItemPhase.READY)
    scheduled = await move_work_item(
        repository,
        ready,
        WorkItemPhase.SCHEDULED,
        attempt=1,
    )
    running = await move_work_item(
        repository,
        scheduled,
        WorkItemPhase.RUNNING,
        attempt=1,
    )
    verifying = await move_work_item(
        repository,
        running,
        WorkItemPhase.VERIFYING,
        attempt=1,
    )
    return await move_work_item(
        repository,
        verifying,
        WorkItemPhase.SUCCEEDED,
        attempt=1,
    )


async def ready_workspace(
    repository: SQLiteWorkspaceRepository,
    workspace: Workspace,
    path: Path,
) -> Workspace:
    preparing = await repository.update_status(
        workspace.metadata.id,
        WorkspaceStatus(phase=WorkspacePhase.PREPARING),
        expected_resource_version=workspace.metadata.resource_version,
    )
    return await repository.update_status(
        preparing.metadata.id,
        WorkspaceStatus(
            phase=WorkspacePhase.READY,
            path=path,
            observedRevision="abc123",
        ),
        expected_resource_version=preparing.metadata.resource_version,
    )


def workflow() -> Workflow:
    return Workflow.new(
        name="software-delivery",
        spec=WorkflowSpec(
            version="v1alpha1",
            entrypoint="planning",
            steps=(
                WorkflowRoleStep(
                    id="planning",
                    roleRef=WorkflowRoleReference(
                        name="planner",
                        version="v1alpha1",
                    ),
                    onSuccess="completed",
                    maxAttempts=1,
                ),
                WorkflowTerminalStep(
                    id="completed",
                    outcome=TerminalOutcome.SUCCESS,
                ),
            ),
        ),
    )


def project(repo_path: Path) -> Project:
    return Project.new(
        name="tour-manager",
        spec=ProjectSpec(
            description="Test project",
            workflowRef=WorkflowReference(
                name="software-delivery",
                version="v1alpha1",
            ),
            roleBindings={
                "planner": ProjectRoleBinding(
                    agentRef=AgentReference(name="planner-local")
                )
            },
        ),
    )


def workspace_resource(execution_id: UUID) -> Workspace:
    return Workspace.new(
        name="execution-backend",
        spec=WorkspaceSpec(
            executionRef=WorkspaceExecutionReference(
                id=execution_id,
                name="add-health-endpoint",
            ),
            repositoryRef="backend",
            providerRef=WorkspaceProviderReference(name="local-git-worktree"),
            baseRevision="main",
            branchName="maestro/execution-123",
        ),
    )


def review_resource(execution_id: UUID, work_item_id: UUID) -> Review:
    artifact_id = uuid4()
    return Review.new(
        name="review-1",
        spec=ReviewSpec(
            executionRef=ReviewExecutionReference(
                id=execution_id,
                name="add-health-endpoint",
            ),
            workItemRef=ReviewWorkItemReference(id=work_item_id, name="add-health"),
            reviewerRoleRef=ReviewRoleReference(name="reviewer", version="v1alpha1"),
            subjectRefs=(
                ReviewArtifactReference(
                    id=artifact_id,
                    name="git-diff",
                    resourceVersion=1,
                ),
            ),
            acceptanceCriteria=("GET /health returns 200",),
        ),
    )


def checksum(content: bytes = b"diff\n") -> str:
    return hashlib.sha256(content).hexdigest()


def artifact_resource(execution_id: UUID) -> Artifact:
    return Artifact.new(
        name="git-diff",
        spec=ArtifactSpec(
            executionRef=ArtifactExecutionReference(
                id=execution_id,
                name="add-health-endpoint",
            ),
            workItemRef=ArtifactWorkItemReference(id=uuid4(), name="add-health"),
            type=ArtifactType.GIT_DIFF,
            mediaType="text/x-diff",
            storage=ArtifactStorageMetadata(uri="file:///tmp/artifacts/diff.patch"),
            sha256=checksum(),
            sizeBytes=len(b"diff\n"),
            producer=ArtifactProducer(subsystem="test"),
        ),
    )


@dataclass(frozen=True)
class ReviewWorkflowHarness:
    executions: SQLiteExecutionRepository
    work_items: SQLiteWorkItemRepository
    artifacts: SQLiteArtifactRepository
    reviews: SQLiteReviewRepository
    approvals: SQLiteApprovalRepository
    controller: ExecutionController
    execution: Execution
    work_item: WorkItem

    def close(self) -> None:
        self.executions.close()
        self.work_items.close()
        self.artifacts.close()
        self.reviews.close()
        self.approvals.close()


async def review_workflow_harness(
    *,
    max_review_iterations: int = 2,
    review_iteration: int = 0,
) -> ReviewWorkflowHarness:
    executions = SQLiteExecutionRepository(":memory:")
    work_items = SQLiteWorkItemRepository(":memory:")
    artifacts = SQLiteArtifactRepository(":memory:")
    reviews = SQLiteReviewRepository(":memory:")
    approvals = SQLiteApprovalRepository(":memory:")
    spec = execution_spec().model_copy(
        update={
            "limits": ExecutionLimits(maxReviewIterations=max_review_iterations),
        }
    )
    created_execution = await executions.create(
        Execution.new(name="add-health-endpoint", spec=spec)
    )
    work_item = await work_items.create(
        WorkItem.new(
            name="add-health",
            spec=work_item_spec(
                created_execution.metadata.id,
                uuid4(),
            ),
        )
    )
    succeeded = await succeed_work_item(work_items, work_item)
    await create_review_artifacts(artifacts, created_execution, succeeded)

    reviewing = created_execution
    for phase in (
        ExecutionPhase.PLANNING,
        ExecutionPhase.WAITING_FOR_PLAN_APPROVAL,
        ExecutionPhase.PREPARING_WORKSPACE,
        ExecutionPhase.EXECUTING,
        ExecutionPhase.VERIFYING,
        ExecutionPhase.REVIEWING,
    ):
        updates: dict[str, Any] = {"phase": phase}
        if phase == ExecutionPhase.REVIEWING:
            updates["iteration"] = reviewing.status.iteration.model_copy(
                update={"review": review_iteration}
            )
        reviewing = await executions.update_status(
            reviewing.metadata.id,
            reviewing.status.model_copy(update=updates),
            expected_resource_version=reviewing.metadata.resource_version,
        )

    controller = ExecutionController(
        executions,
        work_item_repository=work_items,
        artifact_repository=artifacts,
        review_repository=reviews,
        approval_repository=approvals,
    )
    return ReviewWorkflowHarness(
        executions=executions,
        work_items=work_items,
        artifacts=artifacts,
        reviews=reviews,
        approvals=approvals,
        controller=controller,
        execution=reviewing,
        work_item=succeeded,
    )


async def create_review_artifacts(
    repository: SQLiteArtifactRepository,
    execution: Execution,
    work_item: WorkItem,
    *,
    suffix: str = "initial",
) -> tuple[Artifact, ...]:
    artifact_specs = (
        (ArtifactType.GIT_DIFF, "text/x-diff", b"diff\n"),
        (ArtifactType.TOOL_LOG, "application/json", b'{"tool": "run-command"}\n'),
        (ArtifactType.VERIFICATION_REPORT, "application/json", b'{"ok": true}\n'),
    )
    created: list[Artifact] = []
    for artifact_type, media_type, content in artifact_specs:
        name = f"{work_item.spec.plan_work_item_id}-{artifact_type}-{suffix}"[:63]
        artifact = await repository.create(
            Artifact.new(
                name=name,
                spec=ArtifactSpec(
                    executionRef=ArtifactExecutionReference(
                        id=execution.metadata.id,
                        name=execution.metadata.name,
                    ),
                    workItemRef=ArtifactWorkItemReference(
                        id=work_item.metadata.id,
                        name=work_item.metadata.name,
                    ),
                    type=artifact_type,
                    mediaType=media_type,
                    storage=ArtifactStorageMetadata(
                        uri=f"file:///tmp/artifacts/{name}"
                    ),
                    sha256=checksum(content),
                    sizeBytes=len(content),
                    producer=ArtifactProducer(subsystem="test"),
                ),
            )
        )
        created.append(artifact)
    return tuple(created)


def blocking_finding(
    issue: str = "Health endpoint is missing coverage",
) -> ReviewFinding:
    return ReviewFinding(
        id="fix-health-test",
        severity=ReviewFindingSeverity.HIGH,
        category=ReviewFindingCategory.TESTS,
        file="tests/test_health.py",
        line=12,
        issue=issue,
        evidence="The verification report does not show endpoint coverage.",
        suggestedFix="Add the missing test and rerun verification.",
    )


async def complete_review(
    repository: SQLiteReviewRepository,
    review: Review,
    verdict: ReviewVerdict,
    *,
    blocking_findings: tuple[ReviewFinding, ...] = (),
    missing_evidence: tuple[str, ...] = (),
) -> Review:
    return await repository.update_status(
        review.metadata.id,
        ReviewStatus(
            observedGeneration=review.metadata.generation,
            phase=ReviewPhase.COMPLETED,
            verdict=verdict,
            summary="Review completed",
            blockingFindings=blocking_findings,
            missingEvidence=missing_evidence,
            completedAt=utc_now(),
        ),
        expected_resource_version=review.metadata.resource_version,
    )


async def fail_review(
    repository: SQLiteReviewRepository,
    review: Review,
) -> Review:
    return await repository.update_status(
        review.metadata.id,
        ReviewStatus(
            observedGeneration=review.metadata.generation,
            phase=ReviewPhase.FAILED,
            failureMessage="Reviewer provider failed",
        ),
        expected_resource_version=review.metadata.resource_version,
    )


def provider_resource() -> Provider:
    return Provider.new(
        name="ollama-local",
        spec=ProviderSpec(
            type="ollama",
            endpoint="http://127.0.0.1:11434",
            allowedModels=("qwen3:14b", "qwen2.5-coder:14b"),
            dataPolicy=ProviderDataPolicy(allowSourceCode=True),
        ),
    )


def agent_resource() -> Agent:
    return Agent.new(
        name="coder-local",
        spec=AgentSpec(
            providerRef=AgentProviderReference(name="ollama-local"),
            model="qwen2.5-coder:14b",
            supportedRoles=(AgentSupportedRole(name="coding", versions=("v1alpha1",)),),
            capacity=AgentCapacity(maxConcurrentAssignments=2),
            scheduling=AgentScheduling(priority=100),
        ),
    )


class ReadyRuntime:
    async def health(self) -> ProviderHealth:
        return ProviderHealth(
            phase=ProviderPhase.READY,
            capabilities=ProviderFeatureSet(structuredOutput=True),
        )

    async def list_models(self) -> ProviderModelList:
        return ProviderModelList(models=("qwen2.5-coder:14b",))

    async def generate_structured(
        self,
        request: StructuredGenerationRequest,
    ) -> StructuredGenerationResult:
        return StructuredGenerationResult(model=request.model, output={})

    async def run_tool_loop(self, request: ToolLoopRequest) -> ToolLoopResult:
        return ToolLoopResult(model=request.model, output={})


class MatchingStorage:
    async def write_bytes(
        self,
        relative_path: Path,
        content: bytes,
    ) -> ArtifactStorageWriteResult:
        return ArtifactStorageWriteResult(
            storage=ArtifactStorageMetadata(uri=f"file:///{relative_path}"),
            sha256=hashlib.sha256(content).hexdigest(),
            sizeBytes=len(content),
        )

    async def read_bytes(self, artifact: Artifact) -> bytes:
        return b"diff\n"

    async def verify(self, artifact: Artifact) -> ArtifactIntegrityResult:
        return ArtifactIntegrityResult(
            exists=True,
            sha256=artifact.spec.sha256,
            sizeBytes=artifact.spec.size_bytes,
        )


def test_plan_controller_approves_and_materializes_work_items_once() -> None:
    async def scenario() -> None:
        plan_repository = SQLitePlanRepository(":memory:")
        approval_repository = SQLiteApprovalRepository(":memory:")
        work_item_repository = SQLiteWorkItemRepository(":memory:")
        execution_id = uuid4()
        plan = await plan_repository.create(
            Plan.new(name="plan-1", spec=plan_spec(execution_id))
        )
        controller = PlanController(
            plan_repository,
            approval_repository=approval_repository,
            work_item_repository=work_item_repository,
        )

        result = await controller.reconcile(context_for(plan))
        waiting_plan = await plan_repository.get(plan.metadata.id)

        assert result.requeue is True
        assert waiting_plan.status.phase == PlanPhase.WAITING_FOR_APPROVAL
        assert condition(waiting_plan, "ReadyForApproval").status is (
            ConditionStatus.TRUE
        )

        approval = await approval_repository.create(
            Approval.new(
                name="plan-approval",
                spec=approval_spec(execution_id, waiting_plan),
            )
        )
        await approve(approval_repository, approval)

        await controller.reconcile(context_for(waiting_plan))
        approved = await plan_repository.get(plan.metadata.id)
        assert approved.status.phase == PlanPhase.APPROVED

        await controller.reconcile(context_for(approved))
        await controller.reconcile(context_for(approved))

        work_items = await work_item_repository.list_by_plan(approved.metadata.id)
        by_id = {item.spec.plan_work_item_id: item for item in work_items}

        assert sorted(by_id) == ["add-health", "update-tests"]
        assert len(work_items) == 2
        assert by_id["update-tests"].spec.depends_on == (
            WorkItemDependencyReference(
                id=by_id["add-health"].metadata.id,
                name=by_id["add-health"].metadata.name,
            ),
        )

        plan_repository.close()
        approval_repository.close()
        work_item_repository.close()

    asyncio.run(scenario())


def test_work_item_controller_sets_readiness_and_preserves_exhausted_failures() -> None:
    async def scenario() -> None:
        repository = SQLiteWorkItemRepository(":memory:")
        execution_id = uuid4()
        plan_id = uuid4()
        dependency = await repository.create(
            WorkItem.new(
                name="add-health",
                spec=work_item_spec(
                    execution_id,
                    plan_id,
                    plan_work_item_id="add-health",
                ),
            )
        )
        dependent = await repository.create(
            WorkItem.new(
                name="update-tests",
                spec=work_item_spec(
                    execution_id,
                    plan_id,
                    plan_work_item_id="update-tests",
                    depends_on=(
                        WorkItemDependencyReference(
                            id=dependency.metadata.id,
                            name=dependency.metadata.name,
                        ),
                    ),
                ),
            )
        )
        controller = WorkItemController(repository)

        await controller.reconcile(context_for(dependent))
        blocked = await repository.get(dependent.metadata.id)
        assert blocked.status.phase == WorkItemPhase.PENDING
        assert condition(blocked, "Ready").reason == "WaitingForDependencies"

        await succeed_work_item(repository, dependency)
        await controller.reconcile(context_for(blocked))
        ready = await repository.get(dependent.metadata.id)
        assert ready.status.phase == WorkItemPhase.READY
        assert condition(ready, "Ready").status is ConditionStatus.TRUE

        failed = await repository.create(
            WorkItem.new(
                name="maxed-out",
                spec=work_item_spec(
                    execution_id,
                    plan_id,
                    plan_work_item_id="maxed-out",
                    max_attempts=1,
                ),
            )
        )
        failed = await move_work_item(repository, failed, WorkItemPhase.READY)
        failed = await move_work_item(
            repository,
            failed,
            WorkItemPhase.SCHEDULED,
            attempt=1,
        )
        failed = await move_work_item(
            repository,
            failed,
            WorkItemPhase.RUNNING,
            attempt=1,
        )
        failed = await move_work_item(
            repository,
            failed,
            WorkItemPhase.FAILED,
            attempt=1,
        )

        await controller.reconcile(context_for(failed))
        still_failed = await repository.get(failed.metadata.id)
        assert still_failed.status.phase == WorkItemPhase.FAILED
        assert condition(still_failed, "Ready").reason == "RetryAttemptsExhausted"

        repository.close()

    asyncio.run(scenario())


def test_execution_controller_advances_only_with_evidence_and_cancels_safely(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        execution_repository = SQLiteExecutionRepository(":memory:")
        plan_repository = SQLitePlanRepository(":memory:")
        workspace_repository = SQLiteWorkspaceRepository(":memory:")
        work_item_repository = SQLiteWorkItemRepository(":memory:")
        review_repository = SQLiteReviewRepository(":memory:")
        approval_repository = SQLiteApprovalRepository(":memory:")
        project_id = uuid4()
        created_execution = await execution_repository.create(execution(project_id))
        controller = ExecutionController(
            execution_repository,
            plan_repository=plan_repository,
            workspace_repository=workspace_repository,
            work_item_repository=work_item_repository,
            review_repository=review_repository,
            approval_repository=approval_repository,
        )

        await controller.reconcile(context_for(created_execution))
        planning = await execution_repository.get(created_execution.metadata.id)
        assert planning.status.phase == ExecutionPhase.PLANNING

        await controller.reconcile(context_for(planning))
        still_planning = await execution_repository.get(created_execution.metadata.id)
        assert still_planning.status.phase == ExecutionPhase.PLANNING
        assert condition(still_planning, "Reconciled").reason == "WaitingForPlan"

        plan = await plan_repository.create(
            Plan.new(name="plan-1", spec=plan_spec(created_execution.metadata.id))
        )
        approved_plan = await approve_plan_status(plan_repository, plan)

        await controller.reconcile(context_for(still_planning))
        waiting_for_plan_approval = await execution_repository.get(
            created_execution.metadata.id
        )
        assert (
            waiting_for_plan_approval.status.phase
            == ExecutionPhase.WAITING_FOR_PLAN_APPROVAL
        )
        assert waiting_for_plan_approval.status.approved_plan_ref is not None

        await controller.reconcile(context_for(waiting_for_plan_approval))
        preparing_workspace = await execution_repository.get(
            created_execution.metadata.id
        )
        assert preparing_workspace.status.phase == ExecutionPhase.PREPARING_WORKSPACE

        workspace = await workspace_repository.create(
            workspace_resource(created_execution.metadata.id)
        )
        await ready_workspace(
            workspace_repository,
            workspace,
            tmp_path / "workspaces" / "execution-backend",
        )

        await controller.reconcile(context_for(preparing_workspace))
        executing = await execution_repository.get(created_execution.metadata.id)
        assert executing.status.phase == ExecutionPhase.EXECUTING
        assert len(executing.status.workspace_refs) == 1

        work_item = await work_item_repository.create(
            WorkItem.new(
                name="add-health",
                spec=work_item_spec(
                    created_execution.metadata.id,
                    approved_plan.metadata.id,
                ),
            )
        )
        await succeed_work_item(work_item_repository, work_item)

        await controller.reconcile(context_for(executing))
        verifying = await execution_repository.get(created_execution.metadata.id)
        assert verifying.status.phase == ExecutionPhase.VERIFYING
        assert len(verifying.status.active_work_item_refs) == 1

        await controller.reconcile(context_for(verifying))
        reviewing = await execution_repository.get(created_execution.metadata.id)
        assert reviewing.status.phase == ExecutionPhase.REVIEWING

        review = await review_repository.create(
            review_resource(created_execution.metadata.id, work_item.metadata.id)
        )
        await review_repository.update_status(
            review.metadata.id,
            ReviewStatus(
                observedGeneration=review.metadata.generation,
                phase=ReviewPhase.COMPLETED,
                verdict=ReviewVerdict.APPROVE,
                summary="Looks good",
                completedAt=utc_now(),
            ),
            expected_resource_version=review.metadata.resource_version,
        )

        await controller.reconcile(context_for(reviewing))
        waiting_for_final = await execution_repository.get(
            created_execution.metadata.id
        )
        assert (
            waiting_for_final.status.phase == ExecutionPhase.WAITING_FOR_FINAL_APPROVAL
        )

        final_approvals = await approval_repository.list_by_execution(
            created_execution.metadata.id
        )
        final_approval = next(
            approval
            for approval in final_approvals
            if approval.spec.approval_type == ApprovalType.FINAL
            and approval.spec.subject_ref.kind == "Review"
        )
        await approve(approval_repository, final_approval)

        await controller.reconcile(context_for(waiting_for_final))
        completed = await execution_repository.get(created_execution.metadata.id)
        assert completed.status.phase == ExecutionPhase.COMPLETED
        completed_resource_version = completed.metadata.resource_version

        await controller.reconcile(context_for(completed))
        terminal = await execution_repository.get(created_execution.metadata.id)
        assert terminal.status.phase == ExecutionPhase.COMPLETED
        assert terminal.metadata.resource_version == completed_resource_version

        cancellable = await execution_repository.create(
            Execution.new(name="cancel-me", spec=execution_spec(project_id))
        )
        cancellable = await execution_repository.update_status(
            cancellable.metadata.id,
            ExecutionStatus(phase=ExecutionPhase.PLANNING),
            expected_resource_version=cancellable.metadata.resource_version,
        )
        cancellable = await execution_repository.update_status(
            cancellable.metadata.id,
            ExecutionStatus(phase=ExecutionPhase.WAITING_FOR_PLAN_APPROVAL),
            expected_resource_version=cancellable.metadata.resource_version,
        )
        cancellable = await execution_repository.update_status(
            cancellable.metadata.id,
            ExecutionStatus(phase=ExecutionPhase.PREPARING_WORKSPACE),
            expected_resource_version=cancellable.metadata.resource_version,
        )
        cancellable = await execution_repository.update_status(
            cancellable.metadata.id,
            ExecutionStatus(phase=ExecutionPhase.EXECUTING),
            expected_resource_version=cancellable.metadata.resource_version,
        )
        cancellable = await execution_repository.update_spec(
            cancellable.metadata.id,
            cancellable.spec.model_copy(update={"cancellation_requested": True}),
            expected_resource_version=cancellable.metadata.resource_version,
        )

        await controller.reconcile(context_for(cancellable))
        cancelled = await execution_repository.get(cancellable.metadata.id)
        assert cancelled.status.phase == ExecutionPhase.CANCELLED
        assert cancelled.status.completed_at is not None

        execution_repository.close()
        plan_repository.close()
        workspace_repository.close()
        work_item_repository.close()
        review_repository.close()
        approval_repository.close()

    asyncio.run(scenario())


def test_review_repair_workflow_approve_path_creates_exact_final_approval() -> None:
    async def scenario() -> None:
        harness = await review_workflow_harness()

        await harness.controller.reconcile(context_for(harness.execution))
        reviewing = await harness.executions.get(harness.execution.metadata.id)
        reviews = await harness.reviews.list_by_execution(harness.execution.metadata.id)
        artifacts = await harness.artifacts.list_by_work_item(
            harness.work_item.metadata.id
        )

        assert reviewing.status.phase == ExecutionPhase.REVIEWING
        assert condition(reviewing, "Reconciled").reason == "WaitingForReview"
        assert len(reviews) == 1
        assert reviews[0].spec.policy.require_tests is False
        assert {subject.id for subject in reviews[0].spec.subject_refs} == {
            artifact.metadata.id for artifact in artifacts
        }

        completed_review = await complete_review(
            harness.reviews,
            reviews[0],
            ReviewVerdict.APPROVE,
        )

        await harness.controller.reconcile(context_for(reviewing))
        waiting = await harness.executions.get(harness.execution.metadata.id)
        approvals = await harness.approvals.list_by_execution(
            harness.execution.metadata.id
        )
        final_approval = approvals[0]

        assert waiting.status.phase == ExecutionPhase.WAITING_FOR_FINAL_APPROVAL
        assert condition(waiting, "Reconciled").reason == "ReviewApproved"
        assert final_approval.spec.approval_type == ApprovalType.FINAL
        assert final_approval.spec.subject_ref.kind == "Review"
        assert final_approval.spec.subject_ref.id == completed_review.metadata.id
        assert (
            final_approval.spec.subject_ref.resource_version
            == completed_review.metadata.resource_version
        )

        await approve(harness.approvals, final_approval)
        await harness.controller.reconcile(context_for(waiting))
        completed = await harness.executions.get(harness.execution.metadata.id)
        assert completed.status.phase == ExecutionPhase.COMPLETED

        harness.close()

    asyncio.run(scenario())


def test_review_workflow_reviews_all_succeeded_work_item_artifacts() -> None:
    async def scenario() -> None:
        harness = await review_workflow_harness()
        sibling = await harness.work_items.create(
            WorkItem.new(
                name="update-readme",
                spec=work_item_spec(
                    harness.execution.metadata.id,
                    harness.work_item.spec.plan_ref.id,
                    plan_work_item_id="update-readme",
                ),
            )
        )
        succeeded_sibling = await succeed_work_item(harness.work_items, sibling)
        sibling_artifacts = await create_review_artifacts(
            harness.artifacts,
            harness.execution,
            succeeded_sibling,
            suffix="readme",
        )
        initial_artifacts = await harness.artifacts.list_by_work_item(
            harness.work_item.metadata.id
        )

        await harness.controller.reconcile(context_for(harness.execution))
        reviews = await harness.reviews.list_by_execution(harness.execution.metadata.id)

        assert len(reviews) == 1
        assert reviews[0].spec.work_item_ref.id == succeeded_sibling.metadata.id
        assert {subject.id for subject in reviews[0].spec.subject_refs} == {
            artifact.metadata.id
            for artifact in (*initial_artifacts, *sibling_artifacts)
        }

        harness.close()

    asyncio.run(scenario())


def test_review_repair_workflow_request_changes_creates_one_repair_iteration() -> None:
    async def scenario() -> None:
        harness = await review_workflow_harness()
        await harness.controller.reconcile(context_for(harness.execution))
        reviews = await harness.reviews.list_by_execution(harness.execution.metadata.id)
        review = reviews[0]
        await complete_review(
            harness.reviews,
            review,
            ReviewVerdict.REQUEST_CHANGES,
            blocking_findings=(blocking_finding(),),
        )

        await harness.controller.reconcile(context_for(harness.execution))
        executing = await harness.executions.get(harness.execution.metadata.id)
        work_items = await harness.work_items.list_by_execution(
            harness.execution.metadata.id
        )
        repair_items = tuple(
            item
            for item in work_items
            if item.spec.plan_work_item_id.startswith("repair-")
        )

        assert executing.status.phase == ExecutionPhase.EXECUTING
        assert executing.status.iteration.coding == 1
        assert executing.status.iteration.review == 1
        assert len(repair_items) == 1
        assert repair_items[0].spec.depends_on[0].id == harness.work_item.metadata.id
        assert any(
            "fix-health-test" in criterion
            for criterion in repair_items[0].spec.acceptance_criteria
        )

        await harness.controller.reconcile(context_for(executing))
        restarted = await harness.executions.get(harness.execution.metadata.id)
        restart_items = await harness.work_items.list_by_execution(
            harness.execution.metadata.id
        )

        assert restarted.status.phase == ExecutionPhase.EXECUTING
        assert len(restart_items) == 2

        harness.close()

    asyncio.run(scenario())


def test_review_repair_workflow_repair_success_reviews_new_artifacts() -> None:
    async def scenario() -> None:
        harness = await review_workflow_harness()
        await harness.controller.reconcile(context_for(harness.execution))
        reviews = await harness.reviews.list_by_execution(harness.execution.metadata.id)
        review = reviews[0]
        await complete_review(
            harness.reviews,
            review,
            ReviewVerdict.REQUEST_CHANGES,
            blocking_findings=(blocking_finding(),),
        )
        await harness.controller.reconcile(context_for(harness.execution))
        repair = next(
            item
            for item in await harness.work_items.list_by_execution(
                harness.execution.metadata.id
            )
            if item.spec.plan_work_item_id.startswith("repair-")
        )

        succeeded_repair = await succeed_work_item(harness.work_items, repair)
        repair_artifacts = await create_review_artifacts(
            harness.artifacts,
            harness.execution,
            succeeded_repair,
            suffix="repair",
        )
        executing = await harness.executions.get(harness.execution.metadata.id)
        await harness.controller.reconcile(context_for(executing))
        verifying = await harness.executions.get(harness.execution.metadata.id)
        await harness.controller.reconcile(context_for(verifying))
        reviewing = await harness.executions.get(harness.execution.metadata.id)
        await harness.controller.reconcile(context_for(reviewing))

        repair_reviews = await harness.reviews.list_by_work_item(
            succeeded_repair.metadata.id
        )
        initial_artifacts = await harness.artifacts.list_by_work_item(
            harness.work_item.metadata.id
        )
        assert reviewing.status.phase == ExecutionPhase.REVIEWING
        assert len(repair_reviews) == 1
        assert {subject.id for subject in repair_reviews[0].spec.subject_refs} == {
            artifact.metadata.id for artifact in (*initial_artifacts, *repair_artifacts)
        }

        completed_repair_review = await complete_review(
            harness.reviews,
            repair_reviews[0],
            ReviewVerdict.APPROVE,
        )
        reviewing = await harness.executions.get(harness.execution.metadata.id)
        await harness.controller.reconcile(context_for(reviewing))
        waiting = await harness.executions.get(harness.execution.metadata.id)
        final_approval = (
            await harness.approvals.list_by_execution(harness.execution.metadata.id)
        )[0]

        assert waiting.status.phase == ExecutionPhase.WAITING_FOR_FINAL_APPROVAL
        assert final_approval.spec.subject_ref.id == completed_repair_review.metadata.id
        assert (
            final_approval.spec.subject_ref.resource_version
            == completed_repair_review.metadata.resource_version
        )

        harness.close()

    asyncio.run(scenario())


def test_review_repair_workflow_enforces_repair_limit() -> None:
    async def scenario() -> None:
        harness = await review_workflow_harness(
            max_review_iterations=1,
            review_iteration=1,
        )
        await harness.controller.reconcile(context_for(harness.execution))
        reviews = await harness.reviews.list_by_execution(harness.execution.metadata.id)
        review = reviews[0]
        await complete_review(
            harness.reviews,
            review,
            ReviewVerdict.REQUEST_CHANGES,
            blocking_findings=(blocking_finding(),),
        )

        await harness.controller.reconcile(context_for(harness.execution))
        failed = await harness.executions.get(harness.execution.metadata.id)
        work_items = await harness.work_items.list_by_execution(
            harness.execution.metadata.id
        )

        assert failed.status.phase == ExecutionPhase.FAILED
        assert condition(failed, "Reconciled").reason == "ReviewRepairLimitExceeded"
        assert len(work_items) == 1

        harness.close()

    asyncio.run(scenario())


def test_review_repair_workflow_failed_review_is_terminal() -> None:
    async def scenario() -> None:
        harness = await review_workflow_harness()
        await harness.controller.reconcile(context_for(harness.execution))
        reviews = await harness.reviews.list_by_execution(harness.execution.metadata.id)
        await fail_review(harness.reviews, reviews[0])

        await harness.controller.reconcile(context_for(harness.execution))
        failed = await harness.executions.get(harness.execution.metadata.id)
        reviews_after_failure = await harness.reviews.list_by_execution(
            harness.execution.metadata.id
        )

        assert failed.status.phase == ExecutionPhase.FAILED
        assert condition(failed, "Reconciled").reason == "ReviewFailed"
        assert len(reviews_after_failure) == 1

        harness.close()

    asyncio.run(scenario())


def test_review_repair_workflow_needs_human_decision_pauses_for_approval() -> None:
    async def scenario() -> None:
        harness = await review_workflow_harness()
        await harness.controller.reconcile(context_for(harness.execution))
        reviews = await harness.reviews.list_by_execution(harness.execution.metadata.id)
        review = reviews[0]
        await complete_review(
            harness.reviews,
            review,
            ReviewVerdict.NEEDS_HUMAN_DECISION,
        )

        await harness.controller.reconcile(context_for(harness.execution))
        waiting = await harness.executions.get(harness.execution.metadata.id)
        approvals = await harness.approvals.list_by_execution(
            harness.execution.metadata.id
        )

        assert waiting.status.phase == ExecutionPhase.WAITING_FOR_FINAL_APPROVAL
        assert condition(waiting, "Reconciled").reason == "ReviewNeedsHumanDecision"
        assert len(approvals) == 1
        assert approvals[0].status.phase == ApprovalPhase.PENDING

        harness.close()

    asyncio.run(scenario())


def test_review_repair_workflow_ignores_stale_final_approval() -> None:
    async def scenario() -> None:
        harness = await review_workflow_harness()
        await harness.controller.reconcile(context_for(harness.execution))
        reviews = await harness.reviews.list_by_execution(harness.execution.metadata.id)
        review = reviews[0]
        stale_approval = await harness.approvals.create(
            Approval.new(
                name="stale-final-approval",
                spec=approval_spec(
                    harness.execution.metadata.id,
                    review,
                    approval_type=ApprovalType.FINAL,
                ),
            )
        )
        await approve(harness.approvals, stale_approval)
        await complete_review(harness.reviews, review, ReviewVerdict.APPROVE)

        await harness.controller.reconcile(context_for(harness.execution))
        waiting = await harness.executions.get(harness.execution.metadata.id)
        await harness.controller.reconcile(context_for(waiting))
        still_waiting = await harness.executions.get(harness.execution.metadata.id)
        approvals = await harness.approvals.list_by_execution(
            harness.execution.metadata.id
        )
        current_approval = next(
            approval
            for approval in approvals
            if approval.status.phase == ApprovalPhase.PENDING
        )

        assert still_waiting.status.phase == ExecutionPhase.WAITING_FOR_FINAL_APPROVAL
        assert current_approval.spec.subject_ref.resource_version == 2

        await approve(harness.approvals, current_approval)
        await harness.controller.reconcile(context_for(still_waiting))
        completed = await harness.executions.get(harness.execution.metadata.id)

        assert completed.status.phase == ExecutionPhase.COMPLETED

        harness.close()

    asyncio.run(scenario())


def test_provider_agent_and_artifact_controllers_record_external_evidence() -> None:
    async def scenario() -> None:
        provider_repository = SQLiteProviderRepository(":memory:")
        agent_repository = SQLiteAgentRepository(":memory:")
        artifact_repository = SQLiteArtifactRepository(":memory:")
        provider = await provider_repository.create(provider_resource())
        agent = await agent_repository.create(agent_resource())
        artifact = await artifact_repository.create(artifact_resource(uuid4()))

        provider_controller = ProviderController(
            provider_repository,
            runtime_resolver=lambda _: ReadyRuntime(),
        )
        await provider_controller.reconcile(context_for(provider))
        ready_provider = await provider_repository.get(provider.metadata.id)
        assert ready_provider.status.phase == ProviderPhase.READY
        assert condition(ready_provider, "Ready").status is ConditionStatus.TRUE

        agent_controller = AgentController(
            agent_repository,
            provider_repository=provider_repository,
        )
        await agent_controller.reconcile(context_for(agent))
        ready_agent = await agent_repository.get(agent.metadata.id)
        assert ready_agent.status.phase == AgentPhase.READY
        assert ready_agent.status.model_available is True
        assert condition(ready_agent, "Ready").status is ConditionStatus.TRUE

        artifact_controller = ArtifactController(
            artifact_repository,
            storage=MatchingStorage(),
        )
        await artifact_controller.reconcile(context_for(artifact))
        available_artifact = await artifact_repository.get(artifact.metadata.id)
        assert available_artifact.status.phase == ArtifactPhase.AVAILABLE
        assert (
            available_artifact.status.verified_sha256 == available_artifact.spec.sha256
        )

        provider_repository.close()
        agent_repository.close()
        artifact_repository.close()

    asyncio.run(scenario())


def test_status_only_controllers_update_owned_conditions(tmp_path: Path) -> None:
    async def scenario() -> None:
        project_repository = SQLiteProjectRepository(":memory:")
        workflow_repository = SQLiteWorkflowRepository(":memory:")
        workspace_repository = SQLiteWorkspaceRepository(":memory:")
        approval_repository = SQLiteApprovalRepository(":memory:")
        review_repository = SQLiteReviewRepository(":memory:")
        execution_id = uuid4()

        created_project = await project_repository.create(project(tmp_path / "repo"))
        await ProjectController(project_repository).reconcile(
            context_for(created_project)
        )
        ready_project = await project_repository.get(created_project.metadata.id)
        assert ready_project.status.phase == ProjectPhase.READY
        assert condition(ready_project, "Ready").status is ConditionStatus.TRUE

        created_workflow = await workflow_repository.create(workflow())
        await WorkflowController(workflow_repository).reconcile(
            context_for(created_workflow)
        )
        ready_workflow = await workflow_repository.get(created_workflow.metadata.id)
        assert ready_workflow.status.phase == WorkflowPhase.READY
        assert condition(ready_workflow, "Ready").status is ConditionStatus.TRUE

        created_workspace = await workspace_repository.create(
            workspace_resource(execution_id)
        )
        await WorkspaceController(workspace_repository).reconcile(
            context_for(created_workspace)
        )
        pending_workspace = await workspace_repository.get(
            created_workspace.metadata.id
        )
        assert pending_workspace.status.phase == WorkspacePhase.PENDING
        assert condition(pending_workspace, "Ready").status is ConditionStatus.UNKNOWN

        expired_subject = execution()
        expired_approval = await approval_repository.create(
            Approval.new(
                name="expired-approval",
                spec=approval_spec(
                    execution_id,
                    expired_subject,
                    approval_type=ApprovalType.MANUAL,
                    expires_at=datetime(2026, 1, 1, tzinfo=UTC),
                ),
            )
        )
        await ApprovalController(
            approval_repository,
            now=lambda: datetime(2026, 1, 2, tzinfo=UTC),
        ).reconcile(context_for(expired_approval))
        expired = await approval_repository.get(expired_approval.metadata.id)
        assert expired.status.phase == ApprovalPhase.EXPIRED

        created_review = await review_repository.create(
            review_resource(execution_id, uuid4())
        )
        await ReviewController(review_repository).reconcile(context_for(created_review))
        scheduled_review = await review_repository.get(created_review.metadata.id)
        assert scheduled_review.status.phase == ReviewPhase.SCHEDULED
        assert condition(scheduled_review, "Scheduled").status is ConditionStatus.TRUE

        project_repository.close()
        workflow_repository.close()
        workspace_repository.close()
        approval_repository.close()
        review_repository.close()

    asyncio.run(scenario())
