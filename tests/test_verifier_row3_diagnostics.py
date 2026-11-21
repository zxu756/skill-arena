"""Unit and script tests for M2 Row 3 failure diagnostics and evidence preservation."""

from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import subprocess
from typing import Any

import pytest

from skill_arena.verifier.row3_diagnostics import (
    FLAKY_TARGET_TEST_NOTE,
    evaluate_row3_runs,
)


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _base_verify_payload(*, started_at: str = "2026-11-21T01:00:00+00:00", wall_ms: int = 1200) -> dict[str, Any]:
    return {
        "schema_version": "m2.v1",
        "repo_name": "sample-repo",
        "ref": "HEAD",
        "commit_sha": "52207fd803d419c2e27cba9ae126e30860b6e1cc",
        "conclusion": "FAIL",
        "exit_code": 1,
        "env_guard": {
            "status": "PASS",
            "source_repo_untouched": True,
            "temp_copy_deleted": True,
            "no_leaked_processes": True,
        },
        "build_and_test": {
            "build": {
                "name": "build",
                "command": "uv sync --offline --frozen",
                "status": "PASS",
                "exit_code": 0,
                "reason": "构建成功",
                "output_summary": "Checked 35 packages in <TIME>",
            },
            "test": {
                "name": "test",
                "command": "uv run pytest",
                "status": "PASS",
                "exit_code": 0,
                "reason": "测试通过且用例数 > 0",
                "output_summary": "1154 passed in <TIME>",
                "zero_tests_ran": False,
            },
        },
        "readme_replay": {
            "status": "FAIL",
            "discovered_readmes": ["README.md"],
            "summary_counts": {
                "total_commands": 2,
                "executed_pass": 1,
                "executed_fail": 1,
                "unverifiable": 0,
            },
            "items": [
                {
                    "index": 1,
                    "doc_name": "README.md",
                    "line_no": 10,
                    "command": "uv sync",
                    "status": "PASS",
                    "category": "EXECUTED_PASS",
                    "exit_code": 0,
                    "reason": "离线回放通过（退出码 0）",
                    "output_summary": "ok",
                },
                {
                    "index": 2,
                    "doc_name": "README.md",
                    "line_no": 12,
                    "command": "bash scripts/ci_sim.sh",
                    "status": "FAIL",
                    "category": "EXECUTED_FAIL",
                    "exit_code": 1,
                    "reason": "退出码 1",
                    "output_summary": "failed",
                },
            ],
        },
        "mutation": {
            "enabled": True,
            "status": "PASS",
            "killed_count": 1,
            "survived_count": 0,
            "items": [{"file": "src/app.py", "line_no": 10, "verdict": "KILLED"}],
        },
        "self_report": {"enabled": False, "status": "SKIPPED", "items": []},
        "failures": ["README.md:L12 `bash scripts/ci_sim.sh` -> 退出码 1"],
        "timing_meta": {
            "started_at": started_at,
            "total_wall_ms": wall_ms,
        },
    }


def _write_report_pair(
    directory: Path,
    p1: dict[str, Any],
    p2: dict[str, Any],
    *,
    md1_text: str = "# Run 1 Markdown\n",
    md2_text: str = "# Run 2 Markdown\n",
) -> tuple[Path, Path, Path, Path]:
    directory.mkdir(parents=True, exist_ok=True)
    j1 = directory / "src_run1.json"
    j2 = directory / "src_run2.json"
    m1 = directory / "src_run1.md"
    m2 = directory / "src_run2.md"
    j1.write_text(json.dumps(p1, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    j2.write_text(json.dumps(p2, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    m1.write_text(md1_text, encoding="utf-8")
    m2.write_text(md2_text, encoding="utf-8")
    return j1, j2, m1, m2


def _init_git_repo(repo: Path, files: dict[str, str]) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "-C", str(repo), "init", "-b", "main"], capture_output=True, text=True, check=True)
    for rel, content in files.items():
        dest = repo / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(content, encoding="utf-8")
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "-c",
            "commit.gpgsign=false",
            "add",
            "-A",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "-c",
            "commit.gpgsign=false",
            "commit",
            "-q",
            "-m",
            "init",
        ],
        capture_output=True,
        text=True,
        check=True,
    )


def test_row3_pass_leaves_no_evidence_files_and_preserves_strict_criteria(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_tmp = tmp_path / "fake_tmp"
    fake_tmp.mkdir()
    monkeypatch.setenv("TMPDIR", str(fake_tmp))

    p1 = _base_verify_payload(started_at="2026-11-21T01:00:00+00:00", wall_ms=1100)
    p2 = _base_verify_payload(started_at="2026-11-21T01:05:00+00:00", wall_ms=2900)
    j1, j2, m1, m2 = _write_report_pair(tmp_path / "inputs", p1, p2)

    explicit_ev = tmp_path / "should_not_exist_ev"
    res = evaluate_row3_runs(
        rc1=1,
        rc2=1,
        run1_json=j1,
        run2_json=j2,
        run1_md=m1,
        run2_md=m2,
        evidence_dir=explicit_ev,
    )
    assert res.passed is True
    assert res.evidence_dir is None
    assert res.diff_lines == []
    assert not explicit_ev.exists()
    assert list(fake_tmp.iterdir()) == []

    # Exit-code mismatch still fails even when JSONs match (verdict criteria unchanged)
    rc_mismatch = evaluate_row3_runs(
        rc1=0,
        rc2=1,
        run1_json=j1,
        run2_json=j2,
        run1_md=m1,
        run2_md=m2,
        evidence_dir=tmp_path / "rc_mismatch_ev",
    )
    assert rc_mismatch.passed is False
    assert rc_mismatch.only_in_target_tests is False
    assert FLAKY_TARGET_TEST_NOTE not in rc_mismatch.summary_message
    assert rc_mismatch.evidence_dir == (tmp_path / "rc_mismatch_ev").resolve()
    assert (rc_mismatch.evidence_dir / "run1.json").is_file()
    assert (rc_mismatch.evidence_dir / "run2.json").is_file()


def test_row3_fail_only_in_target_tests_adds_flaky_hint_still_fails_and_copies_evidence(tmp_path: Path) -> None:
    p1 = _base_verify_payload()
    p2 = _base_verify_payload(wall_ms=3400)
    p2["build_and_test"]["test"]["status"] = "FAIL"
    p2["build_and_test"]["test"]["exit_code"] = 1
    p2["build_and_test"]["test"]["output_summary"] = "1 failed, 1153 passed in <TIME>"

    j1, j2, m1, m2 = _write_report_pair(
        tmp_path / "inputs",
        p1,
        p2,
        md1_text="# MD Run 1\nall passed\n",
        md2_text="# MD Run 2\n1 flaky test failed\n",
    )
    ev_dir = tmp_path / "custom_evidence"
    res = evaluate_row3_runs(
        rc1=1,
        rc2=1,
        run1_json=j1,
        run2_json=j2,
        run1_md=m1,
        run2_md=m2,
        evidence_dir=ev_dir,
    )

    # Verdict MUST remain FAIL (never relaxed)
    assert res.passed is False
    assert res.only_in_target_tests is True
    assert FLAKY_TARGET_TEST_NOTE in res.summary_message
    assert res.evidence_dir is not None
    assert str(res.evidence_dir) in res.summary_message

    # Both JSONs and Markdowns preserved identically
    assert (res.evidence_dir / "run1.json").read_text(encoding="utf-8") == j1.read_text(encoding="utf-8")
    assert (res.evidence_dir / "run2.json").read_text(encoding="utf-8") == j2.read_text(encoding="utf-8")
    assert (res.evidence_dir / "run1.md").read_text(encoding="utf-8") == "# MD Run 1\nall passed\n"
    assert (res.evidence_dir / "run2.md").read_text(encoding="utf-8") == "# MD Run 2\n1 flaky test failed\n"

    diff_paths = [d.path for d in res.displayed_diffs]
    assert diff_paths == [
        "build_and_test.test.status",
        "build_and_test.test.exit_code",
        "build_and_test.test.output_summary",
    ]
    assert len(res.diff_lines) == 3
    joined_diffs = "\n".join(res.diff_lines)
    assert "build_and_test.test.status" in joined_diffs
    assert "1154 passed" in joined_diffs
    assert "1 failed, 1153 passed" in joined_diffs


def test_row3_fail_outside_target_tests_omits_flaky_hint_and_uses_default_tmpdir(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_tmp = tmp_path / "sys_tmpdir"
    fake_tmp.mkdir()
    monkeypatch.setenv("TMPDIR", str(fake_tmp))

    # Case A: Difference in build_and_test.test AND outside it (e.g. failures list)
    p1 = _base_verify_payload()
    p2 = copy.deepcopy(p1)
    p2["build_and_test"]["test"]["status"] = "FAIL"
    p2["failures"].insert(0, "test: pytest failed (exit 1)")
    j1, j2, m1, m2 = _write_report_pair(tmp_path / "case_a", p1, p2)

    res = evaluate_row3_runs(
        rc1=1,
        rc2=1,
        run1_json=j1,
        run2_json=j2,
        run1_md=m1,
        run2_md=m2,
        evidence_dir=None,
    )
    assert res.passed is False
    assert res.only_in_target_tests is False
    assert FLAKY_TARGET_TEST_NOTE not in res.summary_message
    assert res.evidence_dir is not None
    assert res.evidence_dir.parent == fake_tmp.absolute()
    assert res.evidence_dir.name.startswith("skill-arena-row3-")
    assert str(res.evidence_dir) in res.summary_message
    assert (res.evidence_dir / "run1.json").is_file()
    assert (res.evidence_dir / "run2.json").is_file()
    assert (res.evidence_dir / "run1.md").is_file()
    assert (res.evidence_dir / "run2.md").is_file()

    # Case B: First 10 diffs under build_and_test.test, 11th diff in mutation.status -> must check ALL diffs!
    p3 = _base_verify_payload()
    p4 = copy.deepcopy(p3)
    for idx in range(10):
        p3["build_and_test"]["test"][f"extra_{idx:02d}"] = f"v1_{idx}"
        p4["build_and_test"]["test"][f"extra_{idx:02d}"] = f"v2_{idx}"
    p4["mutation"]["status"] = "FAIL"
    j3, j4, m3, m4 = _write_report_pair(tmp_path / "case_b", p3, p4)
    res_b = evaluate_row3_runs(
        rc1=1,
        rc2=1,
        run1_json=j3,
        run2_json=j4,
        run1_md=m3,
        run2_md=m4,
        evidence_dir=tmp_path / "ev_b",
    )
    assert res_b.passed is False
    assert res_b.total_diff_count == 11
    assert len(res_b.displayed_diffs) == 10
    assert res_b.only_in_target_tests is False
    assert FLAKY_TARGET_TEST_NOTE not in res_b.summary_message


def test_row3_fail_caps_diffs_at_10_and_truncates_values_to_160_chars(tmp_path: Path) -> None:
    p1 = _base_verify_payload()
    p2 = copy.deepcopy(p1)

    long_left = "L" * 260
    long_right = "R" * 260
    p1["build_and_test"]["test"]["output_summary"] = long_left
    p2["build_and_test"]["test"]["output_summary"] = long_right

    # Add 13 more differing fields (14 total > 10 limit)
    for idx in range(13):
        p1["readme_replay"][f"field_{idx:02d}"] = f"alpha_{idx}_" + ("x" * 220)
        p2["readme_replay"][f"field_{idx:02d}"] = f"beta_{idx}_" + ("y" * 220)

    j1, j2, m1, m2 = _write_report_pair(tmp_path / "many_diffs", p1, p2)
    res = evaluate_row3_runs(
        rc1=1,
        rc2=1,
        run1_json=j1,
        run2_json=j2,
        run1_md=m1,
        run2_md=m2,
        evidence_dir=tmp_path / "many_ev",
    )
    assert res.passed is False
    assert res.total_diff_count == 14
    assert len(res.displayed_diffs) == 10
    assert len(res.diff_lines) == 10
    for d in res.displayed_diffs:
        assert len(d.run1_value) <= 160
        assert len(d.run2_value) <= 160
    assert res.displayed_diffs[0].path == "build_and_test.test.output_summary"
    assert len(res.displayed_diffs[0].run1_value) == 160
    assert len(res.displayed_diffs[0].run2_value) == 160


def test_m2_acceptance_row3_cli_evidence_dir_on_pass_and_flaky_target_test_fail(tmp_path: Path) -> None:
    root = _repo_root()
    m2_sh = root / "Scripts" / "acceptance" / "m2-acceptance.sh"

    # 1. Missing --evidence-dir value exits 2
    bad_ev = subprocess.run(
        ["bash", str(m2_sh), "--evidence-dir"],
        cwd=str(root),
        capture_output=True,
        text=True,
        check=False,
    )
    assert bad_ev.returncode == 2

    # 2. Deterministic repo -> Row 3 PASS, evidence dir is NOT created
    stable_repo = tmp_path / "stable_repo"
    _init_git_repo(
        stable_repo,
        {
            "calc.py": "def add(a: int, b: int) -> int:\n    return a + b\n",
            "tests/__init__.py": "",
            "tests/test_calc.py": (
                "import unittest\n"
                "from calc import add\n\n"
                "class CalcTests(unittest.TestCase):\n"
                "    def test_add(self) -> None:\n"
                "        self.assertEqual(add(2, 3), 5)\n"
            ),
        },
    )
    pass_ev = tmp_path / "ev_should_stay_absent"
    env_stable = dict(os.environ)
    env_stable["SKILL_ARENA_M2_DIY_HARNESS_REPO"] = str(stable_repo)
    pass_proc = subprocess.run(
        ["bash", str(m2_sh), "--only-row", "3", "--evidence-dir", str(pass_ev)],
        cwd=str(root),
        env=env_stable,
        capture_output=True,
        text=True,
        check=False,
    )
    assert pass_proc.returncode == 0, pass_proc.stdout + pass_proc.stderr
    assert "[Row 3] PASS:" in pass_proc.stdout
    assert not pass_ev.exists()

    # 3. Repo with volatile output only in build_and_test.test -> Row 3 FAIL, saves evidence & prints flaky note
    flaky_repo = tmp_path / "flaky_repo"
    _init_git_repo(
        flaky_repo,
        {
            "calc.py": "def add(a: int, b: int) -> int:\n    return a + b\n",
            "tests/__init__.py": "",
            "tests/test_calc.py": (
                "import sys\n"
                "import unittest\n"
                "import uuid\n"
                "from calc import add\n\n"
                "class CalcTests(unittest.TestCase):\n"
                "    def test_add(self) -> None:\n"
                "        self.assertEqual(add(2, 3), 5)\n"
                "        sys.stderr.write('volatile-test-banner-' + uuid.uuid4().hex + '\\n')\n"
            ),
        },
    )
    fail_ev = tmp_path / "ev_captured_on_fail"
    env_flaky = dict(os.environ)
    env_flaky["SKILL_ARENA_M2_DIY_HARNESS_REPO"] = str(flaky_repo)
    fail_proc = subprocess.run(
        ["bash", str(m2_sh), "--only-row", "3", "--evidence-dir", str(fail_ev)],
        cwd=str(root),
        env=env_flaky,
        capture_output=True,
        text=True,
        check=False,
    )
    assert fail_proc.returncode == 1, fail_proc.stdout + fail_proc.stderr
    assert "[Row 3] FAIL:" in fail_proc.stdout
    assert FLAKY_TARGET_TEST_NOTE in fail_proc.stdout
    assert str(fail_ev.resolve()) in fail_proc.stdout or str(fail_ev.absolute()) in fail_proc.stdout
    assert "build_and_test.test.output_summary" in fail_proc.stdout
    for fname in ("run1.json", "run2.json", "run1.md", "run2.md"):
        assert (fail_ev / fname).is_file(), f"Missing {fname} in {fail_ev}"
