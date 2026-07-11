"""Tests for the common Maestro resource envelope."""

from typing import Literal
from uuid import uuid4

import pytest
from pydantic import ValidationError

from maestro.domain.resources import (
    API_VERSION,
    BaseResource,
    Condition,
    ConditionStatus,
    Metadata,
    OwnerReference,
    ResourceReference,
    Spec,
    Status,
)


class ExampleSpec(Spec):
    """Desired state for resource model tests."""

    desired_value: str


class ExampleStatus(Status):
    """Observed state for resource model tests."""


class ExampleResource(BaseResource[ExampleSpec, ExampleStatus]):
    """Concrete resource used to test the generic envelope."""

    kind: Literal["Example"] = "Example"


def make_resource() -> ExampleResource:
    """Build a valid example resource."""

    return ExampleResource(
        metadata=Metadata(name="example"),
        spec=ExampleSpec(desired_value="initial"),
        status=ExampleStatus(),
    )


def test_resource_serializes_with_canonical_aliases() -> None:
    resource = make_resource()

    payload = resource.model_dump(mode="json", by_alias=True)

    assert payload["apiVersion"] == API_VERSION
    assert payload["kind"] == "Example"
    assert payload["metadata"]["resourceVersion"] == 1
    assert payload["metadata"]["createdAt"]
    assert payload["status"]["observedGeneration"] == 0


def test_resource_round_trips_from_serialized_payload() -> None:
    resource = make_resource()
    payload = resource.model_dump(mode="json", by_alias=True)

    round_tripped = ExampleResource.model_validate(payload)

    assert round_tripped == resource


def test_invalid_resource_name_is_rejected() -> None:
    with pytest.raises(ValidationError):
        Metadata(name="Not_Valid")


def test_invalid_api_version_is_rejected() -> None:
    payload = make_resource().model_dump(mode="json", by_alias=True)
    payload["apiVersion"] = "maestro.dev/v2"

    with pytest.raises(ValidationError):
        ExampleResource.model_validate(payload)


def test_status_rejects_duplicate_condition_types() -> None:
    condition = Condition(
        type="Ready",
        status=ConditionStatus.TRUE,
        reason="Validated",
        observedGeneration=1,
    )

    with pytest.raises(ValidationError):
        ExampleStatus(conditions=(condition, condition))


def test_status_replaces_condition_by_type() -> None:
    false_condition = Condition(
        type="Ready",
        status=ConditionStatus.FALSE,
        reason="ValidationFailed",
        observedGeneration=1,
    )
    true_condition = Condition(
        type="Ready",
        status=ConditionStatus.TRUE,
        reason="Validated",
        observedGeneration=1,
    )

    status = ExampleStatus(conditions=(false_condition,)).with_condition(true_condition)

    assert status.conditions == (true_condition,)


def test_status_observed_generation_cannot_exceed_metadata_generation() -> None:
    with pytest.raises(ValidationError):
        ExampleResource(
            metadata=Metadata(name="example", generation=1),
            spec=ExampleSpec(desired_value="initial"),
            status=ExampleStatus(observedGeneration=2),
        )


def test_secret_like_metadata_is_rejected() -> None:
    with pytest.raises(ValidationError):
        Metadata(name="example", labels={"api-token": "value"})


def test_duplicate_finalizers_are_rejected() -> None:
    with pytest.raises(ValidationError):
        Metadata(name="example", finalizers=("cleanup.maestro.dev/run",) * 2)


def test_references_use_stable_ids_and_optional_names() -> None:
    resource_id = uuid4()

    reference = ResourceReference(id=resource_id, kind="Project")
    owner_reference = OwnerReference(
        id=resource_id,
        kind="Project",
        name="tour-manager",
        controller=True,
        blockOwnerDeletion=True,
    )

    assert reference.id == resource_id
    assert owner_reference.model_dump(by_alias=True)["blockOwnerDeletion"] is True
