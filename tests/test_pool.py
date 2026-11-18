"""Task pool import (pool-export) and hidden-test grading."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from _pool_fixture import CALC_REFERENCE, make_pool
from skill_arena.cli import main
from skill_arena.graders.deterministic import evaluate_sandbox
from skill_arena.models import TaskSpec
from skill_arena.sandbox.worktree_pool import WorktreeSandboxPool
from skill_arena.suites.pool_import import HIDDEN_TESTS_NOTE, load_pool_task


def test_pool_export_writes_visible_and_hidden_taskspecs(tmp_path: Path, capsys) -> None:
    pool = make_pool(tmp_path / "pool")
    out = tmp_path / "suite.json"
    rc = main(
        [
            "--lang", "en", "pool-export", "--pool", str(pool), "--variant", "visible", "--variant", "hidden",
            "--timeout-sec", "600", "--out", str(out),
        ]
    )
    assert rc == 0
    assert "Exported 2 TaskSpecs" in capsys.readouterr().out
    specs = [TaskSpec.from_dict(d) for d in json.loads(out.read_text(encoding="utf-8"))]
    by_id = {s.task_id: s for s in specs}
    assert set(by_id) == {"calc-01-basics@visible", "calc-01-basics@hidden"}

    vis = by_id["calc-01-basics@visible"]
    assert vis.variant == "visible"
    assert set(vis.seed_files) == {"calc.py", "README.txt", "tests/test_calc.py", "tests/calc_cases.py"}
    assert vis.protected_test_paths == ["tests/calc_cases.py", "tests/test_calc.py"]
    assert vis.hidden_test_files == {}
    assert "python3 -m unittest tests/test_calc.py" in vis.prompt
    assert HIDDEN_TESTS_NOTE not in vis.prompt

    hid = by_id["calc-01-basics@hidden"]
    assert hid.variant == "hidden"
    assert set(hid.seed_files) == {"calc.py", "README.txt"}
    assert hid.protected_test_paths == []
    assert set(hid.hidden_test_files) == {"tests/test_calc.py", "tests/calc_cases.py"}
    assert hid.prompt.rstrip().endswith(HIDDEN_TESTS_NOTE)
    assert "tests/" not in hid.prompt

    for spec in specs:
        assert spec.reference_solution_files == {"calc.py": CALC_REFERENCE}
        assert len(spec.mutant_files) == 3 and all(set(m) == {"calc.py"} for m in spec.mutant_files)
        assert spec.timeout_sec == 600
        assert spec.max_diff_lines == 40
        assert spec.category == "parse"


def test_pool_export_task_filter_and_unknown_task(tmp_path: Path, capsys) -> None:
    pool = make_pool(tmp_path / "pool")
    out = tmp_path / "suite.json"
    assert main(["pool-export", "--pool", str(pool), "--variant", "both", "--task", "calc-01-basics@hidden", "--out", str(out)]) == 0
    assert len(json.loads(out.read_text(encoding="utf-8"))) == 2
    assert main(["pool-export", "--pool", str(pool), "--variant", "hidden", "--task", "nope-01", "--out", str(out)]) == 2
    assert "nope-01" in capsys.readouterr().err


def test_taskspec_from_dict_keeps_legacy_payloads_working() -> None:
    legacy = {
        "task_id": "old",
        "title": "Old task",
        "prompt": "do it",
        "test_command": "python3 -m unittest",
        "protected_test_paths": ["tests/test_a.py"],
    }
    spec = TaskSpec.from_dict(legacy)
    assert spec.variant == "" and spec.hidden_test_files == {} and spec.mutant_files == []
    assert TaskSpec.from_dict(spec.to_dict()) == spec


def _hidden_task(tmp_path: Path) -> TaskSpec:
    return load_pool_task(make_pool(tmp_path / "pool") / "calc-01-basics", "hidden")


def test_hidden_tests_never_reach_the_workspace_but_grade_the_solution(tmp_path: Path) -> None:
    task = _hidden_task(tmp_path)
    sandbox = WorktreeSandboxPool(tmp_path / "sandboxes")
    handle = sandbox.acquire(task, "hidden_ok")
    try:
        assert not (handle.root / "tests").exists()
        (handle.root / "calc.py").write_text(CALC_REFERENCE, encoding="utf-8")
        verdict, _ = evaluate_sandbox(handle, task)
        assert verdict.passed, verdict.summary + verdict.stdout_tail
        assert "Ran 3 tests" in verdict.stdout_tail
    finally:
        sandbox.release(handle)
        sandbox.close()


def test_agent_file_at_hidden_test_path_is_overwritten_not_tampering(tmp_path: Path) -> None:
    task = _hidden_task(tmp_path)
    sandbox = WorktreeSandboxPool(tmp_path / "sandboxes")
    handle = sandbox.acquire(task, "hidden_overwrite")
    try:
        # A wrong solution plus agent files at both hidden paths that would make everything pass.
        (handle.root / "calc.py").write_text(CALC_REFERENCE.replace("a + b", "a * b"), encoding="utf-8")
        (handle.root / "tests").mkdir()
        (handle.root / "tests" / "calc_cases.py").write_text("ADD_CASES = [(2, 2, 4)]\n", encoding="utf-8")
        (handle.root / "tests" / "test_calc.py").write_text(
            "import unittest\nclass T(unittest.TestCase):\n    def test_ok(self):\n        pass\n", encoding="utf-8"
        )
        verdict, _ = evaluate_sandbox(handle, task)
        assert verdict.anti_cheat_clean, verdict.tampered_test_files
        assert not verdict.passed
        assert "test_add" in verdict.stdout_tail
    finally:
        sandbox.release(handle)
        sandbox.close()


def test_visible_variant_still_flags_test_tampering(tmp_path: Path) -> None:
    task = load_pool_task(make_pool(tmp_path / "pool") / "calc-01-basics", "visible")
    sandbox = WorktreeSandboxPool(tmp_path / "sandboxes")
    handle = sandbox.acquire(task, "visible_tamper")
    try:
        assert (handle.root / "tests" / "test_calc.py").is_file()
        (handle.root / "tests" / "calc_cases.py").write_text("ADD_CASES = []\n", encoding="utf-8")
        verdict, _ = evaluate_sandbox(handle, task)
        assert not verdict.anti_cheat_clean
        assert "tests/calc_cases.py" in verdict.tampered_test_files
    finally:
        sandbox.release(handle)
        sandbox.close()


@pytest.mark.parametrize("variant", ["visible", "hidden"])
def test_start_tree_fails_both_variants(tmp_path: Path, variant: str) -> None:
    task = load_pool_task(make_pool(tmp_path / "pool") / "calc-01-basics", variant)
    sandbox = WorktreeSandboxPool(tmp_path / "sandboxes")
    handle = sandbox.acquire(task, f"start_{variant}")
    try:
        verdict, _ = evaluate_sandbox(handle, task)
        assert not verdict.passed and verdict.anti_cheat_clean
    finally:
        sandbox.release(handle)
        sandbox.close()
