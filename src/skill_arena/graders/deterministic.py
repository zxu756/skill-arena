"""Zero-LLM deterministic grader combining anti-cheat integrity, unit tests, diff bloat, and craft checks."""

from __future__ import annotations

import shlex

from skill_arena.graders.frontend_craft import audit_html_craft
from skill_arena.models import GraderVerdict, TaskSpec
from skill_arena.sandbox.worktree_pool import SandboxHandle, WorktreeSandboxPool


def evaluate_sandbox(handle: SandboxHandle, task: TaskSpec) -> tuple[GraderVerdict, str]:
    """Verify protected test integrity, capture git diff, run test suite, and run craft checks."""
    anti_cheat_clean, tampered_files = handle.verify_and_restore_protected_tests()
    git_diff, added, removed = handle.capture_git_diff()
    bloat_ratio = round((added + removed) / float(max(1, task.max_diff_lines)), 3)

    argv = shlex.split(task.test_command)
    exit_code, stdout, stderr, timed_out = WorktreeSandboxPool.run_bounded_command(
        argv,
        cwd=handle.root,
        timeout_sec=min(60, task.timeout_sec),
    )
    combined_out = ((stdout or "") + ("\n" if stdout and stderr else "") + (stderr or "")).strip()
    tail = combined_out[-1200:] if len(combined_out) > 1200 else combined_out

    sloplint_fails = 0
    sloplint_warns = 0
    craft_notes: list[str] = []
    if task.frontend_target_html:
        craft = audit_html_craft(handle.root / task.frontend_target_html)
        sloplint_fails = craft.fails
        sloplint_warns = craft.warns
        craft_notes = craft.findings

    passed = (
        anti_cheat_clean
        and not timed_out
        and exit_code == 0
        and sloplint_fails == 0
        and sloplint_warns == 0
    )

    reasons: list[str] = []
    if not anti_cheat_clean:
        reasons.append(f"ANTI-CHEAT VIOLATION: modified protected test(s) {', '.join(tampered_files)}")
    if timed_out:
        reasons.append("Test suite timed out")
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
            exit_code=exit_code,
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
