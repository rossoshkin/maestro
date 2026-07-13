"""Tests for the Planner Role runtime."""

from __future__ import annotations

import asyncio
import json
from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from maestro.application.artifacts import ArtifactService
from maestro.application.planner import (
    PlannerOutputError,
    PlannerProviderError,
    PlannerRuntime,
    build_planner_input,
)
from maestro.domain.agents import (
    Agent,
    AgentCapacity,
    AgentProviderReference,
    AgentSpec,
    AgentSupportedRole,
)
from maestro.domain.artifacts import ArtifactPhase, ArtifactType
from maestro.domain.events import EventDraft
from maestro.domain.exceptions import CapabilityPolicyDeniedError
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
from maestro.domain.plans import PlanPhase
from maestro.domain.projects import (
    AgentReference,
    Project,
    ProjectRepositoryBinding,
    ProjectRoleBinding,
    ProjectSpec,
    WorkflowReference,
)
from maestro.domain.providers import (
    Provider,
    ProviderErrorCode,
    ProviderFailure,
    ProviderFeatureSet,
    ProviderHealth,
    ProviderMessage,
    ProviderModelList,
    ProviderOperationError,
    ProviderPhase,
    ProviderSpec,
    ProviderTokenUsage,
    StructuredGenerationRequest,
    StructuredGenerationResult,
    ToolLoopRequest,
    ToolLoopResult,
)
from maestro.domain.role_invocations import RoleInvocationPhase
from maestro.infrastructure.artifacts import LocalArtifactStorage
from maestro.infrastructure.persistence import (
    SQLiteArtifactRepository,
    SQLiteExecutionRepository,
    SQLitePlanRepository,
    SQLiteProjectRepository,
    SQLiteRoleInvocationRepository,
)


class RecordingProvider:
    """Capture structured generation calls and return queued outputs."""

    def __init__(
        self,
        outputs: Iterable[dict[str, Any]],
        *,
        model: str = "mock-planner",
    ) -> None:
        self.calls: list[StructuredGenerationRequest] = []
        self._outputs = deque(outputs)
        self._model = model

    async def health(self) -> ProviderHealth:
        return ProviderHealth(
            phase=ProviderPhase.READY,
            capabilities=ProviderFeatureSet(structuredOutput=True),
            availableModels=(self._model,),
        )

    async def list_models(self) -> ProviderModelList:
        return ProviderModelList(models=(self._model,))

    async def generate_structured(
        self,
        request: StructuredGenerationRequest,
    ) -> StructuredGenerationResult:
        self.calls.append(request)
        if not self._outputs:
            raise AssertionError("RecordingProvider has no queued output")
        output = self._outputs.popleft()
        return StructuredGenerationResult(
            model=request.model,
            output=output,
            rawText=json.dumps(output, sort_keys=True),
            tokenUsage=ProviderTokenUsage(
                inputTokens=_token_count(request.messages),
                outputTokens=len(json.dumps(output).split()),
            ),
        )

    async def run_tool_loop(self, request: ToolLoopRequest) -> ToolLoopResult:
        return ToolLoopResult(model=request.model, output={})


class FailingProvider(RecordingProvider):
    """Provider that fails structured generation like a real adapter can."""

    async def generate_structured(
        self,
        request: StructuredGenerationRequest,
    ) -> StructuredGenerationResult:
        self.calls.append(request)
        raise ProviderOperationError(
            ProviderFailure(
                code=ProviderErrorCode.PROVIDER_UNAVAILABLE,
                message="failed to parse grammar",
                retryable=True,
            )
        )


class RecordingPublisher:
    """Capture planner audit events."""

    def __init__(self) -> None:
        self.events: list[EventDraft] = []

    async def publish(self, draft: EventDraft) -> object:
        self.events.append(draft)
        return object()


@dataclass(slots=True)
class PlannerHarness:
    """Repositories and runtime for one planner test."""

    runtime: PlannerRuntime
    projects: SQLiteProjectRepository
    executions: SQLiteExecutionRepository
    plans: SQLitePlanRepository
    role_invocations: SQLiteRoleInvocationRepository
    artifacts: SQLiteArtifactRepository
    project: Project
    execution: Execution

    def close(self) -> None:
        self.projects.close()
        self.executions.close()
        self.plans.close()
        self.role_invocations.close()
        self.artifacts.close()


def valid_planner_output() -> dict[str, Any]:
    """Build a valid structured Planner output."""

    return {
        "summary": "Add a small health endpoint and verify it.",
        "assumptions": ("The application router already exists.",),
        "questions": (),
        "risks": (
            {
                "description": "Route wiring may live in a different module.",
                "mitigation": "Search existing routers before editing.",
            },
        ),
        "workItems": (
            {
                "id": "add-health",
                "title": "Add health endpoint",
                "roleRef": {"name": "coding", "version": "v1alpha1"},
                "repositoryRef": "backend",
                "objective": "Implement GET /health.",
                "contextRefs": (),
                "constraints": ("Keep the endpoint dependency-free.",),
                "acceptanceCriteria": ("GET /health returns 200.",),
                "verification": {"commands": ("pytest tests/test_health.py",)},
                "dependsOn": (),
                "requestedCapabilities": ("filesystem.read",),
            },
        ),
    }


def mvp_planner_output_without_negative_guardrail() -> dict[str, Any]:
    """Build MVP-style output that omits negative criteria from WorkItems."""

    return {
        "summary": "Create a minimal FastAPI application.",
        "assumptions": (),
        "questions": (),
        "risks": (),
        "workItems": (
            {
                "id": "add-health",
                "title": "Add health endpoint",
                "roleRef": {"name": "coding", "version": "v1alpha1"},
                "repositoryRef": "backend",
                "objective": ('Implement GET /health returning {"status":"ok"}.'),
                "acceptanceCriteria": ('GET /health returns {"status":"ok"}.',),
                "requestedCapabilities": ("filesystem.read", "filesystem.write"),
            },
            {
                "id": "add-test",
                "title": "Add automated test",
                "roleRef": {"name": "coding", "version": "v1alpha1"},
                "repositoryRef": "backend",
                "objective": "Add one automated test for GET /health.",
                "acceptanceCriteria": ("One automated test is added.",),
                "dependsOn": ("add-health",),
                "requestedCapabilities": ("filesystem.read", "filesystem.write"),
            },
            {
                "id": "add-readme",
                "title": "Add README instructions",
                "roleRef": {"name": "coding", "version": "v1alpha1"},
                "repositoryRef": "backend",
                "objective": "Add README run instructions.",
                "acceptanceCriteria": ("README contains run instructions.",),
                "dependsOn": ("add-health",),
                "requestedCapabilities": ("filesystem.read", "filesystem.write"),
            },
        ),
    }


def blocking_question_output() -> dict[str, Any]:
    """Build Planner output that asks for user input before planning."""

    return {
        "summary": "The goal needs one decision before planning.",
        "assumptions": (),
        "questions": (
            {
                "id": "target-route",
                "question": "Which HTTP path should expose the health endpoint?",
                "blocking": True,
            },
        ),
        "risks": (),
        "workItems": (),
    }


def invalid_planner_output() -> dict[str, Any]:
    """Build output that parses but cannot produce a valid Plan."""

    return {
        "summary": "No concrete work was proposed.",
        "assumptions": (),
        "questions": (),
        "risks": (),
        "workItems": (),
    }


def inspection_only_planner_output() -> dict[str, Any]:
    """Build a semantically weak plan for an implementation goal."""

    return {
        "summary": "Check whether FastAPI is available.",
        "assumptions": (),
        "questions": (),
        "risks": (),
        "workItems": (
            {
                "id": "check-fastapi-installed",
                "title": "Check FastAPI installation",
                "roleRef": {"name": "coding", "version": "v1alpha1"},
                "repositoryRef": "backend",
                "objective": "Check if FastAPI is installed.",
                "acceptanceCriteria": ("FastAPI availability is known.",),
                "dependsOn": (),
                "requestedCapabilities": ("filesystem.read",),
            },
        ),
    }


def recoverable_planner_output() -> dict[str, Any]:
    """Build Planner output with common model formatting drift."""

    return {
        "summary": "Create a small FastAPI app and verify it.",
        "assumptions": ("The repository can host a minimal Python app.",),
        "questions": (
            {
                "id": "project_structure",
                "question": "Should the app use src/ layout?",
                "blocking": False,
            },
        ),
        "risks": (),
        "workItems": (
            {
                "id": "create_fastapi_app",
                "title": "Create FastAPI app",
                "roleRef": {"name": "coding", "version": "v1"},
                "repositoryRef": "backend",
                "objective": "Create a FastAPI application.",
                "acceptanceCriteria": ("Application exposes GET /health.",),
                "dependsOn": (),
                "requestedCapabilities": ("coding_fastapi", "python3_8+"),
            },
            {
                "id": "add_automated_test",
                "title": "Add automated test",
                "roleRef": {"name": "coding", "version": "v1alpha1"},
                "repositoryRef": "backend",
                "objective": "Add automated test coverage.",
                "acceptanceCriteria": ("Tests assert GET /health returns 200.",),
                "dependsOn": ("create_fastapi_app",),
                "requestedCapabilities": ("filesystem_write",),
            },
            {
                "id": "verify_no_database_auth",
                "title": "Verify no database or auth",
                "roleRef": {"name": "coding_review", "version": "v1alpha1"},
                "repositoryRef": "backend",
                "objective": "Check the app stays minimal.",
                "acceptanceCriteria": ("No database or authentication is added.",),
                "dependsOn": ("create_fastapi_app", "add_automated_test"),
                "requestedCapabilities": ("coding_review",),
            },
        ),
    }


def omitted_dependency_planner_output() -> dict[str, Any]:
    """Build Planner output that omits obvious follow-up dependencies."""

    return {
        "summary": "Create a minimal FastAPI application.",
        "assumptions": (),
        "questions": (),
        "risks": (),
        "workItems": (
            {
                "id": "add-health-endpoint",
                "title": "Add /health endpoint",
                "roleRef": {"name": "coding", "version": "v1alpha1"},
                "repositoryRef": "backend",
                "objective": (
                    'Implement a GET /health endpoint that returns {"status":"ok"}.'
                ),
                "acceptanceCriteria": ('GET /health returns {"status":"ok"}.',),
                "dependsOn": (),
                "requestedCapabilities": (
                    "filesystem.read",
                    "filesystem.write",
                    "shell.execute.test",
                ),
            },
            {
                "id": "add-automated-test",
                "title": "Add one automated test",
                "roleRef": {"name": "coding", "version": "v1alpha1"},
                "repositoryRef": "backend",
                "objective": "Create an automated test for the /health endpoint.",
                "acceptanceCriteria": ("One automated test is added.",),
                "dependsOn": (),
                "requestedCapabilities": (
                    "filesystem.read",
                    "filesystem.write",
                    "shell.execute.test",
                ),
            },
            {
                "id": "add-readme-instructions",
                "title": "Add README instructions",
                "roleRef": {"name": "coding", "version": "v1alpha1"},
                "repositoryRef": "backend",
                "objective": (
                    "Create a README file with run instructions for the FastAPI "
                    "application."
                ),
                "acceptanceCriteria": ("README contains run instructions.",),
                "dependsOn": (),
                "requestedCapabilities": ("filesystem.read", "filesystem.write"),
            },
        ),
    }


def test_build_planner_input_includes_goal_project_context_and_policy(
    tmp_path: Path,
) -> None:
    project = project_resource(tmp_path)
    execution = execution_resource(project)

    planner_input = build_planner_input(
        execution,
        project,
        repository_context={"files": ("README.md", "pyproject.toml")},
        knowledge_context={"docs": ("architecture",)},
    )

    assert planner_input["goal"]["summary"] == "Add health endpoint"
    assert planner_input["project"]["repositories"][0]["id"] == "backend"
    assert planner_input["repositoryContext"]["files"] == (
        "README.md",
        "pyproject.toml",
    )
    assert planner_input["knowledgeContext"]["docs"] == ("architecture",)
    assert planner_input["workflowContext"]["permittedRoleRefs"] == (
        "planner",
        "coding",
        "reviewer",
    )
    assert (
        "Planner may only produce a Plan"
        in planner_input["workflowContext"]["policySummary"]
    )


def test_planner_creates_plan_artifacts_and_role_invocation_records(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        provider_runtime = RecordingProvider((valid_planner_output(),))
        publisher = RecordingPublisher()
        harness = await make_harness(tmp_path, publisher=publisher)

        result = await harness.runtime.invoke_planner(
            harness.execution.metadata.id,
            agent=agent_resource(),
            provider=provider_resource(),
            runtime=provider_runtime,
            granted_capabilities=("filesystem.read", "knowledge.search"),
        )

        plans = await harness.plans.list_by_execution(harness.execution.metadata.id)
        artifacts = await harness.artifacts.list_by_execution(
            harness.execution.metadata.id
        )
        invocations = await harness.role_invocations.list_by_execution(
            harness.execution.metadata.id
        )
        invocation = invocations[0]

        assert result.plan_ref is not None
        assert result.plan_artifact_ref is not None
        assert result.repair_attempted is False
        assert len(provider_runtime.calls) == 1
        assert len(plans) == 1
        assert plans[0].status.phase == PlanPhase.DRAFT
        assert plans[0].spec.work_items[0].id == "add-health"
        assert invocation.status.phase == RoleInvocationPhase.SUCCEEDED
        assert invocation.spec.work_item_ref is None
        assert invocation.spec.granted_capabilities == (
            "filesystem.read",
            "knowledge.search",
        )
        assert invocation.spec.limits.max_steps == (
            harness.execution.spec.limits.max_tool_calls_per_invocation
        )
        assert invocation.status.prompt_artifact_ref is not None
        assert invocation.status.response_artifact_ref is not None
        assert len(invocation.status.output_artifact_refs) == 1
        assert all(
            artifact.status.phase == ArtifactPhase.AVAILABLE for artifact in artifacts
        )
        assert {artifact.spec.artifact_type for artifact in artifacts} == {
            ArtifactType.PROMPT,
            ArtifactType.MODEL_RESPONSE,
            ArtifactType.PLAN,
        }
        assert all(
            artifact.spec.producer.role_invocation_ref is not None
            for artifact in artifacts
        )
        assert publisher.events[0].event_type == "PlanProduced"
        harness.close()

    asyncio.run(scenario())


def test_planner_normalizes_recoverable_model_output(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        provider_runtime = RecordingProvider((recoverable_planner_output(),))
        harness = await make_harness(tmp_path)

        result = await harness.runtime.invoke_planner(
            harness.execution.metadata.id,
            agent=agent_resource(),
            provider=provider_resource(),
            runtime=provider_runtime,
            granted_capabilities=("filesystem.read",),
        )

        plans = await harness.plans.list_by_execution(harness.execution.metadata.id)
        plan = plans[0]

        assert result.plan_ref is not None
        assert result.repair_attempted is False
        assert result.questions[0].id == "project-structure"
        assert tuple(item.id for item in plan.spec.work_items) == (
            "create-fastapi-app",
            "add-automated-test",
            "verify-no-database-auth",
        )
        assert plan.spec.work_items[1].depends_on == ("create-fastapi-app",)
        assert plan.spec.work_items[2].depends_on == (
            "create-fastapi-app",
            "add-automated-test",
        )
        assert plan.spec.work_items[0].requested_capabilities == ()
        assert plan.spec.work_items[1].requested_capabilities == ("filesystem.write",)
        assert plan.spec.work_items[2].role_ref.name == "coding"
        assert all(item.role_ref.version == "v1alpha1" for item in plan.spec.work_items)
        assert plan.status.phase == PlanPhase.DRAFT
        harness.close()

    asyncio.run(scenario())


def test_planner_infers_follow_up_dependencies_when_model_omits_them(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        provider_runtime = RecordingProvider((omitted_dependency_planner_output(),))
        harness = await make_harness(tmp_path)

        result = await harness.runtime.invoke_planner(
            harness.execution.metadata.id,
            agent=agent_resource(),
            provider=provider_resource(),
            runtime=provider_runtime,
            granted_capabilities=("filesystem.read",),
        )

        plans = await harness.plans.list_by_execution(harness.execution.metadata.id)
        plan = plans[0]

        assert result.plan_ref is not None
        assert tuple(item.id for item in plan.spec.work_items) == (
            "add-health-endpoint",
            "add-automated-test",
            "add-readme-instructions",
        )
        assert plan.spec.work_items[0].depends_on == ()
        assert plan.spec.work_items[1].depends_on == ("add-health-endpoint",)
        assert plan.spec.work_items[2].depends_on == ("add-health-endpoint",)
        harness.close()

    asyncio.run(scenario())


def test_planner_retries_once_with_repair_prompt_after_invalid_output(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        harness = await make_harness(tmp_path)
        provider_runtime = RecordingProvider(
            (invalid_planner_output(), valid_planner_output())
        )

        result = await harness.runtime.invoke_planner(
            harness.execution.metadata.id,
            agent=agent_resource(),
            provider=provider_resource(),
            runtime=provider_runtime,
            granted_capabilities=("filesystem.read",),
        )

        artifacts = await harness.artifacts.list_by_execution(
            harness.execution.metadata.id
        )

        assert result.repair_attempted is True
        assert len(provider_runtime.calls) == 2
        assert "repairInstructions" in provider_runtime.calls[1].messages[1].content
        assert (
            sum(
                artifact.spec.artifact_type == ArtifactType.PROMPT
                for artifact in artifacts
            )
            == 2
        )
        assert (
            sum(
                artifact.spec.artifact_type == ArtifactType.MODEL_RESPONSE
                for artifact in artifacts
            )
            == 2
        )
        assert (
            sum(
                artifact.spec.artifact_type == ArtifactType.PLAN
                for artifact in artifacts
            )
            == 1
        )
        harness.close()

    asyncio.run(scenario())


def test_planner_repairs_semantically_weak_plan_before_creating_plan(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        harness = await make_harness(tmp_path)
        provider_runtime = RecordingProvider(
            (inspection_only_planner_output(), valid_planner_output())
        )

        result = await harness.runtime.invoke_planner(
            harness.execution.metadata.id,
            agent=agent_resource(),
            provider=provider_resource(),
            runtime=provider_runtime,
            granted_capabilities=("filesystem.read",),
        )

        plans = await harness.plans.list_by_execution(harness.execution.metadata.id)

        assert result.repair_attempted is True
        assert len(provider_runtime.calls) == 2
        assert "PlanQualityInvalid" in provider_runtime.calls[1].messages[1].content
        assert len(plans) == 1
        assert plans[0].spec.work_items[0].id == "add-health"
        harness.close()

    asyncio.run(scenario())


def test_planner_raises_after_one_repair_attempt_for_invalid_output(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        harness = await make_harness(tmp_path)
        provider_runtime = RecordingProvider(
            (invalid_planner_output(), invalid_planner_output())
        )

        with pytest.raises(PlannerOutputError):
            await harness.runtime.invoke_planner(
                harness.execution.metadata.id,
                agent=agent_resource(),
                provider=provider_resource(),
                runtime=provider_runtime,
                granted_capabilities=("filesystem.read",),
            )

        plans = await harness.plans.list_by_execution(harness.execution.metadata.id)
        artifacts = await harness.artifacts.list_by_execution(
            harness.execution.metadata.id
        )
        invocation = (
            await harness.role_invocations.list_by_execution(
                harness.execution.metadata.id
            )
        )[0]

        assert len(provider_runtime.calls) == 2
        assert plans == ()
        assert invocation.status.phase == RoleInvocationPhase.FAILED
        assert invocation.status.failure is not None
        assert invocation.status.failure.reason == "PlannerOutputInvalid"
        assert (
            sum(
                artifact.spec.artifact_type == ArtifactType.PROMPT
                for artifact in artifacts
            )
            == 2
        )
        assert (
            sum(
                artifact.spec.artifact_type == ArtifactType.MODEL_RESPONSE
                for artifact in artifacts
            )
            == 2
        )
        harness.close()

    asyncio.run(scenario())


def test_planner_rejects_repeated_semantically_weak_plan(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        harness = await make_harness(tmp_path)
        provider_runtime = RecordingProvider(
            (inspection_only_planner_output(), inspection_only_planner_output())
        )

        with pytest.raises(PlannerOutputError):
            await harness.runtime.invoke_planner(
                harness.execution.metadata.id,
                agent=agent_resource(),
                provider=provider_resource(),
                runtime=provider_runtime,
                granted_capabilities=("filesystem.read",),
            )

        plans = await harness.plans.list_by_execution(harness.execution.metadata.id)
        invocation = (
            await harness.role_invocations.list_by_execution(
                harness.execution.metadata.id
            )
        )[0]

        assert plans == ()
        assert invocation.status.phase == RoleInvocationPhase.FAILED
        assert invocation.status.failure is not None
        assert invocation.status.failure.reason == "PlannerOutputInvalid"
        assert "PlanQualityInvalid" in invocation.status.failure.message
        harness.close()

    asyncio.run(scenario())


def test_planner_accepts_negative_acceptance_criteria_as_guardrails(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        goal = Goal(
            summary="Create a minimal FastAPI application.",
            description=(
                'GET /health returns {"status":"ok"}, add one automated '
                "test, add README instructions, and do not add a database or "
                "authentication."
            ),
            acceptanceCriteria=(
                'GET /health returns {"status":"ok"}.',
                "One automated test is added.",
                "README contains run instructions.",
                "No database or authentication is added.",
            ),
        )
        harness = await make_harness(tmp_path, goal=goal)
        provider_runtime = RecordingProvider(
            (mvp_planner_output_without_negative_guardrail(),)
        )

        result = await harness.runtime.invoke_planner(
            harness.execution.metadata.id,
            agent=agent_resource(),
            provider=provider_resource(),
            runtime=provider_runtime,
            granted_capabilities=("filesystem.read",),
        )

        plan = await harness.plans.get(result.plan_ref.id)

        assert len(provider_runtime.calls) == 1
        assert plan.spec.work_items
        assert all(
            "No database or authentication is added." in work_item.constraints
            for work_item in plan.spec.work_items
        )
        harness.close()

    asyncio.run(scenario())


def test_planner_marks_provider_failure_and_allows_retry(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        harness = await make_harness(tmp_path)
        failing_provider = FailingProvider(())

        with pytest.raises(PlannerProviderError):
            await harness.runtime.invoke_planner(
                harness.execution.metadata.id,
                agent=agent_resource(),
                provider=provider_resource(),
                runtime=failing_provider,
                granted_capabilities=("filesystem.read",),
            )

        first_invocation = (
            await harness.role_invocations.list_by_execution(
                harness.execution.metadata.id
            )
        )[0]
        assert first_invocation.status.phase == RoleInvocationPhase.FAILED
        assert first_invocation.status.failure is not None
        assert first_invocation.status.failure.reason == "PlannerProviderFailed"

        retry_provider = RecordingProvider((valid_planner_output(),))
        result = await harness.runtime.invoke_planner(
            harness.execution.metadata.id,
            agent=agent_resource(),
            provider=provider_resource(),
            runtime=retry_provider,
            granted_capabilities=("filesystem.read",),
        )

        invocations = await harness.role_invocations.list_by_execution(
            harness.execution.metadata.id
        )
        assert result.plan_ref is not None
        assert len(invocations) == 2
        assert invocations[0].metadata.name != invocations[1].metadata.name
        assert invocations[1].status.phase == RoleInvocationPhase.SUCCEEDED
        harness.close()

    asyncio.run(scenario())


def test_planner_routes_blocking_questions_to_waiting_for_user_input(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        provider_runtime = RecordingProvider((blocking_question_output(),))
        publisher = RecordingPublisher()
        harness = await make_harness(tmp_path, publisher=publisher)

        result = await harness.runtime.invoke_planner(
            harness.execution.metadata.id,
            agent=agent_resource(),
            provider=provider_resource(),
            runtime=provider_runtime,
            granted_capabilities=("filesystem.read",),
        )

        updated_execution = await harness.executions.get(harness.execution.metadata.id)
        plans = await harness.plans.list_by_execution(harness.execution.metadata.id)
        artifacts = await harness.artifacts.list_by_execution(
            harness.execution.metadata.id
        )
        invocation = (
            await harness.role_invocations.list_by_execution(
                harness.execution.metadata.id
            )
        )[0]

        assert result.plan_ref is None
        assert result.questions[0].id == "target-route"
        assert updated_execution.status.phase == ExecutionPhase.WAITING_FOR_USER_INPUT
        assert updated_execution.status.current_step == "planner-questions"
        assert plans == ()
        assert invocation.status.phase == RoleInvocationPhase.SUCCEEDED
        assert invocation.status.output_artifact_refs == ()
        assert {artifact.spec.artifact_type for artifact in artifacts} == {
            ArtifactType.PROMPT,
            ArtifactType.MODEL_RESPONSE,
        }
        assert publisher.events[0].event_type == "PlannerQuestionsProduced"
        harness.close()

    asyncio.run(scenario())


def test_planner_rejects_write_and_shell_capabilities_before_invocation(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        harness = await make_harness(tmp_path)
        provider_runtime = RecordingProvider((valid_planner_output(),))

        with pytest.raises(CapabilityPolicyDeniedError) as error:
            await harness.runtime.invoke_planner(
                harness.execution.metadata.id,
                agent=agent_resource(),
                provider=provider_resource(),
                runtime=provider_runtime,
                granted_capabilities=("filesystem.write", "shell.execute"),
            )

        assert error.value.reason == "PlannerForbiddenCapability"
        assert provider_runtime.calls == []
        assert (
            await harness.role_invocations.list_by_execution(
                harness.execution.metadata.id
            )
            == ()
        )
        assert (
            await harness.artifacts.list_by_execution(harness.execution.metadata.id)
            == ()
        )
        harness.close()

    asyncio.run(scenario())


async def make_harness(
    tmp_path: Path,
    *,
    publisher: RecordingPublisher | None = None,
    goal: Goal | None = None,
) -> PlannerHarness:
    projects = SQLiteProjectRepository(":memory:")
    executions = SQLiteExecutionRepository(":memory:")
    plans = SQLitePlanRepository(":memory:")
    role_invocations = SQLiteRoleInvocationRepository(":memory:")
    artifacts = SQLiteArtifactRepository(":memory:")
    artifact_service = ArtifactService(
        artifacts,
        LocalArtifactStorage(tmp_path / "artifacts"),
    )
    runtime = PlannerRuntime(
        execution_repository=executions,
        project_repository=projects,
        plan_repository=plans,
        role_invocation_repository=role_invocations,
        artifact_service=artifact_service,
        event_publisher=publisher,
    )
    project = await projects.create(project_resource(tmp_path))
    execution = await executions.create(
        execution_resource_with_goal(project, goal)
        if goal is not None
        else execution_resource(project)
    )
    planning_execution = await executions.update_status(
        execution.metadata.id,
        ExecutionStatus(
            observedGeneration=execution.metadata.generation,
            phase=ExecutionPhase.PLANNING,
            currentStep="planner",
        ),
        expected_resource_version=execution.metadata.resource_version,
    )
    return PlannerHarness(
        runtime=runtime,
        projects=projects,
        executions=executions,
        plans=plans,
        role_invocations=role_invocations,
        artifacts=artifacts,
        project=project,
        execution=planning_execution,
    )


def project_resource(tmp_path: Path) -> Project:
    return Project.new(
        name="tour-manager",
        spec=ProjectSpec(
            description="Test project",
            repositories=(
                ProjectRepositoryBinding(
                    id="backend",
                    path=tmp_path / "backend",
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


def execution_resource(project: Project) -> Execution:
    return execution_resource_with_goal(
        project,
        Goal(
            summary="Add health endpoint",
            description="Create GET /health.",
            acceptanceCriteria=("GET /health returns 200.",),
        ),
    )


def execution_resource_with_goal(project: Project, goal: Goal) -> Execution:
    return Execution.new(
        name="add-health-endpoint",
        spec=ExecutionSpec(
            projectRef=ProjectReference(
                id=project.metadata.id,
                name=project.metadata.name,
            ),
            goal=goal,
            workflowRef=ExecutionWorkflowReference(
                name="software-delivery",
                version="v1alpha1",
            ),
            requestedRoles=("planner", "coding", "reviewer"),
            limits=ExecutionLimits(
                maxCodingIterations=2,
                maxReviewIterations=1,
                maxDurationSeconds=300,
                maxToolCallsPerInvocation=12,
            ),
        ),
    )


def provider_resource(*, model: str = "mock-planner") -> Provider:
    return Provider.new(
        name="ollama-local",
        spec=ProviderSpec(
            type="ollama",
            endpoint="http://127.0.0.1:11434",
            allowedModels=(model,),
            timeoutSeconds=30,
        ),
    )


def agent_resource(*, model: str = "mock-planner") -> Agent:
    return Agent.new(
        name="planner-local",
        spec=AgentSpec(
            providerRef=AgentProviderReference(name="ollama-local"),
            model=model,
            supportedRoles=(
                AgentSupportedRole(name="planner", versions=("v1alpha1",)),
            ),
            capacity=AgentCapacity(maxConcurrentAssignments=1),
        ),
    )


def _token_count(messages: tuple[ProviderMessage, ...]) -> int:
    return sum(len(message.content.split()) for message in messages)
