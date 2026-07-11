"""Tests for Workflow resource graph validation."""

import pytest
from pydantic import ValidationError

from maestro.domain.exceptions import ResourceImmutableFieldError
from maestro.domain.workflows import (
    TerminalOutcome,
    Workflow,
    WorkflowApprovalStep,
    WorkflowDecisionStep,
    WorkflowFanOutStep,
    WorkflowRetryPolicy,
    WorkflowRoleReference,
    WorkflowRoleStep,
    WorkflowSpec,
    WorkflowSystemStep,
    WorkflowTerminalStep,
    apply_workflow_spec_update,
)


def valid_workflow_spec() -> WorkflowSpec:
    """Build the default MVP Workflow shape for tests."""

    return WorkflowSpec(
        version="v1alpha1",
        description="Plan, code, verify, review and approve",
        entrypoint="planning",
        steps=(
            WorkflowRoleStep(
                id="planning",
                roleRef=WorkflowRoleReference(name="planner", version="v1alpha1"),
                onSuccess="plan-approval",
            ),
            WorkflowApprovalStep(
                id="plan-approval",
                subjectRef="latestPlan",
                onApproved="prepare-workspace",
                onRejected="planning",
                retryPolicy=WorkflowRetryPolicy(maxAttempts=2),
            ),
            WorkflowSystemStep(
                id="prepare-workspace",
                controller="workspace",
                onSuccess="execute-work-items",
            ),
            WorkflowFanOutStep(
                id="execute-work-items",
                source="approvedPlan.workItems",
                maxParallel=1,
                onSuccess="verify",
            ),
            WorkflowSystemStep(
                id="verify",
                controller="verification",
                onSuccess="review",
                onFailure="repair",
            ),
            WorkflowRoleStep(
                id="review",
                roleRef=WorkflowRoleReference(name="reviewer", version="v1alpha1"),
                onApproved="final-approval",
                onChangesRequested="repair",
                onNeedsHumanDecision="final-approval",
            ),
            WorkflowRoleStep(
                id="repair",
                roleRef=WorkflowRoleReference(name="coding", version="v1alpha1"),
                maxAttempts=2,
                onSuccess="verify",
                onFailure="failed",
            ),
            WorkflowApprovalStep(
                id="final-approval",
                subjectRef="finalArtifacts",
                onApproved="completed",
                onRejected="cancelled",
            ),
            WorkflowTerminalStep(
                id="completed",
                outcome=TerminalOutcome.SUCCESS,
            ),
            WorkflowTerminalStep(
                id="failed",
                outcome=TerminalOutcome.FAILURE,
            ),
            WorkflowTerminalStep(
                id="cancelled",
                outcome=TerminalOutcome.CANCELLED,
            ),
        ),
    )


def test_workflow_serializes_and_deserializes() -> None:
    workflow = Workflow.new(name="software-delivery", spec=valid_workflow_spec())

    payload = workflow.model_dump(mode="json", by_alias=True)
    round_tripped = Workflow.model_validate(payload)

    assert payload["kind"] == "Workflow"
    assert payload["spec"]["version"] == "v1alpha1"
    assert round_tripped == workflow


def test_duplicate_step_ids_are_rejected() -> None:
    with pytest.raises(ValidationError):
        WorkflowSpec(
            version="v1alpha1",
            entrypoint="planning",
            steps=(
                WorkflowRoleStep(
                    id="planning",
                    roleRef=WorkflowRoleReference(name="planner", version="v1alpha1"),
                    onSuccess="completed",
                ),
                WorkflowTerminalStep(
                    id="planning",
                    outcome=TerminalOutcome.SUCCESS,
                ),
            ),
        )


def test_invalid_entrypoint_is_rejected() -> None:
    with pytest.raises(ValidationError):
        WorkflowSpec(
            version="v1alpha1",
            entrypoint="missing",
            steps=(
                WorkflowTerminalStep(id="completed", outcome=TerminalOutcome.SUCCESS),
            ),
        )


def test_missing_transition_targets_are_rejected() -> None:
    with pytest.raises(ValidationError):
        WorkflowSpec(
            version="v1alpha1",
            entrypoint="planning",
            steps=(
                WorkflowRoleStep(
                    id="planning",
                    roleRef=WorkflowRoleReference(name="planner", version="v1alpha1"),
                    onSuccess="missing",
                ),
                WorkflowTerminalStep(id="completed", outcome=TerminalOutcome.SUCCESS),
            ),
        )


def test_unreachable_terminal_states_are_rejected() -> None:
    with pytest.raises(ValidationError):
        WorkflowSpec(
            version="v1alpha1",
            entrypoint="planning",
            steps=(
                WorkflowRoleStep(
                    id="planning",
                    roleRef=WorkflowRoleReference(name="planner", version="v1alpha1"),
                    onSuccess="completed",
                ),
                WorkflowTerminalStep(id="completed", outcome=TerminalOutcome.SUCCESS),
                WorkflowTerminalStep(id="failed", outcome=TerminalOutcome.FAILURE),
            ),
        )


def test_steps_without_terminal_path_are_rejected() -> None:
    with pytest.raises(ValidationError):
        WorkflowSpec(
            version="v1alpha1",
            entrypoint="planning",
            steps=(
                WorkflowRoleStep(
                    id="planning",
                    roleRef=WorkflowRoleReference(name="planner", version="v1alpha1"),
                    onSuccess="review",
                ),
                WorkflowRoleStep(
                    id="review",
                    roleRef=WorkflowRoleReference(name="reviewer", version="v1alpha1"),
                ),
                WorkflowTerminalStep(id="completed", outcome=TerminalOutcome.SUCCESS),
            ),
        )


def test_unbounded_cycles_are_rejected() -> None:
    with pytest.raises(ValidationError):
        WorkflowSpec(
            version="v1alpha1",
            entrypoint="first",
            steps=(
                WorkflowSystemStep(
                    id="first",
                    controller="verification",
                    onSuccess="second",
                ),
                WorkflowSystemStep(
                    id="second",
                    controller="review",
                    onSuccess="first",
                    onFailure="completed",
                ),
                WorkflowTerminalStep(id="completed", outcome=TerminalOutcome.SUCCESS),
            ),
        )


def test_bounded_cycles_are_allowed() -> None:
    spec = valid_workflow_spec()

    assert spec.entrypoint == "planning"


def test_retry_counts_must_be_finite() -> None:
    with pytest.raises(ValidationError):
        WorkflowRetryPolicy(maxAttempts=0)


def test_provider_specific_details_are_rejected() -> None:
    with pytest.raises(ValidationError):
        WorkflowRoleStep.model_validate(
            {
                "id": "planning",
                "type": "role",
                "roleRef": {"name": "planner", "version": "v1alpha1"},
                "providerRef": "ollama-local",
                "onSuccess": "completed",
            }
        )


def test_invalid_role_reference_is_rejected() -> None:
    with pytest.raises(ValidationError):
        WorkflowRoleStep.model_validate(
            {
                "id": "planning",
                "type": "role",
                "roleRef": {"name": "Planner", "version": "v1alpha1"},
                "onSuccess": "completed",
            }
        )


def test_workflow_spec_is_immutable_after_creation() -> None:
    workflow = Workflow.new(name="software-delivery", spec=valid_workflow_spec())
    changed_spec = workflow.spec.model_copy(update={"version": "v1alpha2"})

    with pytest.raises(ResourceImmutableFieldError):
        apply_workflow_spec_update(
            workflow,
            changed_spec,
            expected_resource_version=workflow.metadata.resource_version,
        )


def test_decision_step_routes_are_validated() -> None:
    spec = WorkflowSpec(
        version="v1alpha1",
        entrypoint="route-review",
        steps=(
            WorkflowDecisionStep(
                id="route-review",
                expression="review.status.verdict",
                cases={"Approve": "completed"},
                default="failed",
            ),
            WorkflowTerminalStep(id="completed", outcome=TerminalOutcome.SUCCESS),
            WorkflowTerminalStep(id="failed", outcome=TerminalOutcome.FAILURE),
        ),
    )

    assert spec.entrypoint == "route-review"
