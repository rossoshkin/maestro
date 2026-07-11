"""Tests for Role resource validation and immutable versioning."""

from uuid import uuid4

import pytest
from pydantic import ValidationError

from maestro.domain.exceptions import ResourceImmutableFieldError
from maestro.domain.resources import Metadata, OwnerReference
from maestro.domain.roles import (
    Role,
    RoleExecutionPolicy,
    RolePromptReference,
    RoleSpec,
    apply_role_spec_update,
)


def valid_role_spec(*, version: str = "v1alpha1") -> RoleSpec:
    """Build a valid RoleSpec for tests."""

    return RoleSpec(
        version=version,
        purpose="Implement one software Work Item",
        inputSchemaRef="CodingInput/v1",
        outputSchemaRef="CodingResult/v1",
        promptRef=RolePromptReference(name="coding-prompt-v1", version=version),
        requiredCapabilities=(
            "filesystem.read",
            "filesystem.write",
            "filesystem.edit",
            "git.status",
            "git.diff",
        ),
        optionalCapabilities=(
            "shell.execute.test",
            "shell.execute.build",
            "knowledge.search",
        ),
        prohibitedCapabilities=(
            "git.push",
            "deployment.execute",
            "approval.decide",
            "workflow.transition",
        ),
        executionPolicy=RoleExecutionPolicy(
            maxSteps=40,
            maxDurationSeconds=1800,
            requireStructuredOutput=True,
            requireIndependentVerification=True,
        ),
    )


def test_role_serializes_and_deserializes() -> None:
    role = Role.new(name="coding", spec=valid_role_spec())

    payload = role.model_dump(mode="json", by_alias=True)
    round_tripped = Role.model_validate(payload)

    assert payload["kind"] == "Role"
    assert payload["spec"]["version"] == "v1alpha1"
    assert payload["spec"]["inputSchemaRef"] == "CodingInput/v1"
    assert round_tripped == role


def test_role_rejects_controller_owner_references() -> None:
    with pytest.raises(ValidationError):
        Role(
            metadata=Metadata(
                name="coding",
                ownerReferences=(
                    OwnerReference(kind="Project", id=uuid4(), controller=True),
                ),
            ),
            spec=valid_role_spec(),
            status=Role.new(name="coding", spec=valid_role_spec()).status,
        )


def test_input_and_output_schema_references_are_required() -> None:
    payload = valid_role_spec().model_dump(mode="json", by_alias=True)
    del payload["inputSchemaRef"]
    del payload["outputSchemaRef"]

    with pytest.raises(ValidationError):
        RoleSpec.model_validate(payload)


def test_schema_references_must_include_version_segment() -> None:
    with pytest.raises(ValidationError):
        RoleSpec(
            version="v1alpha1",
            purpose="Implement one software Work Item",
            inputSchemaRef="CodingInput",
            outputSchemaRef="CodingResult/v1",
        )


def test_prompt_reference_requires_id_or_name() -> None:
    with pytest.raises(ValidationError):
        RolePromptReference()


def test_roles_do_not_reference_models_or_providers() -> None:
    payload = valid_role_spec().model_dump(mode="json", by_alias=True)
    payload["model"] = "qwen2.5-coder"
    payload["providerRef"] = "ollama-local"

    with pytest.raises(ValidationError):
        RoleSpec.model_validate(payload)


def test_workflow_transition_capability_cannot_be_requested() -> None:
    with pytest.raises(ValidationError):
        RoleSpec(
            version="v1alpha1",
            purpose="Bad workflow mutator",
            inputSchemaRef="CodingInput/v1",
            outputSchemaRef="CodingResult/v1",
            requiredCapabilities=("workflow.transition",),
        )


def test_workflow_transition_capability_can_be_explicitly_prohibited() -> None:
    spec = valid_role_spec()

    assert "workflow.transition" in spec.prohibited_capabilities


def test_duplicate_capabilities_are_rejected() -> None:
    with pytest.raises(ValidationError):
        RoleSpec(
            version="v1alpha1",
            purpose="Duplicate capabilities",
            inputSchemaRef="CodingInput/v1",
            outputSchemaRef="CodingResult/v1",
            requiredCapabilities=("filesystem.read", "filesystem.read"),
        )


def test_capability_cannot_be_required_and_optional() -> None:
    with pytest.raises(ValidationError):
        RoleSpec(
            version="v1alpha1",
            purpose="Conflicting capabilities",
            inputSchemaRef="CodingInput/v1",
            outputSchemaRef="CodingResult/v1",
            requiredCapabilities=("filesystem.read",),
            optionalCapabilities=("filesystem.read",),
        )


def test_required_capability_cannot_be_prohibited() -> None:
    with pytest.raises(ValidationError):
        RoleSpec(
            version="v1alpha1",
            purpose="Impossible capabilities",
            inputSchemaRef="CodingInput/v1",
            outputSchemaRef="CodingResult/v1",
            requiredCapabilities=("filesystem.read",),
            prohibitedCapabilities=("filesystem.read",),
        )


def test_prohibited_capabilities_override_optional_capabilities() -> None:
    spec = RoleSpec(
        version="v1alpha1",
        purpose="Optional deny policy",
        inputSchemaRef="CodingInput/v1",
        outputSchemaRef="CodingResult/v1",
        optionalCapabilities=("web.search", "git.push"),
        prohibitedCapabilities=("git.push",),
    )

    assert spec.effective_optional_capabilities == ("web.search",)


def test_execution_policy_limits_are_bounded() -> None:
    with pytest.raises(ValidationError):
        RoleExecutionPolicy(maxSteps=0)


def test_role_versions_are_immutable_after_creation() -> None:
    role = Role.new(name="coding", spec=valid_role_spec())
    changed_spec = role.spec.model_copy(update={"purpose": "Changed"})

    with pytest.raises(ResourceImmutableFieldError):
        apply_role_spec_update(
            role,
            changed_spec,
            expected_resource_version=role.metadata.resource_version,
        )
