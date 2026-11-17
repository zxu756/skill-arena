"""CLI, HTML export, simulation guardrails, consent gate, and bilingual metric honesty tests (M0 Items 1, 2, 3, 7, 8)."""

from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import subprocess
import sys

import pytest

from skill_arena.cli import main
from skill_arena.graders.frontend_craft import audit_html_craft
from skill_arena.store.arena_db import ArenaDatabase
from skill_arena.suites.builtin_tasks import load_builtin_suite


def _write_executable(path: Path, body: str) -> Path:
    path.write_text(body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


def test_cli_mock_rejected_in_en_and_zh(tmp_path: Path, capsys) -> None:
    db_path = tmp_path / "arena.sqlite"

    rc_en = main(["--lang", "en", "run", "--cli", "mock", "--suite", "concurrency", "--reps", "1", "--db", str(db_path)])
    captured_en = capsys.readouterr()
    assert rc_en != 0
    err_en = (captured_en.out + "\n" + captured_en.err).lower()
    assert "simulate" in err_en
    assert "mock" in err_en

    rc_zh = main(["--lang", "zh", "run", "--cli", "mock", "--suite", "concurrency", "--reps", "1", "--db", str(db_path)])
    captured_zh = capsys.readouterr()
    assert rc_zh != 0
    err_zh = captured_zh.out + "\n" + captured_zh.err
    assert "simulate" in err_zh
    assert "已移除" in err_zh or "已更名" in err_zh


@pytest.mark.parametrize(
    "extra_args",
    [
        ["--model", "claude-sonnet-4-5"],
        ["--model", "gpt-5"],
        ["--model", "o3-mini"],
        ["--model", "gemini-2.5-pro"],
        ["--title", "Claude " + "Sonnet 4.5: Baseline vs Curated Engineering & Hallmark Skills"],
        ["--title", "Benchmark ~/.agents/skills/hallmark"],
        ["--title", "Eval /tmp/custom-skill/SKILL.md"],
    ],
)
def test_cli_simulate_rejects_real_models_and_skill_titles(tmp_path: Path, capsys, extra_args: list[str]) -> None:
    db_path = tmp_path / "arena.sqlite"
    rc = main(
        ["--lang", "en", "run", "--cli", "simulate", "--suite", "concurrency", "--reps", "1", "--db", str(db_path)]
        + extra_args
    )
    captured = capsys.readouterr()
    assert rc != 0
    assert "simulate" in (captured.out + "\n" + captured.err).lower()


def test_cli_simulate_rejects_skill_flag_in_zh(tmp_path: Path, capsys) -> None:
    skill_dir = tmp_path / "demo-skill"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text("# Demo\n", encoding="utf-8")
    db_path = tmp_path / "arena.sqlite"

    rc = main(
        [
            "--lang",
            "zh",
            "run",
            "--cli",
            "simulate",
            "--suite",
            "concurrency",
            "--reps",
            "1",
            "--skill",
            str(skill_dir),
            "--db",
            str(db_path),
        ]
    )
    captured = capsys.readouterr()
    assert rc != 0
    assert "--skill" in (captured.out + "\n" + captured.err)


def test_cli_simulate_marks_terminal_db_and_html_as_simulated_and_honest_metrics(tmp_path: Path, capsys) -> None:
    db_path = tmp_path / "sim.sqlite"
    html_en_path = tmp_path / "sim-en.html"
    html_zh_path = tmp_path / "sim-zh.html"
    single_spec = tmp_path / "one_task.json"

    # Load 1 builtin task into a spec file for fast deterministic CLI verification
    from skill_arena.suites.builtin_tasks import load_builtin_suite

    spec = load_builtin_suite(category="concurrency")[0]
    single_spec.write_text(json.dumps(spec.to_dict()), encoding="utf-8")

    rc = main(
        [
            "--lang",
            "en",
            "run",
            "--cli",
            "simulate",
            "--suite",
            str(single_spec),
            "--reps",
            "1",
            "--db",
            str(db_path),
            "--html-out",
            str(html_en_path),
        ]
    )
    out_en = capsys.readouterr().out
    assert rc == 0
    assert "SIMULATED" in out_en
    assert "Win-Rate Derived Elo (2-arm)" in out_en
    assert "Mean Turn-1 input token delta (treatment minus baseline)" in out_en
    assert "Bradley-Terry" not in out_en
    assert "BT Elo" not in out_en

    # Chinese compare + export-html
    rc_cmp_zh = main(["--lang", "zh", "compare", "--db", str(db_path)])
    out_zh = capsys.readouterr().out
    assert rc_cmp_zh == 0
    assert "SIMULATED" in out_zh
    assert "模拟数据" in out_zh
    assert "成对胜率换算的 Elo（双组）" in out_zh
    assert "实验组与对照组首轮输入 Token 均值之差" in out_zh
    assert "Bradley-Terry" not in out_zh
    assert "BT Elo" not in out_zh

    rc_html_zh = main(["--lang", "zh", "export-html", "--db", str(db_path), "--out", str(html_zh_path)])
    capsys.readouterr()
    assert rc_html_zh == 0

    # Verify DB markers
    db = ArenaDatabase(db_path)
    exp_id = db.latest_experiment_id()
    assert exp_id is not None
    report = db.build_comparison_report(exp_id)
    assert report["is_simulated"] is True
    assert "SIMULATED" in report["title"]

    # Verify EN & ZH HTML markers, honest metric names, K1 disclosure, and Hallmark craft audit
    html_en = html_en_path.read_text(encoding="utf-8")
    html_zh = html_zh_path.read_text(encoding="utf-8")

    assert "SIMULATED" in html_en
    assert "Win-Rate Derived Elo (2-arm)" in html_en
    assert "Mean Turn-1 input token delta (treatment minus baseline)" in html_en
    assert "None (stripped)" not in html_en
    assert "Bradley-Terry" not in html_en
    assert "BT Elo" not in html_en

    assert "SIMULATED" in html_zh
    assert "模拟数据" in html_zh
    assert "成对胜率换算的 Elo（双组）" in html_zh
    assert "实验组与对照组首轮输入 Token 均值之差" in html_zh
    assert "无（已剥离）" not in html_zh
    assert "Bradley-Terry" not in html_zh
    assert "BT Elo" not in html_zh

    craft_en = audit_html_craft(html_en_path)
    assert craft_en.passed, f"EN HTML failed craft audit: {craft_en.findings}"
    craft_zh = audit_html_craft(html_zh_path)
    assert craft_zh.passed, f"ZH HTML failed craft audit: {craft_zh.findings}"


def test_real_cli_requires_explicit_consent_flag_and_unmetered_renders_cleanly(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub_claude = _write_executable(
        bin_dir / "claude",
        "#!/usr/bin/env python3\n"
        "import json, sys\n"
        "_ = sys.stdin.read()\n"
        "print(json.dumps({'result': 'No-op stub.\\nSTATUS: success'}))\n",
    )
    monkeypatch.setenv("RELAY_CLAUDE_BIN", str(stub_claude))

    from skill_arena.suites.builtin_tasks import load_builtin_suite

    spec_file = tmp_path / "task.json"
    spec_file.write_text(json.dumps(load_builtin_suite()[0].to_dict()), encoding="utf-8")
    db_path = tmp_path / "real_gate.sqlite"

    # 1. Unsupported CLI (not in agent-relay) -> must fail cleanly
    rc_bad = main(
        [
            "--lang",
            "en",
            "run",
            "--cli",
            "opencode",
            "--model",
            "stub-model",
            "--suite",
            str(spec_file),
            "--reps",
            "1",
            "--db",
            str(db_path),
        ]
    )
    err_bad = capsys.readouterr()
    assert rc_bad != 0
    assert "agent-relay" in (err_bad.out + "\n" + err_bad.err)
    assert not db_path.exists()

    # 2. Explicit --host-home WITHOUT --allow-unisolated-host-home -> must fail in EN and ZH before running any trial
    rc_no_en = main(
        [
            "--lang",
            "en",
            "run",
            "--cli",
            "claude",
            "--model",
            "stub-model",
            "--host-home",
            "--suite",
            str(spec_file),
            "--reps",
            "1",
            "--db",
            str(db_path),
        ]
    )
    err_en = capsys.readouterr()
    assert rc_no_en != 0
    assert "--allow-unisolated-host-home" in (err_en.out + "\n" + err_en.err)
    assert not db_path.exists()

    rc_no_zh = main(
        [
            "--lang",
            "zh",
            "run",
            "--cli",
            "claude",
            "--model",
            "stub-model",
            "--host-home",
            "--suite",
            str(spec_file),
            "--reps",
            "1",
            "--db",
            str(db_path),
        ]
    )
    err_zh = capsys.readouterr()
    assert rc_no_zh != 0
    assert "--allow-unisolated-host-home" in (err_zh.out + "\n" + err_zh.err)
    assert "真实" in (err_zh.out + "\n" + err_zh.err) or "宿主" in (err_zh.out + "\n" + err_zh.err)
    assert not db_path.exists()

    # 3. Default M1 run (isolated HOME via relay.sh) -> runs safely without --allow-unisolated-host-home,
    #    and when no HTTP turn hits agent-xray, records Unmetered / 未计量 (NEVER $0.0000 or +0 tok)
    html_en_path = tmp_path / "unmetered-en.html"
    html_zh_path = tmp_path / "unmetered-zh.html"
    rc_ok = main(
        [
            "--lang",
            "en",
            "run",
            "--cli",
            "claude",
            "--model",
            "stub-model",
            "--suite",
            str(spec_file),
            "--reps",
            "1",
            "--db",
            str(db_path),
            "--html-out",
            str(html_en_path),
        ]
    )
    out_ok_en = capsys.readouterr().out
    assert rc_ok == 0
    assert "Unmetered" in out_ok_en
    assert "$0.0000" not in out_ok_en
    assert "+0 tok" not in out_ok_en

    rc_cmp_zh = main(["--lang", "zh", "compare", "--db", str(db_path)])
    out_ok_zh = capsys.readouterr().out
    assert rc_cmp_zh == 0
    assert "未计量" in out_ok_zh
    assert "$0.0000" not in out_ok_zh

    assert main(["--lang", "zh", "export-html", "--db", str(db_path), "--out", str(html_zh_path)]) == 0
    capsys.readouterr()

    html_en = html_en_path.read_text(encoding="utf-8")
    html_zh = html_zh_path.read_text(encoding="utf-8")
    assert "Unmetered" in html_en
    assert "$0.0000" not in html_en
    assert "未计量" in html_zh
    assert "$0.0000" not in html_zh


def test_extract_commit_ambiguous_repo_returns_clean_cli_error(tmp_path: Path, capsys) -> None:
    repo = tmp_path / "ambig_repo"
    repo.mkdir()
    git_env = os.environ.copy()
    git_env.update(
        {
            "GIT_AUTHOR_NAME": "Tester",
            "GIT_AUTHOR_EMAIL": "t@example.com",
            "GIT_COMMITTER_NAME": "Tester",
            "GIT_COMMITTER_EMAIL": "t@example.com",
        }
    )
    subprocess.run(["git", "init", "-b", "main"], cwd=repo, env=git_env, check=True, capture_output=True)
    (repo / "m.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    (repo / "tests").mkdir()
    (repo / "tests/test_m.py").write_text("from m import f\nassert f() == 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=repo, env=git_env, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo, env=git_env, check=True, capture_output=True)
    (repo / "m.py").write_text("def f():\n    return 2\n", encoding="utf-8")
    (repo / "tests/test_m.py").write_text("from m import f\nassert f() == 2\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=repo, env=git_env, check=True)
    subprocess.run(["git", "commit", "-m", "fix f"], cwd=repo, env=git_env, check=True, capture_output=True)

    rc = main(["--lang", "en", "extract-commit", "--repo", str(repo), "--commit", "HEAD"])
    captured = capsys.readouterr()
    assert rc != 0
    assert "--test-cmd" in (captured.out + "\n" + captured.err)


def test_repo_has_no_demo_leaderboard_or_fabricated_sonnet_title() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    assert not (repo_root / "docs" / "demo-leaderboard.html").exists()

    docs_dir = repo_root / "docs"
    if docs_dir.exists():
        assert list(docs_dir.glob("screenshot-*.png")) == [], (
            "Fabricated v0.2.0 screenshots must be removed from docs/"
        )

    banned = "Claude Sonnet 4.5" + ": Baseline vs Curated Engineering"
    offenders: list[str] = []
    for path in repo_root.rglob("*"):
        if not path.is_file():
            continue
        rel_parts = path.relative_to(repo_root).parts
        if any(p in {".git", ".venv", "__pycache__", ".pytest_cache"} for p in rel_parts):
            continue
        try:
            content = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        if banned in content:
            offenders.append(str(path.relative_to(repo_root)))

    assert offenders == [], f"Fabricated demo title still present in: {offenders}"


def test_readmes_have_no_fabricated_images_and_disclose_unreleased_and_limitations() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    readme_en = (repo_root / "README.md").read_text(encoding="utf-8")
    readme_zh = (repo_root / "README.zh-CN.md").read_text(encoding="utf-8")

    # K12: No fabricated screenshot links
    assert "docs/screenshot-" not in readme_en
    assert "docs/screenshot-" not in readme_zh

    # W5: Clean title without branch suffix, and unreleased behaviors marked clearly
    assert readme_en.splitlines()[0].strip() == "# SkillArena"
    assert readme_zh.splitlines()[0].strip() == "# SkillArena"
    assert "(m0/honest)" not in readme_en
    assert "(m0/honest)" not in readme_zh
    assert "since `v0.3.0`" in readme_en and "Unreleased" not in readme_en
    assert "`v0.3.0` 起" in readme_zh and "未发布" not in readme_zh

    # K13: macOS SIP platform binary double-fork + setsid non-guarantee
    assert "SIP" in readme_en and "/bin/sleep" in readme_en and "does not guarantee" in readme_en
    assert "SIP" in readme_zh and "/bin/sleep" in readme_zh and "不保证" in readme_zh

    # Extract-commit implementation filenames & repo_path limitation
    assert "implementation filename" in readme_en.lower() and "repo_path" in readme_en
    assert "实现文件名" in readme_zh and "repo_path" in readme_zh


def test_cli_run_missing_pytest_fails_preflight_with_nonzero_exit(
    tmp_path: Path,
    capsys,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bin_dir = tmp_path / "no_pytest_bin"
    bin_dir.mkdir()
    _write_executable(
        bin_dir / "python3",
        "#!/bin/sh\necho 'No module named pytest' >&2\nexit 1\n",
    )
    agent_ran_marker = tmp_path / "agent_ran.txt"
    stub_claude = _write_executable(
        bin_dir / "claude",
        f"#!/bin/sh\necho ran > {str(agent_ran_marker)!r}\nexit 0\n",
    )
    monkeypatch.setenv("RELAY_CLAUDE_BIN", str(stub_claude))
    monkeypatch.setenv("PATH", f"{bin_dir}:/usr/bin:/bin")
    monkeypatch.setenv("HOME", str(tmp_path / "fake_home"))

    spec_path = tmp_path / "pytest_task.json"
    spec_path.write_text(
        json.dumps(
            {
                "task_id": "pytest-preflight",
                "title": "Pytest Preflight Task",
                "category": "refactor_test",
                "prompt": "Fix m.py",
                "test_command": "python3 -m pytest tests/test_m.py",
                "protected_test_paths": ["tests/test_m.py"],
                "seed_files": {
                    "m.py": "def f(): return 1\n",
                    "tests/test_m.py": "from m import f\ndef test_f(): assert f() == 1\n",
                },
            }
        ),
        encoding="utf-8",
    )
    db_path = tmp_path / "preflight.sqlite"

    for lang_code in ("en", "zh"):
        rc = main(
            [
                "--lang",
                lang_code,
                "run",
                "--cli",
                "claude",
                "--model",
                "stub-model",
                "--suite",
                str(spec_path),
                "--reps",
                "1",
                "--db",
                str(db_path),
            ]
        )
        captured = capsys.readouterr()
        combined = captured.out + "\n" + captured.err
        assert rc != 0, f"Expected non-zero exit when pytest missing ({lang_code}), got {rc}"
        assert "pytest" in combined.lower()
        assert not agent_ran_marker.exists(), "Agent CLI must not run when pytest preflight fails!"
        assert not db_path.exists(), "Database must not be created when pytest preflight fails!"


def test_equal_pass_rate_20_trials_each_cli_and_html_report_inconclusive_zh_and_en(
    tmp_path: Path,
    capsys,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """M1 Acceptance Row 5: Both arms use the exact same stub for 20 reps each -> report says 看不出差别 / Inconclusive."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    # Deterministic counter file so both arms achieve the exact same 12/20 (60%) pass rate (or probabilistic parity)
    state_dir = tmp_path / "stub_state"
    state_dir.mkdir()
    stub_claude = _write_executable(
        bin_dir / "claude",
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        "from pathlib import Path\n"
        "_ = sys.stdin.read()\n"
        f"st = Path({str(state_dir)!r})\n"
        "arm = os.environ.get('SKILL_ARENA_CONTENDER', 'x')\n"
        "cnt_file = st / f'{arm}.cnt'\n"
        "idx = int(cnt_file.read_text()) + 1 if cnt_file.exists() else 1\n"
        "cnt_file.write_text(str(idx))\n"
        "if idx % 2 == 1:\n"
        "    Path('m.py').write_text('def f():\\n    return 2\\n', encoding='utf-8')\n"
        "print(json.dumps({'result': 'Attempted fix.\\nSTATUS: success'}))\n",
    )
    monkeypatch.setenv("RELAY_CLAUDE_BIN", str(stub_claude))

    spec_path = tmp_path / "one_fast_task.json"
    spec_path.write_text(
        json.dumps(
            {
                "task_id": "fast-parity-01",
                "title": "Fast Parity Task",
                "category": "refactor_test",
                "prompt": "Update `m.py` so `f()` returns 2.",
                "test_command": "python3 -m unittest tests/test_m.py",
                "protected_test_paths": ["tests/test_m.py"],
                "seed_files": {
                    "m.py": "def f():\n    return 1\n",
                    "tests/test_m.py": (
                        "import unittest\nfrom m import f\n"
                        "class T(unittest.TestCase):\n"
                        "    def test_f(self):\n"
                        "        self.assertEqual(f(), 2)\n"
                    ),
                },
            }
        ),
        encoding="utf-8",
    )
    cand_skill = tmp_path / "placebo-skill"
    cand_skill.mkdir()
    (cand_skill / "SKILL.md").write_text("# Placebo Skill\n", encoding="utf-8")

    db_path = tmp_path / "parity20.sqlite"
    html_zh_path = tmp_path / "parity20-zh.html"
    html_en_path = tmp_path / "parity20-en.html"

    rc_run = main(
        [
            "--lang",
            "zh",
            "run",
            "--cli",
            "claude",
            "--model",
            "stub-model",
            "--skill",
            str(cand_skill),
            "--suite",
            str(spec_path),
            "--reps",
            "20",
            "--db",
            str(db_path),
            "--html-out",
            str(html_zh_path),
        ]
    )
    out_zh = capsys.readouterr().out
    assert rc_run == 0
    assert "看不出差别" in out_zh
    assert "95%" in out_zh

    rc_en = main(["--lang", "en", "compare", "--db", str(db_path)])
    out_en = capsys.readouterr().out
    assert rc_en == 0
    assert "Inconclusive" in out_en
    assert "95%" in out_en

    rc_json = main(["compare", "--db", str(db_path), "--json"])
    rep_json = json.loads(capsys.readouterr().out)
    assert rc_json == 0
    sc = rep_json["statistical_comparison"]
    assert sc["control_trials"] == 20
    assert sc["treatment_trials"] == 20
    assert sc["verdict"] == "inconclusive"
    assert sc["winner"] is None

    assert main(["--lang", "en", "export-html", "--db", str(db_path), "--out", str(html_en_path)]) == 0
    capsys.readouterr()

    html_zh = html_zh_path.read_text(encoding="utf-8")
    html_en = html_en_path.read_text(encoding="utf-8")
    assert "看不出差别" in html_zh
    assert "Inconclusive" in html_en
    assert audit_html_craft(html_zh_path).passed
    assert audit_html_craft(html_en_path).passed


def test_readmes_disclose_env_scrub_detached_daemon_limitation_bilingually() -> None:
    """K16: Both README.md and README.zh-CN.md must disclose detached env-scrubbing daemons alongside SIP."""
    repo_root = Path(__file__).resolve().parents[1]
    readme_en = (repo_root / "README.md").read_text(encoding="utf-8")
    readme_zh = (repo_root / "README.zh-CN.md").read_text(encoding="utf-8")

    # English README must state both SIP binaries and environment-scrubbing detached processes (execve / env)
    assert "SIP" in readme_en and "/bin/sleep" in readme_en
    assert "execve" in readme_en and "SKILL_ARENA_TRIAL_TOKEN" in readme_en
    assert "chdir" in readme_en and "does not guarantee" in readme_en

    # Chinese README must carry matching bilingual disclosure
    assert "SIP" in readme_zh and "/bin/sleep" in readme_zh
    assert "execve" in readme_zh and "SKILL_ARENA_TRIAL_TOKEN" in readme_zh
    assert "chdir" in readme_zh and "不保证" in readme_zh


def test_acceptance_scripts_report_skipped_separately_from_passed() -> None:
    """Skipped checks must be tallied separately in summary lines, never folded into 'ALL ... VERIFIED'."""
    repo_root = Path(__file__).resolve().parents[1]
    readme_replay_sh = (repo_root / "Scripts" / "acceptance" / "readme-commands.sh").read_text(encoding="utf-8")
    test_sh = (repo_root / "Scripts" / "test.sh").read_text(encoding="utf-8")

    assert "ALL PARSED README COMMANDS VERIFIED" not in readme_replay_sh
    assert "skipped_count" in readme_replay_sh and "passed_count" in readme_replay_sh
    assert "skipped" in test_sh


def test_kr2_experiment_wide_budget_ceiling_stops_before_exceeding_budget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """KR2: 每次试验花 0.01、预算 0.015、reps 2 时在超出前停下 (plus 0.025 and 0.005 cases)."""
    one_task = load_builtin_suite(category="concurrency")[0]
    task_file = tmp_path / "one_task.json"
    task_file.write_text(json.dumps(one_task.to_dict(), indent=2), encoding="utf-8")

    ref_sol = tmp_path / "ref_rate_limiter.py"
    ref_sol.write_text(one_task.reference_solution_files["rate_limiter.py"], encoding="utf-8")

    seen_budgets_log = tmp_path / "seen_budgets.jsonl"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub_claude = bin_dir / "claude"
    stub_claude.write_text(
        f"#!{sys.executable}\n"
        "import json, os, shutil, sys\n"
        "from pathlib import Path\n"
        "_ = sys.stdin.read()\n"
        f"with open({str(seen_budgets_log)!r}, 'a', encoding='utf-8') as fh:\n"
        "    fh.write(json.dumps({'relay_budget_usd': os.environ.get('RELAY_BUDGET_USD')}) + '\\n')\n"
        f"shutil.copyfile({str(ref_sol)!r}, 'rate_limiter.py')\n"
        "print(json.dumps({'result': 'Done.\\nSTATUS: success', 'total_cost_usd': 0.01, 'session_id': 'kr2'}))\n",
        encoding="utf-8",
    )
    stub_claude.chmod(0o755)
    monkeypatch.setenv("RELAY_CLAUDE_BIN", str(stub_claude))

    # Case 1 (Mandatory): $0.01/trial, --budget-usd 0.015, --reps 2 (4 planned trials) -> stops after 1 trial ($0.01 <= $0.015)
    db_015 = tmp_path / "budget_015.sqlite"
    html_015_en = tmp_path / "budget_015_en.html"
    html_015_zh = tmp_path / "budget_015_zh.html"
    from skill_arena.cli import EXIT_BUDGET_EXCEEDED

    assert EXIT_BUDGET_EXCEEDED == 3

    rc1 = main(
        [
            "--lang",
            "en",
            "run",
            "--cli",
            "claude",
            "--model",
            "claude-sonnet-4-5",
            "--suite",
            str(task_file),
            "--reps",
            "2",
            "--budget-usd",
            "0.015",
            "--db",
            str(db_015),
            "--html-out",
            str(html_015_en),
        ]
    )
    out1_en = capsys.readouterr().out
    assert rc1 == EXIT_BUDGET_EXCEEDED
    seen_1 = [json.loads(ln) for ln in seen_budgets_log.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert len(seen_1) == 1, f"Expected to halt after 1 trial before exceeding $0.015, ran {len(seen_1)}"
    assert float(seen_1[0]["relay_budget_usd"]) == pytest.approx(0.015)

    assert "BUDGET STOPPED" in out1_en or "Budget ceiling" in out1_en
    assert main(["--lang", "zh", "compare", "--db", str(db_015)]) == 0
    out1_zh = capsys.readouterr().out
    assert "预算" in out1_zh and ("停止" in out1_zh or "上限" in out1_zh)

    assert main(["compare", "--db", str(db_015), "--json"]) == 0
    rep1 = json.loads(capsys.readouterr().out)
    assert rep1["budget_usd"] == pytest.approx(0.015)
    assert rep1["cumulative_cost_usd"] == pytest.approx(0.01)
    assert rep1["cumulative_cost_usd"] <= 0.015
    assert rep1["planned_trials"] == 4
    assert rep1["completed_trials"] == 1
    assert rep1["budget_stopped"] is True
    assert rep1["budget_halted"] is True

    assert main(["--lang", "zh", "export-html", "--db", str(db_015), "--out", str(html_015_zh)]) == 0
    capsys.readouterr()
    assert "0.015" in html_015_en.read_text(encoding="utf-8")
    assert "预算" in html_015_zh.read_text(encoding="utf-8")
    assert audit_html_craft(html_015_en).passed
    assert audit_html_craft(html_015_zh).passed

    # Case 2: --budget-usd 0.025 with $0.01/trial -> stops after 2 trials ($0.02 <= $0.025), passing remaining budget
    seen_budgets_log.unlink()
    db_025 = tmp_path / "budget_025.sqlite"
    assert (
        main(
            [
                "--lang",
                "en",
                "run",
                "--cli",
                "claude",
                "--model",
                "claude-sonnet-4-5",
                "--suite",
                str(task_file),
                "--reps",
                "2",
                "--budget-usd",
                "0.025",
                "--db",
                str(db_025),
            ]
        )
        == EXIT_BUDGET_EXCEEDED
    )
    capsys.readouterr()
    seen_2 = [json.loads(ln) for ln in seen_budgets_log.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert len(seen_2) == 2
    assert float(seen_2[0]["relay_budget_usd"]) == pytest.approx(0.025)
    assert float(seen_2[1]["relay_budget_usd"]) == pytest.approx(0.015)
    rep2 = ArenaDatabase(db_025).build_comparison_report(ArenaDatabase(db_025).latest_experiment_id() or "")
    assert rep2["completed_trials"] == 2
    assert rep2["cumulative_cost_usd"] == pytest.approx(0.02)
    assert rep2["budget_stopped"] is True

    # Case 3: --budget-usd 0.005 where Trial 1 spends $0.01 -> stops immediately after Trial 1 (0 subsequent trials)
    seen_budgets_log.unlink()
    db_005 = tmp_path / "budget_005.sqlite"
    assert (
        main(
            [
                "--lang",
                "en",
                "run",
                "--cli",
                "claude",
                "--model",
                "claude-sonnet-4-5",
                "--suite",
                str(task_file),
                "--reps",
                "2",
                "--budget-usd",
                "0.005",
                "--db",
                str(db_005),
            ]
        )
        == EXIT_BUDGET_EXCEEDED
    )
    out3_en = capsys.readouterr().out
    assert "[BUDGET STOPPED]" in out3_en
    seen_3 = [json.loads(ln) for ln in seen_budgets_log.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert len(seen_3) == 1
    rep3 = ArenaDatabase(db_005).build_comparison_report(ArenaDatabase(db_005).latest_experiment_id() or "")
    assert rep3["completed_trials"] == 1
    assert rep3["cumulative_cost_usd"] == pytest.approx(0.01)
    assert rep3["budget_stopped"] is True

    # Case 4: Bilingual README parity check for experiment-wide cumulative ceiling
    repo_root = Path(__file__).resolve().parents[1]
    readme_en = (repo_root / "README.md").read_text(encoding="utf-8")
    readme_zh = (repo_root / "README.zh-CN.md").read_text(encoding="utf-8")
    assert "cumulative" in readme_en.lower() and "--budget-usd" in readme_en
    assert "累计" in readme_zh and "--budget-usd" in readme_zh


def test_kr8_budget_halt_returns_dedicated_exit_code_and_discloses_first_trial_overrun(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """KR8: Budget halt returns EXIT_BUDGET_EXCEEDED (3) vs 0 when within budget; READMEs disclose exit 3 & trial-1 overrun."""
    from skill_arena.cli import EXIT_BUDGET_EXCEEDED

    assert EXIT_BUDGET_EXCEEDED == 3

    fake_home = tmp_path / "caller_home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub_claude = _write_executable(
        bin_dir / "claude",
        (
            f"#!{sys.executable}\n"
            "import json, sys\n"
            "_ = sys.stdin.read()\n"
            "print(json.dumps({'result': 'Attempt.\\nSTATUS: success', 'total_cost_usd': 0.01}))\n"
        ),
    )
    monkeypatch.setenv("RELAY_CLAUDE_BIN", str(stub_claude))

    task_file = tmp_path / "kr8_budget_task.json"
    task_file.write_text(json.dumps([load_builtin_suite()[0].to_dict()]), encoding="utf-8")

    # 1. Within budget (2 planned trials x $0.01 = $0.02 <= $0.05) -> exits 0, budget_stopped=False
    db_within = tmp_path / "within.sqlite"
    rc_within = main(
        [
            "--lang",
            "en",
            "run",
            "--cli",
            "claude",
            "--model",
            "claude-sonnet-4-5",
            "--suite",
            str(task_file),
            "--reps",
            "1",
            "--budget-usd",
            "0.05",
            "--db",
            str(db_within),
        ]
    )
    capsys.readouterr()
    assert rc_within == 0
    rep_within = ArenaDatabase(db_within).build_comparison_report(
        ArenaDatabase(db_within).latest_experiment_id() or ""
    )
    assert rep_within["completed_trials"] == 2
    assert rep_within["budget_stopped"] is False

    # 2. Halted before next trial (2 planned trials x $0.01, --budget-usd 0.015) -> exits 3 (not 0, 1, or 2)
    db_halt = tmp_path / "halt.sqlite"
    rc_halt = main(
        [
            "--lang",
            "zh",
            "run",
            "--cli",
            "claude",
            "--model",
            "claude-sonnet-4-5",
            "--suite",
            str(task_file),
            "--reps",
            "1",
            "--budget-usd",
            "0.015",
            "--db",
            str(db_halt),
        ]
    )
    out_halt_zh = capsys.readouterr().out
    assert rc_halt == EXIT_BUDGET_EXCEEDED == 3
    assert "[预算上限停止]" in out_halt_zh

    # 3. Single-trial overrun (2 planned trials x $0.01, --budget-usd 0.005 < $0.01) -> trial 1 runs, overruns, halts, exits 3
    db_overrun = tmp_path / "overrun.sqlite"
    rc_overrun = main(
        [
            "--lang",
            "en",
            "run",
            "--cli",
            "claude",
            "--model",
            "claude-sonnet-4-5",
            "--suite",
            str(task_file),
            "--reps",
            "1",
            "--budget-usd",
            "0.005",
            "--db",
            str(db_overrun),
        ]
    )
    out_overrun = capsys.readouterr().out
    assert rc_overrun == EXIT_BUDGET_EXCEEDED == 3
    assert "[BUDGET STOPPED]" in out_overrun
    rep_overrun = ArenaDatabase(db_overrun).build_comparison_report(
        ArenaDatabase(db_overrun).latest_experiment_id() or ""
    )
    assert rep_overrun["completed_trials"] == 1
    assert rep_overrun["cumulative_cost_usd"] == pytest.approx(0.01)
    assert rep_overrun["budget_stopped"] is True

    # 4. Subprocess CLI check confirming actual process exit status is 3
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "skill_arena.cli",
            "--lang",
            "en",
            "run",
            "--cli",
            "simulate",
            "--suite",
            "concurrency",
            "--reps",
            "1",
            "--budget-usd",
            "0.0001",
            "--db",
            str(tmp_path / "subproc_overrun.sqlite"),
        ],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 3, f"Expected exit code 3, got {proc.returncode}: {proc.stdout}\n{proc.stderr}"

    # 5. Bilingual README disclosure check for exit code 3 and first-trial overrun
    repo_root = Path(__file__).resolve().parents[1]
    readme_en = (repo_root / "README.md").read_text(encoding="utf-8")
    readme_zh = (repo_root / "README.zh-CN.md").read_text(encoding="utf-8")
    assert "EXIT_BUDGET_EXCEEDED" in readme_en and "3" in readme_en
    assert "smaller than the cost of a single trial" in readme_en
    assert "first trial still executes" in readme_en
    assert "EXIT_BUDGET_EXCEEDED" in readme_zh and "3" in readme_zh
    assert "小于单次试验" in readme_zh
    assert "第 1 次试验仍会先执行" in readme_zh or "第 `1` 次试验仍会先执行" in readme_zh


def test_kr3_cli_json_and_html_display_both_xray_and_relay_costs(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """KR3: CLI (EN & ZH), JSON arms/trials, and HTML report show both agent-xray and .relay/LOG.md costs."""
    from skill_arena.models import (
        ContenderConfig,
        GraderVerdict,
        TokenUsageLedger,
        TrajectoryStep,
        TrialResult,
    )

    db_path = tmp_path / "kr3_cli.sqlite"
    db = ArenaDatabase(db_path)
    ctrl = ContenderConfig(contender_id="arm_control", label="claude (control)", role="control", cli="claude", model="claude-sonnet-4-5")
    treat = ContenderConfig(contender_id="arm_treatment", label="claude (+skill)", role="treatment", cli="claude", model="claude-sonnet-4-5")
    db.create_experiment("exp_kr3_cli", title="Dual Cost Report", suite_name="concurrency", k_reps=1, contenders=[ctrl, treat])

    for arm, xray_cost, relay_cost in ((ctrl, 0.0019, 0.0142), (treat, 0.0027, 0.0188)):
        db.record_trial(
            TrialResult(
                trial_id=f"tr_{arm.contender_id}",
                experiment_id="exp_kr3_cli",
                task_id="conc-01-token-bucket-race",
                task_title="Thread-Safe Token Bucket Rate Limiter",
                task_category="concurrency",
                contender_id=arm.contender_id,
                contender_label=arm.label,
                contender_role=arm.role,
                rep_index=1,
                passed=True,
                anti_cheat_clean=True,
                usage=TokenUsageLedger(
                    turns=1,
                    uncached_input_tokens=300,
                    first_turn_prompt_tokens=400,
                    total_prompt_tokens=400,
                    cached_prompt_tokens=100,
                    completion_tokens=60,
                    ttft_ms=25.0,
                    total_latency_ms=120,
                    total_cost_usd=xray_cost,
                    is_metered=True,
                    usage_status="metered",
                    relay_status="ok",
                    relay_cost_usd=relay_cost,
                    isolated_home_used=True,
                ),
                verdict=GraderVerdict(passed=True, exit_code=0, anti_cheat_clean=True, summary="Unit tests passed (exit 0)"),
                steps=[TrajectoryStep(step_index=1, timestamp_ms=50, kind="verify", summary="Deterministic Grader -> PASS", detail="ok")],
                git_diff="--- a/rate_limiter.py\n+++ b/rate_limiter.py\n",
                started_at="2026-11-15T00:00:00Z",
                finished_at="2026-11-15T00:00:01Z",
            )
        )

    assert main(["--lang", "en", "compare", "--db", str(db_path)]) == 0
    out_en = capsys.readouterr().out
    assert "$0.0019" in out_en and "$0.0142" in out_en
    assert "$0.0027" in out_en and "$0.0188" in out_en
    assert "Relay" in out_en

    assert main(["--lang", "zh", "compare", "--db", str(db_path)]) == 0
    out_zh = capsys.readouterr().out
    assert "$0.0019" in out_zh and "$0.0142" in out_zh
    assert "$0.0027" in out_zh and "$0.0188" in out_zh
    assert "Relay" in out_zh

    html_en = tmp_path / "kr3_en.html"
    html_zh = tmp_path / "kr3_zh.html"
    assert main(["--lang", "en", "export-html", "--db", str(db_path), "--out", str(html_en)]) == 0
    assert main(["--lang", "zh", "export-html", "--db", str(db_path), "--out", str(html_zh)]) == 0
    capsys.readouterr()

    txt_en = html_en.read_text(encoding="utf-8")
    txt_zh = html_zh.read_text(encoding="utf-8")
    for txt in (txt_en, txt_zh):
        assert "$0.0019" in txt and "$0.0142" in txt
        assert "$0.0027" in txt and "$0.0188" in txt
    assert audit_html_craft(html_en).passed
    assert audit_html_craft(html_zh).passed


def test_kr5_simulated_verdict_line_carries_simulated_tag_in_cli_and_html(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """KR5: --cli simulate final verdict line in EN/ZH CLI and HTML includes [SIMULATED] / [SIMULATED | 模拟]."""
    db_win = tmp_path / "sim_win.sqlite"
    html_en = tmp_path / "sim_win_en.html"
    html_zh = tmp_path / "sim_win_zh.html"

    # 6 non-UI builtin tasks x 5 reps -> simulated Treatment wins with a task-paired 95% CI > 0.
    # (The 2-task concurrency suite is too few: the task, not the trial, is the unit of analysis.
    # UI tasks are left out because their simulated page diffs are embedded in the report HTML.)
    import json as _json

    from skill_arena.suites.builtin_tasks import load_builtin_suite

    suite_file = tmp_path / "non_ui_suite.json"
    suite_file.write_text(
        _json.dumps([t.to_dict() for t in load_builtin_suite() if not t.task_id.startswith("ui-")]),
        encoding="utf-8",
    )
    assert (
        main(
            [
                "--lang",
                "en",
                "run",
                "--cli",
                "simulate",
                "--suite",
                str(suite_file),
                "--reps",
                "5",
                "--db",
                str(db_win),
                "--html-out",
                str(html_en),
            ]
        )
        == 0
    )
    out_en = capsys.readouterr().out
    verdict_lines_en = [ln for ln in out_en.splitlines() if ln.startswith("Verdict:")]
    assert len(verdict_lines_en) == 1
    assert verdict_lines_en[0].startswith(
        "Verdict: [SIMULATED] Treatment statistically outperforms Control"
    ), verdict_lines_en[0]

    assert main(["--lang", "zh", "compare", "--db", str(db_win)]) == 0
    out_zh = capsys.readouterr().out
    verdict_lines_zh = [ln for ln in out_zh.splitlines() if ln.startswith("统计结论：")]
    assert len(verdict_lines_zh) == 1
    assert verdict_lines_zh[0].startswith("统计结论：[SIMULATED | 模拟] 实验组显著占优"), verdict_lines_zh[0]

    assert main(["--lang", "zh", "export-html", "--db", str(db_win), "--out", str(html_zh)]) == 0
    capsys.readouterr()

    html_en_txt = html_en.read_text(encoding="utf-8")
    html_zh_txt = html_zh.read_text(encoding="utf-8")
    assert "Verdict: [SIMULATED] Treatment wins (95% CI &gt; 0)" in html_en_txt or (
        "Verdict: [SIMULATED] Treatment wins (95% CI > 0)" in html_en_txt
    )
    assert "结论：[SIMULATED | 模拟] 实验组显著占优" in html_zh_txt
    assert audit_html_craft(html_en).passed
    assert audit_html_craft(html_zh).passed


def test_kr5_readmes_disclose_newcombe_wilson_repeated_reps_independence_assumption() -> None:
    """KR5: Both READMEs explain that repeated task runs are not independent samples and how the interval handles it."""
    repo_root = Path(__file__).resolve().parents[1]
    readme_en = (repo_root / "README.md").read_text(encoding="utf-8")
    readme_zh = (repo_root / "README.zh-CN.md").read_text(encoding="utf-8")

    assert "independent" in readme_en.lower()
    assert "--reps" in readme_en
    assert "narrower" in readme_en.lower()
    assert "minimum detectable effect" in readme_en.lower()
    assert "invalid run" in readme_en.lower()

    assert "独立样本" in readme_zh or "独立试验" in readme_zh
    assert "--reps" in readme_zh
    assert "偏窄" in readme_zh
    assert "最小可检测差别" in readme_zh
    assert "实验无效" in readme_zh


def test_kr5_real_ab_gate_script_executes_offline_against_local_stub_and_fake_upstream(
    tmp_path: Path,
) -> None:
    """KR5: Scripts/acceptance/real-ab-gate.py is wired into test.sh/m1-acceptance.sh and runs end-to-end offline."""
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    import threading

    repo_root = Path(__file__).resolve().parents[1]
    gate_script = repo_root / "Scripts" / "acceptance" / "real-ab-gate.py"
    assert gate_script.is_file(), "Missing Scripts/acceptance/real-ab-gate.py"

    test_sh = (repo_root / "Scripts" / "test.sh").read_text(encoding="utf-8")
    m1_sh = (repo_root / "Scripts" / "acceptance" / "m1-acceptance.sh").read_text(encoding="utf-8")
    assert "real-ab-gate.py" in test_sh
    assert "real-ab-gate.py" in m1_sh

    class _FakeAnthropicSSE(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args: object) -> None:
            return

        def do_POST(self) -> None:  # noqa: N802
            n = int(self.headers.get("Content-Length", "0"))
            _ = self.rfile.read(n)
            sse = (
                b'event: message_start\n'
                b'data: {"type":"message_start","message":{"model":"claude-sonnet-4-5-20250929","usage":{"input_tokens":320,"cache_read_input_tokens":80,"output_tokens":1}}}\n\n'
                b'event: message_delta\n'
                b'data: {"type":"message_delta","usage":{"output_tokens":45}}\n\n'
                b'event: message_stop\n'
                b'data: {"type":"message_stop"}\n\n'
            )
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(sse)))
            self.end_headers()
            self.wfile.write(sse)

    srv = ThreadingHTTPServer(("127.0.0.1", 0), _FakeAnthropicSSE)
    srv_thread = threading.Thread(target=srv.serve_forever, daemon=True)
    srv_thread.start()
    upstream_url = f"http://127.0.0.1:{srv.server_address[1]}"

    try:
        t0 = load_builtin_suite()[0]
        ref_code = t0.reference_solution_files["rate_limiter.py"]
        suite_path = tmp_path / "gate_one_task.json"
        suite_path.write_text(json.dumps([t0.to_dict()]), encoding="utf-8")

        skill_dir = tmp_path / "gate-skill"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text("# Gate Skill\nUse lock.\n", encoding="utf-8")

        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        stub_claude = _write_executable(
            bin_dir / "claude",
            (
                f"#!{sys.executable}\n"
                "import json, os, sys, urllib.request\n"
                "from pathlib import Path\n"
                "_ = sys.stdin.read()\n"
                "base = os.environ['ANTHROPIC_BASE_URL'].rstrip('/')\n"
                "req = urllib.request.Request(\n"
                "    base + '/v1/messages',\n"
                "    data=json.dumps({'model': 'claude-sonnet-4-5-20250929', 'stream': True}).encode('utf-8'),\n"
                "    headers={'Content-Type': 'application/json'},\n"
                "    method='POST',\n"
                ")\n"
                "with urllib.request.urlopen(req, timeout=5) as resp:\n"
                "    _ = resp.read()\n"
                f"Path('rate_limiter.py').write_text({ref_code!r}, encoding='utf-8')\n"
                "print(json.dumps({'result': 'Fixed.\\nSTATUS: success', 'total_cost_usd': 0.0085}))\n"
            ),
        )

        db_out = tmp_path / "gate.sqlite"
        html_out = tmp_path / "gate.html"
        env = os.environ.copy()
        env["HOME"] = str(tmp_path / "isolated_caller_home")
        Path(env["HOME"]).mkdir(parents=True, exist_ok=True)
        env["RELAY_CLAUDE_BIN"] = str(stub_claude)

        proc = subprocess.run(
            [
                sys.executable,
                str(gate_script),
                "--cli",
                "claude",
                "--model",
                "claude-sonnet-4-5",
                "--suite",
                str(suite_path),
                "--skill",
                str(skill_dir),
                "--reps",
                "1",
                "--budget-usd",
                "0.50",
                "--upstream-url",
                upstream_url,
                "--db",
                str(db_out),
                "--html-out",
                str(html_out),
            ],
            cwd=repo_root,
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        assert proc.returncode == 0, f"STDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
        assert "REAL_AB_GATE PASS" in proc.stdout
    finally:
        srv.shutdown()
        srv.server_close()


def test_kr7_real_ab_gate_defaults_copy_auth_false_and_threads_pass_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """KR7 + KR6: real-ab-gate.py defaults copy_auth to False and threads --pass-env cleanly."""
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    import importlib.util
    import threading

    repo_root = Path(__file__).resolve().parents[1]
    gate_script = repo_root / "Scripts" / "acceptance" / "real-ab-gate.py"
    spec = importlib.util.spec_from_file_location("real_ab_gate", gate_script)
    assert spec is not None and spec.loader is not None
    gate_mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gate_mod)

    monkeypatch.delenv("SKILL_ARENA_REAL_COPY_AUTH", raising=False)
    default_args = gate_mod._parse_args([])
    assert default_args.copy_auth is False, (
        f"real-ab-gate.py must default copy_auth to False (matching skill-arena CLI), got {default_args.copy_auth!r}"
    )
    assert default_args.pass_env == []
    assert gate_mod._parse_args(["--copy-auth"]).copy_auth is True
    assert gate_mod._parse_args(["--no-copy-auth"]).copy_auth is False
    parsed_env = gate_mod._parse_args(["--pass-env", "OPENAI_API_KEY", "--pass-env", "MISTRAL_API_KEY"])
    assert parsed_env.pass_env == ["OPENAI_API_KEY", "MISTRAL_API_KEY"]

    help_proc = subprocess.run(
        [sys.executable, str(gate_script), "--help"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=True,
    )
    assert "(default)" not in next(
        (ln for ln in help_proc.stdout.splitlines() if "--copy-auth" in ln), ""
    )

    class _FakeAnthropicJSON(BaseHTTPRequestHandler):
        def log_message(self, *args: object) -> None:
            return

        def do_POST(self) -> None:  # noqa: N802
            n = int(self.headers.get("Content-Length", "0"))
            _ = self.rfile.read(n)
            body = json.dumps(
                {
                    "id": "msg_kr7",
                    "type": "message",
                    "model": "claude-sonnet-4-5",
                    "usage": {
                        "input_tokens": 200,
                        "cache_read_input_tokens": 50,
                        "cache_creation_input_tokens": 0,
                        "output_tokens": 30,
                    },
                }
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    srv = ThreadingHTTPServer(("127.0.0.1", 0), _FakeAnthropicJSON)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    upstream_url = f"http://127.0.0.1:{srv.server_address[1]}"

    try:
        t0 = load_builtin_suite()[0]
        ref_code = t0.reference_solution_files["rate_limiter.py"]
        suite_path = tmp_path / "kr7_one_task.json"
        suite_path.write_text(json.dumps([t0.to_dict()]), encoding="utf-8")

        skill_dir = tmp_path / "kr7-skill"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text("# KR7 Skill\n", encoding="utf-8")

        caller_home = tmp_path / "caller_home_with_creds"
        (caller_home / ".claude").mkdir(parents=True)
        (caller_home / ".claude" / ".credentials.json").write_text('{"secret":"host-cred"}', encoding="utf-8")

        obs_dir = tmp_path / "kr7_obs"
        obs_dir.mkdir()
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        stub_claude = _write_executable(
            bin_dir / "claude",
            (
                f"#!{sys.executable}\n"
                "import json, os, sys, urllib.request, uuid\n"
                "from pathlib import Path\n"
                "_ = sys.stdin.read()\n"
                "home = Path(os.environ['HOME'])\n"
                "rec = {\n"
                "    'cred_copied': (home / '.claude' / '.credentials.json').exists(),\n"
                "    'openai_key': os.environ.get('OPENAI_API_KEY'),\n"
                "    'anthropic_key': os.environ.get('ANTHROPIC_API_KEY'),\n"
                "}\n"
                f"(Path({str(obs_dir)!r}) / f'obs_{{uuid.uuid4().hex[:8]}}.json').write_text(json.dumps(rec), encoding='utf-8')\n"
                "base = os.environ['ANTHROPIC_BASE_URL'].rstrip('/')\n"
                "req = urllib.request.Request(\n"
                "    base + '/v1/messages',\n"
                "    data=json.dumps({'model': 'claude-sonnet-4-5', 'messages': [{'role': 'user', 'content': 'hi'}]}).encode('utf-8'),\n"
                "    headers={'Content-Type': 'application/json'},\n"
                "    method='POST',\n"
                ")\n"
                "with urllib.request.urlopen(req, timeout=5) as resp:\n"
                "    _ = resp.read()\n"
                f"Path('rate_limiter.py').write_text({ref_code!r}, encoding='utf-8')\n"
                "print(json.dumps({'result': 'Fixed.\\nSTATUS: success', 'total_cost_usd': 0.0085}))\n"
            ),
        )

        env = os.environ.copy()
        env["HOME"] = str(caller_home)
        env["RELAY_CLAUDE_BIN"] = str(stub_claude)
        env["OPENAI_API_KEY"] = "kr7-named-oai"
        env["ANTHROPIC_API_KEY"] = "kr7-unnamed-ant"

        proc = subprocess.run(
            [
                sys.executable,
                str(gate_script),
                "--cli",
                "claude",
                "--model",
                "claude-sonnet-4-5",
                "--suite",
                str(suite_path),
                "--skill",
                str(skill_dir),
                "--reps",
                "1",
                "--budget-usd",
                "0.50",
                "--pass-env",
                "OPENAI_API_KEY",
                "--upstream-url",
                upstream_url,
                "--db",
                str(tmp_path / "kr7.sqlite"),
                "--html-out",
                str(tmp_path / "kr7.html"),
            ],
            cwd=repo_root,
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        assert proc.returncode == 0, f"STDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
        obs_records = [json.loads(p.read_text(encoding="utf-8")) for p in sorted(obs_dir.glob("*.json"))]
        assert len(obs_records) == 2
        for rec in obs_records:
            assert rec["cred_copied"] is False, f"Default real-ab-gate.py copied host credentials: {rec}"
            assert rec["openai_key"] == "kr7-named-oai", rec
            assert rec["anthropic_key"] is None, f"Un-named ANTHROPIC_API_KEY leaked via real-ab-gate.py: {rec}"
    finally:
        srv.shutdown()
        srv.server_close()


def test_kr8_cli_run_prints_per_trial_relay_cost_line_en_and_zh(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """KR8: `skill-arena run` per-trial stdout lines print `relay=$...` matching summed `.relay/LOG.md` step costs."""
    fake_home = tmp_path / "caller_home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))

    t0 = load_builtin_suite()[0]
    ref_code = t0.reference_solution_files["rate_limiter.py"]
    suite_path = tmp_path / "kr8_relay_line_task.json"
    suite_path.write_text(json.dumps([t0.to_dict()]), encoding="utf-8")

    skill_dir = tmp_path / "kr8-skill"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text("# KR8 Skill\n", encoding="utf-8")

    # Stub appends a helper step row ($0.0040) to .relay/LOG.md AND returns its main step cost ($0.0095 ctrl / $0.0205 treat)
    # so summed .relay/LOG.md trial costs are $0.0135 (control) and $0.0245 (treatment).
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub_claude = _write_executable(
        bin_dir / "claude",
        (
            f"#!{sys.executable}\n"
            "import json, os, sys\n"
            "from pathlib import Path\n"
            "_ = sys.stdin.read()\n"
            "log_md = Path('.relay/LOG.md')\n"
            "log_md.parent.mkdir(parents=True, exist_ok=True)\n"
            "with log_md.open('a', encoding='utf-8') as fh:\n"
            "    fh.write('## 00_helper: ok, 1s, 2026-11-15 00:00\\n- cost $0.0040\\n\\n')\n"
            "arm = os.environ.get('SKILL_ARENA_CONTENDER', '')\n"
            "step_cost = 0.0095 if arm == 'arm_control' else 0.0205\n"
            f"Path('rate_limiter.py').write_text({ref_code!r}, encoding='utf-8')\n"
            "print(json.dumps({'result': 'Fixed.\\nSTATUS: success', 'total_cost_usd': step_cost}))\n"
        ),
    )
    monkeypatch.setenv("RELAY_CLAUDE_BIN", str(stub_claude))

    for lang_code, pass_badge in (("en", "[PASS]"), ("zh", "[通过]")):
        db_file = tmp_path / f"relay_line_{lang_code}.sqlite"
        rc = main(
            [
                "--lang",
                lang_code,
                "run",
                "--cli",
                "claude",
                "--model",
                "claude-sonnet-4-5",
                "--skill",
                str(skill_dir),
                "--suite",
                str(suite_path),
                "--reps",
                "1",
                "--db",
                str(db_file),
            ]
        )
        out = capsys.readouterr().out
        assert rc == 0, out
        trial_lines = [ln for ln in out.splitlines() if ln.startswith(pass_badge)]
        assert len(trial_lines) == 2, f"Expected 2 per-trial stdout lines for {lang_code}, got: {trial_lines}\n{out}"
        assert "| relay=$0.0135 |" in trial_lines[0], trial_lines[0]
        assert "| relay=$0.0245 |" in trial_lines[1], trial_lines[1]






