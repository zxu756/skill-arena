"""Per-category (and per-variant) task-paired deltas."""

from __future__ import annotations

from pathlib import Path

import pytest

from skill_arena.cli import main
from skill_arena.graders.frontend_craft import audit_html_craft
from skill_arena.models import ContenderConfig, GraderVerdict, TokenUsageLedger, TrialResult
from skill_arena.store.arena_db import ArenaDatabase, compute_paired_task_comparison
from skill_arena.studio.templates import render_arena_html


def _t(task: str, cat: str, c: ContenderConfig, rep: int, passed: bool) -> TrialResult:
    return TrialResult(
        trial_id=f"tr_{c.contender_id}_{task}_{rep}",
        experiment_id="exp",
        task_id=task,
        task_title=task,
        task_category=cat,
        contender_id=c.contender_id,
        contender_label=c.label,
        contender_role=c.role,
        rep_index=rep,
        passed=passed,
        anti_cheat_clean=True,
        usage=TokenUsageLedger(),
        verdict=GraderVerdict(passed=passed, exit_code=0 if passed else 1, anti_cheat_clean=True),
        steps=[],
        git_diff="",
        started_at="",
        finished_at="",
    )


def _report(tmp_path: Path) -> dict:
    db = ArenaDatabase(tmp_path / "c.sqlite")
    ctrl = ContenderConfig(contender_id="arm_control", label="ctrl", role="control", cli="claude", model="m")
    treat = ContenderConfig(contender_id="arm_tdd", label="tdd", role="treatment", cli="claude", model="m")
    db.create_experiment("exp", "T", "s", 2, [ctrl, treat])
    plan = {
        # task: (category, control passes per rep, treatment passes per rep)
        "parse-01@visible": ("parse", (False, False), (True, True)),
        "parse-02@visible": ("parse", (True, False), (True, True)),
        "parse-01@hidden": ("parse", (False, False), (False, True)),
        "state-01@hidden": ("state", (True, True), (False, False)),
    }
    for task, (cat, c_out, t_out) in plan.items():
        for rep in (1, 2):
            db.record_trial(_t(task, cat, ctrl, rep, c_out[rep - 1]))
            db.record_trial(_t(task, cat, treat, rep, t_out[rep - 1]))
    return db.build_comparison_report("exp")


def test_breakdown_by_category_and_variant(tmp_path: Path) -> None:
    report = _report(tmp_path)
    groups = {(g["by"], g["group"]): g for g in report["category_breakdown"]}
    assert set(groups) == {("category", "parse"), ("category", "state"), ("variant", "hidden"), ("variant", "visible")}
    parse = groups[("category", "parse")]
    assert parse["tasks_paired"] == 3
    # per-task deltas 1.0, 0.5, 0.5 -> mean 2/3
    assert parse["delta_pass_rate"] == pytest.approx(0.6667, abs=1e-4)
    expected = compute_paired_task_comparison(
        control_id="arm_control", treatment_id="arm_tdd", per_task=[(0, 2, 2, 2), (1, 2, 2, 2), (0, 2, 1, 2)]
    )
    assert (parse["ci_low"], parse["ci_high"]) == (expected["ci_low"], expected["ci_high"])
    state = groups[("category", "state")]
    assert state["tasks_paired"] == 1 and state["delta_pass_rate"] == -1.0
    assert state["ci_low"] is None and state["ci_high"] is None
    assert groups[("variant", "visible")]["delta_pass_rate"] == pytest.approx(0.75)
    assert groups[("variant", "hidden")]["tasks_paired"] == 2
    assert all(g["treatment_id"] == "arm_tdd" for g in report["category_breakdown"])


def test_breakdown_in_cli_and_html(tmp_path: Path, capsys) -> None:
    report = _report(tmp_path)
    assert main(["--lang", "en", "compare", "--db", str(tmp_path / "c.sqlite")]) == 0
    out = capsys.readouterr().out
    assert "Per-category delta (task-paired)" in out
    assert "category=state: -100.0 pp CI n/a (< 2 tasks)" in out
    page = tmp_path / "c.html"
    page.write_text(render_arena_html(report, lang="en"), encoding="utf-8")
    text = page.read_text(encoding="utf-8")
    assert "Per-category delta (task-paired)" in text and "variant: hidden" in text
    assert audit_html_craft(page).passed, audit_html_craft(page).findings
    zh = tmp_path / "c_zh.html"
    zh.write_text(render_arena_html(report, lang="zh"), encoding="utf-8")
    assert "分类别差值（按题配对）" in zh.read_text(encoding="utf-8")
    assert audit_html_craft(zh).passed, audit_html_craft(zh).findings


def test_breakdown_skips_variant_grouping_for_plain_task_ids(tmp_path: Path, capsys) -> None:
    db_path = tmp_path / "s.sqlite"
    assert main(["--lang", "en", "run", "--cli", "simulate", "--suite", "builtin-8", "--reps", "1", "--db", str(db_path)]) == 0
    capsys.readouterr()
    report = ArenaDatabase(db_path).build_comparison_report(ArenaDatabase(db_path).latest_experiment_id())
    bys = {g["by"] for g in report["category_breakdown"]}
    assert bys == {"category"}
    cats = {g["group"]: g for g in report["category_breakdown"]}
    assert set(cats) == {"concurrency", "frontend_craft", "refactor_test", "security_hygiene"}
    assert all(g["tasks_paired"] == 2 and g["ci_low"] is not None for g in cats.values())
