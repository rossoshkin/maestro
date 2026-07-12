"""Tests for the generic controller framework."""

import asyncio
from typing import Literal
from uuid import UUID, uuid4

import pytest

from maestro.application.controllers import (
    ControllerRegistry,
    ControllerRuntime,
    ReconcileQueue,
    ReconcileResult,
    ReconcileRunStatus,
    ReconciliationContext,
    RetryPolicy,
    StatusWriter,
    observe_generation,
    with_condition,
)
from maestro.domain.events import EventDraft
from maestro.domain.exceptions import ResourceConflictError
from maestro.domain.repositories import (
    ResourceSelector,
    apply_spec_update,
    apply_status_update,
)
from maestro.domain.resources import (
    BaseResource,
    ConditionStatus,
    Metadata,
    OwnerReference,
    Spec,
    Status,
)


class WidgetSpec(Spec):
    """Tiny desired state for controller framework tests."""

    value: str = "desired"


class WidgetStatus(Status):
    """Tiny observed state for controller framework tests."""

    phase: str = "Pending"
    failure_message: str = ""


class Widget(BaseResource[WidgetSpec, WidgetStatus]):
    """Tiny resource for controller framework tests."""

    kind: Literal["Widget"] = "Widget"

    @classmethod
    def new(cls, name: str, *, execution_id: UUID | None = None) -> "Widget":
        owner_id = execution_id or uuid4()
        return cls(
            metadata=Metadata(
                name=name,
                ownerReferences=(
                    OwnerReference(
                        kind="Execution",
                        id=owner_id,
                        name="execution-1",
                        controller=True,
                    ),
                ),
            ),
            spec=WidgetSpec(),
            status=WidgetStatus(),
        )


class WidgetRepository:
    """In-memory Widget repository for tests."""

    def __init__(self, *, conflict_once: bool = False) -> None:
        self.resources: dict[UUID, Widget] = {}
        self.update_count = 0
        self._conflict_once = conflict_once

    async def create(self, resource: Widget) -> Widget:
        self.resources[resource.metadata.id] = resource
        return resource

    async def get(self, resource_id: UUID) -> Widget:
        return self.resources[resource_id]

    async def list(
        self,
        selector: ResourceSelector | None = None,
    ) -> tuple[Widget, ...]:
        return tuple(self.resources.values())

    async def update_spec(
        self,
        resource_id: UUID,
        spec: WidgetSpec,
        *,
        expected_resource_version: int,
    ) -> Widget:
        resource = self.resources[resource_id]
        updated = apply_spec_update(
            resource,
            spec,
            expected_resource_version=expected_resource_version,
        )
        self.resources[resource_id] = updated
        return updated

    async def update_status(
        self,
        resource_id: UUID,
        status: WidgetStatus,
        *,
        expected_resource_version: int,
    ) -> Widget:
        resource = self.resources[resource_id]
        if self._conflict_once:
            self._conflict_once = False
            bumped = apply_status_update(
                resource,
                resource.status,
                expected_resource_version=resource.metadata.resource_version,
            )
            self.resources[resource_id] = bumped
            raise ResourceConflictError(
                resource_id,
                expected_resource_version,
                bumped.metadata.resource_version,
            )

        updated = apply_status_update(
            resource,
            status,
            expected_resource_version=expected_resource_version,
        )
        self.resources[resource_id] = updated
        self.update_count += 1
        return updated


class RecordingPublisher:
    """Event publisher that records drafts."""

    def __init__(self) -> None:
        self.events: list[EventDraft] = []

    async def publish(self, draft: EventDraft) -> object:
        self.events.append(draft)
        return object()


class ReadyWidgetController:
    """Idempotent controller that marks Widgets ready once."""

    name = "ready-widget-controller"
    kind = "Widget"

    def __init__(
        self,
        repository: WidgetRepository,
        publisher: RecordingPublisher | None = None,
    ) -> None:
        self._repository = repository
        self._writer = StatusWriter(
            repository,
            event_publisher=publisher,
            producer=self.name,
        )

    async def reconcile(self, context: ReconciliationContext) -> ReconcileResult:
        resource = await self._repository.get(context.resource_id)
        if resource.status.phase == "Ready":
            return ReconcileResult()

        await self._writer.update_status(
            context.resource_id,
            lambda current: observe_generation(
                current,
                current.status.model_copy(update={"phase": "Ready"}),
            ),
            event_type="WidgetPhaseChanged",
        )
        return ReconcileResult()


class FlakyController:
    """Controller that fails once and then succeeds."""

    name = "flaky-controller"
    kind = "Widget"

    async def reconcile(self, context: ReconciliationContext) -> ReconcileResult:
        if context.attempt == 1:
            raise RuntimeError("transient controller failure")
        return ReconcileResult()


def test_reconcile_queue_deduplicates_duplicate_resources() -> None:
    queue = ReconcileQueue()
    resource_id = uuid4()

    first = queue.enqueue("Widget", resource_id)
    second = queue.enqueue("Widget", resource_id)

    assert first is True
    assert second is False
    assert len(queue) == 1


def test_controller_runtime_lifecycle_and_idempotent_reconcile() -> None:
    async def scenario() -> None:
        repository = WidgetRepository()
        publisher = RecordingPublisher()
        resource = await repository.create(Widget.new("widget-1"))
        registry = ControllerRegistry()
        registry.register(ReadyWidgetController(repository, publisher))
        queue = ReconcileQueue()
        runtime = ControllerRuntime(registry, queue)

        queue.enqueue("Widget", resource.metadata.id)
        stopped = await runtime.run_once()
        runtime.start()
        first = await runtime.run_once()
        queue.enqueue("Widget", resource.metadata.id)
        second = await runtime.run_once()

        assert stopped.status == ReconcileRunStatus.STOPPED
        assert first.status == ReconcileRunStatus.SUCCEEDED
        assert second.status == ReconcileRunStatus.SUCCEEDED
        assert repository.update_count == 1
        assert (await repository.get(resource.metadata.id)).status.phase == "Ready"
        assert len(publisher.events) == 1
        assert publisher.events[0].event_type == "WidgetPhaseChanged"

    asyncio.run(scenario())


def test_status_writer_retries_stale_resource_versions() -> None:
    async def scenario() -> None:
        repository = WidgetRepository(conflict_once=True)
        resource = await repository.create(Widget.new("widget-1"))
        writer = StatusWriter(
            repository, retry_policy=RetryPolicy(max_conflict_retries=1)
        )

        updated = await writer.update_status(
            resource.metadata.id,
            lambda current: observe_generation(
                current,
                current.status.model_copy(update={"phase": "Ready"}),
            ),
        )

        assert updated.status.phase == "Ready"
        assert updated.metadata.generation == 1
        assert updated.metadata.resource_version == 3
        assert repository.update_count == 1

    asyncio.run(scenario())


def test_condition_helper_replaces_condition_and_preserves_transition_time() -> None:
    async def scenario() -> None:
        repository = WidgetRepository()
        resource = await repository.create(Widget.new("widget-1"))
        first_status = with_condition(
            resource,
            observe_generation(resource, resource.status),
            condition_type="Ready",
            condition_status=ConditionStatus.TRUE,
            reason="WidgetReady",
            message="Widget is ready",
        )
        first = await repository.update_status(
            resource.metadata.id,
            first_status,
            expected_resource_version=resource.metadata.resource_version,
        )
        first_transition = first.status.conditions[0].last_transition_time
        second_status = with_condition(
            first,
            first.status,
            condition_type="Ready",
            condition_status=ConditionStatus.TRUE,
            reason="StillReady",
            message="Still ready",
        )

        second = await repository.update_status(
            first.metadata.id,
            second_status,
            expected_resource_version=first.metadata.resource_version,
        )

        assert len(second.status.conditions) == 1
        assert second.status.conditions[0].reason == "StillReady"
        assert second.status.conditions[0].observed_generation == 1
        assert second.status.conditions[0].last_transition_time == first_transition
        assert second.metadata.generation == 1

    asyncio.run(scenario())


def test_status_writer_emits_deterministic_phase_transition_event() -> None:
    async def scenario() -> None:
        repository = WidgetRepository()
        publisher = RecordingPublisher()
        resource = await repository.create(Widget.new("widget-1"))
        writer = StatusWriter(
            repository,
            event_publisher=publisher,
            producer="widget-controller",
        )

        updated = await writer.update_status(
            resource.metadata.id,
            lambda current: observe_generation(
                current,
                current.status.model_copy(update={"phase": "Ready"}),
            ),
            event_type="WidgetPhaseChanged",
        )

        event = publisher.events[0]
        assert event.execution_ref.name == "execution-1"
        assert event.subject_ref.id == resource.metadata.id
        assert event.correlation_id == (
            f"Widget:{resource.metadata.id}:{updated.metadata.resource_version}:phase"
        )
        assert event.payload["fromPhase"] == "Pending"
        assert event.payload["toPhase"] == "Ready"
        assert event.payload["resourceVersion"] == updated.metadata.resource_version

    asyncio.run(scenario())


def test_controller_runtime_retries_failures_and_preserves_error_evidence() -> None:
    async def scenario() -> None:
        resource_id = uuid4()
        registry = ControllerRegistry()
        registry.register(FlakyController())
        queue = ReconcileQueue()
        queue.enqueue("Widget", resource_id)
        runtime = ControllerRuntime(
            registry,
            queue,
            retry_policy=RetryPolicy(max_attempts=2),
        )
        runtime.start()

        failed = await runtime.run_once()
        succeeded = await runtime.run_once()

        assert failed.status == ReconcileRunStatus.FAILED
        assert failed.requeued is True
        assert failed.error_message == "transient controller failure"
        assert succeeded.status == ReconcileRunStatus.SUCCEEDED

    asyncio.run(scenario())


def test_controller_runtime_recovers_unfinished_resources_after_restart() -> None:
    async def scenario() -> None:
        repository = WidgetRepository()
        pending = await repository.create(Widget.new("pending-widget"))
        ready = await repository.create(Widget.new("ready-widget"))
        await repository.update_status(
            ready.metadata.id,
            ready.status.model_copy(update={"phase": "Ready"}),
            expected_resource_version=ready.metadata.resource_version,
        )
        registry = ControllerRegistry()
        registry.register(ReadyWidgetController(repository))
        queue = ReconcileQueue()
        runtime = ControllerRuntime(registry, queue)

        enqueued = await runtime.recover(
            "Widget",
            repository,
            lambda resource: resource.status.phase != "Ready",
        )
        runtime.start()
        result = await runtime.run_once()

        assert enqueued == 1
        assert result.status == ReconcileRunStatus.SUCCEEDED
        assert (await repository.get(pending.metadata.id)).status.phase == "Ready"
        assert len(queue) == 0

    asyncio.run(scenario())


def test_controller_registry_rejects_duplicate_kinds() -> None:
    registry = ControllerRegistry()
    registry.register(FlakyController())

    with pytest.raises(ValueError):
        registry.register(FlakyController())
