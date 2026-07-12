"""Generic controller framework for deterministic reconciliation."""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol
from uuid import UUID

from maestro.domain.events import (
    EventDraft,
    EventExecutionReference,
    EventPublisher,
)
from maestro.domain.exceptions import ResourceConflictError
from maestro.domain.repositories import ResourceRepository
from maestro.domain.resources import (
    BaseResource,
    Condition,
    ConditionStatus,
    ResourceReference,
    Spec,
    Status,
    utc_now,
)


class ReconcileRunStatus(StrEnum):
    """Controller runtime outcome."""

    IDLE = "Idle"
    SUCCEEDED = "Succeeded"
    FAILED = "Failed"
    STOPPED = "Stopped"


@dataclass(frozen=True, slots=True)
class ReconcileKey:
    """Stable key for one resource reconciliation."""

    kind: str
    resource_id: UUID


@dataclass(frozen=True, slots=True)
class ReconcileItem:
    """Queued reconciliation item."""

    key: ReconcileKey
    attempt: int = 1

    def next_attempt(self) -> ReconcileItem:
        """Return this item with its attempt incremented."""

        return ReconcileItem(key=self.key, attempt=self.attempt + 1)


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """Finite retry policy for controller operations."""

    max_attempts: int = 3
    max_conflict_retries: int = 3

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        if self.max_conflict_retries < 0:
            raise ValueError("max_conflict_retries must not be negative")

    def should_retry(self, attempt: int) -> bool:
        """Return whether a failed reconcile attempt should be retried."""

        return attempt < self.max_attempts


@dataclass(frozen=True, slots=True)
class ReconciliationContext:
    """Context passed to controllers during reconciliation."""

    key: ReconcileKey
    controller_name: str
    attempt: int
    retry_policy: RetryPolicy

    @property
    def resource_id(self) -> UUID:
        """Return the reconciled resource ID."""

        return self.key.resource_id

    @property
    def kind(self) -> str:
        """Return the reconciled resource kind."""

        return self.key.kind


@dataclass(frozen=True, slots=True)
class ReconcileResult:
    """Controller reconciliation result."""

    requeue: bool = False
    events: tuple[EventDraft, ...] = field(default_factory=tuple)
    message: str = ""


@dataclass(frozen=True, slots=True)
class ReconcileRun:
    """Runtime result for one queue item."""

    status: ReconcileRunStatus
    item: ReconcileItem | None = None
    requeued: bool = False
    error_message: str = ""
    published_events: int = 0


class Controller(Protocol):
    """Base controller protocol."""

    name: str
    kind: str

    async def reconcile(self, context: ReconciliationContext) -> ReconcileResult:
        """Reconcile one resource."""


class ControllerRegistry:
    """Registry of controllers by resource kind."""

    def __init__(self) -> None:
        self._controllers: dict[str, Controller] = {}

    def register(self, controller: Controller) -> None:
        """Register a controller for its resource kind."""

        if controller.kind in self._controllers:
            raise ValueError(f"Controller already registered for {controller.kind}")
        self._controllers[controller.kind] = controller

    def get(self, kind: str) -> Controller:
        """Return a controller by resource kind."""

        try:
            return self._controllers[kind]
        except KeyError as error:
            raise KeyError(f"No controller registered for {kind}") from error

    def list(self) -> tuple[Controller, ...]:
        """Return registered controllers in deterministic order."""

        return tuple(
            self._controllers[kind] for kind in sorted(self._controllers.keys())
        )


class ReconcileQueue:
    """In-memory FIFO reconcile queue with key-level deduplication."""

    def __init__(self) -> None:
        self._items: deque[ReconcileItem] = deque()
        self._queued_keys: set[ReconcileKey] = set()

    def enqueue(
        self,
        kind: str,
        resource_id: UUID,
        *,
        attempt: int = 1,
    ) -> bool:
        """Enqueue a reconciliation key unless it is already pending."""

        item = ReconcileItem(
            key=ReconcileKey(kind=kind, resource_id=resource_id),
            attempt=attempt,
        )
        if item.key in self._queued_keys:
            return False
        self._items.append(item)
        self._queued_keys.add(item.key)
        return True

    def requeue(self, item: ReconcileItem) -> bool:
        """Requeue an existing item with its attempt preserved."""

        return self.enqueue(
            item.key.kind,
            item.key.resource_id,
            attempt=item.attempt,
        )

    def dequeue(self) -> ReconcileItem | None:
        """Return the next queued item."""

        if not self._items:
            return None
        item = self._items.popleft()
        self._queued_keys.remove(item.key)
        return item

    def __len__(self) -> int:
        return len(self._items)


class ControllerRuntime:
    """Run registered controllers against a reconcile queue."""

    def __init__(
        self,
        registry: ControllerRegistry,
        queue: ReconcileQueue,
        *,
        retry_policy: RetryPolicy | None = None,
        event_publisher: EventPublisher | None = None,
    ) -> None:
        self._registry = registry
        self._queue = queue
        self._retry_policy = retry_policy or RetryPolicy()
        self._event_publisher = event_publisher
        self._running = False

    @property
    def running(self) -> bool:
        """Return whether the runtime is started."""

        return self._running

    def start(self) -> None:
        """Start the runtime."""

        self._running = True

    def stop(self) -> None:
        """Stop the runtime."""

        self._running = False

    async def run_once(self) -> ReconcileRun:
        """Run one queued reconciliation item."""

        if not self._running:
            return ReconcileRun(status=ReconcileRunStatus.STOPPED)

        item = self._queue.dequeue()
        if item is None:
            return ReconcileRun(status=ReconcileRunStatus.IDLE)

        controller = self._registry.get(item.key.kind)
        context = ReconciliationContext(
            key=item.key,
            controller_name=controller.name,
            attempt=item.attempt,
            retry_policy=self._retry_policy,
        )

        try:
            result = await controller.reconcile(context)
        except Exception as error:  # noqa: BLE001 - controller boundary.
            requeued = False
            if self._retry_policy.should_retry(item.attempt):
                requeued = self._queue.requeue(item.next_attempt())
            return ReconcileRun(
                status=ReconcileRunStatus.FAILED,
                item=item,
                requeued=requeued,
                error_message=str(error),
            )

        published_events = await self._publish_events(result.events)
        if result.requeue:
            self._queue.enqueue(item.key.kind, item.key.resource_id)

        return ReconcileRun(
            status=ReconcileRunStatus.SUCCEEDED,
            item=item,
            requeued=result.requeue,
            published_events=published_events,
        )

    async def recover[
        ResourceT: BaseResource[Any, Any],
        SpecT: Spec,
        StatusT: Status,
    ](
        self,
        kind: str,
        repository: ResourceRepository[ResourceT, SpecT, StatusT],
        is_unfinished: Callable[[ResourceT], bool],
    ) -> int:
        """Enqueue unfinished resources after a restart."""

        enqueued = 0
        for resource in await repository.list():
            if is_unfinished(resource) and self._queue.enqueue(
                kind,
                resource.metadata.id,
            ):
                enqueued += 1
        return enqueued

    async def _publish_events(self, events: tuple[EventDraft, ...]) -> int:
        if self._event_publisher is None:
            return 0
        for event in events:
            await self._event_publisher.publish(event)
        return len(events)


class StatusWriter[
    ResourceT: BaseResource[Any, Any],
    SpecT: Spec,
    StatusT: Status,
]:
    """Optimistic-concurrency-safe resource status writer."""

    def __init__(
        self,
        repository: ResourceRepository[ResourceT, SpecT, StatusT],
        *,
        event_publisher: EventPublisher | None = None,
        producer: str = "controller-framework",
        retry_policy: RetryPolicy | None = None,
    ) -> None:
        self._repository = repository
        self._event_publisher = event_publisher
        self._producer = producer
        self._retry_policy = retry_policy or RetryPolicy()

    async def update_status(
        self,
        resource_id: UUID,
        build_status: Callable[[ResourceT], StatusT],
        *,
        event_type: str | None = None,
    ) -> ResourceT:
        """Update status, retrying stale resource versions by reloading."""

        conflicts = 0
        while True:
            resource = await self._repository.get(resource_id)
            status = build_status(resource)
            try:
                updated = await self._repository.update_status(
                    resource_id,
                    status,
                    expected_resource_version=resource.metadata.resource_version,
                )
            except ResourceConflictError:
                if conflicts >= self._retry_policy.max_conflict_retries:
                    raise
                conflicts += 1
                continue

            if event_type is not None and _phase(resource) != _phase(updated):
                await self._publish_phase_event(event_type, resource, updated)
            return updated

    async def _publish_phase_event(
        self,
        event_type: str,
        before: ResourceT,
        after: ResourceT,
    ) -> None:
        if self._event_publisher is None:
            return
        await self._event_publisher.publish(
            phase_transition_event(
                event_type=event_type,
                producer=self._producer,
                before=before,
                after=after,
            )
        )


def observe_generation[StatusT: Status](
    resource: BaseResource[Any, Any],
    status: StatusT,
) -> StatusT:
    """Return status with observedGeneration set to the resource generation."""

    return status.model_copy(
        update={"observed_generation": resource.metadata.generation}
    )


def with_condition[StatusT: Status](
    resource: BaseResource[Any, Any],
    status: StatusT,
    *,
    condition_type: str,
    condition_status: ConditionStatus,
    reason: str,
    message: str = "",
) -> StatusT:
    """Return status with one updated Condition."""

    existing = next(
        (
            condition
            for condition in status.conditions
            if condition.type == condition_type
        ),
        None,
    )
    last_transition_time = (
        existing.last_transition_time
        if existing is not None and existing.status == condition_status
        else utc_now()
    )
    condition = Condition(
        type=condition_type,
        status=condition_status,
        reason=reason,
        message=message,
        observedGeneration=resource.metadata.generation,
        lastTransitionTime=last_transition_time,
    )
    return status.with_condition(condition)


def phase_transition_event(
    *,
    event_type: str,
    producer: str,
    before: BaseResource[Any, Any],
    after: BaseResource[Any, Any],
) -> EventDraft:
    """Build a deterministic EventDraft for a resource phase transition."""

    execution_ref = _execution_ref_for(after)
    return EventDraft(
        type=event_type,
        occurredAt=after.metadata.updated_at,
        producer=producer,
        correlationId=(
            f"{after.kind}:{after.metadata.id}:{after.metadata.resource_version}:phase"
        ),
        executionRef=execution_ref,
        subjectRef=ResourceReference(
            kind=after.kind,
            id=after.metadata.id,
            name=after.metadata.name,
        ),
        payload={
            "fromPhase": _phase(before),
            "toPhase": _phase(after),
            "resourceVersion": after.metadata.resource_version,
        },
    )


def _phase(resource: BaseResource[Any, Any]) -> str:
    return str(resource.status.phase)


def _execution_ref_for(resource: BaseResource[Any, Any]) -> EventExecutionReference:
    if resource.kind == "Execution":
        return EventExecutionReference(
            id=resource.metadata.id,
            name=resource.metadata.name,
        )
    for owner in resource.metadata.owner_references:
        if owner.kind == "Execution":
            return EventExecutionReference(id=owner.id, name=owner.name)
    raise ValueError(f"{resource.kind} is not owned by an Execution")
