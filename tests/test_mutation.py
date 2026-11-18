"""Mutation score of the tests an agent writes itself."""

from __future__ import annotations

import json
from pathlib import Path
import stat
import sys
import time

import pytest

from _pool_fixture import CALC_REFERENCE, make_pool
from skill_arena.cli import main
from skill_arena.graders.mutation import collect_agent_test_files, score_agent_tests
from skill_arena.models import TaskSpec
from skill_arena.sandbox.worktree_pool import WorktreeSandboxPool
from skill_arena.store.arena_db import ArenaDatabase
from skill_arena.suites.pool_import import load_pool_task

ADD_ONLY_TEST = (
    "import unittest\nfrom calc import add\n\n"
    "class Mine(unittest.TestCase):\n    def test_add(self):\n        self.assertEqual(add(2, 3), 5)\n"
)
FULL_TEST = (
    "import unittest\nfrom calc import add, clamp\n\n"
    "class Mine(unittest.TestCase):\n"
    "    def test_add(self):\n        self.assertEqual(add(2, 3), 5)\n"
    "    def test_clamp(self):\n        self.assertEqual(clamp(9, 0, 3), 3)\n"
    "    def test_raise(self):\n        with self.assertRaises(ValueError):\n            clamp(1, 3, 0)\n"
)
WRONG_TEST = (
    "import unittest\nfrom calc import add\n\n"
    "class Mine(unittest.TestCase):\n    def test_add(self):\n        self.assertEqual(add(2, 2), 5)\n"
)


def _score(tmp_path: Path, files: dict[str, str], *, variant: str = "hidden", task_edit=None, **kw):
    task = load_pool_task(make_pool(tmp_path / "taskpool") / "calc-01-basics", variant)
    if task_edit:
        task_edit(task)
    pool = WorktreeSandboxPool(tmp_path / "pool")
    handle = pool.acquire(task, "mut")
    try:
        (handle.root / "calc.py").write_text(CALC_REFERENCE, encoding="utf-8")
        for rel, src in files.items():
            dest = handle.root / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(src, encoding="utf-8")
        return score_agent_tests(handle, task, **kw), collect_agent_test_files(handle)
    finally:
        pool.release(handle)
        pool.close()


def test_partial_tests_kill_only_the_mutant_they_cover(tmp_path: Path) -> None:
    score, collected = _score(tmp_path, {"tests/test_mine.py": ADD_ONLY_TEST, "notes.txt": "x"})
    assert list(collected) == ["tests/test_mine.py"]
    assert score.files == ["tests/test_mine.py"]
    assert score.valid is True
    assert (score.mutants_killed, score.mutants_total) == (1, 3)


def test_thorough_tests_kill_every_mutant(tmp_path: Path) -> None:
    score, _ = _score(tmp_path, {"tests/unit/test_full.py": FULL_TEST}, variant="visible")
    assert score.valid is True
    assert (score.mutants_killed, score.mutants_total) == (3, 3)


def test_tests_that_fail_on_the_reference_are_invalid(tmp_path: Path) -> None:
    score, _ = _score(tmp_path, {"tests/test_wrong.py": WRONG_TEST})
    assert score.valid is False
    assert score.mutants_killed is None and score.mutants_total == 3
    assert "invalid" in score.note


def test_no_agent_tests_or_no_reference_is_skipped(tmp_path: Path) -> None:
    score, _ = _score(tmp_path / "a", {})
    assert score.files == [] and score.valid is None and score.mutants_total is None

    def _strip(task: TaskSpec) -> None:
        task.reference_solution_files = {}

    score2, _ = _score(tmp_path / "b", {"tests/test_mine.py": ADD_ONLY_TEST}, task_edit=_strip)
    assert score2.files == ["tests/test_mine.py"] and score2.valid is None
    assert "no reference" in score2.note

    def _no_mutants(task: TaskSpec) -> None:
        task.mutant_files = []

    score3, _ = _score(tmp_path / "c", {"tests/test_mine.py": ADD_ONLY_TEST}, task_edit=_no_mutants)
    assert score3.valid is True and score3.mutants_total is None


def test_hanging_agent_tests_are_cut_off(tmp_path: Path) -> None:
    hang = "import time, unittest\n\nclass H(unittest.TestCase):\n    def test_h(self):\n        time.sleep(60)\n"
    t0 = time.monotonic()
    score, _ = _score(tmp_path, {"tests/test_hang.py": hang}, timeout_sec=2)
    assert time.monotonic() - t0 < 20
    assert score.valid is False


def test_driver_stores_mutation_score_and_report_averages_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub = bin_dir / "claude"
    stub.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys\nfrom pathlib import Path\nsys.stdin.read()\n"
        f"Path('calc.py').write_text({CALC_REFERENCE!r})\n"
        "Path('tests').mkdir(exist_ok=True)\n"
        "arm = os.environ.get('SKILL_ARENA_CONTENDER', '')\n"
        f"src = {FULL_TEST!r} if arm != 'arm_control' else {ADD_ONLY_TEST!r}\n"
        "Path('tests/test_agent.py').write_text(src)\n"
        "print(json.dumps({'result': 'Done.\\nSTATUS: success', 'total_cost_usd': 0.001}))\n",
        encoding="utf-8",
    )
    stub.chmod(stub.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("RELAY_CLAUDE_BIN", str(stub))
    skill = tmp_path / "tdd" / "SKILL.md"
    skill.parent.mkdir()
    skill.write_text("# Write tests first\n", encoding="utf-8")
    task = load_pool_task(make_pool(tmp_path / "taskpool") / "calc-01-basics", "hidden")
    task.timeout_sec = 60
    suite = tmp_path / "suite.json"
    suite.write_text(json.dumps([task.to_dict()]), encoding="utf-8")
    db_path = tmp_path / "m.sqlite"
    rc = main(
        ["--lang", "en", "run", "--cli", "claude", "--model", "m", "--suite", str(suite), "--reps", "1",
         "--arm", f"tdd={skill}", "--db", str(db_path)]
    )
    out = capsys.readouterr().out
    assert rc == 0, out
    report = ArenaDatabase(db_path).build_comparison_report(ArenaDatabase(db_path).latest_experiment_id())
    by_arm = {t["contender_id"]: t["verdict"] for t in report["trials"]}
    assert by_arm["arm_control"]["agent_tests_files"] == ["tests/test_agent.py"]
    assert (by_arm["arm_control"]["mutants_killed"], by_arm["arm_control"]["mutants_total"]) == (1, 3)
    assert (by_arm["arm_tdd"]["mutants_killed"], by_arm["arm_tdd"]["mutants_total"]) == (3, 3)
    quality = {r["contender_id"]: r["agent_test_quality"] for r in report["leaderboard"]}
    assert quality["arm_control"]["mean_mutation_score"] == pytest.approx(1 / 3, abs=1e-3)
    assert quality["arm_tdd"]["mean_mutation_score"] == 1.0
    assert quality["arm_tdd"]["valid_rate"] == 1.0
    assert "Agent-written tests (mutation score)" in out and "mean mutation score 100.0%" in out
