"""Multi-arm experiments: --arm, per-treatment comparisons, sign-flip permutation p-values and Holm."""

from __future__ import annotations

import itertools
import json
from pathlib import Path
import random

import pytest

from skill_arena.cli import main, parse_arm_specs
from skill_arena.graders.frontend_craft import audit_html_craft
from skill_arena.models import ContenderConfig, GraderVerdict, TokenUsageLedger, TrialResult
from skill_arena.store.arena_db import ArenaDatabase, holm_adjust, sign_flip_permutation_p_value
from skill_arena.studio.templates import render_arena_html


def _brute_force_p(diffs: list[float]) -> float:
    observed = abs(sum(diffs))
    hits = 0
    total = 0
    for signs in itertools.product((1, -1), repeat=len(diffs)):
        total += 1
        if abs(sum(s * d for s, d in zip(signs, diffs))) >= observed - 1e-9:
            hits += 1
    return hits / total


@pytest.mark.parametrize(
    "diffs",
    [[1.0, 1.0, 1.0], [1.0] * 5, [0.5, -0.25, 1.0, 0.0, 0.0], [1 / 3, 2 / 3, -1 / 3, 1.0, 0.0, -2 / 3, 1 / 3]],
)
def test_exact_sign_flip_matches_brute_force(diffs: list[float]) -> None:
    p, method = sign_flip_permutation_p_value(diffs)
    assert method == "sign_flip_exact"
    assert p == pytest.approx(_brute_force_p(diffs))


def test_known_exact_values_and_degenerate_cases() -> None:
    assert sign_flip_permutation_p_value([1.0, 1.0, 1.0])[0] == pytest.approx(0.25)
    assert sign_flip_permutation_p_value([1.0] * 5)[0] == pytest.approx(2 / 32)
    assert sign_flip_permutation_p_value([1.0, 1.0, 1.0, 0.0, 0.0])[0] == pytest.approx(0.25)
    assert sign_flip_permutation_p_value([])[0] == 1.0
    assert sign_flip_permutation_p_value([0.0, 0.0])[0] == 1.0


def test_monte_carlo_beyond_sixteen_tasks_is_seeded_and_close_to_exact() -> None:
    rng = random.Random(7)
    diffs = [rng.choice([-1.0, -2 / 3, -1 / 3, 1 / 3, 2 / 3, 1.0]) + 0.3 for _ in range(18)]
    p_mc, method = sign_flip_permutation_p_value(diffs)
    assert method == "sign_flip_monte_carlo_20000"
    assert p_mc == sign_flip_permutation_p_value(diffs)[0]  # same seed, same answer
    p_exact, exact_method = sign_flip_permutation_p_value(diffs, exact_max=20)
    assert exact_method == "sign_flip_exact"
    assert abs(p_mc - p_exact) < 0.01
    # 20 tasks all in the same direction: tiny p, floored at 1 / (draws + 1).
    p_all, _ = sign_flip_permutation_p_value([1.0] * 20)
    assert 1 / 20001 <= p_all <= 3 / 20001


def test_holm_adjustment() -> None:
    assert holm_adjust([0.01, 0.04, 0.03]) == pytest.approx([0.03, 0.06, 0.06])
    assert holm_adjust([0.5]) == [0.5]
    assert holm_adjust([0.2, 0.9]) == pytest.approx([0.4, 0.9])


def _trial(exp: str, task: str, c: ContenderConfig, rep: int, passed: bool) -> TrialResult:
    return TrialResult(
        trial_id=f"tr_{c.contender_id}_{task}_{rep}",
        experiment_id=exp,
        task_id=task,
        task_title=task,
        task_category="parse",
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


def test_report_has_one_comparison_per_treatment_with_holm(tmp_path: Path) -> None:
    db = ArenaDatabase(tmp_path / "a.sqlite")
    ctrl = ContenderConfig(contender_id="arm_control", label="ctrl", role="control", cli="claude", model="m")
    strong = ContenderConfig(contender_id="arm_strong", label="strong", role="treatment", cli="claude", model="m")
    null = ContenderConfig(contender_id="arm_null", label="null", role="treatment", cli="claude", model="m")
    db.create_experiment("exp", "T", "s", 2, [ctrl, strong, null])
    for i in range(10):
        task = f"t{i}"
        for rep in (1, 2):
            # Control solves t0-t1; "strong" solves everything (8 tasks better); "null" solves t2-t3 only,
            # so it wins two tasks and loses two (sum of differences 0).
            db.record_trial(_trial("exp", task, ctrl, rep, passed=i < 2))
            db.record_trial(_trial("exp", task, strong, rep, passed=True))
            db.record_trial(_trial("exp", task, null, rep, passed=i in (2, 3)))
    report = db.build_comparison_report("exp")
    comps = report["comparisons"]
    assert [c["treatment_id"] for c in comps] == ["arm_strong", "arm_null"]
    assert report["statistical_comparison"] == comps[0]
    strong_c, null_c = comps
    assert strong_c["p_value_method"] == "sign_flip_exact"
    assert strong_c["p_value"] == pytest.approx(2 / 256, abs=1e-6)
    assert null_c["p_value"] == pytest.approx(1.0)
    assert strong_c["p_value_holm"] == pytest.approx(min(1.0, 2 * strong_c["p_value"]))
    assert strong_c["significant_holm_05"] is True and null_c["significant_holm_05"] is False
    assert strong_c["holm_family_size"] == 2
    # With several treatments no single winner rank is assigned.
    assert all(row["rank"] is None for row in report["leaderboard"])


def test_parse_arm_specs_validation(tmp_path: Path) -> None:
    skill = tmp_path / "tdd" / "SKILL.md"
    skill.parent.mkdir()
    skill.write_text("# tdd\n", encoding="utf-8")
    other = tmp_path / "other.md"
    other.write_text("x\n", encoding="utf-8")
    assert parse_arm_specs([f"tdd={skill}", f"both={skill},{other}"], simulate=False) == [
        ("tdd", [str(skill)]),
        ("both", [str(skill), str(other)]),
    ]
    with pytest.raises(ValueError, match="not found"):
        parse_arm_specs([f"x={tmp_path / 'missing'}"], simulate=False)
    with pytest.raises(ValueError, match="at least one skill path"):
        parse_arm_specs(["x"], simulate=False)
    with pytest.raises(ValueError, match="Duplicate or reserved"):
        parse_arm_specs([f"a={skill}", f"A={skill}"], simulate=False)
    with pytest.raises(ValueError, match="Duplicate or reserved"):
        parse_arm_specs([f"control={skill}"], simulate=False)
    with pytest.raises(ValueError, match="label only"):
        parse_arm_specs([f"a={skill}"], simulate=True)
    with pytest.raises(ValueError, match="Invalid --arm label"):
        parse_arm_specs([f"bad label={skill}"], simulate=False)


def test_cli_rejects_skill_and_arm_together(tmp_path: Path, capsys) -> None:
    skill = tmp_path / "SKILL.md"
    skill.write_text("x\n", encoding="utf-8")
    rc = main(
        ["run", "--cli", "claude", "--suite", "concurrency", "--skill", str(skill), "--arm", f"a={skill}",
         "--db", str(tmp_path / "x.sqlite")]
    )
    assert rc == 2
    assert "not both" in capsys.readouterr().err


def test_simulated_multi_arm_run_reports_every_treatment(tmp_path: Path, capsys) -> None:
    db_path = tmp_path / "multi.sqlite"
    html_path = tmp_path / "multi.html"
    rc = main(
        [
            "--lang", "en", "run", "--cli", "simulate", "--suite", "concurrency", "--reps", "1",
            "--arm", "alpha", "--arm", "beta", "--db", str(db_path), "--html-out", str(html_path),
        ]
    )
    out = capsys.readouterr().out
    assert rc == 0
    assert "Each treatment vs control" in out
    assert "(+ Synthetic alpha)" in out and "(+ Synthetic beta)" in out
    assert "Holm p=" in out
    assert main(["compare", "--db", str(db_path), "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert [c["contender_id"] for c in report["contenders"]] == ["arm_control", "arm_alpha", "arm_beta"]
    assert len(report["comparisons"]) == 2
    assert {c["treatment_id"] for c in report["comparisons"]} == {"arm_alpha", "arm_beta"}
    page = html_path.read_text(encoding="utf-8")
    assert "Each treatment vs control (task-paired)" in page and "Holm p" in page
    assert audit_html_craft(html_path).passed, audit_html_craft(html_path).findings
    assert main(["--lang", "zh", "compare", "--db", str(db_path)]) == 0
    assert "Holm p=" in capsys.readouterr().out
