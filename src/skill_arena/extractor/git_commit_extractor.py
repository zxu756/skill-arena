"""Reverse-engineer real bug-fix commits from any local Git repository into TaskSpec tasks without leaking commit messages."""

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

_PYTEST_IMPORT_OR_MARK_RE = re.compile(r"(?:^\s*(?:import\s+pytest|from\s+pytest\s+import)\b|@pytest\.)", re.MULTILINE)
_UNITTEST_IMPORT_RE = re.compile(r"^\s*(?:import\s+unittest|from\s+unittest\s+import)\b", re.MULTILINE)
_UNITTEST_CASE_RE = re.compile(r"\b(?:unittest\.)?TestCase\b")
_BARE_PYTEST_DEF_RE = re.compile(r"^(?:async\s+def|def)\s+test_[A-Za-z0-9_]*\s*\(|^class\s+Test[A-Za-z0-9_]*\b", re.MULTILINE)


def is_test_path(rel_path: str) -> bool:
    return any(p.search(rel_path) for p in _TEST_PATH_PATTERNS)


def _git_show_file(repo_dir: Path, rev: str, rel_path: str) -> str | None:
    proc = subprocess.run(
        ["git", "show", f"{rev}:{rel_path}"],
        cwd=repo_dir,
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        return None
    return proc.stdout


def detect_default_test_command(
    repo_dir: Path,
    commit_sha: str,
    parent_sha: str,
    test_files: list[str],
) -> str:
    """Auto-detect unittest vs pytest (or Swift/Rust/Node) at extracted commit; fail if ambiguous."""
    root_entries = set(
        subprocess.run(
            ["git", "ls-tree", "--name-only", parent_sha],
            cwd=repo_dir,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.splitlines()
    )
    if "Package.swift" in root_entries:
        return "swift test"
    if "Cargo.toml" in root_entries:
        return "cargo test"
    if "package.json" in root_entries and not any(f.endswith(".py") for f in test_files):
        return "npm test"

    py_tests = [f for f in test_files if f.endswith(".py")]
    if not py_tests:
        raise ValueError(
            "Cannot auto-detect test runner for extracted commit; please specify `--test-cmd` explicitly. "
            "/ 无法自动判断测试框架，请显式传入 `--test-cmd`。"
        )

    has_pytest_cfg = False
    for cfg_name in ("pytest.ini", ".pytest.ini", "conftest.py", "tests/conftest.py"):
        if _git_show_file(repo_dir, parent_sha, cfg_name) is not None or _git_show_file(repo_dir, commit_sha, cfg_name) is not None:
            has_pytest_cfg = True
            break
    if not has_pytest_cfg:
        pyproject_txt = _git_show_file(repo_dir, parent_sha, "pyproject.toml") or ""
        setup_cfg_txt = _git_show_file(repo_dir, parent_sha, "setup.cfg") or ""
        if "[tool.pytest" in pyproject_txt or "[tool:pytest]" in setup_cfg_txt:
            has_pytest_cfg = True

    uses_pytest_api = False
    uses_unittest_case = False
    has_bare_test_defs = False

    for rel_test in py_tests:
        src = _git_show_file(repo_dir, commit_sha, rel_test) or ""
        if _PYTEST_IMPORT_OR_MARK_RE.search(src):
            uses_pytest_api = True
        if _UNITTEST_IMPORT_RE.search(src) and _UNITTEST_CASE_RE.search(src):
            uses_unittest_case = True
        if _BARE_PYTEST_DEF_RE.search(src):
            has_bare_test_defs = True

    joined = " ".join(py_tests)
    if uses_pytest_api or (has_bare_test_defs and not uses_unittest_case) or (has_pytest_cfg and (has_bare_test_defs or uses_unittest_case)):
        return f"python3 -m pytest {joined}"
    if uses_unittest_case:
        return f"python3 -m unittest {joined}"

    raise ValueError(
        "Cannot auto-detect whether extracted Python tests use unittest or pytest; please pass `--test-cmd` explicitly. "
        "/ 无法自动判断使用 unittest 还是 pytest，请显式指定 `--test-cmd`。"
    )


def extract_task_from_commit(
    repo_path: Path,
    commit_ref: str = "HEAD",
    test_command: str | None = None,
) -> TaskSpec:
    """Extract (parent_commit, test_patch, protected_test_paths, prompt) without commit message leakage."""
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
            f"Commit {sha[:8]} does not touch any recognizable test files; cannot extract deterministic test oracle."
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

    stem_slug = re.sub(r"[^a-z0-9]+", "-", Path(impl_files[0]).stem.lower()).strip("-")[:24] or "task"
    task_id = f"git-{parent_sha[:7]}-{stem_slug}"
    impl_summary = ", ".join(impl_files[:3])
    title = f"Regression task in {impl_summary} (base {parent_sha[:7]})"
    resolved_cmd = test_command or detect_default_test_command(repo, sha, parent_sha, test_files)

    prompt = (
        f"Fix the failing behavior in {', '.join(impl_files)} so that the test suite passes.\n\n"
        f"Verification:\n"
        f"Run `{resolved_cmd}` until all tests in {', '.join(test_files)} pass without modifying protected test files."
    )

    return TaskSpec(
        task_id=task_id,
        title=title,
        category="git_extracted",
        prompt=prompt,
        test_command=resolved_cmd,
        protected_test_paths=test_files,
        repo_path=str(repo),
        base_commit=parent_sha,
        test_patch=test_patch,
        max_diff_lines=max(40, gold_lines * 2),
        timeout_sec=120,
    )
