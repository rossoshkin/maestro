"""FastAPI application for the Maestro control plane."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from typing import Annotated, Any, cast
from uuid import UUID

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Request, status
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from maestro import __version__
from maestro.application.approvals import ApprovalService
from maestro.application.executions import ExecutionService
from maestro.application.projects import ProjectService
from maestro.config import Settings, get_settings
from maestro.domain.agents import Agent, AgentSpec
from maestro.domain.approvals import (
    Approval,
    ApprovalActorKind,
    ApprovalDecision,
    ApprovalDecisionValue,
)
from maestro.domain.artifacts import ArtifactStorageError
from maestro.domain.events import Event
from maestro.domain.exceptions import (
    MaestroDomainError,
    ResourceAlreadyExistsError,
    ResourceConflictError,
    ResourceImmutableFieldError,
    ResourceNameNotFoundError,
    ResourceNotFoundError,
    ResourceTransitionError,
)
from maestro.domain.executions import ExecutionSpec
from maestro.domain.projects import ProjectSpec
from maestro.domain.providers import Provider, ProviderSpec
from maestro.domain.repositories import ResourceSelector
from maestro.domain.resources import BaseResource, ResourceName
from maestro.infrastructure.artifacts import LocalArtifactStorage
from maestro.infrastructure.persistence import (
    SQLiteAgentRepository,
    SQLiteApprovalRepository,
    SQLiteArtifactRepository,
    SQLiteEventStore,
    SQLiteExecutionRepository,
    SQLitePlanRepository,
    SQLiteProjectRepository,
    SQLiteProviderRepository,
    SQLiteReviewRepository,
    SQLiteRoleInvocationRepository,
    SQLiteWorkItemRepository,
)
from maestro.logging import configure_logging
from maestro.presentation.web import ui_router


class HealthResponse(BaseModel):
    """Health endpoint response."""

    status: str


class ResourceListResponse(BaseModel):
    """Paginated resource list response."""

    model_config = ConfigDict(populate_by_name=True)

    items: list[dict[str, Any]]
    next_cursor: str | None = Field(default=None, alias="nextCursor")
    total: int = Field(ge=0)


class CreateProjectRequest(BaseModel):
    """Create Project request body."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    name: ResourceName
    namespace: ResourceName = "default"
    created_by: str = Field(default="local-user", alias="createdBy")
    spec: ProjectSpec


class UpdateProjectSpecRequest(BaseModel):
    """Update Project spec request body."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    resource_version: int = Field(ge=1, alias="resourceVersion")
    spec: ProjectSpec


class CreateExecutionRequest(BaseModel):
    """Create Execution request body."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    name: ResourceName
    namespace: ResourceName = "default"
    created_by: str = Field(default="local-user", alias="createdBy")
    spec: ExecutionSpec


class UpdateExecutionSpecRequest(BaseModel):
    """Update Execution spec request body."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    resource_version: int = Field(ge=1, alias="resourceVersion")
    spec: ExecutionSpec


class ExecutionActionRequest(BaseModel):
    """Execution action request body."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    resource_version: int = Field(ge=1, alias="resourceVersion")


class CreateProviderRequest(BaseModel):
    """Create Provider request body."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    name: ResourceName
    namespace: ResourceName = "default"
    created_by: str = Field(default="local-user", alias="createdBy")
    spec: ProviderSpec


class UpdateProviderSpecRequest(BaseModel):
    """Update Provider spec request body."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    resource_version: int = Field(ge=1, alias="resourceVersion")
    spec: ProviderSpec


class CreateAgentRequest(BaseModel):
    """Create Agent request body."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    name: ResourceName
    namespace: ResourceName = "default"
    created_by: str = Field(default="local-user", alias="createdBy")
    spec: AgentSpec


class UpdateAgentSpecRequest(BaseModel):
    """Update Agent spec request body."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    resource_version: int = Field(ge=1, alias="resourceVersion")
    spec: AgentSpec


class ApprovalActionRequest(BaseModel):
    """Approval decision request body."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    resource_version: int = Field(ge=1, alias="resourceVersion")
    subject_resource_version: int = Field(ge=1, alias="subjectResourceVersion")
    actor: str = Field(min_length=1)
    actor_kind: ApprovalActorKind = Field(
        default=ApprovalActorKind.HUMAN,
        alias="actorKind",
    )
    comment: str = ""
    request_source: str = Field(default="api", alias="requestSource")


@dataclass(slots=True)
class ApiContext:
    """Runtime dependencies used by API endpoints."""

    settings: Settings
    projects: SQLiteProjectRepository
    executions: SQLiteExecutionRepository
    plans: SQLitePlanRepository
    work_items: SQLiteWorkItemRepository
    artifacts: SQLiteArtifactRepository
    artifact_storage: LocalArtifactStorage
    reviews: SQLiteReviewRepository
    approvals: SQLiteApprovalRepository
    providers: SQLiteProviderRepository
    agents: SQLiteAgentRepository
    role_invocations: SQLiteRoleInvocationRepository
    events: SQLiteEventStore

    def close(self) -> None:
        """Close repository connections owned by the API context."""

        self.projects.close()
        self.executions.close()
        self.plans.close()
        self.work_items.close()
        self.artifacts.close()
        self.reviews.close()
        self.approvals.close()
        self.providers.close()
        self.agents.close()
        self.role_invocations.close()
        self.events.close()


def create_app(
    settings: Settings | None = None,
    *,
    api_context: ApiContext | None = None,
) -> FastAPI:
    """Create and configure the Maestro FastAPI application."""

    resolved_settings = settings or get_settings()
    configure_logging(resolved_settings)

    api = FastAPI(
        title="Maestro",
        description="Local-first AI orchestration control plane.",
        version=__version__,
    )
    api.state.maestro_settings = resolved_settings
    api.state.maestro_api_context = api_context

    _install_exception_handlers(api)

    @api.get("/health/live", response_model=HealthResponse, tags=["health"])
    def liveness() -> HealthResponse:
        """Report that the API process is alive."""

        return HealthResponse(status="ok")

    @api.get("/health/ready", response_model=HealthResponse, tags=["health"])
    def readiness() -> HealthResponse:
        """Report that the API process is ready for bootstrap traffic."""

        return HealthResponse(status="ok")

    api.include_router(_v1_router())
    api.include_router(ui_router())
    return api


def _v1_router() -> APIRouter:
    router = APIRouter(prefix="/api/v1")

    @router.get("", tags=["api"])
    async def describe_api() -> dict[str, str]:
        return {"name": "maestro", "version": __version__}

    @router.get(
        "/projects",
        response_model=ResourceListResponse,
        tags=["projects"],
    )
    async def list_projects(
        context: ApiContextDep,
        namespace: NamespaceQuery = None,
        label: LabelQuery = None,
        limit: LimitQuery = 50,
        cursor: CursorQuery = None,
    ) -> ResourceListResponse:
        selector = _selector(namespace, label)
        resources = await context.projects.list(selector)
        return _list_response(resources, limit=limit, cursor=cursor)

    @router.post(
        "/projects",
        status_code=status.HTTP_201_CREATED,
        tags=["projects"],
    )
    async def create_project(
        request: CreateProjectRequest,
        context: ApiContextDep,
    ) -> dict[str, Any]:
        service = ProjectService(
            context.projects,
            forbidden_repository_roots=(
                context.settings.artifact_root,
                context.settings.workspace_root,
            ),
        )
        project = await service.create_project(
            name=request.name,
            namespace=request.namespace,
            created_by=request.created_by,
            spec=request.spec,
        )
        return _dump_resource(project)

    @router.get("/projects/{resource_id}", tags=["projects"])
    async def get_project(
        resource_id: UUID,
        context: ApiContextDep,
    ) -> dict[str, Any]:
        return _dump_resource(await context.projects.get(resource_id))

    @router.patch("/projects/{resource_id}/spec", tags=["projects"])
    async def update_project_spec(
        resource_id: UUID,
        request: UpdateProjectSpecRequest,
        context: ApiContextDep,
    ) -> dict[str, Any]:
        service = ProjectService(
            context.projects,
            forbidden_repository_roots=(
                context.settings.artifact_root,
                context.settings.workspace_root,
            ),
        )
        project = await service.update_project_spec(
            resource_id,
            request.spec,
            expected_resource_version=request.resource_version,
        )
        return _dump_resource(project)

    @router.get(
        "/executions",
        response_model=ResourceListResponse,
        tags=["executions"],
    )
    async def list_executions(
        context: ApiContextDep,
        namespace: NamespaceQuery = None,
        label: LabelQuery = None,
        limit: LimitQuery = 50,
        cursor: CursorQuery = None,
    ) -> ResourceListResponse:
        selector = _selector(namespace, label)
        resources = await context.executions.list(selector)
        return _list_response(resources, limit=limit, cursor=cursor)

    @router.post(
        "/executions",
        status_code=status.HTTP_201_CREATED,
        tags=["executions"],
    )
    async def create_execution(
        request: CreateExecutionRequest,
        context: ApiContextDep,
    ) -> dict[str, Any]:
        service = ExecutionService(context.executions, context.projects)
        execution = await service.create_execution(
            name=request.name,
            namespace=request.namespace,
            created_by=request.created_by,
            spec=request.spec,
        )
        return _dump_resource(execution)

    @router.get("/executions/{resource_id}", tags=["executions"])
    async def get_execution(
        resource_id: UUID,
        context: ApiContextDep,
    ) -> dict[str, Any]:
        return _dump_resource(await context.executions.get(resource_id))

    @router.patch("/executions/{resource_id}/spec", tags=["executions"])
    async def update_execution_spec(
        resource_id: UUID,
        request: UpdateExecutionSpecRequest,
        context: ApiContextDep,
    ) -> dict[str, Any]:
        service = ExecutionService(context.executions, context.projects)
        execution = await service.update_execution_spec(
            resource_id,
            request.spec,
            expected_resource_version=request.resource_version,
        )
        return _dump_resource(execution)

    @router.post("/executions/{resource_id}/actions/cancel", tags=["executions"])
    async def cancel_execution(
        resource_id: UUID,
        request: ExecutionActionRequest,
        context: ApiContextDep,
    ) -> dict[str, Any]:
        service = ExecutionService(context.executions, context.projects)
        execution = await service.request_cancellation(
            resource_id,
            expected_resource_version=request.resource_version,
        )
        return _dump_resource(execution)

    @router.post("/executions/{resource_id}/actions/suspend", tags=["executions"])
    async def suspend_execution(
        resource_id: UUID,
        request: ExecutionActionRequest,
        context: ApiContextDep,
    ) -> dict[str, Any]:
        service = ExecutionService(context.executions, context.projects)
        execution = await service.set_suspended(
            resource_id,
            True,
            expected_resource_version=request.resource_version,
        )
        return _dump_resource(execution)

    @router.post("/executions/{resource_id}/actions/resume", tags=["executions"])
    async def resume_execution(
        resource_id: UUID,
        request: ExecutionActionRequest,
        context: ApiContextDep,
    ) -> dict[str, Any]:
        service = ExecutionService(context.executions, context.projects)
        execution = await service.set_suspended(
            resource_id,
            False,
            expected_resource_version=request.resource_version,
        )
        return _dump_resource(execution)

    @router.get(
        "/executions/{resource_id}/events",
        response_model=ResourceListResponse,
        tags=["events"],
    )
    async def list_execution_events(
        resource_id: UUID,
        context: ApiContextDep,
        limit: LimitQuery = 50,
        cursor: CursorQuery = None,
    ) -> ResourceListResponse:
        await context.executions.get(resource_id)
        events = await context.events.list_by_execution(resource_id)
        return _list_response(events, limit=limit, cursor=cursor)

    @router.get("/executions/{resource_id}/events/stream", tags=["events"])
    async def stream_execution_events(
        resource_id: UUID,
        context: ApiContextDep,
    ) -> StreamingResponse:
        await context.executions.get(resource_id)
        events = await context.events.list_by_execution(resource_id)
        return StreamingResponse(
            _event_stream(events),
            media_type="text/event-stream",
        )

    @router.get("/plans", response_model=ResourceListResponse, tags=["plans"])
    async def list_plans(
        context: ApiContextDep,
        execution_id: ExecutionIdQuery = None,
        namespace: NamespaceQuery = None,
        label: LabelQuery = None,
        limit: LimitQuery = 50,
        cursor: CursorQuery = None,
    ) -> ResourceListResponse:
        selector = _selector(namespace, label)
        resources = (
            await context.plans.list_by_execution(execution_id)
            if execution_id is not None
            else await context.plans.list(selector)
        )
        return _list_response(
            _filter_resources(resources, selector),
            limit=limit,
            cursor=cursor,
        )

    @router.get("/plans/{resource_id}", tags=["plans"])
    async def get_plan(resource_id: UUID, context: ApiContextDep) -> dict[str, Any]:
        return _dump_resource(await context.plans.get(resource_id))

    @router.get(
        "/work-items",
        response_model=ResourceListResponse,
        tags=["work-items"],
    )
    async def list_work_items(
        context: ApiContextDep,
        execution_id: ExecutionIdQuery = None,
        namespace: NamespaceQuery = None,
        label: LabelQuery = None,
        limit: LimitQuery = 50,
        cursor: CursorQuery = None,
    ) -> ResourceListResponse:
        selector = _selector(namespace, label)
        resources = (
            await context.work_items.list_by_execution(execution_id)
            if execution_id is not None
            else await context.work_items.list(selector)
        )
        return _list_response(
            _filter_resources(resources, selector),
            limit=limit,
            cursor=cursor,
        )

    @router.get("/work-items/{resource_id}", tags=["work-items"])
    async def get_work_item(
        resource_id: UUID,
        context: ApiContextDep,
    ) -> dict[str, Any]:
        return _dump_resource(await context.work_items.get(resource_id))

    @router.get(
        "/role-invocations",
        response_model=ResourceListResponse,
        tags=["role-invocations"],
    )
    async def list_role_invocations(
        context: ApiContextDep,
        execution_id: ExecutionIdQuery = None,
        namespace: NamespaceQuery = None,
        label: LabelQuery = None,
        limit: LimitQuery = 50,
        cursor: CursorQuery = None,
    ) -> ResourceListResponse:
        selector = _selector(namespace, label)
        resources = (
            await context.role_invocations.list_by_execution(execution_id)
            if execution_id is not None
            else await context.role_invocations.list(selector)
        )
        return _list_response(
            _filter_resources(resources, selector),
            limit=limit,
            cursor=cursor,
        )

    @router.get("/role-invocations/{resource_id}", tags=["role-invocations"])
    async def get_role_invocation(
        resource_id: UUID,
        context: ApiContextDep,
    ) -> dict[str, Any]:
        return _dump_resource(await context.role_invocations.get(resource_id))

    @router.get(
        "/artifacts",
        response_model=ResourceListResponse,
        tags=["artifacts"],
    )
    async def list_artifacts(
        context: ApiContextDep,
        execution_id: ExecutionIdQuery = None,
        namespace: NamespaceQuery = None,
        label: LabelQuery = None,
        limit: LimitQuery = 50,
        cursor: CursorQuery = None,
    ) -> ResourceListResponse:
        selector = _selector(namespace, label)
        resources = (
            await context.artifacts.list_by_execution(execution_id)
            if execution_id is not None
            else await context.artifacts.list(selector)
        )
        return _list_response(
            _filter_resources(resources, selector),
            limit=limit,
            cursor=cursor,
        )

    @router.get("/artifacts/{resource_id}", tags=["artifacts"])
    async def get_artifact(
        resource_id: UUID,
        context: ApiContextDep,
    ) -> dict[str, Any]:
        return _dump_resource(await context.artifacts.get(resource_id))

    @router.get("/artifacts/{resource_id}/content", tags=["artifacts"])
    async def get_artifact_content(
        resource_id: UUID,
        context: ApiContextDep,
    ) -> Response:
        artifact = await context.artifacts.get(resource_id)
        content = await context.artifact_storage.read_bytes(artifact)
        return Response(
            content=content,
            media_type=artifact.spec.media_type,
            headers={
                "X-Maestro-Artifact-Sha256": artifact.spec.sha256,
                "X-Maestro-Artifact-Resource-Version": str(
                    artifact.metadata.resource_version
                ),
            },
        )

    @router.get("/reviews", response_model=ResourceListResponse, tags=["reviews"])
    async def list_reviews(
        context: ApiContextDep,
        execution_id: ExecutionIdQuery = None,
        namespace: NamespaceQuery = None,
        label: LabelQuery = None,
        limit: LimitQuery = 50,
        cursor: CursorQuery = None,
    ) -> ResourceListResponse:
        selector = _selector(namespace, label)
        resources = (
            await context.reviews.list_by_execution(execution_id)
            if execution_id is not None
            else await context.reviews.list(selector)
        )
        return _list_response(
            _filter_resources(resources, selector),
            limit=limit,
            cursor=cursor,
        )

    @router.get("/reviews/{resource_id}", tags=["reviews"])
    async def get_review(resource_id: UUID, context: ApiContextDep) -> dict[str, Any]:
        return _dump_resource(await context.reviews.get(resource_id))

    @router.get(
        "/approvals",
        response_model=ResourceListResponse,
        tags=["approvals"],
    )
    async def list_approvals(
        context: ApiContextDep,
        execution_id: ExecutionIdQuery = None,
        namespace: NamespaceQuery = None,
        label: LabelQuery = None,
        limit: LimitQuery = 50,
        cursor: CursorQuery = None,
    ) -> ResourceListResponse:
        selector = _selector(namespace, label)
        resources = (
            await context.approvals.list_by_execution(execution_id)
            if execution_id is not None
            else await context.approvals.list(selector)
        )
        return _list_response(
            _filter_resources(resources, selector),
            limit=limit,
            cursor=cursor,
        )

    @router.get("/approvals/{resource_id}", tags=["approvals"])
    async def get_approval(
        resource_id: UUID,
        context: ApiContextDep,
    ) -> dict[str, Any]:
        return _dump_resource(await context.approvals.get(resource_id))

    @router.post("/approvals/{resource_id}/actions/approve", tags=["approvals"])
    async def approve_resource(
        resource_id: UUID,
        request: ApprovalActionRequest,
        context: ApiContextDep,
    ) -> dict[str, Any]:
        approval = await _record_approval_decision(
            resource_id,
            request,
            ApprovalDecisionValue.APPROVE,
            context,
        )
        return _dump_resource(approval)

    @router.post("/approvals/{resource_id}/actions/reject", tags=["approvals"])
    async def reject_resource(
        resource_id: UUID,
        request: ApprovalActionRequest,
        context: ApiContextDep,
    ) -> dict[str, Any]:
        approval = await _record_approval_decision(
            resource_id,
            request,
            ApprovalDecisionValue.REJECT,
            context,
        )
        return _dump_resource(approval)

    @router.get(
        "/providers",
        response_model=ResourceListResponse,
        tags=["providers"],
    )
    async def list_providers(
        context: ApiContextDep,
        namespace: NamespaceQuery = None,
        label: LabelQuery = None,
        limit: LimitQuery = 50,
        cursor: CursorQuery = None,
    ) -> ResourceListResponse:
        selector = _selector(namespace, label)
        resources = await context.providers.list(selector)
        return _list_response(resources, limit=limit, cursor=cursor)

    @router.post(
        "/providers",
        status_code=status.HTTP_201_CREATED,
        tags=["providers"],
    )
    async def create_provider(
        request: CreateProviderRequest,
        context: ApiContextDep,
    ) -> dict[str, Any]:
        provider = await context.providers.create(
            Provider.new(
                name=request.name,
                namespace=request.namespace,
                created_by=request.created_by,
                spec=request.spec,
            )
        )
        return _dump_resource(provider)

    @router.get("/providers/{resource_id}", tags=["providers"])
    async def get_provider(
        resource_id: UUID,
        context: ApiContextDep,
    ) -> dict[str, Any]:
        return _dump_resource(await context.providers.get(resource_id))

    @router.patch("/providers/{resource_id}/spec", tags=["providers"])
    async def update_provider_spec(
        resource_id: UUID,
        request: UpdateProviderSpecRequest,
        context: ApiContextDep,
    ) -> dict[str, Any]:
        provider = await context.providers.update_spec(
            resource_id,
            request.spec,
            expected_resource_version=request.resource_version,
        )
        return _dump_resource(provider)

    @router.get("/agents", response_model=ResourceListResponse, tags=["agents"])
    async def list_agents(
        context: ApiContextDep,
        namespace: NamespaceQuery = None,
        label: LabelQuery = None,
        limit: LimitQuery = 50,
        cursor: CursorQuery = None,
    ) -> ResourceListResponse:
        selector = _selector(namespace, label)
        resources = await context.agents.list(selector)
        return _list_response(resources, limit=limit, cursor=cursor)

    @router.post(
        "/agents",
        status_code=status.HTTP_201_CREATED,
        tags=["agents"],
    )
    async def create_agent(
        request: CreateAgentRequest,
        context: ApiContextDep,
    ) -> dict[str, Any]:
        agent = await context.agents.create(
            Agent.new(
                name=request.name,
                namespace=request.namespace,
                created_by=request.created_by,
                spec=request.spec,
            )
        )
        return _dump_resource(agent)

    @router.get("/agents/{resource_id}", tags=["agents"])
    async def get_agent(resource_id: UUID, context: ApiContextDep) -> dict[str, Any]:
        return _dump_resource(await context.agents.get(resource_id))

    @router.patch("/agents/{resource_id}/spec", tags=["agents"])
    async def update_agent_spec(
        resource_id: UUID,
        request: UpdateAgentSpecRequest,
        context: ApiContextDep,
    ) -> dict[str, Any]:
        agent = await context.agents.update_spec(
            resource_id,
            request.spec,
            expected_resource_version=request.resource_version,
        )
        return _dump_resource(agent)

    return router


def create_api_context(settings: Settings) -> ApiContext:
    """Create the default SQLite-backed API dependency context."""

    database_path = _sqlite_database_path(settings.database_url)
    return ApiContext(
        settings=settings,
        projects=SQLiteProjectRepository(database_path),
        executions=SQLiteExecutionRepository(database_path),
        plans=SQLitePlanRepository(database_path),
        work_items=SQLiteWorkItemRepository(database_path),
        artifacts=SQLiteArtifactRepository(database_path),
        artifact_storage=LocalArtifactStorage(settings.artifact_root),
        reviews=SQLiteReviewRepository(database_path),
        approvals=SQLiteApprovalRepository(database_path),
        providers=SQLiteProviderRepository(database_path),
        agents=SQLiteAgentRepository(database_path),
        role_invocations=SQLiteRoleInvocationRepository(database_path),
        events=SQLiteEventStore(database_path),
    )


def _api_context(request: Request) -> ApiContext:
    context = cast(ApiContext | None, request.app.state.maestro_api_context)
    if context is not None:
        return context

    settings = cast(Settings, request.app.state.maestro_settings)
    context = create_api_context(settings)
    request.app.state.maestro_api_context = context
    return context


ApiContextDep = Annotated[ApiContext, Depends(_api_context)]
NamespaceQuery = Annotated[
    str | None,
    Query(description="Resource namespace filter."),
]
LabelQuery = Annotated[
    list[str] | None,
    Query(alias="label", description="Repeatable label selector in key=value form."),
]
LimitQuery = Annotated[int, Query(ge=1, le=100)]
CursorQuery = Annotated[str | None, Query(description="Opaque pagination cursor.")]
ExecutionIdQuery = Annotated[UUID | None, Query(alias="executionId")]


async def _record_approval_decision(
    resource_id: UUID,
    request: ApprovalActionRequest,
    decision_value: ApprovalDecisionValue,
    context: ApiContext,
) -> Approval:
    approval = await context.approvals.get(resource_id)
    if request.subject_resource_version != approval.spec.subject_ref.resource_version:
        raise ResourceConflictError(
            approval.metadata.id,
            request.subject_resource_version,
            approval.spec.subject_ref.resource_version,
        )

    service = ApprovalService(context.approvals)
    return await service.record_decision(
        resource_id,
        ApprovalDecision(
            actor=request.actor,
            actorKind=request.actor_kind,
            decision=decision_value,
            comment=request.comment,
            requestSource=request.request_source,
        ),
        expected_resource_version=request.resource_version,
    )


def _selector(namespace: str | None, labels: list[str] | None) -> ResourceSelector:
    return ResourceSelector(namespace=namespace, labels=_parse_labels(labels))


def _parse_labels(labels: list[str] | None) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for label in labels or []:
        key, separator, value = label.partition("=")
        if not key or separator != "=":
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Label filters must use key=value syntax",
            )
        if key in parsed:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Duplicate label filter: {key}",
            )
        parsed[key] = value
    return parsed


def _filter_resources[
    ResourceT: BaseResource[Any, Any],
](
    resources: Sequence[ResourceT],
    selector: ResourceSelector,
) -> tuple[ResourceT, ...]:
    return tuple(
        resource for resource in resources if _matches_selector(resource, selector)
    )


def _matches_selector(
    resource: BaseResource[Any, Any],
    selector: ResourceSelector,
) -> bool:
    namespace_matches = (
        selector.namespace is None or resource.metadata.namespace == selector.namespace
    )
    labels_match = all(
        resource.metadata.labels.get(key) == value
        for key, value in selector.labels.items()
    )
    return namespace_matches and labels_match


def _list_response(
    resources: Sequence[BaseResource[Any, Any]],
    *,
    limit: int,
    cursor: str | None,
) -> ResourceListResponse:
    offset = _cursor_offset(cursor)
    page = tuple(resources)[offset : offset + limit]
    next_offset = offset + limit
    next_cursor = str(next_offset) if next_offset < len(resources) else None
    return ResourceListResponse(
        items=[_dump_resource(resource) for resource in page],
        nextCursor=next_cursor,
        total=len(resources),
    )


def _cursor_offset(cursor: str | None) -> int:
    if cursor is None:
        return 0
    try:
        offset = int(cursor)
    except ValueError as error:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid pagination cursor",
        ) from error
    if offset < 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid pagination cursor",
        )
    return offset


def _dump_resource(resource: BaseResource[Any, Any]) -> dict[str, Any]:
    return resource.model_dump(mode="json", by_alias=True)


async def _event_stream(events: Sequence[Event]) -> AsyncIterator[str]:
    for event in events:
        payload = json.dumps(_dump_resource(event), separators=(",", ":"))
        yield (
            f"id: {event.spec.sequence}\n"
            f"event: {event.spec.event_type}\n"
            f"data: {payload}\n\n"
        )


def _sqlite_database_path(database_url: str) -> str:
    prefix = "sqlite:///"
    if not database_url.startswith(prefix):
        raise ValueError("Only sqlite:/// database URLs are supported")
    path = database_url[len(prefix) :]
    return ":memory:" if path == ":memory:" else path


def _install_exception_handlers(api: FastAPI) -> None:
    api.add_exception_handler(ResourceNotFoundError, _not_found_handler)
    api.add_exception_handler(ResourceNameNotFoundError, _name_not_found_handler)
    api.add_exception_handler(ResourceAlreadyExistsError, _already_exists_handler)
    api.add_exception_handler(ResourceConflictError, _conflict_handler)
    api.add_exception_handler(ResourceImmutableFieldError, _immutable_field_handler)
    api.add_exception_handler(ResourceTransitionError, _transition_handler)
    api.add_exception_handler(ArtifactStorageError, _artifact_storage_handler)
    api.add_exception_handler(MaestroDomainError, _domain_handler)
    api.add_exception_handler(RequestValidationError, _validation_handler)
    api.add_exception_handler(HTTPException, _http_exception_handler)
    api.add_exception_handler(ValueError, _value_error_handler)


async def _not_found_handler(
    request: Request,
    exc: Exception,
) -> JSONResponse:
    typed_exc = cast(ResourceNotFoundError, exc)
    return _problem_response(
        request,
        status_code=status.HTTP_404_NOT_FOUND,
        title="Resource Not Found",
        detail=str(typed_exc),
        problem_type="https://maestro.dev/problems/resource-not-found",
        extensions={"resourceId": str(typed_exc.resource_id)},
    )


async def _name_not_found_handler(
    request: Request,
    exc: Exception,
) -> JSONResponse:
    typed_exc = cast(ResourceNameNotFoundError, exc)
    return _problem_response(
        request,
        status_code=status.HTTP_404_NOT_FOUND,
        title="Resource Not Found",
        detail=str(typed_exc),
        problem_type="https://maestro.dev/problems/resource-not-found",
        extensions={
            "kind": typed_exc.kind,
            "namespace": typed_exc.namespace,
            "name": typed_exc.name,
        },
    )


async def _already_exists_handler(
    request: Request,
    exc: Exception,
) -> JSONResponse:
    typed_exc = cast(ResourceAlreadyExistsError, exc)
    return _problem_response(
        request,
        status_code=status.HTTP_409_CONFLICT,
        title="Resource Already Exists",
        detail=str(typed_exc),
        problem_type="https://maestro.dev/problems/resource-already-exists",
        extensions={
            "kind": typed_exc.kind,
            "namespace": typed_exc.namespace,
            "name": typed_exc.name,
        },
    )


async def _conflict_handler(
    request: Request,
    exc: Exception,
) -> JSONResponse:
    typed_exc = cast(ResourceConflictError, exc)
    return _problem_response(
        request,
        status_code=status.HTTP_409_CONFLICT,
        title="Resource Version Conflict",
        detail=str(typed_exc),
        problem_type="https://maestro.dev/problems/resource-version-conflict",
        extensions={
            "resourceId": str(typed_exc.resource_id),
            "expectedResourceVersion": typed_exc.expected_resource_version,
            "actualResourceVersion": typed_exc.actual_resource_version,
        },
    )


async def _immutable_field_handler(
    request: Request,
    exc: Exception,
) -> JSONResponse:
    typed_exc = cast(ResourceImmutableFieldError, exc)
    return _problem_response(
        request,
        status_code=status.HTTP_409_CONFLICT,
        title="Immutable Field",
        detail=str(typed_exc),
        problem_type="https://maestro.dev/problems/immutable-field",
        extensions={
            "resourceId": str(typed_exc.resource_id),
            "field": typed_exc.field_name,
        },
    )


async def _transition_handler(
    request: Request,
    exc: Exception,
) -> JSONResponse:
    typed_exc = cast(ResourceTransitionError, exc)
    return _problem_response(
        request,
        status_code=status.HTTP_409_CONFLICT,
        title="Invalid Resource Transition",
        detail=str(typed_exc),
        problem_type="https://maestro.dev/problems/invalid-transition",
        extensions={
            "resourceId": str(typed_exc.resource_id),
            "currentPhase": str(typed_exc.current_phase),
            "nextPhase": str(typed_exc.next_phase),
        },
    )


async def _artifact_storage_handler(
    request: Request,
    exc: Exception,
) -> JSONResponse:
    return _problem_response(
        request,
        status_code=status.HTTP_404_NOT_FOUND,
        title="Artifact Content Unavailable",
        detail=str(exc),
        problem_type="https://maestro.dev/problems/artifact-content-unavailable",
    )


async def _domain_handler(request: Request, exc: Exception) -> JSONResponse:
    return _problem_response(
        request,
        status_code=status.HTTP_400_BAD_REQUEST,
        title="Domain Error",
        detail=str(exc),
        problem_type="https://maestro.dev/problems/domain-error",
    )


async def _validation_handler(
    request: Request,
    exc: Exception,
) -> JSONResponse:
    typed_exc = cast(RequestValidationError, exc)
    return _problem_response(
        request,
        status_code=422,
        title="Validation Error",
        detail="Request validation failed",
        problem_type="https://maestro.dev/problems/validation-error",
        extensions={"errors": jsonable_encoder(typed_exc.errors())},
    )


async def _http_exception_handler(
    request: Request,
    exc: Exception,
) -> JSONResponse:
    typed_exc = cast(HTTPException, exc)
    detail = (
        typed_exc.detail
        if isinstance(typed_exc.detail, str)
        else json.dumps(typed_exc.detail)
    )
    return _problem_response(
        request,
        status_code=typed_exc.status_code,
        title="HTTP Error",
        detail=detail,
        problem_type="about:blank",
    )


async def _value_error_handler(request: Request, exc: Exception) -> JSONResponse:
    return _problem_response(
        request,
        status_code=status.HTTP_400_BAD_REQUEST,
        title="Invalid Request",
        detail=str(exc),
        problem_type="https://maestro.dev/problems/invalid-request",
    )


def _problem_response(
    request: Request,
    *,
    status_code: int,
    title: str,
    detail: str,
    problem_type: str,
    extensions: dict[str, Any] | None = None,
) -> JSONResponse:
    payload: dict[str, Any] = {
        "type": problem_type,
        "title": title,
        "status": status_code,
        "detail": detail,
        "instance": str(request.url.path),
    }
    payload.update(extensions or {})
    return JSONResponse(status_code=status_code, content=jsonable_encoder(payload))


app = create_app()
