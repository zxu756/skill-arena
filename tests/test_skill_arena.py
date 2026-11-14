"""Comprehensive test suite for SkillArena sandbox, anti-cheat, wire-tap proxy, git extractor, and Studio."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import urllib.request

from skill_arena import __version__
from skill_arena.cli import main, run_ab_experiment
from skill_arena.extractor.git_commit_extractor import extract_task_from_commit
from skill_arena.graders.deterministic import evaluate_sandbox
from skill_arena.i18n import resolve_html_lang_mode, resolve_lang
from skill_arena.models import ContenderConfig
from skill_arena.runners.harness_driver import HarnessDriver
from skill_arena.runners.xray_bridge import XRayMeteringBridge
from skill_arena.sandbox.skill_injector import SkillInjector
from skill_arena.sandbox.worktree_pool import WorktreeSandboxPool
from skill_arena.store.arena_db import ArenaDatabase
from skill_arena.studio.server import create_studio_server
from skill_arena.studio.templates import render_arena_html
from skill_arena.suites.builtin_tasks import load_builtin_suite


def test_builtin_8_tasks_fail_unpatched_and_pass_reference(tmp_path: Path) -> None:
    pool = WorktreeSandboxPool(tmp_path / "pool")
    tasks = load_builtin_suite()
    assert len(tasks) == 8

    for task in tasks:
        # 1. Unpatched seed must fail deterministic grader
        h_seed = pool.acquire(task, f"{task.task_id}_seed")
        try:
            verdict_seed, _ = evaluate_sandbox(h_seed, task)
            assert not verdict_seed.passed, f"Seed unexpectedly passed for {task.task_id}"
        finally:
            pool.release(h_seed)

        # 2. Reference solution must pass 100% cleanly (including Hallmark frontend craft checks)
        h_ref = pool.acquire(task, f"{task.task_id}_ref")
        try:
            for rel, content in task.reference_solution_files.items():
                dest = h_ref.root / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_text(content, encoding="utf-8")
            verdict_ref, diff_text = evaluate_sandbox(h_ref, task)
            assert verdict_ref.passed, f"Reference failed on {task.task_id}: {verdict_ref.summary}\n{verdict_ref.stdout_tail}"
            assert verdict_ref.anti_cheat_clean
            assert len(diff_text) > 0
        finally:
            pool.release(h_ref)


def test_anti_cheat_catches_test_tampering(tmp_path: Path) -> None:
    pool = WorktreeSandboxPool(tmp_path / "pool")
    driver = HarnessDriver(pool)
    task = load_builtin_suite()[0]
    cheater = ContenderConfig(
        contender_id="arm_cheater",
        label="[SIMULATED] Test-Tampering Agent",
        role="treatment",
        cli="simulate:cheater",
        model="synthetic-sim-model",
    )
    result = driver.run_trial("exp_cheat", task, cheater, rep_index=1)
    assert not result.anti_cheat_clean
    assert not result.passed
    assert "tests/test_rate_limiter.py" in result.verdict.tampered_test_files
    assert "ANTI-CHEAT VIOLATION" in result.verdict.summary


def test_skill_injector_mounts_skill_tree_and_estimates_tokens(tmp_path: Path) -> None:
    skill_dir = tmp_path / "sample-skill"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text("# Concurrency Guardrails\nAlways lock shared state.\n" * 8, encoding="utf-8")

    workspace = tmp_path / "ws"
    workspace.mkdir()
    contender = ContenderConfig(
        contender_id="arm_treat",
        label="With Skill",
        role="treatment",
        cli="mock:skilled",
        skill_paths=[str(skill_dir)],
    )
    report = SkillInjector.apply(workspace, contender)
    assert report.mounted_skill_names == ["sample-skill"]
    assert report.estimated_tokens > 40
    assert (workspace / ".agents/skills/sample-skill/SKILL.md").is_file()
    assert (workspace / ".claude/skills/sample-skill/SKILL.md").is_file()
    assert "sample-skill" in (workspace / "AGENTS.md").read_text(encoding="utf-8")


def test_wire_tap_proxy_meters_json_and_sse_usage() -> None:
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    import threading

    class Upstream(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args: object) -> None:
            return

        def do_POST(self) -> None:  # noqa: N802
            n = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(n) if n > 0 else b"{}"
            req_obj = json.loads(raw.decode("utf-8"))
            if req_obj.get("stream"):
                sse_body = (
                    b'data: {"type":"message_start","message":{"model":"claude-sonnet-4-5","usage":{"input_tokens":900,"cache_read_input_tokens":100,"output_tokens":0}}}\n\n'
                    b'data: {"type":"message_delta","usage":{"output_tokens":180}}\n\n'
                    b"data: [DONE]\n\n"
                )
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(sse_body)
            else:
                body = json.dumps(
                    {
                        "model": "claude-sonnet-4-5",
                        "usage": {
                            "input_tokens": 1500,
                            "cache_read_input_tokens": 500,
                            "output_tokens": 250,
                        },
                    }
                ).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(body)

    up = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
    threading.Thread(target=up.serve_forever, daemon=True).start()
    try:
        up_url = f"http://127.0.0.1:{up.server_address[1]}"
        bridge = XRayMeteringBridge(anthropic_upstream=up_url, openai_upstream=up_url)
        bridge.start()
        try:
            req = urllib.request.Request(
                f"{bridge.anthropic_base_url}/v1/messages",
                data=b'{"model":"claude-sonnet-4-5","messages":[{"role":"user","content":"hi"}]}',
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                assert resp.status == 200
                resp.read()

            req_sse = urllib.request.Request(
                f"{bridge.anthropic_base_url}/v1/messages",
                data=b'{"model":"claude-sonnet-4-5","stream":true,"messages":[{"role":"user","content":"hi2"}]}',
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req_sse, timeout=5) as resp_sse:
                assert resp_sse.status == 200
                resp_sse.read()

            ledger, turns = bridge.summarize(injected_skill_tokens_est=320)
            assert ledger.turns == 2
            assert ledger.first_turn_prompt_tokens == 2000
            assert ledger.uncached_input_tokens == 2400
            assert ledger.cached_prompt_tokens == 600
            assert ledger.total_prompt_tokens == 3000
            assert ledger.completion_tokens == 430
            assert ledger.total_cost_usd is not None and ledger.total_cost_usd > 0
            assert len(turns) == 2
            assert turns[1].prompt_tokens == 1000
            assert turns[1].cached_tokens == 100
            assert turns[1].completion_tokens == 180
        finally:
            bridge.stop()
    finally:
        up.shutdown()
        up.server_close()


def test_git_commit_extractor_and_detached_worktree_execution(tmp_path: Path) -> None:
    repo = tmp_path / "mini_repo"
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
    (repo / "calc.py").write_text("def clamp(x: int, lo: int, hi: int) -> int:\n    return max(lo, x)\n", encoding="utf-8")
    (repo / "tests").mkdir()
    (repo / "tests/test_calc.py").write_text(
        "import unittest\nfrom calc import clamp\nclass T(unittest.TestCase):\n    def test_lo(self): self.assertEqual(clamp(-1, 0, 10), 0)\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "add", "-A"], cwd=repo, env=git_env, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo, env=git_env, check=True, capture_output=True)

    # Create bug-fix commit touching both calc.py and tests/test_calc.py
    (repo / "calc.py").write_text(
        "def clamp(x: int, lo: int, hi: int) -> int:\n    return min(hi, max(lo, x))\n",
        encoding="utf-8",
    )
    (repo / "tests/test_calc.py").write_text(
        "import unittest\nfrom calc import clamp\nclass T(unittest.TestCase):\n"
        "    def test_lo(self): self.assertEqual(clamp(-1, 0, 10), 0)\n"
        "    def test_hi(self): self.assertEqual(clamp(25, 0, 10), 10)\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "add", "-A"], cwd=repo, env=git_env, check=True)
    subprocess.run(
        ["git", "commit", "-m", "fix: enforce upper bound in clamp()"],
        cwd=repo,
        env=git_env,
        check=True,
        capture_output=True,
    )

    spec = extract_task_from_commit(repo, "HEAD")
    assert spec.category == "git_extracted"
    assert spec.protected_test_paths == ["tests/test_calc.py"]

    pool = WorktreeSandboxPool(tmp_path / "wt_pool")
    handle = pool.acquire(spec, "wt_trial")
    try:
        # Unpatched parent commit + new test patch should fail initially
        verdict_before, _ = evaluate_sandbox(handle, spec)
        assert not verdict_before.passed

        # Apply fix inside isolated git worktree -> should pass!
        (handle.root / "calc.py").write_text(
            "def clamp(x: int, lo: int, hi: int) -> int:\n    return min(hi, max(lo, x))\n",
            encoding="utf-8",
        )
        verdict_after, _ = evaluate_sandbox(handle, spec)
        assert verdict_after.passed
    finally:
        pool.release(handle)


def test_full_ab_experiment_db_studio_and_html_export(tmp_path: Path) -> None:
    db = ArenaDatabase(tmp_path / "arena.sqlite")
    tasks = load_builtin_suite()[:4]
    contenders = [
        ContenderConfig(
            contender_id="ctrl",
            label="[SIMULATED] Baseline Synthetic",
            role="control",
            cli="simulate:baseline",
            model="synthetic-sim-model",
        ),
        ContenderConfig(
            contender_id="treat",
            label="[SIMULATED] Treatment Synthetic",
            role="treatment",
            cli="simulate:skilled",
            model="synthetic-sim-model",
        ),
    ]
    exp_id = run_ab_experiment(
        db=db,
        title="[SIMULATED] Synthetic Calibration",
        suite_name="subset-4",
        tasks=tasks,
        contenders=contenders,
        k_reps=2,
        quiet=True,
    )
    report = db.build_comparison_report(exp_id)
    assert report["is_simulated"] is True
    lb = {r["contender_id"]: r for r in report["leaderboard"]}
    assert lb["treat"]["pass_at_1"] > lb["ctrl"]["pass_at_1"]
    assert lb["ctrl"]["pass_hat_k"] == 0.0
    assert lb["treat"]["pass_hat_k"] == 1.0
    assert lb["treat"]["context_overhead_delta_tokens"] > 0

    html_doc = render_arena_html(report)
    assert "SkillArena Telemetry" in html_doc
    assert "Dual-track trajectory replay" in html_doc

    server = create_studio_server(db, host="127.0.0.1", port=0)
    import threading

    th = threading.Thread(target=server.serve_forever, daemon=True)
    th.start()
    try:
        port = server.server_address[1]
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/comparison/{exp_id}", timeout=5) as resp:
            api_json = json.loads(resp.read().decode("utf-8"))
            assert api_json["experiment_id"] == exp_id
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/?lang=zh", timeout=5) as resp_zh:
            studio_zh = resp_zh.read().decode("utf-8")
            assert 'lang="zh-CN"' in studio_zh
            assert "参赛智能体 A/B 排行榜" in studio_zh
    finally:
        server.shutdown()
        server.server_close()


def test_version_and_bilingual_lang_resolution() -> None:
    assert __version__ == "0.2.0"

    # Explicit CLI flag overrides env and locale
    assert resolve_lang("zh", {"SKILL_ARENA_LANG": "en", "LC_ALL": "en_US.UTF-8"}) == "zh"
    assert resolve_lang("en", {"SKILL_ARENA_LANG": "zh", "LC_ALL": "zh_CN.UTF-8"}) == "en"

    # SKILL_ARENA_LANG overrides POSIX locale when CLI is auto
    assert resolve_lang("auto", {"SKILL_ARENA_LANG": "zh_CN", "LC_ALL": "en_US.UTF-8"}) == "zh"
    assert resolve_lang("auto", {"SKILL_ARENA_LANG": "en", "LC_ALL": "zh_CN.UTF-8"}) == "en"

    # System locale zh* auto-detection
    assert resolve_lang("auto", {"LC_ALL": "zh_CN.UTF-8"}) == "zh"
    assert resolve_lang("auto", {"LANG": "zh_Hans_CN.UTF-8"}) == "zh"
    assert resolve_lang("auto", {"LANG": "en_US.UTF-8"}) == "en"
    assert resolve_lang("auto", {}) == "en"

    assert resolve_html_lang_mode("zh", {}) == ("zh", "zh")
    assert resolve_html_lang_mode("en", {}) == ("en", "en")
    assert resolve_html_lang_mode("auto", {"LANG": "en_US.UTF-8"}) == ("en", "auto")
    assert resolve_html_lang_mode("auto", {"LC_ALL": "zh_CN.UTF-8"}) == ("zh", "zh")


def test_cli_and_html_bilingual_zh_en_outputs(tmp_path: Path, capsys, monkeypatch) -> None:
    monkeypatch.setenv("SKILL_ARENA_LANG", "")
    monkeypatch.setenv("LC_ALL", "en_US.UTF-8")

    # 1. list-suites in Chinese & English (testing --lang both before and after subcommand)
    rc_zh = main(["--lang", "zh", "list-suites", "--category", "concurrency"])
    assert rc_zh == 0
    out_zh = capsys.readouterr().out
    assert "并发控制" in out_zh
    assert "TokenBucket" in out_zh

    rc_en = main(["list-suites", "--lang", "en", "--category", "concurrency"])
    assert rc_en == 0
    out_en = capsys.readouterr().out
    assert "[concurrency" in out_en
    assert "Thread-safe TokenBucket burst" in out_en

    # 2. compare & export-html in Chinese and English
    db_path = tmp_path / "bilingual.sqlite"
    db = ArenaDatabase(db_path)
    tasks = load_builtin_suite()[:2]
    contenders = [
        ContenderConfig(
            contender_id="ctrl",
            label="[SIMULATED] Baseline No Skill",
            role="control",
            cli="simulate:baseline",
            model="synthetic-sim-model",
        ),
        ContenderConfig(
            contender_id="treat",
            label="[SIMULATED] Treatment + Synthetic Skill Pack",
            role="treatment",
            cli="simulate:skilled",
            model="synthetic-sim-model",
        ),
    ]
    exp_id = run_ab_experiment(
        db=db,
        title="[SIMULATED] Synthetic Pipeline Calibration",
        suite_name="subset-2",
        tasks=tasks,
        contenders=contenders,
        k_reps=2,
        quiet=True,
        lang="zh",
    )

    assert main(["compare", "--lang", "zh", "--db", str(db_path), "--exp", exp_id]) == 0
    cmp_zh = capsys.readouterr().out
    assert "参赛组别" in cmp_zh
    assert "对照组" in cmp_zh
    assert "实验组" in cmp_zh
    assert "成对胜率换算的 Elo（双组）" in cmp_zh
    assert "SIMULATED" in cmp_zh

    assert main(["--lang", "en", "compare", "--db", str(db_path), "--exp", exp_id]) == 0
    cmp_en = capsys.readouterr().out
    assert "Contender" in cmp_en
    assert "control" in cmp_en
    assert "treatment" in cmp_en
    assert "Win-Rate Derived Elo (2-arm)" in cmp_en
    assert "SIMULATED" in cmp_en

    report = db.build_comparison_report(exp_id)
    lb = {r["contender_id"]: r for r in report["leaderboard"]}
    assert lb["ctrl"]["elo_rating"] == 1500
    assert lb["treat"]["elo_rating"] >= 1500

    html_zh = render_arena_html(report, lang="zh")
    assert 'lang="zh-CN"' in html_zh
    assert 'data-default-lang="zh"' in html_zh
    assert 'data-lang-switch="zh"' in html_zh
    assert 'data-lang-switch="en"' in html_zh
    assert "参赛智能体 A/B 排行榜" in html_zh
    assert "逐任务 Worktree 沙盒遥测矩阵" in html_zh
    assert 'data-task-filter="concurrency"' in html_zh
    assert "成对胜率换算的 Elo（双组）" in html_zh
    assert "SIMULATED" in html_zh
    assert "\u2014" not in html_zh
    assert "\u2013" not in html_zh

    html_en = render_arena_html(report, lang="en")
    assert 'lang="en"' in html_en
    assert 'data-default-lang="en"' in html_en
    assert "Contender A/B scoreboard" in html_en
    assert "Dual-track trajectory replay" in html_en
    assert "Win-Rate Derived Elo (2-arm)" in html_en
    assert "SIMULATED" in html_en
    assert "\u2014" not in html_en
    assert "\u2013" not in html_en
