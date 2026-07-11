"""Typed domain exceptions for Maestro."""

from uuid import UUID


class MaestroDomainError(Exception):
    """Base exception for domain-level failures."""


class ResourceNotFoundError(MaestroDomainError):
    """Raised when a repository cannot find a resource."""

    def __init__(self, resource_id: UUID) -> None:
        self.resource_id = resource_id
        super().__init__(f"Resource not found: {resource_id}")


class ResourceConflictError(MaestroDomainError):
    """Raised when optimistic concurrency detects a stale resource version."""

    def __init__(
        self,
        resource_id: UUID,
        expected_resource_version: int,
        actual_resource_version: int,
    ) -> None:
        self.resource_id = resource_id
        self.expected_resource_version = expected_resource_version
        self.actual_resource_version = actual_resource_version
        super().__init__(
            "Resource version conflict for "
            f"{resource_id}: expected {expected_resource_version}, "
            f"actual {actual_resource_version}"
        )
