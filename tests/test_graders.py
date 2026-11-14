"""Scoped tests for K4 anti-cheat hardening, clean-checkout grading, stdlib shadowing, and 0-test detection."""

from __future__ import annotations

from pathlib import Path

import pytest

from skill_arena.graders.deterministic import evaluate_sandbox
from skill_arena.models import TaskSpec
from skill_arena.sandbox.worktree_pool import WorktreeSandboxPool
from skill_arena.suites.builtin_tasks import load_builtin_suite


def test_bypass_tests_init_exit_zero_is_blocked(tmp_path: Path) -> None:
    pool = WorktreeSandboxPool(tmp_path / "pool")
    task = load_builtin_suite()[0]
    handle = pool.acquire(task, "k4_tests_init")
    try:
        # Leave buggy rate_limiter.py (or write wrong answer) and drop tests/__init__.py -> os._exit(0)
        (handle.root / "rate_limiter.py").write_text(
            "class TokenBucket:\n    def __init__(self, *a, **kw): pass\n    def try_acquire(self, *a): return True\n",
            encoding="utf-8",
        )
        (handle.root / "tests" / "__init__.py").write_text("import os\nos._exit(0)\n", encoding="utf-8")

        verdict, _ = evaluate_sandbox(handle, task)
        assert not verdict.passed, "tests/__init__.py os._exit(0) bypass MUST fail!"
        assert not verdict.anti_cheat_clean
        assert "tests/__init__.py" in verdict.tampered_test_files
    finally:
        pool.release(handle)
        pool.close()


def test_bypass_root_unittest_shadowing_is_blocked(tmp_path: Path) -> None:
    pool = WorktreeSandboxPool(tmp_path / "pool")
    task = load_builtin_suite()[0]
    handle = pool.acquire(task, "k4_unittest_py")
    try:
        (handle.root / "rate_limiter.py").write_text(
            "class TokenBucket:\n    def __init__(self, *a, **kw): pass\n    def try_acquire(self, *a): return True\n",
            encoding="utf-8",
        )
        (handle.root / "unittest.py").write_text("import sys\nsys.exit(0)\n", encoding="utf-8")

        verdict, _ = evaluate_sandbox(handle, task)
        assert not verdict.passed, "Root unittest.py shadowing bypass MUST fail!"
        assert not verdict.anti_cheat_clean
        assert "unittest.py" in verdict.tampered_test_files
    finally:
        pool.release(handle)
        pool.close()


@pytest.mark.parametrize(
    "forbidden_rel_path",
    [
        "sitecustomize.py",
        "usercustomize.py",
        "pytest.py",
        "threading.py",
        "conftest.py",
        "tests/helper_extra.py",
        "subpkg/__init__.py",
        "evil.pth",
    ],
)
def test_forbidden_agent_files_are_rejected_and_not_copied_to_clean_grading_checkout(
    tmp_path: Path,
    forbidden_rel_path: str,
) -> None:
    pool = WorktreeSandboxPool(tmp_path / "pool")
    task = load_builtin_suite()[0]
    handle = pool.acquire(task, "k4_forbidden")
    try:
        # Even if agent ALSO writes reference solution, supplying a forbidden file is an anti-cheat violation!
        for rel, src in task.reference_solution_files.items():
            (handle.root / rel).write_text(src, encoding="utf-8")
        bad = handle.root / forbidden_rel_path
        bad.parent.mkdir(parents=True, exist_ok=True)
        bad.write_text("import os\nos._exit(0)\n", encoding="utf-8")

        verdict, _ = evaluate_sandbox(handle, task)
        assert not verdict.passed
        assert not verdict.anti_cheat_clean
        assert forbidden_rel_path in verdict.tampered_test_files
    finally:
        pool.release(handle)
        pool.close()


def test_zero_tests_executed_marks_trial_failed_for_unittest_and_pytest(tmp_path: Path) -> None:
    pool = WorktreeSandboxPool(tmp_path / "pool")
    try:
        # 1. unittest exits 0 on 'Ran 0 tests in 0.000s' when no TestCase exists!
        t_unit = TaskSpec(
            task_id="zero-unittest",
            title="Zero unittest check",
            category="refactor_test",
            prompt="Fix m.py",
            test_command="python3 -m unittest tests/test_empty.py",
            protected_test_paths=["tests/test_empty.py"],
            seed_files={
                "m.py": "def f(): return 2\n",
                "tests/test_empty.py": "from m import f\ndef helper_not_unittest(): assert f() == 1\n",
            },
        )
        h1 = pool.acquire(t_unit, "zero_u")
        try:
            v1, _ = evaluate_sandbox(h1, t_unit)
            assert not v1.passed, "0 executed unittest tests MUST fail!"
            assert "0 test" in v1.summary.lower()
        finally:
            pool.release(h1)

        # 2. pytest exits 5 when 0 items collected
        t_py = TaskSpec(
            task_id="zero-pytest",
            title="Zero pytest check",
            category="refactor_test",
            prompt="Fix m.py",
            test_command="python3 -m pytest tests/test_none.py",
            protected_test_paths=["tests/test_none.py"],
            seed_files={
                "m.py": "def f(): return 2\n",
                "tests/test_none.py": "X = 1\n",
            },
        )
        h2 = pool.acquire(t_py, "zero_p")
        try:
            v2, _ = evaluate_sandbox(h2, t_py)
            assert not v2.passed, "0 executed pytest tests MUST fail!"
        finally:
            pool.release(h2)
    finally:
        pool.close()


def test_protected_test_restoration_restores_original_bytes_and_cleans_diff(tmp_path: Path) -> None:
    pool = WorktreeSandboxPool(tmp_path / "pool")
    task = load_builtin_suite()[0]
    handle = pool.acquire(task, "k11_restore")
    try:
        test_path = handle.root / "tests" / "test_rate_limiter.py"
        original_bytes = test_path.read_bytes()

        # Agent writes passing implementation AND overwrites protected test file
        for rel, src in task.reference_solution_files.items():
            (handle.root / rel).write_text(src, encoding="utf-8")
        test_path.write_text("import unittest\nclass Fake(unittest.TestCase):\n    def test_x(self): pass\n", encoding="utf-8")

        verdict, git_diff = evaluate_sandbox(handle, task)
        assert not verdict.passed
        assert not verdict.anti_cheat_clean
        assert "tests/test_rate_limiter.py" in verdict.tampered_test_files
        assert test_path.read_bytes() == original_bytes, "Protected test file MUST be restored on disk!"
        assert "tests/test_rate_limiter.py" not in git_diff, "Restored test file must not appear in implementation diff!"
    finally:
        pool.release(handle)
        pool.close()
