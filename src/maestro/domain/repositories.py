"""Repository contracts and revision helpers for Maestro resources."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol
from uuid import UUID

from maestro.domain.exceptions import ResourceConflictError
from maestro.domain.resources import BaseResource, Spec, Status, utc_now


@dataclass(frozen=True, slots=True)
class ResourceSelector:
    """Selection criteria for listing resources."""

    namespace: str | None = None
    labels: dict[str, str] = field(default_factory=dict)


class ResourceRepository[
    ResourceT: BaseResource[Any, Any],
    SpecT: Spec,
    StatusT: Status,
](Protocol):
    """Persistence contract for a single resource kind."""

    async def create(self, resource: ResourceT) -> ResourceT:
        """Persist a new resource."""

    async def get(self, resource_id: UUID) -> ResourceT:
        """Load a resource by stable ID."""

    async def list(
        self,
        selector: ResourceSelector | None = None,
    ) -> tuple[ResourceT, ...]:
        """List resources matching optional selection criteria."""

    async def update_spec(
        self,
        resource_id: UUID,
        spec: SpecT,
        *,
        expected_resource_version: int,
    ) -> ResourceT:
        """Persist a spec update using optimistic concurrency."""

    async def update_status(
        self,
        resource_id: UUID,
        status: StatusT,
        *,
        expected_resource_version: int,
    ) -> ResourceT:
        """Persist a status update using optimistic concurrency."""


def ensure_expected_resource_version[ResourceT: BaseResource[Any, Any]](
    resource: ResourceT,
    expected_resource_version: int,
) -> None:
    """Raise if a resource does not have the expected resourceVersion."""

    actual_resource_version = resource.metadata.resource_version
    if actual_resource_version != expected_resource_version:
        raise ResourceConflictError(
            resource.metadata.id,
            expected_resource_version,
            actual_resource_version,
        )


def apply_spec_update[ResourceT: BaseResource[Any, Any]](
    resource: ResourceT,
    spec: Spec,
    *,
    expected_resource_version: int,
    now: datetime | None = None,
) -> ResourceT:
    """Return a resource snapshot after a persisted spec mutation."""

    ensure_expected_resource_version(resource, expected_resource_version)
    timestamp = now or utc_now()
    spec_changed = spec != resource.spec
    metadata = resource.metadata.model_copy(
        update={
            "generation": resource.metadata.generation + (1 if spec_changed else 0),
            "resource_version": resource.metadata.resource_version + 1,
            "updated_at": timestamp,
        }
    )
    return validate_resource_snapshot(
        resource.model_copy(update={"metadata": metadata, "spec": spec})
    )


def apply_status_update[ResourceT: BaseResource[Any, Any]](
    resource: ResourceT,
    status: Status,
    *,
    expected_resource_version: int,
    now: datetime | None = None,
) -> ResourceT:
    """Return a resource snapshot after a persisted status mutation."""

    ensure_expected_resource_version(resource, expected_resource_version)
    timestamp = now or utc_now()
    metadata = resource.metadata.model_copy(
        update={
            "resource_version": resource.metadata.resource_version + 1,
            "updated_at": timestamp,
        }
    )
    return validate_resource_snapshot(
        resource.model_copy(update={"metadata": metadata, "status": status})
    )


def apply_deletion_mark[ResourceT: BaseResource[Any, Any]](
    resource: ResourceT,
    *,
    expected_resource_version: int,
    now: datetime | None = None,
) -> ResourceT:
    """Return a resource snapshot with deletionTimestamp set."""

    ensure_expected_resource_version(resource, expected_resource_version)
    timestamp = now or utc_now()
    metadata = resource.metadata.model_copy(
        update={
            "resource_version": resource.metadata.resource_version + 1,
            "updated_at": timestamp,
            "deletion_timestamp": resource.metadata.deletion_timestamp or timestamp,
        }
    )
    return validate_resource_snapshot(
        resource.model_copy(update={"metadata": metadata})
    )


def validate_resource_snapshot[ResourceT: BaseResource[Any, Any]](
    resource: ResourceT,
) -> ResourceT:
    """Re-run resource validation after constructing an updated snapshot."""

    payload = resource.model_dump(mode="python", by_alias=True)
    return type(resource).model_validate(payload)
