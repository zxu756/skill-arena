#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT_DIR"

mkdir -p "${ROOT_DIR}/reports"
REPORT_FILE="${ROOT_DIR}/reports/m0-acceptance.txt"

PYTHON="${ROOT_DIR}/.venv/bin/python3"
PYTEST="${ROOT_DIR}/.venv/bin/pytest"
if [[ ! -x "$PYTHON" ]]; then
  PYTHON="python3"
fi
if [[ ! -x "$PYTEST" ]]; then
  PYTEST="pytest"
fi

TMP_WORK="$(mktemp -d "${TMPDIR:-/tmp}/skill-arena-m0-acc.XXXXXX")"
cleanup() {
  rm -rf "$TMP_WORK"
}
trap cleanup EXIT

export HOME="${TMP_WORK}/isolated-home"
mkdir -p "$HOME" "${TMP_WORK}/bin"

run_suite() {
  echo "================================================================"
  echo "SkillArena Milestone M0 Acceptance Suite (Stub Agents Only)"
  echo "Repository: skill-arena (branch: $(git rev-parse --abbrev-ref HEAD))"
  echo "================================================================"

  # ------------------------------------------------------------------
  # Row 1: --cli simulate + SIMULATED markers + 0 matches for demo title + K2/K8/K9/K10
  # ------------------------------------------------------------------
  echo ""
  echo "[Row 1/8] --cli simulate outputs SIMULATED in CLI/DB/HTML & fabricated title absent..."
  SIM_DB="${TMP_WORK}/row1_sim.sqlite"
  SIM_HTML_EN="${TMP_WORK}/row1_sim_en.html"
  SIM_HTML_ZH="${TMP_WORK}/row1_sim_zh.html"

  SIM_OUT_EN="$("$PYTHON" -m skill_arena.cli --lang en run \
    --cli simulate \
    --suite concurrency \
    --reps 1 \
    --db "$SIM_DB" \
    --html-out "$SIM_HTML_EN")"
  echo "$SIM_OUT_EN" | grep -q "SIMULATED"
  echo "$SIM_OUT_EN" | grep -q "Win-Rate Derived Elo (2-arm)"
  echo "$SIM_OUT_EN" | grep -q "Mean Turn-1 input token delta (treatment minus baseline)"

  SIM_OUT_ZH="$("$PYTHON" -m skill_arena.cli --lang zh compare --db "$SIM_DB")"
  echo "$SIM_OUT_ZH" | grep -q "SIMULATED"
  echo "$SIM_OUT_ZH" | grep -q "模拟数据"
  echo "$SIM_OUT_ZH" | grep -q "成对胜率换算的 Elo（双组）"
  echo "$SIM_OUT_ZH" | grep -q "实验组与对照组首轮输入 Token 均值之差"

  "$PYTHON" -m skill_arena.cli --lang zh export-html --db "$SIM_DB" --out "$SIM_HTML_ZH" >/dev/null
  grep -q "SIMULATED" "$SIM_HTML_EN"
  grep -q "SIMULATED" "$SIM_HTML_ZH"
  grep -q "模拟数据" "$SIM_HTML_ZH"

  "$PYTHON" - "$SIM_DB" <<'PY'
import sqlite3, sys
conn = sqlite3.connect(sys.argv[1])
exp_row = conn.execute("SELECT title, is_simulated FROM experiments").fetchone()
assert exp_row is not None and exp_row[1] == 1 and "SIMULATED" in exp_row[0], exp_row
trial_sims = [r[0] for r in conn.execute("SELECT is_simulated FROM trials").fetchall()]
assert trial_sims and all(x == 1 for x in trial_sims), trial_sims
print("  SQLite verified: experiments.is_simulated=1, trials.is_simulated=1")
PY

  # Verify --cli mock and real model names in --cli simulate are rejected
  if "$PYTHON" -m skill_arena.cli --lang en run --cli mock --suite concurrency --reps 1 --db "${TMP_WORK}/bad.sqlite" >/dev/null 2>&1; then
    echo "FAIL: --cli mock unexpectedly succeeded" >&2
    exit 1
  fi
  if "$PYTHON" -m skill_arena.cli --lang en run --cli simulate --model claude-sonnet-4-5 --suite concurrency --reps 1 --db "${TMP_WORK}/bad2.sqlite" >/dev/null 2>&1; then
    echo "FAIL: --cli simulate accepted real model name" >&2
    exit 1
  fi

  # Verify docs/demo-leaderboard.html is gone and fabricated headline has 0 matches in repo
  [[ ! -e "${ROOT_DIR}/docs/demo-leaderboard.html" ]]
  BANNED_PREFIX="Claude Sonnet 4.5"
  BANNED_SUFFIX=": Baseline vs Curated Engineering"
  if rg -q "${BANNED_PREFIX}${BANNED_SUFFIX}" "$ROOT_DIR"; then
    echo "FAIL: Fabricated demo headline still found in repository!" >&2
    exit 1
  fi
  echo "  PASS Row 1: CLI, SQLite DB, and HTML carry SIMULATED; repo search returned 0 matches."

  # ------------------------------------------------------------------
  # Prepare single concurrency task JSON for fast stub-agent rows 2..5
  # ------------------------------------------------------------------
  ONE_TASK_JSON="${TMP_WORK}/task_conc01.json"
  "$PYTHON" - "$ONE_TASK_JSON" <<'PY'
import json, sys
from pathlib import Path
from skill_arena.suites.builtin_tasks import load_builtin_suite
t = load_builtin_suite(category="concurrency")[0]
d = t.to_dict()
d["timeout_sec"] = 2
Path(sys.argv[1]).write_text(json.dumps(d, indent=2), encoding="utf-8")
PY

  # Verify K2 consent gate rejects stub real CLI without --allow-unisolated-host-home
  cat > "${TMP_WORK}/bin/opencode" <<'SH'
#!/bin/sh
exit 0
SH
  chmod +x "${TMP_WORK}/bin/opencode"
  if PATH="${TMP_WORK}/bin:${PATH}" "$PYTHON" -m skill_arena.cli --lang en run --cli opencode --model stub-m --suite "$ONE_TASK_JSON" --reps 1 --db "${TMP_WORK}/gate.sqlite" >/dev/null 2>&1; then
    echo "FAIL: real CLI ran without --allow-unisolated-host-home" >&2
    exit 1
  fi

  # ------------------------------------------------------------------
  # Row 2: Stub writes wrong answer + adds tests/__init__.py (os._exit(0)) -> FAIL
  # ------------------------------------------------------------------
  echo ""
  echo "[Row 2/8] Stub writes wrong answer + tests/__init__.py (os._exit(0))..."
  cat > "${TMP_WORK}/bin/opencode" <<'PY'
#!/usr/bin/env python3
from pathlib import Path
Path("rate_limiter.py").write_text("class TokenBucket:\n    pass\n", encoding="utf-8")
Path("tests/__init__.py").write_text("import os\nos._exit(0)\n", encoding="utf-8")
PY
  chmod +x "${TMP_WORK}/bin/opencode"
  ROW2_DB="${TMP_WORK}/row2.sqlite"
  ROW2_OUT="$(PATH="${TMP_WORK}/bin:${PATH}" "$PYTHON" -m skill_arena.cli --lang en run \
    --cli opencode --model stub-m --allow-unisolated-host-home \
    --suite "$ONE_TASK_JSON" --reps 1 --db "$ROW2_DB")"
  echo "$ROW2_OUT" | grep -q "Unmetered"
  "$PYTHON" - "$ROW2_DB" <<'PY'
import json, sqlite3, sys
conn = sqlite3.connect(sys.argv[1])
rows = conn.execute("SELECT passed, anti_cheat_clean, verdict_json FROM trials").fetchall()
assert len(rows) == 2, rows
for passed, clean, vjson in rows:
    v = json.loads(vjson)
    assert passed == 0, f"Expected FAIL, got passed={passed}: {v}"
    assert clean == 0, f"Expected anti-cheat violation: {v}"
    assert "tests/__init__.py" in v["tampered_test_files"], v
print("  PASS Row 2: Both arms marked FAILED & anti-cheat flagged tests/__init__.py.")
PY

  # ------------------------------------------------------------------
  # Row 3: Stub writes wrong answer + adds root unittest.py -> FAIL
  # ------------------------------------------------------------------
  echo ""
  echo "[Row 3/8] Stub writes wrong answer + root unittest.py shadowing..."
  cat > "${TMP_WORK}/bin/opencode" <<'PY'
#!/usr/bin/env python3
from pathlib import Path
Path("rate_limiter.py").write_text("class TokenBucket:\n    pass\n", encoding="utf-8")
Path("unittest.py").write_text("import sys\nsys.exit(0)\n", encoding="utf-8")
PY
  chmod +x "${TMP_WORK}/bin/opencode"
  ROW3_DB="${TMP_WORK}/row3.sqlite"
  PATH="${TMP_WORK}/bin:${PATH}" "$PYTHON" -m skill_arena.cli --lang en run \
    --cli opencode --model stub-m --allow-unisolated-host-home \
    --suite "$ONE_TASK_JSON" --reps 1 --db "$ROW3_DB" >/dev/null
  "$PYTHON" - "$ROW3_DB" <<'PY'
import json, sqlite3, sys
conn = sqlite3.connect(sys.argv[1])
rows = conn.execute("SELECT passed, anti_cheat_clean, verdict_json FROM trials").fetchall()
assert len(rows) == 2, rows
for passed, clean, vjson in rows:
    v = json.loads(vjson)
    assert passed == 0, f"Expected FAIL, got passed={passed}: {v}"
    assert clean == 0, f"Expected anti-cheat violation: {v}"
    assert "unittest.py" in v["tampered_test_files"], v
print("  PASS Row 3: Both arms marked FAILED & anti-cheat blocked root unittest.py.")
PY

  # ------------------------------------------------------------------
  # Row 4: Stub writes exact passing answer, then sleeps past timeout -> FAIL (reason: timeout)
  # ------------------------------------------------------------------
  echo ""
  echo "[Row 4/8] Stub writes reference passing answer then sleeps past timeout..."
  REF_SOLUTION_FILE="${TMP_WORK}/ref_rate_limiter.py"
  "$PYTHON" - "$REF_SOLUTION_FILE" <<'PY'
import sys
from pathlib import Path
from skill_arena.suites.builtin_tasks import load_builtin_suite
t = load_builtin_suite(category="concurrency")[0]
Path(sys.argv[1]).write_text(t.reference_solution_files["rate_limiter.py"], encoding="utf-8")
PY
  cat > "${TMP_WORK}/bin/opencode" <<'PY'
#!/usr/bin/env python3
import os, shutil, time
shutil.copyfile(os.environ["SA_REF_SOLUTION_FILE"], "rate_limiter.py")
time.sleep(15)
PY
  chmod +x "${TMP_WORK}/bin/opencode"
  ROW4_DB="${TMP_WORK}/row4.sqlite"
  SA_REF_SOLUTION_FILE="$REF_SOLUTION_FILE" PATH="${TMP_WORK}/bin:${PATH}" "$PYTHON" -m skill_arena.cli --lang en run \
    --cli opencode --model stub-m --allow-unisolated-host-home \
    --suite "$ONE_TASK_JSON" --reps 1 --db "$ROW4_DB" >/dev/null
  "$PYTHON" - "$ROW4_DB" <<'PY'
import json, sqlite3, sys
conn = sqlite3.connect(sys.argv[1])
rows = conn.execute("SELECT passed, verdict_json FROM trials").fetchall()
assert len(rows) == 2, rows
for passed, vjson in rows:
    v = json.loads(vjson)
    assert passed == 0, f"Timed-out trial must FAIL even with valid fix: {v}"
    assert "timeout" in v["summary"].lower(), f"Expected timeout failure reason: {v}"
print("  PASS Row 4: Timed-out trials marked FAILED with explicit timeout reason.")
PY

  # ------------------------------------------------------------------
  # Row 5: Stub spawns detached setsid background process & exits -> does not hang, orphan reaped
  # ------------------------------------------------------------------
  echo ""
  echo "[Row 5/8] Stub spawns detached setsid background sleeper and exits..."
  ORPHAN_PID_LOG="${TMP_WORK}/orphan_pids.txt"
  cat > "${TMP_WORK}/bin/opencode" <<'PY'
#!/usr/bin/env python3
import os, subprocess
from pathlib import Path
p = subprocess.Popen(["sleep", "120"], start_new_session=True)
with Path(os.environ["SA_ORPHAN_PID_LOG"]).open("a", encoding="utf-8") as fh:
    fh.write(f"{p.pid}\n")
PY
  chmod +x "${TMP_WORK}/bin/opencode"
  ROW5_DB="${TMP_WORK}/row5.sqlite"
  T_START="$("$PYTHON" -c 'import time; print(time.monotonic())')"
  SA_ORPHAN_PID_LOG="$ORPHAN_PID_LOG" PATH="${TMP_WORK}/bin:${PATH}" "$PYTHON" -m skill_arena.cli --lang en run \
    --cli opencode --model stub-m --allow-unisolated-host-home \
    --suite "$ONE_TASK_JSON" --reps 1 --db "$ROW5_DB" >/dev/null
  "$PYTHON" - "$T_START" "$ORPHAN_PID_LOG" <<'PY'
import os, sys, time
elapsed = time.monotonic() - float(sys.argv[1])
assert elapsed < 10.0, f"Trial stalled waiting on detached child ({elapsed:.2f}s)!"
pids = [int(x) for x in open(sys.argv[2], encoding="utf-8").read().splitlines() if x.strip()]
assert len(pids) == 2, pids
alive = []
for pid in pids:
    try:
        os.kill(pid, 0)
        alive.append(pid)
    except ProcessLookupError:
        pass
assert not alive, f"Detached PIDs survived: {alive}"
print(f"  PASS Row 5: Completed in {elapsed:.2f}s (<10s) and reaped all detached setsid PIDs.")
PY

  # ------------------------------------------------------------------
  # Row 6: extract-commit strips commit message & excludes fix commit from task .git history
  # ------------------------------------------------------------------
  echo ""
  echo "[Row 6/8] extract-commit omits commit message & isolates task workspace git history..."
  MINI_REPO="${TMP_WORK}/mini_bug_repo"
  mkdir -p "${MINI_REPO}/tests"
  git -C "$MINI_REPO" init -b main >/dev/null 2>&1 || git -C "$MINI_REPO" init >/dev/null 2>&1
  git -C "$MINI_REPO" config user.name "Tester"
  git -C "$MINI_REPO" config user.email "tester@example.com"
  cat > "${MINI_REPO}/calc.py" <<'PY'
def clamp(x: int, lo: int, hi: int) -> int:
    return max(lo, x)
PY
  cat > "${MINI_REPO}/tests/test_calc.py" <<'PY'
import unittest
from calc import clamp

class ClampTests(unittest.TestCase):
    def test_low(self) -> None:
        self.assertEqual(clamp(-3, 0, 10), 0)
PY
  git -C "$MINI_REPO" add -A
  git -C "$MINI_REPO" commit -m "initial buggy clamp" >/dev/null
  cat > "${MINI_REPO}/calc.py" <<'PY'
def clamp(x: int, lo: int, hi: int) -> int:
    return min(hi, max(lo, x))
PY
  cat > "${MINI_REPO}/tests/test_calc.py" <<'PY'
import unittest
from calc import clamp

class ClampTests(unittest.TestCase):
    def test_low(self) -> None:
        self.assertEqual(clamp(-3, 0, 10), 0)

    def test_high(self) -> None:
        self.assertEqual(clamp(25, 0, 10), 10)
PY
  git -C "$MINI_REPO" add -A
  git -C "$MINI_REPO" commit -m "fix(calc): SECRET_SUBJECT_CLAMP_HINT" -m "SECRET_BODY_USE_MIN_HI_MAX_LO" >/dev/null
  FIX_SHA="$(git -C "$MINI_REPO" rev-parse HEAD)"

  EXTRACTED_UNIT="${TMP_WORK}/extracted_unit.json"
  "$PYTHON" -m skill_arena.cli --lang en extract-commit --repo "$MINI_REPO" --commit HEAD --out "$EXTRACTED_UNIT" >/dev/null
  "$PYTHON" - "$EXTRACTED_UNIT" "$FIX_SHA" <<'PY'
import json, subprocess, sys
from pathlib import Path
from skill_arena.models import TaskSpec
from skill_arena.sandbox.worktree_pool import WorktreeSandboxPool

raw = Path(sys.argv[1]).read_text(encoding="utf-8")
fix_sha = sys.argv[2]
assert "SECRET_SUBJECT_CLAMP_HINT" not in raw, raw
assert "SECRET_BODY_USE_MIN_HI_MAX_LO" not in raw, raw

spec = TaskSpec.from_dict(json.loads(raw))
pool = WorktreeSandboxPool()
handle = pool.acquire(spec, "row6_check")
try:
    log_out = subprocess.run(
        ["git", "log", "--all", "--format=%H %B"],
        cwd=handle.root,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert fix_sha not in log_out, log_out
    assert "SECRET_SUBJECT_CLAMP_HINT" not in log_out, log_out
    assert "SECRET_BODY_USE_MIN_HI_MAX_LO" not in log_out, log_out
finally:
    pool.release(handle)
    pool.close()
print("  PASS Row 6: Prompt contains zero commit message lines; workspace git log --all hides fix commit.")
PY

  # ------------------------------------------------------------------
  # Row 7: Extract from pytest-style repo + run no-op stub -> FAIL (not 0 tests = PASS)
  # ------------------------------------------------------------------
  echo ""
  echo "[Row 7/8] extract-commit on pytest-style repo + no-op stub agent -> FAIL..."
  PYTEST_REPO="${TMP_WORK}/pytest_bug_repo"
  mkdir -p "${PYTEST_REPO}/tests"
  git -C "$PYTEST_REPO" init -b main >/dev/null 2>&1 || git -C "$PYTEST_REPO" init >/dev/null 2>&1
  git -C "$PYTEST_REPO" config user.name "Tester"
  git -C "$PYTEST_REPO" config user.email "tester@example.com"
  cat > "${PYTEST_REPO}/pyproject.toml" <<'TOML'
[tool.pytest.ini_options]
testpaths = ["tests"]
TOML
  cat > "${PYTEST_REPO}/m.py" <<'PY'
def double(x: int) -> int:
    return x + 1
PY
  cat > "${PYTEST_REPO}/tests/test_m.py" <<'PY'
from m import double

def test_one():
    assert double(1) == 2
PY
  git -C "$PYTEST_REPO" add -A
  git -C "$PYTEST_REPO" commit -m "initial" >/dev/null
  cat > "${PYTEST_REPO}/m.py" <<'PY'
def double(x: int) -> int:
    return x * 2
PY
  cat > "${PYTEST_REPO}/tests/test_m.py" <<'PY'
from m import double

def test_one():
    assert double(1) == 2

def test_five():
    assert double(5) == 10
PY
  git -C "$PYTEST_REPO" add -A
  git -C "$PYTEST_REPO" commit -m "fix double for all ints" >/dev/null

  EXTRACTED_PYTEST="${TMP_WORK}/extracted_pytest.json"
  "$PYTHON" -m skill_arena.cli --lang en extract-commit --repo "$PYTEST_REPO" --commit HEAD --out "$EXTRACTED_PYTEST" >/dev/null
  cat > "${TMP_WORK}/bin/opencode" <<'SH'
#!/bin/sh
# No-op stub agent
exit 0
SH
  chmod +x "${TMP_WORK}/bin/opencode"
  ROW7_DB="${TMP_WORK}/row7.sqlite"
  PATH="${TMP_WORK}/bin:${PATH}" "$PYTHON" -m skill_arena.cli --lang en run \
    --cli opencode --model stub-m --allow-unisolated-host-home \
    --suite "$EXTRACTED_PYTEST" --reps 1 --db "$ROW7_DB" >/dev/null
  "$PYTHON" - "$EXTRACTED_PYTEST" "$ROW7_DB" <<'PY'
import json, sqlite3, sys
from pathlib import Path
spec = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
assert "pytest" in spec["test_command"], spec["test_command"]
conn = sqlite3.connect(sys.argv[2])
rows = conn.execute("SELECT passed, verdict_json FROM trials").fetchall()
assert len(rows) == 2, rows
for passed, vjson in rows:
    assert passed == 0, f"No-op stub on pytest task must FAIL, got passed={passed}: {vjson}"
print(f"  PASS Row 7: Auto-detected '{spec['test_command']}' and marked no-op stub trials FAILED.")
PY

  # ------------------------------------------------------------------
  # Row 8: 3 Live Mutation Checks (isolated copy loaded before editable MetaPathFinder)
  # ------------------------------------------------------------------
  echo ""
  echo "[Row 8/8] Running 3 live mutation checks (timeout kill, Pass^k=1, test restoration)..."

  run_pytest_with_mutant() {
    local mut_dir="$1"
    shift
    "$PYTHON" -c '
import sys
mut_root = sys.argv.pop(1)
sys.meta_path = [
    finder for finder in sys.meta_path
    if "editable" not in type(finder).__name__.lower()
    and "editable" not in type(finder).__module__.lower()
]
sys.path.insert(0, mut_root)
import skill_arena
assert skill_arena.__file__ and skill_arena.__file__.startswith(mut_root), skill_arena.__file__
import pytest
raise SystemExit(pytest.main(sys.argv[1:]))
' "$mut_dir" "$@"
  }

  # Mutation 8a: remove timeout process-group kill
  MUT1="${TMP_WORK}/mut1"
  mkdir -p "$MUT1"
  cp -R "${ROOT_DIR}/src/skill_arena" "${MUT1}/skill_arena"
  "$PYTHON" - "${MUT1}/skill_arena/sandbox/worktree_pool.py" <<'PY'
from pathlib import Path
import sys
p = Path(sys.argv[1])
src = p.read_text(encoding="utf-8")
target = """            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except OSError:
                pass
            try:
                proc.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                _kill_pid_and_group(proc.pid, signal.SIGKILL)"""
assert target in src
p.write_text(src.replace(target, "            pass", 1), encoding="utf-8")
PY
  if run_pytest_with_mutant "$MUT1" -q "${ROOT_DIR}/tests/test_sandbox.py" -k "kills_timed_out_leader" >/dev/null 2>&1; then
    echo "FAIL: Mutation 8a (remove timeout kill) did NOT turn test RED!" >&2
    exit 1
  fi
  echo "  Mutation 8a (remove timeout kill) -> RED confirmed."

  # Mutation 8b: force Pass^k = 1.0
  MUT2="${TMP_WORK}/mut2"
  mkdir -p "$MUT2"
  cp -R "${ROOT_DIR}/src/skill_arena" "${MUT2}/skill_arena"
  "$PYTHON" - "${MUT2}/skill_arena/store/arena_db.py" <<'PY'
from pathlib import Path
import sys
p = Path(sys.argv[1])
src = p.read_text(encoding="utf-8")
target = """            pass_hat_k = (
                sum(1.0 if all(r.passed for r in t_list) else 0.0 for t_list in by_task.values())
                / max(1, tasks_total)
            )"""
assert target in src
p.write_text(src.replace(target, "            pass_hat_k = 1.0", 1), encoding="utf-8")
PY
  if run_pytest_with_mutant "$MUT2" -q "${ROOT_DIR}/tests/test_store.py" -k "pass_hat_k" >/dev/null 2>&1; then
    echo "FAIL: Mutation 8b (Pass^k = 1.0) did NOT turn test RED!" >&2
    exit 1
  fi
  echo "  Mutation 8b (force Pass^k = 1.0) -> RED confirmed."

  # Mutation 8c: disable restoring tampered test files
  MUT3="${TMP_WORK}/mut3"
  mkdir -p "$MUT3"
  cp -R "${ROOT_DIR}/src/skill_arena" "${MUT3}/skill_arena"
  "$PYTHON" - "${MUT3}/skill_arena/sandbox/worktree_pool.py" <<'PY'
from pathlib import Path
import sys
p = Path(sys.argv[1])
src = p.read_text(encoding="utf-8")
t1 = "                    full_path.write_bytes(backup)"
t2 = "                    target.write_bytes(base_bytes)"
assert t1 in src and t2 in src
p.write_text(src.replace(t1, "                    pass", 1).replace(t2, "                    pass", 1), encoding="utf-8")
PY
  if run_pytest_with_mutant "$MUT3" -q "${ROOT_DIR}/tests/test_graders.py" -k "protected_test_restoration" >/dev/null 2>&1; then
    echo "FAIL: Mutation 8c (skip restoring tampered tests) did NOT turn test RED!" >&2
    exit 1
  fi
  echo "  Mutation 8c (skip restoring tampered tests) -> RED confirmed."
  echo "  PASS Row 8: All 3 mutations turned scoped tests RED."

  echo ""
  echo "================================================================"
  echo "RESULT: 8/8 M0 ACCEPTANCE CHECKS PASSED"
  echo "================================================================"
}

run_suite 2>&1 | tee "$REPORT_FILE"
