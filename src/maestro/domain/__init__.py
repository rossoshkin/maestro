"""Provider-independent domain model for Maestro."""

from maestro.domain.exceptions import (
    MaestroDomainError,
    ResourceConflictError,
    ResourceNotFoundError,
)
from maestro.domain.repositories import (
    ResourceRepository,
    ResourceSelector,
    apply_spec_update,
    apply_status_update,
    ensure_expected_resource_version,
    validate_resource_snapshot,
)
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

__all__ = [
    "API_VERSION",
    "BaseResource",
    "Condition",
    "ConditionStatus",
    "MaestroDomainError",
    "Metadata",
    "OwnerReference",
    "ResourceConflictError",
    "ResourceNotFoundError",
    "ResourceReference",
    "ResourceRepository",
    "ResourceSelector",
    "Spec",
    "Status",
    "apply_spec_update",
    "apply_status_update",
    "ensure_expected_resource_version",
    "validate_resource_snapshot",
]
