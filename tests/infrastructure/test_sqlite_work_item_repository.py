"""Tests for SQLite WorkItem persistence."""

import asyncio
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from maestro.domain import ResourceSelector
from maestro.domain.exceptions import (
    ResourceAlreadyExistsError,
    ResourceConflictError,
    ResourceImmutableFieldError,
    ResourceTransitionError,
)
from maestro.domain.resources import ResourceReference
from maestro.domain.work_items import (
    WorkItem,
    WorkItemExecutionReference,
    WorkItemPhase,
    WorkItemPlanReference,
    WorkItemRetryPolicy,
    WorkItemRoleReference,
    WorkItemSpec,
    WorkItemStatus,
    WorkItemVerificationCommandResult,
    WorkItemVerificationSpec,
    WorkItemVerificationStatus,
)
from maestro.infrastructure.persistence import SQLiteWorkItemRepository


def valid_work_item_spec(
    execution_id: UUID | None = None,
    plan_id: UUID | None = None,
    *,
    plan_work_item_id: str = "add-health",
    verification_commands: tuple[str, ...] = ("pytest",),
    max_attempts: int = 2,
) -> WorkItemSpec:
    """Build a valid WorkItemSpec for persistence tests."""

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
        acceptanceCriteria=("GET /health returns 200",),
        verification=WorkItemVerificationSpec(commands=verification_commands),
        requestedCapabilities=("filesystem.read", "filesystem.write"),
        retryPolicy=WorkItemRetryPolicy(maxAttempts=max_attempts),
    )


def valid_work_item(
    execution_id: UUID | None = None,
    plan_id: UUID | None = None,
    *,
    name: str = "add-health",
    plan_work_item_id: str = "add-health",
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
            verification_commands=verification_commands,
            max_attempts=max_attempts,
        ),
    )


def test_work_item_persistence_round_trip(tmp_path) -> None:
    async def scenario() -> None:
        repository = SQLiteWorkItemRepository(tmp_path / "maestro.db")
        work_item = await repository.create(valid_work_item())
        loaded = await repository.get(work_item.metadata.id)

        assert loaded == work_item
        repository.close()

    asyncio.run(scenario())


def test_work_item_persistence_survives_repository_restart(tmp_path) -> None:
    async def scenario() -> None:
        database_path = tmp_path / "maestro.db"
        first_repository = SQLiteWorkItemRepository(database_path)
        work_item = await first_repository.create(valid_work_item())
        first_repository.close()

        second_repository = SQLiteWorkItemRepository(database_path)
        loaded = await second_repository.get(work_item.metadata.id)

        assert loaded.metadata.id == work_item.metadata.id
        assert loaded.spec.plan_work_item_id == "add-health"
        second_repository.close()

    asyncio.run(scenario())


def test_work_item_lookup_by_plan_work_item_id() -> None:
    async def scenario() -> None:
        repository = SQLiteWorkItemRepository(":memory:")
        plan_id = uuid4()
        work_item = await repository.create(valid_work_item(plan_id=plan_id))

        loaded = await repository.get_by_plan_work_item_id(plan_id, "add-health")

        assert loaded == work_item
        repository.close()

    asyncio.run(scenario())


def test_work_item_ids_are_unique_within_plan() -> None:
    async def scenario() -> None:
        repository = SQLiteWorkItemRepository(":memory:")
        execution_id = uuid4()
        plan_id = uuid4()
        await repository.create(valid_work_item(execution_id, plan_id))

        with pytest.raises(ResourceAlreadyExistsError):
            await repository.create(
                valid_work_item(
                    execution_id,
                    plan_id,
                    name="alternate-name",
                    plan_work_item_id="add-health",
                )
            )
        repository.close()

    asyncio.run(scenario())


def test_same_plan_work_item_id_can_exist_in_different_plans() -> None:
    async def scenario() -> None:
        repository = SQLiteWorkItemRepository(":memory:")
        execution_id = uuid4()
        first = await repository.create(valid_work_item(execution_id, uuid4()))
        second = await repository.create(
            valid_work_item(
                execution_id,
                uuid4(),
                name="add-health-v2",
                plan_work_item_id="add-health",
            )
        )

        assert first.spec.plan_ref.id != second.spec.plan_ref.id
        repository.close()

    asyncio.run(scenario())


def test_work_item_repository_lists_by_execution_plan_and_labels() -> None:
    async def scenario() -> None:
        repository = SQLiteWorkItemRepository(":memory:")
        execution_id = uuid4()
        plan_id = uuid4()
        work_item = valid_work_item(execution_id, plan_id)
        labeled_work_item = work_item.model_copy(
            update={
                "metadata": work_item.metadata.model_copy(
                    update={"labels": {"role": "coding"}}
                )
            }
        )
        await repository.create(labeled_work_item)
        await repository.create(
            valid_work_item(name="other", plan_work_item_id="other")
        )

        by_execution = await repository.list_by_execution(execution_id)
        by_plan = await repository.list_by_plan(plan_id)
        by_label = await repository.list(ResourceSelector(labels={"role": "coding"}))

        assert [work_item.metadata.name for work_item in by_execution] == ["add-health"]
        assert [work_item.metadata.name for work_item in by_plan] == ["add-health"]
        assert [work_item.metadata.name for work_item in by_label] == ["add-health"]
        repository.close()

    asyncio.run(scenario())


def test_work_item_update_status_validates_transition() -> None:
    async def scenario() -> None:
        repository = SQLiteWorkItemRepository(":memory:")
        work_item = await repository.create(valid_work_item())

        ready = await repository.update_status(
            work_item.metadata.id,
            WorkItemStatus(phase=WorkItemPhase.READY),
            expected_resource_version=work_item.metadata.resource_version,
        )

        assert ready.status.phase == WorkItemPhase.READY
        assert ready.metadata.generation == 1
        assert ready.metadata.resource_version == 2

        with pytest.raises(ResourceTransitionError):
            await repository.update_status(
                ready.metadata.id,
                WorkItemStatus(phase=WorkItemPhase.SUCCEEDED),
                expected_resource_version=ready.metadata.resource_version,
            )
        repository.close()

    asyncio.run(scenario())


def test_work_item_update_spec_uses_optimistic_concurrency() -> None:
    async def scenario() -> None:
        repository = SQLiteWorkItemRepository(":memory:")
        work_item = await repository.create(valid_work_item())
        changed_spec = work_item.spec.model_copy(
            update={"constraints": ("Keep the change minimal",)}
        )

        updated = await repository.update_spec(
            work_item.metadata.id,
            changed_spec,
            expected_resource_version=work_item.metadata.resource_version,
        )

        assert updated.metadata.generation == 2
        assert updated.metadata.resource_version == 2

        with pytest.raises(ResourceConflictError):
            await repository.update_spec(
                work_item.metadata.id,
                changed_spec,
                expected_resource_version=work_item.metadata.resource_version,
            )
        repository.close()

    asyncio.run(scenario())


def test_work_item_spec_update_after_running_is_rejected() -> None:
    async def scenario() -> None:
        repository = SQLiteWorkItemRepository(":memory:")
        work_item = await repository.create(valid_work_item())
        ready = await repository.update_status(
            work_item.metadata.id,
            WorkItemStatus(phase=WorkItemPhase.READY),
            expected_resource_version=work_item.metadata.resource_version,
        )
        scheduled = await repository.update_status(
            ready.metadata.id,
            WorkItemStatus(phase=WorkItemPhase.SCHEDULED, attempt=1),
            expected_resource_version=ready.metadata.resource_version,
        )
        running = await repository.update_status(
            scheduled.metadata.id,
            WorkItemStatus(phase=WorkItemPhase.RUNNING, attempt=1),
            expected_resource_version=scheduled.metadata.resource_version,
        )

        with pytest.raises(ResourceImmutableFieldError):
            await repository.update_spec(
                running.metadata.id,
                running.spec.model_copy(update={"constraints": ("Changed",)}),
                expected_resource_version=running.metadata.resource_version,
            )
        repository.close()

    asyncio.run(scenario())


def test_work_item_stale_status_update_returns_conflict() -> None:
    async def scenario() -> None:
        repository = SQLiteWorkItemRepository(":memory:")
        work_item = await repository.create(valid_work_item())

        await repository.update_status(
            work_item.metadata.id,
            WorkItemStatus(phase=WorkItemPhase.READY),
            expected_resource_version=work_item.metadata.resource_version,
        )

        with pytest.raises(ResourceConflictError):
            await repository.update_status(
                work_item.metadata.id,
                WorkItemStatus(phase=WorkItemPhase.READY),
                expected_resource_version=work_item.metadata.resource_version,
            )
        repository.close()

    asyncio.run(scenario())


def test_work_item_success_requires_verification_evidence() -> None:
    async def scenario() -> None:
        repository = SQLiteWorkItemRepository(":memory:")
        work_item = await repository.create(valid_work_item())
        ready = await repository.update_status(
            work_item.metadata.id,
            WorkItemStatus(phase=WorkItemPhase.READY),
            expected_resource_version=work_item.metadata.resource_version,
        )
        scheduled = await repository.update_status(
            ready.metadata.id,
            WorkItemStatus(phase=WorkItemPhase.SCHEDULED, attempt=1),
            expected_resource_version=ready.metadata.resource_version,
        )
        running = await repository.update_status(
            scheduled.metadata.id,
            WorkItemStatus(phase=WorkItemPhase.RUNNING, attempt=1),
            expected_resource_version=scheduled.metadata.resource_version,
        )
        verifying = await repository.update_status(
            running.metadata.id,
            WorkItemStatus(phase=WorkItemPhase.VERIFYING, attempt=1),
            expected_resource_version=running.metadata.resource_version,
        )

        with pytest.raises(ValidationError):
            await repository.update_status(
                verifying.metadata.id,
                WorkItemStatus(
                    phase=WorkItemPhase.SUCCEEDED,
                    attempt=1,
                    resultArtifactRefs=(
                        ResourceReference(kind="Artifact", id=uuid4()),
                    ),
                ),
                expected_resource_version=verifying.metadata.resource_version,
            )

        succeeded = await repository.update_status(
            verifying.metadata.id,
            WorkItemStatus(
                phase=WorkItemPhase.SUCCEEDED,
                attempt=1,
                verification=WorkItemVerificationStatus(
                    commandResults=(
                        WorkItemVerificationCommandResult(
                            command="pytest",
                            exitCode=0,
                        ),
                    ),
                ),
            ),
            expected_resource_version=verifying.metadata.resource_version,
        )

        assert succeeded.status.phase == WorkItemPhase.SUCCEEDED
        repository.close()

    asyncio.run(scenario())
