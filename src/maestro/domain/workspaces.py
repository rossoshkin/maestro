"""Workspace resources, lifecycle contracts and path safety rules."""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from typing import Annotated, Literal, Protocol, Self
from uuid import UUID

from pydantic import Field, field_validator, model_validator

from maestro.domain.exceptions import (
    ResourceImmutableFieldError,
    ResourceTransitionError,
)
from maestro.domain.repositories import (
    ResourceRepository,
    apply_spec_update,
    apply_status_update,
)
from maestro.domain.resources import (
    BaseResource,
    MaestroModel,
    Metadata,
    OwnerReference,
    ResourceName,
    Spec,
    Status,
)

WORKSPACE_CLEANUP_FINALIZER = "workspace.maestro.dev/cleanup"
Revision = Annotated[str, Field(min_length=1)]
BranchName = Annotated[str, Field(min_length=1, max_length=255)]
CommandArgument = Annotated[str, Field(min_length=1)]


class WorkspacePhase(StrEnum):
    """Workspace lifecycle phases."""

    PENDING = "Pending"
    PREPARING = "Preparing"
    READY = "Ready"
    IN_USE = "InUse"
    DIRTY = "Dirty"
    RELEASING = "Releasing"
    RELEASED = "Released"
    FAILED = "Failed"


VALID_WORKSPACE_TRANSITIONS = frozenset(
    {
        (WorkspacePhase.PENDING, WorkspacePhase.PREPARING),
        (WorkspacePhase.PENDING, WorkspacePhase.FAILED),
        (WorkspacePhase.PREPARING, WorkspacePhase.READY),
        (WorkspacePhase.PREPARING, WorkspacePhase.FAILED),
        (WorkspacePhase.READY, WorkspacePhase.IN_USE),
        (WorkspacePhase.READY, WorkspacePhase.DIRTY),
        (WorkspacePhase.READY, WorkspacePhase.RELEASING),
        (WorkspacePhase.IN_USE, WorkspacePhase.READY),
        (WorkspacePhase.IN_USE, WorkspacePhase.DIRTY),
        (WorkspacePhase.IN_USE, WorkspacePhase.RELEASING),
        (WorkspacePhase.IN_USE, WorkspacePhase.FAILED),
        (WorkspacePhase.DIRTY, WorkspacePhase.READY),
        (WorkspacePhase.DIRTY, WorkspacePhase.IN_USE),
        (WorkspacePhase.DIRTY, WorkspacePhase.RELEASING),
        (WorkspacePhase.DIRTY, WorkspacePhase.FAILED),
        (WorkspacePhase.RELEASING, WorkspacePhase.RELEASED),
        (WorkspacePhase.RELEASING, WorkspacePhase.FAILED),
        (WorkspacePhase.FAILED, WorkspacePhase.PREPARING),
        (WorkspacePhase.FAILED, WorkspacePhase.RELEASING),
    }
)


class WorkspaceNetworkPolicy(StrEnum):
    """Workspace network policy."""

    DENY = "deny"
    ALLOW = "allow"


class WorkspaceExecutionReference(MaestroModel):
    """Reference to the owning Execution."""

    kind: Literal["Execution"] = "Execution"
    id: UUID
    name: ResourceName | None = None


class WorkspaceProviderReference(MaestroModel):
    """Reference to the WorkspaceProvider implementation."""

    kind: Literal["WorkspaceProvider"] = "WorkspaceProvider"
    name: ResourceName


class WorkspacePolicy(MaestroModel):
    """Workspace safety policy."""

    network: WorkspaceNetworkPolicy = WorkspaceNetworkPolicy.DENY
    allow_secrets: bool = Field(default=False, alias="allowSecrets")
    max_disk_bytes: int = Field(
        default=10 * 1024 * 1024 * 1024,
        ge=1,
        alias="maxDiskBytes",
    )
    command_timeout_seconds: int = Field(
        default=300,
        ge=1,
        alias="commandTimeoutSeconds",
    )


class WorkspaceSpec(Spec):
    """Desired Workspace configuration."""

    execution_ref: WorkspaceExecutionReference = Field(alias="executionRef")
    repository_ref: ResourceName = Field(alias="repositoryRef")
    provider_ref: WorkspaceProviderReference = Field(alias="providerRef")
    base_revision: Revision = Field(alias="baseRevision")
    branch_name: BranchName = Field(alias="branchName")
    requested_path: Path | None = Field(default=None, alias="requestedPath")
    policy: WorkspacePolicy = Field(default_factory=WorkspacePolicy)

    @field_validator("branch_name")
    @classmethod
    def validate_branch_name(cls, value: BranchName) -> BranchName:
        """Reject branch names that can escape refs or paths."""

        if (
            value.startswith("/")
            or value.endswith("/")
            or ".." in value
            or any(character.isspace() for character in value)
        ):
            raise ValueError("branchName must be a safe Git branch name")
        return value


class WorkspaceStatus(Status):
    """Observed Workspace state."""

    phase: WorkspacePhase = WorkspacePhase.PENDING
    path: Path | None = None
    observed_revision: str | None = Field(default=None, alias="observedRevision")
    dirty: bool = False
    lock_holder: str | None = Field(default=None, min_length=1, alias="lockHolder")
    failure_message: str = Field(default="", alias="failureMessage")

    @field_validator("path")
    @classmethod
    def require_absolute_path(cls, value: Path | None) -> Path | None:
        """Persist only absolute Workspace paths."""

        if value is not None and not value.is_absolute():
            raise ValueError("status.path must be absolute")
        return value


class Workspace(BaseResource[WorkspaceSpec, WorkspaceStatus]):
    """Isolated execution environment."""

    kind: Literal["Workspace"] = "Workspace"

    @model_validator(mode="after")
    def validate_workspace_metadata(self) -> Self:
        """Require matching Execution ownership and cleanup finalizer."""

        execution_owners = tuple(
            owner
            for owner in self.metadata.owner_references
            if owner.kind == "Execution" and owner.controller
        )
        if len(execution_owners) != 1:
            raise ValueError(
                "Workspace must have exactly one Execution controller owner"
            )

        execution_owner = execution_owners[0]
        if execution_owner.id != self.spec.execution_ref.id:
            raise ValueError("Workspace Execution owner must match spec.executionRef")

        if WORKSPACE_CLEANUP_FINALIZER not in self.metadata.finalizers:
            raise ValueError("Workspace requires cleanup finalizer")

        return self

    @classmethod
    def new(
        cls,
        *,
        name: ResourceName,
        spec: WorkspaceSpec,
        created_by: str = "local-user",
        namespace: ResourceName = "default",
    ) -> Self:
        """Create a new Workspace resource."""

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
                        controller=True,
                        blockOwnerDeletion=True,
                    ),
                ),
                finalizers=(WORKSPACE_CLEANUP_FINALIZER,),
            ),
            spec=spec,
            status=WorkspaceStatus(),
        )


class WorkspaceHandle(MaestroModel):
    """Prepared Workspace handle."""

    path: Path
    observed_revision: Revision = Field(alias="observedRevision")

    @field_validator("path")
    @classmethod
    def require_absolute_path(cls, value: Path) -> Path:
        """Require absolute Workspace handle paths."""

        if not value.is_absolute():
            raise ValueError("WorkspaceHandle.path must be absolute")
        return value


class WorkspacePrepareRequest(MaestroModel):
    """Request to prepare a Workspace."""

    workspace: Workspace
    source_repository_path: Path = Field(alias="sourceRepositoryPath")
    workspace_root: Path = Field(alias="workspaceRoot")


class WorkspaceState(MaestroModel):
    """Collected Workspace state."""

    observed_revision: Revision = Field(alias="observedRevision")
    dirty: bool


class WorkspaceDiff(MaestroModel):
    """Collected Workspace diff."""

    text: str = ""


class WorkspaceCommandRequest(MaestroModel):
    """Local command execution request within a Workspace."""

    command: tuple[CommandArgument, ...] = Field(min_length=1)
    cwd: Path | None = None
    timeout_seconds: int = Field(default=300, ge=1, alias="timeoutSeconds")


class WorkspaceCommandResult(MaestroModel):
    """Local command execution result."""

    exit_code: int = Field(alias="exitCode")
    stdout: str = ""
    stderr: str = ""


class WorkspaceProviderError(Exception):
    """Raised by Workspace providers with user-safe diagnostics."""


class WorkspaceProvider(Protocol):
    """Workspace provider contract."""

    async def prepare(self, request: WorkspacePrepareRequest) -> WorkspaceHandle:
        """Prepare an isolated Workspace."""

    async def cleanup(self, handle: WorkspaceHandle) -> None:
        """Clean up an isolated Workspace."""

    async def collect_state(self, handle: WorkspaceHandle) -> WorkspaceState:
        """Collect status information for a Workspace."""

    async def collect_diff(self, handle: WorkspaceHandle) -> WorkspaceDiff:
        """Collect a Workspace diff artifact."""

    async def run_command(
        self,
        handle: WorkspaceHandle,
        request: WorkspaceCommandRequest,
    ) -> WorkspaceCommandResult:
        """Run a local command within a Workspace boundary."""


class WorkspaceRepository(
    ResourceRepository[Workspace, WorkspaceSpec, WorkspaceStatus],
    Protocol,
):
    """Persistence contract for Workspace resources."""

    async def list_by_execution(self, execution_id: UUID) -> tuple[Workspace, ...]:
        """List Workspaces belonging to one Execution."""


def validate_workspace_transition(
    resource_id: UUID,
    current_phase: WorkspacePhase,
    next_phase: WorkspacePhase,
) -> None:
    """Reject illegal Workspace phase transitions."""

    if current_phase == next_phase:
        return

    if (current_phase, next_phase) not in VALID_WORKSPACE_TRANSITIONS:
        raise ResourceTransitionError(resource_id, current_phase, next_phase)


def resolve_workspace_child(root: Path, requested_path: Path | str) -> Path:
    """Resolve a child path and reject path traversal or symlink escapes."""

    root_resolved = root.resolve(strict=True)
    requested = Path(requested_path)
    candidate = requested if requested.is_absolute() else root_resolved / requested
    resolved = candidate.resolve(strict=False)
    try:
        resolved.relative_to(root_resolved)
    except ValueError as error:
        raise ValueError("Workspace path escapes boundary") from error
    return resolved


def acquire_workspace_lock(
    workspace: Workspace,
    holder: str,
    *,
    expected_resource_version: int,
) -> Workspace:
    """Return a Workspace with a lock held by `holder`."""

    if workspace.status.lock_holder not in {None, holder}:
        raise ResourceTransitionError(
            workspace.metadata.id,
            workspace.status.phase,
            "LockHeld",
        )
    status = workspace.status.model_copy(
        update={
            "phase": WorkspacePhase.IN_USE,
            "lock_holder": holder,
        }
    )
    return apply_workspace_status_update(
        workspace,
        status,
        expected_resource_version=expected_resource_version,
    )


def release_workspace_lock(
    workspace: Workspace,
    holder: str,
    *,
    expected_resource_version: int,
) -> Workspace:
    """Return a Workspace after releasing a held lock."""

    if workspace.status.lock_holder != holder:
        raise ResourceTransitionError(
            workspace.metadata.id,
            workspace.status.phase,
            "LockRelease",
        )
    status = workspace.status.model_copy(
        update={
            "phase": (
                WorkspacePhase.READY
                if not workspace.status.dirty
                else WorkspacePhase.DIRTY
            ),
            "lock_holder": None,
        }
    )
    return apply_workspace_status_update(
        workspace,
        status,
        expected_resource_version=expected_resource_version,
    )


def apply_workspace_spec_update(
    workspace: Workspace,
    spec: WorkspaceSpec,
    *,
    expected_resource_version: int,
) -> Workspace:
    """Apply limited Workspace spec updates."""

    immutable_fields = ("execution_ref", "repository_ref", "provider_ref")
    for field_name in immutable_fields:
        if getattr(spec, field_name) != getattr(workspace.spec, field_name):
            raise ResourceImmutableFieldError(
                workspace.metadata.id,
                f"spec.{field_name}",
            )

    if workspace.status.phase not in {WorkspacePhase.PENDING, WorkspacePhase.FAILED}:
        if spec != workspace.spec:
            raise ResourceImmutableFieldError(workspace.metadata.id, "spec")

    return apply_spec_update(
        workspace,
        spec,
        expected_resource_version=expected_resource_version,
    )


def apply_workspace_status_update(
    workspace: Workspace,
    status: WorkspaceStatus,
    *,
    expected_resource_version: int,
) -> Workspace:
    """Apply Workspace status updates with phase transition validation."""

    validate_workspace_transition(
        workspace.metadata.id,
        workspace.status.phase,
        status.phase,
    )
    return apply_status_update(
        workspace,
        status,
        expected_resource_version=expected_resource_version,
    )
