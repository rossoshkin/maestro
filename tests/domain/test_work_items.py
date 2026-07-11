"""Tests for WorkItem resource validation and lifecycle rules."""

from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from maestro.domain.exceptions import (
    ResourceImmutableFieldError,
    ResourceTransitionError,
)
from maestro.domain.resources import Metadata, OwnerReference, ResourceReference
from maestro.domain.work_items import (
    WorkItem,
    WorkItemDependencyReference,
    WorkItemExecutionReference,
    WorkItemPhase,
    WorkItemPlanReference,
    WorkItemReadinessReason,
    WorkItemRetryPolicy,
    WorkItemRoleReference,
    WorkItemSpec,
    WorkItemStatus,
    WorkItemVerificationCommandResult,
    WorkItemVerificationSpec,
    WorkItemVerificationStatus,
    apply_work_item_spec_update,
    apply_work_item_status_update,
    evaluate_work_item_readiness,
    validate_work_item_transition,
)


def valid_work_item_spec(
    execution_id: UUID | None = None,
    plan_id: UUID | None = None,
    *,
    plan_work_item_id: str = "add-health",
    depends_on: tuple[WorkItemDependencyReference, ...] = (),
    verification_commands: tuple[str, ...] = ("pytest",),
    max_attempts: int = 2,
) -> WorkItemSpec:
    """Build a valid WorkItemSpec for tests."""

    return WorkItemSpec(
        executionRef=WorkItemExecutionReference(
            id=execution_id or uuid4(),
            name="add-health-endpoint",
        ),
        planRef=WorkItemPlanReference(
            id=plan_id or uuid4(),
            name="add-health-plan-1",
            version=1,
        ),
        planWorkItemId=plan_work_item_id,
        roleRef=WorkItemRoleReference(name="coding", version="v1alpha1"),
        repositoryRef="backend",
        objective="Implement GET /health",
        constraints=("Do not add unrelated dependencies",),
        acceptanceCriteria=("GET /health returns 200",),
        verification=WorkItemVerificationSpec(commands=verification_commands),
        dependsOn=depends_on,
        requestedCapabilities=("filesystem.read", "filesystem.write"),
        retryPolicy=WorkItemRetryPolicy(maxAttempts=max_attempts),
    )


def valid_work_item(
    execution_id: UUID | None = None,
    plan_id: UUID | None = None,
    *,
    name: str = "add-health",
    plan_work_item_id: str = "add-health",
    depends_on: tuple[WorkItemDependencyReference, ...] = (),
    verification_commands: tuple[str, ...] = ("pytest",),
    max_attempts: int = 2,
) -> WorkItem:
    """Build a valid WorkItem resource."""

    return WorkItem.new(
        name=name,
        spec=valid_work_item_spec(
            execution_id,
            plan_id,
            plan_work_item_id=plan_work_item_id,
            depends_on=depends_on,
            verification_commands=verification_commands,
            max_attempts=max_attempts,
        ),
    )


def with_status(work_item: WorkItem, status: WorkItemStatus) -> WorkItem:
    """Return a WorkItem snapshot with a replaced status."""

    return WorkItem(
        metadata=work_item.metadata,
        spec=work_item.spec,
        status=status,
    )


def succeeded_status() -> WorkItemStatus:
    """Build a successful status without configured verification commands."""

    return WorkItemStatus(phase=WorkItemPhase.SUCCEEDED, attempt=1)


def succeeded_verified_status() -> WorkItemStatus:
    """Build a successful status with verification evidence."""

    return WorkItemStatus(
        phase=WorkItemPhase.SUCCEEDED,
        attempt=1,
        verification=WorkItemVerificationStatus(
            commandResults=(
                WorkItemVerificationCommandResult(command="pytest", exitCode=0),
            ),
        ),
    )


def test_work_item_serializes_and_deserializes() -> None:
    work_item = valid_work_item()

    payload = work_item.model_dump(mode="json", by_alias=True)
    round_tripped = WorkItem.model_validate(payload)

    assert payload["kind"] == "WorkItem"
    assert payload["spec"]["roleRef"]["name"] == "coding"
    assert payload["spec"]["retryPolicy"]["maxAttempts"] == 2
    assert round_tripped == work_item


def test_work_item_requires_matching_execution_owner() -> None:
    spec = valid_work_item_spec()

    with pytest.raises(ValidationError):
        WorkItem(
            metadata=Metadata(
                name="add-health",
                ownerReferences=(
                    OwnerReference(
                        kind="Execution",
                        id=uuid4(),
                        controller=True,
                    ),
                ),
            ),
            spec=spec,
            status=WorkItemStatus(),
        )


def test_work_item_requires_exactly_one_execution_controller_owner() -> None:
    with pytest.raises(ValidationError):
        WorkItem(
            metadata=Metadata(name="add-health"),
            spec=valid_work_item_spec(),
            status=WorkItemStatus(),
        )


def test_missing_plan_reference_is_rejected() -> None:
    payload = valid_work_item_spec().model_dump(mode="json", by_alias=True)
    del payload["planRef"]

    with pytest.raises(ValidationError):
        WorkItemSpec.model_validate(payload)


def test_invalid_role_reference_is_rejected() -> None:
    with pytest.raises(ValidationError):
        WorkItemRoleReference(name="Coding", version="v1alpha1")


def test_missing_acceptance_criteria_are_rejected() -> None:
    payload = valid_work_item_spec().model_dump(mode="json", by_alias=True)
    payload["acceptanceCriteria"] = []

    with pytest.raises(ValidationError):
        WorkItemSpec.model_validate(payload)


def test_duplicate_dependency_references_are_rejected() -> None:
    dependency_id = uuid4()

    with pytest.raises(ValidationError):
        valid_work_item_spec(
            depends_on=(
                WorkItemDependencyReference(id=dependency_id),
                WorkItemDependencyReference(id=dependency_id),
            )
        )


def test_retry_counts_must_be_finite() -> None:
    with pytest.raises(ValidationError):
        WorkItemRetryPolicy(maxAttempts=0)


def test_work_item_cannot_depend_on_itself() -> None:
    work_item = valid_work_item()

    with pytest.raises(ValidationError):
        WorkItem(
            metadata=work_item.metadata,
            spec=valid_work_item_spec(
                work_item.spec.execution_ref.id,
                work_item.spec.plan_ref.id,
                depends_on=(WorkItemDependencyReference(id=work_item.metadata.id),),
            ),
            status=WorkItemStatus(),
        )


def test_work_item_readiness_without_dependencies_is_ready() -> None:
    decision = evaluate_work_item_readiness(valid_work_item(), ())

    assert decision.ready is True
    assert decision.reason == WorkItemReadinessReason.READY


def test_work_item_readiness_waits_for_pending_dependency() -> None:
    execution_id = uuid4()
    plan_id = uuid4()
    dependency = valid_work_item(
        execution_id,
        plan_id,
        name="inspect-api",
        plan_work_item_id="inspect-api",
        verification_commands=(),
    )
    dependent = valid_work_item(
        execution_id,
        plan_id,
        depends_on=(WorkItemDependencyReference(id=dependency.metadata.id),),
    )

    decision = evaluate_work_item_readiness(dependent, (dependency,))

    assert decision.ready is False
    assert decision.blocked is False
    assert decision.reason == WorkItemReadinessReason.WAITING_FOR_DEPENDENCIES


def test_failed_dependency_blocks_dependent_work_item() -> None:
    execution_id = uuid4()
    plan_id = uuid4()
    dependency = with_status(
        valid_work_item(
            execution_id,
            plan_id,
            name="inspect-api",
            plan_work_item_id="inspect-api",
            verification_commands=(),
        ),
        WorkItemStatus(phase=WorkItemPhase.FAILED, attempt=1),
    )
    dependent = valid_work_item(
        execution_id,
        plan_id,
        depends_on=(WorkItemDependencyReference(id=dependency.metadata.id),),
    )

    decision = evaluate_work_item_readiness(dependent, (dependency,))

    assert decision.ready is False
    assert decision.blocked is True
    assert decision.reason == WorkItemReadinessReason.DEPENDENCY_BLOCKED


def test_missing_dependency_blocks_dependent_work_item() -> None:
    dependent = valid_work_item(
        depends_on=(WorkItemDependencyReference(id=uuid4()),),
    )

    decision = evaluate_work_item_readiness(dependent, ())

    assert decision.ready is False
    assert decision.blocked is True
    assert decision.reason == WorkItemReadinessReason.MISSING_DEPENDENCY


def test_succeeded_dependency_allows_dependent_work_item_to_be_ready() -> None:
    execution_id = uuid4()
    plan_id = uuid4()
    dependency = with_status(
        valid_work_item(
            execution_id,
            plan_id,
            name="inspect-api",
            plan_work_item_id="inspect-api",
            verification_commands=(),
        ),
        succeeded_status(),
    )
    dependent = valid_work_item(
        execution_id,
        plan_id,
        depends_on=(WorkItemDependencyReference(id=dependency.metadata.id),),
    )

    decision = evaluate_work_item_readiness(dependent, (dependency,))

    assert decision.ready is True


def test_valid_work_item_transition_is_accepted() -> None:
    validate_work_item_transition(
        uuid4(),
        WorkItemPhase.PENDING,
        WorkItemPhase.READY,
        attempt=0,
        max_attempts=2,
    )


def test_invalid_work_item_transition_is_rejected() -> None:
    with pytest.raises(ResourceTransitionError):
        validate_work_item_transition(
            uuid4(),
            WorkItemPhase.PENDING,
            WorkItemPhase.SUCCEEDED,
            attempt=0,
            max_attempts=2,
        )


def test_failed_work_item_can_retry_before_limit() -> None:
    failed = with_status(
        valid_work_item(max_attempts=2),
        WorkItemStatus(phase=WorkItemPhase.FAILED, attempt=1),
    )

    retried = apply_work_item_status_update(
        failed,
        WorkItemStatus(phase=WorkItemPhase.READY, attempt=1),
        expected_resource_version=failed.metadata.resource_version,
    )

    assert retried.status.phase == WorkItemPhase.READY


def test_failed_work_item_cannot_retry_at_limit() -> None:
    failed = with_status(
        valid_work_item(max_attempts=1),
        WorkItemStatus(phase=WorkItemPhase.FAILED, attempt=1),
    )

    with pytest.raises(ResourceTransitionError):
        apply_work_item_status_update(
            failed,
            WorkItemStatus(phase=WorkItemPhase.READY, attempt=1),
            expected_resource_version=failed.metadata.resource_version,
        )


def test_attempt_cannot_exceed_retry_policy() -> None:
    work_item = valid_work_item(max_attempts=1)

    with pytest.raises(ValidationError):
        WorkItem(
            metadata=work_item.metadata,
            spec=work_item.spec,
            status=WorkItemStatus(phase=WorkItemPhase.RUNNING, attempt=2),
        )


def test_agent_result_alone_cannot_mark_success_when_verification_configured() -> None:
    verifying = with_status(
        valid_work_item(),
        WorkItemStatus(phase=WorkItemPhase.VERIFYING, attempt=1),
    )

    with pytest.raises(ValidationError):
        apply_work_item_status_update(
            verifying,
            WorkItemStatus(
                phase=WorkItemPhase.SUCCEEDED,
                attempt=1,
                resultArtifactRefs=(ResourceReference(kind="Artifact", id=uuid4()),),
            ),
            expected_resource_version=verifying.metadata.resource_version,
        )


def test_verification_evidence_allows_success() -> None:
    verifying = with_status(
        valid_work_item(),
        WorkItemStatus(phase=WorkItemPhase.VERIFYING, attempt=1),
    )

    succeeded = apply_work_item_status_update(
        verifying,
        succeeded_verified_status(),
        expected_resource_version=verifying.metadata.resource_version,
    )

    assert succeeded.status.phase == WorkItemPhase.SUCCEEDED


def test_spec_owner_fields_are_immutable() -> None:
    work_item = valid_work_item()
    changed_spec = work_item.spec.model_copy(
        update={"role_ref": WorkItemRoleReference(name="reviewer", version="v1alpha1")}
    )

    with pytest.raises(ResourceImmutableFieldError):
        apply_work_item_spec_update(
            work_item,
            changed_spec,
            expected_resource_version=work_item.metadata.resource_version,
        )
