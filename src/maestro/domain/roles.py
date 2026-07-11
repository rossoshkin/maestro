"""Role resource models and capability policy validation."""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal, Protocol, Self
from uuid import UUID

from pydantic import Field, field_validator, model_validator

from maestro.domain.capabilities import CapabilityName
from maestro.domain.exceptions import ResourceImmutableFieldError
from maestro.domain.projects import ReferenceVersion
from maestro.domain.repositories import ResourceRepository, apply_spec_update
from maestro.domain.resources import (
    BaseResource,
    MaestroModel,
    Metadata,
    ResourceName,
    Spec,
    Status,
)

SchemaReference = Annotated[
    str,
    Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*$",
    ),
]
NonEmptyText = Annotated[str, Field(min_length=1)]
WORKFLOW_TRANSITION_CAPABILITY = "workflow.transition"


class RolePhase(StrEnum):
    """Role status phases."""

    PENDING = "Pending"
    VALIDATING = "Validating"
    READY = "Ready"
    INVALID = "Invalid"
    DEPRECATED = "Deprecated"


class RoleValidationResult(MaestroModel):
    """Role validation result stored in status."""

    valid: bool = False
    errors: tuple[str, ...] = Field(default_factory=tuple)


class RolePromptReference(MaestroModel):
    """Reference to a versioned prompt artifact for a Role."""

    kind: Literal["Artifact"] = "Artifact"
    id: UUID | None = None
    name: ResourceName | None = None
    version: ReferenceVersion | None = None

    @model_validator(mode="after")
    def require_stable_prompt_identity(self) -> Self:
        """Require enough identity to resolve the prompt artifact."""

        if self.id is None and self.name is None:
            raise ValueError("promptRef requires id or name")
        return self


class RoleExecutionPolicy(MaestroModel):
    """Execution limits and validation requirements for a Role."""

    max_steps: int = Field(default=40, ge=1, alias="maxSteps")
    max_duration_seconds: int = Field(default=1800, ge=1, alias="maxDurationSeconds")
    require_structured_output: bool = Field(
        default=True,
        alias="requireStructuredOutput",
    )
    require_independent_verification: bool = Field(
        default=True,
        alias="requireIndependentVerification",
    )


class RoleSpec(Spec):
    """Versioned, provider-independent Role contract."""

    version: ReferenceVersion
    purpose: NonEmptyText
    input_schema_ref: SchemaReference = Field(alias="inputSchemaRef")
    output_schema_ref: SchemaReference = Field(alias="outputSchemaRef")
    prompt_ref: RolePromptReference | None = Field(default=None, alias="promptRef")
    required_capabilities: tuple[CapabilityName, ...] = Field(
        default_factory=tuple,
        alias="requiredCapabilities",
    )
    optional_capabilities: tuple[CapabilityName, ...] = Field(
        default_factory=tuple,
        alias="optionalCapabilities",
    )
    prohibited_capabilities: tuple[CapabilityName, ...] = Field(
        default_factory=tuple,
        alias="prohibitedCapabilities",
    )
    execution_policy: RoleExecutionPolicy = Field(
        default_factory=RoleExecutionPolicy,
        alias="executionPolicy",
    )

    @field_validator(
        "required_capabilities",
        "optional_capabilities",
        "prohibited_capabilities",
    )
    @classmethod
    def reject_duplicate_capabilities(
        cls,
        value: tuple[CapabilityName, ...],
    ) -> tuple[CapabilityName, ...]:
        """Reject duplicate Capability names within one policy set."""

        if len(set(value)) != len(value):
            raise ValueError("Capability names must be unique within each policy set")
        return value

    @property
    def effective_required_capabilities(self) -> tuple[CapabilityName, ...]:
        """Return required Capabilities after prohibited deny rules are applied."""

        prohibited = set(self.prohibited_capabilities)
        return tuple(
            capability
            for capability in self.required_capabilities
            if capability not in prohibited
        )

    @property
    def effective_optional_capabilities(self) -> tuple[CapabilityName, ...]:
        """Return optional Capabilities after prohibited deny rules are applied."""

        prohibited = set(self.prohibited_capabilities)
        return tuple(
            capability
            for capability in self.optional_capabilities
            if capability not in prohibited
        )

    @model_validator(mode="after")
    def validate_capability_policy(self) -> Self:
        """Validate Role Capability policy combinations."""

        required = set(self.required_capabilities)
        optional = set(self.optional_capabilities)
        prohibited = set(self.prohibited_capabilities)

        duplicated_requests = required & optional
        if duplicated_requests:
            raise ValueError(
                "Capabilities cannot be both required and optional: "
                + ", ".join(sorted(duplicated_requests))
            )

        prohibited_required = required & prohibited
        if prohibited_required:
            raise ValueError(
                "Capabilities cannot be both required and prohibited: "
                + ", ".join(sorted(prohibited_required))
            )

        workflow_transition_requests = tuple(
            capability
            for capability in (*self.required_capabilities, *self.optional_capabilities)
            if _is_workflow_transition_capability(capability)
        )
        if workflow_transition_requests:
            raise ValueError(
                "Roles cannot request Workflow transition Capabilities: "
                + ", ".join(sorted(workflow_transition_requests))
            )

        return self


class RoleStatus(Status):
    """Observed state for a Role definition."""

    phase: RolePhase = RolePhase.PENDING
    validation: RoleValidationResult = Field(default_factory=RoleValidationResult)


class Role(BaseResource[RoleSpec, RoleStatus]):
    """Immutable, versioned Role definition."""

    kind: Literal["Role"] = "Role"

    @model_validator(mode="after")
    def validate_role_metadata(self) -> Self:
        """Ensure Role metadata is consistent with reusable definitions."""

        for owner_reference in self.metadata.owner_references:
            if owner_reference.controller:
                raise ValueError("Role resources cannot have controller owners")
        return self

    @classmethod
    def new(
        cls,
        *,
        name: ResourceName,
        spec: RoleSpec,
        created_by: str = "local-user",
        namespace: ResourceName = "default",
    ) -> Self:
        """Create a new Role resource."""

        return cls(
            metadata=Metadata(
                name=name,
                namespace=namespace,
                createdBy=created_by,
            ),
            spec=spec,
            status=RoleStatus(),
        )


class RoleRepository(ResourceRepository[Role, RoleSpec, RoleStatus], Protocol):
    """Persistence contract for Role resources."""

    async def get_by_name_version(
        self,
        namespace: str,
        name: str,
        version: str,
    ) -> Role:
        """Load a Role by namespace, name and version."""


def apply_role_spec_update(
    role: Role,
    spec: RoleSpec,
    *,
    expected_resource_version: int,
) -> Role:
    """Reject actual Role spec changes because versions are immutable."""

    if spec != role.spec:
        raise ResourceImmutableFieldError(role.metadata.id, "spec")

    return apply_spec_update(
        role,
        spec,
        expected_resource_version=expected_resource_version,
    )


def _is_workflow_transition_capability(capability: str) -> bool:
    return capability == WORKFLOW_TRANSITION_CAPABILITY or capability.startswith(
        f"{WORKFLOW_TRANSITION_CAPABILITY}."
    )
