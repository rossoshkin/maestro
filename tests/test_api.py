"""Tests for the FastAPI control-plane application."""

from __future__ import annotations

import asyncio
from pathlib import Path
from uuid import UUID

from httpx import ASGITransport, AsyncClient, Response

from maestro.config import Settings
from maestro.domain.approvals import (
    Approval,
    ApprovalExecutionReference,
    ApprovalSpec,
    ApprovalSubjectReference,
    ApprovalType,
)
from maestro.domain.events import EventDraft, EventExecutionReference
from maestro.domain.executions import (
    Execution,
    ExecutionSpec,
    ExecutionWorkflowReference,
    Goal,
    ProjectReference,
)
from maestro.domain.projects import (
    Project,
    ProjectPhase,
    ProjectSpec,
    ProjectStatus,
    WorkflowReference,
)
from maestro.domain.resources import ResourceReference
from maestro.presentation.api import create_api_context, create_app


def api_settings(tmp_path: Path) -> Settings:
    return Settings(
        database_url=f"sqlite:///{tmp_path / 'maestro.db'}",
        artifact_root=tmp_path / "artifacts",
        workspace_root=tmp_path / "workspaces",
    )


def project_spec(description: str = "Test project") -> ProjectSpec:
    return ProjectSpec(
        description=description,
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
    context: object,
    *,
    name: str = "tour-manager",
) -> Project:
    typed_context = context
    project = await typed_context.projects.create(
        Project.new(name=name, spec=project_spec())
    )
    return await typed_context.projects.update_status(
        project.metadata.id,
        ProjectStatus(phase=ProjectPhase.READY),
        expected_resource_version=project.metadata.resource_version,
    )


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
                assert payload["actualResourceVersion"] == 2
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
    assert "/api/v1/approvals/{resource_id}/actions/approve" in payload["paths"]
