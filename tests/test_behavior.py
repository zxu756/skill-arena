"""Behaviour metrics (test runs, test-first, skill loaded) from vibe's tool-call record."""

from __future__ import annotations

import json
from pathlib import Path
import stat
import sys

import pytest

from _pool_fixture import CALC_REFERENCE, make_pool
from skill_arena.cli import main
from skill_arena.models import ContenderConfig
from skill_arena.runners.harness_driver import HarnessDriver
from skill_arena.runners.trajectory import behavior_from_tool_calls, load_vibe_tool_calls
from skill_arena.sandbox.worktree_pool import WorktreeSandboxPool
from skill_arena.store.arena_db import ArenaDatabase
from skill_arena.suites.pool_import import load_pool_task


def _effect(tool: str, **inp) -> dict:
    return {"type": "effect", "title": tool, "detail": {"toolName": tool, "input": inp}, "state": {"status": "completed"}}


def test_behavior_from_tool_calls(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    calls_path = tmp_path / "messages.json"
    msgs = [
        {"type": "message", "role": "user", "content": "go"},
        _effect("file_system.read_file", filePath=str(ws / ".agents/skills/tdd/SKILL.md")),
        _effect("file_system.write_file", filePath=str(ws / "AGENTS.md"), content="x"),
        _effect("file_system.write_file", filePath=str(ws / "tests/test_calc_new.py"), content="x"),
        _effect("file_system.bash", command="cd /w && python3 -m unittest tests/test_calc_new.py"),
        _effect("file_system.edit", filePath=str(ws / "calc.py"), oldString="a", newString="b"),
        _effect("file_system.bash", command="python3 -m pytest -q"),
        _effect("file_system.bash", command="ls -la"),
        {**_effect("file_system.bash", command="pytest"), "state": {"error": {"code": "tool_denied"}}},
    ]
    calls_path.write_text(json.dumps(msgs), encoding="utf-8")
    calls = load_vibe_tool_calls(calls_path)
    assert calls is not None and len(calls) == 8
    b = behavior_from_tool_calls(calls, ws, skill_names=["tdd"], test_command="python3 -m unittest tests/test_calc.py")
    assert b["test_runs"] == 2
    assert b["test_first"] is True
    assert b["skill_loaded"] is True

    impl_first = [
        _effect("file_system.write_file", filePath=str(ws / "calc.py")),
        _effect("file_system.write_file", filePath=str(ws / "tests/test_x.py")),
        _effect("file_system.bash", command="cat .agents/skills/other/SKILL.md"),
    ]
    calls_path.write_text(json.dumps(impl_first), encoding="utf-8")
    b2 = behavior_from_tool_calls(load_vibe_tool_calls(calls_path) or [], ws, skill_names=["tdd"])
    assert b2["test_first"] is False and b2["skill_loaded"] is False and b2["test_runs"] == 0
    b3 = behavior_from_tool_calls([], ws, skill_names=[])
    assert b3["test_first"] is None and b3["skill_loaded"] is None and b3["test_runs"] == 0
    assert load_vibe_tool_calls(tmp_path / "missing.json") is None


def _stub_vibe(bin_dir: Path, *, write_tests_first: bool, read_skill: bool) -> Path:
    body = f"""
import json, os, sys
from pathlib import Path
args = sys.argv[1:]
def opt(name):
    return args[args.index(name) + 1] if name in args else None
workdir = Path(opt('--workdir'))
outdir = Path(opt('--output-dir'))
outdir.mkdir(parents=True, exist_ok=True)
Path(opt('--prompt-file')).read_text()
msgs = [{{'type': 'message', 'role': 'user', 'content': 'task'}}]
def eff(tool, **inp):
    msgs.append({{'type': 'effect', 'title': tool, 'detail': {{'toolName': tool, 'input': inp}}, 'state': {{'status': 'completed'}}}})
if {read_skill!r}:
    skill = workdir / '.agents' / 'skills' / 'tdd' / 'SKILL.md'
    skill.read_text()
    eff('file_system.read_file', filePath=str(skill))
test_src = 'import unittest\\nfrom calc import add\\nclass T(unittest.TestCase):\\n    def test_a(self):\\n        self.assertEqual(add(1, 1), 2)\\n'
def write_test():
    (workdir / 'tests').mkdir(exist_ok=True)
    (workdir / 'tests' / 'test_mine.py').write_text(test_src)
    eff('file_system.write_file', filePath=str(workdir / 'tests' / 'test_mine.py'), content=test_src)
def write_impl():
    (workdir / 'calc.py').write_text({CALC_REFERENCE!r})
    eff('file_system.write_file', filePath=str(workdir / 'calc.py'), content='...')
if {write_tests_first!r}:
    write_test(); eff('file_system.bash', command='python3 -m unittest tests/test_mine.py'); write_impl()
else:
    write_impl(); write_test()
eff('file_system.bash', command='python3 -m unittest tests/test_mine.py')
msgs.append({{'type': 'message', 'role': 'assistant', 'content': 'Done.\\nSTATUS: success'}})
(outdir / 'export.json').write_text(json.dumps({{'outcome': 'finished', 'cost_usd': 0.002, 'config': {{'active_model': 'stub'}}, 'usage': {{'total_tokens': 10}}}}))
print(json.dumps(msgs))
"""
    path = bin_dir / "vibe"
    path.write_text(f"#!{sys.executable}\n{body}", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def _task(tmp_path: Path):
    task = load_pool_task(make_pool(tmp_path / "taskpool") / "calc-01-basics", "hidden")
    task.timeout_sec = 120
    return task


def test_vibe_run_through_relay_records_behavior(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    monkeypatch.setenv("RELAY_VIBE_BIN", str(_stub_vibe(bin_dir, write_tests_first=True, read_skill=True)))
    skill = tmp_path / "tdd" / "SKILL.md"
    skill.parent.mkdir()
    skill.write_text("# tdd\nWrite a failing test first.\n", encoding="utf-8")
    pool = WorktreeSandboxPool(tmp_path / "pool")
    driver = HarnessDriver(pool)
    contender = ContenderConfig(
        contender_id="arm_tdd", label="vibe + tdd", role="treatment", cli="vibe", model="m", skill_paths=[str(skill)]
    )
    try:
        result = driver.run_trial("exp", _task(tmp_path), contender, rep_index=1)
    finally:
        pool.close()
    assert result.passed, result.verdict.summary
    assert result.behavior["test_runs"] == 2
    assert result.behavior["test_first"] is True
    assert result.behavior["skill_loaded"] is True
    assert result.behavior["source"] == "vibe messages.json"
    assert result.usage.relay_cost_usd == pytest.approx(0.002)
    summaries = [s.summary for s in result.steps]
    assert any(s.startswith("read_file:") for s in summaries)
    assert any(s.startswith("bash: python3 -m unittest") for s in summaries)
    # The agent's own test was also scored.
    assert result.verdict.agent_tests_files == ["tests/test_mine.py"] and result.verdict.agent_tests_valid is True


def test_behavior_is_none_for_clis_without_tool_calls_and_report_summarizes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    monkeypatch.setenv("RELAY_VIBE_BIN", str(_stub_vibe(bin_dir, write_tests_first=False, read_skill=False)))
    suite = tmp_path / "suite.json"
    suite.write_text(json.dumps([_task(tmp_path).to_dict()]), encoding="utf-8")
    db_path = tmp_path / "b.sqlite"
    assert main(["--lang", "en", "run", "--cli", "vibe", "--model", "m", "--suite", str(suite), "--reps", "1",
                 "--db", str(db_path)]) == 0
    out = capsys.readouterr().out
    report = ArenaDatabase(db_path).build_comparison_report(ArenaDatabase(db_path).latest_experiment_id())
    for row in report["leaderboard"]:
        b = row["behavior_summary"]
        assert b["trials_observed"] == 1 and b["mean_test_runs"] == 1.0 and b["test_first_rate"] == 0.0
        assert b["skill_loaded_rate"] is None  # no skill mounted in either arm here
    assert "Behaviour (from the agent's tool calls)" in out and "mean test runs 1.0, test first 0%" in out

    # A simulated run cannot observe behaviour: metrics stay None and say why.
    sim_db = tmp_path / "sim.sqlite"
    assert main(["--lang", "en", "run", "--cli", "simulate", "--suite", "concurrency", "--reps", "1", "--db", str(sim_db)]) == 0
    sim_out = capsys.readouterr().out
    sim = ArenaDatabase(sim_db).build_comparison_report(ArenaDatabase(sim_db).latest_experiment_id())
    assert all(t["behavior"]["test_runs"] is None for t in sim["trials"])
    assert all(r["behavior_summary"]["mean_test_runs"] is None for r in sim["leaderboard"])
    assert "not observable (unavailable: simulated trial)" in sim_out


def test_vibe_builtin_skill_tool_counts_as_skill_loaded(tmp_path: Path) -> None:
    """vibe loads skills with its own `skill` tool (input {"name": ...}), not by reading SKILL.md."""
    ws = tmp_path / "ws"
    ws.mkdir()
    calls_path = tmp_path / "messages.json"
    calls_path.write_text(json.dumps([_effect("skill", name="test-driven-development")]), encoding="utf-8")
    calls = load_vibe_tool_calls(calls_path)
    assert calls is not None
    got = behavior_from_tool_calls(calls, ws, skill_names=["test-driven-development"])
    assert got["skill_loaded"] is True
    other = behavior_from_tool_calls(calls, ws, skill_names=["careful-implementation"])
    assert other["skill_loaded"] is False
