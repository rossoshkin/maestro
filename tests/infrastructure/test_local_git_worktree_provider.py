"""Tests for the local Git worktree Workspace provider."""

import asyncio
import shutil
import subprocess
from pathlib import Path
from uuid import uuid4

import pytest

from maestro.domain.workspaces import (
    Workspace,
    WorkspaceCommandRequest,
    WorkspaceExecutionReference,
    WorkspaceHandle,
    WorkspacePrepareRequest,
    WorkspaceProviderError,
    WorkspaceProviderReference,
    WorkspaceSpec,
)
from maestro.infrastructure.workspaces import LocalGitWorktreeProvider


def git_binary() -> str:
    """Return the git binary path or skip tests that need Git."""

    binary = shutil.which("git")
    if binary is None:
        pytest.skip("git is required for local worktree provider tests")
    return binary


def run_git(cwd: Path, *args: str) -> str:
    """Run a Git command and return stripped stdout."""

    completed = subprocess.run(
        (git_binary(), "-C", str(cwd), *args),
        capture_output=True,
        check=False,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
    return completed.stdout.strip()


def create_source_repository(tmp_path: Path) -> Path:
    """Create a small local Git repository for provider tests."""

    source = tmp_path / "source"
    source.mkdir()
    run_git(source, "init")
    run_git(source, "checkout", "-b", "main")
    run_git(source, "config", "user.name", "Maestro Tests")
    run_git(source, "config", "user.email", "maestro@example.test")
    (source / "README.md").write_text("source\n")
    run_git(source, "add", "README.md")
    run_git(source, "commit", "-m", "initial")
    return source


def valid_workspace(
    *,
    requested_path: Path | None = None,
    name: str = "execution-backend",
) -> Workspace:
    """Build a valid Workspace resource for provider tests."""

    execution_id = uuid4()
    return Workspace.new(
        name=name,
        spec=WorkspaceSpec(
            executionRef=WorkspaceExecutionReference(
                id=execution_id,
                name="implement-health",
            ),
            repositoryRef="backend",
            providerRef=WorkspaceProviderReference(name="local-git-worktree"),
            baseRevision="main",
            branchName="maestro/execution-123",
            requestedPath=requested_path,
        ),
    )


def prepare_request(
    workspace: Workspace,
    *,
    source: Path,
    workspace_root: Path,
) -> WorkspacePrepareRequest:
    """Build a WorkspacePrepareRequest."""

    return WorkspacePrepareRequest(
        workspace=workspace,
        sourceRepositoryPath=source,
        workspaceRoot=workspace_root,
    )


def test_prepare_creates_isolated_worktree_without_dirtying_source(tmp_path) -> None:
    async def scenario() -> None:
        source = create_source_repository(tmp_path)
        workspace_root = tmp_path / "workspaces"
        workspace = valid_workspace()
        provider = LocalGitWorktreeProvider(git_binary())

        handle = await provider.prepare(
            prepare_request(
                workspace,
                source=source,
                workspace_root=workspace_root,
            )
        )

        assert handle.path == workspace_root / "default" / "execution-backend"
        assert handle.path.exists()
        assert (source / "README.md").read_text() == "source\n"
        assert run_git(source, "status", "--short") == ""
        assert run_git(handle.path, "branch", "--show-current") == (
            workspace.spec.branch_name
        )

    asyncio.run(scenario())


def test_prepare_rejects_worktree_inside_source_checkout(tmp_path) -> None:
    async def scenario() -> None:
        source = create_source_repository(tmp_path)
        provider = LocalGitWorktreeProvider(git_binary())
        workspace = valid_workspace(requested_path=source / "nested-workspace")

        with pytest.raises(WorkspaceProviderError):
            await provider.prepare(
                prepare_request(
                    workspace,
                    source=source,
                    workspace_root=tmp_path,
                )
            )

    asyncio.run(scenario())


def test_prepare_rejects_path_traversal(tmp_path) -> None:
    async def scenario() -> None:
        source = create_source_repository(tmp_path)
        provider = LocalGitWorktreeProvider(git_binary())
        workspace = valid_workspace(requested_path=Path("../escape"))

        with pytest.raises(WorkspaceProviderError):
            await provider.prepare(
                prepare_request(
                    workspace,
                    source=source,
                    workspace_root=tmp_path / "workspaces",
                )
            )

    asyncio.run(scenario())


def test_cleanup_removes_worktree_and_preserves_source_repository(tmp_path) -> None:
    async def scenario() -> None:
        source = create_source_repository(tmp_path)
        workspace = valid_workspace()
        provider = LocalGitWorktreeProvider(git_binary())
        handle = await provider.prepare(
            prepare_request(
                workspace,
                source=source,
                workspace_root=tmp_path / "workspaces",
            )
        )

        await provider.cleanup(handle)

        assert not handle.path.exists()
        assert (source / "README.md").read_text() == "source\n"
        assert (source / ".git").is_dir()

    asyncio.run(scenario())


def test_cleanup_refuses_to_delete_source_checkout(tmp_path) -> None:
    async def scenario() -> None:
        source = create_source_repository(tmp_path)
        revision = run_git(source, "rev-parse", "HEAD")
        provider = LocalGitWorktreeProvider(git_binary())

        with pytest.raises(WorkspaceProviderError):
            await provider.cleanup(
                WorkspaceHandle(path=source.resolve(), observedRevision=revision)
            )

        assert source.exists()
        assert (source / "README.md").read_text() == "source\n"

    asyncio.run(scenario())


def test_provider_collects_status_diff_and_runs_commands(tmp_path) -> None:
    async def scenario() -> None:
        source = create_source_repository(tmp_path)
        workspace = valid_workspace()
        provider = LocalGitWorktreeProvider(git_binary())
        handle = await provider.prepare(
            prepare_request(
                workspace,
                source=source,
                workspace_root=tmp_path / "workspaces",
            )
        )
        (handle.path / "README.md").write_text("changed\n")

        state = await provider.collect_state(handle)
        diff = await provider.collect_diff(handle)
        result = await provider.run_command(
            handle,
            WorkspaceCommandRequest(command=("git", "status", "--short")),
        )

        assert state.dirty is True
        assert "-source" in diff.text
        assert "+changed" in diff.text
        assert result.exit_code == 0
        assert "README.md" in result.stdout

    asyncio.run(scenario())


def test_command_cwd_cannot_escape_workspace(tmp_path) -> None:
    async def scenario() -> None:
        source = create_source_repository(tmp_path)
        workspace = valid_workspace()
        provider = LocalGitWorktreeProvider(git_binary())
        handle = await provider.prepare(
            prepare_request(
                workspace,
                source=source,
                workspace_root=tmp_path / "workspaces",
            )
        )

        with pytest.raises(WorkspaceProviderError):
            await provider.run_command(
                handle,
                WorkspaceCommandRequest(
                    command=("git", "status", "--short"),
                    cwd=tmp_path,
                ),
            )

    asyncio.run(scenario())
