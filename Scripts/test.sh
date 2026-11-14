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
    "$PYTEST" -q
    if [[ -x Scripts/acceptance/m0-acceptance.sh ]]; then
      Scripts/acceptance/m0-acceptance.sh ${RECORD_FLAG[@]+"${RECORD_FLAG[@]}"}
    else
      echo "Missing executable Scripts/acceptance/m0-acceptance.sh" >&2
      exit 1
    fi
    if [[ -x Scripts/acceptance/readme-commands.sh ]]; then
      Scripts/acceptance/readme-commands.sh ${RECORD_FLAG[@]+"${RECORD_FLAG[@]}"}
    else
      echo "Missing executable Scripts/acceptance/readme-commands.sh" >&2
      exit 1
    fi
    ;;

  release)
    if [[ ${#REST[@]} -ne 0 ]]; then
      usage
    fi
    export SKILL_ARENA_IN_MILESTONE=1
    "$PYTEST" -q
    if [[ -x Scripts/acceptance/m0-acceptance.sh ]]; then
      Scripts/acceptance/m0-acceptance.sh ${RECORD_FLAG[@]+"${RECORD_FLAG[@]}"}
    else
      echo "Missing executable Scripts/acceptance/m0-acceptance.sh" >&2
      exit 1
    fi
    if [[ -x Scripts/acceptance/readme-commands.sh ]]; then
      Scripts/acceptance/readme-commands.sh ${RECORD_FLAG[@]+"${RECORD_FLAG[@]}"}
    else
      echo "Missing executable Scripts/acceptance/readme-commands.sh" >&2
      exit 1
    fi
    if [[ "${RELAY_REAL_AGENTS:-0}" == "1" ]]; then
      echo "RELAY_REAL_AGENTS=1 enabled: live real-agent A/B evaluation belongs to M1 (not implemented in M0)." >&2
      exit 2
    else
      echo "Real agent checks: SKIPPED (RELAY_REAL_AGENTS!=1)"
    fi
    ;;

  *)
    usage
    ;;
esac
