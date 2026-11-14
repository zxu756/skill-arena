"""CLI, HTML export, simulation guardrails, consent gate, and bilingual metric honesty tests (M0 Items 1, 2, 3, 7, 8)."""

from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import subprocess

import pytest

from skill_arena.cli import main
from skill_arena.graders.frontend_craft import audit_html_craft
from skill_arena.store.arena_db import ArenaDatabase


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
    _write_executable(
        bin_dir / "opencode",
        "#!/bin/sh\n"
        "# Stub opencode CLI that makes no proxy API call\n"
        "exit 0\n",
    )
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")

    from skill_arena.suites.builtin_tasks import load_builtin_suite

    spec_file = tmp_path / "task.json"
    spec_file.write_text(json.dumps(load_builtin_suite()[0].to_dict()), encoding="utf-8")
    db_path = tmp_path / "real_gate.sqlite"

    # 1. Without --allow-unisolated-host-home -> must fail in EN and ZH before running any trial
    rc_no_en = main(
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
    err_zh = capsys.readouterr()
    assert rc_no_zh != 0
    assert "--allow-unisolated-host-home" in (err_zh.out + "\n" + err_zh.err)
    assert "真实" in (err_zh.out + "\n" + err_zh.err)

    # 2. With --allow-unisolated-host-home -> stub runs, records Unmetered / 未计量 (NEVER $0.0000 or 0 tok)
    html_en_path = tmp_path / "unmetered-en.html"
    html_zh_path = tmp_path / "unmetered-zh.html"
    rc_ok = main(
        [
            "--lang",
            "en",
            "run",
            "--cli",
            "opencode",
            "--model",
            "stub-model",
            "--allow-unisolated-host-home",
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
