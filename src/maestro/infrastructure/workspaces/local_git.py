"""Local Git worktree Workspace provider."""

from __future__ import annotations

import subprocess
from pathlib import Path

from maestro.domain.workspaces import (
    WorkspaceCommandRequest,
    WorkspaceCommandResult,
    WorkspaceDiff,
    WorkspaceHandle,
    WorkspacePrepareRequest,
    WorkspaceProviderError,
    WorkspaceState,
    resolve_workspace_child,
)

GIT_TIMEOUT_SECONDS = 30


class LocalGitWorktreeProvider:
    """Prepare, inspect, execute commands in, and clean up local Git worktrees."""

    def __init__(self, git_binary: str = "git") -> None:
        self._git_binary = git_binary

    async def prepare(self, request: WorkspacePrepareRequest) -> WorkspaceHandle:
        """Prepare an isolated Git worktree for a Workspace."""

        source_path = request.source_repository_path.resolve(strict=True)
        source_root = self._source_repository_root(source_path)
        workspace_root = self._ensure_workspace_root(request.workspace_root)
        target = self._target_path(request, workspace_root)
        self._ensure_not_source_checkout(source_root, target)
        target.parent.mkdir(parents=True, exist_ok=True)

        self._git(
            source_root,
            "worktree",
            "add",
            "-B",
            request.workspace.spec.branch_name,
            str(target),
            request.workspace.spec.base_revision,
        )
        revision = self._git(target, "rev-parse", "HEAD")
        return WorkspaceHandle(path=target, observedRevision=revision)

    async def cleanup(self, handle: WorkspaceHandle) -> None:
        """Remove a prepared Git worktree without deleting source repositories."""

        if not handle.path.exists():
            return

        if handle.path.is_symlink():
            raise WorkspaceProviderError("cleanup refused symlink workspace path")

        path = handle.path.resolve(strict=True)
        self._ensure_safe_worktree_for_cleanup(path)
        common_git_dir = self._git_common_dir(path)
        self._run_checked(
            (
                self._git_binary,
                "--git-dir",
                str(common_git_dir),
                "worktree",
                "remove",
                "--force",
                str(path),
            ),
            cwd=path.parent,
            timeout=GIT_TIMEOUT_SECONDS,
        )
        if path.exists():
            raise WorkspaceProviderError("Git worktree removal left workspace path")

    async def collect_state(self, handle: WorkspaceHandle) -> WorkspaceState:
        """Collect the current revision and dirty flag for a Workspace."""

        path = self._existing_workspace_path(handle)
        revision = self._git(path, "rev-parse", "HEAD")
        status = self._git(path, "status", "--porcelain")
        return WorkspaceState(observedRevision=revision, dirty=bool(status.strip()))

    async def collect_diff(self, handle: WorkspaceHandle) -> WorkspaceDiff:
        """Collect staged and unstaged Git diff text for a Workspace."""

        path = self._existing_workspace_path(handle)
        staged = self._git(path, "diff", "--cached", "--binary")
        unstaged = self._git(path, "diff", "--binary")
        parts = tuple(part for part in (staged, unstaged) if part)
        return WorkspaceDiff(text="\n".join(parts))

    async def run_command(
        self,
        handle: WorkspaceHandle,
        request: WorkspaceCommandRequest,
    ) -> WorkspaceCommandResult:
        """Run a command inside the Workspace boundary."""

        workspace_path = self._existing_workspace_path(handle)
        try:
            cwd = (
                workspace_path
                if request.cwd is None
                else resolve_workspace_child(workspace_path, request.cwd)
            )
        except ValueError as error:
            raise WorkspaceProviderError(str(error)) from error
        if not cwd.exists() or not cwd.is_dir():
            raise WorkspaceProviderError("command cwd must be an existing directory")

        try:
            completed = self._run_process(
                request.command,
                cwd=cwd,
                timeout=request.timeout_seconds,
            )
        except subprocess.TimeoutExpired as error:
            stderr = self._timeout_text(error.stderr)
            timeout_message = f"Command timed out after {request.timeout_seconds}s"
            return WorkspaceCommandResult(
                exitCode=124,
                stdout=self._timeout_text(error.stdout),
                stderr=f"{stderr}\n{timeout_message}".strip(),
            )

        return WorkspaceCommandResult(
            exitCode=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
        )

    def _source_repository_root(self, source_path: Path) -> Path:
        inside = self._git(source_path, "rev-parse", "--is-inside-work-tree")
        if inside != "true":
            raise WorkspaceProviderError("source path is not inside a Git worktree")
        return Path(self._git(source_path, "rev-parse", "--show-toplevel")).resolve(
            strict=True
        )

    @staticmethod
    def _ensure_workspace_root(workspace_root: Path) -> Path:
        workspace_root.mkdir(parents=True, exist_ok=True)
        return workspace_root.resolve(strict=True)

    @staticmethod
    def _target_path(
        request: WorkspacePrepareRequest,
        workspace_root: Path,
    ) -> Path:
        requested_path = request.workspace.spec.requested_path
        if requested_path is None:
            requested_path = Path(
                request.workspace.metadata.namespace,
                request.workspace.metadata.name,
            )

        try:
            target = resolve_workspace_child(workspace_root, requested_path)
        except ValueError as error:
            raise WorkspaceProviderError(str(error)) from error
        if target.exists() and target.is_symlink():
            raise WorkspaceProviderError("workspace target must not be a symlink")
        return target

    @staticmethod
    def _ensure_not_source_checkout(source_root: Path, target: Path) -> None:
        source = source_root.resolve(strict=True)
        resolved_target = target.resolve(strict=False)
        if (
            resolved_target == source
            or _is_relative_to(resolved_target, source)
            or _is_relative_to(source, resolved_target)
        ):
            raise WorkspaceProviderError(
                "workspace path must be outside the source checkout"
            )

    @staticmethod
    def _existing_workspace_path(handle: WorkspaceHandle) -> Path:
        if handle.path.is_symlink():
            raise WorkspaceProviderError("workspace path must not be a symlink")
        path = handle.path.resolve(strict=True)
        if not path.is_dir():
            raise WorkspaceProviderError("workspace path must be a directory")
        return path

    def _ensure_safe_worktree_for_cleanup(self, path: Path) -> None:
        git_marker = path / ".git"
        if git_marker.is_dir():
            raise WorkspaceProviderError(
                "cleanup refused repository with .git directory"
            )
        if not git_marker.is_file() or git_marker.is_symlink():
            raise WorkspaceProviderError("cleanup refused non-worktree directory")

        top_level = Path(self._git(path, "rev-parse", "--show-toplevel")).resolve(
            strict=True
        )
        if top_level != path:
            raise WorkspaceProviderError("cleanup handle must point at worktree root")

    def _git_common_dir(self, path: Path) -> Path:
        common_git_dir = Path(self._git(path, "rev-parse", "--git-common-dir"))
        if common_git_dir.is_absolute():
            return common_git_dir.resolve(strict=True)
        return (path / common_git_dir).resolve(strict=True)

    def _git(self, cwd: Path, *args: str) -> str:
        completed = self._run_checked(
            (self._git_binary, "-C", str(cwd), *args),
            cwd=cwd,
            timeout=GIT_TIMEOUT_SECONDS,
        )
        return completed.stdout.strip()

    def _run_checked(
        self,
        command: tuple[str, ...],
        *,
        cwd: Path,
        timeout: int,
    ) -> subprocess.CompletedProcess[str]:
        try:
            completed = self._run_process(command, cwd=cwd, timeout=timeout)
        except subprocess.TimeoutExpired as error:
            raise WorkspaceProviderError("Git command timed out") from error
        if completed.returncode != 0:
            detail = completed.stderr.strip() or completed.stdout.strip()
            raise WorkspaceProviderError(detail or "Git command failed")
        return completed

    @staticmethod
    def _run_process(
        command: tuple[str, ...],
        *,
        cwd: Path,
        timeout: int,
    ) -> subprocess.CompletedProcess[str]:
        try:
            return subprocess.run(
                command,
                cwd=cwd,
                capture_output=True,
                check=False,
                text=True,
                timeout=timeout,
            )
        except OSError as error:
            raise WorkspaceProviderError(str(error)) from error

    @staticmethod
    def _timeout_text(value: bytes | str | None) -> str:
        if value is None:
            return ""
        if isinstance(value, bytes):
            return value.decode(errors="replace")
        return value


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True
