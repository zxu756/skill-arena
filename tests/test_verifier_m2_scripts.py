"""M2 Stage 3 tests for m2-acceptance.sh, Scripts/test.sh milestone 2, and bilingual README verify docs."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess

import pytest

from skill_arena.cli import main


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _extract_bash_commands(md_path: Path) -> list[str]:
    text = md_path.read_text(encoding="utf-8")
    blocks = re.findall(r"```(?:bash|sh)\n(.*?)```", text, flags=re.DOTALL)
    commands: list[str] = []
    for block in blocks:
        buf = ""
        for raw_line in block.splitlines():
            stripped = raw_line.strip()
            if not buf and (not stripped or stripped.startswith("#")):
                continue
            if stripped.endswith("\\"):
                buf += stripped[:-1].rstrip() + " "
            else:
                buf += stripped
                cmd = re.sub(r"\s+", " ", buf).strip()
                if cmd:
                    commands.append(cmd)
                buf = ""
        if buf.strip():
            commands.append(re.sub(r"\s+", " ", buf).strip())
    return commands


def test_bilingual_readmes_document_verifier_and_keep_command_parity(tmp_path: Path) -> None:
    root = _repo_root()
    readme_en = (root / "README.md").read_text(encoding="utf-8")
    readme_zh = (root / "README.zh-CN.md").read_text(encoding="utf-8")

    # Both READMEs must include a dedicated skill-arena verify section, key flags,
    # sandbox-exec offline/HOME rules with ~/.cache/uv exception, exit codes, and diy-harness summary.
    assert "skill-arena verify" in readme_en
    assert "验收器（skill-arena verify）" in readme_zh
    for flag in (
        "--repo",
        "--ref",
        "--build-cmd",
        "--test-cmd",
        "--no-mutate",
        "--max-mutations",
        "--self-report",
        "--skip-cmd",
        "--md-out",
        "--json-out",
    ):
        assert flag in readme_en, f"Missing {flag} in README.md"
        assert flag in readme_zh, f"Missing {flag} in README.zh-CN.md"

    assert "sandbox-exec" in readme_en and ".cache/uv" in readme_en
    assert "sandbox-exec" in readme_zh and ".cache/uv" in readme_zh
    assert "diy-harness" in readme_en and "diy-harness" in readme_zh

    cmds_en = _extract_bash_commands(root / "README.md")
    cmds_zh = _extract_bash_commands(root / "README.zh-CN.md")
    norm_en = [re.sub(r"--lang\s+(?:en|zh)\b", "--lang <LANG>", c) for c in cmds_en]
    norm_zh = [re.sub(r"--lang\s+(?:en|zh)\b", "--lang <LANG>", c) for c in cmds_zh]
    assert norm_en == norm_zh
    assert any("skill-arena verify" in c for c in cmds_en)
    assert "./Scripts/test.sh scope verifier" in cmds_en
    assert "./Scripts/test.sh milestone 2" in cmds_en

    # Bundled sample self-report fixture must have zero real user paths and pass `verify --self-report`
    sample_sr = root / "tests" / "fixtures" / "verify_sample_self_report.md"
    assert sample_sr.is_file()
    sr_text = sample_sr.read_text(encoding="utf-8")
    assert "/Users/" not in sr_text and "/home/" not in sr_text
    out_json = tmp_path / "sr_sample.json"
    rc = main(["verify", "--lang", "zh", "--self-report", str(sample_sr), "--json-out", str(out_json)])
    assert rc == 0
    payload = json.loads(out_json.read_text(encoding="utf-8"))
    assert payload["conclusion"] == "PASS"
    assert payload["self_report"]["summary_counts"]["unsupported"] == 0
    assert payload["self_report"]["summary_counts"]["supported"] >= 1
    assert payload["self_report"]["summary_counts"]["denoised"] >= 1


def test_m2_acceptance_script_missing_targets_exit_2_and_synthetic_rows_pass(tmp_path: Path) -> None:
    root = _repo_root()
    m2_sh = root / "Scripts" / "acceptance" / "m2-acceptance.sh"
    assert m2_sh.is_file() and os.access(m2_sh, os.X_OK), "Missing executable Scripts/acceptance/m2-acceptance.sh"

    # 1. Invalid --mutation argument exits 2
    bad_mut = subprocess.run(
        ["bash", str(m2_sh), "--mutation", "99"],
        cwd=str(root),
        capture_output=True,
        text=True,
        check=False,
    )
    assert bad_mut.returncode == 2

    # 2. Missing diy-harness repo -> Row 2 prints "没跑" and exits 2
    env_no_diy = dict(os.environ)
    env_no_diy["SKILL_ARENA_M2_DIY_HARNESS_REPO"] = str(tmp_path / "missing-diy-harness")
    row2_skip = subprocess.run(
        ["bash", str(m2_sh), "--only-row", "2"],
        cwd=str(root),
        env=env_no_diy,
        capture_output=True,
        text=True,
        check=False,
    )
    assert row2_skip.returncode == 2, row2_skip.stdout + row2_skip.stderr
    assert "没跑" in row2_skip.stdout

    # 3. Missing self-reports dir -> Row 6 prints "没跑" and exits 2
    env_no_sr = dict(os.environ)
    env_no_sr["SKILL_ARENA_M2_SELF_REPORTS_DIR"] = str(tmp_path / "missing-self-reports")
    row6_skip = subprocess.run(
        ["bash", str(m2_sh), "--only-row", "6"],
        cwd=str(root),
        env=env_no_sr,
        capture_output=True,
        text=True,
        check=False,
    )
    assert row6_skip.returncode == 2, row6_skip.stdout + row6_skip.stderr
    assert "没跑" in row6_skip.stdout

    # 4. Row 5 (synthetic `assert True` repo -> mutation survives, verify exits 1 -> Row 5 PASS)
    row5_ok = subprocess.run(
        ["bash", str(m2_sh), "--only-row", "5"],
        cwd=str(root),
        capture_output=True,
        text=True,
        check=False,
    )
    assert row5_ok.returncode == 0, row5_ok.stdout + row5_ok.stderr
    assert "PASS" in row5_ok.stdout

    # 5. Row 7 (exit codes 0, 1, 2 contract -> Row 7 PASS)
    row7_ok = subprocess.run(
        ["bash", str(m2_sh), "--only-row", "7"],
        cwd=str(root),
        capture_output=True,
        text=True,
        check=False,
    )
    assert row7_ok.returncode == 0, row7_ok.stdout + row7_ok.stderr
    assert "PASS" in row7_ok.stdout


def test_test_sh_wires_milestone_2_and_propagates_exit_2_on_skipped_targets() -> None:
    root = _repo_root()
    test_sh = (root / "Scripts" / "test.sh").read_text(encoding="utf-8")
    assert "m2-acceptance.sh" in test_sh
    assert "Milestone M2 summary:" in test_sh
