"""Tests for SQLite Plan persistence."""

import asyncio
from uuid import UUID, uuid4

import pytest

from maestro.domain import ResourceSelector
from maestro.domain.exceptions import (
    ResourceAlreadyExistsError,
    ResourceConflictError,
    ResourceImmutableFieldError,
)
from maestro.domain.plans import (
    Plan,
    PlanExecutionReference,
    PlanPhase,
    PlanRoleReference,
    PlanSpec,
    PlanStatus,
    PlanValidationResult,
    PlanWorkItemProposal,
    apply_plan_status_update,
)
from maestro.domain.resources import ResourceReference, utc_now
from maestro.infrastructure.persistence import SQLitePlanRepository


def valid_plan_spec(execution_id: UUID | None = None, *, version: int = 1) -> PlanSpec:
    """Build a valid PlanSpec for persistence tests."""

    return PlanSpec(
        executionRef=PlanExecutionReference(
            id=execution_id or uuid4(),
            name="add-health-endpoint",
        ),
        version=version,
        summary="Implement a health endpoint",
        assumptions=("FastAPI is already configured",),
        workItems=(
            PlanWorkItemProposal(
                id="add-health",
                title="Add health endpoint",
                roleRef=PlanRoleReference(name="coding", version="v1alpha1"),
                repositoryRef="backend",
                objective="Implement GET /health",
                acceptanceCriteria=("GET /health returns 200",),
                requestedCapabilities=("filesystem.read", "filesystem.write"),
            ),
        ),
    )


def valid_plan(execution_id: UUID | None = None, *, version: int = 1) -> Plan:
    """Build a valid Plan resource."""

    return Plan.new(
        name=f"add-health-plan-{version}",
        spec=valid_plan_spec(execution_id, version=version),
    )


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


def test_plan_persistence_round_trip(tmp_path) -> None:
    async def scenario() -> None:
        repository = SQLitePlanRepository(tmp_path / "maestro.db")
        plan = await repository.create(valid_plan())
        loaded = await repository.get(plan.metadata.id)

        assert loaded == plan
        repository.close()

    asyncio.run(scenario())


def test_plan_persistence_survives_repository_restart(tmp_path) -> None:
    async def scenario() -> None:
        database_path = tmp_path / "maestro.db"
        first_repository = SQLitePlanRepository(database_path)
        plan = await first_repository.create(valid_plan())
        first_repository.close()

        second_repository = SQLitePlanRepository(database_path)
        loaded = await second_repository.get(plan.metadata.id)

        assert loaded.metadata.id == plan.metadata.id
        assert loaded.spec.version == 1
        second_repository.close()

    asyncio.run(scenario())


def test_plan_lookup_by_execution_and_version() -> None:
    async def scenario() -> None:
        repository = SQLitePlanRepository(":memory:")
        execution_id = uuid4()
        plan = await repository.create(valid_plan(execution_id, version=1))

        loaded = await repository.get_by_execution_version(execution_id, 1)

        assert loaded == plan
        repository.close()

    asyncio.run(scenario())


def test_plan_versions_are_unique_per_execution() -> None:
    async def scenario() -> None:
        repository = SQLitePlanRepository(":memory:")
        execution_id = uuid4()
        await repository.create(valid_plan(execution_id, version=1))

        with pytest.raises(ResourceAlreadyExistsError):
            await repository.create(
                Plan.new(
                    name="alternate-plan-name",
                    spec=valid_plan_spec(execution_id, version=1),
                )
            )
        repository.close()

    asyncio.run(scenario())


def test_plan_new_version_can_be_registered() -> None:
    async def scenario() -> None:
        repository = SQLitePlanRepository(":memory:")
        execution_id = uuid4()
        first = await repository.create(valid_plan(execution_id, version=1))
        second_spec = valid_plan_spec(execution_id, version=2).model_copy(
            update={
                "supersedes_plan_ref": ResourceReference(
                    kind="Plan",
                    id=first.metadata.id,
                    name=first.metadata.name,
                )
            }
        )
        second = await repository.create(
            Plan.new(name="add-health-plan-2", spec=second_spec)
        )

        assert second.spec.version == 2
        assert second.spec.supersedes_plan_ref is not None
        repository.close()

    asyncio.run(scenario())


def test_plan_spec_updates_are_rejected() -> None:
    async def scenario() -> None:
        repository = SQLitePlanRepository(":memory:")
        plan = await repository.create(valid_plan())
        changed_spec = plan.spec.model_copy(update={"summary": "Changed"})

        with pytest.raises(ResourceImmutableFieldError):
            await repository.update_spec(
                plan.metadata.id,
                changed_spec,
                expected_resource_version=plan.metadata.resource_version,
            )
        repository.close()

    asyncio.run(scenario())


def test_plan_status_update_preserves_generation() -> None:
    async def scenario() -> None:
        repository = SQLitePlanRepository(":memory:")
        plan = await repository.create(valid_plan())

        updated = await repository.update_status(
            plan.metadata.id,
            approval_ready_status(),
            expected_resource_version=plan.metadata.resource_version,
        )

        assert updated.metadata.generation == 1
        assert updated.metadata.resource_version == 2
        assert updated.status.phase == PlanPhase.WAITING_FOR_APPROVAL
        repository.close()

    asyncio.run(scenario())


def test_plan_stale_update_returns_conflict() -> None:
    async def scenario() -> None:
        repository = SQLitePlanRepository(":memory:")
        plan = await repository.create(valid_plan())

        await repository.update_status(
            plan.metadata.id,
            approval_ready_status(),
            expected_resource_version=plan.metadata.resource_version,
        )

        with pytest.raises(ResourceConflictError):
            await repository.update_status(
                plan.metadata.id,
                approval_ready_status(),
                expected_resource_version=plan.metadata.resource_version,
            )
        repository.close()

    asyncio.run(scenario())


def test_only_one_approved_plan_exists_per_execution() -> None:
    async def scenario() -> None:
        repository = SQLitePlanRepository(":memory:")
        execution_id = uuid4()
        first = await repository.create(valid_plan(execution_id, version=1))
        second = await repository.create(valid_plan(execution_id, version=2))

        first_waiting = await repository.update_status(
            first.metadata.id,
            approval_ready_status(),
            expected_resource_version=first.metadata.resource_version,
        )
        await repository.update_status(
            first.metadata.id,
            approved_status(),
            expected_resource_version=first_waiting.metadata.resource_version,
        )

        second_waiting = await repository.update_status(
            second.metadata.id,
            approval_ready_status(),
            expected_resource_version=second.metadata.resource_version,
        )

        with pytest.raises(ResourceAlreadyExistsError):
            await repository.update_status(
                second.metadata.id,
                approved_status(),
                expected_resource_version=second_waiting.metadata.resource_version,
            )

        approved = await repository.get_approved_for_execution(execution_id)

        assert approved is not None
        assert approved.spec.version == 1
        repository.close()

    asyncio.run(scenario())


def test_plan_repository_lists_by_execution_and_labels() -> None:
    async def scenario() -> None:
        repository = SQLitePlanRepository(":memory:")
        execution_id = uuid4()
        plan = valid_plan(execution_id, version=1)
        labeled_plan = plan.model_copy(
            update={
                "metadata": plan.metadata.model_copy(
                    update={"labels": {"stage": "planning"}}
                )
            }
        )
        await repository.create(labeled_plan)
        await repository.create(valid_plan(version=2))

        by_execution = await repository.list_by_execution(execution_id)
        by_label = await repository.list(ResourceSelector(labels={"stage": "planning"}))

        assert [plan.metadata.name for plan in by_execution] == ["add-health-plan-1"]
        assert [plan.metadata.name for plan in by_label] == ["add-health-plan-1"]
        repository.close()

    asyncio.run(scenario())


def test_rejected_plan_can_be_marked_superseded() -> None:
    async def scenario() -> None:
        repository = SQLitePlanRepository(":memory:")
        execution_id = uuid4()
        first = await repository.create(valid_plan(execution_id, version=1))
        second = await repository.create(valid_plan(execution_id, version=2))
        waiting = await repository.update_status(
            first.metadata.id,
            approval_ready_status(),
            expected_resource_version=first.metadata.resource_version,
        )
        rejected = await repository.update_status(
            first.metadata.id,
            PlanStatus(
                observedGeneration=1,
                phase=PlanPhase.REJECTED,
                validation=PlanValidationResult(valid=True),
                rejectedBy="sashka",
                rejectedAt=utc_now(),
            ),
            expected_resource_version=waiting.metadata.resource_version,
        )

        superseded = await repository.update_status(
            first.metadata.id,
            PlanStatus(
                observedGeneration=1,
                phase=PlanPhase.SUPERSEDED,
                validation=PlanValidationResult(valid=True),
                supersededByRef=ResourceReference(
                    kind="Plan",
                    id=second.metadata.id,
                    name=second.metadata.name,
                ),
            ),
            expected_resource_version=rejected.metadata.resource_version,
        )

        assert superseded.status.phase == PlanPhase.SUPERSEDED
        repository.close()

    asyncio.run(scenario())


def test_plan_status_helpers_use_same_transition_rules() -> None:
    plan = valid_plan()
    waiting = apply_plan_status_update(
        plan,
        approval_ready_status(),
        expected_resource_version=plan.metadata.resource_version,
    )

    assert waiting.status.phase == PlanPhase.WAITING_FOR_APPROVAL
