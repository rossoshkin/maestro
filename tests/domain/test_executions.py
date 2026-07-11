"""Tests for the Execution aggregate resource."""

from uuid import uuid4

import pytest
from pydantic import ValidationError

from maestro.domain.exceptions import (
    ResourceImmutableFieldError,
    ResourceTransitionError,
)
from maestro.domain.executions import (
    Execution,
    ExecutionPhase,
    ExecutionSpec,
    ExecutionStatus,
    ExecutionWorkflowReference,
    Goal,
    ProjectReference,
    apply_execution_spec_update,
    apply_execution_status_update,
    validate_execution_transition,
)
from maestro.domain.resources import Metadata, OwnerReference


def valid_execution_spec(project_id=None) -> ExecutionSpec:
    """Build a valid ExecutionSpec for tests."""

    return ExecutionSpec(
        projectRef=ProjectReference(
            id=project_id or uuid4(),
            name="tour-manager",
        ),
        goal=Goal(
            summary="Add health endpoint",
            description="Create GET /health.",
            acceptanceCriteria=("GET /health returns 200",),
        ),
        workflowRef=ExecutionWorkflowReference(
            name="software-delivery",
            version="v1alpha1",
        ),
        requestedRoles=("planner", "coding", "reviewer"),
    )


def valid_execution() -> Execution:
    """Build a valid Execution resource."""

    return Execution.new(
        name="add-health-endpoint",
        spec=valid_execution_spec(),
    )


def test_execution_serializes_and_deserializes() -> None:
    execution = valid_execution()

    payload = execution.model_dump(mode="json", by_alias=True)
    round_tripped = Execution.model_validate(payload)

    assert payload["kind"] == "Execution"
    assert payload["spec"]["goal"]["summary"] == "Add health endpoint"
    assert payload["status"]["phase"] == "Draft"
    assert round_tripped == execution


def test_goal_summary_is_required() -> None:
    with pytest.raises(ValidationError):
        Goal(summary="")


def test_missing_workflow_reference_is_rejected() -> None:
    payload = valid_execution_spec().model_dump(mode="json", by_alias=True)
    del payload["workflowRef"]

    with pytest.raises(ValidationError):
        ExecutionSpec.model_validate(payload)


def test_duplicate_requested_roles_are_rejected() -> None:
    with pytest.raises(ValidationError):
        ExecutionSpec(
            projectRef=ProjectReference(id=uuid4()),
            goal=Goal(summary="Do the thing"),
            workflowRef=ExecutionWorkflowReference(
                name="software-delivery",
                version="v1alpha1",
            ),
            requestedRoles=("coding", "coding"),
        )


def test_owner_reference_must_match_project_ref() -> None:
    spec = valid_execution_spec()

    with pytest.raises(ValidationError):
        Execution(
            metadata=Metadata(
                name="add-health-endpoint",
                ownerReferences=(
                    OwnerReference(
                        kind="Project",
                        id=uuid4(),
                        controller=True,
                    ),
                ),
            ),
            spec=spec,
            status=ExecutionStatus(),
        )


def test_execution_requires_exactly_one_project_controller_owner() -> None:
    spec = valid_execution_spec()

    with pytest.raises(ValidationError):
        Execution(
            metadata=Metadata(name="add-health-endpoint"),
            spec=spec,
            status=ExecutionStatus(),
        )


def test_valid_phase_transition_is_accepted() -> None:
    validate_execution_transition(
        uuid4(),
        ExecutionPhase.DRAFT,
        ExecutionPhase.PLANNING,
    )


def test_invalid_phase_transition_is_rejected() -> None:
    with pytest.raises(ResourceTransitionError):
        validate_execution_transition(
            uuid4(),
            ExecutionPhase.DRAFT,
            ExecutionPhase.COMPLETED,
        )


def test_terminal_execution_cannot_resume_implicitly() -> None:
    execution = valid_execution()
    completed = execution.model_copy(
        update={"status": ExecutionStatus(phase=ExecutionPhase.COMPLETED)}
    )

    with pytest.raises(ResourceTransitionError):
        apply_execution_status_update(
            completed,
            ExecutionStatus(phase=ExecutionPhase.PLANNING),
            expected_resource_version=1,
        )


def test_completed_execution_can_transition_to_archived() -> None:
    execution = valid_execution()
    completed = execution.model_copy(
        update={"status": ExecutionStatus(phase=ExecutionPhase.COMPLETED)}
    )

    archived = apply_execution_status_update(
        completed,
        ExecutionStatus(phase=ExecutionPhase.ARCHIVED),
        expected_resource_version=1,
    )

    assert archived.status.phase == ExecutionPhase.ARCHIVED


def test_goal_mutation_after_planning_is_rejected() -> None:
    execution = valid_execution()
    planning = apply_execution_status_update(
        execution,
        ExecutionStatus(phase=ExecutionPhase.PLANNING),
        expected_resource_version=1,
    )
    changed_spec = planning.spec.model_copy(
        update={"goal": Goal(summary="A different goal")}
    )

    with pytest.raises(ResourceImmutableFieldError):
        apply_execution_spec_update(
            planning,
            changed_spec,
            expected_resource_version=planning.metadata.resource_version,
        )


def test_non_goal_spec_change_after_planning_is_allowed() -> None:
    execution = valid_execution()
    planning = apply_execution_status_update(
        execution,
        ExecutionStatus(phase=ExecutionPhase.PLANNING),
        expected_resource_version=1,
    )
    changed_spec = planning.spec.model_copy(update={"cancellation_requested": True})

    updated = apply_execution_spec_update(
        planning,
        changed_spec,
        expected_resource_version=planning.metadata.resource_version,
    )

    assert updated.spec.cancellation_requested is True
    assert updated.metadata.generation == 2
