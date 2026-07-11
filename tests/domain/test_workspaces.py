"""Tests for Workspace resources and safety rules."""

from pathlib import Path
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from maestro.domain.exceptions import (
    ResourceImmutableFieldError,
    ResourceTransitionError,
)
from maestro.domain.resources import Metadata, OwnerReference
from maestro.domain.workspaces import (
    WORKSPACE_CLEANUP_FINALIZER,
    Workspace,
    WorkspaceExecutionReference,
    WorkspacePhase,
    WorkspaceProviderReference,
    WorkspaceSpec,
    WorkspaceStatus,
    acquire_workspace_lock,
    apply_workspace_spec_update,
    apply_workspace_status_update,
    release_workspace_lock,
    resolve_workspace_child,
)


def valid_workspace_spec(
    execution_id: UUID | None = None,
    *,
    branch_name: str = "maestro/execution-123",
    requested_path: Path | None = None,
) -> WorkspaceSpec:
    """Build a valid WorkspaceSpec for tests."""

    return WorkspaceSpec(
        executionRef=WorkspaceExecutionReference(
            id=execution_id or uuid4(),
            name="implement-health",
        ),
        repositoryRef="backend",
        providerRef=WorkspaceProviderReference(name="local-git-worktree"),
        baseRevision="main",
        branchName=branch_name,
        requestedPath=requested_path,
    )


def valid_workspace(
    execution_id: UUID | None = None,
    *,
    name: str = "execution-backend",
) -> Workspace:
    """Build a valid Workspace resource."""

    return Workspace.new(name=name, spec=valid_workspace_spec(execution_id))


def ready_workspace(workspace: Workspace) -> Workspace:
    """Move a Workspace through the prepare transition sequence."""

    preparing = apply_workspace_status_update(
        workspace,
        WorkspaceStatus(phase=WorkspacePhase.PREPARING),
        expected_resource_version=workspace.metadata.resource_version,
    )
    return apply_workspace_status_update(
        preparing,
        WorkspaceStatus(phase=WorkspacePhase.READY),
        expected_resource_version=preparing.metadata.resource_version,
    )


def test_workspace_serializes_and_deserializes() -> None:
    workspace = valid_workspace()

    payload = workspace.model_dump(mode="json", by_alias=True)
    round_tripped = Workspace.model_validate(payload)

    assert payload["kind"] == "Workspace"
    assert payload["metadata"]["finalizers"] == [WORKSPACE_CLEANUP_FINALIZER]
    assert payload["spec"]["providerRef"]["kind"] == "WorkspaceProvider"
    assert round_tripped == workspace


def test_workspace_requires_matching_execution_owner() -> None:
    spec = valid_workspace_spec()

    with pytest.raises(ValidationError):
        Workspace(
            metadata=Metadata(
                name="execution-backend",
                ownerReferences=(
                    OwnerReference(
                        kind="Execution",
                        id=uuid4(),
                        controller=True,
                    ),
                ),
                finalizers=(WORKSPACE_CLEANUP_FINALIZER,),
            ),
            spec=spec,
            status=WorkspaceStatus(),
        )


def test_workspace_requires_cleanup_finalizer() -> None:
    execution_id = uuid4()
    spec = valid_workspace_spec(execution_id)

    with pytest.raises(ValidationError):
        Workspace(
            metadata=Metadata(
                name="execution-backend",
                ownerReferences=(
                    OwnerReference(
                        kind="Execution",
                        id=execution_id,
                        controller=True,
                    ),
                ),
            ),
            spec=spec,
            status=WorkspaceStatus(),
        )


def test_branch_names_must_be_safe() -> None:
    for branch_name in ("/bad", "bad/", "bad branch", "bad..branch"):
        with pytest.raises(ValidationError):
            valid_workspace_spec(branch_name=branch_name)


def test_status_path_must_be_absolute() -> None:
    with pytest.raises(ValidationError):
        WorkspaceStatus(path=Path("relative/workspace"))


def test_path_traversal_is_rejected(tmp_path) -> None:
    root = tmp_path / "workspaces"
    root.mkdir()

    with pytest.raises(ValueError):
        resolve_workspace_child(root, "../source")


def test_symlink_escape_is_rejected(tmp_path) -> None:
    root = tmp_path / "workspaces"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (root / "link").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError):
        resolve_workspace_child(root, "link/secret.txt")


def test_workspace_locking_uses_phase_and_holder() -> None:
    workspace = ready_workspace(valid_workspace())

    locked = acquire_workspace_lock(
        workspace,
        "coding-agent",
        expected_resource_version=workspace.metadata.resource_version,
    )

    assert locked.status.phase == WorkspacePhase.IN_USE
    assert locked.status.lock_holder == "coding-agent"

    with pytest.raises(ResourceTransitionError):
        acquire_workspace_lock(
            locked,
            "other-agent",
            expected_resource_version=locked.metadata.resource_version,
        )

    released = release_workspace_lock(
        locked,
        "coding-agent",
        expected_resource_version=locked.metadata.resource_version,
    )

    assert released.status.phase == WorkspacePhase.READY
    assert released.status.lock_holder is None


def test_invalid_workspace_transition_is_rejected() -> None:
    workspace = valid_workspace()

    with pytest.raises(ResourceTransitionError):
        apply_workspace_status_update(
            workspace,
            WorkspaceStatus(phase=WorkspacePhase.RELEASED),
            expected_resource_version=workspace.metadata.resource_version,
        )


def test_workspace_spec_is_immutable_after_ready() -> None:
    workspace = ready_workspace(valid_workspace())
    changed_spec = workspace.spec.model_copy(update={"base_revision": "main~1"})

    with pytest.raises(ResourceImmutableFieldError):
        apply_workspace_spec_update(
            workspace,
            changed_spec,
            expected_resource_version=workspace.metadata.resource_version,
        )
