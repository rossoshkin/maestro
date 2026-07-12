"""Immutable Event resources and append-only Event store contracts."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal, Protocol, Self
from uuid import UUID

from pydantic import Field, field_validator, model_validator

from maestro.domain.resources import (
    BaseResource,
    MaestroModel,
    Metadata,
    OwnerReference,
    ResourceName,
    ResourceReference,
    Spec,
    Status,
    utc_now,
)

EventType = Annotated[str, Field(min_length=1, max_length=128)]
EventProducer = Annotated[str, Field(min_length=1, max_length=128)]
CorrelationId = Annotated[str, Field(min_length=1, max_length=128)]
type JsonValue = (
    str | int | float | bool | None | tuple["JsonValue", ...] | dict[str, "JsonValue"]
)
EventPayload = dict[str, JsonValue]


class EventExecutionReference(MaestroModel):
    """Reference to the Execution whose history includes this Event."""

    kind: Literal["Execution"] = "Execution"
    id: UUID
    name: ResourceName | None = None


class EventDraft(MaestroModel):
    """Event input before the EventStore assigns sequence and resource metadata."""

    event_type: EventType = Field(alias="type")
    occurred_at: datetime = Field(default_factory=utc_now, alias="occurredAt")
    producer: EventProducer
    correlation_id: CorrelationId = Field(alias="correlationId")
    execution_ref: EventExecutionReference = Field(alias="executionRef")
    subject_ref: ResourceReference = Field(alias="subjectRef")
    payload: EventPayload = Field(default_factory=dict)

    @field_validator("payload")
    @classmethod
    def reject_empty_payload_keys(cls, value: EventPayload) -> EventPayload:
        """Reject ambiguous empty payload keys."""

        if any(key == "" for key in value):
            raise ValueError("payload keys must be non-empty")
        return value


class EventSpec(Spec):
    """Immutable Event fact."""

    sequence: int = Field(ge=1)
    event_type: EventType = Field(alias="type")
    occurred_at: datetime = Field(alias="occurredAt")
    producer: EventProducer
    correlation_id: CorrelationId = Field(alias="correlationId")
    execution_ref: EventExecutionReference = Field(alias="executionRef")
    subject_ref: ResourceReference = Field(alias="subjectRef")
    payload: EventPayload = Field(default_factory=dict)

    @field_validator("payload")
    @classmethod
    def reject_empty_payload_keys(cls, value: EventPayload) -> EventPayload:
        """Reject ambiguous empty payload keys."""

        if any(key == "" for key in value):
            raise ValueError("payload keys must be non-empty")
        return value

    @classmethod
    def from_draft(cls, draft: EventDraft, *, sequence: int) -> Self:
        """Create an EventSpec with an assigned sequence."""

        return cls(
            sequence=sequence,
            type=draft.event_type,
            occurredAt=draft.occurred_at,
            producer=draft.producer,
            correlationId=draft.correlation_id,
            executionRef=draft.execution_ref,
            subjectRef=draft.subject_ref,
            payload=draft.payload,
        )


class EventStatus(Status):
    """Observed Event record status."""

    phase: Literal["Recorded"] = "Recorded"
    observed_generation: int = Field(default=1, ge=1, alias="observedGeneration")


class Event(BaseResource[EventSpec, EventStatus]):
    """Immutable statement that something occurred."""

    kind: Literal["Event"] = "Event"

    @model_validator(mode="after")
    def validate_event_metadata(self) -> Self:
        """Require exactly one matching Execution owner reference."""

        execution_owners = tuple(
            owner
            for owner in self.metadata.owner_references
            if owner.kind == "Execution"
        )
        if len(execution_owners) != 1:
            raise ValueError("Event must have exactly one Execution owner")

        execution_owner = execution_owners[0]
        if execution_owner.id != self.spec.execution_ref.id:
            raise ValueError("Event Execution owner must match spec.executionRef")

        return self

    @classmethod
    def new(
        cls,
        *,
        name: ResourceName,
        spec: EventSpec,
        created_by: str = "event-store",
        namespace: ResourceName = "default",
    ) -> Self:
        """Create a new recorded Event resource."""

        return cls(
            metadata=Metadata(
                name=name,
                namespace=namespace,
                createdBy=created_by,
                ownerReferences=(
                    OwnerReference(
                        kind="Execution",
                        id=spec.execution_ref.id,
                        name=spec.execution_ref.name,
                        blockOwnerDeletion=True,
                    ),
                ),
            ),
            spec=spec,
            status=EventStatus(),
        )


class EventQuery(MaestroModel):
    """Event query filters."""

    execution_id: UUID | None = Field(default=None, alias="executionId")
    event_type: EventType | None = Field(default=None, alias="type")
    correlation_id: CorrelationId | None = Field(default=None, alias="correlationId")
    subject_ref: ResourceReference | None = Field(default=None, alias="subjectRef")


class EventPublisher(Protocol):
    """Event publication boundary."""

    async def publish(self, draft: EventDraft) -> Event:
        """Publish an Event draft."""


class EventStore(EventPublisher, Protocol):
    """Append-only Event store contract."""

    async def append(self, draft: EventDraft) -> Event:
        """Append an Event draft, assigning sequence within its Execution."""

    async def get(self, resource_id: UUID) -> Event:
        """Load an Event by ID."""

    async def list(
        self,
        query: EventQuery | None = None,
    ) -> tuple[Event, ...]:
        """List Events matching optional filters."""

    async def list_by_execution(self, execution_id: UUID) -> tuple[Event, ...]:
        """List Events for one Execution ordered by sequence."""

    async def list_by_correlation(
        self,
        execution_id: UUID,
        correlation_id: str,
    ) -> tuple[Event, ...]:
        """List Events sharing one correlation ID within an Execution."""


def event_name(execution_id: UUID, sequence: int) -> ResourceName:
    """Build a deterministic ResourceName for an Event sequence."""

    return f"event-{execution_id.hex[:12]}-{sequence:012d}"


def event_spec_matches_draft(event: Event, draft: EventDraft) -> bool:
    """Return whether an existing Event is an idempotent duplicate of a draft."""

    expected = EventSpec.from_draft(draft, sequence=event.spec.sequence)
    return event.spec == expected
