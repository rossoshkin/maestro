"""Tests for SQLite Event store."""

import asyncio
from uuid import UUID, uuid4

import pytest

from maestro.domain.events import (
    EventDraft,
    EventExecutionReference,
    EventQuery,
)
from maestro.domain.exceptions import ResourceAlreadyExistsError
from maestro.domain.resources import ResourceReference
from maestro.infrastructure.persistence import SQLiteEventStore


def valid_event_draft(
    execution_id: UUID | None = None,
    *,
    event_type: str = "WorkItemSucceeded",
    correlation_id: str = "work-item-1",
    subject_id: UUID | None = None,
    payload: dict[str, object] | None = None,
) -> EventDraft:
    """Build a valid EventDraft for persistence tests."""

    return EventDraft(
        type=event_type,
        producer="work-item-controller",
        correlationId=correlation_id,
        executionRef=EventExecutionReference(
            id=execution_id or uuid4(),
            name="implement-health",
        ),
        subjectRef=ResourceReference(
            kind="WorkItem",
            id=subject_id or uuid4(),
            name="add-health",
        ),
        payload=payload or {"result": "ok"},
    )


def test_event_store_assigns_ordered_sequences_per_execution() -> None:
    async def scenario() -> None:
        store = SQLiteEventStore(":memory:")
        execution_id = uuid4()

        first = await store.append(
            valid_event_draft(
                execution_id,
                correlation_id="first",
                subject_id=uuid4(),
            )
        )
        second = await store.append(
            valid_event_draft(
                execution_id,
                event_type="WorkspaceReady",
                correlation_id="second",
                subject_id=uuid4(),
            )
        )
        other_execution = await store.append(valid_event_draft())

        assert first.spec.sequence == 1
        assert second.spec.sequence == 2
        assert other_execution.spec.sequence == 1
        assert [
            event.spec.sequence for event in await store.list_by_execution(execution_id)
        ] == [
            1,
            2,
        ]
        store.close()

    asyncio.run(scenario())


def test_event_store_duplicate_delivery_is_idempotent() -> None:
    async def scenario() -> None:
        store = SQLiteEventStore(":memory:")
        draft = valid_event_draft()

        first = await store.publish(draft)
        duplicate = await store.publish(draft)
        events = await store.list_by_execution(draft.execution_ref.id)

        assert duplicate == first
        assert events == (first,)
        store.close()

    asyncio.run(scenario())


def test_event_store_duplicate_key_with_changed_payload_is_rejected() -> None:
    async def scenario() -> None:
        store = SQLiteEventStore(":memory:")
        draft = valid_event_draft()
        await store.append(draft)
        changed = draft.model_copy(update={"payload": {"result": "changed"}})

        with pytest.raises(ResourceAlreadyExistsError):
            await store.append(changed)
        store.close()

    asyncio.run(scenario())


def test_event_store_queries_by_correlation_subject_and_type() -> None:
    async def scenario() -> None:
        store = SQLiteEventStore(":memory:")
        execution_id = uuid4()
        subject_id = uuid4()
        matching = await store.append(
            valid_event_draft(
                execution_id,
                event_type="WorkItemSucceeded",
                correlation_id="correlation-1",
                subject_id=subject_id,
            )
        )
        await store.append(
            valid_event_draft(
                execution_id,
                event_type="WorkspaceReady",
                correlation_id="correlation-1",
                subject_id=uuid4(),
            )
        )

        by_correlation = await store.list_by_correlation(execution_id, "correlation-1")
        by_query = await store.list(
            EventQuery(
                executionId=execution_id,
                type="WorkItemSucceeded",
                subjectRef=ResourceReference(kind="WorkItem", id=subject_id),
            )
        )

        assert [event.metadata.id for event in by_correlation] == [
            matching.metadata.id,
            (await store.list_by_execution(execution_id))[1].metadata.id,
        ]
        assert by_query == (matching,)
        store.close()

    asyncio.run(scenario())


def test_event_store_persistence_survives_restart(tmp_path) -> None:
    async def scenario() -> None:
        database_path = tmp_path / "maestro.db"
        first_store = SQLiteEventStore(database_path)
        draft = valid_event_draft()
        event = await first_store.append(draft)
        first_store.close()

        second_store = SQLiteEventStore(database_path)
        loaded = await second_store.get(event.metadata.id)
        events = await second_store.list_by_execution(draft.execution_ref.id)

        assert loaded == event
        assert events == (event,)
        second_store.close()

    asyncio.run(scenario())
