"""Scoped tests for ArenaDatabase: exact Pass^k consistency math (K11), Unmetered propagation (K8/K9), and SIMULATED metadata (K3)."""

from __future__ import annotations

from pathlib import Path

from skill_arena.models import ContenderConfig, GraderVerdict, TokenUsageLedger, TrialResult
from skill_arena.store.arena_db import ArenaDatabase


def _make_trial(
    exp_id: str,
    task_id: str,
    contender: ContenderConfig,
    rep: int,
    passed: bool,
    *,
    metered: bool = True,
    turn1_tokens: int = 1000,
    cost_usd: float = 0.01,
    is_simulated: bool = False,
) -> TrialResult:
    usage = (
        TokenUsageLedger(
            turns=2,
            first_turn_prompt_tokens=turn1_tokens,
            total_prompt_tokens=turn1_tokens + 500,
            cached_prompt_tokens=200,
            completion_tokens=150,
            ttft_ms=120,
            total_latency_ms=800,
            total_cost_usd=cost_usd,
            is_metered=True,
            usage_status="metered",
            is_simulated=is_simulated,
        )
        if metered
        else TokenUsageLedger(
            turns=0,
            total_latency_ms=400,
            is_metered=False,
            usage_status="unmetered",
            is_simulated=is_simulated,
        )
    )
    return TrialResult(
        trial_id=f"tr_{exp_id}_{task_id}_{contender.contender_id}_{rep}",
        experiment_id=exp_id,
        task_id=task_id,
        task_title=f"Title {task_id}",
        task_category="concurrency",
        contender_id=contender.contender_id,
        contender_label=contender.label,
        contender_role=contender.role,
        rep_index=rep,
        passed=passed,
        anti_cheat_clean=True,
        usage=usage,
        verdict=GraderVerdict(passed=passed, exit_code=0 if passed else 1, anti_cheat_clean=True, summary="ok"),
        steps=[],
        git_diff="",
        started_at="2026-11-14T00:00:00+00:00",
        finished_at="2026-11-14T00:00:01+00:00",
        is_simulated=is_simulated,
    )


def test_pass_hat_k_exact_values_reject_constant_one_mutation(tmp_path: Path) -> None:
    db = ArenaDatabase(tmp_path / "passk.sqlite")
    ctrl = ContenderConfig(contender_id="ctrl", label="Control", role="control", cli="claude", model="claude-sonnet-4-5")
    treat = ContenderConfig(contender_id="treat", label="Treatment", role="treatment", cli="claude", model="claude-sonnet-4-5")
    db.create_experiment("exp_pk", title="Pass^k Verification", suite_name="s", k_reps=2, contenders=[ctrl, treat])

    # Control:
    # t1: True, True  -> all passed (1)
    # t2: True, False -> flaky (0 for Pass^2!)
    # t3: False, False -> fail (0)
    # t4: False, True -> flaky (0 for Pass^2!)
    # => Pass@1 = 4/8 = 0.5000, Pass^2 = 1/4 = 0.2500!
    outcomes_ctrl = {"t1": [True, True], "t2": [True, False], "t3": [False, False], "t4": [False, True]}
    # Treatment: all flaky [True, False] -> Pass@1 = 0.5000, Pass^2 = 0.0000!
    outcomes_treat = {"t1": [True, False], "t2": [True, False], "t3": [True, False], "t4": [True, False]}

    for tid, reps in outcomes_ctrl.items():
        for idx, ok in enumerate(reps, start=1):
            db.record_trial(_make_trial("exp_pk", tid, ctrl, idx, ok, turn1_tokens=1000, cost_usd=0.01))
    for tid, reps in outcomes_treat.items():
        for idx, ok in enumerate(reps, start=1):
            db.record_trial(_make_trial("exp_pk", tid, treat, idx, ok, turn1_tokens=1350, cost_usd=0.02))

    report = db.build_comparison_report("exp_pk")
    lb = {r["contender_id"]: r for r in report["leaderboard"]}

    assert lb["ctrl"]["pass_at_1"] == 0.5
    assert lb["ctrl"]["pass_hat_k"] == 0.25
    assert lb["ctrl"]["total_spend_usd"] == 0.08
    assert lb["ctrl"]["cost_per_success_usd"] == 0.02  # 0.08 / 4 successes

    assert lb["treat"]["pass_at_1"] == 0.5
    assert lb["treat"]["pass_hat_k"] == 0.0
    assert lb["treat"]["context_overhead_delta_tokens"] == 350


def test_unmetered_cli_propagates_none_and_unmetered_status(tmp_path: Path) -> None:
    db = ArenaDatabase(tmp_path / "unmetered.sqlite")
    ctrl = ContenderConfig(contender_id="ctrl", label="OpenCode Control", role="control", cli="opencode", model="m1")
    treat = ContenderConfig(contender_id="treat", label="OpenCode Treat", role="treatment", cli="opencode", model="m1")
    db.create_experiment("exp_unm", title="Unmetered Check", suite_name="s", k_reps=1, contenders=[ctrl, treat])

    db.record_trial(_make_trial("exp_unm", "t1", ctrl, 1, False, metered=False))
    db.record_trial(_make_trial("exp_unm", "t1", treat, 1, True, metered=False))

    report = db.build_comparison_report("exp_unm")
    lb = {r["contender_id"]: r for r in report["leaderboard"]}
    for cid in ("ctrl", "treat"):
        assert lb[cid]["is_metered"] is False
        assert lb[cid]["usage_status"] == "unmetered"
        assert lb[cid]["total_spend_usd"] is None
        assert lb[cid]["cost_per_success_usd"] is None
        assert lb[cid]["mean_first_turn_tokens"] is None
        assert lb[cid]["context_overhead_delta_tokens"] is None

    m_arms = report["task_matrix"][0]["arms"]
    assert m_arms["ctrl"]["is_metered"] is False
    assert m_arms["ctrl"]["mean_cost_usd"] is None
    assert m_arms["ctrl"]["mean_tokens"] is None


def test_simulated_experiment_persists_simulated_flags_in_db_and_report(tmp_path: Path) -> None:
    db = ArenaDatabase(tmp_path / "sim.sqlite")
    ctrl = ContenderConfig(contender_id="ctrl", label="[SIMULATED] Control", role="control", cli="simulate:baseline", model="synthetic-sim")
    treat = ContenderConfig(contender_id="treat", label="[SIMULATED] Treat", role="treatment", cli="simulate:skilled", model="synthetic-sim")
    db.create_experiment(
        "exp_sim",
        title="[SIMULATED] Synthetic Calibration",
        suite_name="s",
        k_reps=1,
        contenders=[ctrl, treat],
        is_simulated=True,
    )
    db.record_trial(_make_trial("exp_sim", "t1", ctrl, 1, False, is_simulated=True))
    db.record_trial(_make_trial("exp_sim", "t1", treat, 1, True, is_simulated=True))

    report = db.build_comparison_report("exp_sim")
    assert report["is_simulated"] is True
    for row in report["leaderboard"]:
        assert row["is_simulated"] is True


def test_newcombe_wilson_ci_20_vs_20_equal_rates_is_inconclusive_with_no_winner(tmp_path: Path) -> None:
    for label_case, ctrl_passes, treat_passes in (
        ("half", 10, 10),
        ("all_pass", 20, 20),
        ("all_fail", 0, 0),
    ):
        db = ArenaDatabase(tmp_path / f"ci_{label_case}.sqlite")
        ctrl = ContenderConfig(contender_id="ctrl", label="Control", role="control", cli="claude", model="claude-sonnet-4-5")
        treat = ContenderConfig(contender_id="treat", label="Treatment", role="treatment", cli="claude", model="claude-sonnet-4-5")
        exp_id = f"exp_{label_case}"
        db.create_experiment(exp_id, title="Equal Rate 20x20", suite_name="s", k_reps=20, contenders=[ctrl, treat])

        for rep in range(1, 21):
            db.record_trial(_make_trial(exp_id, "t1", ctrl, rep, rep <= ctrl_passes))
            db.record_trial(_make_trial(exp_id, "t1", treat, rep, rep <= treat_passes))

        report = db.build_comparison_report(exp_id)
        sc = report["statistical_comparison"]
        assert sc["method"] == "newcombe_wilson_95"
        assert sc["control_trials"] == 20
        assert sc["treatment_trials"] == 20
        assert sc["ci_low"] < 0.0 < sc["ci_high"], f"Expected non-zero 95% CI spanning 0 for {label_case}, got {sc}"
        assert sc["verdict"] == "inconclusive"
        assert sc["winner"] is None
        assert "看不出差别" in sc["verdict_label_zh"]
        assert "Inconclusive" in sc["verdict_label_en"]
        for row in report["leaderboard"]:
            assert row["rank"] is None


def test_newcombe_wilson_ci_clear_separation_ranks_winner_and_small_sample_is_inconclusive(tmp_path: Path) -> None:
    db = ArenaDatabase(tmp_path / "ci_sep.sqlite")
    ctrl = ContenderConfig(contender_id="ctrl", label="Control", role="control", cli="claude", model="claude-sonnet-4-5")
    treat = ContenderConfig(contender_id="treat", label="Treatment", role="treatment", cli="claude", model="claude-sonnet-4-5")

    # 1. Clear separation: 2/20 vs 18/20 -> Treatment wins with 95% CI strictly > 0
    db.create_experiment("exp_win", title="Clear Winner", suite_name="s", k_reps=20, contenders=[ctrl, treat])
    for rep in range(1, 21):
        db.record_trial(_make_trial("exp_win", "t1", ctrl, rep, rep <= 2))
        db.record_trial(_make_trial("exp_win", "t1", treat, rep, rep <= 18))

    rep_win = db.build_comparison_report("exp_win")
    sc_win = rep_win["statistical_comparison"]
    assert sc_win["verdict"] == "treatment_better"
    assert sc_win["winner"] == "treat"
    assert sc_win["ci_low"] > 0.0
    lb_win = {r["contender_id"]: r for r in rep_win["leaderboard"]}
    assert lb_win["treat"]["rank"] == 1
    assert lb_win["ctrl"]["rank"] == 2

    # 2. Small sample (3 vs 3): even 0/3 vs 3/3 must refuse ranking as inconclusive
    db.create_experiment("exp_tiny", title="Tiny Sample", suite_name="s", k_reps=3, contenders=[ctrl, treat])
    for rep in range(1, 4):
        db.record_trial(_make_trial("exp_tiny", "t1", ctrl, rep, False))
        db.record_trial(_make_trial("exp_tiny", "t1", treat, rep, True))

    rep_tiny = db.build_comparison_report("exp_tiny")
    sc_tiny = rep_tiny["statistical_comparison"]
    assert sc_tiny["verdict"] == "inconclusive"
    assert sc_tiny["winner"] is None
    assert "看不出差别" in sc_tiny["verdict_label_zh"]
    for row in rep_tiny["leaderboard"]:
        assert row["rank"] is None


def test_metered_tokens_with_unknown_model_price_keeps_cost_none(tmp_path: Path) -> None:
    db = ArenaDatabase(tmp_path / "unknown_price.sqlite")
    ctrl = ContenderConfig(contender_id="ctrl", label="Control", role="control", cli="claude", model="custom-local-llm")
    db.create_experiment("exp_up", title="Unknown Price", suite_name="s", k_reps=1, contenders=[ctrl])
    tr = _make_trial("exp_up", "t1", ctrl, 1, True, metered=True, turn1_tokens=1400, cost_usd=0.01)
    tr.usage.total_cost_usd = None
    db.record_trial(tr)

    report = db.build_comparison_report("exp_up")
    row = report["leaderboard"][0]
    assert row["is_metered"] is True
    assert row["mean_first_turn_tokens"] == 1400
    assert row["total_spend_usd"] is None
    assert row["cost_per_success_usd"] is None


def test_kr3_parse_relay_log_md_sums_step_costs_and_surfaces_alongside_xray_cost(
    tmp_path: Path,
) -> None:
    import pytest
    from skill_arena.runners.harness_driver import parse_relay_artifacts

    workdir = tmp_path / "work"
    relay_dir = workdir / ".relay"
    relay_dir.mkdir(parents=True)
    (relay_dir / "LOG.md").write_text(
        "# Relay Log\n\n"
        "## 01-claude: ok, 3s, 2026-11-15 10:00\n"
        "- claude: model claude-sonnet-4-5, cost $0.0125, tokens 450, session s1\n"
        "- files changed:\n"
        "  - a.py\n\n"
        "## 02-claude: ok, 2s, 2026-11-15 10:01\n"
        "- claude: model claude-sonnet-4-5, cost $0.0075, tokens 350, session s2\n"
        "- files changed:\n"
        "  - b.py\n",
        encoding="utf-8",
    )
    # Even if step 02 raw .log only has step 02's $0.0075, LOG.md step sum ($0.0200) must win!
    (relay_dir / "02-claude.log").write_text(
        '{"result":"done","total_cost_usd":0.0075,"usage":{"total_tokens":350}}\n',
        encoding="utf-8",
    )

    rec = parse_relay_artifacts(workdir)
    assert rec.status == "ok"
    assert rec.cost_usd == pytest.approx(0.0200)
    assert rec.tokens == 800
    assert rec.changed_files == ["a.py", "b.py"]

    db = ArenaDatabase(tmp_path / "kr3.sqlite")
    ctrl = ContenderConfig(contender_id="ctrl", label="Control", role="control", cli="claude", model="claude-sonnet-4-5")
    treat = ContenderConfig(contender_id="treat", label="Treatment", role="treatment", cli="claude", model="claude-sonnet-4-5")
    db.create_experiment("exp_kr3", title="KR3 Dual Cost", suite_name="s", k_reps=2, contenders=[ctrl, treat])

    t_c1 = _make_trial("exp_kr3", "task1", ctrl, 1, True, metered=True, turn1_tokens=500, cost_usd=0.0021)
    t_c1.usage.relay_cost_usd = 0.0150
    t_c2 = _make_trial("exp_kr3", "task1", ctrl, 2, True, metered=True, turn1_tokens=500, cost_usd=0.0023)
    t_c2.usage.relay_cost_usd = 0.0160

    t_t1 = _make_trial("exp_kr3", "task1", treat, 1, True, metered=True, turn1_tokens=620, cost_usd=0.0031)
    t_t1.usage.relay_cost_usd = 0.0210
    t_t2 = _make_trial("exp_kr3", "task1", treat, 2, True, metered=True, turn1_tokens=620, cost_usd=0.0033)
    t_t2.usage.relay_cost_usd = 0.0220

    for tr in (t_c1, t_c2, t_t1, t_t2):
        db.record_trial(tr)

    rep = db.build_comparison_report("exp_kr3")
    by_id = {r["contender_id"]: r for r in rep["leaderboard"]}
    assert by_id["ctrl"]["total_spend_usd"] == pytest.approx(0.0044)
    assert by_id["ctrl"]["metered_cost_usd"] == pytest.approx(0.0044)
    assert by_id["ctrl"]["total_relay_cost_usd"] == pytest.approx(0.0310)
    assert by_id["ctrl"]["relay_cost_usd"] == pytest.approx(0.0310)

    assert by_id["treat"]["total_spend_usd"] == pytest.approx(0.0064)
    assert by_id["treat"]["metered_cost_usd"] == pytest.approx(0.0064)
    assert by_id["treat"]["total_relay_cost_usd"] == pytest.approx(0.0430)
    assert by_id["treat"]["relay_cost_usd"] == pytest.approx(0.0430)

    assert "arms" in rep and len(rep["arms"]) == 2
    assert "trials" in rep and len(rep["trials"]) == 4
    assert [tr["relay_cost_usd"] for tr in rep["trials"]] == [0.015, 0.016, 0.021, 0.022]

