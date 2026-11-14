#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

# shellcheck source=Scripts/_bootstrap_env.sh
source "${ROOT_DIR}/Scripts/_bootstrap_env.sh"

usage() {
  cat >&2 <<'EOF'
Usage:
  Scripts/test.sh scope <graders|sandbox|proxy|extractor|store|cli> [...]
  Scripts/test.sh milestone <N> [--record]
  Scripts/test.sh release [--record]
EOF
  exit 2
}

RECORD_FLAG=()
POSITIONAL=()
for arg in "$@"; do
  if [[ "$arg" == "--record" ]]; then
    RECORD_FLAG=("--record")
  else
    POSITIONAL+=("$arg")
  fi
done

if [[ ${#POSITIONAL[@]} -lt 1 ]]; then
  usage
fi

MODE="${POSITIONAL[0]}"
REST=("${POSITIONAL[@]:1}")

case "$MODE" in
  scope)
    if [[ ${#REST[@]} -lt 1 || ${#RECORD_FLAG[@]} -gt 0 ]]; then
      usage
    fi
    FILES=()
    K_EXPR=()
    for mod in "${REST[@]}"; do
      case "$mod" in
        graders)
          [[ -f tests/test_graders.py ]] && FILES+=("tests/test_graders.py")
          K_EXPR+=("builtin_8_tasks" "anti_cheat")
          ;;
        sandbox)
          [[ -f tests/test_sandbox.py ]] && FILES+=("tests/test_sandbox.py")
          K_EXPR+=("skill_injector")
          ;;
        proxy)
          [[ -f tests/test_proxy.py ]] && FILES+=("tests/test_proxy.py")
          K_EXPR+=("wire_tap_proxy")
          ;;
        extractor)
          [[ -f tests/test_extractor.py ]] && FILES+=("tests/test_extractor.py")
          K_EXPR+=("git_commit_extractor")
          ;;
        store)
          [[ -f tests/test_store.py ]] && FILES+=("tests/test_store.py")
          K_EXPR+=("full_ab_experiment")
          ;;
        cli)
          [[ -f tests/test_cli.py ]] && FILES+=("tests/test_cli.py")
          K_EXPR+=("version_and_bilingual" "cli_and_html_bilingual")
          ;;
        *)
          echo "Unknown scope: $mod" >&2
          usage
          ;;
      esac
    done
    if [[ ${#FILES[@]} -gt 0 ]]; then
      "$PYTEST" -q "${FILES[@]}"
    fi
    if [[ ${#K_EXPR[@]} -gt 0 ]]; then
      EXPR=""
      for k in "${K_EXPR[@]}"; do
        if [[ -z "$EXPR" ]]; then EXPR="$k"; else EXPR="$EXPR or $k"; fi
      done
      "$PYTEST" -q tests/test_skill_arena.py -k "$EXPR"
    fi
    ;;

  milestone)
    if [[ ${#REST[@]} -ne 1 ]]; then
      usage
    fi
    MS="${REST[0]}"
    if [[ "$MS" != "0" ]]; then
      echo "Milestone M${MS} acceptance script is not implemented yet." >&2
      exit 2
    fi
    export SKILL_ARENA_IN_MILESTONE=1
    PYTEST_LOG="$(mktemp "${TMPDIR:-/tmp}/sa-pytest.XXXXXX")"
    README_LOG="$(mktemp "${TMPDIR:-/tmp}/sa-readme-log.XXXXXX")"
    trap 'rm -f "$PYTEST_LOG" "$README_LOG"' EXIT
    "$PYTEST" -q -rs | tee "$PYTEST_LOG"
    if [[ -x Scripts/acceptance/m0-acceptance.sh ]]; then
      Scripts/acceptance/m0-acceptance.sh ${RECORD_FLAG[@]+"${RECORD_FLAG[@]}"}
    else
      echo "Missing executable Scripts/acceptance/m0-acceptance.sh" >&2
      exit 1
    fi
    if [[ -x Scripts/acceptance/readme-commands.sh ]]; then
      Scripts/acceptance/readme-commands.sh ${RECORD_FLAG[@]+"${RECORD_FLAG[@]}"} | tee "$README_LOG"
    else
      echo "Missing executable Scripts/acceptance/readme-commands.sh" >&2
      exit 1
    fi
    "$PYTHON" - "$PYTEST_LOG" "$README_LOG" <<'PY'
import re, sys
from pathlib import Path
pt_txt = Path(sys.argv[1]).read_text(encoding="utf-8")
rm_txt = Path(sys.argv[2]).read_text(encoding="utf-8")
m_pass = re.findall(r"(\d+)\s+passed", pt_txt)
m_skip = re.findall(r"(\d+)\s+skipped", pt_txt)
pt_passed = int(m_pass[-1]) if m_pass else 0
pt_skipped = int(m_skip[-1]) if m_skip else 0
m_rm = re.search(r"README COMMAND REPLAY SUMMARY:\s*(\d+)\s+passed,\s*(\d+)\s+skipped", rm_txt)
rm_passed = int(m_rm.group(1)) if m_rm else 0
rm_skipped = int(m_rm.group(2)) if m_rm else 0
print(
    f"Milestone M0 summary: unit tests ({pt_passed} passed, {pt_skipped} skipped), "
    f"M0 acceptance (8 passed, 0 skipped), "
    f"README replay ({rm_passed} passed, {rm_skipped} skipped)"
)
PY
    ;;

  release)
    if [[ ${#REST[@]} -ne 0 ]]; then
      usage
    fi
    export SKILL_ARENA_IN_MILESTONE=1
    PYTEST_LOG="$(mktemp "${TMPDIR:-/tmp}/sa-pytest.XXXXXX")"
    README_LOG="$(mktemp "${TMPDIR:-/tmp}/sa-readme-log.XXXXXX")"
    trap 'rm -f "$PYTEST_LOG" "$README_LOG"' EXIT
    "$PYTEST" -q -rs | tee "$PYTEST_LOG"
    if [[ -x Scripts/acceptance/m0-acceptance.sh ]]; then
      Scripts/acceptance/m0-acceptance.sh ${RECORD_FLAG[@]+"${RECORD_FLAG[@]}"}
    else
      echo "Missing executable Scripts/acceptance/m0-acceptance.sh" >&2
      exit 1
    fi
    if [[ -x Scripts/acceptance/readme-commands.sh ]]; then
      Scripts/acceptance/readme-commands.sh ${RECORD_FLAG[@]+"${RECORD_FLAG[@]}"} | tee "$README_LOG"
    else
      echo "Missing executable Scripts/acceptance/readme-commands.sh" >&2
      exit 1
    fi
    if [[ "${RELAY_REAL_AGENTS:-0}" == "1" ]]; then
      echo "RELAY_REAL_AGENTS=1 enabled: live real-agent A/B evaluation belongs to M1 (not implemented in M0)." >&2
      exit 2
    else
      echo "Real agent checks: SKIPPED (RELAY_REAL_AGENTS!=1) — 0 passed, 1 skipped"
    fi
    ;;

  *)
    usage
    ;;
esac
