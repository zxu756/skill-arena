"""Randomized blocked trial order, parallel workers and budget accounting under concurrency."""

from __future__ import annotations

from collections import Counter
import json
from pathlib import Path
import stat
import sys

import pytest

from _pool_fixture import CALC_REFERENCE, make_pool
from skill_arena.cli import DEFAULT_SCHEDULE_SEED, EXIT_BUDGET_EXCEEDED, build_trial_schedule, main
from skill_arena.models import ContenderConfig
from skill_arena.store.arena_db import ArenaDatabase
from skill_arena.suites.builtin_tasks import load_builtin_suite
from skill_arena.suites.pool_import import load_pool_task


def _contenders(n_treatments: int = 2) -> list[ContenderConfig]:
    out = [ContenderConfig(contender_id="arm_control", label="c", role="control", cli="simulate:baseline")]
    for i in range(n_treatments):
        out.append(ContenderConfig(contender_id=f"arm_t{i}", label=f"t{i}", role="treatment", cli="simulate:skilled"))
    return out


def test_schedule_is_blocked_shuffled_and_reproducible() -> None:
    tasks = load_builtin_suite()
    arms = _contenders(2)
    sched = build_trial_schedule(tasks, arms, 3, seed=11)
    assert len(sched) == len(tasks) * 3 * len(arms)
    assert Counter((t.task_id, c.contender_id, r) for t, c, r in sched) == Counter(
        (t.task_id, c.contender_id, r) for t in tasks for c in arms for r in (1, 2, 3)
    )
    # Every consecutive group of len(arms) trials is one (task, rep) block holding each arm once.
    blocks = [sched[i : i + len(arms)] for i in range(0, len(sched), len(arms))]
    for block in blocks:
        assert len({(t.task_id, r) for t, _c, r in block}) == 1
        assert {c.contender_id for _t, c, _r in block} == {c.contender_id for c in arms}
    # Arm order inside blocks and block order are both shuffled.
    first_arms = {block[0][1].contender_id for block in blocks}
    assert len(first_arms) == len(arms)
    natural = [(t.task_id, r) for t in tasks for r in (1, 2, 3)]
    assert [(b[0][0].task_id, b[0][2]) for b in blocks] != natural
    # Same seed, same order; another seed, another order.
    key = lambda s: [(t.task_id, c.contender_id, r) for t, c, r in s]  # noqa: E731
    assert key(build_trial_schedule(tasks, arms, 3, seed=11)) == key(sched)
    assert key(build_trial_schedule(tasks, arms, 3, seed=12)) != key(sched)
    assert key(build_trial_schedule(tasks, arms, 3)) == key(build_trial_schedule(tasks, arms, 3, seed=DEFAULT_SCHEDULE_SEED))


def test_simulated_run_records_launch_order_matching_the_schedule(tmp_path: Path, capsys) -> None:
    db_path = tmp_path / "s.sqlite"
    rc = main(
        ["--lang", "en", "run", "--cli", "simulate", "--suite", "concurrency", "--reps", "2", "--seed", "5",
         "--workers", "3", "--db", str(db_path)]
    )
    assert rc == 0
    out = capsys.readouterr().out
    report = ArenaDatabase(db_path).build_comparison_report(ArenaDatabase(db_path).latest_experiment_id())
    trials = report["trials"]
    assert sorted(t["launch_index"] for t in trials) == list(range(len(trials)))
    assert report["design"] == {"seed": 5, "workers": 3, "schedule": "blocked-randomized"}
    tasks = load_builtin_suite(category="concurrency")
    contenders = [ContenderConfig.from_dict(c) for c in report["contenders"]]
    expected = [(t.task_id, c.contender_id, r) for t, c, r in build_trial_schedule(tasks, contenders, 2, seed=5)]
    by_index = sorted(trials, key=lambda t: t["launch_index"])
    assert [(t["task_id"], t["contender_id"], t["rep_index"]) for t in by_index] == expected
    assert all(t["started_at"] and t["finished_at"] and t["started_at"] <= t["finished_at"] for t in trials)
    # One console line per trial, each tagged with its launch number.
    trial_lines = [ln for ln in out.splitlines() if ln.startswith("[SIMULATED] [PASS]") or ln.startswith("[SIMULATED] [FAIL]")]
    assert len(trial_lines) == len(trials)
    assert all(" #" in ln for ln in trial_lines)


def _stub_claude(tmp_path: Path, *, cost: float, sleep_sec: float) -> tuple[Path, Path]:
    events = tmp_path / "events.jsonl"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub = bin_dir / "claude"
    stub.write_text(
        f"#!{sys.executable}\n"
        "import json, sys, time\nfrom pathlib import Path\n"
        "sys.stdin.read()\n"
        f"with open({str(events)!r}, 'a') as fh:\n    fh.write(json.dumps({{'start': time.time()}}) + '\\n')\n"
        f"time.sleep({sleep_sec})\n"
        f"Path('calc.py').write_text({CALC_REFERENCE!r})\n"
        f"with open({str(events)!r}, 'a') as fh:\n    fh.write(json.dumps({{'end': time.time()}}) + '\\n')\n"
        f"print(json.dumps({{'result': 'Done.\\nSTATUS: success', 'total_cost_usd': {cost}}}))\n",
        encoding="utf-8",
    )
    stub.chmod(stub.stat().st_mode | stat.S_IXUSR)
    return stub, events


def _suite(tmp_path: Path) -> Path:
    task = load_pool_task(make_pool(tmp_path / "pool") / "calc-01-basics", "hidden")
    task.timeout_sec = 60
    suite = tmp_path / "suite.json"
    suite.write_text(json.dumps([task.to_dict()]), encoding="utf-8")
    return suite


def _max_concurrency(events: Path) -> int:
    points = []
    for line in events.read_text(encoding="utf-8").splitlines():
        ev = json.loads(line)
        points.append((ev["start"], 1) if "start" in ev else (ev["end"], -1))
    cur = best = 0
    for _ts, delta in sorted(points):
        cur += delta
        best = max(best, cur)
    return best


def test_workers_run_trials_concurrently(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    stub, events = _stub_claude(tmp_path, cost=0.001, sleep_sec=1.5)
    monkeypatch.setenv("RELAY_CLAUDE_BIN", str(stub))
    db_path = tmp_path / "w.sqlite"
    rc = main(
        ["--lang", "en", "run", "--cli", "claude", "--model", "m", "--suite", str(_suite(tmp_path)), "--reps", "2",
         "--workers", "4", "--db", str(db_path)]
    )
    assert rc == 0, capsys.readouterr().err
    assert _max_concurrency(events) >= 2
    report = ArenaDatabase(db_path).build_comparison_report(ArenaDatabase(db_path).latest_experiment_id())
    assert len(report["trials"]) == 4 and all(t["passed"] for t in report["trials"])
    assert report["completed_trials"] == 4


def test_budget_holds_with_parallel_workers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    stub, events = _stub_claude(tmp_path, cost=0.01, sleep_sec=0.2)
    monkeypatch.setenv("RELAY_CLAUDE_BIN", str(stub))
    db_path = tmp_path / "b.sqlite"
    rc = main(
        ["--lang", "en", "run", "--cli", "claude", "--model", "m", "--suite", str(_suite(tmp_path)), "--reps", "5",
         "--workers", "2", "--budget-usd", "0.035", "--db", str(db_path)]
    )
    assert rc == EXIT_BUDGET_EXCEEDED
    assert "BUDGET STOPPED" in capsys.readouterr().out
    report = ArenaDatabase(db_path).build_comparison_report(ArenaDatabase(db_path).latest_experiment_id())
    launched = sum(1 for line in events.read_text(encoding="utf-8").splitlines() if "start" in line)
    assert launched == len(report["trials"]) == 3
    assert report["cumulative_cost_usd"] == pytest.approx(0.03)
    assert report["cumulative_cost_usd"] <= 0.035
    assert report["planned_trials"] == 10 and report["completed_trials"] == 3
