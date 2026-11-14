"""Zero-LLM deterministic grader combining clean-checkout anti-cheat integrity, unit tests, diff bloat, and craft checks."""

from __future__ import annotations

import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import tempfile
from typing import Any

from skill_arena.graders.frontend_craft import audit_html_craft
from skill_arena.models import GraderVerdict, TaskSpec
from skill_arena.sandbox.worktree_pool import SandboxHandle, WorktreeSandboxPool

_UNITTEST_RAN_RE = re.compile(r"\bRan\s+(\d+)\s+tests?\s+in\b")
_PYTEST_ZERO_RE = re.compile(r"\b(?:no tests ran|collected\s+0\s+items)\b", re.IGNORECASE)
_PYTEST_PASSED_RE = re.compile(r"\b(\d+)\s+passed\b")
_PYTEST_PROBE_CACHE: dict[tuple[str, int], bool] = {}


def verify_test_runner_available(test_command: str) -> None:
    """Preflight check that `pytest` is installed in the grading environment before running `pytest` tasks."""
    argv = shlex.split(test_command)
    if not argv:
        raise ValueError("Task test_command cannot be empty.")

    exe = argv[0]
    exe_base = os.path.basename(exe)
    invokes_m_pytest = len(argv) >= 3 and argv[1] == "-m" and argv[2] in {"pytest", "py.test"}
    invokes_pytest_bin = exe_base in {"pytest", "py.test"}
    if not (invokes_m_pytest or invokes_pytest_bin):
        return

    resolved = exe if os.path.sep in exe else shutil.which(exe)
    if not resolved or not os.path.isfile(resolved) or not os.access(resolved, os.X_OK):
        raise RuntimeError(
            f"Grading preflight failed: required runner {exe!r} for test_command {test_command!r} was not found in PATH. "
            f"/ 判分启动自检失败：未在 PATH 中找到测试命令 {test_command!r} 所需的 {exe!r}（需安装 pytest）。"
        )

    if invokes_pytest_bin:
        return

    real_bin = os.path.realpath(resolved)
    try:
        mtime_ns = os.stat(real_bin).st_mtime_ns
    except OSError:
        mtime_ns = 0
    cache_key = (f"{resolved}->{real_bin}", mtime_ns)
    ok = _PYTEST_PROBE_CACHE.get(cache_key)
    if ok is None:
        probe = subprocess.run(
            [resolved, "-c", "import pytest"],
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        ok = probe.returncode == 0
        _PYTEST_PROBE_CACHE[cache_key] = ok
    if not ok:
        raise RuntimeError(
            f"Grading preflight failed: `pytest` is not installed in {resolved!r} (required by test_command {test_command!r}). "
            "Activate a Python environment with `pytest` installed before running this task. "
            f"/ 判分启动自检失败：当前 PATH 中的 {resolved!r} 未安装 `pytest`（测试命令 {test_command!r} 需要 pytest）。"
        )


def verify_suite_runners_available(tasks: list[TaskSpec]) -> None:
    """Verify required test runners (e.g. `pytest`) exist upfront for every task in `tasks`."""
    seen: set[str] = set()
    for task in tasks:
        if task.test_command not in seen:
            seen.add(task.test_command)
            verify_test_runner_available(task.test_command)


def _detect_zero_tests_ran(test_command: str, exit_code: int, combined_out: str) -> bool:
    """Return True if the test runner collected or executed 0 tests."""
    m_unit = _UNITTEST_RAN_RE.search(combined_out)
    if m_unit is not None and int(m_unit.group(1)) == 0:
        return True
    if _PYTEST_ZERO_RE.search(combined_out):
        return True
    cmd_lower = test_command.lower()
    if "pytest" in cmd_lower and exit_code == 5:
        return True
    if exit_code == 0:
        if "unittest" in cmd_lower and m_unit is None:
            return True
        if "pytest" in cmd_lower:
            m_pass = _PYTEST_PASSED_RE.search(combined_out)
            if m_pass is None or int(m_pass.group(1)) == 0:
                return True
    return False


def evaluate_sandbox(
    handle: SandboxHandle,
    task: TaskSpec,
    *,
    agent_timed_out: bool = False,
    reaped_descendant_pids: list[int] | None = None,
    unreaped_descendant_pids: list[int] | None = None,
    leaked_processes: list[dict[str, Any]] | None = None,
) -> tuple[GraderVerdict, str]:
    """Verify anti-cheat integrity, restore tampered files, and grade inside a fresh clean checkout."""
    reaped_pids = list(reaped_descendant_pids or [])
    unreaped_pids = list(unreaped_descendant_pids or [])
    leaked_list = [dict(item) for item in (leaked_processes or [])]
    if not leaked_list and (reaped_pids or unreaped_pids):
        for pid in reaped_pids:
            leaked_list.append({"pid": int(pid), "command": "", "reaped": True})
        for pid in unreaped_pids:
            leaked_list.append({"pid": int(pid), "command": "", "reaped": False})

    def _format_leaked_summary() -> str:
        parts = []
        for item in leaked_list:
            pid = item.get("pid")
            cmd = str(item.get("command") or "").strip()
            status = "reaped" if item.get("reaped", True) else "UNREAPED"
            parts.append(f"pid={pid} ({status}{': ' + cmd if cmd else ''})")
        return ", ".join(parts)

    anti_cheat_clean, tampered_files = handle.verify_and_restore_protected_tests()
    git_diff, added, removed = handle.capture_git_diff()
    bloat_ratio = round((added + removed) / float(max(1, task.max_diff_lines)), 3)

    if agent_timed_out:
        reasons = [f"AGENT TIMEOUT: agent execution exceeded {task.timeout_sec}s limit (超时判败)"]
        if not anti_cheat_clean:
            reasons.append(f"ANTI-CHEAT VIOLATION: forbidden/protected path(s) {', '.join(tampered_files)}")
        if leaked_list:
            reasons.append(f"LEAKED BACKGROUND PROCESS(ES): {_format_leaked_summary()}")
        reasons.append(f"diff +{added}/-{removed} ({bloat_ratio:.2f}x budget)")
        return (
            GraderVerdict(
                passed=False,
                exit_code=124,
                anti_cheat_clean=anti_cheat_clean,
                tampered_test_files=tampered_files,
                leaked_processes=leaked_list,
                diff_lines_added=added,
                diff_lines_removed=removed,
                diff_bloat_ratio=bloat_ratio,
                summary="; ".join(reasons),
                stdout_tail="Trial aborted and failed due to agent execution timeout.",
            ),
            git_diff,
        )

    grading_dir = Path(tempfile.mkdtemp(prefix="sa-clean-grade-", dir=handle.root.parent))
    try:
        verify_test_runner_available(task.test_command)
        handle.materialize_clean_grading_checkout(grading_dir)
        argv = shlex.split(task.test_command)
        exit_code, stdout, stderr, timed_out = WorktreeSandboxPool.run_bounded_command(
            argv,
            cwd=grading_dir,
            env={"PYTHONDONTWRITEBYTECODE": "1"},
            timeout_sec=min(60, task.timeout_sec),
        )
        combined_out = ((stdout or "") + ("\n" if stdout and stderr else "") + (stderr or "")).strip()
        tail = combined_out[-1200:] if len(combined_out) > 1200 else combined_out
        zero_tests_ran = _detect_zero_tests_ran(task.test_command, exit_code, combined_out)

        sloplint_fails = 0
        sloplint_warns = 0
        craft_notes: list[str] = []
        if task.frontend_target_html:
            craft = audit_html_craft(grading_dir / task.frontend_target_html)
            sloplint_fails = craft.fails
            sloplint_warns = craft.warns
            craft_notes = craft.findings
    finally:
        shutil.rmtree(grading_dir, ignore_errors=True)

    effective_exit = exit_code if (exit_code != 0 or not zero_tests_ran) else 5
    passed = (
        anti_cheat_clean
        and not timed_out
        and not leaked_list
        and not unreaped_pids
        and not zero_tests_ran
        and exit_code == 0
        and sloplint_fails == 0
        and sloplint_warns == 0
    )

    reasons = []
    if not anti_cheat_clean:
        reasons.append(f"ANTI-CHEAT VIOLATION: modified protected/forbidden file(s) {', '.join(tampered_files)}")
    if leaked_list:
        reasons.append(f"LEAKED BACKGROUND PROCESS(ES): {_format_leaked_summary()}")
    if timed_out:
        reasons.append("Test suite timed out")
    elif zero_tests_ran:
        reasons.append("ZERO TESTS EXECUTED: 0 tests ran (跑了 0 个测试判败)")
    elif exit_code != 0:
        reasons.append(f"Tests exited with code {exit_code}")
    else:
        reasons.append("Unit tests passed (exit 0)")

    if task.frontend_target_html:
        if sloplint_fails == 0 and sloplint_warns == 0:
            reasons.append("Hallmark craft gate clean (0 FAIL, 0 WARN)")
        else:
            reasons.append(f"Craft gate {sloplint_fails} FAIL / {sloplint_warns} WARN")
            if craft_notes:
                tail = (tail + "\n" + "\n".join(craft_notes)).strip()[-1200:]

    reasons.append(f"diff +{added}/-{removed} ({bloat_ratio:.2f}x budget)")
    return (
        GraderVerdict(
            passed=passed,
            exit_code=effective_exit,
            anti_cheat_clean=anti_cheat_clean,
            tampered_test_files=tampered_files,
            leaked_processes=leaked_list,
            diff_lines_added=added,
            diff_lines_removed=removed,
            diff_bloat_ratio=bloat_ratio,
            sloplint_fails=sloplint_fails,
            sloplint_warns=sloplint_warns,
            summary="; ".join(reasons),
            stdout_tail=tail,
        ),
        git_diff,
    )
