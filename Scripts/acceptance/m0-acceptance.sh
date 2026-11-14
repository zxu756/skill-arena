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

REPORT_FILE="${ROOT_DIR}/reports/m0-acceptance.txt"

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
  echo "Workspace: <REPO_ROOT> (branch: $(git rev-parse --abbrev-ref HEAD))"
  echo "================================================================"

  # ------------------------------------------------------------------
  # Row 1: --cli simulate + SIMULATED markers + K12 no fabricated screenshots + K2/K8/K9/K10
  # ------------------------------------------------------------------
  echo ""
  echo "[Row 1/8] --cli simulate outputs SIMULATED in CLI/DB/HTML, fabricated title & screenshots absent..."
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

  if "$PYTHON" -m skill_arena.cli --lang en run --cli mock --suite concurrency --reps 1 --db "${TMP_WORK}/bad.sqlite" >/dev/null 2>&1; then
    echo "FAIL: --cli mock unexpectedly succeeded" >&2
    exit 1
  fi
  if "$PYTHON" -m skill_arena.cli --lang en run --cli simulate --model claude-sonnet-4-5 --suite concurrency --reps 1 --db "${TMP_WORK}/bad2.sqlite" >/dev/null 2>&1; then
    echo "FAIL: --cli simulate accepted real model name" >&2
    exit 1
  fi

  # K12: Verify docs/demo-leaderboard.html and docs/screenshot-*.png are gone and READMEs do not reference them
  [[ ! -e "${ROOT_DIR}/docs/demo-leaderboard.html" ]]
  if compgen -G "${ROOT_DIR}/docs/screenshot-*.png" >/dev/null; then
    echo "FAIL: Fabricated screenshot PNGs still exist in docs/!" >&2
    exit 1
  fi
  if grep -q "docs/screenshot-" "${ROOT_DIR}/README.md" "${ROOT_DIR}/README.zh-CN.md"; then
    echo "FAIL: README still references docs/screenshot-*!" >&2
    exit 1
  fi

  BANNED_PREFIX="Claude Sonnet 4.5"
  BANNED_SUFFIX=": Baseline vs Curated Engineering"
  if command -v rg >/dev/null 2>&1; then
    if rg -q "${BANNED_PREFIX}${BANNED_SUFFIX}" "$ROOT_DIR"; then
      echo "FAIL: Fabricated demo headline still found in repository!" >&2
      exit 1
    fi
  else
    if grep -rn --exclude-dir=.git --exclude-dir=.venv "${BANNED_PREFIX}${BANNED_SUFFIX}" "$ROOT_DIR" >/dev/null 2>&1; then
      echo "FAIL: Fabricated demo headline still found in repository!" >&2
      exit 1
    fi
  fi
  echo "  PASS Row 1: CLI, SQLite DB, and HTML carry SIMULATED; fabricated screenshots & headline absent."

  # ------------------------------------------------------------------
  # Prepare single concurrency task JSON for stub-agent rows 2..5
  # ------------------------------------------------------------------
  ONE_TASK_JSON="${TMP_WORK}/task_conc01.json"
  "$PYTHON" - "$ONE_TASK_JSON" <<'PY'
import json, sys
from pathlib import Path
from skill_arena.suites.builtin_tasks import load_builtin_suite
t = load_builtin_suite(category="concurrency")[0]
d = t.to_dict()
d["timeout_sec"] = 3
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
  cat > "${TMP_WORK}/bin/opencode" <<PY
#!${PYTHON}
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
  cat > "${TMP_WORK}/bin/opencode" <<PY
#!${PYTHON}
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
  cat > "${TMP_WORK}/bin/opencode" <<PY
#!${PYTHON}
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
  # Row 5: Detached background sleeper & K13 double-fork + setsid + close-fds + chdir("/")
  # ------------------------------------------------------------------
  echo ""
  echo "[Row 5/8] Detached setsid & double-fork background daemons (K13)..."
  ORPHAN_PID_LOG="${TMP_WORK}/orphan_pids.txt"
  cat > "${TMP_WORK}/bin/opencode" <<PY
#!${PYTHON}
import os, shutil, sys
from pathlib import Path
shutil.copyfile(os.environ["SA_REF_SOLUTION_FILE"], "rate_limiter.py")
pid1 = os.fork()
if pid1 == 0:
    os.setsid()
    pid2 = os.fork()
    if pid2 == 0:
        os.chdir("/")
        for fd in range(3, 256):
            try:
                os.close(fd)
            except OSError:
                pass
        dn = os.open(os.devnull, os.O_RDWR)
        os.dup2(dn, 0); os.dup2(dn, 1); os.dup2(dn, 2)
        if dn > 2:
            os.close(dn)
        with Path(os.environ["SA_ORPHAN_PID_LOG"]).open("a", encoding="utf-8") as fh:
            fh.write(f"{os.getpid()}\n")
        os.execv(sys.executable, [sys.executable, "-c", "import time; time.sleep(120)"])
    os._exit(0)
os.waitpid(pid1, 0)
PY
  chmod +x "${TMP_WORK}/bin/opencode"
  ROW5_DB="${TMP_WORK}/row5.sqlite"
  T_START="$("$PYTHON" -c 'import time; print(time.monotonic())')"
  SA_REF_SOLUTION_FILE="$REF_SOLUTION_FILE" SA_ORPHAN_PID_LOG="$ORPHAN_PID_LOG" PATH="${TMP_WORK}/bin:${PATH}" \
    "$PYTHON" -m skill_arena.cli --lang en run \
    --cli opencode --model stub-m --allow-unisolated-host-home \
    --suite "$ONE_TASK_JSON" --reps 1 --db "$ROW5_DB" >/dev/null
  "$PYTHON" - "$T_START" "$ORPHAN_PID_LOG" "$ROW5_DB" <<'PY'
import json, os, signal, sqlite3, sys, time
elapsed = time.monotonic() - float(sys.argv[1])
pids = [int(x) for x in open(sys.argv[2], encoding="utf-8").read().splitlines() if x.strip()]
try:
    assert elapsed < 10.0, f"Trial stalled waiting on detached child ({elapsed:.2f}s)!"
    assert len(pids) == 2, pids
    alive = []
    for pid in pids:
        try:
            os.kill(pid, 0)
            alive.append(pid)
        except ProcessLookupError:
            pass
    assert not alive, f"Detached non-SIP PIDs survived: {alive}"
    conn = sqlite3.connect(sys.argv[3])
    rows = conn.execute("SELECT passed, verdict_json FROM trials").fetchall()
    assert len(rows) == 2, rows
    for (passed, vjson), expected_pid in zip(rows, pids):
        v = json.loads(vjson)
        assert passed == 0, f"Trial with leaked daemon must FAIL even with valid fix: {v}"
        leaked_pids = {int(item["pid"]) for item in v.get("leaked_processes", [])}
        assert expected_pid in leaked_pids, f"Expected leaked PID {expected_pid} in {v}"
        assert "leaked" in v["summary"].lower(), v
finally:
    for pid in pids:
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass
print("  PASS Row 5: Double-fork + setsid + close-fd + chdir('/') non-SIP daemons killed, recorded in leaked_processes, and trials marked FAILED (<10s).")
PY

  # Also verify README explicitly documents both macOS SIP /bin/sleep and env-scrubbing (execve) double-fork non-guarantees
  grep -q "/bin/sleep" "${ROOT_DIR}/README.md"
  grep -q "execve" "${ROOT_DIR}/README.md"
  grep -q "does not guarantee" "${ROOT_DIR}/README.md"
  grep -q "/bin/sleep" "${ROOT_DIR}/README.zh-CN.md"
  grep -q "execve" "${ROOT_DIR}/README.zh-CN.md"
  grep -q "不保证" "${ROOT_DIR}/README.zh-CN.md"
  echo "  Verified README.md & README.zh-CN.md explicitly document macOS SIP /bin/sleep and env-scrubbing (execve) double-fork non-guarantees."

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
  # Row 7: Extract from pytest-style repo + no-op stub -> FAIL + missing-pytest preflight check
  # ------------------------------------------------------------------
  echo ""
  echo "[Row 7/8] extract-commit on pytest-style repo + no-op stub -> FAIL & pytest preflight check..."
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

  # Verify missing-pytest preflight exits non-zero immediately
  NO_PYTEST_BIN="${TMP_WORK}/no_pytest_bin"
  mkdir -p "$NO_PYTEST_BIN"
  cat > "${NO_PYTEST_BIN}/python3" <<'SH'
#!/bin/sh
echo "No module named pytest" >&2
exit 1
SH
  chmod +x "${NO_PYTEST_BIN}/python3"
  ln -sf "${TMP_WORK}/bin/opencode" "${NO_PYTEST_BIN}/opencode"
  if PATH="${NO_PYTEST_BIN}:/usr/bin:/bin" "$PYTHON" -m skill_arena.cli --lang en run \
    --cli opencode --model stub-m --allow-unisolated-host-home \
    --suite "$EXTRACTED_PYTEST" --reps 1 --db "${TMP_WORK}/no_pytest.sqlite" >/dev/null 2>&1; then
    echo "FAIL: missing pytest in grading python3 did not fail preflight!" >&2
    exit 1
  fi
  echo "  Verified preflight check fails immediately with non-zero exit when pytest is missing."

  # ------------------------------------------------------------------
  # Row 8 (W1): Scripted Live Mutations on Isolated Clone (assert rc==1 & exact FAILED nodeid & clean restore)
  # ------------------------------------------------------------------
  echo ""
  echo "[Row 8/8] Running scripted mutation checks on isolated repository clone (W1)..."

  MUT_REPO="${TMP_WORK}/mut_repo"
  git clone -q "$ROOT_DIR" "$MUT_REPO"
  "$PYTHON" - "$ROOT_DIR" "$MUT_REPO" <<'PY'
import os, shutil, subprocess, sys
from pathlib import Path

root = Path(sys.argv[1])
mut = Path(sys.argv[2])
for sub in ("src", "tests", "Scripts"):
    shutil.rmtree(mut / sub, ignore_errors=True)
    shutil.copytree(
        root / sub,
        mut / sub,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".pytest_cache"),
    )
for fname in ("README.md", "README.zh-CN.md", "pyproject.toml"):
    if (root / fname).is_file():
        shutil.copy2(root / fname, mut / fname)
subprocess.run(["git", "-C", str(mut), "config", "user.name", "MutCheck"], check=True)
subprocess.run(["git", "-C", str(mut), "config", "user.email", "mut@example.com"], check=True)
subprocess.run(["git", "-C", str(mut), "add", "-A"], check=True)
subprocess.run(["git", "-C", str(mut), "commit", "--allow-empty", "-q", "-m", "working-tree-sync"], check=True)
PY

  run_pytest_in_mut_repo() {
    local test_rel="$1"
    local test_func="$2"
    "$PYTHON" - "$MUT_REPO" "${test_rel}::${test_func}" <<'PY'
import os, sys
mut_repo = sys.argv[1]
nodeid = sys.argv[2]
mut_src = os.path.join(mut_repo, "src")
sys.meta_path = [
    finder for finder in sys.meta_path
    if "editable" not in type(finder).__name__.lower()
    and "editable" not in type(finder).__module__.lower()
]
sys.path.insert(0, mut_src)
os.chdir(mut_repo)
import skill_arena
assert skill_arena.__file__ and os.path.realpath(skill_arena.__file__).startswith(os.path.realpath(mut_src)), skill_arena.__file__
import pytest
raise SystemExit(pytest.main(["-v", "--tb=short", nodeid]))
PY
  }

  verify_named_mutation() {
    local label="$1"
    local target_rel="$2"
    local test_rel="$3"
    local test_func="$4"
    local patch_code="$5"
    local nodeid="${test_rel}::${test_func}"

    "$PYTHON" - "${MUT_REPO}/${target_rel}" "$patch_code" <<'PY'
from pathlib import Path
import py_compile, sys
target_file = Path(sys.argv[1])
code = sys.argv[2]
ns = {"target_file": target_file, "src": target_file.read_text(encoding="utf-8")}
exec(code, ns)
py_compile.compile(str(target_file), doraise=True)
PY

    set +e
    MUT_OUT="$(run_pytest_in_mut_repo "$test_rel" "$test_func" 2>&1)"
    MUT_RC=$?
    set -e

    if [[ "$MUT_RC" -ne 1 ]]; then
      echo "FAIL: ${label} exited with code ${MUT_RC} (expected pytest failure exit code 1, not pass/syntax/collection error)!" >&2
      echo "$MUT_OUT" >&2
      exit 1
    fi
    if ! echo "$MUT_OUT" | grep -q "FAILED ${nodeid}"; then
      echo "FAIL: ${label} did not fail named test ${nodeid}!" >&2
      echo "$MUT_OUT" >&2
      exit 1
    fi

    git -C "$MUT_REPO" checkout -- .
    git -C "$MUT_REPO" clean -fd >/dev/null
    if [[ -n "$(git -C "$MUT_REPO" status --porcelain)" ]]; then
      echo "FAIL: ${label} left dirty git status in mutation clone after restore!" >&2
      exit 1
    fi

    if ! run_pytest_in_mut_repo "$test_rel" "$test_func" >/dev/null 2>&1; then
      echo "FAIL: ${label} named test ${nodeid} did not pass after restoring clean source!" >&2
      exit 1
    fi
    echo "  ${label} -> FAILED ${nodeid} (exit 1) & restored clean (git status empty, test GREEN)."
  }

  verify_named_mutation \
    "Mutation 8a (delete timeout process-group kill)" \
    "src/skill_arena/sandbox/worktree_pool.py" \
    "tests/test_sandbox.py" \
    "test_run_bounded_command_kills_timed_out_leader_and_process_group" \
    '
needle = """            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except OSError:
                pass
            try:
                proc.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                _kill_pid_and_group(proc.pid, signal.SIGKILL)"""
assert needle in src, "Mutation 8a target not found"
target_file.write_text(src.replace(needle, "            pass", 1), encoding="utf-8")
'

  verify_named_mutation \
    "Mutation 8b (force Pass^k = 1.0)" \
    "src/skill_arena/store/arena_db.py" \
    "tests/test_store.py" \
    "test_pass_hat_k_exact_values_reject_constant_one_mutation" \
    '
needle = """            pass_hat_k = (
                sum(1.0 if all(r.passed for r in t_list) else 0.0 for t_list in by_task.values())
                / max(1, tasks_total)
            )"""
assert needle in src, "Mutation 8b target not found"
target_file.write_text(src.replace(needle, "            pass_hat_k = 1.0", 1), encoding="utf-8")
'

  verify_named_mutation \
    "Mutation 8c (skip restoring tampered test files)" \
    "src/skill_arena/sandbox/worktree_pool.py" \
    "tests/test_graders.py" \
    "test_protected_test_restoration_restores_original_bytes_and_cleans_diff" \
    '
t1 = "                    full_path.write_bytes(backup)"
t2 = "                    target.write_bytes(base_bytes)"
assert t1 in src and t2 in src, "Mutation 8c target not found"
target_file.write_text(src.replace(t1, "                    pass", 1).replace(t2, "                    pass", 1), encoding="utf-8")
'

  verify_named_mutation \
    "Mutation 8d (ignore agent timeout during grading)" \
    "src/skill_arena/runners/harness_driver.py" \
    "tests/test_sandbox.py" \
    "test_agent_timeout_fails_even_when_solution_written_and_kills_sleeper" \
    '
needle = "agent_timed_out=bool(exec_outcome and exec_outcome.timed_out),"
assert needle in src, "Mutation 8d target not found"
target_file.write_text(src.replace(needle, "agent_timed_out=False,", 1), encoding="utf-8")
'

  verify_named_mutation \
    "Mutation 8e (disable SKILL_ARENA_TRIAL_TOKEN scan)" \
    "src/skill_arena/sandbox/worktree_pool.py" \
    "tests/test_sandbox.py" \
    "test_detached_double_fork_non_sip_daemon_is_killed_and_fails_trial" \
    '
needle = "candidates |= _find_pids_with_env_token(trial_token)"
assert needle in src, "Mutation 8e target not found"
target_file.write_text(src.replace(needle, "pass", 1), encoding="utf-8")
'

  verify_named_mutation \
    "Mutation 8f (disable sandbox open-file +D scan)" \
    "src/skill_arena/sandbox/worktree_pool.py" \
    "tests/test_sandbox.py" \
    "test_detached_env_scrubbed_daemon_using_sandbox_dir_is_killed_and_fails_trial" \
    '
needle = "if os.path.isdir(real_target):"
assert needle in src, "Mutation 8f target not found"
target_file.write_text(src.replace(needle, "if False:", 1), encoding="utf-8")
'

  echo "  PASS Row 8: All 6 mutations failed their exact named tests (exit 1) and restored cleanly."

  echo ""
  echo "================================================================"
  echo "RESULT: 8/8 M0 ACCEPTANCE CHECKS PASSED (8 passed, 0 skipped, 0 failed)"
  echo "================================================================"
}

if [[ "$RECORD" -eq 1 ]]; then
  mkdir -p "${ROOT_DIR}/reports"
  RAW_OUT="${TMP_WORK}/raw_m0_acceptance.txt"
  run_suite 2>&1 | tee "$RAW_OUT"
  scrub_report_file "$RAW_OUT" "$REPORT_FILE"
else
  run_suite
fi
