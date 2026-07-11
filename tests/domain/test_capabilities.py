"""Tests for Capability resources and admission resolution."""

from uuid import uuid4

import pytest
from pydantic import ValidationError

from maestro.domain.capabilities import (
    Capability,
    CapabilityApprovalPolicy,
    CapabilityBinding,
    CapabilityBindingPhase,
    CapabilityBindingScopes,
    CapabilityBindingSpec,
    CapabilityBindingStatus,
    CapabilityDenialReason,
    CapabilityPhase,
    CapabilityResolutionContext,
    CapabilityScope,
    CapabilitySideEffectLevel,
    CapabilitySpec,
    CapabilityStatus,
    LabelSelector,
    ensure_capability_admission,
    resolve_capabilities,
)
from maestro.domain.exceptions import CapabilityPolicyDeniedError
from maestro.domain.resources import Metadata, OwnerReference
from maestro.domain.roles import Role, RoleExecutionPolicy, RoleSpec


def capability_spec(
    canonical_name: str,
    *,
    side_effect_level: CapabilitySideEffectLevel = CapabilitySideEffectLevel.READ_ONLY,
    approval_policy: CapabilityApprovalPolicy = CapabilityApprovalPolicy.NONE,
) -> CapabilitySpec:
    """Build a valid CapabilitySpec."""

    schema_name = "".join(part.capitalize() for part in canonical_name.split("."))
    return CapabilitySpec(
        canonicalName=canonical_name,
        description=f"Capability for {canonical_name}",
        sideEffectLevel=side_effect_level,
        approvalPolicy=approval_policy,
        scopes=(CapabilityScope.WORKSPACE,),
        inputSchemaRef=f"{schema_name}Input/v1",
        outputSchemaRef=f"{schema_name}Output/v1",
    )


def ready_capability(
    canonical_name: str,
    *,
    side_effect_level: CapabilitySideEffectLevel = CapabilitySideEffectLevel.READ_ONLY,
    approval_policy: CapabilityApprovalPolicy = CapabilityApprovalPolicy.NONE,
) -> Capability:
    """Build a Ready Capability resource."""

    capability = Capability.new(
        name=canonical_name.replace(".", "-"),
        spec=capability_spec(
            canonical_name,
            side_effect_level=side_effect_level,
            approval_policy=approval_policy,
        ),
    )
    return Capability(
        metadata=capability.metadata,
        spec=capability.spec,
        status=CapabilityStatus(
            phase=CapabilityPhase.READY,
            toolImplementations=("local-tool",),
        ),
    )


def role(name: str, required_capabilities: tuple[str, ...]) -> Role:
    """Build a Role with required Capabilities."""

    return Role.new(
        name=name,
        spec=RoleSpec(
            version="v1alpha1",
            purpose=f"{name} role",
            inputSchemaRef="RoleInput/v1",
            outputSchemaRef="RoleOutput/v1",
            requiredCapabilities=required_capabilities,
            prohibitedCapabilities=("workflow.transition",),
            executionPolicy=RoleExecutionPolicy(maxSteps=40),
        ),
    )


def ready_binding(
    *,
    grants: tuple[str, ...] = (),
    denies: tuple[str, ...] = (),
    scopes: CapabilityBindingScopes | None = None,
) -> CapabilityBinding:
    """Build a Ready CapabilityBinding resource."""

    binding = CapabilityBinding.new(
        name="local-workspace-safe",
        spec=CapabilityBindingSpec(
            grants=grants,
            denies=denies,
            scopes=scopes or CapabilityBindingScopes(),
        ),
    )
    return CapabilityBinding(
        metadata=binding.metadata,
        spec=binding.spec,
        status=CapabilityBindingStatus(phase=CapabilityBindingPhase.READY),
    )


def test_capability_serializes_and_deserializes() -> None:
    capability = ready_capability("filesystem.read")

    payload = capability.model_dump(mode="json", by_alias=True)
    round_tripped = Capability.model_validate(payload)

    assert payload["kind"] == "Capability"
    assert payload["spec"]["canonicalName"] == "filesystem.read"
    assert round_tripped == capability


def test_capability_binding_serializes_and_deserializes() -> None:
    binding = ready_binding(grants=("filesystem.read",), denies=("git.push",))

    payload = binding.model_dump(mode="json", by_alias=True)
    round_tripped = CapabilityBinding.model_validate(payload)

    assert payload["kind"] == "CapabilityBinding"
    assert payload["spec"]["grants"] == ["filesystem.read"]
    assert round_tripped == binding


def test_destructive_capability_requires_explicit_policy() -> None:
    with pytest.raises(ValidationError):
        capability_spec(
            "git.push",
            side_effect_level=CapabilitySideEffectLevel.DESTRUCTIVE,
        )


def test_duplicate_binding_grants_are_rejected() -> None:
    with pytest.raises(ValidationError):
        CapabilityBindingSpec(grants=("filesystem.read", "filesystem.read"))


def test_agents_cannot_self_grant_capabilities() -> None:
    with pytest.raises(ValidationError):
        CapabilityBinding(
            metadata=Metadata(
                name="agent-self-grant",
                ownerReferences=(
                    OwnerReference(kind="Agent", id=uuid4(), controller=True),
                ),
            ),
            spec=CapabilityBindingSpec(grants=("filesystem.write",)),
            status=CapabilityBindingStatus(),
        )


def test_deny_by_default_blocks_required_capability() -> None:
    result = resolve_capabilities(
        role=role("coding", ("filesystem.read",)),
        capabilities=(ready_capability("filesystem.read"),),
        bindings=(),
    )

    assert result.allowed is False
    assert result.missing_required == ("filesystem.read",)
    assert result.violations[0].reason == CapabilityDenialReason.MISSING_REQUIRED


def test_explicit_grant_allows_required_capability() -> None:
    result = resolve_capabilities(
        role=role("coding", ("filesystem.read",)),
        capabilities=(ready_capability("filesystem.read"),),
        bindings=(ready_binding(grants=("filesystem.read",)),),
    )

    assert result.allowed is True
    assert result.effective == ("filesystem.read",)


def test_explicit_deny_overrides_grant() -> None:
    result = resolve_capabilities(
        role=role("coding", ("filesystem.read",)),
        capabilities=(ready_capability("filesystem.read"),),
        bindings=(
            ready_binding(
                grants=("filesystem.read",),
                denies=("filesystem.read",),
            ),
        ),
    )

    assert result.allowed is False
    assert result.effective == ()
    assert result.violations[0].reason == CapabilityDenialReason.EXPLICITLY_DENIED


def test_agent_supported_capabilities_do_not_self_grant() -> None:
    result = resolve_capabilities(
        role=role("coding", ("filesystem.write",)),
        capabilities=(ready_capability("filesystem.write"),),
        bindings=(),
        agent_supported_capabilities=("filesystem.write",),
    )

    assert result.allowed is False
    assert result.violations[0].reason == CapabilityDenialReason.MISSING_REQUIRED


def test_required_capability_must_be_ready_in_catalog() -> None:
    pending = Capability.new(
        name="filesystem-read",
        spec=capability_spec("filesystem.read"),
    )

    result = resolve_capabilities(
        role=role("coding", ("filesystem.read",)),
        capabilities=(pending,),
        bindings=(ready_binding(grants=("filesystem.read",)),),
    )

    assert result.allowed is False
    assert result.unavailable == ("filesystem.read",)
    assert result.violations[0].reason == CapabilityDenialReason.UNAVAILABLE


def test_planner_cannot_receive_filesystem_write_or_shell_execute() -> None:
    result = resolve_capabilities(
        role=role("planner", ("repository.structure.read",)),
        capabilities=(
            ready_capability("repository.structure.read"),
            ready_capability(
                "filesystem.write",
                side_effect_level=CapabilitySideEffectLevel.MUTATING,
            ),
            ready_capability(
                "shell.execute.test",
                side_effect_level=CapabilitySideEffectLevel.MUTATING,
            ),
        ),
        bindings=(
            ready_binding(
                grants=(
                    "repository.structure.read",
                    "filesystem.write",
                    "shell.execute.test",
                )
            ),
        ),
    )

    assert result.allowed is False
    assert {violation.capability for violation in result.violations} == {
        "filesystem.write",
        "shell.execute.test",
    }


def test_reviewer_cannot_receive_filesystem_write() -> None:
    result = resolve_capabilities(
        role=role("reviewer", ("git.diff.read",)),
        capabilities=(
            ready_capability("git.diff.read"),
            ready_capability(
                "filesystem.write",
                side_effect_level=CapabilitySideEffectLevel.MUTATING,
            ),
        ),
        bindings=(ready_binding(grants=("git.diff.read", "filesystem.write")),),
    )

    assert result.allowed is False
    assert result.violations[0].reason == CapabilityDenialReason.ROLE_POLICY


def test_scoped_binding_applies_only_when_workspace_labels_match() -> None:
    binding = ready_binding(
        grants=("filesystem.read",),
        scopes=CapabilityBindingScopes(
            workspaceSelector=LabelSelector(matchLabels={"type": "git-worktree"})
        ),
    )

    denied = resolve_capabilities(
        role=role("coding", ("filesystem.read",)),
        capabilities=(ready_capability("filesystem.read"),),
        bindings=(binding,),
        context=CapabilityResolutionContext(workspaceLabels={"type": "scratch"}),
    )
    allowed = resolve_capabilities(
        role=role("coding", ("filesystem.read",)),
        capabilities=(ready_capability("filesystem.read"),),
        bindings=(binding,),
        context=CapabilityResolutionContext(workspaceLabels={"type": "git-worktree"}),
    )

    assert denied.allowed is False
    assert allowed.allowed is True


def test_ensure_capability_admission_raises_policy_error() -> None:
    result = resolve_capabilities(
        role=role("coding", ("filesystem.read",)),
        capabilities=(ready_capability("filesystem.read"),),
        bindings=(),
    )

    with pytest.raises(CapabilityPolicyDeniedError):
        ensure_capability_admission(result)
