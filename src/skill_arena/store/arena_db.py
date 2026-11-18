"""SQLite storage and scientific A/B comparison calculator for SkillArena."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import random
import sqlite3
import statistics
from typing import Any

from skill_arena.outcomes import FAILURE_KINDS, effective_failure_kind, is_interrupted
from skill_arena.models import (
    ContenderConfig,
    GraderVerdict,
    LeaderboardSummary,
    TokenUsageLedger,
    TrajectoryStep,
    TrialResult,
)

WILSON_Z_95 = 1.959963984540054
MIN_TRIALS_FOR_RANKING = 5


def compute_wilson_ci_95(successes: int, total: int, z: float = WILSON_Z_95) -> tuple[float, float]:
    """Closed-form Wilson score 95% confidence interval for a binomial proportion."""
    if total <= 0:
        return (0.0, 1.0)
    k = max(0, min(int(successes), int(total)))
    n = float(total)
    p = k / n
    z2 = z * z
    denom = 1.0 + (z2 / n)
    center = (p + z2 / (2.0 * n)) / denom
    half = (z / denom) * math.sqrt((p * (1.0 - p)) / n + (z2 / (4.0 * n * n)))
    return (max(0.0, center - half), min(1.0, center + half))


# Two-sided 97.5% Student-t quantiles for df = 1..30.
_T_975 = (
    12.706, 4.303, 3.182, 2.776, 2.571, 2.447, 2.365, 2.306, 2.262, 2.228,
    2.201, 2.179, 2.160, 2.145, 2.131, 2.120, 2.110, 2.101, 2.093, 2.086,
    2.080, 2.074, 2.069, 2.064, 2.060, 2.056, 2.052, 2.048, 2.045, 2.042,
)
Z_POWER_80 = 0.8416212335729143
MAX_INTERRUPTED_FRACTION = 0.10


def student_t_975(df: int) -> float:
    """Two-sided 95% critical value of Student's t (Cornish-Fisher beyond the table)."""
    if df <= 0:
        return WILSON_Z_95
    if df <= len(_T_975):
        return _T_975[df - 1]
    z = WILSON_Z_95
    return z + (z**3 + z) / (4.0 * df) + (5 * z**5 + 16 * z**3 + 3 * z) / (96.0 * df * df)


def trial_was_interrupted(trial: TrialResult) -> bool:
    """True when the agent was stopped by the time limit instead of finishing on its own."""
    return is_interrupted(trial.usage.relay_status, trial.verdict.summary)


def compute_paired_task_comparison(
    *,
    control_id: str | None,
    treatment_id: str | None,
    per_task: list[tuple[int, int, int, int]],
    control_interrupted: int = 0,
    treatment_interrupted: int = 0,
) -> dict[str, Any]:
    """95% CI on the Pass@1 difference, paired by task (Miller 2024, arXiv 2411.00640, §3-4).

    ``per_task`` holds ``(control_passed, control_trials, treatment_passed, treatment_trials)``
    for every task both arms ran. Repeats of one task are correlated, so each task is
    reduced to its pass rate first and the task is the unit of analysis:

    * between-task SE: ``sqrt(sum((d_i - d_bar)^2) / (n (n - 1)))`` with ``d_i = p_T,i - p_C,i``;
    * within-task SE: ``sqrt(sum(v_i)) / n`` with Agresti-Caffo variances
      ``v_i = p~(1 - p~) / (k + 2)``, ``p~ = (x + 1) / (k + 2)`` per arm, which stays
      non-zero when a task passes or fails every repeat.

    The larger of the two is used (conservative), with a Student-t critical value on
    ``n - 1`` degrees of freedom (normal when only one task). The minimum detectable
    effect at 80% power is ``(t + z_0.8) * SE``. If more than 10% of either arm's trials
    were stopped by the time limit the run is reported as invalid and no winner is named.
    """
    rows = [
        (max(0, min(int(xc), int(kc))), max(0, int(kc)), max(0, min(int(xt), int(kt))), max(0, int(kt)))
        for xc, kc, xt, kt in per_task
        if int(kc) > 0 and int(kt) > 0
    ]
    n = len(rows)
    n_c = sum(r[1] for r in rows)
    n_t = sum(r[3] for r in rows)
    k_c = sum(r[0] for r in rows)
    k_t = sum(r[2] for r in rows)
    p_c = sum(r[0] / r[1] for r in rows) / n if n else 0.0
    p_t = sum(r[2] / r[3] for r in rows) / n if n else 0.0
    delta = p_t - p_c

    if n:
        diffs = [r[2] / r[3] - r[0] / r[1] for r in rows]
        mean_d = sum(diffs) / n
        se_between = math.sqrt(sum((d - mean_d) ** 2 for d in diffs) / (n * (n - 1))) if n > 1 else 0.0

        def _ac_var(x: int, k: int) -> float:
            p_tilde = (x + 1.0) / (k + 2.0)
            return p_tilde * (1.0 - p_tilde) / (k + 2.0)

        se_within = math.sqrt(sum(_ac_var(r[0], r[1]) + _ac_var(r[2], r[3]) for r in rows)) / n
    else:
        se_between = 0.0
        se_within = 0.5
    se = max(se_between, se_within)
    df = n - 1 if n > 1 else 0
    crit = student_t_975(df)
    ci_low = max(-1.0, delta - crit * se)
    ci_high = min(1.0, delta + crit * se)
    mde = min(1.0, (crit + Z_POWER_80) * se)

    frac_c = (control_interrupted / n_c) if n_c else 0.0
    frac_t = (treatment_interrupted / n_t) if n_t else 0.0
    worst_frac = max(frac_c, frac_t)

    if control_id is None or treatment_id is None or control_id == treatment_id:
        verdict = "inconclusive"
        winner = None
        label_zh = "看不出差别（缺少可比双组样本，不排定名次）"
        label_en = "Inconclusive (single arm; no rank assigned)"
    elif n_c < MIN_TRIALS_FOR_RANKING or n_t < MIN_TRIALS_FOR_RANKING:
        verdict = "inconclusive"
        winner = None
        min_n = min(n_c, n_t)
        label_zh = f"看不出差别（每组试验数 n={min_n} < {MIN_TRIALS_FOR_RANKING}，样本过少不排定名次）"
        label_en = f"Inconclusive (n={min_n} < {MIN_TRIALS_FOR_RANKING} trials per arm; no rank assigned)"
    elif worst_frac > MAX_INTERRUPTED_FRACTION:
        verdict = "invalid"
        winner = None
        pct_c = round(frac_c * 100)
        pct_t = round(frac_t * 100)
        label_zh = (
            f"实验无效：被时限打断的试验对照组 {pct_c}%、实验组 {pct_t}%，"
            f"超过 {round(MAX_INTERRUPTED_FRACTION * 100)}%；请加长 timeout_sec 后重跑，不排定名次"
        )
        label_en = (
            f"Invalid run: {pct_c}% of control and {pct_t}% of treatment trials hit the time limit "
            f"(> {round(MAX_INTERRUPTED_FRACTION * 100)}%); raise timeout_sec and rerun; no rank assigned"
        )
    elif ci_low <= 0.0 <= ci_high:
        verdict = "inconclusive"
        winner = None
        label_zh = "看不出差别（95% 置信区间跨越 0，不排定名次）"
        label_en = "Inconclusive (95% CI spans 0; no rank assigned)"
    elif ci_low > 0.0:
        verdict = "treatment_better"
        winner = treatment_id
        label_zh = "实验组显著占优（95% 置信区间 > 0）"
        label_en = "Treatment statistically outperforms Control (95% CI > 0)"
    else:
        verdict = "control_better"
        winner = control_id
        label_zh = "对照组显著占优（95% 置信区间 < 0）"
        label_en = "Control statistically outperforms Treatment (95% CI < 0)"

    return {
        "method": "paired_task_t_95",
        "confidence_level": 0.95,
        "min_trials_per_arm": MIN_TRIALS_FOR_RANKING,
        "control_id": control_id,
        "treatment_id": treatment_id,
        "tasks_paired": n,
        "control_passed": k_c,
        "control_trials": n_c,
        "control_rate": round(p_c, 4),
        "treatment_passed": k_t,
        "treatment_trials": n_t,
        "treatment_rate": round(p_t, 4),
        "delta_pass_rate": round(delta, 4),
        "standard_error": round(se, 4),
        "se_between_tasks": round(se_between, 4),
        "se_within_tasks": round(se_within, 4),
        "degrees_of_freedom": df,
        "critical_value": round(crit, 4),
        "ci_low": round(ci_low, 4),
        "ci_high": round(ci_high, 4),
        "min_detectable_effect_80": round(mde, 4),
        "control_interrupted": int(control_interrupted),
        "treatment_interrupted": int(treatment_interrupted),
        "control_interrupted_fraction": round(frac_c, 4),
        "treatment_interrupted_fraction": round(frac_t, 4),
        "max_interrupted_fraction": MAX_INTERRUPTED_FRACTION,
        "verdict": verdict,
        "winner": winner,
        "verdict_label_en": label_en,
        "verdict_label_zh": label_zh,
    }

EXACT_PERMUTATION_MAX_TASKS = 16
MONTE_CARLO_PERMUTATIONS = 20_000
PERMUTATION_SEED = 20261118


def sign_flip_permutation_p_value(
    diffs: list[float],
    *,
    exact_max: int = EXACT_PERMUTATION_MAX_TASKS,
    n_draws: int = MONTE_CARLO_PERMUTATIONS,
    seed: int = PERMUTATION_SEED,
) -> tuple[float, str]:
    """Two-sided paired sign-flip permutation test on per-task differences ``d_i = p_T,i - p_C,i``.

    Under the null hypothesis the skill does nothing, so each task's difference is as likely to have
    either sign. The statistic is ``|sum(d_i)|``. Tasks with ``d_i = 0`` do not change any flipped
    sum, so they are left out of the enumeration (the p-value is identical). Up to ``exact_max``
    non-zero tasks all ``2^n`` sign patterns are enumerated (Gray code); beyond that a seeded Monte
    Carlo with ``n_draws`` draws is used and ``(hits + 1) / (n_draws + 1)`` is returned.
    """
    nz = [float(d) for d in diffs if abs(float(d)) > 1e-12]
    if not nz:
        return 1.0, "sign_flip_exact"
    observed = abs(sum(nz))
    tol = 1e-9
    n = len(nz)
    if n <= exact_max:
        total = sum(nz)
        signs = [1] * n
        hits = 1 if abs(total) >= observed - tol else 0
        for i in range(1, 1 << n):
            # Gray code: flip the lowest set bit of i.
            k = (i & -i).bit_length() - 1
            total -= 2 * signs[k] * nz[k]
            signs[k] = -signs[k]
            if abs(total) >= observed - tol:
                hits += 1
        return hits / float(1 << n), "sign_flip_exact"
    rng = random.Random(seed)
    hits = 0
    for _ in range(n_draws):
        total = 0.0
        for d in nz:
            total += d if rng.random() < 0.5 else -d
        if abs(total) >= observed - tol:
            hits += 1
    return (hits + 1) / float(n_draws + 1), f"sign_flip_monte_carlo_{n_draws}"


def holm_adjust(p_values: list[float]) -> list[float]:
    """Holm-Bonferroni step-down adjusted p-values, returned in the input order."""
    m = len(p_values)
    order = sorted(range(m), key=lambda i: p_values[i])
    adjusted = [1.0] * m
    running = 0.0
    for rank, idx in enumerate(order):
        val = min(1.0, (m - rank) * float(p_values[idx]))
        running = max(running, val)
        adjusted[idx] = running
    return adjusted


def _paired_rows(ctrl_trials: list[TrialResult], treat_trials: list[TrialResult]) -> list[tuple[str, int, int, int, int]]:
    ctrl_by_task: dict[str, list[TrialResult]] = defaultdict(list)
    treat_by_task: dict[str, list[TrialResult]] = defaultdict(list)
    for t in ctrl_trials:
        ctrl_by_task[t.task_id].append(t)
    for t in treat_trials:
        treat_by_task[t.task_id].append(t)
    return [
        (
            tid,
            sum(1 for r in ctrl_by_task[tid] if r.passed),
            len(ctrl_by_task[tid]),
            sum(1 for r in treat_by_task[tid] if r.passed),
            len(treat_by_task[tid]),
        )
        for tid in ctrl_by_task
        if tid in treat_by_task
    ]


def compare_arms(
    control_id: str | None,
    treatment_id: str | None,
    ctrl_trials: list[TrialResult],
    treat_trials: list[TrialResult],
) -> dict[str, Any]:
    """Task-paired CI plus the exact / Monte Carlo sign-flip permutation p-value for one treatment."""
    rows = _paired_rows(ctrl_trials, treat_trials)
    comp = compute_paired_task_comparison(
        control_id=control_id,
        treatment_id=treatment_id,
        per_task=[r[1:] for r in rows],
        control_interrupted=sum(1 for t in ctrl_trials if trial_was_interrupted(t)),
        treatment_interrupted=sum(1 for t in treat_trials if trial_was_interrupted(t)),
    )
    diffs = [xt / kt - xc / kc for _tid, xc, kc, xt, kt in rows if kc > 0 and kt > 0]
    p_val, method = sign_flip_permutation_p_value(diffs)
    comp["p_value"] = round(p_val, 6)
    comp["p_value_method"] = method
    return comp


PASS_AT_TIME_GRID_SEC: tuple[int, ...] = (60, 120, 180, 300, 600, 900)


def summarize_agent_test_quality(trials: list[TrialResult]) -> dict[str, Any]:
    """Per-arm summary of the agent's own tests (mutation score averaged over trials with valid tests)."""
    with_tests = [t for t in trials if t.verdict.agent_tests_files]
    scored = [t for t in with_tests if t.verdict.agent_tests_valid is not None]
    valid = [t for t in scored if t.verdict.agent_tests_valid]
    with_mutants = [t for t in valid if t.verdict.mutants_total and t.verdict.mutants_killed is not None]
    scores = [float(t.verdict.mutants_killed or 0) / float(t.verdict.mutants_total or 1) for t in with_mutants]
    return {
        "trials": len(trials),
        "trials_with_tests": len(with_tests),
        "trials_scored": len(scored),
        "trials_valid": len(valid),
        "valid_rate": round(len(valid) / len(scored), 4) if scored else None,
        "trials_with_mutants": len(with_mutants),
        "mean_mutation_score": round(sum(scores) / len(scores), 4) if scores else None,
        "mutants_killed": sum(int(t.verdict.mutants_killed or 0) for t in with_mutants),
        "mutants_total": sum(int(t.verdict.mutants_total or 0) for t in with_mutants),
    }


def trial_agent_seconds(trial: TrialResult) -> float:
    """Agent wall time in seconds (falls back to the recorded latency for rows without it)."""
    ms = trial.agent_wall_ms if trial.agent_wall_ms is not None else trial.usage.total_latency_ms
    return max(0.0, float(ms or 0) / 1000.0)


def compute_pass_at_time(
    trials: list[TrialResult],
    grid_sec: tuple[int, ...] = PASS_AT_TIME_GRID_SEC,
) -> tuple[list[dict[str, Any]], int | None]:
    """Pass@T: share of trials that passed AND whose agent finished within T seconds.

    Like Pass@1 it is averaged per task first, then across tasks. Grid points above the time limit
    are dropped; the time limit itself (the largest ``task_timeout_sec`` seen) is always included and
    equals Pass@1 there, since a trial stopped by the limit cannot pass.
    """
    by_task: dict[str, list[TrialResult]] = defaultdict(list)
    for t in trials:
        by_task[t.task_id].append(t)
    limits = [int(t.task_timeout_sec) for t in trials if t.task_timeout_sec]
    full_limit = max(limits) if limits else None
    points: list[int | None] = [g for g in grid_sec if full_limit is None or g < full_limit]
    points.append(full_limit)
    curve: list[dict[str, Any]] = []
    for t_sec in points:
        def _ok(tr: TrialResult) -> bool:
            if not tr.passed:
                return False
            return t_sec is None or trial_agent_seconds(tr) <= float(t_sec) + 1e-9

        rate = (
            sum(sum(1 for tr in lst if _ok(tr)) / len(lst) for lst in by_task.values()) / len(by_task)
            if by_task
            else 0.0
        )
        curve.append(
            {
                "t_sec": t_sec,
                "pass_rate": round(rate, 4),
                "passed_trials": sum(1 for tr in trials if _ok(tr)),
                "is_full_limit": t_sec == full_limit,
            }
        )
    return curve, full_limit


class ArenaDatabase:
    """Zero-dependency SQLite WAL store for SkillArena experiments and trajectories."""

    def __init__(self, db_path: Path | str) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        return conn

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS experiments (
                    experiment_id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    suite_name TEXT NOT NULL,
                    k_reps INTEGER NOT NULL,
                    contenders_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    is_simulated INTEGER NOT NULL DEFAULT 0,
                    budget_usd REAL,
                    cumulative_cost_usd REAL,
                    budget_stopped INTEGER NOT NULL DEFAULT 0,
                    planned_trials INTEGER,
                    completed_trials INTEGER
                );

                CREATE TABLE IF NOT EXISTS trials (
                    trial_id TEXT PRIMARY KEY,
                    experiment_id TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    task_title TEXT NOT NULL,
                    task_category TEXT NOT NULL,
                    contender_id TEXT NOT NULL,
                    contender_label TEXT NOT NULL,
                    contender_role TEXT NOT NULL,
                    rep_index INTEGER NOT NULL,
                    passed INTEGER NOT NULL,
                    anti_cheat_clean INTEGER NOT NULL,
                    usage_json TEXT NOT NULL,
                    verdict_json TEXT NOT NULL,
                    steps_json TEXT NOT NULL,
                    git_diff TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    finished_at TEXT NOT NULL,
                    is_simulated INTEGER NOT NULL DEFAULT 0,
                    FOREIGN KEY(experiment_id) REFERENCES experiments(experiment_id)
                );

                CREATE INDEX IF NOT EXISTS idx_trials_exp ON trials(experiment_id);
                """
            )
            for alter_sql in (
                "ALTER TABLE experiments ADD COLUMN is_simulated INTEGER NOT NULL DEFAULT 0;",
                "ALTER TABLE experiments ADD COLUMN budget_usd REAL;",
                "ALTER TABLE experiments ADD COLUMN cumulative_cost_usd REAL;",
                "ALTER TABLE experiments ADD COLUMN budget_stopped INTEGER NOT NULL DEFAULT 0;",
                "ALTER TABLE experiments ADD COLUMN planned_trials INTEGER;",
                "ALTER TABLE experiments ADD COLUMN completed_trials INTEGER;",
                "ALTER TABLE trials ADD COLUMN is_simulated INTEGER NOT NULL DEFAULT 0;",
                "ALTER TABLE trials ADD COLUMN meta_json TEXT;",
                "ALTER TABLE experiments ADD COLUMN design_json TEXT;",
            ):
                try:
                    conn.execute(alter_sql)
                except sqlite3.OperationalError:
                    pass

    def create_experiment(
        self,
        experiment_id: str,
        title: str,
        suite_name: str,
        k_reps: int,
        contenders: list[ContenderConfig],
        is_simulated: bool | None = None,
        budget_usd: float | None = None,
        planned_trials: int | None = None,
        design: dict[str, Any] | None = None,
    ) -> None:
        sim = (
            bool(is_simulated)
            if is_simulated is not None
            else any(c.cli.split(":", 1)[0] == "simulate" for c in contenders)
        )
        stored_title = title
        if sim and "SIMULATED" not in stored_title.upper():
            stored_title = f"[SIMULATED] {stored_title}"
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO experiments
                (experiment_id, title, suite_name, k_reps, contenders_json, created_at, is_simulated,
                 budget_usd, cumulative_cost_usd, budget_stopped, planned_trials, completed_trials, design_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    experiment_id,
                    stored_title,
                    suite_name,
                    k_reps,
                    json.dumps([c.to_dict() for c in contenders]),
                    now,
                    1 if sim else 0,
                    float(budget_usd) if budget_usd is not None else None,
                    0.0 if budget_usd is not None else None,
                    0,
                    int(planned_trials) if planned_trials is not None else None,
                    0 if planned_trials is not None else None,
                    json.dumps(design) if design is not None else None,
                ),
            )

    def update_experiment_budget_state(
        self,
        experiment_id: str,
        *,
        budget_usd: float | None,
        cumulative_cost_usd: float,
        budget_stopped: bool,
        planned_trials: int,
        completed_trials: int,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE experiments
                SET budget_usd = ?,
                    cumulative_cost_usd = ?,
                    budget_stopped = ?,
                    planned_trials = ?,
                    completed_trials = ?
                WHERE experiment_id = ?
                """,
                (
                    float(budget_usd) if budget_usd is not None else None,
                    round(float(cumulative_cost_usd), 6),
                    1 if budget_stopped else 0,
                    int(planned_trials),
                    int(completed_trials),
                    experiment_id,
                ),
            )

    def record_trial(self, trial: TrialResult) -> None:
        sim = bool(trial.is_simulated or trial.usage.is_simulated)
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO trials (
                    trial_id, experiment_id, task_id, task_title, task_category,
                    contender_id, contender_label, contender_role, rep_index,
                    passed, anti_cheat_clean, usage_json, verdict_json, steps_json,
                    git_diff, started_at, finished_at, is_simulated, meta_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    trial.trial_id,
                    trial.experiment_id,
                    trial.task_id,
                    trial.task_title,
                    trial.task_category,
                    trial.contender_id,
                    trial.contender_label,
                    trial.contender_role,
                    trial.rep_index,
                    1 if trial.passed else 0,
                    1 if trial.anti_cheat_clean else 0,
                    json.dumps(trial.usage.to_dict()),
                    json.dumps(trial.verdict.to_dict()),
                    json.dumps([s.to_dict() for s in trial.steps]),
                    trial.git_diff,
                    trial.started_at,
                    trial.finished_at,
                    1 if sim else 0,
                    json.dumps(
                        {
                            "launch_index": trial.launch_index,
                            "agent_wall_ms": trial.agent_wall_ms,
                            "task_timeout_sec": trial.task_timeout_sec,
                            "behavior": trial.behavior,
                        }
                    ),
                ),
            )

    def list_experiments(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT experiment_id, title, suite_name, k_reps, created_at, is_simulated FROM experiments ORDER BY created_at DESC"
            ).fetchall()
            out: list[dict[str, Any]] = []
            for r in rows:
                d = dict(r)
                d["is_simulated"] = bool(d.get("is_simulated", 0))
                out.append(d)
            return out

    def latest_experiment_id(self) -> str | None:
        exps = self.list_experiments()
        return exps[0]["experiment_id"] if exps else None

    def get_trials(self, experiment_id: str) -> list[TrialResult]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM trials WHERE experiment_id = ? ORDER BY task_id, contender_role, rep_index",
                (experiment_id,),
            ).fetchall()
        results: list[TrialResult] = []
        for r in rows:
            usage = TokenUsageLedger.from_dict(json.loads(r["usage_json"]))
            row_keys = set(r.keys())
            is_sim = bool(r["is_simulated"]) if "is_simulated" in row_keys else usage.is_simulated
            meta = json.loads(r["meta_json"]) if "meta_json" in row_keys and r["meta_json"] else {}
            results.append(
                TrialResult(
                    trial_id=r["trial_id"],
                    experiment_id=r["experiment_id"],
                    task_id=r["task_id"],
                    task_title=r["task_title"],
                    task_category=r["task_category"],
                    contender_id=r["contender_id"],
                    contender_label=r["contender_label"],
                    contender_role=r["contender_role"],
                    rep_index=int(r["rep_index"]),
                    passed=bool(r["passed"]),
                    anti_cheat_clean=bool(r["anti_cheat_clean"]),
                    usage=usage,
                    verdict=GraderVerdict.from_dict(json.loads(r["verdict_json"])),
                    steps=[TrajectoryStep.from_dict(s) for s in json.loads(r["steps_json"])],
                    git_diff=r["git_diff"],
                    started_at=r["started_at"],
                    finished_at=r["finished_at"],
                    is_simulated=is_sim,
                    launch_index=int(meta.get("launch_index", -1)),
                    agent_wall_ms=int(meta["agent_wall_ms"]) if meta.get("agent_wall_ms") is not None else None,
                    task_timeout_sec=(
                        int(meta["task_timeout_sec"]) if meta.get("task_timeout_sec") is not None else None
                    ),
                    behavior=dict(meta.get("behavior") or {}),
                )
            )
        return results

    def build_comparison_report(self, experiment_id: str) -> dict[str, Any]:
        """Compute Pass@1, Pass^k, Cost-per-Success ($), Turn-1 Context Tax, Win-Rate Derived Elo (2-arm), and replay bundle."""
        with self._connect() as conn:
            exp_row = conn.execute(
                "SELECT * FROM experiments WHERE experiment_id = ?", (experiment_id,)
            ).fetchone()
        if exp_row is None:
            raise KeyError(f"Unknown experiment_id: {experiment_id}")

        contenders = [ContenderConfig.from_dict(c) for c in json.loads(exp_row["contenders_json"])]
        trials = self.get_trials(experiment_id)
        k_reps = int(exp_row["k_reps"])
        exp_keys = set(exp_row.keys())
        exp_is_simulated = (
            bool(exp_row["is_simulated"]) if "is_simulated" in exp_keys else False
        ) or any(c.cli.split(":", 1)[0] == "simulate" for c in contenders) or any(t.is_simulated for t in trials)

        by_contender: dict[str, list[TrialResult]] = defaultdict(list)
        for tr in trials:
            by_contender[tr.contender_id].append(tr)

        control_cid: str | None = None
        control_turn1_tokens: int | None = None
        for c in contenders:
            c_trials = by_contender.get(c.contender_id, [])
            if c.role == "control" and c_trials:
                control_cid = c.contender_id
                if all(t.usage.is_metered and t.usage.first_turn_prompt_tokens is not None for t in c_trials):
                    control_turn1_tokens = int(
                        round(statistics.mean(int(t.usage.first_turn_prompt_tokens or 0) for t in c_trials))
                    )
                break
        if control_cid is None and contenders:
            control_cid = contenders[0].contender_id
            first_trials = by_contender.get(control_cid, [])
            if first_trials and all(t.usage.is_metered and t.usage.first_turn_prompt_tokens is not None for t in first_trials):
                control_turn1_tokens = int(
                    round(statistics.mean(int(t.usage.first_turn_prompt_tokens or 0) for t in first_trials))
                )

        ctrl_outcome_map: dict[tuple[str, int], bool] = {
            (t.task_id, t.rep_index): t.passed
            for t in by_contender.get(control_cid or "", [])
        }

        leaderboard: list[LeaderboardSummary] = []
        for c in contenders:
            c_trials = by_contender.get(c.contender_id, [])
            by_task: dict[str, list[TrialResult]] = defaultdict(list)
            for t in c_trials:
                by_task[t.task_id].append(t)

            tasks_total = len(by_task)
            trials_total = len(c_trials)
            trials_passed = sum(1 for t in c_trials if t.passed)

            # Unbiased Pass@1: mean empirical pass rate across tasks
            pass_at_1 = (
                sum(sum(1 for r in t_list if r.passed) / len(t_list) for t_list in by_task.values())
                / max(1, tasks_total)
            )
            # Consistency Pass^k: fraction of tasks where ALL repetitions succeeded
            pass_hat_k = (
                sum(1.0 if all(r.passed for r in t_list) else 0.0 for t_list in by_task.values())
                / max(1, tasks_total)
            )

            arm_sim = c.cli.split(":", 1)[0] == "simulate" or any(t.is_simulated for t in c_trials)
            arm_metered = bool(c_trials) and all(t.usage.is_metered for t in c_trials)
            arm_costs_known = arm_metered and all(t.usage.total_cost_usd is not None for t in c_trials)
            if arm_metered:
                if arm_costs_known:
                    total_spend: float | None = round(
                        sum(float(t.usage.total_cost_usd or 0.0) for t in c_trials), 4
                    )
                    cost_per_success: float | None = (
                        round(total_spend / trials_passed, 4) if trials_passed > 0 else None
                    )
                else:
                    total_spend = None
                    cost_per_success = None
                mean_turn1: int | None = int(
                    round(statistics.mean(int(t.usage.first_turn_prompt_tokens or 0) for t in c_trials))
                )
                overhead_delta: int | None = (
                    (mean_turn1 - control_turn1_tokens) if control_turn1_tokens is not None else None
                )
            else:
                total_spend = None
                cost_per_success = None
                mean_turn1 = None
                overhead_delta = None

            median_lat = (
                int(statistics.median(t.usage.total_latency_ms for t in c_trials)) if c_trials else 0
            )
            integrity_rate = (
                (sum(1 for t in c_trials if t.anti_cheat_clean) / trials_total)
                if trials_total > 0
                else 1.0
            )

            if c.contender_id == control_cid or not ctrl_outcome_map or not c_trials:
                elo_rating = 1500
            else:
                wins = 0.0
                pairs = 0
                for t in c_trials:
                    key = (t.task_id, t.rep_index)
                    if key in ctrl_outcome_map:
                        pairs += 1
                        c_ok = ctrl_outcome_map[key]
                        if t.passed and not c_ok:
                            wins += 1.0
                        elif t.passed == c_ok:
                            wins += 0.5
                if pairs > 0:
                    score = (wins + 0.5) / (pairs + 1.0)
                    elo_rating = int(round(1500.0 + 400.0 * math.log10(score / max(1e-6, 1.0 - score))))
                else:
                    elo_rating = 1500

            skill_names = [Path(p).stem if Path(p).stem.upper() != "SKILL" else Path(p).parent.name for p in c.skill_paths]
            arm_status = "simulated" if arm_sim else ("metered" if arm_metered else "unmetered")
            arm_isolated = bool(c.isolate_home) and (
                arm_sim or not c_trials or all(t.usage.isolated_home_used for t in c_trials)
            )
            relay_cost_vals = [
                float(t.usage.relay_cost_usd)
                for t in c_trials
                if t.usage.relay_cost_usd is not None
            ]
            total_relay_spend = round(sum(relay_cost_vals), 4) if relay_cost_vals else None
            failure_counts = {kind: 0 for kind in FAILURE_KINDS}
            for t in c_trials:
                kind = effective_failure_kind(t)
                failure_counts[kind] = failure_counts.get(kind, 0) + 1
            leaderboard.append(
                LeaderboardSummary(
                    contender_id=c.contender_id,
                    label=c.label,
                    role=c.role,
                    cli=c.cli,
                    model=c.model,
                    skills_mounted=skill_names,
                    trials_total=trials_total,
                    trials_passed=trials_passed,
                    tasks_total=tasks_total,
                    pass_at_1=round(pass_at_1, 4),
                    pass_hat_k=round(pass_hat_k, 4),
                    k_reps=k_reps,
                    total_spend_usd=total_spend,
                    cost_per_success_usd=cost_per_success,
                    mean_first_turn_tokens=mean_turn1,
                    context_overhead_delta_tokens=overhead_delta,
                    median_latency_ms=median_lat,
                    anti_cheat_integrity_rate=round(integrity_rate, 4),
                    elo_rating=elo_rating,
                    rank=None,
                    isolate_home=arm_isolated,
                    is_metered=arm_metered,
                    usage_status=arm_status,
                    is_simulated=arm_sim,
                    metered_cost_usd=total_spend,
                    total_relay_cost_usd=total_relay_spend,
                    relay_cost_usd=total_relay_spend,
                    failure_counts=failure_counts,
                    pass_at_time=compute_pass_at_time(c_trials)[0],
                    agent_test_quality=summarize_agent_test_quality(c_trials),
                )
            )

        ctrl_summary = next((r for r in leaderboard if r.role == "control"), leaderboard[0] if leaderboard else None)
        treat_summaries = [r for r in leaderboard if r.role == "treatment"]
        if not treat_summaries and len(leaderboard) > 1:
            treat_summaries = [leaderboard[1]]
        ctrl_cid_for_stats = ctrl_summary.contender_id if ctrl_summary else None
        ctrl_stat_trials = by_contender.get(ctrl_cid_for_stats or "", [])
        comparisons: list[dict[str, Any]] = []
        for treat_summary in treat_summaries or [None]:
            treat_cid = treat_summary.contender_id if treat_summary else None
            comp = compare_arms(
                ctrl_cid_for_stats,
                treat_cid,
                ctrl_stat_trials,
                by_contender.get(treat_cid or "", []),
            )
            comp["treatment_label"] = treat_summary.label if treat_summary else None
            comp["control_label"] = ctrl_summary.label if ctrl_summary else None
            comp["is_simulated"] = exp_is_simulated
            if exp_is_simulated:
                comp["verdict_label_zh"] = f"[SIMULATED | 模拟] {comp['verdict_label_zh']}"
                comp["verdict_label_en"] = f"[SIMULATED] {comp['verdict_label_en']}"
            comparisons.append(comp)
        holm = holm_adjust([float(c["p_value"]) for c in comparisons])
        for comp, adj in zip(comparisons, holm):
            comp["p_value_holm"] = round(adj, 6)
            comp["holm_family_size"] = len(comparisons)
            comp["significant_holm_05"] = bool(adj < 0.05)
        stat_comp = comparisons[0]
        winner_id = stat_comp["winner"] if len(comparisons) == 1 else None
        if winner_id is not None:
            for row in leaderboard:
                row.rank = 1 if row.contender_id == winner_id else 2

        # Build per-task paired matrix for table & side-by-side Studio replay
        tasks_order: list[str] = []
        task_meta: dict[str, tuple[str, str]] = {}
        task_arm_trials: dict[tuple[str, str], list[TrialResult]] = defaultdict(list)
        for tr in trials:
            if tr.task_id not in task_meta:
                tasks_order.append(tr.task_id)
                task_meta[tr.task_id] = (tr.task_title, tr.task_category)
            task_arm_trials[(tr.task_id, tr.contender_id)].append(tr)

        task_matrix: list[dict[str, Any]] = []
        for tid in tasks_order:
            title, cat = task_meta[tid]
            arms_data: dict[str, Any] = {}
            for c in contenders:
                arm_trs = sorted(task_arm_trials.get((tid, c.contender_id), []), key=lambda x: x.rep_index)
                passed_n = sum(1 for x in arm_trs if x.passed)
                rep1 = arm_trs[0] if arm_trs else None
                arm_task_metered = bool(arm_trs) and all(x.usage.is_metered for x in arm_trs)
                arm_task_costs_known = arm_task_metered and all(
                    x.usage.total_cost_usd is not None for x in arm_trs
                )
                arm_task_sim = c.cli.split(":", 1)[0] == "simulate" or any(x.is_simulated for x in arm_trs)
                task_relay_vals = [
                    float(x.usage.relay_cost_usd)
                    for x in arm_trs
                    if x.usage.relay_cost_usd is not None
                ]
                arms_data[c.contender_id] = {
                    "passed_count": passed_n,
                    "total_reps": len(arm_trs),
                    "all_passed": bool(arm_trs) and passed_n == len(arm_trs),
                    "is_metered": arm_task_metered,
                    "usage_status": "simulated" if arm_task_sim else ("metered" if arm_task_metered else "unmetered"),
                    "is_simulated": arm_task_sim,
                    "mean_cost_usd": (
                        round(statistics.mean(float(x.usage.total_cost_usd or 0.0) for x in arm_trs), 4)
                        if arm_task_costs_known
                        else None
                    ),
                    "metered_cost_usd": (
                        round(sum(float(x.usage.total_cost_usd or 0.0) for x in arm_trs), 4)
                        if arm_task_costs_known
                        else None
                    ),
                    "mean_relay_cost_usd": (
                        round(statistics.mean(task_relay_vals), 4)
                        if task_relay_vals
                        else None
                    ),
                    "relay_cost_usd": (
                        round(sum(task_relay_vals), 4)
                        if task_relay_vals
                        else None
                    ),
                    "mean_tokens": (
                        int(
                            round(
                                statistics.mean(
                                    int(x.usage.total_prompt_tokens or 0) + int(x.usage.completion_tokens or 0)
                                    for x in arm_trs
                                )
                            )
                        )
                        if arm_task_metered
                        else None
                    ),
                    "mean_latency_ms": int(round(statistics.mean(x.usage.total_latency_ms for x in arm_trs))) if arm_trs else 0,
                    "rep1_trial": rep1.to_dict() if rep1 else None,
                }
            task_matrix.append(
                {
                    "task_id": tid,
                    "title": title,
                    "category": cat,
                    "arms": arms_data,
                }
            )

        disp_title = str(exp_row["title"])
        if exp_is_simulated and "SIMULATED" not in disp_title.upper():
            disp_title = f"[SIMULATED] {disp_title}"

        computed_cum_cost = round(
            sum(
                max(float(t.usage.total_cost_usd or 0.0), float(t.usage.relay_cost_usd or 0.0))
                for t in trials
            ),
            4,
        )
        raw_budget = exp_row["budget_usd"] if "budget_usd" in exp_keys else None
        raw_cum = exp_row["cumulative_cost_usd"] if "cumulative_cost_usd" in exp_keys else None
        budget_stopped = bool(exp_row["budget_stopped"]) if "budget_stopped" in exp_keys else False
        raw_planned = exp_row["planned_trials"] if "planned_trials" in exp_keys else None
        raw_completed = exp_row["completed_trials"] if "completed_trials" in exp_keys else None
        inferred_planned = max(len(trials), len(contenders) * max(1, len(tasks_order)) * k_reps)

        return {
            "experiment_id": exp_row["experiment_id"],
            "title": disp_title,
            "suite_name": exp_row["suite_name"],
            "k_reps": k_reps,
            "created_at": exp_row["created_at"],
            "is_simulated": exp_is_simulated,
            "budget_usd": round(float(raw_budget), 4) if raw_budget is not None else None,
            "cumulative_cost_usd": round(float(raw_cum), 4) if raw_cum is not None else computed_cum_cost,
            "budget_stopped": budget_stopped,
            "budget_halted": budget_stopped,
            "planned_trials": int(raw_planned) if raw_planned is not None else inferred_planned,
            "completed_trials": int(raw_completed) if raw_completed is not None else len(trials),
            "design": (
                json.loads(exp_row["design_json"]) if "design_json" in exp_keys and exp_row["design_json"] else {}
            ),
            "contenders": [c.to_dict() for c in contenders],
            "leaderboard": [row.to_dict() for row in leaderboard],
            "arms": [row.to_dict() for row in leaderboard],
            "trials": [tr.to_dict() for tr in trials],
            "statistical_comparison": stat_comp,
            "comparisons": comparisons,
            "task_matrix": task_matrix,
        }
