"""Agent resource models, readiness and Role compatibility rules."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal, Protocol, Self

from pydantic import Field, field_validator, model_validator

from maestro.domain.modeling import ModelIdentifier
from maestro.domain.projects import ReferenceVersion
from maestro.domain.repositories import (
    ResourceRepository,
    apply_spec_update,
    apply_status_update,
)
from maestro.domain.resources import (
    BaseResource,
    MaestroModel,
    Metadata,
    ResourceName,
    Spec,
    Status,
)
from maestro.domain.roles import Role, RolePhase


class AgentPhase(StrEnum):
    """Agent operational phases."""

    PENDING = "Pending"
    READY = "Ready"
    BUSY = "Busy"
    DEGRADED = "Degraded"
    UNAVAILABLE = "Unavailable"
    DISABLED = "Disabled"


class ProviderReadinessPhase(StrEnum):
    """Provider phases relevant to Agent readiness."""

    PENDING = "Pending"
    READY = "Ready"
    DEGRADED = "Degraded"
    UNAVAILABLE = "Unavailable"
    DISABLED = "Disabled"


class AgentCompatibilityReason(StrEnum):
    """Reasons returned by Agent and Role compatibility checks."""

    COMPATIBLE = "Compatible"
    ROLE_NOT_READY = "RoleNotReady"
    ROLE_NOT_SUPPORTED = "RoleNotSupported"
    ROLE_VERSION_NOT_SUPPORTED = "RoleVersionNotSupported"


class AgentReadinessReason(StrEnum):
    """Reasons returned by Agent readiness evaluation."""

    READY = "Ready"
    BUSY = "Busy"
    DISABLED = "Disabled"
    PROVIDER_MISMATCH = "ProviderMismatch"
    PROVIDER_PENDING = "ProviderPending"
    PROVIDER_DEGRADED = "ProviderDegraded"
    PROVIDER_UNAVAILABLE = "ProviderUnavailable"
    MODEL_UNAVAILABLE = "ModelUnavailable"


class AgentProviderReference(MaestroModel):
    """Reference to the Provider used by an Agent."""

    kind: Literal["Provider"] = "Provider"
    name: ResourceName


class AgentSupportedRole(MaestroModel):
    """Role versions supported by an Agent."""

    name: ResourceName
    versions: tuple[ReferenceVersion, ...] = Field(min_length=1)

    @field_validator("versions")
    @classmethod
    def reject_duplicate_versions(
        cls,
        value: tuple[ReferenceVersion, ...],
    ) -> tuple[ReferenceVersion, ...]:
        """Reject duplicate supported Role versions."""

        if len(set(value)) != len(value):
            raise ValueError("supported Role versions must be unique")
        return value


class AgentCapabilityBindingReference(MaestroModel):
    """Reference to a CapabilityBinding available to an Agent."""

    kind: Literal["CapabilityBinding"] = "CapabilityBinding"
    name: ResourceName


class AgentCapacity(MaestroModel):
    """Agent assignment capacity."""

    max_concurrent_assignments: int = Field(
        default=1,
        ge=1,
        alias="maxConcurrentAssignments",
    )


class AgentScheduling(MaestroModel):
    """Scheduler-facing Agent configuration."""

    priority: int = Field(default=100, ge=0)
    enabled: bool = True


class AgentSpec(Spec):
    """Operational Agent configuration."""

    provider_ref: AgentProviderReference = Field(alias="providerRef")
    model: ModelIdentifier
    supported_roles: tuple[AgentSupportedRole, ...] = Field(
        min_length=1,
        alias="supportedRoles",
    )
    capability_bindings: tuple[AgentCapabilityBindingReference, ...] = Field(
        default_factory=tuple,
        alias="capabilityBindings",
    )
    capacity: AgentCapacity = Field(default_factory=AgentCapacity)
    scheduling: AgentScheduling = Field(default_factory=AgentScheduling)

    @field_validator("supported_roles")
    @classmethod
    def reject_duplicate_supported_roles(
        cls,
        value: tuple[AgentSupportedRole, ...],
    ) -> tuple[AgentSupportedRole, ...]:
        """Reject duplicate supported Role entries."""

        role_names = [role.name for role in value]
        if len(set(role_names)) != len(role_names):
            raise ValueError("supportedRoles must be unique by Role name")
        return value

    @field_validator("capability_bindings")
    @classmethod
    def reject_duplicate_capability_bindings(
        cls,
        value: tuple[AgentCapabilityBindingReference, ...],
    ) -> tuple[AgentCapabilityBindingReference, ...]:
        """Reject duplicate CapabilityBinding references."""

        binding_names = [binding.name for binding in value]
        if len(set(binding_names)) != len(binding_names):
            raise ValueError("capabilityBindings must be unique")
        return value


class AgentStatus(Status):
    """Observed operational state for an Agent."""

    phase: AgentPhase = AgentPhase.PENDING
    current_assignments: int = Field(default=0, ge=0, alias="currentAssignments")
    last_heartbeat_at: datetime | None = Field(default=None, alias="lastHeartbeatAt")
    model_available: bool = Field(default=False, alias="modelAvailable")


class ProviderReadinessSnapshot(MaestroModel):
    """Provider readiness input used to evaluate Agent readiness."""

    provider_ref: AgentProviderReference = Field(alias="providerRef")
    phase: ProviderReadinessPhase
    available_models: tuple[ModelIdentifier, ...] = Field(
        default_factory=tuple,
        alias="availableModels",
    )

    @field_validator("available_models")
    @classmethod
    def reject_duplicate_available_models(
        cls,
        value: tuple[ModelIdentifier, ...],
    ) -> tuple[ModelIdentifier, ...]:
        """Reject duplicate available model identifiers."""

        if len(set(value)) != len(value):
            raise ValueError("availableModels must be unique")
        return value


class AgentCompatibilityDecision(MaestroModel):
    """Result of checking whether an Agent can fulfill a Role."""

    compatible: bool
    reason: AgentCompatibilityReason
    message: str = ""


class AgentReadinessDecision(MaestroModel):
    """Result of evaluating Agent operational readiness."""

    phase: AgentPhase
    ready: bool
    can_accept_assignment: bool = Field(alias="canAcceptAssignment")
    model_available: bool = Field(alias="modelAvailable")
    reason: AgentReadinessReason
    message: str = ""


class Agent(BaseResource[AgentSpec, AgentStatus]):
    """Runtime configuration capable of fulfilling compatible Roles."""

    kind: Literal["Agent"] = "Agent"

    @model_validator(mode="after")
    def validate_agent_metadata_and_capacity(self) -> Self:
        """Validate Agent metadata and capacity-sensitive status."""

        for owner_reference in self.metadata.owner_references:
            if owner_reference.controller:
                raise ValueError("Agent resources cannot have controller owners")

        if (
            self.status.current_assignments
            > self.spec.capacity.max_concurrent_assignments
        ):
            raise ValueError(
                "status.currentAssignments cannot exceed "
                "spec.capacity.maxConcurrentAssignments"
            )

        if (
            self.status.phase == AgentPhase.BUSY
            and self.status.current_assignments == 0
        ):
            raise ValueError("Busy Agents require at least one current assignment")

        return self

    @classmethod
    def new(
        cls,
        *,
        name: ResourceName,
        spec: AgentSpec,
        created_by: str = "local-user",
        namespace: ResourceName = "default",
    ) -> Self:
        """Create a new Agent resource."""

        return cls(
            metadata=Metadata(
                name=name,
                namespace=namespace,
                createdBy=created_by,
            ),
            spec=spec,
            status=AgentStatus(),
        )


class AgentRepository(ResourceRepository[Agent, AgentSpec, AgentStatus], Protocol):
    """Persistence contract for Agent resources."""

    async def list_by_provider(
        self,
        namespace: str,
        provider_name: str,
    ) -> tuple[Agent, ...]:
        """List Agents bound to a Provider."""

    async def list_compatible_with_role(
        self,
        namespace: str,
        role_name: str,
        role_version: str,
    ) -> tuple[Agent, ...]:
        """List Agents that declare support for a Role version."""


def evaluate_agent_role_compatibility(
    agent: Agent,
    role: Role,
) -> AgentCompatibilityDecision:
    """Return whether an Agent declares support for a Role version."""

    if role.status.phase not in {RolePhase.READY, RolePhase.DEPRECATED}:
        return AgentCompatibilityDecision(
            compatible=False,
            reason=AgentCompatibilityReason.ROLE_NOT_READY,
            message=(
                f"Role {role.metadata.name}/{role.spec.version} is {role.status.phase}"
            ),
        )

    supported = {
        supported_role.name: supported_role.versions
        for supported_role in agent.spec.supported_roles
    }
    versions = supported.get(role.metadata.name)
    if versions is None:
        return AgentCompatibilityDecision(
            compatible=False,
            reason=AgentCompatibilityReason.ROLE_NOT_SUPPORTED,
            message=f"Agent does not support Role {role.metadata.name}",
        )

    if role.spec.version not in versions:
        return AgentCompatibilityDecision(
            compatible=False,
            reason=AgentCompatibilityReason.ROLE_VERSION_NOT_SUPPORTED,
            message=(
                f"Agent does not support Role {role.metadata.name}/{role.spec.version}"
            ),
        )

    return AgentCompatibilityDecision(
        compatible=True,
        reason=AgentCompatibilityReason.COMPATIBLE,
    )


def evaluate_agent_readiness(
    agent: Agent,
    provider: ProviderReadinessSnapshot,
) -> AgentReadinessDecision:
    """Return Agent readiness using Provider/model/capacity state."""

    model_available = agent.spec.model in provider.available_models

    if not agent.spec.scheduling.enabled or agent.status.phase == AgentPhase.DISABLED:
        return AgentReadinessDecision(
            phase=AgentPhase.DISABLED,
            ready=False,
            canAcceptAssignment=False,
            modelAvailable=model_available,
            reason=AgentReadinessReason.DISABLED,
            message="Agent scheduling is disabled",
        )

    if provider.provider_ref != agent.spec.provider_ref:
        return AgentReadinessDecision(
            phase=AgentPhase.UNAVAILABLE,
            ready=False,
            canAcceptAssignment=False,
            modelAvailable=False,
            reason=AgentReadinessReason.PROVIDER_MISMATCH,
            message="Provider readiness snapshot does not match Agent providerRef",
        )

    if provider.phase == ProviderReadinessPhase.PENDING:
        return AgentReadinessDecision(
            phase=AgentPhase.UNAVAILABLE,
            ready=False,
            canAcceptAssignment=False,
            modelAvailable=model_available,
            reason=AgentReadinessReason.PROVIDER_PENDING,
            message="Provider readiness is pending",
        )

    if provider.phase in {
        ProviderReadinessPhase.UNAVAILABLE,
        ProviderReadinessPhase.DISABLED,
    }:
        return AgentReadinessDecision(
            phase=AgentPhase.UNAVAILABLE,
            ready=False,
            canAcceptAssignment=False,
            modelAvailable=False,
            reason=AgentReadinessReason.PROVIDER_UNAVAILABLE,
            message=f"Provider is {provider.phase}",
        )

    if not model_available:
        return AgentReadinessDecision(
            phase=AgentPhase.UNAVAILABLE,
            ready=False,
            canAcceptAssignment=False,
            modelAvailable=False,
            reason=AgentReadinessReason.MODEL_UNAVAILABLE,
            message=f"Model {agent.spec.model} is not available",
        )

    if provider.phase == ProviderReadinessPhase.DEGRADED:
        return AgentReadinessDecision(
            phase=AgentPhase.DEGRADED,
            ready=False,
            canAcceptAssignment=False,
            modelAvailable=True,
            reason=AgentReadinessReason.PROVIDER_DEGRADED,
            message="Provider is degraded",
        )

    if (
        agent.status.current_assignments
        >= agent.spec.capacity.max_concurrent_assignments
    ):
        return AgentReadinessDecision(
            phase=AgentPhase.BUSY,
            ready=True,
            canAcceptAssignment=False,
            modelAvailable=True,
            reason=AgentReadinessReason.BUSY,
            message="Agent is at capacity",
        )

    return AgentReadinessDecision(
        phase=AgentPhase.READY,
        ready=True,
        canAcceptAssignment=True,
        modelAvailable=True,
        reason=AgentReadinessReason.READY,
    )


def apply_agent_spec_update(
    agent: Agent,
    spec: AgentSpec,
    *,
    expected_resource_version: int,
) -> Agent:
    """Apply an Agent desired-state update."""

    return apply_spec_update(
        agent,
        spec,
        expected_resource_version=expected_resource_version,
    )


def apply_agent_status_update(
    agent: Agent,
    status: AgentStatus,
    *,
    expected_resource_version: int,
) -> Agent:
    """Apply an Agent status update."""

    return apply_status_update(
        agent,
        status,
        expected_resource_version=expected_resource_version,
    )
