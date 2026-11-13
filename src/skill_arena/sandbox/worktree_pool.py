"""Isolated git worktree / seeded repository sandbox pool with anti-cheat SHA-256 snapshotting."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import shutil
import signal
import subprocess
import tempfile

from skill_arena.models import TaskSpec


def _sha256_file(path: Path) -> str | None:
    if not path.is_file():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass(slots=True)
class SandboxHandle:
    """Active isolated workspace for one trial execution."""

    root: Path
    source_repo: Path | None
    is_git_worktree: bool
    protected_hashes: dict[str, str | None]
    protected_backups: dict[str, bytes | None]

    def verify_and_restore_protected_tests(self) -> tuple[bool, list[str]]:
        """Check whether the agent modified or deleted any protected test files, then restore them."""
        tampered: list[str] = []
        for rel_path, expected_hash in self.protected_hashes.items():
            full_path = self.root / rel_path
            actual_hash = _sha256_file(full_path)
            if actual_hash != expected_hash:
                tampered.append(rel_path)
                backup = self.protected_backups.get(rel_path)
                if backup is not None:
                    full_path.parent.mkdir(parents=True, exist_ok=True)
                    full_path.write_bytes(backup)
                elif full_path.exists():
                    full_path.unlink()
        return (len(tampered) == 0, tampered)

    def capture_git_diff(self) -> tuple[str, int, int]:
        """Return unified git diff against baseline HEAD plus added/removed line counts."""
        # Stage untracked implementation files without committing so git diff HEAD sees new files too
        subprocess.run(
            ["git", "add", "-N", "."],
            cwd=self.root,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        proc = subprocess.run(
            [
                "git",
                "diff",
                "HEAD",
                "--",
                ".",
                ":(exclude).agents",
                ":(exclude).claude",
                ":(exclude).skill-arena-env",
                ":(exclude)AGENTS.md",
                ":(exclude)CLAUDE.md",
            ],
            cwd=self.root,
            capture_output=True,
            text=True,
            check=False,
        )
        diff_text = proc.stdout
        added = 0
        removed = 0
        for line in diff_text.splitlines():
            if line.startswith("+++") or line.startswith("---"):
                continue
            if line.startswith("+"):
                added += 1
            elif line.startswith("-"):
                removed += 1
        return diff_text, added, removed


class WorktreeSandboxPool:
    """Manages isolated git workspaces and process-group timeout termination."""

    def __init__(self, base_dir: Path | None = None) -> None:
        self._owns_base = base_dir is None
        self.base_dir = Path(base_dir) if base_dir else Path(tempfile.mkdtemp(prefix="skill-arena-pool-"))
        self.base_dir.mkdir(parents=True, exist_ok=True)

    def acquire(self, task: TaskSpec, trial_slug: str) -> SandboxHandle:
        sandbox_root = self.base_dir / trial_slug
        if sandbox_root.exists():
            shutil.rmtree(sandbox_root, ignore_errors=True)

        source_repo: Path | None = None
        is_worktree = False

        if task.repo_path and task.base_commit:
            source_repo = Path(task.repo_path).resolve()
            subprocess.run(
                ["git", "worktree", "add", "--detach", str(sandbox_root), task.base_commit],
                cwd=source_repo,
                capture_output=True,
                text=True,
                check=True,
            )
            is_worktree = True
            if task.test_patch:
                subprocess.run(
                    ["git", "apply", "--whitespace=nowarn", "-"],
                    cwd=sandbox_root,
                    input=task.test_patch,
                    text=True,
                    capture_output=True,
                    check=True,
                )
                self._git_commit_baseline(sandbox_root, "chore(arena): mount extracted test patch")
        else:
            sandbox_root.mkdir(parents=True, exist_ok=True)
            subprocess.run(
                ["git", "init", "-b", "main"],
                cwd=sandbox_root,
                capture_output=True,
                text=True,
                check=True,
            )
            for rel_path, content in task.seed_files.items():
                dest = sandbox_root / rel_path
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_text(content, encoding="utf-8")
            self._git_commit_baseline(sandbox_root, f"chore(arena): seed baseline for {task.task_id}")

        protected_hashes: dict[str, str | None] = {}
        protected_backups: dict[str, bytes | None] = {}
        for rel_path in task.protected_test_paths:
            target = sandbox_root / rel_path
            protected_hashes[rel_path] = _sha256_file(target)
            protected_backups[rel_path] = target.read_bytes() if target.is_file() else None

        return SandboxHandle(
            root=sandbox_root,
            source_repo=source_repo,
            is_git_worktree=is_worktree,
            protected_hashes=protected_hashes,
            protected_backups=protected_backups,
        )

    def release(self, handle: SandboxHandle) -> None:
        if handle.is_git_worktree and handle.source_repo is not None:
            subprocess.run(
                ["git", "worktree", "remove", "--force", str(handle.root)],
                cwd=handle.source_repo,
                capture_output=True,
                text=True,
                check=False,
            )
        if handle.root.exists():
            shutil.rmtree(handle.root, ignore_errors=True)

    def close(self) -> None:
        if self._owns_base and self.base_dir.exists():
            shutil.rmtree(self.base_dir, ignore_errors=True)

    @staticmethod
    def run_bounded_command(
        argv: list[str],
        cwd: Path,
        env: dict[str, str] | None = None,
        timeout_sec: int = 90,
        stdin_text: str | None = None,
    ) -> tuple[int, str, str, bool]:
        """Execute a command in its own POSIX process group and kill the entire group on timeout."""
        merged_env = os.environ.copy()
        if env:
            merged_env.update(env)

        proc = subprocess.Popen(
            argv,
            cwd=cwd,
            env=merged_env,
            stdin=subprocess.PIPE if stdin_text is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        timed_out = False
        try:
            stdout, stderr = proc.communicate(input=stdin_text, timeout=timeout_sec)
        except subprocess.TimeoutExpired:
            timed_out = True
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            stdout, stderr = proc.communicate()
        return (proc.returncode if not timed_out else 124, stdout, stderr, timed_out)

    @staticmethod
    def _git_commit_baseline(repo_dir: Path, message: str) -> None:
        git_env = os.environ.copy()
        git_env.update(
            {
                "GIT_AUTHOR_NAME": "SkillArena Sandbox",
                "GIT_AUTHOR_EMAIL": "sandbox@skill-arena.local",
                "GIT_COMMITTER_NAME": "SkillArena Sandbox",
                "GIT_COMMITTER_EMAIL": "sandbox@skill-arena.local",
            }
        )
        subprocess.run(["git", "add", "-A"], cwd=repo_dir, env=git_env, capture_output=True, check=True)
        subprocess.run(
            ["git", "commit", "--allow-empty", "-m", message],
            cwd=repo_dir,
            env=git_env,
            capture_output=True,
            check=True,
        )
