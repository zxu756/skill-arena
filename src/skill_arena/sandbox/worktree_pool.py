"""Isolated git worktree / seeded repository sandbox pool with anti-cheat SHA-256 snapshotting."""

from __future__ import annotations

from dataclasses import dataclass, field
import errno
import fcntl
import hashlib
import os
from pathlib import Path
import re
import selectors
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from typing import Any
import uuid

from skill_arena.models import TaskSpec

_STDLIB_AND_RUNNER_MODULES: frozenset[str] = frozenset(sys.stdlib_module_names) | {
    "pytest",
    "_pytest",
    "pluggy",
    "py",
    "sitecustomize",
    "usercustomize",
}
_HARNESS_IGNORE_TOP_DIRS: frozenset[str] = frozenset(
    {
        ".git",
        ".agents",
        ".claude",
        ".codex",
        ".vibe",
        ".opencode",
        ".relay",
        ".skill-arena-env",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
    }
)
_HARNESS_IGNORE_FILES: frozenset[str] = frozenset({"AGENTS.md", "CLAUDE.md", ".DS_Store"})
_FORBIDDEN_CONFIG_FILENAMES: frozenset[str] = frozenset(
    {
        "__init__.py",
        "conftest.py",
        "pytest.ini",
        ".pytest.ini",
        "pyproject.toml",
        "setup.cfg",
        "setup.py",
        "tox.ini",
        "sitecustomize.py",
        "usercustomize.py",
    }
)
_TEST_FILE_RE = re.compile(
    r"(^|/)(tests?/|test_[^/]+\.py$|.*_test\.(py|go|ts|tsx|js|rs)$|.*\.(spec|test)\.(ts|tsx|js|jsx)$|.*Tests\.swift$)"
)


def is_forbidden_agent_path(rel_path: str, protected_paths: set[str]) -> bool:
    """Return True if an agent is forbidden from adding, editing, or deleting `rel_path`."""
    norm = rel_path.strip("/").replace("\\", "/")
    if not norm:
        return True
    if norm in protected_paths:
        return True
    if _TEST_FILE_RE.search(norm):
        return True
    parts = norm.split("/")
    if any(p in {"test", "tests"} for p in parts[:-1]):
        return True
    filename = parts[-1]
    if filename in _FORBIDDEN_CONFIG_FILENAMES or filename.endswith(".pth"):
        return True
    top_seg = parts[0]
    if len(parts) > 1 and top_seg in _STDLIB_AND_RUNNER_MODULES:
        return True
    stem, ext = os.path.splitext(filename)
    if ext in {".py", ".pyw", ".pyc", ".pyo", ".so", ".dylib", ".pyd"} and stem in _STDLIB_AND_RUNNER_MODULES:
        return True
    return False


# A plain test module at the repository root or anywhere under tests/ (agents doing TDD put
# their own tests in either place). Such files never enter the clean grading checkout.
_NEW_AGENT_TEST_FILE_RE = re.compile(r"^(?:tests?/(?:[A-Za-z0-9_.-]+/)*)?test_[A-Za-z0-9_]+\.py$")


def is_permitted_new_agent_test_file(rel_path: str, protected_paths: set[str]) -> bool:
    """Return True for a NEW plain test module the agent wrote itself (e.g. `tests/test_extra.py`).

    Skills such as test-driven development make an agent write its own tests. Adding such a file is
    not tampering: it is still treated as a forbidden path for the clean grading checkout (never copied
    there), so it cannot influence the verdict. Only brand-new `test_*.py` modules under `tests/` qualify;
    editing or deleting any existing file, protected tests, `conftest.py`, `__init__.py`, helper modules
    and stdlib shadows stay violations.
    """
    norm = rel_path.strip("/").replace("\\", "/")
    if not norm or norm in protected_paths:
        return False
    if not _NEW_AGENT_TEST_FILE_RE.match(norm):
        return False
    filename = norm.rsplit("/", 1)[-1]
    if filename in _FORBIDDEN_CONFIG_FILENAMES:
        return False
    stem = filename[:-3]
    return stem not in _STDLIB_AND_RUNNER_MODULES


def _snapshot_workspace_files(root: Path, baseline_include_harness: bool = False) -> tuple[dict[str, bytes], list[str]]:
    """Return (regular_files_bytes, symlink_rel_paths) inside `root` excluding harness/cache dirs."""
    files: dict[str, bytes] = {}
    symlinks: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        rel_dir = os.path.relpath(dirpath, root)
        if rel_dir == ".":
            dirnames[:] = [d for d in dirnames if d not in _HARNESS_IGNORE_TOP_DIRS]
        else:
            dirnames[:] = [d for d in dirnames if d not in {"__pycache__", ".pytest_cache", ".git"}]
        for d in list(dirnames):
            full_d = Path(dirpath) / d
            if full_d.is_symlink():
                symlinks.append(full_d.relative_to(root).as_posix())
                dirnames.remove(d)
        for fn in filenames:
            if fn.endswith((".pyc", ".pyo")) or fn == ".DS_Store":
                continue
            full_f = Path(dirpath) / fn
            rel_posix = full_f.relative_to(root).as_posix()
            if not baseline_include_harness and rel_posix in _HARNESS_IGNORE_FILES:
                continue
            if full_f.is_symlink():
                symlinks.append(rel_posix)
                continue
            if full_f.is_file():
                files[rel_posix] = full_f.read_bytes()
    return files, symlinks


def _pid_alive(pid: int) -> bool:
    if pid <= 1:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _kill_pid_and_group(pid: int, sig: int = signal.SIGKILL) -> None:
    if pid <= 1 or pid == os.getpid():
        return
    try:
        pgid = os.getpgid(pid)
        if pgid > 1 and pgid != os.getpgrp():
            os.killpg(pgid, sig)
    except OSError:
        pass
    try:
        os.kill(pid, sig)
    except OSError:
        pass


def _find_pids_holding_file(file_path: Path) -> set[int]:
    lsof_bin = shutil.which("lsof") or "/usr/sbin/lsof"
    if not os.path.exists(lsof_bin):
        return set()
    proc = subprocess.run(
        [lsof_bin, "-t", "--", str(file_path)],
        capture_output=True,
        text=True,
        check=False,
    )
    return {int(tok) for tok in proc.stdout.split() if tok.isdigit()}


def _find_pids_with_cwd_under(target_dir: Path) -> set[int]:
    """Return current-UID PIDs whose cwd or open/mapped files reside inside `target_dir`."""
    lsof_bin = shutil.which("lsof") or "/usr/sbin/lsof"
    if not os.path.exists(lsof_bin):
        return set()
    real_target = os.path.realpath(target_dir)
    uid_str = str(os.getuid())
    proc = subprocess.run(
        [lsof_bin, "-F", "pn", "-a", "-u", uid_str, "-d", "cwd"],
        capture_output=True,
        text=True,
        check=False,
    )
    found: set[int] = set()
    cur_pid: int | None = None
    for line in proc.stdout.splitlines():
        if line.startswith("p") and line[1:].isdigit():
            cur_pid = int(line[1:])
        elif line.startswith("n") and cur_pid is not None:
            p = line[1:]
            if p == real_target or p.startswith(real_target + os.sep):
                found.add(cur_pid)
    if os.path.isdir(real_target):
        proc_open = subprocess.run(
            [lsof_bin, "-F", "p", "-a", "-u", uid_str, "+D", real_target],
            capture_output=True,
            text=True,
            check=False,
        )
        for line in proc_open.stdout.splitlines():
            if line.startswith("p") and line[1:].isdigit():
                found.add(int(line[1:]))
    return found


def _find_pids_with_env_token(token_value: str) -> set[int]:
    """Scan current user's processes (including detached setsid `??` daemons) for SKILL_ARENA_TRIAL_TOKEN."""
    if not token_value:
        return set()
    ps_bin = shutil.which("ps") or "/bin/ps"
    if not os.path.exists(ps_bin):
        return set()
    clean_env = {k: v for k, v in os.environ.items() if k != "SKILL_ARENA_TRIAL_TOKEN"}
    needle = f"SKILL_ARENA_TRIAL_TOKEN={token_value}"
    proc = subprocess.run(
        [ps_bin, "xewwo", "pid=,command="],
        env=clean_env,
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        proc = subprocess.run(
            [ps_bin, "-u", str(os.getuid()), "-o", "pid=,command=", "-e"],
            env=clean_env,
            capture_output=True,
            text=True,
            check=False,
        )
    found: set[int] = set()
    for line in proc.stdout.splitlines():
        if needle not in line:
            continue
        stripped = line.lstrip()
        parts = stripped.split(None, 1)
        if parts and parts[0].isdigit():
            pid = int(parts[0])
            if pid != os.getpid():
                found.add(pid)
    return found


def _describe_pid_command(pid: int) -> str:
    """Return clean argv string (without environment variables) for `pid`, or empty string if exited."""
    ps_bin = shutil.which("ps") or "/bin/ps"
    if not os.path.exists(ps_bin):
        return ""
    proc = subprocess.run(
        [ps_bin, "-p", str(pid), "-o", "args="],
        capture_output=True,
        text=True,
        check=False,
    )
    cmd = proc.stdout.strip()
    return cmd[:240] if cmd else ""


@dataclass(slots=True)
class BoundedCommandOutcome:
    """Process execution result including timeout and orphan-descendant reaping telemetry."""

    returncode: int
    stdout: str
    stderr: str
    timed_out: bool
    reaped_descendant_pids: list[int] = field(default_factory=list)
    unreaped_descendant_pids: list[int] = field(default_factory=list)
    leaked_processes: list[dict[str, Any]] = field(default_factory=list)


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
    baseline_files: dict[str, bytes] = field(default_factory=dict)
    # Hidden grading tests: never in the agent's workspace, overlaid onto the clean grading checkout.
    hidden_test_files: dict[str, bytes] = field(default_factory=dict)

    def verify_and_restore_protected_tests(self) -> tuple[bool, list[str]]:
        """Check protected tests and forbidden paths (tests/, __init__.py, stdlib shadows), restoring workspace."""
        tampered: list[str] = []
        seen: set[str] = set()
        protected_set = set(self.protected_hashes.keys())

        def _mark(rel_path: str) -> None:
            if rel_path not in seen:
                seen.add(rel_path)
                tampered.append(rel_path)

        # 1. Explicit protected test SHA-256 snapshot check & byte-exact restoration
        for rel_path, expected_hash in self.protected_hashes.items():
            full_path = self.root / rel_path
            actual_hash = _sha256_file(full_path)
            if actual_hash != expected_hash or full_path.is_symlink():
                _mark(rel_path)
                if full_path.is_symlink():
                    full_path.unlink(missing_ok=True)
                backup = self.protected_backups.get(rel_path)
                if backup is not None:
                    full_path.parent.mkdir(parents=True, exist_ok=True)
                    full_path.write_bytes(backup)
                elif full_path.exists():
                    full_path.unlink()

        # 2. Scan workspace against baseline_files for forbidden agent additions/edits/deletions & symlinks
        current_files, symlinks = _snapshot_workspace_files(self.root)
        for sym_rel in sorted(symlinks):
            _mark(sym_rel)
            sym_full = self.root / sym_rel
            sym_full.unlink(missing_ok=True)
            if sym_rel in self.baseline_files:
                sym_full.parent.mkdir(parents=True, exist_ok=True)
                sym_full.write_bytes(self.baseline_files[sym_rel])

        for rel_path in sorted(set(self.baseline_files.keys()) | set(current_files.keys())):
            if rel_path in {"AGENTS.md", "CLAUDE.md"} and rel_path not in self.baseline_files:
                continue
            base_bytes = self.baseline_files.get(rel_path)
            cur_bytes = current_files.get(rel_path)
            if base_bytes == cur_bytes:
                continue
            if base_bytes is None and is_permitted_new_agent_test_file(rel_path, protected_set):
                # New agent-written test module: allowed, but never copied into the grading checkout.
                continue
            if base_bytes is None and rel_path in self.hidden_test_files:
                # The agent wrote a file where a hidden test will go: the grader overwrites it, so it is not tampering.
                continue
            if is_forbidden_agent_path(rel_path, protected_set):
                _mark(rel_path)
                target = self.root / rel_path
                if base_bytes is not None:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(base_bytes)
                elif target.exists():
                    target.unlink()

        return (len(tampered) == 0, tampered)

    def materialize_clean_grading_checkout(self, dest_dir: Path) -> None:
        """Populate `dest_dir` from pristine baseline files plus ONLY permitted non-test agent edits."""
        if dest_dir.exists():
            shutil.rmtree(dest_dir, ignore_errors=True)
        dest_dir.mkdir(parents=True, exist_ok=True)

        # Write pristine baseline files first
        for rel_path, raw_bytes in self.baseline_files.items():
            out_file = dest_dir / rel_path
            out_file.parent.mkdir(parents=True, exist_ok=True)
            out_file.write_bytes(raw_bytes)

        protected_set = set(self.protected_hashes.keys())
        current_files, _ = _snapshot_workspace_files(self.root)

        # Apply ONLY permitted non-test, non-stdlib, non-__init__.py agent changes
        for rel_path, cur_bytes in current_files.items():
            if is_forbidden_agent_path(rel_path, protected_set):
                continue
            if self.baseline_files.get(rel_path) != cur_bytes:
                out_file = dest_dir / rel_path
                out_file.parent.mkdir(parents=True, exist_ok=True)
                out_file.write_bytes(cur_bytes)

        for rel_path in self.baseline_files:
            if rel_path in {"AGENTS.md", "CLAUDE.md"}:
                continue
            if rel_path not in current_files and not is_forbidden_agent_path(rel_path, protected_set):
                victim = dest_dir / rel_path
                if victim.is_file():
                    victim.unlink()

        # Hidden tests go in last so nothing the agent wrote at those paths survives.
        for rel_path, raw_bytes in self.hidden_test_files.items():
            out_file = dest_dir / rel_path
            if out_file.is_symlink():
                out_file.unlink()
            out_file.parent.mkdir(parents=True, exist_ok=True)
            out_file.write_bytes(raw_bytes)

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
                ":(exclude).codex",
                ":(exclude).vibe",
                ":(exclude).opencode",
                ":(exclude).relay",
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
            sandbox_root.mkdir(parents=True, exist_ok=True)
            archive_proc = subprocess.run(
                ["git", "archive", "--format=tar", task.base_commit],
                cwd=source_repo,
                capture_output=True,
                check=True,
            )
            subprocess.run(
                ["tar", "-xf", "-", "-C", str(sandbox_root)],
                input=archive_proc.stdout,
                capture_output=True,
                check=True,
            )
            subprocess.run(
                ["git", "init", "-b", "main"],
                cwd=sandbox_root,
                capture_output=True,
                text=True,
                check=True,
            )
            if task.test_patch:
                subprocess.run(
                    ["git", "apply", "--whitespace=nowarn", "-"],
                    cwd=sandbox_root,
                    input=task.test_patch,
                    text=True,
                    capture_output=True,
                    check=True,
                )
            self._git_commit_baseline(sandbox_root, f"chore(arena): seed baseline for {task.task_id}")
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

        baseline_files, _ = _snapshot_workspace_files(sandbox_root, baseline_include_harness=True)
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
            baseline_files=baseline_files,
            hidden_test_files={k: v.encode("utf-8") for k, v in task.hidden_test_files.items()},
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
    def run_bounded_command_detailed(
        argv: list[str],
        cwd: Path,
        env: dict[str, str] | None = None,
        timeout_sec: int = 90,
        stdin_text: str | None = None,
        reap_sandbox_cwd: bool = False,
        unset_env: tuple[str, ...] = (),
        inherit_parent_env: bool = True,
    ) -> BoundedCommandOutcome:
        """Execute a command in its own session, kill its group on timeout, and reap detached descendants."""
        merged_env = os.environ.copy() if inherit_parent_env else {}
        if env:
            merged_env.update(env)
        for key in unset_env:
            merged_env.pop(key, None)
        trial_token = merged_env.get("SKILL_ARENA_TRIAL_TOKEN") or f"sa_trial_{uuid.uuid4().hex}"
        merged_env["SKILL_ARENA_TRIAL_TOKEN"] = trial_token

        lock_fd_tmp, lock_name = tempfile.mkstemp(prefix="sa_proc_lock_")
        lock_path = Path(lock_name)
        fcntl.flock(lock_fd_tmp, fcntl.LOCK_SH)
        os.set_inheritable(lock_fd_tmp, True)

        proc: subprocess.Popen[bytes] | None = None
        sel = selectors.DefaultSelector()
        out_chunks: list[bytes] = []
        err_chunks: list[bytes] = []
        timed_out = False
        reaped: list[int] = []
        unreaped: list[int] = []
        leaked_records: list[dict[str, Any]] = []

        def _drain_fd_nonblocking(fd: int, sink: list[bytes]) -> bool:
            """Drain available bytes from non-blocking fd; return True when EOF is reached."""
            while True:
                try:
                    chunk = os.read(fd, 65536)
                except BlockingIOError:
                    return False
                except OSError as exc:
                    if exc.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
                        return False
                    return True
                if not chunk:
                    return True
                sink.append(chunk)

        try:
            proc = subprocess.Popen(
                argv,
                cwd=cwd,
                env=merged_env,
                stdin=subprocess.PIPE if stdin_text is not None else subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=False,
                start_new_session=True,
                pass_fds=(lock_fd_tmp,),
            )
            os.close(lock_fd_tmp)
            lock_fd_tmp = -1

            if stdin_text is not None and proc.stdin is not None:
                try:
                    proc.stdin.write(stdin_text.encode("utf-8"))
                except BrokenPipeError:
                    pass
                proc.stdin.close()

            assert proc.stdout is not None and proc.stderr is not None
            out_fd = proc.stdout.fileno()
            err_fd = proc.stderr.fileno()
            os.set_blocking(out_fd, False)
            os.set_blocking(err_fd, False)
            sel.register(out_fd, selectors.EVENT_READ, out_chunks)
            sel.register(err_fd, selectors.EVENT_READ, err_chunks)

            deadline = time.monotonic() + max(0.05, float(timeout_sec))
            while proc.poll() is None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    timed_out = True
                    break
                for key, _ in sel.select(timeout=min(0.04, remaining)):
                    eof = _drain_fd_nonblocking(key.fd, key.data)
                    if eof:
                        try:
                            sel.unregister(key.fd)
                        except KeyError:
                            pass

            # Always kill the initial process group once leader exited or timed out
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except OSError:
                pass
            try:
                proc.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                _kill_pid_and_group(proc.pid, signal.SIGKILL)

            out_eof = _drain_fd_nonblocking(out_fd, out_chunks)
            err_eof = _drain_fd_nonblocking(err_fd, err_chunks)

            # Fast check: does any escaped descendant still hold the inherited shared lock?
            lock_held = False
            check_fd = os.open(str(lock_path), os.O_RDWR)
            try:
                try:
                    fcntl.flock(check_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    fcntl.flock(check_fd, fcntl.LOCK_UN)
                except BlockingIOError:
                    lock_held = True
            finally:
                os.close(check_fd)

            pipes_still_held = (not out_eof) or (not err_eof)
            if lock_held or pipes_still_held or reap_sandbox_cwd:
                exclude = {os.getpid(), proc.pid}
                candidates: set[int] = set()
                if lock_held:
                    candidates |= _find_pids_holding_file(lock_path)
                if reap_sandbox_cwd or pipes_still_held:
                    candidates |= _find_pids_with_cwd_under(cwd)
                if reap_sandbox_cwd:
                    candidates |= _find_pids_with_env_token(trial_token)
                escaped = sorted(pid for pid in (candidates - exclude) if _pid_alive(pid))
                if escaped:
                    cmd_by_pid = {pid: _describe_pid_command(pid) for pid in escaped}
                    for pid in escaped:
                        _kill_pid_and_group(pid, signal.SIGKILL)
                    time.sleep(0.05)
                    for pid in escaped:
                        if _pid_alive(pid):
                            _kill_pid_and_group(pid, signal.SIGKILL)
                    time.sleep(0.03)
                    for pid in escaped:
                        still_alive = _pid_alive(pid)
                        if still_alive:
                            unreaped.append(pid)
                        else:
                            reaped.append(pid)
                        leaked_records.append(
                            {
                                "pid": pid,
                                "command": cmd_by_pid.get(pid, ""),
                                "reaped": not still_alive,
                            }
                        )

            _drain_fd_nonblocking(out_fd, out_chunks)
            _drain_fd_nonblocking(err_fd, err_chunks)
            rc = 124 if timed_out else (proc.returncode if proc.returncode is not None else 1)
            return BoundedCommandOutcome(
                returncode=rc,
                stdout=b"".join(out_chunks).decode("utf-8", errors="replace"),
                stderr=b"".join(err_chunks).decode("utf-8", errors="replace"),
                timed_out=timed_out,
                reaped_descendant_pids=reaped,
                unreaped_descendant_pids=unreaped,
                leaked_processes=leaked_records,
            )
        finally:
            if lock_fd_tmp >= 0:
                try:
                    os.close(lock_fd_tmp)
                except OSError:
                    pass
            try:
                sel.close()
            except OSError:
                pass
            if proc is not None:
                for stream in (proc.stdout, proc.stderr):
                    if stream is not None:
                        try:
                            stream.close()
                        except OSError:
                            pass
            try:
                lock_path.unlink(missing_ok=True)
            except OSError:
                pass

    @staticmethod
    def run_bounded_command(
        argv: list[str],
        cwd: Path,
        env: dict[str, str] | None = None,
        timeout_sec: int = 90,
        stdin_text: str | None = None,
    ) -> tuple[int, str, str, bool]:
        """Execute a command in its own POSIX process group and kill the entire group on timeout."""
        outcome = WorktreeSandboxPool.run_bounded_command_detailed(
            argv=argv,
            cwd=cwd,
            env=env,
            timeout_sec=timeout_sec,
            stdin_text=stdin_text,
        )
        return (outcome.returncode, outcome.stdout, outcome.stderr, outcome.timed_out)

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
