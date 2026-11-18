"""Time-limit curve: Pass@T per arm from each trial's agent wall time."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from skill_arena.cli import main
from skill_arena.graders.frontend_craft import audit_html_craft
from skill_arena.models import ContenderConfig, GraderVerdict, TokenUsageLedger, TrialResult
from skill_arena.store.arena_db import ArenaDatabase, compute_pass_at_time


def _t(task: str, passed: bool, secs: float | None, timeout: int | None = 900, rep: int = 1, arm: str = "arm_control") -> TrialResult:
    return TrialResult(
        trial_id=f"tr_{arm}_{task}_{rep}_{secs}",
        experiment_id="exp",
        task_id=task,
        task_title=task,
        task_category="parse",
        contender_id=arm,
        contender_label=arm,
        contender_role="control" if arm == "arm_control" else "treatment",
        rep_index=rep,
        passed=passed,
        anti_cheat_clean=True,
        usage=TokenUsageLedger(total_latency_ms=123_000),
        verdict=GraderVerdict(passed=passed, exit_code=0 if passed else 1, anti_cheat_clean=True),
        steps=[],
        git_diff="",
        started_at="",
        finished_at="",
        agent_wall_ms=None if secs is None else int(secs * 1000),
        task_timeout_sec=timeout,
    )


def test_pass_at_time_counts_only_passes_that_finished_in_time() -> None:
    trials = [
        _t("a", True, 30, rep=1),
        _t("a", True, 150, rep=2),
        _t("a", True, 400, rep=3),
        _t("a", False, 899, rep=4),
        _t("a", False, 20, rep=5),  # fast but wrong: never counts
    ]
    curve, limit = compute_pass_at_time(trials)
    assert limit == 900
    assert [p["t_sec"] for p in curve] == [60, 120, 180, 300, 600, 900]
    assert [p["pass_rate"] for p in curve] == [0.2, 0.2, 0.4, 0.4, 0.6, 0.6]
    assert [p["passed_trials"] for p in curve] == [1, 1, 2, 2, 3, 3]
    assert [p["is_full_limit"] for p in curve] == [False] * 5 + [True]


def test_pass_at_time_is_task_averaged_and_drops_points_beyond_the_limit() -> None:
    trials = [
        _t("a", True, 50, timeout=200),
        _t("b", True, 50, timeout=200, rep=1),
        _t("b", True, 190, timeout=200, rep=2),
    ]
    curve, limit = compute_pass_at_time(trials)
    assert limit == 200
    assert [p["t_sec"] for p in curve] == [60, 120, 180, 200]
    # task a: 1/1, task b: 1/2 at 60 s -> (1 + 0.5) / 2
    assert curve[0]["pass_rate"] == pytest.approx(0.75)
    assert curve[-1]["pass_rate"] == pytest.approx(1.0)


def test_legacy_rows_fall_back_to_latency_and_unknown_limit() -> None:
    curve, limit = compute_pass_at_time([_t("a", True, None, timeout=None)])
    assert limit is None
    assert curve[-1]["t_sec"] is None and curve[-1]["is_full_limit"] is True
    # 123 s recorded latency: not within 120, within 180.
    by_t = {p["t_sec"]: p["pass_rate"] for p in curve}
    assert by_t[120] == 0.0 and by_t[180] == 1.0


def test_curve_in_report_cli_and_html(tmp_path: Path, capsys) -> None:
    db = ArenaDatabase(tmp_path / "a.sqlite")
    ctrl = ContenderConfig(contender_id="arm_control", label="ctrl", role="control", cli="claude", model="m")
    treat = ContenderConfig(contender_id="arm_treatment", label="treat", role="treatment", cli="claude", model="m")
    db.create_experiment("exp", "T", "s", 1, [ctrl, treat])
    db.record_trial(_t("a", True, 45))
    db.record_trial(_t("a", True, 500, arm="arm_treatment"))
    report = db.build_comparison_report("exp")
    curves = {r["contender_id"]: r["pass_at_time"] for r in report["leaderboard"]}
    assert curves["arm_control"][0] == {"t_sec": 60, "pass_rate": 1.0, "passed_trials": 1, "is_full_limit": False}
    assert curves["arm_treatment"][0]["pass_rate"] == 0.0 and curves["arm_treatment"][-1]["pass_rate"] == 1.0
    assert report["trials"][0]["task_timeout_sec"] == 900

    assert main(["--lang", "en", "compare", "--db", str(tmp_path / "a.sqlite")]) == 0
    out = capsys.readouterr().out
    assert "Time-limit curve Pass@T" in out
    assert "60s 0.0%" in out and "900s (limit) 100.0%" in out

    from skill_arena.studio.templates import render_arena_html

    page = tmp_path / "p.html"
    page.write_text(render_arena_html(report, lang="en"), encoding="utf-8")
    text = page.read_text(encoding="utf-8")
    assert "Pass@60s" in text and "Pass@900s (limit)" in text
    assert audit_html_craft(page).passed, audit_html_craft(page).findings


def test_simulated_trials_record_wall_time_and_limit(tmp_path: Path, capsys) -> None:
    db_path = tmp_path / "s.sqlite"
    assert main(["run", "--cli", "simulate", "--suite", "concurrency", "--reps", "1", "--db", str(db_path)]) == 0
    capsys.readouterr()
    assert main(["compare", "--db", str(db_path), "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert all(t["agent_wall_ms"] and t["task_timeout_sec"] == 900 for t in report["trials"])
    assert all(r["pass_at_time"][-1]["t_sec"] == 900 for r in report["leaderboard"])
