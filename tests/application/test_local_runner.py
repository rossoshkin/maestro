"""Tests for the browser MVP local execution runner helpers."""

from __future__ import annotations

import asyncio
import shutil
import subprocess
from pathlib import Path
from uuid import uuid4

import pytest

from maestro.application.controllers import observe_generation, with_condition
from maestro.application.local_runner import (
    DEFAULT_CODING_CAPABILITIES,
    DEFAULT_LOCAL_ROLE_VERSIONS,
    LocalExecutionRunner,
    PlannerRepairAdvice,
    _effective_coding_capabilities,
)
from maestro.application.workspaces import WorkspaceLifecycleService
from maestro.config import Settings
from maestro.domain.executions import (
    Execution,
    ExecutionPhase,
    ExecutionSpec,
    ExecutionStatus,
    ExecutionWorkflowReference,
    Goal,
    ProjectReference,
)
from maestro.domain.projects import (
    Project,
    ProjectPhase,
    ProjectRepositoryBinding,
    ProjectRepositoryStatus,
    ProjectSpec,
    ProjectStatus,
    WorkflowReference,
)
from maestro.domain.providers import (
    Provider,
    ProviderDataPolicy,
    ProviderFeatureSet,
    ProviderPhase,
    ProviderSpec,
    ProviderStatus,
)
from maestro.domain.resources import ConditionStatus
from maestro.domain.role_invocations import (
    RoleInvocation,
    RoleInvocationAgentReference,
    RoleInvocationExecutionReference,
    RoleInvocationFailure,
    RoleInvocationLimits,
    RoleInvocationPhase,
    RoleInvocationRoleReference,
    RoleInvocationSpec,
    RoleInvocationStatus,
    RoleInvocationWorkItemReference,
)
from maestro.domain.work_items import (
    WorkItem,
    WorkItemExecutionReference,
    WorkItemPhase,
    WorkItemPlanReference,
    WorkItemRoleReference,
    WorkItemSpec,
    WorkItemStatus,
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


def test_runner_expands_partial_coding_capability_requests() -> None:
    assert _effective_coding_capabilities(("shell.execute.test", "git.status")) == (
        DEFAULT_CODING_CAPABILITIES
    )
    assert _effective_coding_capabilities(()) == DEFAULT_CODING_CAPABILITIES
    assert _effective_coding_capabilities(
        ("filesystem.write", "custom.safe-capability")
    ) == (*DEFAULT_CODING_CAPABILITIES, "custom.safe-capability")


def test_runner_catalog_recovers_work_items_blocked_by_v1_role_alias(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        db_path = tmp_path / "maestro.db"
        settings = Settings(
            database_url=f"sqlite:///{db_path}",
            artifact_root=tmp_path / "artifacts",
            workspace_root=tmp_path / "workspaces",
        )
        projects = SQLiteProjectRepository(db_path)
        executions = SQLiteExecutionRepository(db_path)
        plans = SQLitePlanRepository(db_path)
        work_items = SQLiteWorkItemRepository(db_path)
        workspaces = SQLiteWorkspaceRepository(db_path)
        artifacts = SQLiteArtifactRepository(db_path)
        approvals = SQLiteApprovalRepository(db_path)
        reviews = SQLiteReviewRepository(db_path)
        providers = SQLiteProviderRepository(db_path)
        agents = SQLiteAgentRepository(db_path)
        roles = SQLiteRoleRepository(db_path)
        capabilities = SQLiteCapabilityRepository(db_path)
        bindings = SQLiteCapabilityBindingRepository(db_path)
        role_invocations = SQLiteRoleInvocationRepository(db_path)
        events = SQLiteEventStore(db_path)
        closeables = (
            projects,
            executions,
            plans,
            work_items,
            workspaces,
            artifacts,
            approvals,
            reviews,
            providers,
            agents,
            roles,
            capabilities,
            bindings,
            role_invocations,
            events,
        )
        try:
            provider = await providers.create(ready_ollama_provider())
            runner = LocalExecutionRunner(
                settings=settings,
                project_repository=projects,
                execution_repository=executions,
                plan_repository=plans,
                work_item_repository=work_items,
                workspace_repository=workspaces,
                artifact_repository=artifacts,
                artifact_storage=LocalArtifactStorage(settings.artifact_root),
                approval_repository=approvals,
                review_repository=reviews,
                provider_repository=providers,
                agent_repository=agents,
                role_repository=roles,
                capability_repository=capabilities,
                capability_binding_repository=bindings,
                role_invocation_repository=role_invocations,
                event_publisher=events,
            )

            async def fake_ensure_ollama_provider(namespace: str) -> Provider:
                assert namespace == "default"
                return provider

            monkeypatch.setattr(
                runner,
                "_ensure_ollama_provider",
                fake_ensure_ollama_provider,
            )
            execution = await executions.create(execution_resource())
            item = await work_items.create(v1_role_work_item(execution))
            blocked = await work_items.update_status(
                item.metadata.id,
                with_condition(
                    item,
                    observe_generation(
                        item,
                        item.status.model_copy(update={"phase": WorkItemPhase.BLOCKED}),
                    ),
                    condition_type="Scheduled",
                    condition_status=ConditionStatus.FALSE,
                    reason="RoleNotFound",
                    message="Role coding/v1 was not found",
                ),
                expected_resource_version=item.metadata.resource_version,
            )

            await runner._ensure_runtime_catalog("default")
            await runner._retry_role_catalog_blocked_work_items(execution)

            recovered = await work_items.get(blocked.metadata.id)
            coder = (await agents.list())[0]
            published = await events.list_by_execution(execution.metadata.id)

            assert recovered.status.phase == WorkItemPhase.READY
            assert any(
                condition.reason == "SchedulerRetry"
                for condition in recovered.status.conditions
            )
            assert (await roles.get_by_name_version("default", "coding", "v1")).spec
            assert coder.spec.supported_roles[0].versions == DEFAULT_LOCAL_ROLE_VERSIONS
            assert published[-1].spec.event_type == "WorkItemSchedulingRetry"
        finally:
            for repository in closeables:
                repository.close()

    asyncio.run(scenario())


def test_runner_workspace_names_are_unique_per_execution(tmp_path: Path) -> None:
    async def scenario() -> None:
        db_path = tmp_path / "maestro.db"
        runner, closeables = make_runner(tmp_path, db_path)
        try:
            project = ready_project(tmp_path)
            first = execution_resource(project=project, name="hello-one")
            second = execution_resource(project=project, name="hello-two")

            first_workspace = await runner._ensure_workspace(first, project)
            second_workspace = await runner._ensure_workspace(second, project)

            assert first_workspace.metadata.name.startswith(
                f"execution-{first.metadata.id.hex[:12]}-"
            )
            assert second_workspace.metadata.name.startswith(
                f"execution-{second.metadata.id.hex[:12]}-"
            )
            assert first_workspace.metadata.name != second_workspace.metadata.name
            assert first_workspace.spec.execution_ref.id == first.metadata.id
            assert second_workspace.spec.execution_ref.id == second.metadata.id
        finally:
            for repository in closeables:
                repository.close()

    asyncio.run(scenario())


def test_runner_failure_marks_active_execution_failed(tmp_path: Path) -> None:
    async def scenario() -> None:
        db_path = tmp_path / "maestro.db"
        runner, closeables = make_runner(tmp_path, db_path)
        (
            _projects,
            executions,
            _plans,
            _work_items,
            _workspaces,
            _artifacts,
            _approvals,
            _reviews,
            _providers,
            _agents,
            _roles,
            _capabilities,
            _bindings,
            _role_invocations,
            events,
        ) = closeables
        try:
            project = ready_project(tmp_path)
            execution = await executions.create(execution_resource(project=project))
            planning = await executions.update_status(
                execution.metadata.id,
                ExecutionStatus(
                    observedGeneration=execution.metadata.generation,
                    phase=ExecutionPhase.PLANNING,
                ),
                expected_resource_version=execution.metadata.resource_version,
            )

            await runner._publish_runner_failure(
                planning.metadata.id, ValueError("bad plan")
            )

            failed = await executions.get(planning.metadata.id)
            published = await events.list_by_execution(planning.metadata.id)

            assert failed.status.phase == ExecutionPhase.FAILED
            assert failed.status.conditions[0].reason == "RunnerFailed"
            assert published[-1].spec.event_type == "ExecutionRunFailed"
        finally:
            for repository in closeables:
                repository.close()

    asyncio.run(scenario())


def test_runner_builds_planner_advised_coding_retry_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        db_path = tmp_path / "maestro.db"
        runner, closeables = make_runner(tmp_path, db_path)
        (
            _projects,
            executions,
            _plans,
            work_items,
            _workspaces,
            _artifacts,
            _approvals,
            _reviews,
            _providers,
            _agents,
            _roles,
            _capabilities,
            _bindings,
            role_invocations,
            _events,
        ) = closeables
        try:
            project = ready_project(tmp_path)
            execution = await executions.create(execution_resource(project=project))
            work_item = await work_items.create(
                WorkItem.new(
                    name="add-health",
                    spec=WorkItemSpec(
                        executionRef=WorkItemExecutionReference(
                            id=execution.metadata.id,
                            name=execution.metadata.name,
                        ),
                        planRef=WorkItemPlanReference(
                            id=uuid4(),
                            name="plan-1",
                            version=1,
                        ),
                        planWorkItemId="add-health",
                        roleRef=WorkItemRoleReference(
                            name="coding",
                            version="v1alpha1",
                        ),
                        objective="Implement GET /health.",
                        acceptanceCriteria=("GET /health returns 200.",),
                    ),
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
                WorkItemStatus(
                    observedGeneration=ready.metadata.generation,
                    phase=WorkItemPhase.SCHEDULED,
                    attempt=1,
                ),
                expected_resource_version=ready.metadata.resource_version,
            )
            running = await work_items.update_status(
                scheduled.metadata.id,
                WorkItemStatus(
                    observedGeneration=scheduled.metadata.generation,
                    phase=WorkItemPhase.RUNNING,
                    attempt=1,
                ),
                expected_resource_version=scheduled.metadata.resource_version,
            )
            await work_items.update_status(
                running.metadata.id,
                WorkItemStatus(
                    observedGeneration=running.metadata.generation,
                    phase=WorkItemPhase.FAILED,
                    attempt=1,
                ),
                expected_resource_version=running.metadata.resource_version,
            )
            invocation = await role_invocations.create(
                RoleInvocation.new(
                    name="coding-add-health-a1",
                    spec=RoleInvocationSpec(
                        executionRef=RoleInvocationExecutionReference(
                            id=execution.metadata.id,
                            name=execution.metadata.name,
                        ),
                        workItemRef=RoleInvocationWorkItemReference(
                            id=work_item.metadata.id,
                            name=work_item.metadata.name,
                        ),
                        roleRef=RoleInvocationRoleReference(
                            name="coding",
                            version="v1alpha1",
                        ),
                        agentRef=RoleInvocationAgentReference(
                            id=uuid4(),
                            name="coder-local",
                        ),
                        limits=RoleInvocationLimits(
                            maxSteps=20,
                            maxDurationSeconds=120,
                        ),
                    ),
                )
            )
            await role_invocations.update_status(
                invocation.metadata.id,
                RoleInvocationStatus(
                    observedGeneration=invocation.metadata.generation,
                    phase=RoleInvocationPhase.FAILED,
                    failure=RoleInvocationFailure(
                        reason="CodingToolCallsInvalid",
                        message="extra artifactRef in tool call",
                    ),
                ),
                expected_resource_version=invocation.metadata.resource_version,
            )

            async def fake_planner_repair_advice(*_args: object) -> PlannerRepairAdvice:
                return PlannerRepairAdvice(
                    summary="Retry with a valid write-file tool call.",
                    instructions=("Write main.py with GET /health.",),
                )

            monkeypatch.setattr(
                runner,
                "_planner_repair_advice",
                fake_planner_repair_advice,
            )

            provider = ready_ollama_provider()
            retry_context = await runner._coding_context(execution, running, provider)

            assert retry_context["previousCodingFailure"]["name"] == "add-health"
            assert (
                retry_context["previousCodingFailure"]["invocationFailureReason"]
                == "CodingToolCallsInvalid"
            )
            assert retry_context["plannerRepairAdvice"]["instructions"] == [
                "Write main.py with GET /health."
            ]
        finally:
            for repository in closeables:
                repository.close()

    asyncio.run(scenario())


def test_runner_publishes_final_workspace_to_project_repository(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        source_path, revision = create_source_repository(tmp_path)
        db_path = tmp_path / "maestro.db"
        runner, closeables = make_runner(tmp_path, db_path)
        (
            projects,
            executions,
            _plans,
            _work_items,
            workspaces,
            _artifacts,
            _approvals,
            _reviews,
            _providers,
            _agents,
            _roles,
            _capabilities,
            _bindings,
            _role_invocations,
            events,
        ) = closeables
        try:
            project = await projects.create(
                ready_project(
                    tmp_path,
                    repository_path=source_path,
                    head_revision=revision,
                )
            )
            execution = await executions.create(
                execution_resource(project=project, name="publish-hello")
            )
            workspace = await runner._ensure_workspace(execution, project)
            prepared = await WorkspaceLifecycleService(workspaces).prepare_workspace(
                workspace.metadata.id,
                LocalGitWorktreeProvider(git_binary()),
                source_repository_path=source_path,
                workspace_root=tmp_path / "workspaces",
                expected_resource_version=workspace.metadata.resource_version,
            )
            assert prepared.status.path is not None
            (prepared.status.path / "README.md").write_text("Run `./hello_world.sh`.\n")
            script = prepared.status.path / "hello_world.sh"
            script.write_text("#!/bin/sh\necho 'hello world'\n")
            script.chmod(0o755)

            await runner._publish_final_workspace(execution)

            published_events = await events.list_by_execution(execution.metadata.id)
            target_script = source_path / "hello_world.sh"

            assert (source_path / "README.md").read_text() == (
                "Run `./hello_world.sh`.\n"
            )
            assert target_script.read_text() == "#!/bin/sh\necho 'hello world'\n"
            assert target_script.stat().st_mode & 0o111
            assert "README.md" in run_git(source_path, "status", "--short")
            assert any(
                event.spec.event_type == "WorkspacePublished"
                for event in published_events
            )
        finally:
            for repository in closeables:
                repository.close()

    asyncio.run(scenario())


def make_runner(
    tmp_path: Path,
    db_path: Path,
) -> tuple[LocalExecutionRunner, tuple[object, ...]]:
    settings = Settings(
        database_url=f"sqlite:///{db_path}",
        artifact_root=tmp_path / "artifacts",
        workspace_root=tmp_path / "workspaces",
    )
    projects = SQLiteProjectRepository(db_path)
    executions = SQLiteExecutionRepository(db_path)
    plans = SQLitePlanRepository(db_path)
    work_items = SQLiteWorkItemRepository(db_path)
    workspaces = SQLiteWorkspaceRepository(db_path)
    artifacts = SQLiteArtifactRepository(db_path)
    approvals = SQLiteApprovalRepository(db_path)
    reviews = SQLiteReviewRepository(db_path)
    providers = SQLiteProviderRepository(db_path)
    agents = SQLiteAgentRepository(db_path)
    roles = SQLiteRoleRepository(db_path)
    capabilities = SQLiteCapabilityRepository(db_path)
    bindings = SQLiteCapabilityBindingRepository(db_path)
    role_invocations = SQLiteRoleInvocationRepository(db_path)
    events = SQLiteEventStore(db_path)
    closeables = (
        projects,
        executions,
        plans,
        work_items,
        workspaces,
        artifacts,
        approvals,
        reviews,
        providers,
        agents,
        roles,
        capabilities,
        bindings,
        role_invocations,
        events,
    )
    return (
        LocalExecutionRunner(
            settings=settings,
            project_repository=projects,
            execution_repository=executions,
            plan_repository=plans,
            work_item_repository=work_items,
            workspace_repository=workspaces,
            artifact_repository=artifacts,
            artifact_storage=LocalArtifactStorage(settings.artifact_root),
            approval_repository=approvals,
            review_repository=reviews,
            provider_repository=providers,
            agent_repository=agents,
            role_repository=roles,
            capability_repository=capabilities,
            capability_binding_repository=bindings,
            role_invocation_repository=role_invocations,
            event_publisher=events,
        ),
        closeables,
    )


def ready_ollama_provider() -> Provider:
    provider = Provider.new(
        name="ollama-local",
        spec=ProviderSpec(
            type="ollama",
            endpoint="http://127.0.0.1:11434",
            allowedModels=("planner-model", "coder-model"),
            dataPolicy=ProviderDataPolicy(allowSourceCode=True),
        ),
    )
    return Provider(
        metadata=provider.metadata,
        spec=provider.spec,
        status=ProviderStatus(
            observedGeneration=provider.metadata.generation,
            phase=ProviderPhase.READY,
            capabilities=ProviderFeatureSet(
                structuredOutput=True,
                toolCalling=True,
            ),
            availableModels=("planner-model", "coder-model"),
        ),
    )


def ready_project(
    tmp_path: Path,
    *,
    repository_path: Path | None = None,
    head_revision: str = "abc123",
) -> Project:
    repository_path = repository_path or tmp_path / "repo"
    project = Project.new(
        name="demo",
        spec=ProjectSpec(
            description="Demo project",
            repositories=(
                ProjectRepositoryBinding(
                    id="backend",
                    path=repository_path,
                    defaultBranch="main",
                ),
            ),
            workflowRef=WorkflowReference(
                name="software-delivery",
                version="v1alpha1",
            ),
        ),
    )
    return Project(
        metadata=project.metadata,
        spec=project.spec,
        status=ProjectStatus(
            observedGeneration=project.metadata.generation,
            phase=ProjectPhase.READY,
            repositories=(
                ProjectRepositoryStatus(
                    id="backend",
                    reachable=True,
                    gitRepository=True,
                    clean=True,
                    headRevision=head_revision,
                ),
            ),
        ),
    )


def execution_resource(
    *,
    project: Project | None = None,
    name: str = "add-health",
) -> Execution:
    project_id = project.metadata.id if project is not None else uuid4()
    project_name = project.metadata.name if project is not None else "demo"
    return Execution.new(
        name=name,
        spec=ExecutionSpec(
            projectRef=ProjectReference(id=project_id, name=project_name),
            goal=Goal(summary="Add health endpoint"),
            workflowRef=ExecutionWorkflowReference(
                name="software-delivery",
                version="v1alpha1",
            ),
            requestedRoles=("planner", "coding", "reviewer"),
        ),
    )


def git_binary() -> str:
    binary = shutil.which("git")
    if binary is None:
        pytest.skip("git is required for local runner workspace publish tests")
    return binary


def run_git(path: Path, *args: str, stdin: str | None = None) -> str:
    completed = subprocess.run(
        (git_binary(), "-C", str(path), *args),
        input=stdin,
        capture_output=True,
        check=False,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
    return completed.stdout


def create_source_repository(tmp_path: Path) -> tuple[Path, str]:
    source_path = tmp_path / "repo"
    source_path.mkdir()
    run_git(source_path, "init")
    run_git(source_path, "checkout", "-b", "main")
    run_git(source_path, "config", "user.name", "Maestro Tests")
    run_git(source_path, "config", "user.email", "maestro@example.test")
    (source_path / "README.md").write_text("")
    run_git(source_path, "add", "README.md")
    run_git(source_path, "commit", "-m", "initial")
    return source_path, run_git(source_path, "rev-parse", "HEAD").strip()


def v1_role_work_item(execution: Execution) -> WorkItem:
    return WorkItem.new(
        name="add-health",
        spec=WorkItemSpec(
            executionRef=WorkItemExecutionReference(
                id=execution.metadata.id,
                name=execution.metadata.name,
            ),
            planRef=WorkItemPlanReference(id=uuid4(), name="plan", version=1),
            planWorkItemId="add-health",
            roleRef=WorkItemRoleReference(name="coding", version="v1"),
            objective="Implement GET /health.",
            acceptanceCriteria=("GET /health returns 200.",),
        ),
    )
