"""Common resource envelope for Maestro control-plane resources."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Literal, Self
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

API_VERSION = "maestro.dev/v1alpha1"
ApiVersion = Literal["maestro.dev/v1alpha1"]

ResourceName = Annotated[
    str,
    Field(
        min_length=1,
        max_length=63,
        pattern=r"^[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?$",
    ),
]
NamespaceName = ResourceName
ResourceKind = Annotated[str, Field(min_length=1, pattern=r"^[A-Z][A-Za-z0-9]*$")]
ConditionType = Annotated[str, Field(min_length=1, pattern=r"^[A-Z][A-Za-z0-9]*$")]
ConditionReason = Annotated[str, Field(min_length=1, pattern=r"^[A-Z][A-Za-z0-9]*$")]
PhaseName = Annotated[str, Field(min_length=1)]
FinalizerName = Annotated[str, Field(min_length=1)]

SECRET_MARKERS = (
    "apikey",
    "api-key",
    "credential",
    "password",
    "private-key",
    "secret",
    "token",
)


def utc_now() -> datetime:
    """Return the current UTC timestamp."""

    return datetime.now(UTC)


class MaestroModel(BaseModel):
    """Base model with strict resource parsing defaults."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        populate_by_name=True,
    )


class ConditionStatus(StrEnum):
    """Allowed condition status values."""

    TRUE = "True"
    FALSE = "False"
    UNKNOWN = "Unknown"


class ResourceReference(MaestroModel):
    """Stable reference to another resource."""

    id: UUID
    kind: ResourceKind
    name: ResourceName | None = None


class OwnerReference(MaestroModel):
    """Lifecycle ownership reference for subordinate resources."""

    api_version: ApiVersion = Field(
        default="maestro.dev/v1alpha1",
        alias="apiVersion",
    )
    kind: ResourceKind
    id: UUID
    name: ResourceName | None = None
    controller: bool = False
    block_owner_deletion: bool = Field(default=False, alias="blockOwnerDeletion")


class Metadata(MaestroModel):
    """Common resource metadata."""

    id: UUID = Field(default_factory=uuid4)
    name: ResourceName
    namespace: NamespaceName = "default"
    generation: int = Field(default=1, ge=1)
    resource_version: int = Field(default=1, ge=1, alias="resourceVersion")
    created_at: datetime = Field(default_factory=utc_now, alias="createdAt")
    updated_at: datetime = Field(default_factory=utc_now, alias="updatedAt")
    created_by: str = Field(default="local-user", min_length=1, alias="createdBy")
    labels: dict[str, str] = Field(default_factory=dict)
    annotations: dict[str, str] = Field(default_factory=dict)
    owner_references: tuple[OwnerReference, ...] = Field(
        default_factory=tuple,
        alias="ownerReferences",
    )
    finalizers: tuple[FinalizerName, ...] = Field(default_factory=tuple)
    deletion_timestamp: datetime | None = Field(
        default=None,
        alias="deletionTimestamp",
    )

    @field_validator("labels", "annotations")
    @classmethod
    def reject_secret_metadata(cls, value: dict[str, str]) -> dict[str, str]:
        """Reject secret-like metadata keys or values."""

        for key, item in value.items():
            normalized_key = key.lower()
            normalized_item = item.lower()
            if any(marker in normalized_key for marker in SECRET_MARKERS) or any(
                marker in normalized_item for marker in SECRET_MARKERS
            ):
                raise ValueError(
                    "metadata labels and annotations must not contain secrets"
                )
        return value

    @field_validator("finalizers")
    @classmethod
    def reject_duplicate_finalizers(
        cls,
        value: tuple[FinalizerName, ...],
    ) -> tuple[FinalizerName, ...]:
        """Reject duplicate finalizer names."""

        if len(set(value)) != len(value):
            raise ValueError("finalizers must be unique")
        return value

    @model_validator(mode="after")
    def validate_timestamps(self) -> Self:
        """Ensure update timestamps do not precede creation timestamps."""

        if self.updated_at < self.created_at:
            raise ValueError("updatedAt must not be earlier than createdAt")
        return self


class Condition(MaestroModel):
    """Observed condition attached to resource status."""

    type: ConditionType
    status: ConditionStatus
    reason: ConditionReason
    message: str = ""
    observed_generation: int = Field(ge=0, alias="observedGeneration")
    last_transition_time: datetime = Field(
        default_factory=utc_now,
        alias="lastTransitionTime",
    )


class Spec(MaestroModel):
    """Base class for desired resource state."""


class Status(MaestroModel):
    """Base class for observed resource state."""

    observed_generation: int = Field(default=0, ge=0, alias="observedGeneration")
    phase: PhaseName = "Pending"
    conditions: tuple[Condition, ...] = Field(default_factory=tuple)

    @field_validator("conditions")
    @classmethod
    def reject_duplicate_conditions(
        cls,
        value: tuple[Condition, ...],
    ) -> tuple[Condition, ...]:
        """Ensure there is at most one active condition per type."""

        condition_types = [condition.type for condition in value]
        if len(set(condition_types)) != len(condition_types):
            raise ValueError("conditions must be unique by type")
        return value

    def with_condition(self, condition: Condition) -> Self:
        """Return status with the supplied condition inserted or replaced."""

        remaining = tuple(
            existing for existing in self.conditions if existing.type != condition.type
        )
        return self.model_copy(update={"conditions": (*remaining, condition)})


class BaseResource[SpecT: Spec, StatusT: Status](MaestroModel):
    """Common `apiVersion/kind/metadata/spec/status` resource envelope."""

    api_version: ApiVersion = Field(
        default="maestro.dev/v1alpha1",
        alias="apiVersion",
    )
    kind: ResourceKind
    metadata: Metadata
    spec: SpecT
    status: StatusT

    @model_validator(mode="after")
    def validate_observed_generation(self) -> Self:
        """Reject status that claims to observe a future spec generation."""

        if self.status.observed_generation > self.metadata.generation:
            raise ValueError(
                "status.observedGeneration cannot exceed metadata.generation"
            )
        return self
