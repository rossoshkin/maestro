"""Tests for repository revision semantics."""

import asyncio
from typing import Literal
from uuid import UUID

import pytest
from pydantic import ValidationError

from maestro.domain.exceptions import ResourceConflictError, ResourceNotFoundError
from maestro.domain.repositories import (
    ResourceRepository,
    ResourceSelector,
    apply_spec_update,
    apply_status_update,
)
from maestro.domain.resources import BaseResource, Metadata, Spec, Status


class ExampleSpec(Spec):
    """Desired state for repository tests."""

    desired_value: str


class ExampleStatus(Status):
    """Observed state for repository tests."""


class ExampleResource(BaseResource[ExampleSpec, ExampleStatus]):
    """Concrete resource used to test generic repository behavior."""

    kind: Literal["Example"] = "Example"


class InMemoryExampleRepository(
    ResourceRepository[ExampleResource, ExampleSpec, ExampleStatus]
):
    """Small test repository implementing the production interface."""

    def __init__(self) -> None:
        self._resources: dict[UUID, ExampleResource] = {}

    async def create(self, resource: ExampleResource) -> ExampleResource:
        self._resources[resource.metadata.id] = resource
        return resource

    async def get(self, resource_id: UUID) -> ExampleResource:
        resource = self._resources.get(resource_id)
        if resource is None:
            raise ResourceNotFoundError(resource_id)
        return resource

    async def list(
        self,
        selector: ResourceSelector | None = None,
    ) -> tuple[ExampleResource, ...]:
        resources = tuple(self._resources.values())
        if selector is None:
            return resources

        selected: list[ExampleResource] = []
        for resource in resources:
            namespace_matches = (
                selector.namespace is None
                or resource.metadata.namespace == selector.namespace
            )
            labels_match = all(
                resource.metadata.labels.get(key) == value
                for key, value in selector.labels.items()
            )
            if namespace_matches and labels_match:
                selected.append(resource)
        return tuple(selected)

    async def update_spec(
        self,
        resource_id: UUID,
        spec: ExampleSpec,
        *,
        expected_resource_version: int,
    ) -> ExampleResource:
        resource = await self.get(resource_id)
        updated = apply_spec_update(
            resource,
            spec,
            expected_resource_version=expected_resource_version,
        )
        self._resources[resource_id] = updated
        return updated

    async def update_status(
        self,
        resource_id: UUID,
        status: ExampleStatus,
        *,
        expected_resource_version: int,
    ) -> ExampleResource:
        resource = await self.get(resource_id)
        updated = apply_status_update(
            resource,
            status,
            expected_resource_version=expected_resource_version,
        )
        self._resources[resource_id] = updated
        return updated


def make_resource(name: str = "example") -> ExampleResource:
    """Build a valid example resource."""

    return ExampleResource(
        metadata=Metadata(name=name, labels={"area": "test"}),
        spec=ExampleSpec(desired_value="initial"),
        status=ExampleStatus(),
    )


def test_spec_update_increments_generation_and_resource_version() -> None:
    resource = make_resource()

    updated = apply_spec_update(
        resource,
        ExampleSpec(desired_value="changed"),
        expected_resource_version=1,
    )

    assert updated.metadata.generation == 2
    assert updated.metadata.resource_version == 2
    assert updated.spec.desired_value == "changed"


def test_spec_update_without_spec_change_preserves_generation() -> None:
    resource = make_resource()

    updated = apply_spec_update(
        resource,
        ExampleSpec(desired_value="initial"),
        expected_resource_version=1,
    )

    assert updated.metadata.generation == 1
    assert updated.metadata.resource_version == 2


def test_status_update_only_increments_resource_version() -> None:
    resource = make_resource()
    status = ExampleStatus(observedGeneration=1, phase="Ready")

    updated = apply_status_update(
        resource,
        status,
        expected_resource_version=1,
    )

    assert updated.metadata.generation == 1
    assert updated.metadata.resource_version == 2
    assert updated.status.phase == "Ready"


def test_status_update_revalidates_resource_invariants() -> None:
    resource = make_resource()

    with pytest.raises(ValidationError):
        apply_status_update(
            resource,
            ExampleStatus(observedGeneration=2, phase="Ready"),
            expected_resource_version=1,
        )


def test_stale_resource_version_raises_conflict() -> None:
    resource = make_resource()

    with pytest.raises(ResourceConflictError):
        apply_spec_update(
            resource,
            ExampleSpec(desired_value="changed"),
            expected_resource_version=99,
        )


def test_repository_interface_supports_optimistic_concurrency() -> None:
    async def scenario() -> None:
        repository = InMemoryExampleRepository()
        created = await repository.create(make_resource())

        updated = await repository.update_spec(
            created.metadata.id,
            ExampleSpec(desired_value="changed"),
            expected_resource_version=1,
        )

        assert updated.metadata.resource_version == 2

        with pytest.raises(ResourceConflictError):
            await repository.update_status(
                created.metadata.id,
                ExampleStatus(phase="Ready"),
                expected_resource_version=1,
            )

    asyncio.run(scenario())


def test_repository_selector_filters_by_namespace_and_labels() -> None:
    async def scenario() -> None:
        repository = InMemoryExampleRepository()
        await repository.create(make_resource("first"))
        await repository.create(
            ExampleResource(
                metadata=Metadata(
                    name="second",
                    namespace="other",
                    labels={"area": "test"},
                ),
                spec=ExampleSpec(desired_value="initial"),
                status=ExampleStatus(),
            )
        )

        selected = await repository.list(
            ResourceSelector(namespace="default", labels={"area": "test"})
        )

        assert [resource.metadata.name for resource in selected] == ["first"]

    asyncio.run(scenario())
