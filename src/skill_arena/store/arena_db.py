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
            if arm_metered:
                total_spend: float | None = round(sum(float(t.usage.total_cost_usd or 0.0) for t in c_trials), 4)
                cost_per_success: float | None = (
                    round(total_spend / trials_passed, 4) if trials_passed > 0 else None
                )
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
                    is_metered=arm_metered,
                    usage_status=arm_status,
                    is_simulated=arm_sim,
                )
            )

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
                        if arm_task_metered
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
            "task_matrix": task_matrix,
        }
