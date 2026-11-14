"""Zero-LLM deterministic grader combining clean-checkout anti-cheat integrity, unit tests, diff bloat, and craft checks."""

from __future__ import annotations

from pathlib import Path
import re
import shlex
import shutil
import tempfile

from skill_arena.graders.frontend_craft import audit_html_craft
from skill_arena.models import GraderVerdict, TaskSpec
from skill_arena.sandbox.worktree_pool import SandboxHandle, WorktreeSandboxPool

_UNITTEST_RAN_RE = re.compile(r"\bRan\s+(\d+)\s+tests?\s+in\b")
_PYTEST_ZERO_RE = re.compile(r"\b(?:no tests ran|collected\s+0\s+items)\b", re.IGNORECASE)
_PYTEST_PASSED_RE = re.compile(r"\b(\d+)\s+passed\b")


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
) -> tuple[GraderVerdict, str]:
    """Verify anti-cheat integrity, restore tampered files, and grade inside a fresh clean checkout."""
    reaped_pids = list(reaped_descendant_pids or [])
    unreaped_pids = list(unreaped_descendant_pids or [])
    anti_cheat_clean, tampered_files = handle.verify_and_restore_protected_tests()
    git_diff, added, removed = handle.capture_git_diff()
    bloat_ratio = round((added + removed) / float(max(1, task.max_diff_lines)), 3)

    if agent_timed_out:
        reasons = [f"AGENT TIMEOUT: agent execution exceeded {task.timeout_sec}s limit (超时判败)"]
        if not anti_cheat_clean:
            reasons.append(f"ANTI-CHEAT VIOLATION: forbidden/protected path(s) {', '.join(tampered_files)}")
        if reaped_pids:
            reasons.append(f"Reaped detached background process(es): {reaped_pids}")
        if unreaped_pids:
            reasons.append(f"ORPHAN PROCESS LEAK: unreaped background process(es) {unreaped_pids}")
        reasons.append(f"diff +{added}/-{removed} ({bloat_ratio:.2f}x budget)")
        return (
            GraderVerdict(
                passed=False,
                exit_code=124,
                anti_cheat_clean=anti_cheat_clean,
                tampered_test_files=tampered_files,
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
        and not unreaped_pids
        and not zero_tests_ran
        and exit_code == 0
        and sloplint_fails == 0
        and sloplint_warns == 0
    )

    reasons = []
    if not anti_cheat_clean:
        reasons.append(f"ANTI-CHEAT VIOLATION: modified protected/forbidden file(s) {', '.join(tampered_files)}")
    if unreaped_pids:
        reasons.append(f"ORPHAN PROCESS LEAK: unreaped background process(es) {unreaped_pids}")
    if reaped_pids:
        reasons.append(f"Reaped detached background process(es) {reaped_pids}")
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
