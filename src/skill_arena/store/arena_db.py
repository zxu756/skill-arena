"""SQLite storage and scientific A/B comparison calculator for SkillArena."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import sqlite3
import statistics
from typing import Any

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


def compute_newcombe_wilson_comparison(
    *,
    control_id: str | None,
    treatment_id: str | None,
    control_passed: int,
    control_trials: int,
    treatment_passed: int,
    treatment_trials: int,
) -> dict[str, Any]:
    """Newcombe Method 10 hybrid Wilson 95% CI on Pass@1 difference (p_treatment - p_control)."""
    n_c = max(0, int(control_trials))
    n_t = max(0, int(treatment_trials))
    k_c = max(0, min(int(control_passed), n_c))
    k_t = max(0, min(int(treatment_passed), n_t))
    p_c = (k_c / n_c) if n_c > 0 else 0.0
    p_t = (k_t / n_t) if n_t > 0 else 0.0
    delta = p_t - p_c

    l_c, u_c = compute_wilson_ci_95(k_c, n_c)
    l_t, u_t = compute_wilson_ci_95(k_t, n_t)
    ci_low = max(-1.0, delta - math.sqrt((p_t - l_t) ** 2 + (u_c - p_c) ** 2))
    ci_high = min(1.0, delta + math.sqrt((u_t - p_t) ** 2 + (p_c - l_c) ** 2))

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
        "method": "newcombe_wilson_95",
        "confidence_level": 0.95,
        "min_trials_per_arm": MIN_TRIALS_FOR_RANKING,
        "control_id": control_id,
        "treatment_id": treatment_id,
        "control_passed": k_c,
        "control_trials": n_c,
        "control_rate": round(p_c, 4),
        "treatment_passed": k_t,
        "treatment_trials": n_t,
        "treatment_rate": round(p_t, 4),
        "delta_pass_rate": round(delta, 4),
        "ci_low": round(ci_low, 4),
        "ci_high": round(ci_high, 4),
        "verdict": verdict,
        "winner": winner,
        "verdict_label_en": label_en,
        "verdict_label_zh": label_zh,
    }


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
                    is_simulated INTEGER NOT NULL DEFAULT 0
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
                "ALTER TABLE trials ADD COLUMN is_simulated INTEGER NOT NULL DEFAULT 0;",
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
                (experiment_id, title, suite_name, k_reps, contenders_json, created_at, is_simulated)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    experiment_id,
                    stored_title,
                    suite_name,
                    k_reps,
                    json.dumps([c.to_dict() for c in contenders]),
                    now,
                    1 if sim else 0,
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
                    git_diff, started_at, finished_at, is_simulated
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                sum(1 for t in c_trials if t.anti_cheat_clean) / max(1, trials_total)
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
                )
            )

        ctrl_summary = next((r for r in leaderboard if r.role == "control"), leaderboard[0] if leaderboard else None)
        treat_summary = next(
            (r for r in leaderboard if r.role == "treatment"),
            leaderboard[1] if len(leaderboard) > 1 else None,
        )
        stat_comp = compute_newcombe_wilson_comparison(
            control_id=ctrl_summary.contender_id if ctrl_summary else None,
            treatment_id=treat_summary.contender_id if treat_summary else None,
            control_passed=ctrl_summary.trials_passed if ctrl_summary else 0,
            control_trials=ctrl_summary.trials_total if ctrl_summary else 0,
            treatment_passed=treat_summary.trials_passed if treat_summary else 0,
            treatment_trials=treat_summary.trials_total if treat_summary else 0,
        )
        winner_id = stat_comp["winner"]
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

        return {
            "experiment_id": exp_row["experiment_id"],
            "title": disp_title,
            "suite_name": exp_row["suite_name"],
            "k_reps": k_reps,
            "created_at": exp_row["created_at"],
            "is_simulated": exp_is_simulated,
            "contenders": [c.to_dict() for c in contenders],
            "leaderboard": [row.to_dict() for row in leaderboard],
            "statistical_comparison": stat_comp,
            "task_matrix": task_matrix,
        }
