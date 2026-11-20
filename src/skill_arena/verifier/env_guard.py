"""Clean repository cloning, read-only source fingerprinting, isolated offline env, and process/file cleanup."""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import os
from pathlib import Path
import pwd
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from typing import Any

from skill_arena.sandbox.worktree_pool import WorktreeSandboxPool


class VerifyInputError(ValueError):
    """Raised when CLI inputs or target repository fail preflight validation (maps to exit code 2)."""


@dataclass(frozen=True, slots=True)
class RepoFingerprint:
    """Read-only snapshot of source repository git state to detect any accidental mutation."""

    head_sha: str
    index_mtime_ns: int | None
    index_sha256: str | None
    status_sha256: str
    refs_sha256: str

    def short_digest(self) -> str:
        payload = (
            f"{self.head_sha}\n"
            f"{self.index_mtime_ns}\n"
            f"{self.index_sha256}\n"
            f"{self.status_sha256}\n"
            f"{self.refs_sha256}"
        ).encode("utf-8")
        mtime_tag = str(self.index_mtime_ns) if self.index_mtime_ns is not None else "no-index"
        return f"{self.head_sha[:12]}:{mtime_tag}:{hashlib.sha256(payload).hexdigest()[:16]}"


@dataclass(frozen=True, slots=True)
class VerifyWorkspace:
    """Single-mkdtemp isolated directory layout holding repo/, home/, tmp/, and bin/."""

    tmp_base: Path
    repo_dir: Path
    home_dir: Path
    tmp_dir: Path
    bin_dir: Path


@dataclass(slots=True)
class CommandRunResult:
    """Bounded subprocess execution result with wall-clock timing and orphan-process telemetry."""

    returncode: int
    stdout: str
    stderr: str
    timed_out: bool
    wall_ms: int
    reaped_pids: list[int] = field(default_factory=list)
    unreaped_pids: list[int] = field(default_factory=list)
    leaked_processes: list[dict[str, Any]] = field(default_factory=list)

    @property
    def combined_output(self) -> str:
        if self.stdout and self.stderr:
            return f"{self.stdout.rstrip()}\n{self.stderr.rstrip()}"
        return (self.stdout or self.stderr or "").rstrip()


def _real_account_home() -> Path:
    return Path(pwd.getpwuid(os.getuid()).pw_dir)


def _sb_escape(path_str: str) -> str:
    return path_str.replace("\\", "\\\\").replace('"', '\\"')


def sandbox_exec_available() -> bool:
    """Return True when macOS `/usr/bin/sandbox-exec` is available for kernel-level network/HOME isolation."""
    return sys.platform == "darwin" and Path("/usr/bin/sandbox-exec").is_file()


def build_verifier_sandbox_profile() -> str:
    """Build macOS Seatbelt `(version 1)` profile blocking external network and real-HOME writes.

    Rules:
      1. Network: `(deny network*)`, allowing ONLY Unix domain sockets and `localhost` loopback
         (`network-bind`/`network-inbound` on `localhost:*` and `network-outbound` ONLY to `remote ip "localhost:*"`
         so unbound outbound client sockets cannot escape to LAN/Internet).
      2. Real account home (`pwd.getpwuid(os.getuid()).pw_dir`): `(deny file-write* (subpath "<HOME>"))`,
         allowing writes ONLY to shared `~/.cache/uv` (P7) plus standard temp/device paths (`/tmp`, `/private/tmp`,
         `/var/folders`, `/private/var/folders`, `/dev`).
    """
    raw_home = str(_real_account_home())
    real_home = os.path.realpath(raw_home)
    homes: list[str] = []
    for candidate in (raw_home, real_home):
        if candidate and candidate != "/" and candidate not in homes:
            homes.append(candidate)

    clauses = [
        "(version 1)",
        "(allow default)",
        "(deny network*)",
        "(allow network* (local unix))",
        "(allow network* (remote unix))",
        '(allow network-bind (local ip "localhost:*"))',
        '(allow network-inbound (local ip "localhost:*"))',
        '(allow network-outbound (remote ip "localhost:*"))',
    ]
    for home_str in homes:
        clauses.append(f'(deny file-write* (subpath "{_sb_escape(home_str)}"))')
    for home_str in homes:
        clauses.append(f'(allow file-write* (subpath "{_sb_escape(home_str)}/.cache/uv"))')
    for sys_writable in ("/private/tmp", "/tmp", "/private/var/folders", "/var/folders", "/dev"):
        clauses.append(f'(allow file-write* (subpath "{sys_writable}"))')
    return "".join(clauses)


def snapshot_uv_cache_entries(uv_cache_dir: Path | None = None, max_depth: int = 3) -> set[str]:
    """Capture bounded relative entry paths under `~/.cache/uv` (up to `max_depth`) to count newly added cache entries."""
    root = uv_cache_dir if uv_cache_dir is not None else (_real_account_home() / ".cache" / "uv")
    if not root.is_dir():
        return set()
    root_str = str(root)
    entries: set[str] = set()

    def _scan(cur_dir: str, depth: int) -> None:
        try:
            with os.scandir(cur_dir) as iterator:
                for entry in iterator:
                    rel = entry.path[len(root_str) + 1 :]
                    entries.add(rel)
                    if depth < max_depth and entry.is_dir(follow_symlinks=False):
                        _scan(entry.path, depth + 1)
        except OSError:
            return

    _scan(root_str, 1)
    return entries


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path) -> str | None:
    if not path.is_file():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _run_readonly_git(repo_path: Path, *args: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        ["git", "--no-optional-locks", "-C", str(repo_path), *args],
        capture_output=True,
        check=False,
    )


def validate_git_repo_and_resolve_ref(src_repo: Path, ref: str = "HEAD") -> tuple[Path, str]:
    """Verify `src_repo` is an existing Git repository and resolve `ref` to a 40-char commit SHA."""
    expanded = src_repo.expanduser()
    if not expanded.exists() or not expanded.is_dir():
        raise VerifyInputError(
            f"Target repository path does not exist or is not a directory: {src_repo} "
            f"/ 目标仓库路径不存在或不是目录：{src_repo}"
        )
    resolved = expanded.resolve()

    git_dir_proc = _run_readonly_git(resolved, "rev-parse", "--git-dir")
    if git_dir_proc.returncode != 0:
        raise VerifyInputError(
            f"Path is not a valid Git repository: {src_repo} / 路径不是合法的 Git 仓库：{src_repo}"
        )

    commit_proc = _run_readonly_git(resolved, "rev-parse", "--verify", f"{ref}^{{commit}}")
    if commit_proc.returncode != 0:
        detail = commit_proc.stderr.decode("utf-8", errors="replace").strip()
        raise VerifyInputError(
            f"Cannot resolve Git ref {ref!r} in {src_repo}: {detail} "
            f"/ 无法在仓库中解析提交引用 {ref!r}：{detail}"
        )
    commit_sha = commit_proc.stdout.decode("utf-8", errors="replace").strip()
    return resolved, commit_sha


def repo_fingerprint(src_repo: Path) -> RepoFingerprint:
    """Capture a strictly read-only fingerprint of `src_repo` without touching `.git/index` locks."""
    resolved = src_repo.expanduser().resolve()
    git_dir_proc = _run_readonly_git(resolved, "rev-parse", "--git-dir")
    if git_dir_proc.returncode != 0:
        raise VerifyInputError(f"Not a Git repository: {src_repo} / 不是 Git 仓库：{src_repo}")

    raw_git_dir = git_dir_proc.stdout.decode("utf-8", errors="replace").strip()
    git_dir = Path(raw_git_dir)
    if not git_dir.is_absolute():
        git_dir = (resolved / git_dir).resolve()

    head_proc = _run_readonly_git(resolved, "rev-parse", "HEAD")
    head_sha = (
        head_proc.stdout.decode("utf-8", errors="replace").strip()
        if head_proc.returncode == 0
        else "UNBORN"
    )

    index_path = git_dir / "index"
    if index_path.is_file():
        index_mtime_ns: int | None = index_path.stat().st_mtime_ns
        index_sha256 = _sha256_file(index_path)
    else:
        index_mtime_ns = None
        index_sha256 = None

    bare_proc = _run_readonly_git(resolved, "rev-parse", "--is-bare-repository")
    is_bare = bare_proc.stdout.decode("utf-8", errors="replace").strip() == "true"
    if is_bare:
        status_sha256 = _sha256_bytes(b"bare")
    else:
        st_proc = _run_readonly_git(resolved, "status", "--porcelain=v1", "--ignored=matching")
        status_sha256 = _sha256_bytes(st_proc.stdout)

    refs_proc = _run_readonly_git(resolved, "show-ref")
    refs_sha256 = _sha256_bytes(refs_proc.stdout)

    return RepoFingerprint(
        head_sha=head_sha,
        index_mtime_ns=index_mtime_ns,
        index_sha256=index_sha256,
        status_sha256=status_sha256,
        refs_sha256=refs_sha256,
    )


def create_verify_workspace(prefix: str = "sa-verify-") -> VerifyWorkspace:
    """Create a single `mkdtemp` root with isolated `repo/`, `home/`, `tmp/`, and `bin/` directories.

    Prefer `/tmp` (`/private/tmp`) when writable so canonical paths stay short enough for macOS 104-byte
    `AF_UNIX` sockets and single-write `<1024B` shell buffers inside deeply nested pytest fixtures.
    """
    short_parent = "/tmp" if os.path.isdir("/tmp") and os.access("/tmp", os.W_OK) else None
    tmp_base = Path(tempfile.mkdtemp(prefix=prefix, dir=short_parent)).resolve()
    repo_dir = tmp_base / "repo"
    home_dir = tmp_base / "home"
    tmp_dir = tmp_base / "tmp"
    bin_dir = tmp_base / "bin"

    for directory in (
        home_dir,
        home_dir / ".config",
        home_dir / ".cache",
        home_dir / ".local" / "share",
        home_dir / ".local" / "state",
        home_dir / ".local" / "bin",
        tmp_dir,
        bin_dir,
    ):
        directory.mkdir(parents=True, exist_ok=True)

    return VerifyWorkspace(
        tmp_base=tmp_base,
        repo_dir=repo_dir,
        home_dir=home_dir,
        tmp_dir=tmp_dir,
        bin_dir=bin_dir,
    )


def build_isolated_verify_env(workspace: VerifyWorkspace) -> dict[str, str]:
    """Construct an allowlisted, offline, HOME-isolated environment for verification subprocesses."""
    pw_home = _real_account_home()
    harness_venv_bin = Path(sys.executable).resolve().parent

    raw_path_parts = os.environ.get("PATH", "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin").split(os.pathsep)
    system_parts: list[str] = []
    seen_parts: set[str] = set()
    for part in raw_path_parts:
        if not part:
            continue
        try:
            resolved_part = Path(part).resolve()
        except OSError:
            resolved_part = Path(part)
        # Keep SkillArena's own .venv/bin out of front/middle PATH so target repos use their own .venv or system Python first
        if resolved_part == harness_venv_bin:
            continue
        if part not in seen_parts:
            seen_parts.add(part)
            system_parts.append(part)

    ordered_path = [
        str(workspace.home_dir / ".local" / "bin"),
        str(workspace.repo_dir / ".venv" / "bin"),
        str(workspace.bin_dir),
        *system_parts,
        str(harness_venv_bin),
    ]

    env: dict[str, str] = {
        "PATH": os.pathsep.join(ordered_path),
        "HOME": str(workspace.home_dir),
        "TMPDIR": str(workspace.tmp_dir),
        "TEMP": str(workspace.tmp_dir),
        "TMP": str(workspace.tmp_dir),
        "XDG_CONFIG_HOME": str(workspace.home_dir / ".config"),
        "XDG_DATA_HOME": str(workspace.home_dir / ".local" / "share"),
        "XDG_CACHE_HOME": str(workspace.home_dir / ".cache"),
        "XDG_STATE_HOME": str(workspace.home_dir / ".local" / "state"),
        "USER": "verifier",
        "LOGNAME": "verifier",
        "LANG": "en_US.UTF-8",
        "LC_ALL": "en_US.UTF-8",
        "TZ": "UTC",
        "TERM": "dumb",
        "NO_COLOR": "1",
        "CI": "true",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONUNBUFFERED": "1",
        "PIP_NO_INDEX": "1",
        "PIP_DISABLE_PIP_VERSION_CHECK": "1",
        "UV_OFFLINE": "1",
        "UV_NO_PROGRESS": "1",
        "UV_TOOL_DIR": str(workspace.home_dir / ".local" / "share" / "uv" / "tools"),
        "UV_TOOL_BIN_DIR": str(workspace.home_dir / ".local" / "bin"),
        "CLANG_MODULE_CACHE_PATH": str(workspace.tmp_dir / "clang-module-cache"),
        "SWIFTPM_MODULECACHE_OVERRIDE": str(workspace.tmp_dir / "swiftpm-module-cache"),
        "NPM_CONFIG_OFFLINE": "true",
        "CARGO_NET_OFFLINE": "true",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_COUNT": "3",
        "GIT_CONFIG_KEY_0": "protocol.https.allow",
        "GIT_CONFIG_VALUE_0": "never",
        "GIT_CONFIG_KEY_1": "protocol.http.allow",
        "GIT_CONFIG_VALUE_1": "never",
        "GIT_CONFIG_KEY_2": "core.fsmonitor",
        "GIT_CONFIG_VALUE_2": "false",
        "GIT_AUTHOR_NAME": "Verifier",
        "GIT_AUTHOR_EMAIL": "verifier@example.invalid",
        "GIT_COMMITTER_NAME": "Verifier",
        "GIT_COMMITTER_EMAIL": "verifier@example.invalid",
        "HTTP_PROXY": "http://127.0.0.1:9",
        "HTTPS_PROXY": "http://127.0.0.1:9",
        "ALL_PROXY": "http://127.0.0.1:9",
        "NO_PROXY": "127.0.0.1,localhost",
    }

    # Read-only reuse of host uv cache and Python installations so `uv sync --offline` works without network
    uv_cache = os.environ.get("UV_CACHE_DIR") or str(pw_home / ".cache" / "uv")
    if Path(uv_cache).is_dir():
        env["UV_CACHE_DIR"] = uv_cache
    uv_py_dir = os.environ.get("UV_PYTHON_INSTALL_DIR") or str(pw_home / ".local" / "share" / "uv" / "python")
    if Path(uv_py_dir).is_dir():
        env["UV_PYTHON_INSTALL_DIR"] = uv_py_dir

    # Preserve macOS Xcode/SDK paths and SSL CA bundle pointers if present on host
    for passthrough_key in (
        "DEVELOPER_DIR",
        "SDKROOT",
        "SYSTEMROOT",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
    ):
        val = os.environ.get(passthrough_key)
        if val:
            env[passthrough_key] = val

    return env


def run_verified_command(
    argv: list[str],
    cwd: Path,
    env: dict[str, str],
    timeout_sec: int = 300,
    *,
    use_sandbox_exec: bool = True,
) -> CommandRunResult:
    """Run `argv` inside `sandbox-exec` (offline + HOME-write guard) via `WorktreeSandboxPool.run_bounded_command_detailed`."""
    effective_argv = (
        ["/usr/bin/sandbox-exec", "-p", build_verifier_sandbox_profile(), *argv]
        if (use_sandbox_exec and sandbox_exec_available())
        else list(argv)
    )
    t0 = time.monotonic()
    outcome = WorktreeSandboxPool.run_bounded_command_detailed(
        argv=effective_argv,
        cwd=cwd,
        env=env,
        timeout_sec=timeout_sec,
        reap_sandbox_cwd=True,
        inherit_parent_env=False,
    )
    wall_ms = int(round((time.monotonic() - t0) * 1000))
    return CommandRunResult(
        returncode=outcome.returncode,
        stdout=outcome.stdout,
        stderr=outcome.stderr,
        timed_out=outcome.timed_out,
        wall_ms=wall_ms,
        reaped_pids=list(outcome.reaped_descendant_pids),
        unreaped_pids=list(outcome.unreaped_descendant_pids),
        leaked_processes=list(outcome.leaked_processes),
    )


def clone_clean_repo(
    src_repo: Path,
    commit_sha: str,
    workspace: VerifyWorkspace,
    env: dict[str, str],
    timeout_sec: int = 120,
) -> CommandRunResult:
    """Clone `src_repo` with `git clone --no-local`, disable push to origin, and check out `commit_sha`."""
    clone_res = run_verified_command(
        ["git", "clone", "--no-local", "--quiet", str(src_repo), str(workspace.repo_dir)],
        cwd=workspace.tmp_base,
        env=env,
        timeout_sec=timeout_sec,
    )
    if clone_res.returncode != 0:
        return clone_res

    # Block any accidental `git push` inside the clone from touching `src_repo`
    _ = run_verified_command(
        ["git", "remote", "set-url", "--push", "origin", "/dev/null/push-disabled-by-verifier"],
        cwd=workspace.repo_dir,
        env=env,
        timeout_sec=30,
    )

    head_res = run_verified_command(
        ["git", "rev-parse", "HEAD"],
        cwd=workspace.repo_dir,
        env=env,
        timeout_sec=30,
    )
    cloned_head = head_res.stdout.strip()
    if cloned_head != commit_sha:
        co_res = run_verified_command(
            ["git", "-c", "advice.detachedHead=false", "checkout", "--quiet", commit_sha],
            cwd=workspace.repo_dir,
            env=env,
            timeout_sec=60,
        )
        if co_res.returncode != 0:
            return co_res

    return clone_res


def _make_tree_writable(root: Path) -> None:
    if not root.exists() and not root.is_symlink():
        return
    if root.is_symlink():
        return
    try:
        os.chmod(root, root.stat().st_mode | stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
    except OSError:
        pass
    for dirpath, dirnames, filenames in os.walk(root, topdown=True, followlinks=False):
        dp = Path(dirpath)
        try:
            os.chmod(dp, dp.stat().st_mode | stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
        except OSError:
            pass
        for dname in dirnames:
            sub = dp / dname
            if not sub.is_symlink():
                try:
                    os.chmod(sub, sub.stat().st_mode | stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
                except OSError:
                    pass
        for fname in filenames:
            fpath = dp / fname
            if not fpath.is_symlink():
                try:
                    os.chmod(fpath, fpath.stat().st_mode | stat.S_IRUSR | stat.S_IWUSR)
                except OSError:
                    pass


def cleanup_temp_workspace(tmp_base: Path) -> bool:
    """Repair read-only permissions, remove `tmp_base` recursively, and assert it no longer exists."""
    if not tmp_base.exists() and not tmp_base.is_symlink():
        return True

    _make_tree_writable(tmp_base)

    def _retry_remove(func: Any, path_str: str, _exc: Any) -> None:
        parent = os.path.dirname(path_str)
        for candidate, perms in ((parent, 0o700), (path_str, 0o700)):
            if candidate:
                try:
                    os.chmod(candidate, perms)
                except OSError:
                    pass
        func(path_str)

    if sys.version_info >= (3, 12):
        shutil.rmtree(tmp_base, onexc=_retry_remove)
    else:
        shutil.rmtree(tmp_base, onerror=_retry_remove)

    deleted = not tmp_base.exists() and not tmp_base.is_symlink()
    assert deleted, f"Temporary verification workspace was not deleted: {tmp_base}"
    return deleted
