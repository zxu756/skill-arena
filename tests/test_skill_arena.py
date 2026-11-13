"""Comprehensive test suite for SkillArena sandbox, anti-cheat, wire-tap proxy, git extractor, and Studio."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import urllib.request

from skill_arena.cli import run_ab_experiment
from skill_arena.extractor.git_commit_extractor import extract_task_from_commit
from skill_arena.graders.deterministic import evaluate_sandbox
from skill_arena.models import ContenderConfig
from skill_arena.runners.harness_driver import HarnessDriver
from skill_arena.runners.wire_tap_proxy import LoopbackWireTapProxy, _extract_usage_from_sse
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
        label="Test-Tampering Agent",
        role="treatment",
        cli="mock:cheater",
        model="claude-sonnet-4-5",
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
    def synth(path: str, body: dict) -> tuple[int, dict]:
        return (
            200,
            {
                "model": "claude-sonnet-4-5",
                "usage": {
                    "input_tokens": 1500,
                    "cache_read_input_tokens": 500,
                    "output_tokens": 250,
                },
            },
        )

    proxy = LoopbackWireTapProxy(synthetic_responder=synth)
    url = proxy.start()
    try:
        req = urllib.request.Request(
            f"{url}/v1/messages",
            data=b'{"model":"claude-sonnet-4-5"}',
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            assert resp.status == 200
        ledger, turns = proxy.summarize(injected_skill_tokens_est=320)
        assert ledger.turns == 1
        assert ledger.first_turn_prompt_tokens == 2000
        assert ledger.cached_prompt_tokens == 500
        assert ledger.completion_tokens == 250
        assert ledger.total_cost_usd > 0
        assert len(turns) == 1
    finally:
        proxy.stop()

    sse_sample = (
        b'data: {"type":"message_start","message":{"model":"claude-sonnet-4-5","usage":{"input_tokens":900,"cache_read_input_tokens":100,"output_tokens":0}}}\n\n'
        b'data: {"type":"message_delta","usage":{"output_tokens":180}}\n\n'
        b"data: [DONE]\n\n"
    )
    model, p_in, p_cache, p_out = _extract_usage_from_sse(sse_sample, "claude-sonnet-4-5")
    assert model == "claude-sonnet-4-5"
    assert p_in == 1000
    assert p_cache == 100
    assert p_out == 180


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
        ContenderConfig(contender_id="ctrl", label="Baseline No-Skill", role="control", cli="mock:baseline"),
        ContenderConfig(contender_id="treat", label="Treatment + SKILL.md", role="treatment", cli="mock:skilled"),
    ]
    exp_id = run_ab_experiment(
        db=db,
        title="Calibration Benchmark",
        suite_name="subset-4",
        tasks=tasks,
        contenders=contenders,
        k_reps=2,
        quiet=True,
    )
    report = db.build_comparison_report(exp_id)
    lb = {r["contender_id"]: r for r in report["leaderboard"]}
    assert lb["treat"]["pass_at_1"] > lb["ctrl"]["pass_at_1"]
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
    finally:
        server.shutdown()
        server.server_close()
