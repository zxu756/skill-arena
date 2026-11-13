"""Command-line interface for SkillArena: run, compare, studio, export-html, extract-commit, list-suites."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import uuid

from skill_arena.extractor.git_commit_extractor import extract_task_from_commit
from skill_arena.models import ContenderConfig, TaskSpec
from skill_arena.runners.harness_driver import HarnessDriver
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
) -> str:
    exp_id = experiment_id or f"exp_{uuid.uuid4().hex[:8]}"
    db.create_experiment(exp_id, title=title, suite_name=suite_name, k_reps=k_reps, contenders=contenders)

    pool = WorktreeSandboxPool()
    driver = HarnessDriver(pool)
    try:
        for task in tasks:
            for contender in contenders:
                for rep in range(1, k_reps + 1):
                    result = driver.run_trial(exp_id, task, contender, rep_index=rep)
                    db.record_trial(result)
                    if not quiet:
                        badge = "PASS" if result.passed else "FAIL"
                        print(
                            f"[{badge}] {task.task_id} | {contender.label} (rep {rep}/{k_reps}) "
                            f"| ${result.usage.total_cost_usd:.4f} | {result.usage.total_latency_ms}ms"
                        )
    finally:
        pool.close()
    return exp_id


def _print_comparison_table(report: dict) -> None:
    print(f"\n=== {report['title']} ({report['experiment_id']}) ===")
    print(
        f"{'Contender':<32} {'Role':<10} {'Pass@1':>8} {'Pass^k':>8} "
        f"{'Cost/Succ':>11} {'Turn-1 Delta':>14} {'Integrity':>10}"
    )
    print("-" * 98)
    for row in report["leaderboard"]:
        cps = f"${row['cost_per_success_usd']:.4f}" if row["cost_per_success_usd"] is not None else "n/a"
        delta = f"{row['context_overhead_delta_tokens']:+d} tok"
        print(
            f"{row['label'][:31]:<32} {row['role']:<10} "
            f"{row['pass_at_1'] * 100:7.1f}% {row['pass_hat_k'] * 100:7.1f}% "
            f"{cps:>11} {delta:>14} {row['anti_cheat_integrity_rate'] * 100:9.1f}%"
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="skill-arena",
        description="Scientific Git-worktree A/B benchmark harness for coding agents and SKILL.md rules.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_list = sub.add_parser("list-suites", help="List built-in calibration benchmark tasks.")
    p_list.add_argument("--category", default=None, help="Optional category filter.")
    p_list.add_argument("--json", action="store_true", help="Emit JSON output.")

    p_run = sub.add_parser("run", help="Run a controlled Control vs Treatment A/B benchmark.")
    p_run.add_argument("--suite", default="builtin-8", help="Suite name, category, or TaskSpec JSON file.")
    p_run.add_argument("--cli", default="mock", help="Agent CLI (claude, codex, opencode, vibe, agy, mock).")
    p_run.add_argument("--model", default="claude-sonnet-4-5", help="Model identifier.")
    p_run.add_argument("--skill", action="append", default=[], help="Path to SKILL.md or skill folder for Treatment.")
    p_run.add_argument("--reps", type=int, default=3, help="Isolated repetitions k per task (default: 3).")
    p_run.add_argument("--title", default="Control vs SKILL.md A/B Evaluation", help="Experiment title.")
    p_run.add_argument("--db", default=".skill-arena/arena.sqlite", help="SQLite database path.")
    p_run.add_argument("--html-out", default=None, help="Optional path to export single-file HTML report.")

    p_comp = sub.add_parser("compare", help="Print leaderboard summary for an existing experiment.")
    p_comp.add_argument("--db", default=".skill-arena/arena.sqlite", help="SQLite database path.")
    p_comp.add_argument("--exp", default=None, help="Experiment ID (defaults to latest).")
    p_comp.add_argument("--json", action="store_true", help="Emit full JSON report.")

    p_html = sub.add_parser("export-html", help="Export single-file Hallmark HTML scoreboard & replay.")
    p_html.add_argument("--db", default=".skill-arena/arena.sqlite", help="SQLite database path.")
    p_html.add_argument("--exp", default=None, help="Experiment ID (defaults to latest).")
    p_html.add_argument("--out", required=True, help="Destination HTML path.")
    p_html.add_argument("--default-view", choices=["scoreboard", "replay"], default="scoreboard")

    p_ext = sub.add_parser("extract-commit", help="Extract a leak-free TaskSpec JSON from a git bug-fix commit.")
    p_ext.add_argument("--repo", default=".", help="Path to git repository.")
    p_ext.add_argument("--commit", default="HEAD", help="Git commit SHA or ref.")
    p_ext.add_argument("--test-cmd", default=None, help="Override test command.")
    p_ext.add_argument("--out", default=None, help="Optional path to save extracted TaskSpec JSON.")

    p_studio = sub.add_parser("studio", help="Start interactive SkillArena Web Studio.")
    p_studio.add_argument("--db", default=".skill-arena/arena.sqlite", help="SQLite database path.")
    p_studio.add_argument("--host", default="127.0.0.1", help="Bind host.")
    p_studio.add_argument("--port", type=int, default=8765, help="Bind port.")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "list-suites":
        tasks = load_builtin_suite(category=args.category)
        if args.json:
            print(json.dumps([t.to_dict() for t in tasks], indent=2))
        else:
            for t in tasks:
                print(f"{t.task_id:<34} [{t.category:<16}] {t.title}")
        return 0

    if args.command == "run":
        db = ArenaDatabase(args.db)
        tasks = _load_tasks(args.suite)
        ctrl_cli = "mock:baseline" if args.cli == "mock" else args.cli
        treat_cli = "mock:skilled" if args.cli == "mock" else args.cli
        skill_label = (
            "+ " + ", ".join(Path(s).stem if Path(s).stem.upper() != "SKILL" else Path(s).parent.name for s in args.skill)
            if args.skill
            else "+ Curated Engineering Skills"
        )
        contenders = [
            ContenderConfig(
                contender_id="arm_control",
                label=f"{args.cli} ({args.model} · No Skill)",
                role="control",
                cli=ctrl_cli,
                model=args.model,
                skill_paths=[],
                strip_default_skills=True,
            ),
            ContenderConfig(
                contender_id="arm_treatment",
                label=f"{args.cli} ({args.model} {skill_label})",
                role="treatment",
                cli=treat_cli,
                model=args.model,
                skill_paths=list(args.skill),
                strip_default_skills=True,
            ),
        ]
        exp_id = run_ab_experiment(
            db=db,
            title=args.title,
            suite_name=args.suite,
            tasks=tasks,
            contenders=contenders,
            k_reps=args.reps,
        )
        report = db.build_comparison_report(exp_id)
        _print_comparison_table(report)
        if args.html_out:
            out_path = Path(args.html_out)
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(render_arena_html(report), encoding="utf-8")
            print(f"\nExported HTML report -> {out_path}")
        return 0

    if args.command == "compare":
        db = ArenaDatabase(args.db)
        exp_id = args.exp or db.latest_experiment_id()
        if not exp_id:
            print("No experiments found in database.", file=sys.stderr)
            return 1
        report = db.build_comparison_report(exp_id)
        if args.json:
            print(json.dumps(report, indent=2))
        else:
            _print_comparison_table(report)
        return 0

    if args.command == "export-html":
        db = ArenaDatabase(args.db)
        exp_id = args.exp or db.latest_experiment_id()
        if not exp_id:
            print("No experiments found in database.", file=sys.stderr)
            return 1
        report = db.build_comparison_report(exp_id)
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(render_arena_html(report, default_view=args.default_view), encoding="utf-8")
        print(f"Exported single-file HTML report -> {out_path}")
        return 0

    if args.command == "extract-commit":
        spec = extract_task_from_commit(Path(args.repo), commit_ref=args.commit, test_command=args.test_cmd)
        rendered = json.dumps(spec.to_dict(), indent=2)
        if args.out:
            out_path = Path(args.out)
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(rendered + "\n", encoding="utf-8")
            print(f"Extracted TaskSpec {spec.task_id} -> {out_path}")
        else:
            print(rendered)
        return 0

    if args.command == "studio":
        db = ArenaDatabase(args.db)
        server = create_studio_server(db, host=args.host, port=args.port)
        print(f"SkillArena Studio listening on http://{args.host}:{args.port}")
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            server.server_close()
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
