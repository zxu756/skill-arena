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
