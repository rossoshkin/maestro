"""Tests for the FastAPI control-plane application."""

from __future__ import annotations

import asyncio
import shutil
import subprocess
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient, Response

from maestro.application.artifacts import ArtifactService
from maestro.config import Settings
from maestro.domain.approvals import (
    Approval,
    ApprovalExecutionReference,
    ApprovalSpec,
    ApprovalSubjectReference,
    ApprovalType,
)
from maestro.domain.artifacts import (
    ArtifactExecutionReference,
    ArtifactProducer,
    ArtifactType,
)
from maestro.domain.events import EventDraft, EventExecutionReference
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
    ProjectSpec,
    ProjectStatus,
    WorkflowReference,
)
from maestro.domain.resources import ResourceReference
from maestro.domain.role_invocations import (
    RoleInvocation,
    RoleInvocationAgentReference,
    RoleInvocationExecutionReference,
    RoleInvocationLimits,
    RoleInvocationRoleReference,
    RoleInvocationSpec,
)
from maestro.presentation.api import ApiContext, create_api_context, create_app


def api_settings(tmp_path: Path) -> Settings:
    return Settings(
        database_url=f"sqlite:///{tmp_path / 'maestro.db'}",
        artifact_root=tmp_path / "artifacts",
        workspace_root=tmp_path / "workspaces",
    )


def project_spec(
    description: str = "Test project",
    *,
    repository_path: Path | None = None,
) -> ProjectSpec:
    repositories = (
        (
            ProjectRepositoryBinding(
                id="backend",
                path=repository_path,
                defaultBranch="main",
            ),
        )
        if repository_path is not None
        else ()
    )
    return ProjectSpec(
        description=description,
        repositories=repositories,
        workflowRef=WorkflowReference(name="software-delivery", version="v1alpha1"),
    )


def execution_spec(project: Project) -> ExecutionSpec:
    return ExecutionSpec(
        projectRef=ProjectReference(
            id=project.metadata.id,
            name=project.metadata.name,
        ),
        goal=Goal(
            summary="Add health endpoint",
            acceptanceCriteria=("GET /health returns 200",),
        ),
        workflowRef=ExecutionWorkflowReference(
            name="software-delivery",
            version="v1alpha1",
        ),
        requestedRoles=("planner", "coding", "reviewer"),
    )


async def _get(settings: Settings, path: str) -> Response:
    transport = ASGITransport(app=create_app(settings))
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        return await client.get(path)


async def create_ready_project(
    context: ApiContext,
    *,
    name: str = "tour-manager",
) -> Project:
    project = await context.projects.create(Project.new(name=name, spec=project_spec()))
    return await context.projects.update_status(
        project.metadata.id,
        ProjectStatus(phase=ProjectPhase.READY),
        expected_resource_version=project.metadata.resource_version,
    )


def git_binary() -> str:
    binary = shutil.which("git")
    if binary is None:
        pytest.skip("git is required for project repository status tests")
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


def create_source_repository(tmp_path: Path) -> Path:
    git = git_binary()
    source = tmp_path / "source"
    source.mkdir()
    run_git(git, source, "init")
    run_git(git, source, "checkout", "-b", "main")
    run_git(git, source, "config", "user.name", "Maestro Tests")
    run_git(git, source, "config", "user.email", "maestro@example.test")
    (source / "README.md").write_text("source\n")
    run_git(git, source, "add", "README.md")
    run_git(git, source, "commit", "-m", "initial")
    return source


def create_empty_source_repository(tmp_path: Path) -> Path:
    git = git_binary()
    source = tmp_path / "empty-source"
    source.mkdir()
    run_git(git, source, "init")
    run_git(git, source, "checkout", "-b", "main")
    run_git(git, source, "config", "user.name", "Maestro Tests")
    run_git(git, source, "config", "user.email", "maestro@example.test")
    return source


def test_liveness_endpoint_returns_ok(tmp_path: Path) -> None:
    settings = api_settings(tmp_path)

    response = asyncio.run(_get(settings, "/health/live"))

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_readiness_endpoint_returns_ok(tmp_path: Path) -> None:
    settings = api_settings(tmp_path)

    response = asyncio.run(_get(settings, "/health/ready"))

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_api_context_sqlite_repositories_work_across_fastapi_threads(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        settings = api_settings(tmp_path)
        context = await asyncio.to_thread(create_api_context, settings)
        try:
            created = await context.projects.create(
                Project.new(name="tour-manager", spec=project_spec())
            )
            projects = await context.projects.list()

            assert projects[0].metadata.id == created.metadata.id
        finally:
            context.close()

    asyncio.run(scenario())


def test_clear_data_action_wipes_local_resources_and_runtime_roots(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        settings = api_settings(tmp_path)
        context = create_api_context(settings)
        app = create_app(settings, api_context=context)
        transport = ASGITransport(app=app)
        try:
            project = await context.projects.create(
                Project.new(name="tour-manager", spec=project_spec())
            )
            execution = await context.executions.create(
                Execution.new(name="add-health", spec=execution_spec(project))
            )
            subject = ResourceReference(
                kind=execution.kind,
                id=execution.metadata.id,
                name=execution.metadata.name,
            )
            await context.events.append(
                EventDraft(
                    type="GoalCreated",
                    producer="test",
                    correlationId="goal-1",
                    executionRef=EventExecutionReference(
                        id=execution.metadata.id,
                        name=execution.metadata.name,
                    ),
                    subjectRef=subject,
                    payload={"goal": "Add health"},
                )
            )
            settings.artifact_root.mkdir(parents=True, exist_ok=True)
            settings.workspace_root.mkdir(parents=True, exist_ok=True)
            (settings.artifact_root / "artifact.txt").write_text("artifact\n")
            (settings.workspace_root / "workspace.txt").write_text("workspace\n")

            async with AsyncClient(
                transport=transport,
                base_url="http://testserver",
            ) as client:
                rejected = await client.post(
                    "/api/v1/admin/clear-data",
                    json={"confirm": "nope"},
                )
                response = await client.post(
                    "/api/v1/admin/clear-data",
                    json={"confirm": "CLEAR"},
                )

                assert rejected.status_code == 400
                assert response.status_code == 200
                payload = response.json()
                assert payload["status"] == "cleared"
                assert "projects" in payload["clearedTables"]
                assert "events" in payload["clearedTables"]
                assert payload["clearedPaths"] == [
                    str(settings.artifact_root),
                    str(settings.workspace_root),
                ]

                projects = await context.projects.list()
                executions = await context.executions.list()
                events = await context.events.list_by_execution(execution.metadata.id)
                assert projects == ()
                assert executions == ()
                assert events == ()
                assert settings.artifact_root.exists()
                assert settings.workspace_root.exists()
                assert not (settings.artifact_root / "artifact.txt").exists()
                assert not (settings.workspace_root / "workspace.txt").exists()
        finally:
            context.close()

    asyncio.run(scenario())


def test_clear_data_action_prunes_missing_registered_worktrees(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        git = git_binary()
        source = create_source_repository(tmp_path)
        settings = api_settings(tmp_path)
        context = create_api_context(settings)
        app = create_app(settings, api_context=context)
        transport = ASGITransport(app=app)
        try:
            await context.projects.create(
                Project.new(
                    name="tour-manager",
                    spec=project_spec(repository_path=source),
                )
            )
            stale_worktree = settings.workspace_root / "default" / "execution-backend"
            run_git(
                git,
                source,
                "worktree",
                "add",
                "-B",
                "maestro/stale",
                str(stale_worktree),
                "main",
            )
            shutil.rmtree(stale_worktree)
            assert str(stale_worktree) in run_git(
                git,
                source,
                "worktree",
                "list",
                "--porcelain",
            )

            async with AsyncClient(
                transport=transport,
                base_url="http://testserver",
            ) as client:
                response = await client.post(
                    "/api/v1/admin/clear-data",
                    json={"confirm": "CLEAR"},
                )

            assert response.status_code == 200
            assert str(stale_worktree) not in run_git(
                git,
                source,
                "worktree",
                "list",
                "--porcelain",
            )
        finally:
            context.close()

    asyncio.run(scenario())


def test_project_and_execution_endpoints_preserve_resource_shape(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        settings = api_settings(tmp_path)
        context = create_api_context(settings)
        app = create_app(settings, api_context=context)
        transport = ASGITransport(app=app)
        try:
            async with AsyncClient(
                transport=transport,
                base_url="http://testserver",
            ) as client:
                project_response = await client.post(
                    "/api/v1/projects",
                    json={
                        "name": "tour-manager",
                        "spec": project_spec().model_dump(
                            mode="json",
                            by_alias=True,
                        ),
                    },
                )
                assert project_response.status_code == 201
                project_payload = project_response.json()
                assert project_payload["kind"] == "Project"
                assert set(project_payload) >= {"metadata", "spec", "status"}

                project = await context.projects.get(
                    UUID(project_payload["metadata"]["id"])
                )
                await context.projects.update_status(
                    project.metadata.id,
                    ProjectStatus(phase=ProjectPhase.READY),
                    expected_resource_version=project.metadata.resource_version,
                )
                project = await context.projects.get(project.metadata.id)

                execution_response = await client.post(
                    "/api/v1/executions",
                    json={
                        "name": "add-health",
                        "spec": execution_spec(project).model_dump(
                            mode="json",
                            by_alias=True,
                        ),
                    },
                )
                assert execution_response.status_code == 201
                execution_payload = execution_response.json()
                assert execution_payload["kind"] == "Execution"
                assert execution_payload["status"]["phase"] == "Draft"
                assert execution_payload["spec"]["projectRef"]["id"] == str(
                    project.metadata.id
                )
        finally:
            context.close()

    asyncio.run(scenario())


def test_execution_start_action_advances_draft_from_browser(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        settings = api_settings(tmp_path)
        context = create_api_context(settings)
        app = create_app(settings, api_context=context)
        transport = ASGITransport(app=app)
        try:
            project = await create_ready_project(context)
            async with AsyncClient(
                transport=transport,
                base_url="http://testserver",
            ) as client:
                execution_response = await client.post(
                    "/api/v1/executions",
                    json={
                        "name": "add-health",
                        "spec": execution_spec(project).model_dump(
                            mode="json",
                            by_alias=True,
                        ),
                    },
                )
                execution_response.raise_for_status()
                draft = execution_response.json()

                started_response = await client.post(
                    f"/api/v1/executions/{draft['metadata']['id']}/actions/start",
                    json={
                        "resourceVersion": draft["metadata"]["resourceVersion"],
                    },
                )

                assert started_response.status_code == 200
                started = started_response.json()
                assert started["status"]["phase"] == ExecutionPhase.PLANNING
                assert started["status"]["currentStep"] == "planning"
                assert started["status"]["startedAt"] is not None

                events = await context.events.list_by_execution(
                    UUID(started["metadata"]["id"])
                )
                assert [event.spec.event_type for event in events] == [
                    "GoalCreated",
                    "ExecutionPhaseChanged",
                ]
                assert events[0].spec.producer == "browser"
        finally:
            context.close()

    asyncio.run(scenario())


def test_execution_run_action_starts_draft_and_queues_backend_runner(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        settings = api_settings(tmp_path)
        context = create_api_context(settings)
        app = create_app(settings, api_context=context)
        transport = ASGITransport(app=app)
        try:
            project = await create_ready_project(context)
            async with AsyncClient(
                transport=transport,
                base_url="http://testserver",
            ) as client:
                execution_response = await client.post(
                    "/api/v1/executions",
                    json={
                        "name": "add-health",
                        "spec": execution_spec(project).model_dump(
                            mode="json",
                            by_alias=True,
                        ),
                    },
                )
                execution_response.raise_for_status()
                draft = execution_response.json()

                run_response = await client.post(
                    f"/api/v1/executions/{draft['metadata']['id']}/actions/run",
                    json={
                        "resourceVersion": draft["metadata"]["resourceVersion"],
                    },
                )

                assert run_response.status_code == 200
                payload = run_response.json()
                assert payload["status"]["phase"] == ExecutionPhase.PLANNING
                assert app.state.maestro_auto_run_enabled is False
                assert app.state.maestro_execution_tasks == {}
        finally:
            context.close()

    asyncio.run(scenario())


def test_execution_run_action_schedules_background_runner_when_enabled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        settings = api_settings(tmp_path)
        context = create_api_context(settings)
        app = create_app(settings, api_context=context)
        app.state.maestro_auto_run_enabled = True
        transport = ASGITransport(app=app)
        scheduled: list[UUID] = []

        class FakeRunner:
            def __init__(self, **_: object) -> None:
                pass

            async def run(self, execution_id: UUID) -> None:
                scheduled.append(execution_id)

        monkeypatch.setattr("maestro.presentation.api.LocalExecutionRunner", FakeRunner)
        try:
            project = await create_ready_project(context)
            async with AsyncClient(
                transport=transport,
                base_url="http://testserver",
            ) as client:
                execution_response = await client.post(
                    "/api/v1/executions",
                    json={
                        "name": "add-health",
                        "spec": execution_spec(project).model_dump(
                            mode="json",
                            by_alias=True,
                        ),
                    },
                )
                execution_response.raise_for_status()
                draft = execution_response.json()

                run_response = await client.post(
                    f"/api/v1/executions/{draft['metadata']['id']}/actions/run",
                    json={
                        "resourceVersion": draft["metadata"]["resourceVersion"],
                    },
                )
                await asyncio.sleep(0)

                assert run_response.status_code == 200
                assert scheduled == [UUID(draft["metadata"]["id"])]
        finally:
            context.close()

    asyncio.run(scenario())


def test_execution_respond_action_records_answers_and_resumes_runner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        settings = api_settings(tmp_path)
        context = create_api_context(settings)
        app = create_app(settings, api_context=context)
        app.state.maestro_auto_run_enabled = True
        transport = ASGITransport(app=app)
        scheduled: list[UUID] = []

        class FakeRunner:
            def __init__(self, **_: object) -> None:
                pass

            async def run(self, execution_id: UUID) -> None:
                scheduled.append(execution_id)

        monkeypatch.setattr("maestro.presentation.api.LocalExecutionRunner", FakeRunner)
        try:
            project = await create_ready_project(context)
            execution = await context.executions.create(
                Execution.new(name="add-health", spec=execution_spec(project))
            )
            planning = await context.executions.update_status(
                execution.metadata.id,
                ExecutionStatus(
                    observedGeneration=execution.metadata.generation,
                    phase=ExecutionPhase.PLANNING,
                    currentStep="planner",
                ),
                expected_resource_version=execution.metadata.resource_version,
            )
            waiting = await context.executions.update_status(
                planning.metadata.id,
                ExecutionStatus(
                    observedGeneration=planning.metadata.generation,
                    phase=ExecutionPhase.WAITING_FOR_USER_INPUT,
                    currentStep="planner-questions",
                ),
                expected_resource_version=planning.metadata.resource_version,
            )
            async with AsyncClient(
                transport=transport,
                base_url="http://testserver",
            ) as client:
                response = await client.post(
                    f"/api/v1/executions/{waiting.metadata.id}/actions/respond",
                    json={
                        "resourceVersion": waiting.metadata.resource_version,
                        "answers": (
                            {
                                "questionId": "target-route",
                                "answer": "Use GET /health.",
                            },
                        ),
                        "actor": "local-user",
                        "requestSource": "web-ui",
                    },
                )
                await asyncio.sleep(0)

                assert response.status_code == 200
                payload = response.json()
                assert payload["status"]["phase"] == ExecutionPhase.PLANNING
                assert payload["status"]["currentStep"] == "planning"
                assert scheduled == [waiting.metadata.id]

                events = await context.events.list_by_execution(waiting.metadata.id)
                assert events[-1].spec.event_type == "UserInputProvided"
                assert events[-1].spec.payload["answers"][0]["questionId"] == (
                    "target-route"
                )
                assert events[-1].spec.payload["answers"][0]["answer"] == (
                    "Use GET /health."
                )
        finally:
            context.close()

    asyncio.run(scenario())


def test_execution_cancel_action_publishes_event_and_resumes_runner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        settings = api_settings(tmp_path)
        context = create_api_context(settings)
        app = create_app(settings, api_context=context)
        app.state.maestro_auto_run_enabled = True
        transport = ASGITransport(app=app)
        scheduled: list[UUID] = []

        class FakeRunner:
            def __init__(self, **_: object) -> None:
                pass

            async def run(self, execution_id: UUID) -> None:
                scheduled.append(execution_id)

        monkeypatch.setattr("maestro.presentation.api.LocalExecutionRunner", FakeRunner)
        try:
            project = await create_ready_project(context)
            execution = await context.executions.create(
                Execution.new(name="add-health", spec=execution_spec(project))
            )
            planning = await context.executions.update_status(
                execution.metadata.id,
                ExecutionStatus(
                    observedGeneration=execution.metadata.generation,
                    phase=ExecutionPhase.PLANNING,
                    currentStep="planner",
                ),
                expected_resource_version=execution.metadata.resource_version,
            )
            waiting = await context.executions.update_status(
                planning.metadata.id,
                ExecutionStatus(
                    observedGeneration=planning.metadata.generation,
                    phase=ExecutionPhase.WAITING_FOR_PLAN_APPROVAL,
                    currentStep="plan-approval",
                ),
                expected_resource_version=planning.metadata.resource_version,
            )
            async with AsyncClient(
                transport=transport,
                base_url="http://testserver",
            ) as client:
                response = await client.post(
                    f"/api/v1/executions/{waiting.metadata.id}/actions/cancel",
                    json={
                        "resourceVersion": waiting.metadata.resource_version,
                        "actor": "local-user",
                        "requestSource": "web-ui",
                    },
                )
                await asyncio.sleep(0)

                assert response.status_code == 200
                payload = response.json()
                assert payload["spec"]["cancellationRequested"] is True
                assert scheduled == [waiting.metadata.id]

                events = await context.events.list_by_execution(waiting.metadata.id)
                assert events[-1].spec.event_type == "ExecutionCancellationRequested"
                assert events[-1].spec.producer == "web-ui"
                assert events[-1].spec.payload["actor"] == "local-user"
        finally:
            context.close()

    asyncio.run(scenario())


def test_project_creation_reconciles_local_git_status_for_browser(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        settings = api_settings(tmp_path)
        source = create_source_repository(tmp_path)
        context = create_api_context(settings)
        app = create_app(settings, api_context=context)
        transport = ASGITransport(app=app)
        try:
            async with AsyncClient(
                transport=transport,
                base_url="http://testserver",
            ) as client:
                response = await client.post(
                    "/api/v1/projects",
                    json={
                        "name": "fastapi-health",
                        "spec": project_spec(
                            "Fixture project",
                            repository_path=source,
                        ).model_dump(mode="json", by_alias=True),
                    },
                )
                payload = response.json()

                assert response.status_code == 201
                assert payload["status"]["phase"] == "Ready"
                assert payload["status"]["repositories"][0]["id"] == "backend"
                assert payload["status"]["repositories"][0]["reachable"] is True
                assert payload["status"]["repositories"][0]["gitRepository"] is True
                assert payload["status"]["repositories"][0]["clean"] is True
        finally:
            context.close()

    asyncio.run(scenario())


def test_project_readiness_requires_committed_git_head_for_browser(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        settings = api_settings(tmp_path)
        source = create_empty_source_repository(tmp_path)
        context = create_api_context(settings)
        app = create_app(settings, api_context=context)
        transport = ASGITransport(app=app)
        try:
            async with AsyncClient(
                transport=transport,
                base_url="http://testserver",
            ) as client:
                create_response = await client.post(
                    "/api/v1/projects",
                    json={
                        "name": "empty-repo",
                        "spec": project_spec(
                            "Empty repo",
                            repository_path=source,
                        ).model_dump(mode="json", by_alias=True),
                    },
                )
                payload = create_response.json()

                assert create_response.status_code == 201
                assert payload["status"]["phase"] == "Error"
                assert payload["status"]["repositories"][0]["headRevision"] is None

                project = await context.projects.get(UUID(payload["metadata"]["id"]))
                execution_response = await client.post(
                    "/api/v1/executions",
                    json={
                        "name": "add-health",
                        "spec": execution_spec(project).model_dump(
                            mode="json",
                            by_alias=True,
                        ),
                    },
                )
                assert execution_response.status_code == 409

                (source / "README.md").write_text("source\n")
                git = git_binary()
                run_git(git, source, "add", "README.md")
                run_git(git, source, "commit", "-m", "initial")

                list_response = await client.get("/api/v1/projects")
                refreshed = list_response.json()["items"][0]

                assert list_response.status_code == 200
                assert refreshed["status"]["phase"] == "Ready"
                assert refreshed["status"]["repositories"][0]["headRevision"]
        finally:
            context.close()

    asyncio.run(scenario())


def test_api_rejects_status_writes_with_problem_details(tmp_path: Path) -> None:
    async def scenario() -> None:
        settings = api_settings(tmp_path)
        context = create_api_context(settings)
        app = create_app(settings, api_context=context)
        transport = ASGITransport(app=app)
        try:
            async with AsyncClient(
                transport=transport,
                base_url="http://testserver",
            ) as client:
                response = await client.post(
                    "/api/v1/projects",
                    json={
                        "name": "tour-manager",
                        "spec": project_spec().model_dump(
                            mode="json",
                            by_alias=True,
                        ),
                        "status": {"phase": "Ready"},
                    },
                )
                payload = response.json()
                assert response.status_code == 422
                assert payload["type"].endswith("/validation-error")
                assert payload["status"] == 422
                assert payload["title"] == "Validation Error"
        finally:
            context.close()

    asyncio.run(scenario())


def test_spec_updates_use_optimistic_concurrency(tmp_path: Path) -> None:
    async def scenario() -> None:
        settings = api_settings(tmp_path)
        context = create_api_context(settings)
        app = create_app(settings, api_context=context)
        transport = ASGITransport(app=app)
        try:
            project = await context.projects.create(
                Project.new(name="tour-manager", spec=project_spec())
            )
            async with AsyncClient(
                transport=transport,
                base_url="http://testserver",
            ) as client:
                first_update = await client.patch(
                    f"/api/v1/projects/{project.metadata.id}/spec",
                    json={
                        "resourceVersion": project.metadata.resource_version,
                        "spec": project_spec("Updated").model_dump(
                            mode="json",
                            by_alias=True,
                        ),
                    },
                )
                assert first_update.status_code == 200

                stale_update = await client.patch(
                    f"/api/v1/projects/{project.metadata.id}/spec",
                    json={
                        "resourceVersion": project.metadata.resource_version,
                        "spec": project_spec("Stale").model_dump(
                            mode="json",
                            by_alias=True,
                        ),
                    },
                )
                payload = stale_update.json()
                assert stale_update.status_code == 409
                assert payload["type"].endswith("/resource-version-conflict")
                assert payload["expectedResourceVersion"] == 1
                assert payload["actualResourceVersion"] == 3
        finally:
            context.close()

    asyncio.run(scenario())


def test_list_endpoints_support_label_filtering_and_pagination(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        settings = api_settings(tmp_path)
        context = create_api_context(settings)
        app = create_app(settings, api_context=context)
        transport = ASGITransport(app=app)
        try:
            for name, team in (
                ("project-a", "platform"),
                ("project-b", "platform"),
                ("project-c", "growth"),
            ):
                project = Project.new(name=name, spec=project_spec())
                labeled = project.model_copy(
                    update={
                        "metadata": project.metadata.model_copy(
                            update={"labels": {"team": team}}
                        )
                    }
                )
                await context.projects.create(labeled)

            async with AsyncClient(
                transport=transport,
                base_url="http://testserver",
            ) as client:
                first_page = await client.get(
                    "/api/v1/projects",
                    params=[("label", "team=platform"), ("limit", "1")],
                )
                first_payload = first_page.json()
                assert first_page.status_code == 200
                assert first_payload["total"] == 2
                assert len(first_payload["items"]) == 1
                assert first_payload["nextCursor"] == "1"

                second_page = await client.get(
                    "/api/v1/projects",
                    params=[
                        ("label", "team=platform"),
                        ("limit", "1"),
                        ("cursor", first_payload["nextCursor"]),
                    ],
                )
                second_payload = second_page.json()
                assert second_page.status_code == 200
                assert len(second_payload["items"]) == 1
                assert second_payload["nextCursor"] is None
        finally:
            context.close()

    asyncio.run(scenario())


def test_approval_actions_require_exact_subject_version(tmp_path: Path) -> None:
    async def scenario() -> None:
        settings = api_settings(tmp_path)
        context = create_api_context(settings)
        app = create_app(settings, api_context=context)
        transport = ASGITransport(app=app)
        try:
            project = await create_ready_project(context)
            execution = await context.executions.create(
                Execution.new(name="add-health", spec=execution_spec(project))
            )
            approval = await context.approvals.create(
                Approval.new(
                    name="plan-approval",
                    spec=ApprovalSpec(
                        executionRef=ApprovalExecutionReference(
                            id=execution.metadata.id,
                            name=execution.metadata.name,
                        ),
                        subjectRef=ApprovalSubjectReference(
                            kind=project.kind,
                            id=project.metadata.id,
                            name=project.metadata.name,
                            resourceVersion=project.metadata.resource_version,
                        ),
                        type=ApprovalType.PLAN,
                    ),
                )
            )

            async with AsyncClient(
                transport=transport,
                base_url="http://testserver",
            ) as client:
                stale_subject = await client.post(
                    f"/api/v1/approvals/{approval.metadata.id}/actions/approve",
                    json={
                        "resourceVersion": approval.metadata.resource_version,
                        "subjectResourceVersion": (
                            project.metadata.resource_version + 1
                        ),
                        "actor": "sashka",
                    },
                )
                assert stale_subject.status_code == 409
                assert stale_subject.json()["actualResourceVersion"] == (
                    project.metadata.resource_version
                )

                approved = await client.post(
                    f"/api/v1/approvals/{approval.metadata.id}/actions/approve",
                    json={
                        "resourceVersion": approval.metadata.resource_version,
                        "subjectResourceVersion": project.metadata.resource_version,
                        "actor": "sashka",
                    },
                )
                payload = approved.json()
                assert approved.status_code == 200
                assert payload["status"]["phase"] == "Approved"
                assert payload["status"]["decisions"][0]["decision"] == "approve"
        finally:
            context.close()

    asyncio.run(scenario())


def test_execution_event_stream_uses_sse_format(tmp_path: Path) -> None:
    async def scenario() -> None:
        settings = api_settings(tmp_path)
        context = create_api_context(settings)
        app = create_app(settings, api_context=context)
        transport = ASGITransport(app=app)
        try:
            project = await create_ready_project(context)
            execution = await context.executions.create(
                Execution.new(name="add-health", spec=execution_spec(project))
            )
            subject = ResourceReference(
                kind=execution.kind,
                id=execution.metadata.id,
                name=execution.metadata.name,
            )
            await context.events.append(
                EventDraft(
                    type="ExecutionPhaseChanged",
                    producer="test",
                    correlationId="phase-1",
                    executionRef=EventExecutionReference(
                        id=execution.metadata.id,
                        name=execution.metadata.name,
                    ),
                    subjectRef=subject,
                    payload={"phase": "Planning"},
                )
            )

            async with AsyncClient(
                transport=transport,
                base_url="http://testserver",
            ) as client:
                response = await client.get(
                    f"/api/v1/executions/{execution.metadata.id}/events/stream"
                )
                assert response.status_code == 200
                assert response.headers["content-type"].startswith("text/event-stream")
                assert "event: ExecutionPhaseChanged" in response.text
                assert "data: " in response.text
        finally:
            context.close()

    asyncio.run(scenario())


def test_openapi_generation_includes_v1_paths(tmp_path: Path) -> None:
    settings = api_settings(tmp_path)
    response = asyncio.run(_get(settings, "/openapi.json"))
    payload = response.json()

    assert response.status_code == 200
    assert "/api/v1/projects" in payload["paths"]
    assert "/api/v1/admin/clear-data" in payload["paths"]
    assert "/api/v1/executions/{resource_id}/actions/start" in payload["paths"]
    assert "/api/v1/executions/{resource_id}/actions/run" in payload["paths"]
    assert "/api/v1/executions/{resource_id}/actions/respond" in payload["paths"]
    assert "/api/v1/approvals/{resource_id}/actions/approve" in payload["paths"]


def test_ui_shell_and_assets_render_browser_surface(tmp_path: Path) -> None:
    async def scenario() -> None:
        settings = api_settings(tmp_path)
        context = create_api_context(settings)
        app = create_app(settings, api_context=context)
        transport = ASGITransport(app=app)
        try:
            async with AsyncClient(
                transport=transport,
                base_url="http://testserver",
            ) as client:
                shell = await client.get("/ui")
                styles = await client.get("/ui/styles.css")
                script = await client.get("/ui/app.js")

                assert shell.status_code == 200
                assert "text/html" in shell.headers["content-type"]
                assert 'id="main"' in shell.text
                assert 'id="clear-data"' in shell.text
                assert "Clear Data" in shell.text
                assert 'id="project-form"' in shell.text
                assert "Create Project" in shell.text
                assert "New Execution" in shell.text
                assert 'id="run-execution"' in shell.text
                assert "Start" in shell.text
                assert "Approvals" in shell.text
                assert 'role="alert"' in shell.text
                assert "<label>" in shell.text
                assert styles.status_code == 200
                assert "text/css" in styles.headers["content-type"]
                assert ".workspace" in styles.text
                assert script.status_code == 200
                assert "application/javascript" in script.headers["content-type"]
                assert "clearData" in script.text
                assert "/admin/clear-data" in script.text
                assert "createProject" in script.text
                assert "runExecution" in script.text
                assert "submitUserInput" in script.text
                assert "captureUserInputDraft" in script.text
                assert "hasActiveFormControl" in script.text
                assert "window.confirm" in script.text
                assert "ExecutionCancellationRequested" in script.text
                assert 'request("/projects"' in script.text
                assert "/actions/run" in script.text
                assert "/actions/respond" in script.text
                assert "startAutoRefresh" in script.text
                assert "ExecutionRunStarted" in script.text
                assert "PlannerQuestionsProduced" in script.text
                assert "UserInputProvided" in script.text
                assert "WorkspacePreparationFailed" in script.text
                assert "VerificationSkipped" in script.text
                assert "eventSummary" in script.text
                assert "fromPhase" in script.text
                assert "EventSource" in script.text
                assert "/role-invocations" in script.text
                assert "loadArtifactContent" in script.text
        finally:
            context.close()

    asyncio.run(scenario())


def test_artifact_content_and_invocation_endpoints_feed_ui(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        settings = api_settings(tmp_path)
        context = create_api_context(settings)
        app = create_app(settings, api_context=context)
        transport = ASGITransport(app=app)
        try:
            project = await create_ready_project(context)
            execution = await context.executions.create(
                Execution.new(name="add-health", spec=execution_spec(project))
            )
            artifact = await ArtifactService(
                context.artifacts,
                context.artifact_storage,
            ).create_bytes_artifact(
                name="health-diff",
                execution_ref=ArtifactExecutionReference(
                    id=execution.metadata.id,
                    name=execution.metadata.name,
                ),
                artifact_type=ArtifactType.GIT_DIFF,
                media_type="text/x-diff",
                content=b"diff --git a/app.py b/app.py\n",
                producer=ArtifactProducer(subsystem="test"),
            )
            invocation = await context.role_invocations.create(
                RoleInvocation.new(
                    name="coding-invocation",
                    spec=RoleInvocationSpec(
                        executionRef=RoleInvocationExecutionReference(
                            id=execution.metadata.id,
                            name=execution.metadata.name,
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
                            maxSteps=12,
                            maxDurationSeconds=60,
                        ),
                    ),
                )
            )

            async with AsyncClient(
                transport=transport,
                base_url="http://testserver",
            ) as client:
                content = await client.get(
                    f"/api/v1/artifacts/{artifact.metadata.id}/content"
                )
                invocations = await client.get(
                    "/api/v1/role-invocations",
                    params={"executionId": str(execution.metadata.id)},
                )

                assert content.status_code == 200
                assert content.headers["content-type"].startswith("text/x-diff")
                assert "diff --git" in content.text
                assert invocations.status_code == 200
                payload = invocations.json()
                assert payload["items"][0]["metadata"]["id"] == str(
                    invocation.metadata.id
                )
                assert payload["items"][0]["spec"]["agentRef"]["name"] == "coder-local"
        finally:
            context.close()

    asyncio.run(scenario())
