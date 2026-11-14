"""Scoped tests for K6 git commit extractor: zero commit-message leak, git history isolation, and pytest/unittest detection."""

from __future__ import annotations

import os
from pathlib import Path
import stat
import subprocess

import pytest

from skill_arena.extractor.git_commit_extractor import extract_task_from_commit
from skill_arena.models import ContenderConfig
from skill_arena.runners.harness_driver import HarnessDriver
from skill_arena.sandbox.worktree_pool import WorktreeSandboxPool


def _git_env() -> dict[str, str]:
    env = os.environ.copy()
    env.update(
        {
            "GIT_AUTHOR_NAME": "Tester",
            "GIT_AUTHOR_EMAIL": "tester@example.com",
            "GIT_COMMITTER_NAME": "Tester",
            "GIT_COMMITTER_EMAIL": "tester@example.com",
        }
    )
    return env


def test_extract_commit_strips_commit_message_and_isolates_git_history(tmp_path: Path) -> None:
    repo = tmp_path / "bugfix_repo"
    repo.mkdir()
    env = _git_env()
    subprocess.run(["git", "init", "-b", "main"], cwd=repo, env=env, check=True, capture_output=True)

    (repo / "calc.py").write_text("def total(n: int) -> int:\n    return sum(range(n))\n", encoding="utf-8")
    (repo / "tests").mkdir()
    (repo / "tests" / "test_calc.py").write_text(
        "import unittest\nfrom calc import total\nclass T(unittest.TestCase):\n"
        "    def test_zero(self): self.assertEqual(total(0), 0)\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "add", "-A"], cwd=repo, env=env, check=True)
    subprocess.run(["git", "commit", "-m", "initial buggy version"], cwd=repo, env=env, check=True, capture_output=True)

    # Fix commit with explicit answer in subject and body
    (repo / "calc.py").write_text("def total(n: int) -> int:\n    return sum(range(n + 1))\n", encoding="utf-8")
    (repo / "tests" / "test_calc.py").write_text(
        "import unittest\nfrom calc import total\nclass T(unittest.TestCase):\n"
        "    def test_zero(self): self.assertEqual(total(0), 0)\n"
        "    def test_four(self): self.assertEqual(total(4), 10)\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "add", "-A"], cwd=repo, env=env, check=True)
    secret_subject = "fix(calc): off-by-one upper range bound using n + 1"
    secret_body = "SecretAnswerHint9921: change range(n) to range(n + 1) inside total()."
    subprocess.run(
        ["git", "commit", "-m", secret_subject, "-m", secret_body],
        cwd=repo,
        env=env,
        check=True,
        capture_output=True,
    )
    fix_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()

    spec = extract_task_from_commit(repo, "HEAD")
    combined_spec_text = f"{spec.task_id}\n{spec.title}\n{spec.prompt}"
    assert "off-by-one" not in combined_spec_text
    assert "n + 1" not in combined_spec_text
    assert "SecretAnswerHint9921" not in combined_spec_text
    assert secret_subject not in combined_spec_text
    assert secret_body not in combined_spec_text

    pool = WorktreeSandboxPool(tmp_path / "pool")
    handle = pool.acquire(spec, "isolated_hist")
    try:
        log_all = subprocess.run(
            ["git", "log", "--all", "--format=%H %s %b"],
            cwd=handle.root,
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        assert fix_sha not in log_all, "Fix commit SHA leaked into sandbox git history!"
        assert fix_sha[:7] not in log_all
        assert "SecretAnswerHint9921" not in log_all
        assert "off-by-one" not in log_all

        cat_fix = subprocess.run(
            ["git", "cat-file", "-e", fix_sha],
            cwd=handle.root,
            capture_output=True,
            check=False,
        )
        assert cat_fix.returncode != 0, "Fix commit object must not exist in sandbox .git store!"
    finally:
        pool.release(handle)
        pool.close()

    src_status = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert src_status == ""


def test_extract_commit_autodetects_pytest_and_noop_stub_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "pytest_repo"
    repo.mkdir()
    env = _git_env()
    subprocess.run(["git", "init", "-b", "main"], cwd=repo, env=env, check=True, capture_output=True)

    (repo / "m.py").write_text("def f():\n    return 2\n", encoding="utf-8")
    (repo / "tests").mkdir()
    (repo / "tests" / "test_m.py").write_text("from m import f\ndef test_two(): assert f() == 2\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=repo, env=env, check=True)
    subprocess.run(["git", "commit", "-m", "buggy seed"], cwd=repo, env=env, check=True, capture_output=True)

    # Fix commit switches expected f() to 1 using plain pytest style (no unittest.TestCase)
    (repo / "m.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    (repo / "tests" / "test_m.py").write_text(
        "from m import f\ndef test_one(): assert f() == 1\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "add", "-A"], cwd=repo, env=env, check=True)
    subprocess.run(["git", "commit", "-m", "fix f() to return 1"], cwd=repo, env=env, check=True, capture_output=True)

    spec = extract_task_from_commit(repo, "HEAD")
    assert "pytest" in spec.test_command

    # Run a no-op stub agent via HarnessDriver -> MUST FAIL, never pass with 0 tests!
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    noop = bin_dir / "opencode"
    noop.write_text("#!/usr/bin/env python3\nprint('noop')\n", encoding="utf-8")
    noop.chmod(noop.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ.get('PATH', '')}")
    monkeypatch.setenv("HOME", str(tmp_path / "fake_home"))

    pool = WorktreeSandboxPool(tmp_path / "pool")
    driver = HarnessDriver(pool, allow_unisolated_host_home=True)
    contender = ContenderConfig(
        contender_id="ctrl",
        label="No-op Stub",
        role="control",
        cli="opencode",
        model="stub-model",
    )
    result = driver.run_trial("exp_pytest", spec, contender, rep_index=1)
    pool.close()
    assert not result.passed, "No-op stub on pytest repo MUST fail!"


def test_extract_commit_ambiguous_runner_requires_explicit_test_cmd(tmp_path: Path) -> None:
    repo = tmp_path / "custom_repo"
    repo.mkdir()
    env = _git_env()
    subprocess.run(["git", "init", "-b", "main"], cwd=repo, env=env, check=True, capture_output=True)

    (repo / "app.py").write_text("VAL = 0\n", encoding="utf-8")
    (repo / "tests").mkdir()
    (repo / "tests" / "check_runner.py").write_text("import app\nassert app.VAL == 0\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=repo, env=env, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo, env=env, check=True, capture_output=True)

    (repo / "app.py").write_text("VAL = 1\n", encoding="utf-8")
    (repo / "tests" / "check_runner.py").write_text("import app\nassert app.VAL == 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=repo, env=env, check=True)
    subprocess.run(["git", "commit", "-m", "set VAL=1"], cwd=repo, env=env, check=True, capture_output=True)

    with pytest.raises(ValueError, match="--test-cmd"):
        extract_task_from_commit(repo, "HEAD")

    spec_ok = extract_task_from_commit(repo, "HEAD", test_command="python3 tests/check_runner.py")
    assert spec_ok.test_command == "python3 tests/check_runner.py"
