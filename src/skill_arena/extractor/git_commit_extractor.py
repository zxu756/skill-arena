"""Reverse-engineer real bug-fix commits from any local Git repository into TaskSpec tasks."""

from __future__ import annotations

from pathlib import Path
import re
import subprocess

from skill_arena.models import TaskSpec

_TEST_PATH_PATTERNS = (
    re.compile(r"(^|/)tests?/"),
    re.compile(r"(^|/)test_[^/]+\.py$"),
    re.compile(r"_test\.(py|go|ts|tsx|js|rs)$"),
    re.compile(r"\.(spec|test)\.(ts|tsx|js|jsx)$"),
    re.compile(r"Tests\.swift$"),
)


def is_test_path(rel_path: str) -> bool:
    return any(p.search(rel_path) for p in _TEST_PATH_PATTERNS)


def detect_default_test_command(repo_dir: Path, test_files: list[str]) -> str:
    if (repo_dir / "Package.swift").is_file():
        return "swift test"
    if (repo_dir / "Cargo.toml").is_file():
        return "cargo test"
    if (repo_dir / "package.json").is_file():
        return "npm test"
    py_tests = [f for f in test_files if f.endswith(".py")]
    if py_tests:
        return "python3 -m unittest " + " ".join(py_tests)
    if (repo_dir / "pyproject.toml").is_file():
        return "python3 -m unittest discover"
    return "python3 -m unittest"


def extract_task_from_commit(
    repo_path: Path,
    commit_ref: str = "HEAD",
    test_command: str | None = None,
) -> TaskSpec:
    """Extract (parent_commit, test_patch, protected_test_paths, prompt) from a historical commit."""
    repo = Path(repo_path).resolve()
    sha = subprocess.run(
        ["git", "rev-parse", commit_ref],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    parent_sha = subprocess.run(
        ["git", "rev-parse", f"{sha}^"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()

    meta = subprocess.run(
        ["git", " log", "-1", "--format=%s%n%n%b", sha] if False else ["git", "log", "-1", "--format=%s%n%n%b", sha],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    lines = [ln.strip() for ln in meta.splitlines() if ln.strip()]
    subject = lines[0] if lines else f"Fix regression from {sha[:8]}"
    body = "\n".join(lines[1:])

    changed = subprocess.run(
        ["git", "diff-tree", "--no-commit-id", "--name-only", "-r", sha],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.splitlines()

    test_files = [f for f in changed if is_test_path(f)]
    impl_files = [f for f in changed if not is_test_path(f)]
    if not test_files:
        raise ValueError(
            f"Commit {sha[:8]} does not touch any recognizable test files; cannot build a leak-free test oracle."
        )
    if not impl_files:
        raise ValueError(f"Commit {sha[:8]} only modifies test files; no implementation delta to benchmark.")

    test_patch = subprocess.run(
        ["git", "diff", parent_sha, sha, "--", *test_files],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout

    impl_diff_stat = subprocess.run(
        ["git", "diff", "--numstat", parent_sha, sha, "--", *impl_files],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.splitlines()
    gold_lines = 0
    for row in impl_diff_stat:
        parts = row.split("\t")
        if len(parts) >= 2 and parts[0].isdigit() and parts[1].isdigit():
            gold_lines += int(parts[0]) + int(parts[1])

    slug = re.sub(r"[^a-z0-9]+", "-", subject.lower()).strip("-")[:36]
    task_id = f"git-{sha[:7]}-{slug or 'task'}"
    resolved_cmd = test_command or detect_default_test_command(repo, test_files)

    prompt_parts = [
        f"Resolve the following issue in this repository: {subject}",
    ]
    if body:
        prompt_parts.append(f"\nContext:\n{body}")
    prompt_parts.append(
        f"\nVerification:\nRun `{resolved_cmd}` until all tests in {', '.join(test_files)} pass without modifying protected test files."
    )

    return TaskSpec(
        task_id=task_id,
        title=subject,
        category="git_extracted",
        prompt="\n".join(prompt_parts),
        test_command=resolved_cmd,
        protected_test_paths=test_files,
        repo_path=str(repo),
        base_commit=parent_sha,
        test_patch=test_patch,
        max_diff_lines=max(40, gold_lines * 2),
        timeout_sec=120,
    )
