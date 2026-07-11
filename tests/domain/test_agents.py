"""Tests for Agent resource validation, readiness and Role compatibility."""

from uuid import uuid4

import pytest
from pydantic import ValidationError

from maestro.domain.agents import (
    Agent,
    AgentCapacity,
    AgentCompatibilityReason,
    AgentPhase,
    AgentProviderReference,
    AgentReadinessReason,
    AgentScheduling,
    AgentSpec,
    AgentStatus,
    AgentSupportedRole,
    ProviderReadinessPhase,
    ProviderReadinessSnapshot,
    evaluate_agent_readiness,
    evaluate_agent_role_compatibility,
)
from maestro.domain.resources import Metadata, OwnerReference
from maestro.domain.roles import (
    Role,
    RoleExecutionPolicy,
    RolePhase,
    RolePromptReference,
    RoleSpec,
    RoleStatus,
    RoleValidationResult,
)


def valid_role_spec(*, version: str = "v1alpha1") -> RoleSpec:
    """Build a valid RoleSpec for compatibility tests."""

    return RoleSpec(
        version=version,
        purpose="Implement one software Work Item",
        inputSchemaRef="CodingInput/v1",
        outputSchemaRef="CodingResult/v1",
        promptRef=RolePromptReference(name="coding-prompt-v1", version=version),
        requiredCapabilities=("filesystem.read", "filesystem.write"),
        prohibitedCapabilities=("workflow.transition",),
        executionPolicy=RoleExecutionPolicy(maxSteps=40),
    )


def ready_role(name: str = "coding", *, version: str = "v1alpha1") -> Role:
    """Build a Ready Role resource."""

    role = Role.new(name=name, spec=valid_role_spec(version=version))
    return Role(
        metadata=role.metadata,
        spec=role.spec,
        status=RoleStatus(
            observedGeneration=1,
            phase=RolePhase.READY,
            validation=RoleValidationResult(valid=True),
        ),
    )


def valid_agent_spec() -> AgentSpec:
    """Build a valid AgentSpec for tests."""

    return AgentSpec(
        providerRef=AgentProviderReference(name="ollama-local"),
        model="qwen2.5-coder:14b",
        supportedRoles=(
            AgentSupportedRole(name="coding", versions=("v1alpha1", "v1alpha2")),
            AgentSupportedRole(name="reviewer", versions=("v1alpha1",)),
        ),
        capabilityBindings=(
            {"kind": "CapabilityBinding", "name": "local-workspace-safe"},
        ),
        capacity=AgentCapacity(maxConcurrentAssignments=2),
        scheduling=AgentScheduling(priority=100, enabled=True),
    )


def valid_agent() -> Agent:
    """Build a valid Agent resource."""

    return Agent.new(name="coder-local", spec=valid_agent_spec())


def ready_provider(
    *,
    phase: ProviderReadinessPhase = ProviderReadinessPhase.READY,
    provider_name: str = "ollama-local",
    models: tuple[str, ...] = ("qwen2.5-coder:14b",),
) -> ProviderReadinessSnapshot:
    """Build a provider readiness snapshot."""

    return ProviderReadinessSnapshot(
        providerRef=AgentProviderReference(name=provider_name),
        phase=phase,
        availableModels=models,
    )


def test_agent_serializes_and_deserializes() -> None:
    agent = valid_agent()

    payload = agent.model_dump(mode="json", by_alias=True)
    round_tripped = Agent.model_validate(payload)

    assert payload["kind"] == "Agent"
    assert payload["spec"]["providerRef"]["name"] == "ollama-local"
    assert payload["spec"]["supportedRoles"][0]["name"] == "coding"
    assert round_tripped == agent


def test_agent_rejects_controller_owner_references() -> None:
    with pytest.raises(ValidationError):
        Agent(
            metadata=Metadata(
                name="coder-local",
                ownerReferences=(
                    OwnerReference(kind="Project", id=uuid4(), controller=True),
                ),
            ),
            spec=valid_agent_spec(),
            status=AgentStatus(),
        )


def test_supported_roles_must_be_unique() -> None:
    with pytest.raises(ValidationError):
        AgentSpec(
            providerRef=AgentProviderReference(name="ollama-local"),
            model="qwen2.5-coder:14b",
            supportedRoles=(
                AgentSupportedRole(name="coding", versions=("v1alpha1",)),
                AgentSupportedRole(name="coding", versions=("v1alpha2",)),
            ),
        )


def test_supported_role_versions_must_be_unique() -> None:
    with pytest.raises(ValidationError):
        AgentSupportedRole(name="coding", versions=("v1alpha1", "v1alpha1"))


def test_provider_reference_does_not_embed_provider_implementation() -> None:
    with pytest.raises(ValidationError):
        AgentProviderReference.model_validate(
            {
                "kind": "Provider",
                "name": "ollama-local",
                "type": "ollama",
                "endpoint": "http://127.0.0.1:11434",
            }
        )


def test_agent_spec_does_not_store_project_knowledge() -> None:
    payload = valid_agent_spec().model_dump(mode="json", by_alias=True)
    payload["projectRef"] = {"kind": "Project", "name": "tour-manager"}
    payload["knowledgeBindings"] = [{"kind": "KnowledgeSource", "name": "docs"}]

    with pytest.raises(ValidationError):
        AgentSpec.model_validate(payload)


def test_invalid_model_identifier_is_rejected() -> None:
    with pytest.raises(ValidationError):
        AgentSpec(
            providerRef=AgentProviderReference(name="ollama-local"),
            model=" bad model ",
            supportedRoles=(AgentSupportedRole(name="coding", versions=("v1alpha1",)),),
        )


def test_agent_supports_ready_role_version() -> None:
    decision = evaluate_agent_role_compatibility(valid_agent(), ready_role())

    assert decision.compatible is True
    assert decision.reason == AgentCompatibilityReason.COMPATIBLE


def test_agent_rejects_unsupported_role_name() -> None:
    decision = evaluate_agent_role_compatibility(
        valid_agent(),
        ready_role("planner"),
    )

    assert decision.compatible is False
    assert decision.reason == AgentCompatibilityReason.ROLE_NOT_SUPPORTED


def test_agent_rejects_unsupported_role_version() -> None:
    decision = evaluate_agent_role_compatibility(
        valid_agent(),
        ready_role("coding", version="v2"),
    )

    assert decision.compatible is False
    assert decision.reason == AgentCompatibilityReason.ROLE_VERSION_NOT_SUPPORTED


def test_agent_rejects_role_that_is_not_ready() -> None:
    role = Role.new(name="coding", spec=valid_role_spec())

    decision = evaluate_agent_role_compatibility(valid_agent(), role)

    assert decision.compatible is False
    assert decision.reason == AgentCompatibilityReason.ROLE_NOT_READY


def test_ready_provider_and_available_model_make_agent_ready() -> None:
    decision = evaluate_agent_readiness(valid_agent(), ready_provider())

    assert decision.phase == AgentPhase.READY
    assert decision.ready is True
    assert decision.can_accept_assignment is True
    assert decision.reason == AgentReadinessReason.READY


def test_unavailable_provider_makes_agent_unavailable() -> None:
    decision = evaluate_agent_readiness(
        valid_agent(),
        ready_provider(phase=ProviderReadinessPhase.UNAVAILABLE),
    )

    assert decision.phase == AgentPhase.UNAVAILABLE
    assert decision.ready is False
    assert decision.reason == AgentReadinessReason.PROVIDER_UNAVAILABLE


def test_degraded_provider_makes_agent_degraded() -> None:
    decision = evaluate_agent_readiness(
        valid_agent(),
        ready_provider(phase=ProviderReadinessPhase.DEGRADED),
    )

    assert decision.phase == AgentPhase.DEGRADED
    assert decision.ready is False
    assert decision.reason == AgentReadinessReason.PROVIDER_DEGRADED


def test_missing_model_makes_agent_unavailable() -> None:
    decision = evaluate_agent_readiness(
        valid_agent(),
        ready_provider(models=("qwen3:14b",)),
    )

    assert decision.phase == AgentPhase.UNAVAILABLE
    assert decision.model_available is False
    assert decision.reason == AgentReadinessReason.MODEL_UNAVAILABLE


def test_disabled_agent_cannot_accept_assignments() -> None:
    agent = Agent.new(
        name="coder-local",
        spec=valid_agent_spec().model_copy(
            update={"scheduling": AgentScheduling(enabled=False)}
        ),
    )

    decision = evaluate_agent_readiness(agent, ready_provider())

    assert decision.phase == AgentPhase.DISABLED
    assert decision.can_accept_assignment is False
    assert decision.reason == AgentReadinessReason.DISABLED


def test_agent_at_capacity_is_busy() -> None:
    agent = valid_agent()
    busy_agent = Agent(
        metadata=agent.metadata,
        spec=agent.spec,
        status=AgentStatus(
            phase=AgentPhase.BUSY,
            currentAssignments=2,
            modelAvailable=True,
        ),
    )

    decision = evaluate_agent_readiness(busy_agent, ready_provider())

    assert decision.phase == AgentPhase.BUSY
    assert decision.ready is True
    assert decision.can_accept_assignment is False
    assert decision.reason == AgentReadinessReason.BUSY


def test_current_assignments_cannot_exceed_capacity() -> None:
    agent = valid_agent()

    with pytest.raises(ValidationError):
        Agent(
            metadata=agent.metadata,
            spec=agent.spec,
            status=AgentStatus(
                phase=AgentPhase.BUSY,
                currentAssignments=3,
                modelAvailable=True,
            ),
        )


def test_busy_agent_requires_current_assignment() -> None:
    agent = valid_agent()

    with pytest.raises(ValidationError):
        Agent(
            metadata=agent.metadata,
            spec=agent.spec,
            status=AgentStatus(phase=AgentPhase.BUSY, currentAssignments=0),
        )
