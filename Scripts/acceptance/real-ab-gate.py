#!/usr/bin/env python3
"""Real-agent A/B acceptance gate via agent-relay (`relay.sh`) and `agent-xray` (`KR5`)."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys
import tempfile


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Execute and verify a non-simulated SkillArena A/B run via relay.sh + agent-xray."
    )
    parser.add_argument(
        "--cli",
        default=os.environ.get("SKILL_ARENA_REAL_CLI", "claude"),
        choices=["claude", "codex", "agy", "vibe"],
        help="Vendor CLI routed through agent-relay (`relay.sh`).",
    )
    parser.add_argument(
        "--model",
        default=os.environ.get("SKILL_ARENA_REAL_MODEL", "claude-sonnet-4-5"),
        help="Model identifier passed to relay.sh.",
    )
    parser.add_argument(
        "--suite",
        default=os.environ.get("SKILL_ARENA_REAL_SUITE", "builtin-8"),
        help="Suite name, category, or TaskSpec JSON file.",
    )
    parser.add_argument(
        "--skill",
        default=os.environ.get("SKILL_ARENA_REAL_SKILL", ""),
        help="Path to candidate SKILL.md or skill folder for Treatment.",
    )
    parser.add_argument(
        "--reps",
        type=int,
        default=int(os.environ.get("SKILL_ARENA_REAL_REPS", "1")),
        help="Repetitions per task.",
    )
    parser.add_argument(
        "--budget-usd",
        type=float,
        default=float(
            os.environ.get("SKILL_ARENA_REAL_BUDGET_USD")
            or os.environ.get("RELAY_BUDGET_USD")
            or "5.0"
        ),
        help="Experiment-wide cumulative USD ceiling.",
    )
    parser.add_argument(
        "--copy-auth",
        dest="copy_auth",
        action="store_true",
        default=False,
        help="Explicitly copy minimal host CLI auth tokens into per-trial isolated HOME (default: False).",
    )
    parser.add_argument(
        "--no-copy-auth",
        dest="copy_auth",
        action="store_false",
        help="Do not copy host auth tokens into isolated HOME (default).",
    )
    parser.add_argument(
        "--pass-env",
        action="append",
        default=[],
        metavar="VAR",
        help="Explicitly forward named host environment variable(s) into trial subprocesses (repeatable or comma-separated).",
    )
    parser.add_argument(
        "--upstream-url",
        default=None,
        help="Optional upstream base URL override for Anthropic/OpenAI proxy.",
    )
    parser.add_argument("--db", default=None, help="Output SQLite database path.")
    parser.add_argument("--html-out", default=None, help="Output HTML report path.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    repo_root = Path(__file__).resolve().parents[2]
    src_dir = str(repo_root / "src")
    if src_dir not in sys.path:
        sys.path.insert(0, src_dir)

    from skill_arena.cli import main as cli_main
    from skill_arena.graders.frontend_craft import audit_html_craft
    from skill_arena.store.arena_db import ArenaDatabase

    args = _parse_args(argv)
    if not args.skill or not Path(args.skill).expanduser().exists():
        print(
            "FAIL: Pass --skill <path> or set SKILL_ARENA_REAL_SKILL=<path/to/skill> to an existing skill.",
            file=sys.stderr,
        )
        return 2
    if args.budget_usd <= 0.0:
        print(f"FAIL: --budget-usd must be > 0 (got {args.budget_usd})", file=sys.stderr)
        return 2

    if args.upstream_url:
        os.environ["ANTHROPIC_BASE_URL"] = args.upstream_url
        os.environ["OPENAI_BASE_URL"] = args.upstream_url

    tmp_ctx = tempfile.TemporaryDirectory(prefix="sa-real-ab-gate-")
    try:
        tmp_dir = Path(tmp_ctx.name)
        db_path = Path(args.db) if args.db else (tmp_dir / "real_ab.sqlite")
        html_path = Path(args.html_out) if args.html_out else (tmp_dir / "real_ab.html")

        cli_argv = [
            "--lang",
            "en",
            "run",
            "--cli",
            args.cli,
            "--model",
            args.model,
            "--suite",
            args.suite,
            "--skill",
            str(Path(args.skill).expanduser()),
            "--reps",
            str(args.reps),
            "--budget-usd",
            f"{args.budget_usd:.4f}",
            "--db",
            str(db_path),
            "--html-out",
            str(html_path),
        ]
        if args.copy_auth:
            cli_argv.append("--copy-auth")
        for env_spec in args.pass_env:
            cli_argv.extend(["--pass-env", env_spec])

        rc = cli_main(cli_argv)
        if rc != 0:
            print(f"FAIL: skill-arena run exited with code {rc}", file=sys.stderr)
            return rc

        db = ArenaDatabase(db_path)
        exp_id = db.latest_experiment_id()
        if not exp_id:
            print("FAIL: No experiment recorded in database.", file=sys.stderr)
            return 1

        report = db.build_comparison_report(exp_id)
        trials = db.get_trials(exp_id)
        if report.get("is_simulated"):
            print("FAIL: Gate run was marked is_simulated=True.", file=sys.stderr)
            return 1
        if not trials:
            print("FAIL: Zero trials executed.", file=sys.stderr)
            return 1

        cum_cost = float(report.get("cumulative_cost_usd") or 0.0)
        if cum_cost > args.budget_usd + 1e-6:
            print(
                f"FAIL: Cumulative cost ${cum_cost:.4f} exceeded budget ceiling ${args.budget_usd:.4f}.",
                file=sys.stderr,
            )
            return 1

        for tr in trials:
            if not tr.usage.isolated_home_used:
                print(f"FAIL: Trial {tr.trial_id} did not use isolated HOME.", file=sys.stderr)
                return 1
            if not tr.usage.relay_status:
                print(f"FAIL: Trial {tr.trial_id} missing relay.sh status from .relay/LOG.md.", file=sys.stderr)
                return 1
            if args.cli in ("claude", "codex"):
                if not tr.usage.is_metered or (tr.usage.total_prompt_tokens or 0) <= 0:
                    print(
                        f"FAIL: Trial {tr.trial_id} ({args.cli}) missing agent-xray wire token metering: {tr.usage}",
                        file=sys.stderr,
                    )
                    return 1
            if args.cli == "claude" and tr.usage.relay_cost_usd is None:
                print(
                    f"FAIL: Trial {tr.trial_id} (claude) missing .relay/LOG.md step cost.",
                    file=sys.stderr,
                )
                return 1

        craft = audit_html_craft(html_path)
        if not craft.passed:
            print(
                f"FAIL: Exported HTML failed Hallmark craft audit: {[v.to_dict() for v in craft.violations]}",
                file=sys.stderr,
            )
            return 1

        print(
            f"REAL_AB_GATE PASS: exp={exp_id} cli={args.cli} completed={len(trials)}/{report.get('planned_trials')} "
            f"cumulative_usd=${cum_cost:.4f}<=${args.budget_usd:.4f} verdict={report['statistical_comparison']['verdict']}"
        )
        return 0
    finally:
        tmp_ctx.cleanup()


if __name__ == "__main__":
    raise SystemExit(main())
