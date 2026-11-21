#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT_DIR"

# shellcheck source=Scripts/_bootstrap_env.sh
source "${ROOT_DIR}/Scripts/_bootstrap_env.sh"

RECORD=0
for arg in "$@"; do
  if [[ "$arg" == "--record" ]]; then
    RECORD=1
  else
    echo "Unknown argument: $arg" >&2
    exit 2
  fi
done

REPORT_FILE="${ROOT_DIR}/reports/readme-commands.txt"

TMP_README="$(mktemp -d "${TMPDIR:-/tmp}/skill-arena-readme.XXXXXX")"
cleanup() {
  rm -rf "$TMP_README"
}
trap cleanup EXIT

export HOME="${TMP_README}/isolated-home"
SHIM_BIN="${TMP_README}/shim_bin"
mkdir -p "$HOME" "$SHIM_BIN"

# Offline `uv run` shim and `skill-arena` console wrapper so extracted README commands execute verbatim without network
cat > "${SHIM_BIN}/uv" <<'SH'
#!/bin/sh
if [ "${1:-}" = "run" ]; then
  shift
  exec "$@"
fi
echo "Offline README replay only allows 'uv run ...', got: uv $*" >&2
exit 2
SH
chmod +x "${SHIM_BIN}/uv"

cat > "${SHIM_BIN}/skill-arena" <<SH
#!/bin/sh
exec "${PYTHON}" -m skill_arena.cli "\$@"
SH
chmod +x "${SHIM_BIN}/skill-arena"

export PATH="${SHIM_BIN}:${PATH}"

replay_readme() {
  echo "================================================================"
  echo "SkillArena README.md & README.zh-CN.md Parsed Command Replay (W3)"
  echo "================================================================"

  # Create a local sample git repository with a 2-commit bug fix for `extract-commit`
  SAMPLE_REPO="${TMP_README}/sample_repo"
  mkdir -p "${SAMPLE_REPO}/tests"
  git -C "$SAMPLE_REPO" init -b main >/dev/null 2>&1 || git -C "$SAMPLE_REPO" init >/dev/null 2>&1
  git -C "$SAMPLE_REPO" config user.name "ReadmeCheck"
  git -C "$SAMPLE_REPO" config user.email "readme@example.com"
  printf "def add(a, b):\n    return a - b\n" > "${SAMPLE_REPO}/adder.py"
  printf "from adder import add\n\ndef test_zero():\n    assert add(2, 0) == 2\n" > "${SAMPLE_REPO}/tests/test_adder.py"
  git -C "$SAMPLE_REPO" add -A
  git -C "$SAMPLE_REPO" commit -m "seed" >/dev/null
  printf "def add(a, b):\n    return a + b\n" > "${SAMPLE_REPO}/adder.py"
  printf "from adder import add\n\ndef test_zero():\n    assert add(2, 0) == 2\n\ndef test_pos():\n    assert add(2, 3) == 5\n" > "${SAMPLE_REPO}/tests/test_adder.py"
  git -C "$SAMPLE_REPO" add -A
  git -C "$SAMPLE_REPO" commit -m "fix add implementation" >/dev/null

  "$PYTHON" - "$ROOT_DIR" "$TMP_README" "$SAMPLE_REPO" <<'PY'
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import threading
import urllib.request

from skill_arena.store.arena_db import ArenaDatabase
from skill_arena.studio.server import create_studio_server

repo_root = Path(sys.argv[1]).resolve()
tmp_root = Path(sys.argv[2]).resolve()
sample_repo = Path(sys.argv[3]).resolve()


def extract_bash_commands(md_path: Path) -> list[str]:
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


def normalize_for_parity(cmd: str) -> str:
    return re.sub(r"--lang\s+(?:en|zh)\b", "--lang <LANG>", cmd)


cmds_en = extract_bash_commands(repo_root / "README.md")
cmds_zh = extract_bash_commands(repo_root / "README.zh-CN.md")

assert cmds_en, "No bash commands extracted from README.md!"
assert cmds_zh, "No bash commands extracted from README.zh-CN.md!"

norm_en = [normalize_for_parity(c) for c in cmds_en]
norm_zh = [normalize_for_parity(c) for c in cmds_zh]
if norm_en != norm_zh:
    raise AssertionError(
        "Command parity drift between README.md and README.zh-CN.md!\n"
        f"  README.md ({len(norm_en)}): {norm_en}\n"
        f"  README.zh-CN.md ({len(norm_zh)}): {norm_zh}"
    )

print(f"\n[Parity] Verified README.md and README.zh-CN.md list the same {len(cmds_en)} CLI commands.")


def run_cmd(cmd: str, cwd: Path) -> tuple[int, str]:
    proc = subprocess.run(
        ["bash", "-euo", "pipefail", "-c", cmd],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=False,
    )
    combined = ((proc.stdout or "") + ("\n" if proc.stdout and proc.stderr else "") + (proc.stderr or "")).strip()
    return proc.returncode, combined


executed_scopes: set[str] = set()
passed_count = 0
skipped_count = 0

for doc_name, cmds, work_sub in (
    ("README.md", cmds_en, "work_en"),
    ("README.zh-CN.md", cmds_zh, "work_zh"),
):
    print(f"\n--- Replaying {doc_name} ({len(cmds)} commands) ---")
    workdir = tmp_root / work_sub
    workdir.mkdir(parents=True, exist_ok=True)
    src_fixtures = repo_root / "tests" / "fixtures"
    if src_fixtures.is_dir():
        import shutil
        dst_fixtures = workdir / "tests" / "fixtures"
        shutil.rmtree(dst_fixtures, ignore_errors=True)
        dst_fixtures.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(src_fixtures, dst_fixtures)

    verify_sample_repo = tmp_root / f"verify_sample_{work_sub}"
    if not verify_sample_repo.exists():
        (verify_sample_repo / "tests").mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "-C", str(verify_sample_repo), "init", "-b", "main"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(verify_sample_repo), "config", "user.name", "ReadmeVerify"], check=True)
        subprocess.run(["git", "-C", str(verify_sample_repo), "config", "user.email", "readme@example.invalid"], check=True)
        (verify_sample_repo / "pytest.ini").write_text("[pytest]\ntestpaths = tests\n", encoding="utf-8")
        (verify_sample_repo / "adder.py").write_text("def add(a: int, b: int) -> int:\n    return a + b\n", encoding="utf-8")
        (verify_sample_repo / "tests" / "test_adder.py").write_text(
            "from adder import add\n\ndef test_add() -> None:\n    assert add(2, 3) == 5\n",
            encoding="utf-8",
        )
        (verify_sample_repo / "README.md").write_text(
            "# Sample Verify Repo\n\n```bash\npython3 -c \"import adder; assert adder.add(1, 2) == 3\"\n```\n",
            encoding="utf-8",
        )
        subprocess.run(["git", "-C", str(verify_sample_repo), "add", "-A"], check=True)
        subprocess.run(["git", "-C", str(verify_sample_repo), "commit", "-q", "-m", "init"], check=True)

    for idx, cmd in enumerate(cmds, start=1):
        prefix = f"[{doc_name} #{idx}/{len(cmds)}]"

        if "list-suites" in cmd:
            rc, out = run_cmd(cmd, workdir)
            assert rc == 0, f"{prefix} exited {rc}: {out}"
            assert "conc-01-token-bucket-race" in out, out
            if "--lang zh" in cmd:
                assert "并发控制" in out, out
            else:
                assert "[concurrency" in out, out
            passed_count += 1
            print(f"  RUN OK {prefix}: {cmd} -> exit 0, verified suite listing")

        elif "skill-arena run" in cmd and "--cli simulate" in cmd:
            rc, out = run_cmd(cmd, workdir)
            assert rc == 0, f"{prefix} exited {rc}: {out}"
            assert "SIMULATED" in out, out
            if "--lang zh" in cmd:
                assert "模拟数据" in out, out
            db_file = workdir / ".skill-arena" / "sim-check.sqlite"
            html_file = workdir / ".skill-arena" / "sim-check.html"
            assert db_file.is_file(), f"Missing {db_file}"
            assert html_file.is_file(), f"Missing {html_file}"
            assert "SIMULATED" in html_file.read_text(encoding="utf-8")
            with sqlite3.connect(str(db_file)) as conn:
                exp_sim = conn.execute("SELECT is_simulated FROM experiments").fetchone()
                assert exp_sim == (1,), exp_sim
            passed_count += 1
            print(f"  RUN OK {prefix}: {cmd} -> exit 0, verified SIMULATED CLI/SQLite/HTML")

        elif "skill-arena compare" in cmd:
            rc, out = run_cmd(cmd, workdir)
            assert rc == 0, f"{prefix} exited {rc}: {out}"
            assert "SIMULATED" in out, out
            if "--lang zh" in cmd:
                assert "成对胜率换算的 Elo（双组）" in out, out
            else:
                assert "Win-Rate Derived Elo (2-arm)" in out, out
            passed_count += 1
            print(f"  RUN OK {prefix}: {cmd} -> exit 0, verified leaderboard output")

        elif "skill-arena export-html" in cmd:
            rc, out = run_cmd(cmd, workdir)
            assert rc == 0, f"{prefix} exited {rc}: {out}"
            exported = workdir / ".skill-arena" / "sim-export.html"
            assert exported.is_file(), f"Missing {exported}"
            html_txt = exported.read_text(encoding="utf-8")
            assert "SIMULATED" in html_txt
            passed_count += 1
            print(f"  RUN OK {prefix}: {cmd} -> exit 0, verified exported HTML")

        elif "skill-arena pool-export" in cmd:
            sample_pool = tmp_root / "sample_pool"
            task_dir = sample_pool / "calc-01-basics"
            for rel, text in {
                "task.json": json.dumps(
                    {
                        "task_id": "calc-01-basics",
                        "title": "Integer helpers",
                        "category": "parse",
                        "prompt": "Implement calc.py.\n\n1. add(a, b) returns a + b.\n",
                        "test_command": "python3 -m unittest tests/test_calc.py",
                        "impl_paths": ["calc.py"],
                    }
                ),
                "start/calc.py": "def add(a, b):\n    raise NotImplementedError\n",
                "tests/test_calc.py": "import unittest\nfrom calc import add\n\nclass T(unittest.TestCase):\n    def test_add(self):\n        self.assertEqual(add(2, 3), 5)\n",
                "reference/calc.py": "def add(a, b):\n    return a + b\n",
                "mutants/minus/calc.py": "def add(a, b):\n    return a - b\n",
            }.items():
                dest = task_dir / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_text(text, encoding="utf-8")
            real_cmd = cmd.replace("/path/to/pool", str(sample_pool))
            rc, out = run_cmd(real_cmd, workdir)
            assert rc == 0, f"{prefix} exited {rc}: {out}"
            suite_json = workdir / "suites" / "pool-ab.json"
            assert suite_json.is_file(), f"Missing {suite_json}"
            specs = json.loads(suite_json.read_text(encoding="utf-8"))
            assert sorted(s["task_id"] for s in specs) == ["calc-01-basics@hidden", "calc-01-basics@visible"], specs
            assert all(s["timeout_sec"] == 900 and len(s["mutant_files"]) == 1 for s in specs), specs
            passed_count += 1
            print(f"  RUN OK {prefix}: {cmd} -> exit 0, verified visible and hidden TaskSpecs")

        elif "skill-arena run" in cmd and "--cli vibe" in cmd and "--arm" in cmd:
            skipped_count += 1
            print(
                f"  SKIPPED: {cmd} — invokes a real paid agent CLI (vibe) with placeholder skill paths; "
                "prohibited in offline acceptance"
            )
            # The placeholder --arm paths must be rejected before any agent starts (exit 2, nothing spent).
            rc_gate, out_gate = run_cmd(cmd, workdir)
            assert rc_gate == 2 and "not found" in out_gate, (rc_gate, out_gate)

        elif "skill-arena run" in cmd and "--cli claude" in cmd:
            skipped_count += 1
            print(
                f"  SKIPPED: {cmd} — invokes real paid external agent CLI (claude) with placeholder "
                "/path/to/candidate-skill; prohibited in offline acceptance"
            )
            # Also verify consent gate blocks running in host HOME (--host-home) when --allow-unisolated-host-home is omitted
            gated_cmd = cmd.replace("/path/to/candidate-skill", str(workdir)) + " --host-home"
            rc_gate, out_gate = run_cmd(gated_cmd, workdir)
            assert rc_gate != 0 and "--allow-unisolated-host-home" in out_gate, (rc_gate, out_gate)

        elif "skill-arena studio" in cmd:
            skipped_count += 1
            print(
                f"  SKIPPED: {cmd} — long-running blocking HTTP daemon on fixed port 8765 "
                "(probed on ephemeral port against product-generated sim-check.sqlite)"
            )
            sim_db = workdir / ".skill-arena" / "sim-check.sqlite"
            srv = create_studio_server(
                ArenaDatabase(sim_db),
                host="127.0.0.1",
                port=0,
                default_lang="zh" if "--lang zh" in cmd else "en",
            )
            th = threading.Thread(target=srv.serve_forever, daemon=True)
            th.start()
            try:
                port = srv.server_address[1]
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=5) as resp:
                    body = resp.read().decode("utf-8")
                    assert resp.status == 200
                    assert "SIMULATED" in body
            finally:
                srv.shutdown()
                srv.server_close()

        elif "skill-arena extract-commit" in cmd:
            real_cmd = cmd.replace("/path/to/your/repo", str(sample_repo))
            rc, out = run_cmd(real_cmd, workdir)
            assert rc == 0, f"{prefix} exited {rc}: {out}"
            out_json = workdir / "tasks" / "my-regression.json"
            assert out_json.is_file(), f"Missing {out_json}"
            payload = json.loads(out_json.read_text(encoding="utf-8"))
            assert payload["category"] == "git_extracted", payload
            assert "pytest" in payload["test_command"], payload
            passed_count += 1
            print(f"  RUN OK {prefix}: {cmd} -> exit 0, verified extracted TaskSpec JSON")

        elif "skill-arena verify" in cmd and "--repo" in cmd:
            real_cmd = cmd.replace("/path/to/your/repo", str(verify_sample_repo))
            rc, out = run_cmd(real_cmd, workdir)
            assert rc == 0, f"{prefix} exited {rc}: {out}"
            out_md = workdir / ".skill-arena" / "verify-report.md"
            out_json = workdir / ".skill-arena" / "verify-report.json"
            assert out_md.is_file(), f"Missing {out_md}"
            assert out_json.is_file(), f"Missing {out_json}"
            payload = json.loads(out_json.read_text(encoding="utf-8"))
            assert payload["conclusion"] == "PASS", payload
            assert payload["mutation"]["status"] == "PASS" and payload["mutation"]["killed_count"] >= 1, payload
            passed_count += 1
            print(f"  RUN OK {prefix}: {cmd} -> exit 0, verified isolated clone + README replay + mutation KILLED")

        elif "skill-arena verify" in cmd and "--self-report" in cmd:
            rc, out = run_cmd(cmd, workdir)
            assert rc == 0, f"{prefix} exited {rc}: {out}"
            out_json = workdir / ".skill-arena" / "self-report-audit.json"
            assert out_json.is_file(), f"Missing {out_json}"
            payload = json.loads(out_json.read_text(encoding="utf-8"))
            assert payload["conclusion"] == "PASS", payload
            assert payload["self_report"]["summary_counts"]["unsupported"] == 0, payload
            passed_count += 1
            print(f"  RUN OK {prefix}: {cmd} -> exit 0, verified verbatim self-report wording audit")

        elif cmd.startswith("./Scripts/test.sh scope "):
            if cmd in executed_scopes:
                skipped_count += 1
                print(
                    f"  SKIPPED: {cmd} — byte-identical scoped test command already executed and verified in README.md pass"
                )
            else:
                rc, out = run_cmd(cmd, repo_root)
                assert rc == 0, f"{prefix} exited {rc}: {out}"
                assert "passed" in out, out
                executed_scopes.add(cmd)
                passed_count += 1
                print(f"  RUN OK {prefix}: {cmd} -> exit 0, verified pytest passed")

        elif cmd in (
            "./Scripts/test.sh milestone 0",
            "./Scripts/test.sh milestone 1",
            "./Scripts/test.sh milestone 2",
        ):
            skipped_count += 1
            print(
                f"  SKIPPED: {cmd} — top-level milestone orchestrator that invokes readme-commands.sh itself "
                "(skipped to prevent recursive self-invocation)"
            )

        else:
            raise AssertionError(f"Unhandled command in {doc_name}: {cmd}")

total_count = passed_count + skipped_count
print("\n================================================================")
print(
    f"RESULT: README COMMAND REPLAY SUMMARY: {passed_count} passed, "
    f"{skipped_count} skipped, 0 failed (out of {total_count} parsed commands)"
)
print("================================================================")
PY
}

if [[ "$RECORD" -eq 1 ]]; then
  mkdir -p "${ROOT_DIR}/reports"
  RAW_OUT="${TMP_README}/raw_readme.txt"
  replay_readme 2>&1 | tee "$RAW_OUT"
  scrub_report_file "$RAW_OUT" "$REPORT_FILE"
else
  replay_readme
fi
