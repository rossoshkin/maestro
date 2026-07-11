"""Capability resources and admission rules."""

from __future__ import annotations

from collections.abc import Iterable
from enum import StrEnum
from typing import Annotated, Literal, Protocol, Self

from pydantic import Field, field_validator, model_validator

from maestro.domain.exceptions import CapabilityPolicyDeniedError
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

CapabilityName = Annotated[
    str,
    Field(min_length=1, max_length=128, pattern=r"^[a-z0-9][a-z0-9.\-]*$"),
]
CapabilitySchemaReference = Annotated[
    str,
    Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*$",
    ),
]
NonEmptyText = Annotated[str, Field(min_length=1)]


class CapabilitySideEffectLevel(StrEnum):
    """Capability side-effect levels."""

    READ_ONLY = "read-only"
    MUTATING = "mutating"
    DESTRUCTIVE = "destructive"
    EXTERNAL = "external"
    PRIVILEGED = "privileged"


class CapabilityApprovalPolicy(StrEnum):
    """Approval policy required by a Capability."""

    NONE = "none"
    APPROVAL_REQUIRED = "approval-required"


class CapabilityScope(StrEnum):
    """Supported Capability scopes."""

    WORKSPACE = "workspace"
    PROJECT = "project"
    EXECUTION = "execution"
    KNOWLEDGE = "knowledge"
    EXTERNAL = "external"


class CapabilityPhase(StrEnum):
    """Capability status phases."""

    PENDING = "Pending"
    VALIDATING = "Validating"
    READY = "Ready"
    INVALID = "Invalid"
    DEPRECATED = "Deprecated"


class CapabilityBindingPhase(StrEnum):
    """CapabilityBinding status phases."""

    PENDING = "Pending"
    READY = "Ready"
    INVALID = "Invalid"
    DEPRECATED = "Deprecated"


class CapabilityDenialReason(StrEnum):
    """Structured reasons for Capability admission denial."""

    MISSING_REQUIRED = "MissingRequiredCapability"
    EXPLICITLY_DENIED = "ExplicitlyDeniedCapability"
    UNAVAILABLE = "UnavailableCapability"
    ROLE_POLICY = "RolePolicyDenied"
    SENSITIVE_REQUIRES_POLICY = "SensitiveCapabilityRequiresPolicy"


class CapabilityRoleSpec(Protocol):
    """Role spec fields used during Capability resolution."""

    effective_required_capabilities: tuple[CapabilityName, ...]
    prohibited_capabilities: tuple[CapabilityName, ...]


class CapabilityRole(Protocol):
    """Role fields used during Capability resolution."""

    metadata: Metadata
    spec: CapabilityRoleSpec


class CapabilitySpec(Spec):
    """Permission category independent of any tool implementation."""

    canonical_name: CapabilityName = Field(alias="canonicalName")
    description: NonEmptyText
    side_effect_level: CapabilitySideEffectLevel = Field(alias="sideEffectLevel")
    approval_policy: CapabilityApprovalPolicy = Field(
        default=CapabilityApprovalPolicy.NONE,
        alias="approvalPolicy",
    )
    scopes: tuple[CapabilityScope, ...] = Field(min_length=1)
    input_schema_ref: CapabilitySchemaReference = Field(alias="inputSchemaRef")
    output_schema_ref: CapabilitySchemaReference = Field(alias="outputSchemaRef")

    @field_validator("scopes")
    @classmethod
    def reject_duplicate_scopes(
        cls,
        value: tuple[CapabilityScope, ...],
    ) -> tuple[CapabilityScope, ...]:
        """Reject duplicate scopes."""

        if len(set(value)) != len(value):
            raise ValueError("Capability scopes must be unique")
        return value

    @model_validator(mode="after")
    def require_sensitive_policy(self) -> Self:
        """Require explicit policy for destructive and privileged Capabilities."""

        if (
            self.side_effect_level
            in {
                CapabilitySideEffectLevel.DESTRUCTIVE,
                CapabilitySideEffectLevel.PRIVILEGED,
            }
            and self.approval_policy == CapabilityApprovalPolicy.NONE
        ):
            raise ValueError(
                "destructive and privileged Capabilities require explicit policy"
            )
        return self


class CapabilityStatus(Status):
    """Observed Capability status."""

    phase: CapabilityPhase = CapabilityPhase.PENDING
    tool_implementations: tuple[ResourceName, ...] = Field(
        default_factory=tuple,
        alias="toolImplementations",
    )

    @field_validator("tool_implementations")
    @classmethod
    def reject_duplicate_tool_implementations(
        cls,
        value: tuple[ResourceName, ...],
    ) -> tuple[ResourceName, ...]:
        """Reject duplicate tool implementation names."""

        if len(set(value)) != len(value):
            raise ValueError("toolImplementations must be unique")
        return value


class Capability(BaseResource[CapabilitySpec, CapabilityStatus]):
    """Capability permission resource."""

    kind: Literal["Capability"] = "Capability"

    @model_validator(mode="after")
    def validate_capability_metadata(self) -> Self:
        """Ensure Capabilities are reusable definitions."""

        for owner_reference in self.metadata.owner_references:
            if owner_reference.controller:
                raise ValueError("Capability resources cannot have controller owners")
        return self

    @classmethod
    def new(
        cls,
        *,
        name: ResourceName,
        spec: CapabilitySpec,
        created_by: str = "local-user",
        namespace: ResourceName = "default",
    ) -> Self:
        """Create a new Capability resource."""

        return cls(
            metadata=Metadata(
                name=name,
                namespace=namespace,
                createdBy=created_by,
            ),
            spec=spec,
            status=CapabilityStatus(),
        )


class LabelSelector(MaestroModel):
    """Simple label selector used by CapabilityBinding scopes."""

    match_labels: dict[str, str] = Field(default_factory=dict, alias="matchLabels")

    def matches(self, labels: dict[str, str]) -> bool:
        """Return whether all selector labels are present."""

        return all(labels.get(key) == value for key, value in self.match_labels.items())


class CapabilityBindingScopes(MaestroModel):
    """Scope selectors for a CapabilityBinding."""

    workspace_selector: LabelSelector | None = Field(
        default=None,
        alias="workspaceSelector",
    )

    def matches(self, context: CapabilityResolutionContext) -> bool:
        """Return whether this binding applies to the supplied context."""

        if self.workspace_selector is not None and not self.workspace_selector.matches(
            context.workspace_labels
        ):
            return False
        return True


class CapabilityBindingSpec(Spec):
    """Explicit grants and denies for Capability admission."""

    grants: tuple[CapabilityName, ...] = Field(default_factory=tuple)
    denies: tuple[CapabilityName, ...] = Field(default_factory=tuple)
    scopes: CapabilityBindingScopes = Field(default_factory=CapabilityBindingScopes)

    @field_validator("grants", "denies")
    @classmethod
    def reject_duplicate_capabilities(
        cls,
        value: tuple[CapabilityName, ...],
    ) -> tuple[CapabilityName, ...]:
        """Reject duplicate Capability names within grants or denies."""

        if len(set(value)) != len(value):
            raise ValueError("Capability grants and denies must be unique")
        return value


class CapabilityBindingStatus(Status):
    """Observed CapabilityBinding status."""

    phase: CapabilityBindingPhase = CapabilityBindingPhase.PENDING


class CapabilityBinding(BaseResource[CapabilityBindingSpec, CapabilityBindingStatus]):
    """Resource that binds Capability grants and denies into policy."""

    kind: Literal["CapabilityBinding"] = "CapabilityBinding"

    @model_validator(mode="after")
    def validate_binding_metadata(self) -> Self:
        """Prevent controller-owned self-grant style bindings."""

        for owner_reference in self.metadata.owner_references:
            if owner_reference.kind == "Agent" and owner_reference.controller:
                raise ValueError("Agents cannot self-grant Capabilities")
        return self

    @classmethod
    def new(
        cls,
        *,
        name: ResourceName,
        spec: CapabilityBindingSpec,
        created_by: str = "local-user",
        namespace: ResourceName = "default",
    ) -> Self:
        """Create a new CapabilityBinding resource."""

        return cls(
            metadata=Metadata(
                name=name,
                namespace=namespace,
                createdBy=created_by,
            ),
            spec=spec,
            status=CapabilityBindingStatus(),
        )


class CapabilityResolutionContext(MaestroModel):
    """Context used when matching scoped CapabilityBindings."""

    workspace_labels: dict[str, str] = Field(
        default_factory=dict,
        alias="workspaceLabels",
    )


class CapabilityPolicyViolation(MaestroModel):
    """One reason Capability admission denied access."""

    reason: CapabilityDenialReason
    capability: CapabilityName
    message: str = ""


class CapabilityResolution(MaestroModel):
    """Effective Capability calculation result."""

    allowed: bool
    required: tuple[CapabilityName, ...]
    requested: tuple[CapabilityName, ...]
    granted: tuple[CapabilityName, ...]
    denied: tuple[CapabilityName, ...]
    effective: tuple[CapabilityName, ...]
    missing_required: tuple[CapabilityName, ...] = Field(
        default_factory=tuple,
        alias="missingRequired",
    )
    unavailable: tuple[CapabilityName, ...] = Field(default_factory=tuple)
    violations: tuple[CapabilityPolicyViolation, ...] = Field(default_factory=tuple)


class CapabilityRepository(
    ResourceRepository[Capability, CapabilitySpec, CapabilityStatus],
    Protocol,
):
    """Persistence contract for Capability resources."""

    async def get_by_canonical_name(
        self,
        namespace: str,
        canonical_name: str,
    ) -> Capability:
        """Load a Capability by namespace and canonical name."""


class CapabilityBindingRepository(
    ResourceRepository[
        CapabilityBinding,
        CapabilityBindingSpec,
        CapabilityBindingStatus,
    ],
    Protocol,
):
    """Persistence contract for CapabilityBinding resources."""

    async def list_ready(self, namespace: str) -> tuple[CapabilityBinding, ...]:
        """List Ready CapabilityBindings in one namespace."""


def resolve_capabilities(
    *,
    role: CapabilityRole,
    capabilities: Iterable[Capability],
    bindings: Iterable[CapabilityBinding],
    requested_capabilities: Iterable[CapabilityName] = (),
    agent_supported_capabilities: Iterable[CapabilityName] = (),
    context: CapabilityResolutionContext | None = None,
) -> CapabilityResolution:
    """Resolve effective Capabilities using deny-by-default semantics."""

    resolution_context = context or CapabilityResolutionContext()
    ready_capabilities = {
        capability.spec.canonical_name: capability
        for capability in capabilities
        if capability.status.phase == CapabilityPhase.READY
    }
    applicable_bindings = tuple(
        binding
        for binding in bindings
        if binding.status.phase == CapabilityBindingPhase.READY
        and binding.spec.scopes.matches(resolution_context)
    )

    granted = _sorted_unique(
        capability
        for binding in applicable_bindings
        for capability in binding.spec.grants
    )
    denied = _sorted_unique(
        capability
        for binding in applicable_bindings
        for capability in binding.spec.denies
    )
    required = _sorted_unique(role.spec.effective_required_capabilities)
    requested = _sorted_unique(requested_capabilities)
    required_or_requested = set(required) | set(requested)
    denied_set = set(denied) | set(role.spec.prohibited_capabilities)
    agent_supported = set(agent_supported_capabilities)

    unavailable = _sorted_unique(
        capability
        for capability in (set(granted) | required_or_requested)
        if capability not in ready_capabilities
    )
    effective_candidates = set(granted) - denied_set - set(unavailable)
    if agent_supported:
        effective_candidates &= agent_supported
    effective = _sorted_unique(effective_candidates)

    violations: list[CapabilityPolicyViolation] = []
    for capability in sorted(required_or_requested - set(effective)):
        if capability in denied_set:
            violations.append(
                CapabilityPolicyViolation(
                    reason=CapabilityDenialReason.EXPLICITLY_DENIED,
                    capability=capability,
                    message=f"{capability} is explicitly denied",
                )
            )
        elif capability in unavailable:
            violations.append(
                CapabilityPolicyViolation(
                    reason=CapabilityDenialReason.UNAVAILABLE,
                    capability=capability,
                    message=f"{capability} is not Ready in the Capability catalog",
                )
            )
        else:
            violations.append(
                CapabilityPolicyViolation(
                    reason=CapabilityDenialReason.MISSING_REQUIRED,
                    capability=capability,
                    message=f"{capability} is not granted by policy",
                )
            )

    for capability in effective:
        capability_resource = ready_capabilities[capability]
        if (
            capability_resource.spec.side_effect_level
            in {
                CapabilitySideEffectLevel.DESTRUCTIVE,
                CapabilitySideEffectLevel.PRIVILEGED,
            }
            and capability_resource.spec.approval_policy
            == CapabilityApprovalPolicy.NONE
        ):
            violations.append(
                CapabilityPolicyViolation(
                    reason=CapabilityDenialReason.SENSITIVE_REQUIRES_POLICY,
                    capability=capability,
                    message=f"{capability} requires explicit approval policy",
                )
            )

    violations.extend(_role_policy_violations(role, effective))

    missing_required = _sorted_unique(
        violation.capability
        for violation in violations
        if violation.reason == CapabilityDenialReason.MISSING_REQUIRED
    )
    return CapabilityResolution(
        allowed=not violations,
        required=required,
        requested=requested,
        granted=granted,
        denied=tuple(sorted(denied_set)),
        effective=effective,
        missingRequired=missing_required,
        unavailable=unavailable,
        violations=tuple(violations),
    )


def ensure_capability_admission(resolution: CapabilityResolution) -> None:
    """Raise a policy-denied error if Capability admission failed."""

    if resolution.allowed:
        return

    first_violation = resolution.violations[0]
    raise CapabilityPolicyDeniedError(
        first_violation.reason,
        first_violation.message,
    )


def apply_capability_spec_update(
    capability: Capability,
    spec: CapabilitySpec,
    *,
    expected_resource_version: int,
) -> Capability:
    """Apply a Capability spec update."""

    return apply_spec_update(
        capability,
        spec,
        expected_resource_version=expected_resource_version,
    )


def apply_capability_status_update(
    capability: Capability,
    status: CapabilityStatus,
    *,
    expected_resource_version: int,
) -> Capability:
    """Apply a Capability status update."""

    return apply_status_update(
        capability,
        status,
        expected_resource_version=expected_resource_version,
    )


def apply_capability_binding_spec_update(
    binding: CapabilityBinding,
    spec: CapabilityBindingSpec,
    *,
    expected_resource_version: int,
) -> CapabilityBinding:
    """Apply a CapabilityBinding spec update."""

    return apply_spec_update(
        binding,
        spec,
        expected_resource_version=expected_resource_version,
    )


def apply_capability_binding_status_update(
    binding: CapabilityBinding,
    status: CapabilityBindingStatus,
    *,
    expected_resource_version: int,
) -> CapabilityBinding:
    """Apply a CapabilityBinding status update."""

    return apply_status_update(
        binding,
        status,
        expected_resource_version=expected_resource_version,
    )


def _role_policy_violations(
    role: CapabilityRole,
    effective_capabilities: tuple[CapabilityName, ...],
) -> tuple[CapabilityPolicyViolation, ...]:
    role_name = role.metadata.name
    denied_prefixes: tuple[str, ...]
    if role_name == "planner":
        denied_prefixes = ("filesystem.write", "filesystem.edit", "shell.execute")
    elif role_name == "reviewer":
        denied_prefixes = ("filesystem.write", "filesystem.edit")
    else:
        denied_prefixes = ()

    return tuple(
        CapabilityPolicyViolation(
            reason=CapabilityDenialReason.ROLE_POLICY,
            capability=capability,
            message=f"Role {role_name} cannot receive {capability}",
        )
        for capability in effective_capabilities
        if any(
            capability == prefix or capability.startswith(f"{prefix}.")
            for prefix in denied_prefixes
        )
    )


def _sorted_unique(values: Iterable[CapabilityName]) -> tuple[CapabilityName, ...]:
    return tuple(sorted(set(values)))
