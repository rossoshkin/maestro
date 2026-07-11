"""Typed domain exceptions for Maestro."""

from uuid import UUID


class MaestroDomainError(Exception):
    """Base exception for domain-level failures."""


class ResourceNotFoundError(MaestroDomainError):
    """Raised when a repository cannot find a resource."""

    def __init__(self, resource_id: UUID) -> None:
        self.resource_id = resource_id
        super().__init__(f"Resource not found: {resource_id}")


class ResourceNameNotFoundError(MaestroDomainError):
    """Raised when a repository cannot find a named resource."""

    def __init__(self, kind: str, namespace: str, name: str) -> None:
        self.kind = kind
        self.namespace = namespace
        self.name = name
        super().__init__(f"{kind} not found in namespace {namespace}: {name}")


class ResourceAlreadyExistsError(MaestroDomainError):
    """Raised when a create operation would violate resource uniqueness."""

    def __init__(self, kind: str, namespace: str, name: str) -> None:
        self.kind = kind
        self.namespace = namespace
        self.name = name
        super().__init__(f"{kind} already exists in namespace {namespace}: {name}")


class ResourceImmutableFieldError(MaestroDomainError):
    """Raised when an immutable field is changed."""

    def __init__(self, resource_id: object, field_name: str) -> None:
        self.resource_id = resource_id
        self.field_name = field_name
        super().__init__(
            f"Immutable field change rejected for {resource_id}: {field_name}"
        )


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


class ResourceTransitionError(MaestroDomainError):
    """Raised when a resource status transition is invalid."""

    def __init__(
        self,
        resource_id: object,
        current_phase: str,
        next_phase: str,
    ) -> None:
        self.resource_id = resource_id
        self.current_phase = current_phase
        self.next_phase = next_phase
        super().__init__(
            f"Invalid phase transition for {resource_id}: "
            f"{current_phase} -> {next_phase}"
        )


class CapabilityPolicyDeniedError(MaestroDomainError):
    """Raised when Capability admission denies scheduling."""

    def __init__(self, reason: str, message: str = "") -> None:
        self.reason = reason
        self.message = message
        detail = f"Capability policy denied: {reason}"
        if message:
            detail = f"{detail}: {message}"
        super().__init__(detail)
