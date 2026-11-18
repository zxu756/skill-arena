"""Command-line interface for SkillArena: run, compare, studio, export-html, extract-commit, list-suites."""

from __future__ import annotations

import argparse
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
import json
from pathlib import Path
import random
import re
import subprocess
import sys
import uuid

from skill_arena import __version__
from skill_arena.extractor.git_commit_extractor import extract_task_from_commit
from skill_arena.graders.deterministic import verify_suite_runners_available
from skill_arena.i18n import (
    ROLE_ZH,
    SUPPORTED_LANG_CHOICES,
    localize_category,
    localize_task_title,
    pad_cell,
    resolve_lang,
)
from skill_arena.models import ContenderConfig, TaskSpec, TrialResult
from skill_arena.runners.harness_driver import (
    SUPPORTED_RELAY_CLIS,
    HarnessDriver,
    normalize_pass_env_names,
    validate_simulation_inputs,
)
from skill_arena.sandbox.worktree_pool import WorktreeSandboxPool
from skill_arena.store.arena_db import ArenaDatabase
from skill_arena.studio.server import create_studio_server
from skill_arena.studio.templates import render_arena_html
from skill_arena.suites.builtin_tasks import load_builtin_suite
from skill_arena.suites.pool_import import POOL_VARIANTS, export_pool

EXIT_BUDGET_EXCEEDED = 3


def _load_tasks(suite_arg: str) -> list[TaskSpec]:
    p = Path(suite_arg)
    if p.is_file():
        raw = json.loads(p.read_text(encoding="utf-8"))
        if isinstance(raw, list):
            return [TaskSpec.from_dict(item) for item in raw]
        return [TaskSpec.from_dict(raw)]
    if suite_arg in {"builtin", "all", "builtin-8"}:
        return load_builtin_suite()
    tasks = load_builtin_suite(category=suite_arg)
    if not tasks:
        raise ValueError(f"Unknown suite or category: {suite_arg}")
    return tasks


_ARM_LABEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.+-]{0,39}$")


def parse_arm_specs(raw_arms: list[str], *, simulate: bool) -> list[tuple[str, list[str]]]:
    """Parse repeatable ``--arm LABEL=PATH[,PATH...]`` values into ``(label, skill_paths)`` pairs."""
    arms: list[tuple[str, list[str]]] = []
    seen: set[str] = set()
    for raw in raw_arms:
        label, sep, paths_txt = str(raw).partition("=")
        label = label.strip()
        if not _ARM_LABEL_RE.match(label):
            raise ValueError(
                f"Invalid --arm label {label!r}: use 1-40 letters, digits, '_', '.', '+' or '-' "
                "(e.g. --arm tdd=/path/to/tdd-skill)."
            )
        if label.lower() == "control" or label.lower() in seen:
            raise ValueError(f"Duplicate or reserved --arm label {label!r}.")
        seen.add(label.lower())
        paths = [p.strip() for p in paths_txt.split(",") if p.strip()] if sep else []
        if simulate:
            if paths:
                raise ValueError("`--cli simulate` arms take a label only (`--arm NAME`), no skill paths.")
        else:
            if not paths:
                raise ValueError(f"--arm {label!r} needs at least one skill path (LABEL=PATH[,PATH...]).")
            missing = [p for p in paths if not Path(p).expanduser().exists()]
            if missing:
                raise ValueError(f"--arm {label!r}: skill path(s) not found: {', '.join(missing)}")
        arms.append((label, paths))
    return arms


DEFAULT_SCHEDULE_SEED = 20261118


def build_trial_schedule(
    tasks: list[TaskSpec],
    contenders: list[ContenderConfig],
    k_reps: int,
    seed: int = DEFAULT_SCHEDULE_SEED,
) -> list[tuple[TaskSpec, ContenderConfig, int]]:
    """Randomized, blocked launch order for every (task, rep, arm) trial.

    Each (task, rep) is a block holding every arm, so all arms meet the same conditions (time of day,
    provider load, rate limits) close together. Arm order is shuffled inside each block and the blocks
    themselves are shuffled, with one seeded generator so the order is reproducible.
    """
    rng = random.Random(int(seed))
    blocks: list[list[tuple[TaskSpec, ContenderConfig, int]]] = []
    for task in tasks:
        for rep in range(1, int(k_reps) + 1):
            arms = list(contenders)
            rng.shuffle(arms)
            blocks.append([(task, c, rep) for c in arms])
    rng.shuffle(blocks)
    return [trial for block in blocks for trial in block]


def run_ab_experiment(
    db: ArenaDatabase,
    title: str,
    suite_name: str,
    tasks: list[TaskSpec],
    contenders: list[ContenderConfig],
    k_reps: int = 3,
    experiment_id: str | None = None,
    quiet: bool = False,
    lang: str = "en",
    allow_unisolated_host_home: bool = False,
    isolate_home: bool = True,
    auth_copy_mode: bool = False,
    relay_script: str | Path | None = None,
    budget_usd: float | None = None,
    pass_env: list[str] | tuple[str, ...] | None = None,
    seed: int = DEFAULT_SCHEDULE_SEED,
    workers: int = 1,
) -> str:
    exp_id = experiment_id or f"exp_{uuid.uuid4().hex[:8]}"
    workers = max(1, int(workers))
    verify_suite_runners_available(tasks)
    is_sim_exp = any(c.cli.split(":", 1)[0] == "simulate" for c in contenders)
    schedule = build_trial_schedule(tasks, contenders, k_reps, seed)
    planned_trials = len(schedule)
    db.create_experiment(
        exp_id,
        title=title,
        suite_name=suite_name,
        k_reps=k_reps,
        contenders=contenders,
        is_simulated=is_sim_exp,
        budget_usd=budget_usd,
        planned_trials=planned_trials,
        design={
            "seed": int(seed),
            "workers": workers,
            "schedule": "blocked-randomized",
        },
    )

    is_zh = lang == "zh"
    if not quiet and is_sim_exp:
        if is_zh:
            print("[SIMULATED / 模拟数据] 合成管线校准运行 - 未调用任何真实模型或外部技能包。")
        else:
            print("[SIMULATED] Synthetic pipeline calibration run - no real model or external SKILL.md was invoked.")

    pool = WorktreeSandboxPool()
    driver = HarnessDriver(
        pool,
        allow_unisolated_host_home=allow_unisolated_host_home,
        isolate_home=isolate_home,
        auth_copy_mode=auth_copy_mode,
        relay_script=relay_script,
        budget_usd=budget_usd,
        pass_env=pass_env,
    )

    def _announce_budget_stop() -> None:
        if quiet:
            return
        ceil_val = float(budget_usd or 0.0)
        overran = driver.cumulative_cost_usd > ceil_val + 1e-9
        next_n = driver.completed_trials + 1
        if is_zh:
            reason_zh = (
                f"已超出总预算上限，在启动第 {next_n}/{planned_trials} 次试验前停止。"
                if overran
                else f"在启动第 {next_n}/{planned_trials} 次试验前停止以避免超出预算。"
            )
            print(f"[预算上限停止] 实验总预算上限 ${ceil_val:.4f}，当前累计花费 ${driver.cumulative_cost_usd:.4f}，{reason_zh}")
        else:
            reason_en = (
                f"budget already exceeded; halting before trial {next_n}/{planned_trials}."
                if overran
                else f"halting before trial {next_n}/{planned_trials} to avoid exceeding budget."
            )
            print(
                f"[BUDGET STOPPED] Experiment budget ceiling ${ceil_val:.4f} "
                f"(cumulative spend ${driver.cumulative_cost_usd:.4f}): {reason_en}"
            )

    def _report_trial(result: TrialResult) -> None:
        # Runs on the main thread only: database writes and console output are never concurrent.
        db.record_trial(result)
        if quiet:
            return
        cost_txt = (
            f"xray=${result.usage.total_cost_usd:.4f}"
            if (result.usage.is_metered and result.usage.total_cost_usd is not None)
            else ("xray=未计量" if is_zh else "xray=Unmetered")
        )
        relay_txt = (
            f"relay=${result.usage.relay_cost_usd:.4f}" if result.usage.relay_cost_usd is not None else "relay=n/a"
        )
        order_txt = f"#{result.launch_index + 1}"
        if is_zh:
            sim_tag = "[SIMULATED / 模拟数据] " if result.is_simulated else ""
            badge = "通过" if result.passed else "失败"
            print(
                f"{sim_tag}[{badge}] {order_txt} {result.task_id} | {result.contender_label} "
                f"(第 {result.rep_index}/{k_reps} 轮) | {cost_txt} | {relay_txt} | {result.usage.total_latency_ms}ms"
            )
        else:
            sim_tag = "[SIMULATED] " if result.is_simulated else ""
            badge = "PASS" if result.passed else "FAIL"
            print(
                f"{sim_tag}[{badge}] {order_txt} {result.task_id} | {result.contender_label} "
                f"(rep {result.rep_index}/{k_reps}) | {cost_txt} | {relay_txt} | {result.usage.total_latency_ms}ms"
            )

    executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="skill-arena-trial")
    running: set[Future[TrialResult]] = set()
    first_error: BaseException | None = None

    def _collect(done: set[Future[TrialResult]]) -> None:
        nonlocal first_error
        for fut in sorted(done, key=lambda f: getattr(f, "launch_index", 0)):
            running.discard(fut)
            try:
                _report_trial(fut.result())
            except BaseException as exc:  # noqa: BLE001 - re-raised after in-flight trials finish
                if first_error is None:
                    first_error = exc

    try:
        for launch_index, (task, contender, rep) in enumerate(schedule):
            while len(running) >= workers:
                done, _ = wait(running, return_when=FIRST_COMPLETED)
                _collect(done)
            if first_error is not None:
                break
            can_start, _rem_budget = driver.can_start_next_trial(in_flight=len(running))
            if not can_start:
                if running:
                    # Spend still in flight may free up (or use up) the budget: wait, then decide again.
                    done, _ = wait(running)
                    _collect(done)
                    can_start, _rem_budget = driver.can_start_next_trial(in_flight=0)
                if not can_start:
                    _announce_budget_stop()
                    break
            fut = executor.submit(driver.run_trial, exp_id, task, contender, rep, launch_index=launch_index)
            fut.launch_index = launch_index  # type: ignore[attr-defined]
            running.add(fut)
        if running:
            done, _ = wait(running)
            _collect(done)
        if first_error is not None:
            raise first_error
        if budget_usd is not None and driver.cumulative_cost_usd > float(budget_usd) + 1e-9:
            driver.budget_stopped = True
    finally:
        executor.shutdown(wait=True)
        db.update_experiment_budget_state(
            exp_id,
            budget_usd=budget_usd,
            cumulative_cost_usd=driver.cumulative_cost_usd,
            budget_stopped=driver.budget_stopped,
            planned_trials=planned_trials,
            completed_trials=driver.completed_trials,
        )
        pool.close()
    return exp_id


def _print_comparison_table(report: dict, lang: str = "en") -> None:
    is_zh = lang == "zh"
    k_reps = int(report.get("k_reps", 3))
    is_sim = bool(report.get("is_simulated"))
    budget_val = report.get("budget_usd")
    cum_cost_val = float(report.get("cumulative_cost_usd") or 0.0)
    budget_stopped = bool(report.get("budget_stopped"))
    planned_n = int(report.get("planned_trials") or 0)
    completed_n = int(report.get("completed_trials") or 0)
    if is_zh:
        if is_sim:
            print("\n[SIMULATED / 模拟数据] 提示：以下数字由内置合成模拟器生成，不代表任何真实模型或技能的表现。")
        if budget_stopped and budget_val is not None:
            overran = cum_cost_val > float(budget_val) + 1e-9
            suffix_zh = "单试次超支后立即停止" if overran else "在超出预算前停止"
            print(
                f"\n[预算上限停止] 实验总预算上限 ${float(budget_val):.4f}，累计花费 ${cum_cost_val:.4f}，"
                f"已完成 {completed_n}/{planned_n} 次试验（{suffix_zh}）。"
            )
        elif budget_val is not None:
            print(
                f"\n预算统计：实验总预算上限 ${float(budget_val):.4f}，累计花费 ${cum_cost_val:.4f}，"
                f"已完成 {completed_n}/{planned_n} 次试验。"
            )
        print(f"\n=== {report['title']}（实验编号：{report['experiment_id']}）===")
        hdr = (
            f"{pad_cell('参赛组别', 32)} {pad_cell('角色', 8)} "
            f"{pad_cell('胜率Elo', 8, 'right')} {pad_cell('Pass@1', 8, 'right')} "
            f"{pad_cell(f'Pass^{k_reps}', 8, 'right')} {pad_cell('代理总花费', 11, 'right')} "
            f"{pad_cell('Relay花费', 11, 'right')} {pad_cell('单次成功成本', 12, 'right')} "
            f"{pad_cell('首轮上下文税', 13, 'right')} {pad_cell('防作弊率', 9, 'right')}"
        )
        print(hdr)
        print("-" * 130)
        for row in report["leaderboard"]:
            metered = bool(row.get("is_metered", True))
            if not metered:
                xray_tot = "未计量"
                cps = "未计量"
                delta = "未计量"
            else:
                xray_tot = f"${row['total_spend_usd']:.4f}" if row.get("total_spend_usd") is not None else "n/a"
                cps = f"${row['cost_per_success_usd']:.4f}" if row["cost_per_success_usd"] is not None else "n/a"
                ovh_val = row.get("context_overhead_delta_tokens")
                delta = f"{int(ovh_val):+d} tok" if ovh_val is not None else "未计量"
            relay_tot = (
                f"${float(row['total_relay_cost_usd']):.4f}"
                if row.get("total_relay_cost_usd") is not None
                else "n/a"
            )
            role_disp = ROLE_ZH.get(row["role"], row["role"])
            elo_str = str(row.get("elo_rating", 1500))
            print(
                f"{pad_cell(str(row['label'])[:31], 32)} {pad_cell(role_disp, 8)} "
                f"{elo_str:>8} {row['pass_at_1'] * 100:7.1f}% "
                f"{row['pass_hat_k'] * 100:7.1f}% {pad_cell(xray_tot, 11, 'right')} "
                f"{pad_cell(relay_tot, 11, 'right')} {pad_cell(cps, 12, 'right')} "
                f"{pad_cell(delta, 13, 'right')} {row['anti_cheat_integrity_rate'] * 100:8.1f}%"
            )
        trial_rows = list(report.get("trials") or [])
        if trial_rows:
            print("\n--- 逐试次费用台账（agent-xray 代理花费 vs Relay LOG.md 花费）---")
            print(
                f"{pad_cell('任务ID', 28)} {pad_cell('角色', 8)} {pad_cell('轮次', 6, 'right')} "
                f"{pad_cell('结果', 6)} {pad_cell('代理花费', 11, 'right')} {pad_cell('Relay花费', 11, 'right')}"
            )
            for tr in trial_rows:
                u = tr.get("usage") or {}
                t_metered = bool(u.get("is_metered", False))
                t_xray = (
                    f"${float(u['total_cost_usd']):.4f}"
                    if (t_metered and u.get("total_cost_usd") is not None)
                    else ("未计量" if not t_metered else "n/a")
                )
                t_relay = (
                    f"${float(u['relay_cost_usd']):.4f}"
                    if u.get("relay_cost_usd") is not None
                    else "n/a"
                )
                t_role = ROLE_ZH.get(str(tr.get("contender_role", "")), str(tr.get("contender_role", "")))
                t_res = "通过" if tr.get("passed") else "失败"
                print(
                    f"{pad_cell(str(tr.get('task_id', ''))[:27], 28)} {pad_cell(t_role, 8)} "
                    f"{int(tr.get('rep_index', 1)):>6d} {pad_cell(t_res, 6)} "
                    f"{pad_cell(t_xray, 11, 'right')} {pad_cell(t_relay, 11, 'right')}"
                )
        stat_comp = report.get("statistical_comparison") or {}
        if stat_comp:
            delta_pp = float(stat_comp.get("delta_pass_rate", 0.0)) * 100.0
            ci_l_pp = float(stat_comp.get("ci_low", 0.0)) * 100.0
            ci_h_pp = float(stat_comp.get("ci_high", 0.0)) * 100.0
            print(
                f"统计结论：{stat_comp.get('verdict_label_zh', '看不出差别')} | "
                f"Pass@1 差值 {delta_pp:+.1f} pp，95% 置信区间（按题配对）[{ci_l_pp:+.1f} pp, {ci_h_pp:+.1f} pp]"
            )
            mde_pp = float(stat_comp.get("min_detectable_effect_80", 0.0)) * 100.0
            int_c = float(stat_comp.get("control_interrupted_fraction", 0.0)) * 100.0
            int_t = float(stat_comp.get("treatment_interrupted_fraction", 0.0)) * 100.0
            print(
                f"统计说明：配对 {int(stat_comp.get('tasks_paired', 0))} 道题；这次实验能可靠看出的最小差别约 {mde_pp:.1f} pp"
                f"（80% 把握）；被时限打断：对照组 {int_c:.0f}%、实验组 {int_t:.0f}%"
            )
        print("指标说明：胜率Elo = 成对胜率换算的 Elo（双组）；首轮上下文税 = 实验组与对照组首轮输入 Token 均值之差。")
        _print_design_analysis(report, lang=lang)
        return

    if is_sim:
        print("\n[SIMULATED] Notice: Synthetic calibration numbers only - does not reflect any real model or skill.")
    if budget_stopped and budget_val is not None:
        if cum_cost_val > float(budget_val) + 1e-9:
            print(
                f"\n[BUDGET STOPPED] Budget ceiling ${float(budget_val):.4f} exceeded "
                f"(cumulative spend ${cum_cost_val:.4f}): halted after {completed_n}/{planned_n} planned trials."
            )
        else:
            print(
                f"\n[BUDGET STOPPED] Budget ceiling ${float(budget_val):.4f} reached before next trial: "
                f"cumulative spend ${cum_cost_val:.4f}, completed {completed_n}/{planned_n} planned trials."
            )
    elif budget_val is not None:
        print(
            f"\nBudget ceiling: ${float(budget_val):.4f} | cumulative spend ${cum_cost_val:.4f} | "
            f"completed {completed_n}/{planned_n} trials."
        )
    print(f"\n=== {report['title']} ({report['experiment_id']}) ===")
    print(
        f"{'Contender':<32} {'Role':<10} {'WR Elo':>7} {'Pass@1':>8} {f'Pass^{k_reps}':>8} "
        f"{'XRay Spend':>11} {'Relay LOG':>11} {'Cost/Succ':>11} {'Turn-1 Tax':>13} {'Integrity':>10}"
    )
    print("-" * 130)
    for row in report["leaderboard"]:
        metered = bool(row.get("is_metered", True))
        if not metered:
            xray_tot = "Unmetered"
            cps = "Unmetered"
            delta = "Unmetered"
        else:
            xray_tot = f"${row['total_spend_usd']:.4f}" if row.get("total_spend_usd") is not None else "n/a"
            cps = f"${row['cost_per_success_usd']:.4f}" if row["cost_per_success_usd"] is not None else "n/a"
            ovh_val = row.get("context_overhead_delta_tokens")
            delta = f"{int(ovh_val):+d} tok" if ovh_val is not None else "Unmetered"
        relay_tot = (
            f"${float(row['total_relay_cost_usd']):.4f}"
            if row.get("total_relay_cost_usd") is not None
            else "n/a"
        )
        elo_str = str(row.get("elo_rating", 1500))
        print(
            f"{row['label'][:31]:<32} {row['role']:<10} {elo_str:>7} "
            f"{row['pass_at_1'] * 100:7.1f}% {row['pass_hat_k'] * 100:7.1f}% "
            f"{xray_tot:>11} {relay_tot:>11} {cps:>11} {delta:>13} {row['anti_cheat_integrity_rate'] * 100:9.1f}%"
        )
    trial_rows = list(report.get("trials") or [])
    if trial_rows:
        print("\n--- Per-Trial Cost Ledger (agent-xray wire spend vs Relay LOG.md step spend) ---")
        print(
            f"{'Task ID':<28} {'Role':<10} {'Rep':>4} {'Result':<6} {'XRay Spend':>11} {'Relay LOG':>11}"
        )
        for tr in trial_rows:
            u = tr.get("usage") or {}
            t_metered = bool(u.get("is_metered", False))
            t_xray = (
                f"${float(u['total_cost_usd']):.4f}"
                if (t_metered and u.get("total_cost_usd") is not None)
                else ("Unmetered" if not t_metered else "n/a")
            )
            t_relay = (
                f"${float(u['relay_cost_usd']):.4f}"
                if u.get("relay_cost_usd") is not None
                else "n/a"
            )
            t_res = "PASS" if tr.get("passed") else "FAIL"
            print(
                f"{str(tr.get('task_id', ''))[:27]:<28} {str(tr.get('contender_role', '')):<10} "
                f"{int(tr.get('rep_index', 1)):>4d} {t_res:<6} {t_xray:>11} {t_relay:>11}"
            )
    stat_comp = report.get("statistical_comparison") or {}
    if stat_comp:
        delta_pp = float(stat_comp.get("delta_pass_rate", 0.0)) * 100.0
        ci_l_pp = float(stat_comp.get("ci_low", 0.0)) * 100.0
        ci_h_pp = float(stat_comp.get("ci_high", 0.0)) * 100.0
        print(
            f"Verdict: {stat_comp.get('verdict_label_en', 'Inconclusive')} | "
            f"Pass@1 delta {delta_pp:+.1f} pp, 95% task-paired CI [{ci_l_pp:+.1f} pp, {ci_h_pp:+.1f} pp]"
        )
        mde_pp = float(stat_comp.get("min_detectable_effect_80", 0.0)) * 100.0
        int_c = float(stat_comp.get("control_interrupted_fraction", 0.0)) * 100.0
        int_t = float(stat_comp.get("treatment_interrupted_fraction", 0.0)) * 100.0
        print(
            f"Stats: {int(stat_comp.get('tasks_paired', 0))} paired tasks; minimum detectable effect ~{mde_pp:.1f} pp "
            f"(80% power); hit time limit: control {int_c:.0f}%, treatment {int_t:.0f}%"
        )
    print(
        "Metrics: WR Elo = Win-Rate Derived Elo (2-arm); "
        "Turn-1 Tax = Mean Turn-1 input token delta (treatment minus baseline)."
    )
    _print_design_analysis(report, lang=lang)


FAILURE_KIND_ZH = {
    "passed": "通过",
    "wrong": "答错",
    "interrupted": "超时打断",
    "anti_cheat": "违规",
    "craft": "工艺不合格",
    "infra": "环境故障",
    "grader_error": "判分出错",
}


def _print_design_analysis(report: dict, lang: str = "en") -> None:
    """Failure categories and the other per-arm experiment-design metrics."""
    is_zh = lang == "zh"
    rows = list(report.get("leaderboard") or [])
    if not rows:
        return
    comparisons = list(report.get("comparisons") or [])
    if comparisons:
        print(
            "\n--- 各实验组对比对照组（按题配对；置换检验 p 值，Holm 校正）---"
            if is_zh
            else "\n--- Each treatment vs control (task-paired; sign-flip permutation p, Holm-adjusted) ---"
        )
        for comp in comparisons:
            lbl = str(comp.get("treatment_label") or comp.get("treatment_id") or "")[:48]
            d_pp = float(comp.get("delta_pass_rate", 0.0)) * 100.0
            lo_pp = float(comp.get("ci_low", 0.0)) * 100.0
            hi_pp = float(comp.get("ci_high", 0.0)) * 100.0
            p_raw = float(comp.get("p_value", 1.0))
            p_holm = float(comp.get("p_value_holm", p_raw))
            verdict = comp.get("verdict_label_zh" if is_zh else "verdict_label_en", "")
            if is_zh:
                print(
                    f"{lbl}: 差值 {d_pp:+.1f} pp，95% 区间 [{lo_pp:+.1f}, {hi_pp:+.1f}] pp，"
                    f"p={p_raw:.4f}，Holm p={p_holm:.4f}（{int(comp.get('tasks_paired', 0))} 道题）| {verdict}"
                )
            else:
                print(
                    f"{lbl}: delta {d_pp:+.1f} pp, 95% CI [{lo_pp:+.1f}, {hi_pp:+.1f}] pp, "
                    f"p={p_raw:.4f}, Holm p={p_holm:.4f} ({int(comp.get('tasks_paired', 0))} tasks) | {verdict}"
                )
    print("\n--- 失败原因（每组试验数）---" if is_zh else "\n--- Failure categories (trials per arm) ---")
    kinds = list(FAILURE_KIND_ZH)
    for row in rows:
        counts = dict(row.get("failure_counts") or {})
        parts = [
            f"{FAILURE_KIND_ZH[k] if is_zh else k} {int(counts.get(k, 0))}" for k in kinds if int(counts.get(k, 0))
        ]
        label = str(row.get("label", ""))[:40]
        print(f"{label}: {', '.join(parts) if parts else ('无试验' if is_zh else 'no trials')}")

    print(
        "\n--- 时限曲线 Pass@T（通过且在 T 秒内结束）---"
        if is_zh
        else "\n--- Time-limit curve Pass@T (passed and agent finished within T seconds) ---"
    )
    for row in rows:
        curve = list(row.get("pass_at_time") or [])
        cells = []
        for pt in curve:
            t_sec = pt.get("t_sec")
            t_txt = (f"{t_sec}s" if t_sec is not None else ("时限" if is_zh else "limit"))
            if pt.get("is_full_limit") and t_sec is not None:
                t_txt += "（时限）" if is_zh else " (limit)"
            cells.append(f"{t_txt} {float(pt.get('pass_rate', 0.0)) * 100:.1f}%")
        print(f"{str(row.get('label', ''))[:40]}: {' | '.join(cells)}")

    print(
        "\n--- 智能体自写测试质量（变异得分）---" if is_zh else "\n--- Agent-written tests (mutation score) ---"
    )
    for row in rows:
        q = dict(row.get("agent_test_quality") or {})
        mean = q.get("mean_mutation_score")
        valid_rate = q.get("valid_rate")
        mean_txt = f"{float(mean) * 100:.1f}%" if mean is not None else "n/a"
        valid_txt = f"{float(valid_rate) * 100:.0f}%" if valid_rate is not None else "n/a"
        if is_zh:
            print(
                f"{str(row.get('label', ''))[:40]}: 写了测试 {int(q.get('trials_with_tests', 0))}/{int(q.get('trials', 0))} 次，"
                f"在参考实现上有效 {valid_txt}，平均变异得分 {mean_txt}"
                f"（杀死 {int(q.get('mutants_killed', 0))}/{int(q.get('mutants_total', 0))} 个变异体）"
            )
        else:
            print(
                f"{str(row.get('label', ''))[:40]}: wrote tests in {int(q.get('trials_with_tests', 0))}/{int(q.get('trials', 0))} trials, "
                f"valid on reference {valid_txt}, mean mutation score {mean_txt} "
                f"(killed {int(q.get('mutants_killed', 0))}/{int(q.get('mutants_total', 0))} mutants)"
            )

    print("\n--- 行为指标（来自工具调用记录）---" if is_zh else "\n--- Behaviour (from the agent's tool calls) ---")
    for row in rows:
        b = dict(row.get("behavior_summary") or {})
        lbl = str(row.get("label", ""))[:40]
        if not b.get("trials_observed"):
            src = "; ".join(b.get("sources") or []) or "n/a"
            print(f"{lbl}: {'无法观测' if is_zh else 'not observable'} ({src})")
            continue
        runs = b.get("mean_test_runs")
        tf = b.get("test_first_rate")
        sl = b.get("skill_loaded_rate")
        tf_txt = f"{float(tf) * 100:.0f}%" if tf is not None else "n/a"
        sl_txt = f"{float(sl) * 100:.0f}%" if sl is not None else "n/a"
        if is_zh:
            print(
                f"{lbl}: 平均跑测试 {float(runs):.1f} 次，先写测试 {tf_txt}，读了 SKILL.md {sl_txt}"
                f"（{int(b.get('trials_observed', 0))}/{int(b.get('trials', 0))} 次试验可观测）"
            )
        else:
            print(
                f"{lbl}: mean test runs {float(runs):.1f}, test first {tf_txt}, read SKILL.md {sl_txt} "
                f"({int(b.get('trials_observed', 0))}/{int(b.get('trials', 0))} trials observable)"
            )


def _add_subparser_lang_arg(subparser: argparse.ArgumentParser) -> None:
    subparser.add_argument(
        "--lang",
        choices=SUPPORTED_LANG_CHOICES,
        default=argparse.SUPPRESS,
        help="Output & HTML default language: auto (SKILL_ARENA_LANG / LANG), zh, or en.",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="skill-arena",
        description="Controlled Git-worktree A/B benchmark harness for coding agents and SKILL.md rules.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument(
        "--lang",
        choices=SUPPORTED_LANG_CHOICES,
        default="auto",
        help="Output & HTML default language: auto (SKILL_ARENA_LANG / LANG), zh, or en.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_list = sub.add_parser("list-suites", help="List built-in calibration benchmark tasks.")
    _add_subparser_lang_arg(p_list)
    p_list.add_argument("--category", default=None, help="Optional category filter.")
    p_list.add_argument("--json", action="store_true", help="Emit JSON output.")

    p_run = sub.add_parser("run", help="Run a controlled Control vs Treatment A/B benchmark.")
    _add_subparser_lang_arg(p_run)
    p_run.add_argument("--suite", default="builtin-8", help="Suite name, category, or TaskSpec JSON file.")
    p_run.add_argument(
        "--cli",
        default="simulate",
        help="Agent CLI (simulate, or via agent-relay: claude, codex, vibe, agy).",
    )
    p_run.add_argument(
        "--model",
        default=None,
        help="Model identifier (defaults to synthetic-sim-model for simulate, claude-sonnet-4-5 for real CLIs).",
    )
    p_run.add_argument("--skill", action="append", default=[], help="Path to SKILL.md or skill folder for Treatment.")
    p_run.add_argument(
        "--arm",
        action="append",
        default=[],
        metavar="LABEL=PATH[,PATH...]",
        help=(
            "Add a treatment arm named LABEL that mounts these skills (repeatable; the no-skill control is always "
            "present). Each arm is compared with the control, with Holm-adjusted permutation p-values."
        ),
    )
    p_run.add_argument("--reps", type=int, default=3, help="Isolated repetitions k per task (default: 3).")
    p_run.add_argument(
        "--seed",
        type=int,
        default=DEFAULT_SCHEDULE_SEED,
        help=f"Seed for the randomized, blocked trial order (default: {DEFAULT_SCHEDULE_SEED}).",
    )
    p_run.add_argument(
        "--workers",
        type=int,
        default=1,
        help="Trials to run at the same time (default: 1). Each trial keeps its own sandbox and HOME.",
    )
    p_run.add_argument("--title", default=None, help="Experiment title.")
    p_run.add_argument("--db", default=".skill-arena/arena.sqlite", help="SQLite database path.")
    p_run.add_argument("--html-out", default=None, help="Optional path to export single-file HTML report.")
    p_run.add_argument(
        "--relay-script",
        default=None,
        help="Optional path to agent-relay `relay.sh` (falls back to SKILL_ARENA_RELAY_SCRIPT or ~/.agents/skills/agent-relay/scripts/relay.sh).",
    )
    p_run.add_argument(
        "--budget-usd",
        type=float,
        default=None,
        help="Experiment-wide cumulative USD ceiling across all trials (tracks cumulative spend and halts before exceeding budget).",
    )
    p_run.add_argument(
        "--copy-auth",
        action="store_true",
        help="Read-only copy minimal credential files into the per-trial isolated HOME (default: False).",
    )
    p_run.add_argument(
        "--pass-env",
        action="append",
        default=[],
        metavar="VAR",
        help=(
            "Explicitly forward named host environment variable(s) (e.g. OPENAI_API_KEY) into trial subprocesses; "
            "repeatable or comma-separated (default: zero provider API key variables forwarded)."
        ),
    )
    p_run.add_argument(
        "--host-home",
        action="store_true",
        help="Disable per-trial isolated HOME and run inside the real host HOME (requires --allow-unisolated-host-home).",
    )
    p_run.add_argument(
        "--allow-unisolated-host-home",
        action="store_true",
        help="Explicitly consent to running an agent CLI in the unisolated host HOME (`--host-home`).",
    )

    p_comp = sub.add_parser("compare", help="Print leaderboard summary for an existing experiment.")
    _add_subparser_lang_arg(p_comp)
    p_comp.add_argument("--db", default=".skill-arena/arena.sqlite", help="SQLite database path.")
    p_comp.add_argument("--exp", default=None, help="Experiment ID (defaults to latest).")
    p_comp.add_argument("--json", action="store_true", help="Emit full JSON report.")

    p_html = sub.add_parser("export-html", help="Export single-file Hallmark HTML scoreboard & replay.")
    _add_subparser_lang_arg(p_html)
    p_html.add_argument("--db", default=".skill-arena/arena.sqlite", help="SQLite database path.")
    p_html.add_argument("--exp", default=None, help="Experiment ID (defaults to latest).")
    p_html.add_argument("--out", required=True, help="Destination HTML path.")
    p_html.add_argument("--default-view", choices=["scoreboard", "replay"], default="scoreboard")

    p_ext = sub.add_parser(
        "extract-commit",
        help="Extract a TaskSpec JSON from a git bug-fix commit without commit-message hints.",
    )
    _add_subparser_lang_arg(p_ext)
    p_ext.add_argument("--repo", default=".", help="Path to git repository.")
    p_ext.add_argument("--commit", default="HEAD", help="Git commit SHA or ref.")
    p_ext.add_argument("--test-cmd", default=None, help="Explicit test command override.")
    p_ext.add_argument("--out", default=None, help="Optional path to save extracted TaskSpec JSON.")

    p_pool = sub.add_parser(
        "pool-export",
        help="Convert a task pool directory (task.json, start/, tests/, reference/, mutants/) into TaskSpec JSON.",
    )
    _add_subparser_lang_arg(p_pool)
    p_pool.add_argument("--pool", required=True, help="Pool directory holding one sub-directory per task.")
    p_pool.add_argument(
        "--variant",
        action="append",
        choices=[*POOL_VARIANTS, "both"],
        default=None,
        help="visible (tests in the workspace) or hidden (tests only at grading time); repeatable, or 'both'.",
    )
    p_pool.add_argument("--task", action="append", default=[], metavar="ID", help="Only export these task ids.")
    p_pool.add_argument("--timeout-sec", type=int, default=None, help="Per-task agent time limit (default 900).")
    p_pool.add_argument("--out", required=True, help="Destination suite JSON (a list of TaskSpecs).")

    p_studio = sub.add_parser("studio", help="Start interactive SkillArena Web Studio.")
    _add_subparser_lang_arg(p_studio)
    p_studio.add_argument("--db", default=".skill-arena/arena.sqlite", help="SQLite database path.")
    p_studio.add_argument("--host", default="127.0.0.1", help="Bind host.")
    p_studio.add_argument("--port", type=int, default=8765, help="Bind port.")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    raw_lang = getattr(args, "lang", "auto")
    lang = resolve_lang(raw_lang)
    is_zh = lang == "zh"

    if args.command == "list-suites":
        tasks = load_builtin_suite(category=args.category)
        if args.json:
            print(json.dumps([t.to_dict() for t in tasks], indent=2))
        else:
            for t in tasks:
                cat_txt = localize_category(t.category, lang)
                title_txt = localize_task_title(t.task_id, t.title, lang)
                print(f"{t.task_id:<34} [{pad_cell(cat_txt, 16)}] {title_txt}")
        return 0

    if args.command == "run":
        if int(args.workers) < 1 or int(args.reps) < 1:
            print(
                "错误：`--workers` 与 `--reps` 必须至少为 1。" if is_zh else "Error: `--workers` and `--reps` must be at least 1.",
                file=sys.stderr,
            )
            return 2
        if args.budget_usd is not None and float(args.budget_usd) <= 0.0:
            msg = (
                f"错误：`--budget-usd` 必须大于 0（收到 {args.budget_usd}）。"
                if is_zh
                else f"Error: `--budget-usd` must be positive (got {args.budget_usd})."
            )
            print(msg, file=sys.stderr)
            return 2
        raw_cli = str(args.cli).strip()
        base_cli = raw_cli.split(":", 1)[0]
        if base_cli == "mock":
            msg = (
                "错误：`--cli mock` 已移除并更名为 `--cli simulate`（输出均带 [SIMULATED / 模拟数据] 标记）。"
                if is_zh
                else "Error: `--cli mock` has been removed and renamed to `--cli simulate` (all outputs carry [SIMULATED] markers)."
            )
            print(msg, file=sys.stderr)
            return 2

        is_sim_cli = base_cli == "simulate"
        isolate_home = not bool(args.host_home)
        if is_sim_cli:
            eff_model = args.model or "synthetic-sim-model"
            eff_title = args.title or "[SIMULATED] Synthetic Pipeline Calibration"
            try:
                validate_simulation_inputs(
                    model=eff_model,
                    title=eff_title,
                    skill_paths=list(args.skill),
                )
            except ValueError as exc:
                print(f"{'错误' if is_zh else 'Error'}: {exc}", file=sys.stderr)
                return 2
            ctrl_cli = "simulate:baseline" if raw_cli == "simulate" else raw_cli
            treat_cli = "simulate:skilled" if raw_cli == "simulate" else raw_cli
            ctrl_label = f"[SIMULATED] {eff_model} (Baseline · No Skill)"
            treat_label = f"[SIMULATED] {eff_model} (+ Synthetic Skill Pack)"
        else:
            if base_cli not in SUPPORTED_RELAY_CLIS:
                supported_str = ", ".join(sorted(SUPPORTED_RELAY_CLIS))
                msg = (
                    f"错误：不支持的 CLI '{raw_cli}'。真实智能体执行统一通过 agent-relay (`relay.sh`) 调度，仅支持：{supported_str}。"
                    if is_zh
                    else (
                        f"Error: Unsupported CLI '{raw_cli}'. Real agent execution routes exclusively through "
                        f"agent-relay (`relay.sh`), which supports: {supported_str}."
                    )
                )
                print(msg, file=sys.stderr)
                return 2
            if not isolate_home and not args.allow_unisolated_host_home:
                msg = (
                    f"错误：指定 `--host-home` 时必须同时传入 `--allow-unisolated-host-home`，已拒绝在真实宿主 HOME 目录启动 '{raw_cli}'。"
                    if is_zh
                    else (
                        f"Error: Refusing to launch '{raw_cli}' with `--host-home` in the real host HOME directory "
                        "without `--allow-unisolated-host-home`."
                    )
                )
                print(msg, file=sys.stderr)
                return 2
            eff_model = args.model or "claude-sonnet-4-5"
            eff_title = args.title or "Control vs SKILL.md A/B Evaluation"
            ctrl_cli = raw_cli
            treat_cli = raw_cli
            skill_label = (
                "+ "
                + ", ".join(
                    Path(s).stem if Path(s).stem.upper() != "SKILL" else Path(s).parent.name
                    for s in args.skill
                )
                if args.skill
                else "· Treatment No Skill Mounted"
            )
            ctrl_label = f"{raw_cli} ({eff_model} · No Skill)"
            treat_label = f"{raw_cli} ({eff_model} {skill_label})"

        treatment_arms: list[tuple[str, str, str, list[str]]] = [("arm_treatment", treat_label, treat_cli, list(args.skill))]
        if args.arm:
            try:
                if args.skill:
                    raise ValueError("Use either `--skill` (one treatment) or `--arm LABEL=PATH` (one or more), not both.")
                parsed_arms = parse_arm_specs(list(args.arm), simulate=is_sim_cli)
                treatment_arms = []
                for arm_label, arm_paths in parsed_arms:
                    if is_sim_cli:
                        full_label = f"[SIMULATED] {eff_model} (+ Synthetic {arm_label})"
                        validate_simulation_inputs(label=full_label)
                    else:
                        full_label = f"{raw_cli} ({eff_model} + {arm_label})"
                    treatment_arms.append((f"arm_{arm_label}", full_label, treat_cli, arm_paths))
            except ValueError as exc:
                print(f"{'错误' if is_zh else 'Error'}: {exc}", file=sys.stderr)
                return 2

        try:
            validated_pass_env = list(normalize_pass_env_names(args.pass_env))
            tasks = _load_tasks(args.suite)
            verify_suite_runners_available(tasks)
            db = ArenaDatabase(args.db)
            contenders = [
                ContenderConfig(
                    contender_id="arm_control",
                    label=ctrl_label,
                    role="control",
                    cli=ctrl_cli,
                    model=eff_model,
                    skill_paths=[],
                    strip_default_skills=True,
                    isolate_home=isolate_home,
                    auth_copy_mode=bool(args.copy_auth),
                    allow_unisolated_host_home=bool(args.allow_unisolated_host_home),
                    pass_env=validated_pass_env,
                ),
            ]
            for arm_id, arm_label, arm_cli, arm_paths in treatment_arms:
                contenders.append(
                    ContenderConfig(
                        contender_id=arm_id,
                        label=arm_label,
                        role="treatment",
                        cli=arm_cli,
                        model=eff_model,
                        skill_paths=list(arm_paths),
                        strip_default_skills=True,
                        isolate_home=isolate_home,
                        auth_copy_mode=bool(args.copy_auth),
                        allow_unisolated_host_home=bool(args.allow_unisolated_host_home),
                        pass_env=validated_pass_env,
                    )
                )
            exp_id = run_ab_experiment(
                db=db,
                title=eff_title,
                suite_name=args.suite,
                tasks=tasks,
                contenders=contenders,
                k_reps=args.reps,
                lang=lang,
                allow_unisolated_host_home=bool(args.allow_unisolated_host_home),
                isolate_home=isolate_home,
                auth_copy_mode=bool(args.copy_auth),
                relay_script=args.relay_script,
                budget_usd=args.budget_usd,
                pass_env=validated_pass_env,
                seed=int(args.seed),
                workers=int(args.workers),
            )
        except (ValueError, PermissionError, RuntimeError) as exc:
            print(f"{'错误' if is_zh else 'Error'}: {exc}", file=sys.stderr)
            return 2

        report = db.build_comparison_report(exp_id)
        _print_comparison_table(report, lang=lang)
        if args.html_out:
            out_path = Path(args.html_out)
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(render_arena_html(report, lang=raw_lang), encoding="utf-8")
            if is_zh:
                print(f"\n已导出单文件 HTML 评测报告 -> {out_path}")
            else:
                print(f"\nExported HTML report -> {out_path}")
        if bool(report.get("budget_stopped")):
            return EXIT_BUDGET_EXCEEDED
        return 0

    if args.command == "compare":
        db = ArenaDatabase(args.db)
        exp_id = args.exp or db.latest_experiment_id()
        if not exp_id:
            msg = "数据库中未找到实验记录。" if is_zh else "No experiments found in database."
            print(msg, file=sys.stderr)
            return 1
        report = db.build_comparison_report(exp_id)
        if args.json:
            print(json.dumps(report, indent=2))
        else:
            _print_comparison_table(report, lang=lang)
        return 0

    if args.command == "export-html":
        db = ArenaDatabase(args.db)
        exp_id = args.exp or db.latest_experiment_id()
        if not exp_id:
            msg = "数据库中未找到实验记录。" if is_zh else "No experiments found in database."
            print(msg, file=sys.stderr)
            return 1
        report = db.build_comparison_report(exp_id)
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(
            render_arena_html(report, default_view=args.default_view, lang=raw_lang),
            encoding="utf-8",
        )
        if is_zh:
            print(f"已导出单文件 HTML 评测报告 -> {out_path}")
        else:
            print(f"Exported single-file HTML report -> {out_path}")
        return 0

    if args.command == "extract-commit":
        try:
            spec = extract_task_from_commit(
                Path(args.repo),
                commit_ref=args.commit,
                test_command=args.test_cmd,
            )
        except (ValueError, subprocess.CalledProcessError) as exc:
            err_detail = str(exc)
            if is_zh:
                print(f"提取 Git 提交失败：{err_detail}", file=sys.stderr)
            else:
                print(f"Error extracting commit: {err_detail}", file=sys.stderr)
            return 2
        rendered = json.dumps(spec.to_dict(), indent=2)
        if args.out:
            out_path = Path(args.out)
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(rendered + "\n", encoding="utf-8")
            if is_zh:
                print(f"已从 Git 提交提取基准任务 {spec.task_id} -> {out_path}")
            else:
                print(f"Extracted TaskSpec {spec.task_id} -> {out_path}")
        else:
            print(rendered)
        return 0

    if args.command == "pool-export":
        raw_variants = args.variant or ["visible"]
        variants: list[str] = []
        for v in raw_variants:
            for item in (POOL_VARIANTS if v == "both" else (v,)):
                if item not in variants:
                    variants.append(item)
        if args.timeout_sec is not None and int(args.timeout_sec) <= 0:
            print(
                "错误：`--timeout-sec` 必须大于 0。" if is_zh else "Error: `--timeout-sec` must be positive.",
                file=sys.stderr,
            )
            return 2
        try:
            specs = export_pool(
                Path(args.pool),
                variants,
                task_ids=list(args.task) or None,
                timeout_sec=args.timeout_sec,
            )
        except (ValueError, OSError, UnicodeDecodeError) as exc:
            print(f"{'错误' if is_zh else 'Error'}: {exc}", file=sys.stderr)
            return 2
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps([s.to_dict() for s in specs], indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        if is_zh:
            print(f"已导出 {len(specs)} 道题（{', '.join(variants)}）-> {out_path}")
        else:
            print(f"Exported {len(specs)} TaskSpecs ({', '.join(variants)}) -> {out_path}")
        return 0

    if args.command == "studio":
        db = ArenaDatabase(args.db)
        server = create_studio_server(db, host=args.host, port=args.port, default_lang=raw_lang)
        if is_zh:
            print(f"SkillArena Studio 已启动监听：http://{args.host}:{args.port}")
        else:
            print(f"SkillArena Studio listening on http://{args.host}:{args.port}")
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            server.server_close()
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
