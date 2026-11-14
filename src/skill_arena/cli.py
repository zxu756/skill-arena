"""Command-line interface for SkillArena: run, compare, studio, export-html, extract-commit, list-suites."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys
import uuid

from skill_arena import __version__
from skill_arena.extractor.git_commit_extractor import extract_task_from_commit
from skill_arena.i18n import (
    ROLE_ZH,
    SUPPORTED_LANG_CHOICES,
    localize_category,
    localize_task_title,
    pad_cell,
    resolve_lang,
)
from skill_arena.models import ContenderConfig, TaskSpec
from skill_arena.runners.harness_driver import HarnessDriver, validate_simulation_inputs
from skill_arena.sandbox.worktree_pool import WorktreeSandboxPool
from skill_arena.store.arena_db import ArenaDatabase
from skill_arena.studio.server import create_studio_server
from skill_arena.studio.templates import render_arena_html
from skill_arena.suites.builtin_tasks import load_builtin_suite


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
) -> str:
    exp_id = experiment_id or f"exp_{uuid.uuid4().hex[:8]}"
    is_sim_exp = any(c.cli.split(":", 1)[0] == "simulate" for c in contenders)
    db.create_experiment(
        exp_id,
        title=title,
        suite_name=suite_name,
        k_reps=k_reps,
        contenders=contenders,
        is_simulated=is_sim_exp,
    )

    is_zh = lang == "zh"
    if not quiet and is_sim_exp:
        if is_zh:
            print("[SIMULATED / 模拟数据] 合成管线校准运行 - 未调用任何真实模型或外部技能包。")
        else:
            print("[SIMULATED] Synthetic pipeline calibration run - no real model or external SKILL.md was invoked.")

    pool = WorktreeSandboxPool()
    driver = HarnessDriver(pool, allow_unisolated_host_home=allow_unisolated_host_home)
    try:
        for task in tasks:
            for contender in contenders:
                for rep in range(1, k_reps + 1):
                    result = driver.run_trial(exp_id, task, contender, rep_index=rep)
                    db.record_trial(result)
                    if not quiet:
                        cost_txt = (
                            f"${result.usage.total_cost_usd:.4f}"
                            if (result.usage.is_metered and result.usage.total_cost_usd is not None)
                            else ("未计量" if is_zh else "Unmetered")
                        )
                        if is_zh:
                            sim_tag = "[SIMULATED / 模拟数据] " if result.is_simulated else ""
                            badge = "通过" if result.passed else "失败"
                            print(
                                f"{sim_tag}[{badge}] {task.task_id} | {contender.label} (第 {rep}/{k_reps} 轮) "
                                f"| {cost_txt} | {result.usage.total_latency_ms}ms"
                            )
                        else:
                            sim_tag = "[SIMULATED] " if result.is_simulated else ""
                            badge = "PASS" if result.passed else "FAIL"
                            print(
                                f"{sim_tag}[{badge}] {task.task_id} | {contender.label} (rep {rep}/{k_reps}) "
                                f"| {cost_txt} | {result.usage.total_latency_ms}ms"
                            )
    finally:
        pool.close()
    return exp_id


def _print_comparison_table(report: dict, lang: str = "en") -> None:
    is_zh = lang == "zh"
    k_reps = int(report.get("k_reps", 3))
    is_sim = bool(report.get("is_simulated"))
    if is_zh:
        if is_sim:
            print("\n[SIMULATED / 模拟数据] 提示：以下数字由内置合成模拟器生成，不代表任何真实模型或技能的表现。")
        print(f"\n=== {report['title']}（实验编号：{report['experiment_id']}）===")
        hdr = (
            f"{pad_cell('参赛组别', 34)} {pad_cell('角色', 8)} "
            f"{pad_cell('胜率Elo', 9, 'right')} {pad_cell('Pass@1', 8, 'right')} "
            f"{pad_cell(f'Pass^{k_reps}', 8, 'right')} {pad_cell('单次成功成本', 12, 'right')} "
            f"{pad_cell('首轮上下文税', 14, 'right')} {pad_cell('防作弊率', 10, 'right')}"
        )
        print(hdr)
        print("-" * 110)
        for row in report["leaderboard"]:
            metered = bool(row.get("is_metered", True))
            if not metered:
                cps = "未计量"
                delta = "未计量"
            else:
                cps = f"${row['cost_per_success_usd']:.4f}" if row["cost_per_success_usd"] is not None else "n/a"
                ovh_val = row.get("context_overhead_delta_tokens")
                delta = f"{int(ovh_val):+d} tok" if ovh_val is not None else "未计量"
            role_disp = ROLE_ZH.get(row["role"], row["role"])
            elo_str = str(row.get("elo_rating", 1500))
            print(
                f"{pad_cell(str(row['label'])[:33], 34)} {pad_cell(role_disp, 8)} "
                f"{elo_str:>9} {row['pass_at_1'] * 100:7.1f}% "
                f"{row['pass_hat_k'] * 100:7.1f}% {pad_cell(cps, 12, 'right')} {pad_cell(delta, 14, 'right')} "
                f"{row['anti_cheat_integrity_rate'] * 100:9.1f}%"
            )
        print("指标说明：胜率Elo = 成对胜率换算的 Elo（双组）；首轮上下文税 = 实验组与对照组首轮输入 Token 均值之差。")
        return

    if is_sim:
        print("\n[SIMULATED] Notice: Synthetic calibration numbers only - does not reflect any real model or skill.")
    print(f"\n=== {report['title']} ({report['experiment_id']}) ===")
    print(
        f"{'Contender':<34} {'Role':<10} {'WR Elo':>7} {'Pass@1':>8} {f'Pass^{k_reps}':>8} "
        f"{'Cost/Succ':>11} {'Turn-1 Tax':>14} {'Integrity':>10}"
    )
    print("-" * 108)
    for row in report["leaderboard"]:
        metered = bool(row.get("is_metered", True))
        if not metered:
            cps = "Unmetered"
            delta = "Unmetered"
        else:
            cps = f"${row['cost_per_success_usd']:.4f}" if row["cost_per_success_usd"] is not None else "n/a"
            ovh_val = row.get("context_overhead_delta_tokens")
            delta = f"{int(ovh_val):+d} tok" if ovh_val is not None else "Unmetered"
        elo_str = str(row.get("elo_rating", 1500))
        print(
            f"{row['label'][:33]:<34} {row['role']:<10} {elo_str:>7} "
            f"{row['pass_at_1'] * 100:7.1f}% {row['pass_hat_k'] * 100:7.1f}% "
            f"{cps:>11} {delta:>14} {row['anti_cheat_integrity_rate'] * 100:9.1f}%"
        )
    print(
        "Metrics: WR Elo = Win-Rate Derived Elo (2-arm); "
        "Turn-1 Tax = Mean Turn-1 input token delta (treatment minus baseline)."
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
        help="Agent CLI (simulate, claude, codex, opencode, vibe, agy).",
    )
    p_run.add_argument(
        "--model",
        default=None,
        help="Model identifier (defaults to synthetic-sim-model for simulate, claude-sonnet-4-5 for real CLIs).",
    )
    p_run.add_argument("--skill", action="append", default=[], help="Path to SKILL.md or skill folder for Treatment.")
    p_run.add_argument("--reps", type=int, default=3, help="Isolated repetitions k per task (default: 3).")
    p_run.add_argument("--title", default=None, help="Experiment title.")
    p_run.add_argument("--db", default=".skill-arena/arena.sqlite", help="SQLite database path.")
    p_run.add_argument("--html-out", default=None, help="Optional path to export single-file HTML report.")
    p_run.add_argument(
        "--allow-unisolated-host-home",
        action="store_true",
        help=(
            "Explicitly acknowledge that M0 launches real agent CLIs in the host HOME directory "
            "with permission prompts skipped (--dangerously-skip-permissions / --full-auto)."
        ),
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
            if not args.allow_unisolated_host_home:
                msg = (
                    f"错误：未指定 `--allow-unisolated-host-home`，已拒绝启动真实智能体 CLI '{raw_cli}'。"
                    "当前 M0 版本会在真实用户 HOME 目录下（可访问 ~/.claude 与 ~/.agents/skills）"
                    "以跳过权限确认模式（--dangerously-skip-permissions / --full-auto）运行。"
                    "如确认知晓风险，请显式传入 `--allow-unisolated-host-home`。"
                    if is_zh
                    else (
                        f"Error: Refusing to launch real agent CLI '{raw_cli}' without `--allow-unisolated-host-home`. "
                        "In M0, SkillArena runs the agent in your real HOME directory (where ~/.claude and "
                        "~/.agents/skills are visible and writable) with skipped permission prompts "
                        "(--dangerously-skip-permissions / --full-auto). Pass `--allow-unisolated-host-home` to proceed."
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

        try:
            tasks = _load_tasks(args.suite)
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
                    allow_unisolated_host_home=bool(args.allow_unisolated_host_home),
                ),
                ContenderConfig(
                    contender_id="arm_treatment",
                    label=treat_label,
                    role="treatment",
                    cli=treat_cli,
                    model=eff_model,
                    skill_paths=list(args.skill),
                    strip_default_skills=True,
                    allow_unisolated_host_home=bool(args.allow_unisolated_host_home),
                ),
            ]
            exp_id = run_ab_experiment(
                db=db,
                title=eff_title,
                suite_name=args.suite,
                tasks=tasks,
                contenders=contenders,
                k_reps=args.reps,
                lang=lang,
                allow_unisolated_host_home=bool(args.allow_unisolated_host_home),
            )
        except (ValueError, PermissionError) as exc:
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
