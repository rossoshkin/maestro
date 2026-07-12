"""Tests for Event resources."""

from uuid import uuid4

import pytest
from pydantic import ValidationError

from maestro.domain.events import (
    Event,
    EventDraft,
    EventExecutionReference,
    EventSpec,
    event_name,
    event_spec_matches_draft,
)
from maestro.domain.resources import Metadata, OwnerReference, ResourceReference


def valid_event_draft() -> EventDraft:
    """Build a valid EventDraft for tests."""

    execution_id = uuid4()
    return EventDraft(
        type="WorkItemSucceeded",
        producer="work-item-controller",
        correlationId="work-item-1",
        executionRef=EventExecutionReference(
            id=execution_id,
            name="implement-health",
        ),
        subjectRef=ResourceReference(kind="WorkItem", id=uuid4(), name="add-health"),
        payload={"result": "ok", "attempt": 1},
    )


def valid_event() -> Event:
    """Build a valid Event resource."""

    draft = valid_event_draft()
    spec = EventSpec.from_draft(draft, sequence=1)
    return Event.new(name=event_name(draft.execution_ref.id, 1), spec=spec)


def test_event_serializes_and_deserializes() -> None:
    event = valid_event()

    payload = event.model_dump(mode="json", by_alias=True)
    round_tripped = Event.model_validate(payload)

    assert payload["kind"] == "Event"
    assert payload["spec"]["sequence"] == 1
    assert payload["spec"]["type"] == "WorkItemSucceeded"
    assert payload["status"]["phase"] == "Recorded"
    assert round_tripped == event


def test_event_requires_matching_execution_owner() -> None:
    draft = valid_event_draft()
    spec = EventSpec.from_draft(draft, sequence=1)

    with pytest.raises(ValidationError):
        Event(
            metadata=Metadata(
                name=event_name(draft.execution_ref.id, 1),
                ownerReferences=(OwnerReference(kind="Execution", id=uuid4()),),
            ),
            spec=spec,
            status={"phase": "Recorded", "observedGeneration": 1},
        )


def test_event_payload_keys_must_be_non_empty() -> None:
    draft = valid_event_draft()
    payload = draft.model_dump(mode="python", by_alias=True)
    payload["payload"] = {"": "bad"}

    with pytest.raises(ValidationError):
        EventDraft.model_validate(payload)


def test_event_name_is_stable_and_resource_safe() -> None:
    execution_id = uuid4()

    name = event_name(execution_id, 42)

    assert name.startswith("event-")
    assert name.endswith("-000000000042")
    assert len(name) <= 63


def test_event_spec_matches_draft_for_duplicate_detection() -> None:
    draft = valid_event_draft()
    event = Event.new(
        name=event_name(draft.execution_ref.id, 7),
        spec=EventSpec.from_draft(draft, sequence=7),
    )

    assert event_spec_matches_draft(event, draft) is True

    changed = draft.model_copy(update={"payload": {"result": "changed"}})

    assert event_spec_matches_draft(event, changed) is False
