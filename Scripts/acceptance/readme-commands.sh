#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT_DIR"

mkdir -p "${ROOT_DIR}/reports"
REPORT_FILE="${ROOT_DIR}/reports/readme-commands.txt"

PYTHON="${ROOT_DIR}/.venv/bin/python3"
if [[ ! -x "$PYTHON" ]]; then
  PYTHON="python3"
fi

TMP_README="$(mktemp -d "${TMPDIR:-/tmp}/skill-arena-readme.XXXXXX")"
cleanup() {
  rm -rf "$TMP_README"
}
trap cleanup EXIT

export HOME="${TMP_README}/isolated-home"
mkdir -p "$HOME"

replay_readme() {
  echo "================================================================"
  echo "SkillArena README.md & README.zh-CN.md Offline Command Replay"
  echo "================================================================"

  echo ""
  echo "[1/5] list-suites (EN & ZH)..."
  OUT_EN="$("$PYTHON" -m skill_arena.cli --lang en list-suites)"
  echo "$OUT_EN" | grep -q "conc-01-token-bucket-race"
  OUT_ZH="$("$PYTHON" -m skill_arena.cli --lang zh list-suites)"
  echo "$OUT_ZH" | grep -q "并发控制"
  echo "  OK: list-suites EN & ZH verified."

  echo ""
  echo "[2/5] Quick Start --cli simulate + compare + export-html (EN)..."
  DB_EN="${TMP_README}/sim-check-en.sqlite"
  HTML_EN="${TMP_README}/sim-check-en.html"
  RUN_EN="$("$PYTHON" -m skill_arena.cli --lang en run \
    --cli simulate \
    --suite concurrency \
    --reps 1 \
    --db "$DB_EN" \
    --html-out "$HTML_EN")"
  echo "$RUN_EN" | grep -q "SIMULATED"
  CMP_EN="$("$PYTHON" -m skill_arena.cli --lang en compare --db "$DB_EN")"
  echo "$CMP_EN" | grep -q "SIMULATED"
  echo "$CMP_EN" | grep -q "Win-Rate Derived Elo (2-arm)"
  grep -q "SIMULATED" "$HTML_EN"
  echo "  OK: EN Quick Start simulate + compare + HTML verified."

  echo ""
  echo "[3/5] Quick Start --cli simulate + compare + export-html (ZH)..."
  DB_ZH="${TMP_README}/sim-check-zh.sqlite"
  HTML_ZH="${TMP_README}/sim-check-zh.html"
  RUN_ZH="$("$PYTHON" -m skill_arena.cli --lang zh run \
    --cli simulate \
    --suite concurrency \
    --reps 1 \
    --db "$DB_ZH" \
    --html-out "$HTML_ZH")"
  echo "$RUN_ZH" | grep -q "SIMULATED"
  echo "$RUN_ZH" | grep -q "模拟数据"
  CMP_ZH="$("$PYTHON" -m skill_arena.cli --lang zh compare --db "$DB_ZH")"
  echo "$CMP_ZH" | grep -q "成对胜率换算的 Elo（双组）"
  grep -q "模拟数据" "$HTML_ZH"
  echo "  OK: ZH Quick Start simulate + compare + HTML verified."

  echo ""
  echo "[4/5] Real CLI consent gate check (without invoking any real agent)..."
  if "$PYTHON" -m skill_arena.cli --lang en run --cli claude --suite concurrency --reps 1 --db "${TMP_README}/blocked.sqlite" >/dev/null 2>&1; then
    echo "FAIL: claude run succeeded without --allow-unisolated-host-home" >&2
    exit 1
  fi
  echo "  OK: Real CLI blocked cleanly unless --allow-unisolated-host-home is supplied."

  echo ""
  echo "[5/5] extract-commit command check on sample repository..."
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
  OUT_TASK="${TMP_README}/my-regression.json"
  "$PYTHON" -m skill_arena.cli --lang en extract-commit \
    --repo "$SAMPLE_REPO" \
    --commit HEAD \
    --out "$OUT_TASK" >/dev/null
  grep -q "pytest" "$OUT_TASK"
  echo "  OK: extract-commit generated TaskSpec JSON cleanly."

  echo ""
  echo "================================================================"
  echo "RESULT: ALL README COMMAND REPLAY CHECKS PASSED"
  echo "================================================================"
}

replay_readme 2>&1 | tee "$REPORT_FILE"
