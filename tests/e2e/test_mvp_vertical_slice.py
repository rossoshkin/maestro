"""End-to-end MVP workflow harness for Maestro's local-first vertical slice."""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

import pytest

from maestro.application.approvals import ApprovalService
from maestro.application.artifacts import ArtifactService
from maestro.application.coding import CodingOutputStatus, CodingRuntime
from maestro.application.controllers import (
    ReconcileKey,
    ReconciliationContext,
    RetryPolicy,
)
from maestro.application.executions import ExecutionService
from maestro.application.planner import PlannerRuntime
from maestro.application.resource_controllers import (
    ExecutionController,
    PlanController,
    WorkItemController,
)
from maestro.application.reviewer import ReviewerRuntime
from maestro.application.scheduler import WorkItemScheduler
from maestro.application.tools import CodingToolRuntime
from maestro.application.verification import (
    VerificationController,
    VerificationStatus,
)
from maestro.application.workspaces import WorkspaceLifecycleService
from maestro.domain.agents import (
    Agent,
    AgentCapabilityBindingReference,
    AgentCapacity,
    AgentPhase,
    AgentProviderReference,
    AgentScheduling,
    AgentSpec,
    AgentStatus,
    AgentSupportedRole,
)
from maestro.domain.approvals import (
    Approval,
    ApprovalDecision,
    ApprovalDecisionValue,
    ApprovalExecutionReference,
    ApprovalSpec,
    ApprovalSubjectReference,
    ApprovalType,
)
from maestro.domain.capabilities import (
    Capability,
    CapabilityApprovalPolicy,
    CapabilityBinding,
    CapabilityBindingPhase,
    CapabilityBindingSpec,
    CapabilityBindingStatus,
    CapabilityPhase,
    CapabilityScope,
    CapabilitySideEffectLevel,
    CapabilitySpec,
    CapabilityStatus,
)
from maestro.domain.events import (
    EventDraft,
    EventExecutionReference,
)
from maestro.domain.executions import (
    Execution,
    ExecutionLimits,
    ExecutionPhase,
    ExecutionSpec,
    ExecutionWorkflowReference,
    Goal,
    ProjectReference,
)
from maestro.domain.plans import PlanPhase
from maestro.domain.projects import (
    AgentReference,
    Project,
    ProjectPhase,
    ProjectRepositoryBinding,
    ProjectRepositoryStatus,
    ProjectRoleBinding,
    ProjectSpec,
    ProjectStatus,
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
    ProviderStatus,
    ProviderTokenUsage,
    StructuredGenerationRequest,
    StructuredGenerationResult,
    ToolLoopRequest,
    ToolLoopResult,
)
from maestro.domain.resources import (
    BaseResource,
    ResourceReference,
)
from maestro.domain.reviews import ReviewPhase, ReviewVerdict
from maestro.domain.roles import (
    Role,
    RoleExecutionPolicy,
    RolePhase,
    RoleSpec,
    RoleStatus,
    RoleValidationResult,
)
from maestro.domain.work_items import (
    WorkItem,
    WorkItemPhase,
    WorkItemWorkspaceReference,
)
from maestro.domain.workspaces import (
    Workspace,
    WorkspaceExecutionReference,
    WorkspaceProviderReference,
    WorkspaceSpec,
)
from maestro.infrastructure.artifacts import LocalArtifactStorage
from maestro.infrastructure.persistence import (
    SQLiteAgentRepository,
    SQLiteApprovalRepository,
    SQLiteArtifactRepository,
    SQLiteCapabilityBindingRepository,
    SQLiteCapabilityRepository,
    SQLiteEventStore,
    SQLiteExecutionRepository,
    SQLitePlanRepository,
    SQLiteProjectRepository,
    SQLiteProviderRepository,
    SQLiteReviewRepository,
    SQLiteRoleInvocationRepository,
    SQLiteRoleRepository,
    SQLiteWorkItemRepository,
    SQLiteWorkspaceRepository,
)
from maestro.infrastructure.workspaces import LocalGitWorktreeProvider

FIXTURE_ROOT = Path(__file__).parents[1] / "fixtures" / "fastapi_health_app"
GRANTED_CODING_CAPABILITIES = (
    "filesystem.read",
    "filesystem.write",
    "shell.execute.test",
    "git.status",
    "git.diff",
)


class ScriptedModelProvider:
    """Deterministic model provider used by the MVP harness."""

    def __init__(
        self,
        *,
        structured_outputs: Iterable[dict[str, object]] = (),
        tool_outputs: Iterable[dict[str, object]] = (),
        model: str = "scripted-model",
    ) -> None:
        self.structured_calls: list[StructuredGenerationRequest] = []
        self.tool_calls: list[ToolLoopRequest] = []
        self._structured_outputs = deque(structured_outputs)
        self._tool_outputs = deque(tool_outputs)
        self._model = model

    async def health(self) -> ProviderHealth:
        return ProviderHealth(
            phase=ProviderPhase.READY,
            capabilities=ProviderFeatureSet(
                structuredOutput=True,
                toolCalling=True,
            ),
            availableModels=(self._model,),
        )

    async def list_models(self) -> ProviderModelList:
        return ProviderModelList(models=(self._model,))

    async def generate_structured(
        self,
        request: StructuredGenerationRequest,
    ) -> StructuredGenerationResult:
        self.structured_calls.append(request)
        if not self._structured_outputs:
            raise AssertionError("ScriptedModelProvider has no structured output")
        output = self._structured_outputs.popleft()
        return StructuredGenerationResult(
            model=request.model,
            output=output,
            rawText=json.dumps(output, sort_keys=True),
            tokenUsage=ProviderTokenUsage(inputTokens=1, outputTokens=1),
        )

    async def run_tool_loop(self, request: ToolLoopRequest) -> ToolLoopResult:
        self.tool_calls.append(request)
        if not self._tool_outputs:
            raise AssertionError("ScriptedModelProvider has no tool-loop output")
        output = self._tool_outputs.popleft()
        return ToolLoopResult(
            model=request.model,
            output=output,
            toolCallCount=len(output.get("toolCalls", ())),
            tokenUsage=ProviderTokenUsage(inputTokens=1, outputTokens=1),
        )


@dataclass(slots=True)
class MvpHarness:
    """SQLite-backed resources and services for one MVP scenario."""

    db_path: Path
    artifact_root: Path
    workspace_root: Path
    projects: SQLiteProjectRepository
    executions: SQLiteExecutionRepository
    plans: SQLitePlanRepository
    work_items: SQLiteWorkItemRepository
    workspaces: SQLiteWorkspaceRepository
    artifacts: SQLiteArtifactRepository
    reviews: SQLiteReviewRepository
    approvals: SQLiteApprovalRepository
    providers: SQLiteProviderRepository
    agents: SQLiteAgentRepository
    roles: SQLiteRoleRepository
    capabilities: SQLiteCapabilityRepository
    bindings: SQLiteCapabilityBindingRepository
    role_invocations: SQLiteRoleInvocationRepository
    events: SQLiteEventStore
    artifact_storage: LocalArtifactStorage
    artifact_service: ArtifactService

    @classmethod
    def open(cls, tmp_path: Path) -> MvpHarness:
        db_path = tmp_path / "maestro.db"
        artifact_root = tmp_path / "artifacts"
        projects = SQLiteProjectRepository(db_path)
        executions = SQLiteExecutionRepository(db_path)
        plans = SQLitePlanRepository(db_path)
        work_items = SQLiteWorkItemRepository(db_path)
        workspaces = SQLiteWorkspaceRepository(db_path)
        artifacts = SQLiteArtifactRepository(db_path)
        reviews = SQLiteReviewRepository(db_path)
        approvals = SQLiteApprovalRepository(db_path)
        providers = SQLiteProviderRepository(db_path)
        agents = SQLiteAgentRepository(db_path)
        roles = SQLiteRoleRepository(db_path)
        capabilities = SQLiteCapabilityRepository(db_path)
        bindings = SQLiteCapabilityBindingRepository(db_path)
        role_invocations = SQLiteRoleInvocationRepository(db_path)
        events = SQLiteEventStore(db_path)
        artifact_storage = LocalArtifactStorage(artifact_root)
        artifact_service = ArtifactService(artifacts, artifact_storage)
        return cls(
            db_path=db_path,
            artifact_root=artifact_root,
            workspace_root=tmp_path / "workspaces",
            projects=projects,
            executions=executions,
            plans=plans,
            work_items=work_items,
            workspaces=workspaces,
            artifacts=artifacts,
            reviews=reviews,
            approvals=approvals,
            providers=providers,
            agents=agents,
            roles=roles,
            capabilities=capabilities,
            bindings=bindings,
            role_invocations=role_invocations,
            events=events,
            artifact_storage=artifact_storage,
            artifact_service=artifact_service,
        )

    def reopen(self) -> MvpHarness:
        self.close()
        return MvpHarness.open(self.db_path.parent)

    def execution_controller(self) -> ExecutionController:
        return ExecutionController(
            self.executions,
            plan_repository=self.plans,
            workspace_repository=self.workspaces,
            work_item_repository=self.work_items,
            artifact_repository=self.artifacts,
            review_repository=self.reviews,
            approval_repository=self.approvals,
            event_publisher=self.events,
        )

    def plan_controller(self) -> PlanController:
        return PlanController(
            self.plans,
            approval_repository=self.approvals,
            work_item_repository=self.work_items,
            event_publisher=self.events,
        )

    def work_item_controller(self) -> WorkItemController:
        return WorkItemController(self.work_items, event_publisher=self.events)

    def scheduler(self) -> WorkItemScheduler:
        return WorkItemScheduler(
            work_item_repository=self.work_items,
            agent_repository=self.agents,
            role_repository=self.roles,
            provider_repository=self.providers,
            capability_repository=self.capabilities,
            capability_binding_repository=self.bindings,
            event_publisher=self.events,
        )

    def planner_runtime(self) -> PlannerRuntime:
        return PlannerRuntime(
            execution_repository=self.executions,
            project_repository=self.projects,
            plan_repository=self.plans,
            role_invocation_repository=self.role_invocations,
            artifact_service=self.artifact_service,
            event_publisher=self.events,
        )

    def coding_runtime(self) -> CodingRuntime:
        tool_runtime = CodingToolRuntime(
            artifact_service=self.artifact_service,
            event_publisher=self.events,
        )
        return CodingRuntime(
            work_item_repository=self.work_items,
            role_invocation_repository=self.role_invocations,
            artifact_service=self.artifact_service,
            tool_runtime=tool_runtime,
            event_publisher=self.events,
        )

    def verification_controller(
        self,
        workspace_provider: LocalGitWorktreeProvider,
    ) -> VerificationController:
        return VerificationController(
            work_item_repository=self.work_items,
            workspace_repository=self.workspaces,
            workspace_provider=workspace_provider,
            artifact_service=self.artifact_service,
            event_publisher=self.events,
        )

    def reviewer_runtime(self) -> ReviewerRuntime:
        return ReviewerRuntime(
            review_repository=self.reviews,
            artifact_repository=self.artifacts,
            artifact_storage=self.artifact_storage,
            artifact_service=self.artifact_service,
            event_publisher=self.events,
        )

    def close(self) -> None:
        self.projects.close()
        self.executions.close()
        self.plans.close()
        self.work_items.close()
        self.workspaces.close()
        self.artifacts.close()
        self.reviews.close()
        self.approvals.close()
        self.providers.close()
        self.agents.close()
        self.roles.close()
        self.capabilities.close()
        self.bindings.close()
        self.role_invocations.close()
        self.events.close()


def test_mvp_fastapi_health_workflow_survives_restart_and_repairs(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        git = git_binary()
        source = create_fixture_repository(tmp_path, git)
        source_revision = run_git(git, source, "rev-parse", "HEAD")
        harness = MvpHarness.open(tmp_path)
        workspace_provider = LocalGitWorktreeProvider(git)

        try:
            await install_runtime_catalog(harness)
            project = await create_ready_project(harness, source)
            execution = await create_execution(harness, project)
            await publish_human_event(harness, execution, "GoalCreated")

            await harness.execution_controller().reconcile(context_for(execution))
            execution = await harness.executions.get(execution.metadata.id)
            assert execution.status.phase == ExecutionPhase.PLANNING

            planner = ScriptedModelProvider(
                structured_outputs=(planner_output(),),
                model="planner-model",
            )
            plan_result = await harness.planner_runtime().invoke_planner(
                execution.metadata.id,
                agent=planner_agent(),
                provider=ollama_provider(model="planner-model"),
                runtime=planner,
                granted_capabilities=("filesystem.read",),
                repository_context={"fixtureRepository": str(source)},
            )
            assert plan_result.plan_ref is not None

            plan = await harness.plans.get(plan_result.plan_ref.id)
            await harness.plan_controller().reconcile(context_for(plan))
            plan = await harness.plans.get(plan.metadata.id)
            assert plan.status.phase == PlanPhase.WAITING_FOR_APPROVAL

            approval = await create_approval(
                harness,
                execution,
                plan,
                ApprovalType.PLAN,
            )
            await approve(harness, approval, actor="human")
            await harness.plan_controller().reconcile(context_for(plan))
            plan = await harness.plans.get(plan.metadata.id)
            assert plan.status.phase == PlanPhase.APPROVED

            await harness.plan_controller().reconcile(context_for(plan))
            work_item = (await harness.work_items.list_by_plan(plan.metadata.id))[0]

            await harness.execution_controller().reconcile(context_for(execution))
            execution = await harness.executions.get(execution.metadata.id)
            assert execution.status.phase == ExecutionPhase.WAITING_FOR_PLAN_APPROVAL
            await harness.execution_controller().reconcile(context_for(execution))
            execution = await harness.executions.get(execution.metadata.id)
            assert execution.status.phase == ExecutionPhase.PREPARING_WORKSPACE

            workspace = await harness.workspaces.create(workspace_resource(execution))
            workspace = await WorkspaceLifecycleService(
                harness.workspaces
            ).prepare_workspace(
                workspace.metadata.id,
                workspace_provider,
                source_repository_path=source,
                workspace_root=harness.workspace_root,
                expected_resource_version=workspace.metadata.resource_version,
            )
            await publish_human_event(
                harness,
                execution,
                "WorkspacePrepared",
                workspace,
            )
            assert workspace.status.path is not None
            assert workspace.status.path != source

            work_item = await attach_workspace(harness, work_item, workspace)
            await harness.execution_controller().reconcile(context_for(execution))
            execution = await harness.executions.get(execution.metadata.id)
            assert execution.status.phase == ExecutionPhase.EXECUTING

            await harness.work_item_controller().reconcile(context_for(work_item))
            ready_item = await harness.work_items.get(work_item.metadata.id)
            assert ready_item.status.phase == WorkItemPhase.READY
            scheduled = await schedule(harness, ready_item)

            coding = ScriptedModelProvider(
                tool_outputs=initial_coding_script(),
                model="coder-model",
            )
            coding_result = await harness.coding_runtime().invoke_coding(
                scheduled.metadata.id,
                workspace=workspace,
                workspace_provider=workspace_provider,
                agent=coding_agent(),
                provider=ollama_provider(model="coder-model"),
                runtime=coding,
                granted_capabilities=GRANTED_CODING_CAPABILITIES,
                max_steps=8,
            )
            assert coding_result.status == CodingOutputStatus.COMPLETED
            assert (workspace.status.path / "tests" / "test_health.py").exists()
            assert not (source / "tests" / "test_health.py").exists()
            assert run_git(git, source, "status", "--short") == ""

            verification = await harness.verification_controller(
                workspace_provider
            ).verify_work_item(scheduled.metadata.id)
            assert verification.status == VerificationStatus.PASSED

            harness = harness.reopen()
            workspace_provider = LocalGitWorktreeProvider(git)
            execution = await harness.executions.get(execution.metadata.id)
            await harness.execution_controller().reconcile(context_for(execution))
            execution = await harness.executions.get(execution.metadata.id)
            assert execution.status.phase == ExecutionPhase.VERIFYING
            await harness.execution_controller().reconcile(context_for(execution))
            execution = await harness.executions.get(execution.metadata.id)
            assert execution.status.phase == ExecutionPhase.REVIEWING
            await harness.execution_controller().reconcile(context_for(execution))

            initial_review = one(
                await harness.reviews.list_by_work_item(scheduled.metadata.id)
            )
            reviewer = ScriptedModelProvider(
                structured_outputs=(request_changes_review(),),
                model="codex-reviewer",
            )
            review_result = await harness.reviewer_runtime().invoke_review(
                initial_review.metadata.id,
                provider=codex_provider(),
                runtime=reviewer,
                model="codex-reviewer",
            )
            assert review_result.status.verdict == ReviewVerdict.REQUEST_CHANGES
            reviewed = await harness.reviews.get(initial_review.metadata.id)
            assert reviewed.status.phase == ReviewPhase.COMPLETED

            await harness.execution_controller().reconcile(context_for(execution))
            execution = await harness.executions.get(execution.metadata.id)
            assert execution.status.phase == ExecutionPhase.EXECUTING
            assert execution.status.iteration.review == 1
            repair = next(
                item
                for item in await harness.work_items.list_by_execution(
                    execution.metadata.id
                )
                if item.spec.plan_work_item_id.startswith("repair-")
            )
            assert repair.spec.workspace_ref is not None

            await harness.work_item_controller().reconcile(context_for(repair))
            repair_ready = await harness.work_items.get(repair.metadata.id)
            repair_scheduled = await schedule(harness, repair_ready)
            workspace = await harness.workspaces.get(workspace.metadata.id)
            repair_coding = ScriptedModelProvider(
                tool_outputs=repair_coding_script(),
                model="coder-model",
            )
            repair_result = await harness.coding_runtime().invoke_coding(
                repair_scheduled.metadata.id,
                workspace=workspace,
                workspace_provider=workspace_provider,
                agent=coding_agent(),
                provider=ollama_provider(model="coder-model"),
                runtime=repair_coding,
                granted_capabilities=GRANTED_CODING_CAPABILITIES,
                max_steps=5,
            )
            assert repair_result.status == CodingOutputStatus.COMPLETED

            repair_verification = await harness.verification_controller(
                workspace_provider
            ).verify_work_item(repair_scheduled.metadata.id)
            assert repair_verification.status == VerificationStatus.PASSED

            execution = await harness.executions.get(execution.metadata.id)
            await harness.execution_controller().reconcile(context_for(execution))
            execution = await harness.executions.get(execution.metadata.id)
            assert execution.status.phase == ExecutionPhase.VERIFYING
            await harness.execution_controller().reconcile(context_for(execution))
            execution = await harness.executions.get(execution.metadata.id)
            assert execution.status.phase == ExecutionPhase.REVIEWING
            await harness.execution_controller().reconcile(context_for(execution))

            repair_review = one(
                await harness.reviews.list_by_work_item(repair_scheduled.metadata.id)
            )
            approving_reviewer = ScriptedModelProvider(
                structured_outputs=(approve_review(),),
                model="codex-reviewer",
            )
            final_review_result = await harness.reviewer_runtime().invoke_review(
                repair_review.metadata.id,
                provider=codex_provider(),
                runtime=approving_reviewer,
                model="codex-reviewer",
            )
            assert final_review_result.status.verdict == ReviewVerdict.APPROVE

            execution = await harness.executions.get(execution.metadata.id)
            await harness.execution_controller().reconcile(context_for(execution))
            waiting = await harness.executions.get(execution.metadata.id)
            assert waiting.status.phase == ExecutionPhase.WAITING_FOR_FINAL_APPROVAL

            final_approval = one(
                approval
                for approval in await harness.approvals.list_by_execution(
                    execution.metadata.id
                )
                if approval.spec.approval_type == ApprovalType.FINAL
            )
            await approve(harness, final_approval, actor="human")
            await harness.execution_controller().reconcile(context_for(waiting))
            completed = await harness.executions.get(execution.metadata.id)
            assert completed.status.phase == ExecutionPhase.COMPLETED

            events = await harness.events.list_by_execution(execution.metadata.id)
            artifacts = await harness.artifacts.list_by_execution(execution.metadata.id)
            role_invocations = await harness.role_invocations.list_by_execution(
                execution.metadata.id
            )
            event_types = {event.spec.event_type for event in events}
            artifact_types = {artifact.spec.artifact_type for artifact in artifacts}

            assert event_sequence(events) == tuple(range(1, len(events) + 1))
            assert {
                "GoalCreated",
                "ExecutionPhaseChanged",
                "PlanProduced",
                "PlanPhaseChanged",
                "WorkItemPhaseChanged",
                "WorkItemScheduled",
                "ToolCallRecorded",
                "CodingImplementationProduced",
                "VerificationCompleted",
                "ReviewCompleted",
                "ApprovalDecided",
                "WorkspacePrepared",
            } <= event_types
            assert {
                "prompt",
                "model-response",
                "plan",
                "tool-log",
                "git-diff",
                "summary",
                "command-output",
                "verification-report",
                "review",
            } <= {str(value) for value in artifact_types}
            assert len(role_invocations) >= 3
            assert all(artifact.spec.sha256 for artifact in artifacts)
            assert run_git(git, source, "rev-parse", "HEAD") == source_revision
            assert run_git(git, source, "status", "--short") == ""
        finally:
            harness.close()

    asyncio.run(scenario())


def git_binary() -> str:
    binary = shutil.which("git")
    if binary is None:
        pytest.skip("git is required for the MVP e2e harness")
    return binary


def run_git(git: str, cwd: Path, *args: str) -> str:
    completed = subprocess.run(
        (git, "-C", str(cwd), *args),
        capture_output=True,
        check=False,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
    return completed.stdout.strip()


def create_fixture_repository(tmp_path: Path, git: str) -> Path:
    source = tmp_path / "source"
    shutil.copytree(FIXTURE_ROOT, source)
    run_git(git, source, "init")
    run_git(git, source, "checkout", "-b", "main")
    run_git(git, source, "config", "user.name", "Maestro MVP")
    run_git(git, source, "config", "user.email", "maestro@example.test")
    run_git(git, source, "add", ".")
    run_git(git, source, "commit", "-m", "fixture")
    return source


async def install_runtime_catalog(harness: MvpHarness) -> None:
    await harness.roles.create(ready_role())
    for capability in GRANTED_CODING_CAPABILITIES:
        await harness.capabilities.create(ready_capability(capability))
    await harness.bindings.create(ready_binding(grants=GRANTED_CODING_CAPABILITIES))
    await harness.providers.create(ready_provider_resource())
    await harness.agents.create(ready_coding_agent_resource())


async def create_ready_project(harness: MvpHarness, source: Path) -> Project:
    project = await harness.projects.create(
        Project.new(
            name="fastapi-health",
            spec=ProjectSpec(
                description="Fixture FastAPI project",
                repositories=(
                    ProjectRepositoryBinding(
                        id="backend",
                        path=source,
                        defaultBranch="main",
                    ),
                ),
                workflowRef=WorkflowReference(
                    name="software-delivery",
                    version="v1alpha1",
                ),
                roleBindings={
                    "planner": ProjectRoleBinding(
                        agentRef=AgentReference(name="planner-local")
                    ),
                    "coding": ProjectRoleBinding(
                        agentRef=AgentReference(name="coder-local")
                    ),
                    "reviewer": ProjectRoleBinding(
                        agentRef=AgentReference(name="reviewer-local")
                    ),
                },
            ),
        )
    )
    observed = await harness.projects.update_status(
        project.metadata.id,
        ProjectStatus(
            repositories=(
                ProjectRepositoryStatus(
                    id="backend",
                    reachable=True,
                    gitRepository=True,
                    clean=True,
                    headRevision=run_git(git_binary(), source, "rev-parse", "HEAD"),
                ),
            ),
        ),
        expected_resource_version=project.metadata.resource_version,
    )
    return await harness.projects.update_status(
        observed.metadata.id,
        observed.status.model_copy(update={"phase": ProjectPhase.READY}),
        expected_resource_version=observed.metadata.resource_version,
    )


async def create_execution(harness: MvpHarness, project: Project) -> Execution:
    execution = await ExecutionService(
        harness.executions,
        harness.projects,
    ).create_execution(
        name="add-health-endpoint",
        spec=ExecutionSpec(
            projectRef=ProjectReference(
                id=project.metadata.id,
                name=project.metadata.name,
            ),
            goal=Goal(
                summary="Create a minimal FastAPI application.",
                description=(
                    "GET /health returns {'status': 'ok'}, add one automated "
                    "test, add README instructions, and do not add a database "
                    "or authentication."
                ),
                acceptanceCriteria=(
                    "GET /health returns {'status': 'ok'}.",
                    "One automated test is added.",
                    "README contains run instructions.",
                    "No database or authentication is added.",
                ),
            ),
            workflowRef=ExecutionWorkflowReference(
                name="software-delivery",
                version="v1alpha1",
            ),
            requestedRoles=("planner", "coding", "reviewer"),
            limits=ExecutionLimits(
                maxCodingIterations=2,
                maxReviewIterations=2,
                maxToolCallsPerInvocation=12,
            ),
        ),
    )
    assert execution.status.phase == ExecutionPhase.DRAFT
    return execution


def planner_output() -> dict[str, object]:
    return {
        "summary": "Implement a FastAPI health endpoint and verify it.",
        "assumptions": ("The fixture app can be changed in-place.",),
        "questions": (),
        "risks": (),
        "workItems": (
            {
                "id": "add-health",
                "title": "Add FastAPI health endpoint",
                "roleRef": {"name": "coding", "version": "v1alpha1"},
                "repositoryRef": "backend",
                "objective": (
                    "Add GET /health, one automated test, and README run "
                    "instructions without adding persistence or authentication."
                ),
                "constraints": (
                    "Do not add a database.",
                    "Do not add authentication.",
                ),
                "acceptanceCriteria": (
                    "GET /health returns {'status': 'ok'}.",
                    "An automated test covers the endpoint.",
                    "README explains how to run the app.",
                ),
                "verification": {"commands": ("python -m pytest -q",)},
                "dependsOn": (),
                "requestedCapabilities": GRANTED_CODING_CAPABILITIES,
            },
        ),
    }


def initial_coding_script() -> tuple[dict[str, object], ...]:
    return (
        tool_call(
            "write-file",
            {
                "path": "app.py",
                "content": (
                    "from fastapi import FastAPI\n\n"
                    "app = FastAPI()\n\n\n"
                    "@app.get('/health')\n"
                    "def health() -> dict[str, str]:\n"
                    "    return {'status': 'ok'}\n"
                ),
            },
        ),
        tool_call(
            "write-file",
            {
                "path": "tests/test_health.py",
                "content": (
                    "from fastapi.testclient import TestClient\n\n"
                    "from app import app\n\n\n"
                    "def test_health_endpoint() -> None:\n"
                    "    response = TestClient(app).get('/health')\n\n"
                    "    assert response.status_code == 200\n"
                    "    assert response.json() == {'status': 'ok'}\n"
                ),
            },
        ),
        tool_call(
            "write-file",
            {
                "path": "README.md",
                "content": (
                    "# FastAPI Health Fixture\n\n"
                    "Run the app with:\n\n"
                    "```bash\n"
                    "uvicorn app:app --reload\n"
                    "```\n"
                ),
            },
        ),
        tool_call(
            "run-command",
            {
                "command": ("git", "add", "-N", "tests/test_health.py"),
                "capability": "shell.execute.test",
            },
        ),
        coding_completed(
            "Implemented the endpoint, endpoint test, and README run command.",
            (
                ("app.py", "modified"),
                ("tests/test_health.py", "added"),
                ("README.md", "modified"),
            ),
        ),
    )


def repair_coding_script() -> tuple[dict[str, object], ...]:
    return (
        tool_call(
            "write-file",
            {
                "path": "README.md",
                "content": (
                    "# FastAPI Health Fixture\n\n"
                    "Run the app with:\n\n"
                    "```bash\n"
                    "uvicorn app:app --reload\n"
                    "```\n\n"
                    "Run the automated health check with:\n\n"
                    "```bash\n"
                    "python -m pytest -q\n"
                    "```\n"
                ),
            },
        ),
        coding_completed(
            "Addressed reviewer feedback by documenting verification.",
            (("README.md", "modified"),),
        ),
    )


def request_changes_review() -> dict[str, object]:
    return {
        "verdict": "RequestChanges",
        "summary": "The app works, but the README does not show the test command.",
        "blockingFindings": (
            {
                "id": "document-test-command",
                "severity": "medium",
                "category": "tests",
                "file": "README.md",
                "issue": "README omits the verification command.",
                "evidence": "Review artifacts show tests were added.",
                "suggestedFix": "Document python -m pytest -q.",
            },
        ),
        "nonBlockingFindings": (),
        "missingEvidence": (),
    }


def approve_review() -> dict[str, object]:
    return {
        "verdict": "Approve",
        "summary": "Endpoint, test, and README evidence satisfy the goal.",
        "blockingFindings": (),
        "nonBlockingFindings": (),
        "missingEvidence": (),
    }


def tool_call(name: str, arguments: dict[str, object]) -> dict[str, object]:
    return {"toolCalls": ({"name": name, "arguments": arguments},)}


def coding_completed(
    summary: str,
    changed_files: tuple[tuple[str, str], ...],
) -> dict[str, object]:
    return {
        "status": "completed",
        "summary": summary,
        "changedFiles": tuple(
            {"path": path, "changeType": change_type}
            for path, change_type in changed_files
        ),
        "commandsRequested": (),
        "remainingIssues": (),
        "questions": (),
    }


def workspace_resource(execution: Execution) -> Workspace:
    return Workspace.new(
        name="execution-backend",
        spec=WorkspaceSpec(
            executionRef=WorkspaceExecutionReference(
                id=execution.metadata.id,
                name=execution.metadata.name,
            ),
            repositoryRef="backend",
            providerRef=WorkspaceProviderReference(name="local-git-worktree"),
            baseRevision="main",
            branchName=f"maestro/{execution.metadata.id.hex[:12]}",
        ),
    )


async def attach_workspace(
    harness: MvpHarness,
    work_item: WorkItem,
    workspace: Workspace,
) -> WorkItem:
    spec = work_item.spec.model_copy(
        update={
            "workspace_ref": WorkItemWorkspaceReference(
                id=workspace.metadata.id,
                name=workspace.metadata.name,
            )
        }
    )
    return await harness.work_items.update_spec(
        work_item.metadata.id,
        spec,
        expected_resource_version=work_item.metadata.resource_version,
    )


async def schedule(harness: MvpHarness, work_item: WorkItem) -> WorkItem:
    decision = await harness.scheduler().schedule_work_item(work_item.metadata.id)
    assert decision.scheduled is True
    return await harness.work_items.get(work_item.metadata.id)


async def create_approval(
    harness: MvpHarness,
    execution: Execution,
    subject: BaseResource[object, object],
    approval_type: ApprovalType,
) -> Approval:
    return await harness.approvals.create(
        Approval.new(
            name=f"{approval_type.value.lower()}-{subject.metadata.name}",
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


async def approve(harness: MvpHarness, approval: Approval, *, actor: str) -> Approval:
    decided = await ApprovalService(harness.approvals).record_decision(
        approval.metadata.id,
        ApprovalDecision(
            actor=actor,
            decision=ApprovalDecisionValue.APPROVE,
            requestSource="mvp-e2e",
        ),
        expected_resource_version=approval.metadata.resource_version,
    )
    await harness.events.publish(
        EventDraft(
            type="ApprovalDecided",
            producer="human",
            correlationId=(
                f"approval:{decided.metadata.id}:{decided.metadata.resource_version}"
            ),
            executionRef=EventExecutionReference(
                id=decided.spec.execution_ref.id,
                name=decided.spec.execution_ref.name,
            ),
            subjectRef=resource_ref(decided),
            payload={
                "decision": decided.status.decisions[-1].decision,
                "actor": actor,
            },
        )
    )
    return decided


async def publish_human_event(
    harness: MvpHarness,
    execution: Execution,
    event_type: str,
    subject: BaseResource[object, object] | None = None,
) -> None:
    await harness.events.publish(
        EventDraft(
            type=event_type,
            producer="mvp-e2e",
            correlationId=f"{event_type}:{execution.metadata.id}",
            executionRef=EventExecutionReference(
                id=execution.metadata.id,
                name=execution.metadata.name,
            ),
            subjectRef=resource_ref(subject or execution),
            payload={},
        )
    )


def context_for(resource: BaseResource[object, object]) -> ReconciliationContext:
    return ReconciliationContext(
        key=ReconcileKey(kind=resource.kind, resource_id=resource.metadata.id),
        controller_name="mvp-e2e",
        attempt=1,
        retry_policy=RetryPolicy(),
    )


def resource_ref(resource: BaseResource[object, object]) -> ResourceReference:
    return ResourceReference(
        kind=resource.kind,
        id=resource.metadata.id,
        name=resource.metadata.name,
    )


def one[T](items: Iterable[T]) -> T:
    values = tuple(items)
    assert len(values) == 1
    return values[0]


def event_sequence(events: Iterable[object]) -> tuple[int, ...]:
    return tuple(event.spec.sequence for event in events)


def ready_role() -> Role:
    role = Role.new(
        name="coding",
        spec=RoleSpec(
            version="v1alpha1",
            purpose="Coding role",
            inputSchemaRef="WorkItemInput/v1",
            outputSchemaRef="WorkItemOutput/v1",
            requiredCapabilities=("filesystem.read",),
            executionPolicy=RoleExecutionPolicy(maxSteps=20),
        ),
    )
    return Role(
        metadata=role.metadata,
        spec=role.spec,
        status=RoleStatus(
            observedGeneration=role.metadata.generation,
            phase=RolePhase.READY,
            validation=RoleValidationResult(valid=True),
        ),
    )


def ready_capability(canonical_name: str) -> Capability:
    schema_name = "".join(part.capitalize() for part in canonical_name.split("."))
    capability = Capability.new(
        name=canonical_name.replace(".", "-"),
        spec=CapabilitySpec(
            canonicalName=canonical_name,
            description=f"Capability for {canonical_name}",
            sideEffectLevel=CapabilitySideEffectLevel.READ_ONLY,
            approvalPolicy=CapabilityApprovalPolicy.NONE,
            scopes=(CapabilityScope.WORKSPACE,),
            inputSchemaRef=f"{schema_name}Input/v1",
            outputSchemaRef=f"{schema_name}Output/v1",
        ),
    )
    return Capability(
        metadata=capability.metadata,
        spec=capability.spec,
        status=CapabilityStatus(
            observedGeneration=capability.metadata.generation,
            phase=CapabilityPhase.READY,
            toolImplementations=("local-tool",),
        ),
    )


def ready_binding(*, grants: tuple[str, ...]) -> CapabilityBinding:
    binding = CapabilityBinding.new(
        name="local-workspace-safe",
        spec=CapabilityBindingSpec(grants=grants),
    )
    return CapabilityBinding(
        metadata=binding.metadata,
        spec=binding.spec,
        status=CapabilityBindingStatus(
            observedGeneration=binding.metadata.generation,
            phase=CapabilityBindingPhase.READY,
        ),
    )


def ready_provider_resource() -> Provider:
    provider = ollama_provider(model="coder-model")
    return Provider(
        metadata=provider.metadata,
        spec=provider.spec,
        status=ProviderStatus(
            observedGeneration=provider.metadata.generation,
            phase=ProviderPhase.READY,
            capabilities=ProviderFeatureSet(toolCalling=True, structuredOutput=True),
            availableModels=("coder-model",),
        ),
    )


def ready_coding_agent_resource() -> Agent:
    agent = Agent.new(
        name="coder-local",
        spec=AgentSpec(
            providerRef=AgentProviderReference(name="ollama-local"),
            model="coder-model",
            supportedRoles=(AgentSupportedRole(name="coding", versions=("v1alpha1",)),),
            capabilityBindings=(
                AgentCapabilityBindingReference(name="local-workspace-safe"),
            ),
            capacity=AgentCapacity(maxConcurrentAssignments=2),
            scheduling=AgentScheduling(priority=100),
        ),
    )
    return Agent(
        metadata=agent.metadata,
        spec=agent.spec,
        status=AgentStatus(
            observedGeneration=agent.metadata.generation,
            phase=AgentPhase.READY,
            modelAvailable=True,
        ),
    )


def planner_agent() -> Agent:
    return Agent.new(
        name="planner-local",
        spec=AgentSpec(
            providerRef=AgentProviderReference(name="ollama-local"),
            model="planner-model",
            supportedRoles=(
                AgentSupportedRole(name="planner", versions=("v1alpha1",)),
            ),
        ),
    )


def coding_agent() -> Agent:
    return Agent.new(
        name="coder-local",
        spec=AgentSpec(
            providerRef=AgentProviderReference(name="ollama-local"),
            model="coder-model",
            supportedRoles=(AgentSupportedRole(name="coding", versions=("v1alpha1",)),),
        ),
    )


def ollama_provider(*, model: str) -> Provider:
    return Provider.new(
        name="ollama-local",
        spec=ProviderSpec(
            type="ollama",
            endpoint="http://127.0.0.1:11434",
            allowedModels=(model,),
            dataPolicy=ProviderDataPolicy(allowSourceCode=True),
            timeoutSeconds=30,
        ),
    )


def codex_provider() -> Provider:
    return Provider.new(
        name="codex-local",
        spec=ProviderSpec(
            type="codex",
            endpoint="codex",
            allowedModels=("codex-reviewer",),
            timeoutSeconds=30,
        ),
    )
