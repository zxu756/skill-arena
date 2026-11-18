"""Quality of the tests an agent wrote itself, measured by mutation score.

After grading, the agent's NEW test modules (the ones ``is_permitted_new_agent_test_file`` accepts)
are run in throw-away checkouts: once against the task's reference solution (tests that fail there
are invalid) and once against every mutant, a plausible but wrong implementation. A mutant is
killed when the agent's tests fail on it. None of this changes whether the trial passed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile

from skill_arena.graders.deterministic import _detect_zero_tests_ran
from skill_arena.models import TaskSpec
from skill_arena.sandbox.worktree_pool import (
    SandboxHandle,
    WorktreeSandboxPool,
    _snapshot_workspace_files,
    is_permitted_new_agent_test_file,
)

MUTATION_RUN_TIMEOUT_SEC = 60
_PYTEST_AVAILABLE: dict[str, bool] = {}


@dataclass(slots=True)
class AgentTestScore:
    files: list[str] = field(default_factory=list)
    valid: bool | None = None
    mutants_killed: int | None = None
    mutants_total: int | None = None
    note: str = ""


def collect_agent_test_files(handle: SandboxHandle) -> dict[str, bytes]:
    """New plain test modules the agent added to its workspace."""
    protected = set(handle.protected_hashes.keys())
    current, _ = _snapshot_workspace_files(handle.root)
    return {
        rel: data
        for rel, data in sorted(current.items())
        if rel not in handle.baseline_files and is_permitted_new_agent_test_file(rel, protected)
    }


def task_python(test_command: str = "") -> str:
    """The interpreter the task's own test_command uses (e.g. ``python3.12``), else ``python3``."""
    try:
        argv = shlex.split(test_command)
    except ValueError:
        argv = []
    if argv and os.path.basename(argv[0]).startswith("python"):
        exe = argv[0]
        return exe if os.path.sep in exe else (shutil.which(exe) or exe)
    return shutil.which("python3") or sys.executable


def _pytest_available(python: str) -> bool:
    cached = _PYTEST_AVAILABLE.get(python)
    if cached is None:
        try:
            probe = subprocess.run(
                [python, "-c", "import pytest"], capture_output=True, text=True, timeout=15, check=False
            )
            cached = probe.returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            cached = False
        _PYTEST_AVAILABLE[python] = cached
    return cached


def agent_test_command(test_files: list[str], test_command: str = "") -> list[str]:
    """pytest when the task's python has it (it also runs unittest classes), else unittest."""
    python = task_python(test_command)
    if _pytest_available(python):
        return [python, "-m", "pytest", "-q", "-p", "no:cacheprovider", *test_files]
    return [python, "-m", "unittest", *test_files]


def _materialize(dest: Path, layers: list[dict[str, bytes]]) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    for layer in layers:
        for rel, data in layer.items():
            out = dest / rel
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(data)


def _run_agent_tests(work_root: Path, layers: list[dict[str, bytes]], argv: list[str], timeout_sec: int) -> tuple[bool, bool]:
    """Return (tests_passed, zero_tests_ran) for one throw-away checkout."""
    run_dir = Path(tempfile.mkdtemp(prefix="sa-mutation-", dir=work_root))
    try:
        _materialize(run_dir, layers)
        exit_code, stdout, stderr, timed_out = WorktreeSandboxPool.run_bounded_command(
            argv,
            cwd=run_dir,
            env={"PYTHONDONTWRITEBYTECODE": "1"},
            timeout_sec=timeout_sec,
        )
        combined = f"{stdout}\n{stderr}"
        zero = _detect_zero_tests_ran(" ".join(argv), exit_code, combined)
        return (exit_code == 0 and not timed_out and not zero), zero
    finally:
        shutil.rmtree(run_dir, ignore_errors=True)


def score_agent_tests(handle: SandboxHandle, task: TaskSpec, *, timeout_sec: int | None = None) -> AgentTestScore:
    """Mutation score of the agent's own tests; skipped gracefully when there is nothing to measure."""
    agent_tests = collect_agent_test_files(handle)
    score = AgentTestScore(files=sorted(agent_tests))
    if not agent_tests:
        score.note = "agent wrote no new test files"
        return score
    if not task.reference_solution_files:
        score.note = "task has no reference solution; agent tests not scored"
        return score

    limit = int(timeout_sec or min(MUTATION_RUN_TIMEOUT_SEC, max(5, int(task.timeout_sec))))
    argv = agent_test_command(sorted(agent_tests), task.test_command)
    runner = "pytest" if "pytest" in argv else "unittest"
    baseline = {rel: data for rel, data in handle.baseline_files.items() if rel not in {"AGENTS.md", "CLAUDE.md"}}
    reference = {rel: src.encode("utf-8") for rel, src in task.reference_solution_files.items()}
    work_root = handle.root.parent

    ok_on_reference, zero = _run_agent_tests(work_root, [baseline, reference, agent_tests], argv, limit)
    score.valid = ok_on_reference
    if not ok_on_reference:
        score.note = f"{runner}: agent tests {'ran 0 tests' if zero else 'fail'} on the reference solution (invalid)"
        score.mutants_total = len(task.mutant_files) if task.mutant_files else None
        return score
    if not task.mutant_files:
        score.note = f"{runner}: agent tests pass on the reference; task has no mutants"
        return score

    killed = 0
    for mutant in task.mutant_files:
        overlay = {rel: src.encode("utf-8") for rel, src in mutant.items()}
        passed_on_mutant, _ = _run_agent_tests(work_root, [baseline, overlay, agent_tests], argv, limit)
        if not passed_on_mutant:
            killed += 1
    score.mutants_killed = killed
    score.mutants_total = len(task.mutant_files)
    score.note = f"{runner}: killed {killed}/{len(task.mutant_files)} mutants"
    return score
