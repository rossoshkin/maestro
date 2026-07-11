"""Tests for Plan resource validation and lifecycle rules."""

from uuid import uuid4

import pytest
from pydantic import ValidationError

from maestro.domain.exceptions import (
    ResourceImmutableFieldError,
    ResourceTransitionError,
)
from maestro.domain.plans import (
    Plan,
    PlanExecutionReference,
    PlanPhase,
    PlanRisk,
    PlanRoleReference,
    PlanSpec,
    PlanStatus,
    PlanValidationResult,
    PlanWorkItemProposal,
    PlanWorkItemVerification,
    apply_plan_spec_update,
    apply_plan_status_update,
    validate_plan_transition,
)
from maestro.domain.resources import (
    Metadata,
    OwnerReference,
    ResourceReference,
    utc_now,
)


def valid_plan_spec(execution_id=None, *, version: int = 1) -> PlanSpec:
    """Build a valid PlanSpec for tests."""

    return PlanSpec(
        executionRef=PlanExecutionReference(
            id=execution_id or uuid4(),
            name="add-health-endpoint",
        ),
        version=version,
        summary="Implement a health endpoint in small, verifiable steps",
        assumptions=("Python 3.12 is available",),
        questions=("Should the endpoint include build metadata?",),
        risks=(
            PlanRisk(
                description="The API shape may already exist",
                mitigation="Inspect existing routes before editing",
            ),
        ),
        workItems=(
            PlanWorkItemProposal(
                id="inspect-api",
                title="Inspect API structure",
                roleRef=PlanRoleReference(name="coding", version="v1alpha1"),
                repositoryRef="backend",
                objective="Find the existing FastAPI application entrypoint",
                acceptanceCriteria=("Application entrypoint is identified",),
                verification=PlanWorkItemVerification(commands=("pytest",)),
                requestedCapabilities=("filesystem.read", "shell.execute.test"),
            ),
            PlanWorkItemProposal(
                id="add-health",
                title="Add health endpoint",
                roleRef=PlanRoleReference(name="coding", version="v1alpha1"),
                repositoryRef="backend",
                objective="Implement GET /health",
                acceptanceCriteria=(
                    "GET /health returns 200",
                    "Response body includes status ok",
                ),
                verification=PlanWorkItemVerification(commands=("pytest",)),
                dependsOn=("inspect-api",),
                requestedCapabilities=(
                    "filesystem.read",
                    "filesystem.write",
                    "shell.execute.test",
                ),
            ),
        ),
    )


def valid_plan() -> Plan:
    """Build a valid Plan resource."""

    return Plan.new(name="add-health-plan-1", spec=valid_plan_spec())


def approval_ready_status() -> PlanStatus:
    """Build a Plan status ready for human approval."""

    return PlanStatus(
        observedGeneration=1,
        phase=PlanPhase.WAITING_FOR_APPROVAL,
        validation=PlanValidationResult(valid=True),
    )


def approved_status() -> PlanStatus:
    """Build an approved Plan status."""

    return PlanStatus(
        observedGeneration=1,
        phase=PlanPhase.APPROVED,
        validation=PlanValidationResult(valid=True),
        approvedBy="sashka",
        approvedAt=utc_now(),
    )


def rejected_status() -> PlanStatus:
    """Build a rejected Plan status."""

    return PlanStatus(
        observedGeneration=1,
        phase=PlanPhase.REJECTED,
        validation=PlanValidationResult(valid=True),
        rejectedBy="sashka",
        rejectedAt=utc_now(),
        rejectionReason="Need a smaller first version",
    )


def test_plan_serializes_and_deserializes() -> None:
    plan = valid_plan()

    payload = plan.model_dump(mode="json", by_alias=True)
    round_tripped = Plan.model_validate(payload)

    assert payload["kind"] == "Plan"
    assert payload["spec"]["version"] == 1
    assert payload["spec"]["workItems"][1]["dependsOn"] == ["inspect-api"]
    assert round_tripped == plan


def test_plan_requires_matching_execution_owner() -> None:
    spec = valid_plan_spec()

    with pytest.raises(ValidationError):
        Plan(
            metadata=Metadata(
                name="add-health-plan-1",
                ownerReferences=(
                    OwnerReference(
                        kind="Execution",
                        id=uuid4(),
                        controller=True,
                    ),
                ),
            ),
            spec=spec,
            status=PlanStatus(),
        )


def test_plan_requires_exactly_one_execution_controller_owner() -> None:
    with pytest.raises(ValidationError):
        Plan(
            metadata=Metadata(name="add-health-plan-1"),
            spec=valid_plan_spec(),
            status=PlanStatus(),
        )


def test_duplicate_work_item_ids_are_rejected() -> None:
    with pytest.raises(ValidationError):
        PlanSpec(
            executionRef=PlanExecutionReference(id=uuid4()),
            version=1,
            summary="Duplicate IDs",
            workItems=(
                PlanWorkItemProposal(
                    id="same",
                    title="First",
                    roleRef=PlanRoleReference(name="coding", version="v1alpha1"),
                    objective="Do first thing",
                    acceptanceCriteria=("First thing works",),
                ),
                PlanWorkItemProposal(
                    id="same",
                    title="Second",
                    roleRef=PlanRoleReference(name="coding", version="v1alpha1"),
                    objective="Do second thing",
                    acceptanceCriteria=("Second thing works",),
                ),
            ),
        )


def test_missing_role_reference_is_rejected() -> None:
    payload = PlanWorkItemProposal(
        id="add-health",
        title="Add health endpoint",
        roleRef=PlanRoleReference(name="coding", version="v1alpha1"),
        objective="Implement GET /health",
        acceptanceCriteria=("GET /health returns 200",),
    ).model_dump(mode="json", by_alias=True)
    del payload["roleRef"]

    with pytest.raises(ValidationError):
        PlanWorkItemProposal.model_validate(payload)


def test_missing_acceptance_criteria_are_rejected() -> None:
    with pytest.raises(ValidationError):
        PlanWorkItemProposal(
            id="add-health",
            title="Add health endpoint",
            roleRef=PlanRoleReference(name="coding", version="v1alpha1"),
            objective="Implement GET /health",
            acceptanceCriteria=(),
        )


def test_missing_dependency_target_is_rejected() -> None:
    with pytest.raises(ValidationError):
        PlanSpec(
            executionRef=PlanExecutionReference(id=uuid4()),
            version=1,
            summary="Missing dependency",
            workItems=(
                PlanWorkItemProposal(
                    id="add-health",
                    title="Add health endpoint",
                    roleRef=PlanRoleReference(name="coding", version="v1alpha1"),
                    objective="Implement GET /health",
                    acceptanceCriteria=("GET /health returns 200",),
                    dependsOn=("missing",),
                ),
            ),
        )


def test_dependency_cycles_are_rejected() -> None:
    with pytest.raises(ValidationError):
        PlanSpec(
            executionRef=PlanExecutionReference(id=uuid4()),
            version=1,
            summary="Cycle",
            workItems=(
                PlanWorkItemProposal(
                    id="first",
                    title="First",
                    roleRef=PlanRoleReference(name="coding", version="v1alpha1"),
                    objective="Do first thing",
                    acceptanceCriteria=("First thing works",),
                    dependsOn=("second",),
                ),
                PlanWorkItemProposal(
                    id="second",
                    title="Second",
                    roleRef=PlanRoleReference(name="coding", version="v1alpha1"),
                    objective="Do second thing",
                    acceptanceCriteria=("Second thing works",),
                    dependsOn=("first",),
                ),
            ),
        )


def test_plan_status_can_be_approval_ready() -> None:
    status = approval_ready_status()

    assert status.approval_ready is True


def test_approved_status_requires_human_audit_metadata() -> None:
    with pytest.raises(ValidationError):
        PlanStatus(phase=PlanPhase.APPROVED)


def test_valid_plan_transition_is_accepted() -> None:
    validate_plan_transition(
        uuid4(),
        PlanPhase.WAITING_FOR_APPROVAL,
        PlanPhase.APPROVED,
    )


def test_invalid_plan_transition_is_rejected() -> None:
    with pytest.raises(ResourceTransitionError):
        validate_plan_transition(
            uuid4(),
            PlanPhase.APPROVED,
            PlanPhase.REJECTED,
        )


def test_approved_plan_spec_is_immutable() -> None:
    plan = valid_plan()
    waiting = apply_plan_status_update(
        plan,
        approval_ready_status(),
        expected_resource_version=plan.metadata.resource_version,
    )
    approved = apply_plan_status_update(
        waiting,
        approved_status(),
        expected_resource_version=waiting.metadata.resource_version,
    )
    changed_spec = approved.spec.model_copy(update={"summary": "Changed"})

    with pytest.raises(ResourceImmutableFieldError):
        apply_plan_spec_update(
            approved,
            changed_spec,
            expected_resource_version=approved.metadata.resource_version,
        )


def test_rejected_plan_can_be_superseded_by_new_version() -> None:
    plan = valid_plan()
    waiting = apply_plan_status_update(
        plan,
        approval_ready_status(),
        expected_resource_version=plan.metadata.resource_version,
    )
    rejected = apply_plan_status_update(
        waiting,
        rejected_status(),
        expected_resource_version=waiting.metadata.resource_version,
    )

    superseded = apply_plan_status_update(
        rejected,
        PlanStatus(
            observedGeneration=1,
            phase=PlanPhase.SUPERSEDED,
            validation=PlanValidationResult(valid=True),
            supersededByRef=ResourceReference(
                kind="Plan",
                id=uuid4(),
                name="add-health-plan-2",
            ),
        ),
        expected_resource_version=rejected.metadata.resource_version,
    )

    assert superseded.status.phase == PlanPhase.SUPERSEDED
