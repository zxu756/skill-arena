#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT_DIR"

# shellcheck source=Scripts/_bootstrap_env.sh
source "${ROOT_DIR}/Scripts/_bootstrap_env.sh"

usage() {
  cat >&2 <<'EOF'
Usage:
  Scripts/acceptance/m2-acceptance.sh [--record] [--mutation <1|2|3|4|all>] [--all-rows] [--only-row <1|2|3|4|5|6|7|9>] [--evidence-dir <dir>]
EOF
  exit 2
}

RECORD=0
MUTATION_ARG=""
ALL_ROWS=0
ONLY_ROW=""
EVIDENCE_DIR="${SKILL_ARENA_M2_EVIDENCE_DIR:-}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --record)
      RECORD=1
      shift
      ;;
    --all-rows)
      ALL_ROWS=1
      shift
      ;;
    --mutation)
      [[ $# -ge 2 ]] || usage
      MUTATION_ARG="$2"
      shift 2
      ;;
    --mutation=*)
      MUTATION_ARG="${1#*=}"
      shift
      ;;
    --only-row)
      [[ $# -ge 2 ]] || usage
      ONLY_ROW="$2"
      shift 2
      ;;
    --only-row=*)
      ONLY_ROW="${1#*=}"
      shift
      ;;
    --evidence-dir)
      [[ $# -ge 2 && -n "$2" ]] || usage
      EVIDENCE_DIR="$2"
      shift 2
      ;;
    --evidence-dir=*)
      EVIDENCE_DIR="${1#*=}"
      [[ -n "$EVIDENCE_DIR" ]] || usage
      shift
      ;;
    -h|--help)
      usage
      ;;
    *)
      echo "Unknown argument: $1" >&2
      usage
      ;;
  esac
done

if [[ -n "$MUTATION_ARG" ]]; then
  case "$MUTATION_ARG" in
    1|2|3|4|all) ;;
    *)
      echo "Invalid --mutation value: $MUTATION_ARG (expected 1|2|3|4|all)" >&2
      usage
      ;;
  esac
fi

if [[ -n "$ONLY_ROW" ]]; then
  case "$ONLY_ROW" in
    1|2|3|4|5|6|7|9) ;;
    *)
      echo "Invalid --only-row value: $ONLY_ROW (expected 1|2|3|4|5|6|7|9)" >&2
      usage
      ;;
  esac
fi

if [[ -n "$MUTATION_ARG" && -n "$ONLY_ROW" ]]; then
  echo "Cannot combine --mutation and --only-row" >&2
  usage
fi

REPORT_FILE="${ROOT_DIR}/reports/m2-acceptance.txt"
TMP_WORK="$(mktemp -d "${TMPDIR:-/tmp}/skill-arena-m2-acc.XXXXXX")"
cleanup() {
  rm -rf "$TMP_WORK"
}
trap cleanup EXIT

run_m2_driver() {
  "$PYTHON" - "$ROOT_DIR" "$TMP_WORK" "$MUTATION_ARG" "$ALL_ROWS" "$ONLY_ROW" "$EVIDENCE_DIR" <<'PY'
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import pwd
import py_compile
import re
import shutil
import subprocess
import sys
import threading
import time
from typing import Any

root_dir = Path(sys.argv[1]).resolve()
tmp_work = Path(sys.argv[2]).resolve()
mutation_arg = sys.argv[3].strip()
all_rows_flag = sys.argv[4].strip() == "1"
only_row_arg = sys.argv[5].strip()
evidence_dir_arg = sys.argv[6].strip() if len(sys.argv) > 6 else ""
explicit_evidence_dir = Path(evidence_dir_arg).expanduser() if evidence_dir_arg else None

target_root = Path(os.environ.get("SKILL_ARENA_M2_TARGET_ROOT") or str(root_dir)).resolve()
target_src = (target_root / "src").resolve()

# Strip editable-install meta_path finders so `skill_arena` always loads from `target_src`
sys.meta_path = [
    finder
    for finder in sys.meta_path
    if "editable" not in type(finder).__name__.lower()
    and "editable" not in type(finder).__module__.lower()
]
root_src_real = str((root_dir / "src").resolve())
if str(target_src) != root_src_real:
    sys.path = [p for p in sys.path if not (p and os.path.realpath(p) == root_src_real)]
sys.path.insert(0, str(target_src))

import skill_arena
assert skill_arena.__file__ is not None and os.path.realpath(skill_arena.__file__).startswith(
    os.path.realpath(str(target_src))
), f"Loaded wrong skill_arena package: {skill_arena.__file__} vs {target_src}"

from skill_arena.cli import main as cli_main
from skill_arena.verifier.env_guard import repo_fingerprint, snapshot_uv_cache_entries
from skill_arena.verifier.row3_diagnostics import evaluate_row3_runs

pw_home = Path(pwd.getpwuid(os.getuid()).pw_dir)


def _git_origin_chain_parents(repo: Path, max_hops: int = 5) -> list[Path]:
    parents: list[Path] = [repo.parent]
    cur = repo
    seen = {str(cur)}
    for _ in range(max_hops):
        proc = subprocess.run(
            ["git", "-C", str(cur), "--no-optional-locks", "config", "--get", "remote.origin.url"],
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode != 0:
            break
        raw = proc.stdout.strip()
        if not raw or "://" in raw or raw.startswith("git@"):
            break
        cand = Path(raw).expanduser()
        if not cand.is_absolute():
            cand = (cur / cand).resolve()
        else:
            cand = cand.resolve()
        if str(cand) in seen or not cand.exists():
            break
        seen.add(str(cand))
        parents.append(cand.parent)
        cur = cand
    return parents


def _discover_diy_harness_repo() -> Path:
    override = os.environ.get("SKILL_ARENA_M2_DIY_HARNESS_REPO", "").strip()
    if override:
        return Path(override).expanduser()
    for parent in _git_origin_chain_parents(root_dir):
        cand = parent / "diy-harness"
        if (cand / ".git").exists():
            return cand.resolve()
    return pw_home / "Projects" / "diy-harness"


def _discover_self_reports_dir() -> Path:
    override = os.environ.get("SKILL_ARENA_M2_SELF_REPORTS_DIR", "").strip()
    if override:
        return Path(override).expanduser()
    rel_sub = Path("flagship-acceptance") / "reports" / "archive" / "tools-2026-11" / "self-reports"
    for parent in _git_origin_chain_parents(root_dir):
        cand = parent / rel_sub
        if cand.is_dir():
            return cand.resolve()
    return pw_home / "Projects" / rel_sub


def _is_git_repo(p: Path) -> bool:
    return p.is_dir() and (p / ".git").exists()


def _init_git_repo(repo: Path, files: dict[str, str]) -> str:
    repo.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "-C", str(repo), "init", "-b", "main"],
        capture_output=True,
        text=True,
        check=True,
    )
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
            "user.name=VerifierTest",
            "-c",
            "user.email=verifier@example.invalid",
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
            "user.name=VerifierTest",
            "-c",
            "user.email=verifier@example.invalid",
            "-c",
            "commit.gpgsign=false",
            "commit",
            "-q",
            "-m",
            "seed",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    sha = subprocess.run(
        ["git", "-C", str(repo), "--no-optional-locks", "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    return sha


def _list_sa_verify_tmp_dirs() -> set[str]:
    found: set[str] = set()
    for base in ("/tmp", "/private/tmp"):
        bp = Path(base)
        if not bp.is_dir():
            continue
        try:
            for entry in os.scandir(bp):
                if entry.name.startswith("sa-verify-"):
                    found.add(os.path.realpath(entry.path))
        except OSError:
            pass
    return found


def _scan_leaked_verify_processes() -> list[str]:
    proc = subprocess.run(
        ["/bin/ps", "-axo", "pid=,command="],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        return []
    hits: list[str] = []
    my_pid = os.getpid()
    for line in proc.stdout.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        parts = stripped.split(None, 1)
        if len(parts) < 2:
            continue
        try:
            pid = int(parts[0])
        except ValueError:
            continue
        if pid == my_pid:
            continue
        cmd = parts[1]
        if "/sa-verify-" in cmd or "sa_verify_leak_probe" in cmd:
            hits.append(f"{pid}:{cmd[:120]}")
    return hits


def _snapshot_real_home_two_levels() -> set[str]:
    """Snapshot 1st- and 2nd-level entries of real HOME (plus 2nd-level inside ~/Library), excluding ~/.cache/uv."""
    entries: set[str] = set()
    home_real = pw_home.resolve()
    if not home_real.is_dir():
        return entries

    def _safe_scandir(p: Path) -> list[os.DirEntry[str]]:
        try:
            with os.scandir(p) as it:
                return list(it)
        except OSError:
            return []

    for d1 in _safe_scandir(home_real):
        rel1 = d1.name
        entries.add(rel1)
        try:
            is_dir1 = d1.is_dir(follow_symlinks=False)
        except OSError:
            is_dir1 = False
        if not is_dir1:
            continue
        for d2 in _safe_scandir(Path(d1.path)):
            rel2 = f"{rel1}/{d2.name}"
            if rel2 == ".cache/uv" or rel2.startswith(".cache/uv/"):
                continue
            entries.add(rel2)
            if rel1 == "Library":
                try:
                    is_dir2 = d2.is_dir(follow_symlinks=False)
                except OSError:
                    is_dir2 = False
                if is_dir2:
                    for d3 in _safe_scandir(Path(d2.path)):
                        entries.add(f"{rel2}/{d3.name}")
    return entries


def _classify_home_diff(added: list[str]) -> tuple[list[str], list[str]]:
    """Split new real-HOME entries into (forbidden_verifier_related, unrelated_host_with_source)."""
    forbidden_tokens = ("skill-arena", "skill_arena", "diy-harness", "diy_harness", "sa-verify-")
    forbidden: list[str] = []
    unrelated: list[str] = []
    for rel in sorted(added):
        low = rel.lower()
        if any(tok in low for tok in forbidden_tokens):
            forbidden.append(rel)
        else:
            if rel.startswith(".gemini"):
                src = "宿主机当前 agy 会话状态"
            elif rel.startswith("Library/"):
                src = "macOS 系统或桌面后台进程"
            elif rel.startswith(".cache/"):
                src = "其他工具缓存目录"
            else:
                src = "同机并发进程"
            unrelated.append(f"{rel} ({src})")
    return forbidden, unrelated


def _run_cli_in_subproc(argv: list[str]) -> tuple[int, str, str]:
    code = (
        "import os, sys\n"
        "target_src = sys.argv[1]\n"
        "root_src = sys.argv[2]\n"
        "cli_args = sys.argv[3:]\n"
        "sys.meta_path = [\n"
        "    f for f in sys.meta_path\n"
        "    if 'editable' not in type(f).__name__.lower() and 'editable' not in type(f).__module__.lower()\n"
        "]\n"
        "if target_src != root_src:\n"
        "    sys.path = [p for p in sys.path if not (p and os.path.realpath(p) == root_src)]\n"
        "sys.path.insert(0, target_src)\n"
        "import skill_arena\n"
        "assert os.path.realpath(skill_arena.__file__).startswith(os.path.realpath(target_src))\n"
        "from skill_arena.cli import main\n"
        "raise SystemExit(main(cli_args))\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code, str(target_src), root_src_real, *argv],
        cwd=str(tmp_work),
        capture_output=True,
        text=True,
        check=False,
    )
    return proc.returncode, proc.stdout, proc.stderr


def _run_pytest_on_target(test_files: list[str]) -> tuple[int, str]:
    code = (
        "import os, sys\n"
        "target_root = sys.argv[1]\n"
        "target_src = sys.argv[2]\n"
        "root_src = sys.argv[3]\n"
        "files = sys.argv[4:]\n"
        "sys.meta_path = [\n"
        "    f for f in sys.meta_path\n"
        "    if 'editable' not in type(f).__name__.lower() and 'editable' not in type(f).__module__.lower()\n"
        "]\n"
        "if target_src != root_src:\n"
        "    sys.path = [p for p in sys.path if not (p and os.path.realpath(p) == root_src)]\n"
        "sys.path.insert(0, target_src)\n"
        "os.chdir(target_root)\n"
        "import skill_arena\n"
        "assert os.path.realpath(skill_arena.__file__).startswith(os.path.realpath(target_src))\n"
        "import pytest\n"
        "raise SystemExit(pytest.main(['-v', '--tb=short', *files]))\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code, str(target_root), str(target_src), root_src_real, *test_files],
        cwd=str(target_root),
        capture_output=True,
        text=True,
        check=False,
    )
    combined = (proc.stdout or "") + ("\n" if proc.stdout and proc.stderr else "") + (proc.stderr or "")
    return proc.returncode, combined


# Verdict tracking: "PASS" | "FAIL" | "SKIP"
row_results: list[tuple[int, str, str]] = []


def record_row(row_num: int, status: str, detail: str) -> None:
    label = "没跑" if status == "SKIP" else status
    print(f"[Row {row_num}] {label}: {detail}", flush=True)
    row_results.append((row_num, status, detail))


# Shared state between Rows 2/3 and Row 4 when run together
diy_audit_ctx: dict[str, Any] = {"ran": False}


def execute_row_1() -> None:
    test_rels = [
        "tests/test_verifier_replay.py",
        "tests/test_verifier_mutation.py",
        "tests/test_verifier_wording.py",
    ]
    rc, out = _run_pytest_on_target(test_rels)
    required_tests = [
        "test_clean_clone_ignores_untracked_secrets_and_preserves_dot_git",
        "test_bilingual_readme_parity_consistent_passes_and_ignores_subdir_readmes",
        "test_bilingual_readme_parity_mismatch_fails",
        "test_readme_replay_blocks_external_network_allows_loopback_and_reports_offline_replay",
        "test_readme_unverifiable_categories_and_non_command_blocks",
        "test_readonly_files_and_dirs_in_temp_copy_are_cleaned_up",
        "test_leaked_background_process_reaped_and_marked_failed",
        "test_zero_tests_ran_fails_verification",
        "test_python_ast_mutation_killed_and_restored_clean_worktree",
        "test_mutation_survived_on_assert_true_repo_fails_verify",
        "test_syntax_import_compile_and_collection_errors_do_not_count_as_killed",
    ]
    if rc != 0:
        failed_lines = [ln.strip() for ln in out.splitlines() if ln.strip().startswith("FAILED ")]
        summary = "; ".join(failed_lines[:4]) if failed_lines else f"pytest exited {rc}"
        record_row(1, "FAIL", f"验收器单元测试失败（exit={rc}）：{summary}")
        return
    missing = [t for t in required_tests if not re.search(rf"{re.escape(t)}\s+PASSED", out)]
    if missing:
        record_row(1, "FAIL", f"缺少必测用例通过记录：{', '.join(missing)}")
        return
    m_pass = re.findall(r"(\d+)\s+passed", out)
    m_skip = re.findall(r"(\d+)\s+skipped", out)
    passed_n = int(m_pass[-1]) if m_pass else 0
    skipped_n = int(m_skip[-1]) if m_skip else 0
    if skipped_n != 0 or passed_n < len(required_tests):
        record_row(1, "FAIL", f"存在跳过或数量异常（passed={passed_n}, skipped={skipped_n}）")
        return
    record_row(
        1,
        "PASS",
        f"验收器单元测试 {passed_n} passed, 0 failed, 0 skipped（覆盖双语 README 一致/不一致、断网离线、无法验证分类、未跟踪 .env 隔离、只读清理、残留进程、0 测试拦截、变异被杀/存活、语法错误不算杀死）",
    )


def execute_rows_2_and_3(*, want_row2: bool, want_row3: bool) -> None:
    diy_repo = _discover_diy_harness_repo()
    if not _is_git_repo(diy_repo):
        if want_row2:
            record_row(2, "SKIP", "未找到被测仓库 <USER_HOME>/Projects/diy-harness")
        if want_row3:
            record_row(3, "SKIP", "未找到被测仓库 <USER_HOME>/Projects/diy-harness（跳过同仓两次比对）")
        return

    diy_repo_real = diy_repo.resolve()
    pre_fp = repo_fingerprint(diy_repo_real)
    pre_tmp = _list_sa_verify_tmp_dirs()
    pre_home = _snapshot_real_home_two_levels()
    pre_uv = snapshot_uv_cache_entries()

    run_dir = tmp_work / "diy_runs"
    run_dir.mkdir(parents=True, exist_ok=True)
    r1_json = run_dir / "diy_run1.json"
    r1_md = run_dir / "diy_run1.md"
    rc1, out1, err1 = _run_cli_in_subproc(
        [
            "--lang",
            "zh",
            "verify",
            "--repo",
            str(diy_repo_real),
            "--json-out",
            str(r1_json),
            "--md-out",
            str(r1_md),
        ]
    )

    r2_json = run_dir / "diy_run2.json"
    r2_md = run_dir / "diy_run2.md"
    rc2: int | None = None
    if want_row3:
        rc2, _out2, _err2 = _run_cli_in_subproc(
            [
                "--lang",
                "zh",
                "verify",
                "--repo",
                str(diy_repo_real),
                "--json-out",
                str(r2_json),
                "--md-out",
                str(r2_md),
            ]
        )

    post_fp = repo_fingerprint(diy_repo_real)
    post_tmp = _list_sa_verify_tmp_dirs()
    post_home = _snapshot_real_home_two_levels()
    post_uv = snapshot_uv_cache_entries()
    post_leaks = _scan_leaked_verify_processes()

    diy_audit_ctx.update(
        {
            "ran": True,
            "pre_fp": pre_fp,
            "post_fp": post_fp,
            "new_tmp_dirs": sorted(post_tmp - pre_tmp),
            "home_added": sorted(post_home - pre_home),
            "uv_added_count": len(post_uv - pre_uv),
            "leaked_procs": post_leaks,
            "r1_json": r1_json,
        }
    )

    if want_row2:
        if not r1_json.is_file():
            record_row(2, "FAIL", f"diy-harness 运行未产出 JSON（exit={rc1}, err={err1.strip()[:160]}）")
        else:
            d1 = json.loads(r1_json.read_text(encoding="utf-8"))
            b_ok = d1["build_and_test"]["build"]["status"] == "PASS"
            t_ok = d1["build_and_test"]["test"]["status"] == "PASS"
            rr = d1["readme_replay"]
            counts = rr["summary_counts"]
            items = rr["items"]
            failed_items = [it for it in items if it["status"] == "FAIL"]
            failed_cmds = {it["command"] for it in failed_items}
            expected_failed_cmds = {
                "uv run python scripts/sync_prompts.py --check",
                "bash scripts/ci_sim.sh",
            }
            editable_items = [
                it for it in items if it["command"] == "uv tool install --editable ."
            ]
            editable_offline_ok = bool(editable_items) and all(
                it["status"] == "UNVERIFIABLE" and it["category"] == "UNVERIFIABLE_OFFLINE"
                for it in editable_items
            )
            reasons_present = all(bool(it.get("reason")) for it in failed_items)
            mut = d1["mutation"]
            mut_ok = (
                mut["status"] == "PASS"
                and int(mut["killed_count"]) >= 1
                and int(mut["survived_count"]) == 0
            )
            only_two_failures = len(d1["failures"]) == 2 and failed_cmds == expected_failed_cmds
            root_readme_only = rr["discovered_readmes"] == ["README.md"]

            if (
                rc1 == 1
                and b_ok
                and t_ok
                and root_readme_only
                and counts["executed_pass"] >= 11
                and only_two_failures
                and reasons_present
                and editable_offline_ok
                and mut_ok
            ):
                mut_item = mut["items"][0]
                record_row(
                    2,
                    "PASS",
                    f"diy-harness @ {d1['commit_sha'][:12]} 构建与测试 PASS，根 README 共 {counts['total_commands']} 条（通过 {counts['executed_pass']} / 失败 {counts['executed_fail']} 仅 sync_prompts.py 与 ci_sim.sh 且写明原因 / 无法验证 {counts['unverifiable']} 含 uv tool install --editable . 标离线），变异杀死 {mut['killed_count']} 处（{mut_item['file']}:L{mut_item['line_no']}），无其他失败",
                )
            else:
                record_row(
                    2,
                    "FAIL",
                    f"diy-harness 结果不符期望（rc={rc1}, build={b_ok}, test={t_ok}, failed_cmds={sorted(failed_cmds)}, editable_offline={editable_offline_ok}, mut={mut.get('status')}, failures={len(d1['failures'])}）",
                )

    if want_row3:
        row3_res = evaluate_row3_runs(
            rc1=rc1,
            rc2=rc2,
            run1_json=r1_json,
            run2_json=r2_json,
            run1_md=r1_md,
            run2_md=r2_md,
            evidence_dir=explicit_evidence_dir,
        )
        if row3_res.passed:
            record_row(3, "PASS", row3_res.summary_message)
        else:
            record_row(3, "FAIL", row3_res.summary_message)
            for diff_line in row3_res.diff_lines:
                print(diff_line, flush=True)


def execute_row_4() -> None:
    issues: list[str] = []
    evidence_parts: list[str] = []

    # Part A1: If diy-harness ran in Rows 2-3, inspect its pre/post snapshots
    if diy_audit_ctx.get("ran"):
        if diy_audit_ctx["pre_fp"] != diy_audit_ctx["post_fp"]:
            issues.append(
                f"diy-harness 源仓指纹变化 ({diy_audit_ctx['pre_fp'].short_digest()} != {diy_audit_ctx['post_fp'].short_digest()})"
            )
        if diy_audit_ctx["new_tmp_dirs"]:
            issues.append(f"残留临时目录: {diy_audit_ctx['new_tmp_dirs']}")
        if diy_audit_ctx["leaked_procs"]:
            issues.append(f"残留子进程: {diy_audit_ctx['leaked_procs']}")
        forb, unrel = _classify_home_diff(diy_audit_ctx["home_added"])
        if forb:
            issues.append(f"真实 HOME 新增被测相关路径: {forb}")
        r1_data = json.loads(diy_audit_ctx["r1_json"].read_text(encoding="utf-8"))
        eg1 = r1_data.get("env_guard", {})
        if not (eg1.get("source_repo_untouched") and eg1.get("temp_copy_deleted") and eg1.get("no_leaked_processes")):
            issues.append(f"diy-harness env_guard 异常: {eg1}")
        unrel_note = "; ".join(unrel[:5]) if unrel else "无"
        evidence_parts.append(
            f"diy-harness 源仓指纹一致（{diy_audit_ctx['pre_fp'].short_digest()}）、临时目录已删、残留进程 0、~/.cache/uv 新增 {diy_audit_ctx['uv_added_count']} 项、真实 HOME 与 ~/Library 无被测新文件（同机无关项 {len(unrel)} 个：{unrel_note}）"
        )
    else:
        # Standalone Row 4 run: verify clean untampered synthetic repo isolation + HOME snapshot
        clean_probe = tmp_work / "row4_clean_probe"
        _init_git_repo(
            clean_probe,
            {
                "pytest.ini": "[pytest]\ntestpaths = tests\n",
                "calc.py": "def inc(x: int) -> int:\n    return x + 1\n",
                "tests/test_calc.py": "from calc import inc\n\ndef test_inc() -> None:\n    assert inc(2) == 3\n",
                "README.md": "# Probe\n\n```bash\npython3 -c \"import calc; assert calc.inc(1) == 2\"\n```\n",
            },
        )
        pre_fp_c = repo_fingerprint(clean_probe)
        pre_tmp_c = _list_sa_verify_tmp_dirs()
        pre_home_c = _snapshot_real_home_two_levels()
        pre_uv_c = snapshot_uv_cache_entries()
        c_json = tmp_work / "row4_clean_probe.json"
        rc_c, _out_c, err_c = _run_cli_in_subproc(
            ["--lang", "zh", "verify", "--repo", str(clean_probe), "--json-out", str(c_json)]
        )
        post_fp_c = repo_fingerprint(clean_probe)
        post_tmp_c = _list_sa_verify_tmp_dirs()
        post_home_c = _snapshot_real_home_two_levels()
        post_uv_c = snapshot_uv_cache_entries()
        post_leaks_c = _scan_leaked_verify_processes()

        if rc_c != 0 or not c_json.is_file():
            issues.append(f"干净探针仓运行失败 (rc={rc_c}, err={err_c.strip()[:120]})")
        else:
            cd = json.loads(c_json.read_text(encoding="utf-8"))
            eg_c = cd.get("env_guard", {})
            if not (eg_c.get("source_repo_untouched") and eg_c.get("temp_copy_deleted") and eg_c.get("no_leaked_processes")):
                issues.append(f"干净探针仓 env_guard 异常: {eg_c}")
        if pre_fp_c != post_fp_c:
            issues.append("干净探针仓前后指纹不一致")
        if sorted(post_tmp_c - pre_tmp_c):
            issues.append(f"残留临时目录: {sorted(post_tmp_c - pre_tmp_c)}")
        if post_leaks_c:
            issues.append(f"残留子进程: {post_leaks_c}")
        forb_c, unrel_c = _classify_home_diff(sorted(post_home_c - pre_home_c))
        if forb_c:
            issues.append(f"真实 HOME 新增被测相关路径: {forb_c}")
        unrel_note = "; ".join(unrel_c[:5]) if unrel_c else "无"
        evidence_parts.append(
            f"探针仓指纹一致、临时目录已删、残留进程 0、~/.cache/uv 新增 {len(post_uv_c - pre_uv_c)} 项、真实 HOME 与 ~/Library 无被测新文件（同机无关项 {len(unrel_c)} 个：{unrel_note}）"
        )

    # Part B: Active mid-flight tampering check (proves fingerprint verification actively catches
    # tracked working-tree edits & untracked additions outside HEAD during verify)
    tamper_repo = tmp_work / "row4_tamper_probe"
    shutil.rmtree(tamper_repo, ignore_errors=True)
    orig_head = _init_git_repo(
        tamper_repo,
        {
            "pytest.ini": "[pytest]\ntestpaths = tests\n",
            "app.py": "def val() -> int:\n    return 7\n",
            "tests/test_app.py": "from app import val\n\ndef test_val() -> None:\n    assert val() == 7\n",
            "README.md": "# Tamper Probe\n\n```bash\npython3 -c \"import app; assert app.val() == 7\"\n```\n",
        },
    )
    ready_flag = tmp_work / "row4_tamper_ready.flag"
    done_flag = tmp_work / "row4_tamper_done.flag"
    ready_flag.unlink(missing_ok=True)
    done_flag.unlink(missing_ok=True)
    tamper_json = tmp_work / "row4_tamper_out.json"

    build_hook = (
        f"python3 -c \"from pathlib import Path; import time; "
        f"Path({str(ready_flag)!r}).write_text('1'); "
        f"deadline = time.monotonic() + 15.0\n"
        f"while not Path({str(done_flag)!r}).exists() and time.monotonic() < deadline: time.sleep(0.05)\""
    )
    tamper_res: dict[str, Any] = {}

    def _worker() -> None:
        rc_t, out_t, err_t = _run_cli_in_subproc(
            [
                "--lang",
                "zh",
                "verify",
                "--repo",
                str(tamper_repo),
                "--no-mutate",
                "--build-cmd",
                build_hook,
                "--json-out",
                str(tamper_json),
            ]
        )
        tamper_res["rc"] = rc_t
        tamper_res["out"] = out_t
        tamper_res["err"] = err_t

    pre_tmp_t = _list_sa_verify_tmp_dirs()
    th = threading.Thread(target=_worker)
    th.start()
    t_deadline = time.monotonic() + 15.0
    while not ready_flag.exists() and th.is_alive() and time.monotonic() < t_deadline:
        time.sleep(0.03)
    # Modify tracked file AND add untracked file without moving HEAD while verify is mid-flight
    (tamper_repo / "app.py").write_text("def val() -> int:\n    return 7  # tampered during verify\n", encoding="utf-8")
    (tamper_repo / "outside_head_untracked.tmp").write_text("untracked\n", encoding="utf-8")
    done_flag.write_text("1", encoding="utf-8")
    th.join(timeout=30.0)
    post_tmp_t = _list_sa_verify_tmp_dirs()
    after_head = subprocess.run(
        ["git", "-C", str(tamper_repo), "--no-optional-locks", "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()

    rc_tamper = tamper_res.get("rc")
    tamper_untouched_field: bool | None = None
    if tamper_json.is_file():
        td = json.loads(tamper_json.read_text(encoding="utf-8"))
        tamper_untouched_field = td.get("env_guard", {}).get("source_repo_untouched")

    if after_head != orig_head:
        issues.append("探针仓 HEAD 不应改变")
    if rc_tamper != 2 or tamper_untouched_field is not False:
        issues.append(
            f"运行期篡改源仓已跟踪文件但指纹核对未拦截（期望 exit=2 且 source_repo_untouched=False，实测 exit={rc_tamper}, source_repo_untouched={tamper_untouched_field}）"
        )
    if sorted(post_tmp_t - pre_tmp_t):
        issues.append(f"篡改探针运行后残留临时目录: {sorted(post_tmp_t - pre_tmp_t)}")
    else:
        evidence_parts.append("运行期修改源仓已跟踪文件（HEAD 不变）探针被指纹核对检出并退出 2")

    if issues:
        record_row(4, "FAIL", "；".join(issues))
    else:
        record_row(4, "PASS", "；".join(evidence_parts))


def execute_row_5() -> None:
    fake_repo = tmp_work / "row5_assert_true_repo"
    shutil.rmtree(fake_repo, ignore_errors=True)
    _init_git_repo(
        fake_repo,
        {
            "pytest.ini": "[pytest]\ntestpaths = tests\npythonpath = src\n",
            "src/pricing.py": (
                "def apply_tax(amount: int, rate_pct: int) -> int:\n"
                "    if amount <= 0:\n"
                "        return 0\n"
                "    return amount + (amount * rate_pct) // 100\n"
            ),
            "tests/test_pricing.py": "def test_fake_always_green() -> None:\n    assert True\n",
            "README.md": "# Fake Tests Repo\n\n```bash\npython3 -c \"print('ok')\"\n```\n",
        },
    )
    out_json = tmp_work / "row5_out.json"
    rc, _out, err = _run_cli_in_subproc(
        ["--lang", "zh", "verify", "--repo", str(fake_repo), "--json-out", str(out_json)]
    )
    if not out_json.is_file():
        record_row(5, "FAIL", f"assert True 假仓库未产出 JSON（exit={rc}, err={err.strip()[:120]}）")
        return
    data = json.loads(out_json.read_text(encoding="utf-8"))
    test_pass = data["build_and_test"]["test"]["status"] == "PASS"
    mut = data["mutation"]
    items = mut.get("items", [])
    survived_ok = (
        rc == 1
        and data["conclusion"] == "FAIL"
        and test_pass
        and mut.get("status") == "FAIL"
        and int(mut.get("survived_count", 0)) >= 1
        and bool(items)
        and items[0]["verdict"] == "SURVIVED"
        and items[0]["restored_tests_passed"] is True
        and items[0]["worktree_clean"] is True
    )
    if survived_ok:
        m0 = items[0]
        record_row(
            5,
            "PASS",
            f"assert True 假仓库基线测试 PASS 但变异存活（{m0['file']}:L{m0['line_no']} {m0['description']} -> SURVIVED），verify 判 FAIL 并退出 1",
        )
    else:
        record_row(
            5,
            "FAIL",
            f"assert True 假仓库结果不符（rc={rc}, test_pass={test_pass}, mutation={mut}）",
        )


def execute_row_6() -> None:
    sr_dir = _discover_self_reports_dir()
    expected_files = [
        "AgentBar-M0.md",
        "AutopsyMac-M0.md",
        "BusLensMac-M0.md",
        "ChronoMac-M0.md",
        "CiteStudio-M0.md",
        "LaunchLens-M0.md",
        "agent-xray-M0.md",
        "skill-arena-M0.md",
    ]
    if not sr_dir.is_dir() or any(not (sr_dir / fn).is_file() for fn in expected_files):
        record_row(6, "SKIP", "未找到 8 份 *-M0.md 自报文件目录 <USER_HOME>/Projects/flagship-acceptance/reports/archive/tools-2026-11/self-reports")
        return

    must_unsupported: dict[str, set[int]] = {
        "skill-arena-M0.md": {3, 18, 85},   # K13/K15 (L3, L18), K12 (L85)
        "BusLensMac-M0.md": {6, 46},        # U9
        "ChronoMac-M0.md": {20, 22},        # T13
        "LaunchLens-M0.md": {47},           # L13
        "AgentBar-M0.md": {107},            # B11
    }
    must_not_unsupported: dict[str, set[int]] = {
        # Rule 1 ("非零"):
        "AutopsyMac-M0.md": {41, 47},
        "LaunchLens-M0.md": {95, 99, 101},
        "agent-xray-M0.md": {35, 63, 81},
        # Rule 2 (headings/lead-ins), Rule 3 (out-of-scope), Rule 4 (quoted deleted copy):
        "skill-arena-M0.md": {16, 64, 66},
        "BusLensMac-M0.md": {23, 81},
        "ChronoMac-M0.md": {69},
        "AgentBar-M0.md": {75, 133},
        "CiteStudio-M0.md": {71},
    }

    sr_out_dir = tmp_work / "row6_sr"
    sr_out_dir.mkdir(parents=True, exist_ok=True)
    failures: list[str] = []
    total_unsupported = 0

    for fname in expected_files:
        fpath = sr_dir / fname
        jpath = sr_out_dir / f"{fname}.json"
        rc, _out, err = _run_cli_in_subproc(
            ["--lang", "zh", "verify", "--self-report", str(fpath), "--json-out", str(jpath)]
        )
        if rc != 1 or not jpath.is_file():
            failures.append(f"{fname}: 期望 exit=1，实测 exit={rc} ({err.strip()[:80]})")
            continue
        data = json.loads(jpath.read_text(encoding="utf-8"))
        items = data["self_report"]["items"]
        unsup_lines = {int(it["line_no"]) for it in items if it["verdict"] == "UNSUPPORTED"}
        total_unsupported += len(unsup_lines)

        req = must_unsupported.get(fname, set())
        missed = sorted(req - unsup_lines)
        if missed:
            failures.append(f"{fname} 漏标必中行: {missed}")

        forb = must_not_unsupported.get(fname, set())
        false_pos = sorted(forb & unsup_lines)
        if false_pos:
            failures.append(f"{fname} 误标去噪对照行: {false_pos}")

    if failures:
        record_row(6, "FAIL", "；".join(failures))
    else:
        record_row(
            6,
            "PASS",
            f"8 份 *-M0.md 自报全部退出 1（共检出 {total_unsupported} 条无证据断言），K12/K13/K15/U9/T13/L13/B11 共 9 处点名句全中，\"非零\"、标题导语、\"不在范围\"、引号旧文案共 17 处对照行零误标",
        )


def execute_row_7() -> None:
    errs: list[str] = []

    # 1. Exit 0: clean repo where build, test, README replay, and mutation kill all pass
    pass_repo = tmp_work / "row7_pass_repo"
    shutil.rmtree(pass_repo, ignore_errors=True)
    _init_git_repo(
        pass_repo,
        {
            "pytest.ini": "[pytest]\ntestpaths = tests\n",
            "adder.py": "def add(a: int, b: int) -> int:\n    return a + b\n",
            "tests/test_adder.py": "from adder import add\n\ndef test_add() -> None:\n    assert add(2, 3) == 5\n",
            "README.md": "# Adder\n\n```bash\npython3 -c \"import adder; assert adder.add(1, 2) == 3\"\n```\n",
        },
    )
    j0 = tmp_work / "row7_0.json"
    rc0, _, _ = _run_cli_in_subproc(["--lang", "zh", "verify", "--repo", str(pass_repo), "--json-out", str(j0)])
    if rc0 != 0:
        errs.append(f"全通过场景期望 exit=0，实测 {rc0}")

    # 2. Exit 1: README replay command fails
    fail_repo = tmp_work / "row7_fail_repo"
    shutil.rmtree(fail_repo, ignore_errors=True)
    _init_git_repo(
        fail_repo,
        {
            "pytest.ini": "[pytest]\ntestpaths = tests\n",
            "adder.py": "def add(a: int, b: int) -> int:\n    return a + b\n",
            "tests/test_adder.py": "from adder import add\n\ndef test_add() -> None:\n    assert add(2, 3) == 5\n",
            "README.md": "# Broken README\n\n```bash\npython3 -c \"import sys; sys.exit(1)\"\n```\n",
        },
    )
    j1 = tmp_work / "row7_1.json"
    rc1, _, _ = _run_cli_in_subproc(
        ["--lang", "zh", "verify", "--repo", str(fail_repo), "--no-mutate", "--json-out", str(j1)]
    )
    if rc1 != 1:
        errs.append(f"验证项失败场景期望 exit=1，实测 {rc1}")

    # 3. Exit 2a: invalid arguments (missing --repo/--self-report, and --timeout-sec 0)
    rc2_missing, _, _ = _run_cli_in_subproc(["--lang", "zh", "verify"])
    rc2_bad_to, _, _ = _run_cli_in_subproc(
        ["--lang", "zh", "verify", "--repo", str(pass_repo), "--timeout-sec", "0"]
    )
    if rc2_missing != 2 or rc2_bad_to != 2:
        errs.append(f"参数错误场景期望 exit=2，实测 missing={rc2_missing}, bad_timeout={rc2_bad_to}")

    # 4. Exit 2b: non-Git directory
    non_git = tmp_work / "row7_not_a_git_repo"
    non_git.mkdir(parents=True, exist_ok=True)
    (non_git / "hello.txt").write_text("plain dir\n", encoding="utf-8")
    rc2_nongit, _, _ = _run_cli_in_subproc(["--lang", "zh", "verify", "--repo", str(non_git)])
    if rc2_nongit != 2:
        errs.append(f"非 Git 目录场景期望 exit=2，实测 {rc2_nongit}")

    # 5. Exit 2c: source repo fingerprint mismatch
    fp_repo = tmp_work / "row7_fp_mismatch"
    shutil.rmtree(fp_repo, ignore_errors=True)
    _init_git_repo(
        fp_repo,
        {
            "pytest.ini": "[pytest]\ntestpaths = tests\n",
            "adder.py": "def add(a: int, b: int) -> int:\n    return a + b\n",
            "tests/test_adder.py": "from adder import add\n\ndef test_add() -> None:\n    assert add(2, 3) == 5\n",
            "README.md": "# FP\n\n```bash\npython3 -c \"print('ok')\"\n```\n",
        },
    )
    r_flag = tmp_work / "row7_ready.flag"
    d_flag = tmp_work / "row7_done.flag"
    r_flag.unlink(missing_ok=True)
    d_flag.unlink(missing_ok=True)
    b_cmd = (
        f"python3 -c \"from pathlib import Path; import time; "
        f"Path({str(r_flag)!r}).write_text('1'); "
        f"deadline = time.monotonic() + 15.0\n"
        f"while not Path({str(d_flag)!r}).exists() and time.monotonic() < deadline: time.sleep(0.05)\""
    )
    box: dict[str, int] = {}

    def _run_fp() -> None:
        rc_fp, _, _ = _run_cli_in_subproc(
            ["--lang", "zh", "verify", "--repo", str(fp_repo), "--no-mutate", "--build-cmd", b_cmd]
        )
        box["rc"] = rc_fp

    t = threading.Thread(target=_run_fp)
    t.start()
    dl = time.monotonic() + 15.0
    while not r_flag.exists() and t.is_alive() and time.monotonic() < dl:
        time.sleep(0.03)
    (fp_repo / "adder.py").write_text("def add(a: int, b: int) -> int:\n    return a + b  # touched\n", encoding="utf-8")
    d_flag.write_text("1", encoding="utf-8")
    t.join(timeout=30.0)
    if box.get("rc") != 2:
        errs.append(f"源仓指纹不符场景期望 exit=2，实测 {box.get('rc')}")

    if errs:
        record_row(7, "FAIL", "；".join(errs))
    else:
        record_row(
            7,
            "PASS",
            "逐个造出并验证退出码 0（全过）、1（验证项失败）、2（参数错误、非 Git 目录、源仓指纹不符）全部符合契约",
        )


def _tracked_status_and_diff_digest(repo: Path) -> tuple[str, str]:
    st = subprocess.run(
        ["git", "-C", str(repo), "--no-optional-locks", "status", "--porcelain", "-uno"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    df = subprocess.run(
        ["git", "-C", str(repo), "--no-optional-locks", "diff", "HEAD"],
        capture_output=True,
        text=False,
        check=True,
    ).stdout
    return st, hashlib.sha256(df).hexdigest()[:16]


def execute_row_8(which: str) -> None:
    selected = [1, 2, 3, 4] if which == "all" else [int(which)]
    sr_dir = _discover_self_reports_dir()
    sr_available = sr_dir.is_dir() and (sr_dir / "LaunchLens-M0.md").is_file()

    pre_root_st, pre_root_diff = _tracked_status_and_diff_digest(root_dir)

    # Prepare clean temporary clone of current repo + working tree
    mut_repo = tmp_work / "row8_mut_repo"
    shutil.rmtree(mut_repo, ignore_errors=True)
    subprocess.run(
        ["git", "clone", "--no-local", "--quiet", str(root_dir), str(mut_repo)],
        check=True,
    )
    for sub in ("src", "tests", "Scripts"):
        shutil.rmtree(mut_repo / sub, ignore_errors=True)
        shutil.copytree(
            root_dir / sub,
            mut_repo / sub,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".pytest_cache"),
        )
    for fn in ("README.md", "README.zh-CN.md", "pyproject.toml"):
        if (root_dir / fn).is_file():
            shutil.copy2(root_dir / fn, mut_repo / fn)
    subprocess.run(["git", "-C", str(mut_repo), "config", "user.name", "MutVerifier"], check=True)
    subprocess.run(["git", "-C", str(mut_repo), "config", "user.email", "mut@example.invalid"], check=True)
    subprocess.run(["git", "-C", str(mut_repo), "config", "commit.gpgsign", "false"], check=True)
    subprocess.run(["git", "-C", str(mut_repo), "add", "-A"], check=True)
    subprocess.run(
        ["git", "-C", str(mut_repo), "commit", "--allow-empty", "-q", "-m", "sync-working-tree"],
        check=True,
    )

    mutation_specs: dict[int, dict[str, Any]] = {
        1: {
            "name": "变异 1（干净拷贝改成 cp -R）",
            "file": "src/skill_arena/verifier/env_guard.py",
            "needle": '["git", "clone", "--no-local", "--quiet", str(src_repo), str(workspace.repo_dir)]',
            "replacement": '["cp", "-R", str(src_repo), str(workspace.repo_dir)]',
            "target_row": 1,
            "expected_marker": "test_clean_clone_ignores_untracked_secrets_and_preserves_dot_git",
        },
        2: {
            "name": "变异 2（去掉源仓库指纹核对）",
            "file": "src/skill_arena/verifier/runner.py",
            "needle": "source_untouched = pre_fp == post_fp",
            "replacement": "source_untouched = True",
            "target_row": 4,
            "expected_marker": "运行期篡改源仓已跟踪文件但指纹核对未拦截",
        },
        3: {
            "name": "变异 3（变异判定把语法错误算成杀死）",
            "file": "src/skill_arena/verifier/mutator.py",
            "needle": '    if _SYNTAX_OR_IMPORT_ERROR_RE.search(combined_output):\n        return (\n            "INVALID",',
            "replacement": '    if _SYNTAX_OR_IMPORT_ERROR_RE.search(combined_output):\n        return (\n            "KILLED",',
            "target_row": 1,
            "expected_marker": "test_syntax_import_compile_and_collection_errors_do_not_count_as_killed",
        },
        4: {
            "name": "变异 4（用语检查去掉\"非零\"去噪规则）",
            "file": "src/skill_arena/verifier/wording_audit.py",
            "needle": '("零", re.compile(r"(?<!非)零(?![散碎点])")),',
            "replacement": '("零", re.compile(r"零")),',
            "target_row": 6,
            "expected_marker": "误标去噪对照行",
        },
    }

    mut_notes: list[str] = []
    mut_failures: list[str] = []
    mut_skipped: list[str] = []

    for m_id in selected:
        spec = mutation_specs[m_id]
        if m_id == 4 and not sr_available:
            msg = f"{spec['name']} -> 没跑（未找到 8 份 M0 自报目录）"
            print(f"  [Mutation {m_id}] 没跑: {msg}", flush=True)
            mut_skipped.append(msg)
            continue

        tfile = mut_repo / spec["file"]
        orig_txt = tfile.read_text(encoding="utf-8")
        if spec["needle"] not in orig_txt:
            mut_failures.append(f"{spec['name']}: 未找到目标代码片段 ({spec['file']})")
            continue

        patched_txt = orig_txt.replace(spec["needle"], spec["replacement"], 1)
        tfile.write_text(patched_txt, encoding="utf-8")
        py_compile.compile(str(tfile), doraise=True)

        child_env = dict(os.environ)
        child_env["SKILL_ARENA_M2_TARGET_ROOT"] = str(mut_repo)
        target_row = spec["target_row"]
        m2_script = mut_repo / "Scripts" / "acceptance" / "m2-acceptance.sh"

        mut_run = subprocess.run(
            ["bash", str(m2_script), "--only-row", str(target_row)],
            cwd=str(mut_repo),
            env=child_env,
            capture_output=True,
            text=True,
            check=False,
        )
        mut_out = (mut_run.stdout or "") + "\n" + (mut_run.stderr or "")

        # Restore cleanly inside mut_repo
        subprocess.run(["git", "-C", str(mut_repo), "checkout", "--", "."], check=True)
        subprocess.run(["git", "-C", str(mut_repo), "clean", "-fd"], capture_output=True, check=True)
        tracked_after = subprocess.run(
            ["git", "-C", str(mut_repo), "--no-optional-locks", "status", "--porcelain", "-uno"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()

        if (
            mut_run.returncode != 1
            or f"[Row {target_row}] FAIL:" not in mut_out
            or spec["expected_marker"] not in mut_out
        ):
            mut_failures.append(
                f"{spec['name']} 未按预期原因令第 {target_row} 行失败（exit={mut_run.returncode}, marker={spec['expected_marker']}, out={mut_out.strip()[:220]}）"
            )
            continue

        if tracked_after != "":
            mut_failures.append(f"{spec['name']} 还原后临时克隆 git status -uno 不干净: {tracked_after}")
            continue

        restore_run = subprocess.run(
            ["bash", str(m2_script), "--only-row", str(target_row)],
            cwd=str(mut_repo),
            env=child_env,
            capture_output=True,
            text=True,
            check=False,
        )
        if restore_run.returncode != 0:
            mut_failures.append(
                f"{spec['name']} 还原源码后第 {target_row} 行未恢复通过（exit={restore_run.returncode}）"
            )
            continue

        fail_line = next(
            (ln.strip() for ln in mut_out.splitlines() if ln.strip().startswith(f"[Row {target_row}] FAIL:")),
            f"[Row {target_row}] FAIL",
        )
        ev = f"{spec['name']} -> 第 {target_row} 行按预期失败（{fail_line}），还原后第 {target_row} 行恢复 PASS 且 git status -uno 干净"
        print(f"  [Mutation {m_id}] PASS: {ev}", flush=True)
        mut_notes.append(f"#{m_id}->Row{target_row} FAIL & restored PASS")

    post_root_st, post_root_diff = _tracked_status_and_diff_digest(root_dir)
    if (post_root_st, post_root_diff) != (pre_root_st, pre_root_diff):
        mut_failures.append("源仓库已跟踪文件状态在自身变异运行前后发生变化")

    if mut_failures:
        record_row(8, "FAIL", "；".join(mut_failures))
    elif mut_skipped:
        record_row(8, "SKIP", "；".join(mut_skipped))
    else:
        root_clean_note = "空（干净）" if post_root_st.strip() == "" else "与跑前完全一致"
        record_row(
            8,
            "PASS",
            f"共验证 {len(mut_notes)} 条自身变异（{', '.join(mut_notes)}），临时克隆还原后 git status -uno 为空，源仓已跟踪状态{root_clean_note}",
        )


def execute_row_9() -> None:
    parent_ev_path = os.environ.get("SKILL_ARENA_M2_PARENT_EVIDENCE_JSON", "").strip()
    if parent_ev_path and Path(parent_ev_path).is_file():
        pev = json.loads(Path(parent_ev_path).read_text(encoding="utf-8"))
        # Parent `Scripts/test.sh milestone 2` already ran pytest, m1-acceptance.sh, and readme-commands.sh;
        # also run m0-acceptance.sh here so milestone 0's acceptance gate is verified live as well.
        m0_proc = subprocess.run(
            ["bash", str(target_root / "Scripts" / "acceptance" / "m0-acceptance.sh")],
            cwd=str(target_root),
            capture_output=True,
            text=True,
            check=False,
        )
        if m0_proc.returncode != 0:
            record_row(9, "FAIL", f"m0-acceptance.sh 失败（exit={m0_proc.returncode}）")
            return
        record_row(
            9,
            "PASS",
            f"README 回放（{pev['readme_summary']}）、milestone 0（单测 {pev['pytest_summary']} + M0 验收 8/8 通过）、milestone 1（{pev['m1_summary']}）全部通过",
        )
        return

    rm_proc = subprocess.run(
        ["bash", str(target_root / "Scripts" / "acceptance" / "readme-commands.sh")],
        cwd=str(target_root),
        capture_output=True,
        text=True,
        check=False,
    )
    if rm_proc.returncode != 0:
        record_row(9, "FAIL", f"readme-commands.sh 失败（exit={rm_proc.returncode}）")
        return

    m0_proc = subprocess.run(
        ["bash", str(target_root / "Scripts" / "test.sh"), "milestone", "0"],
        cwd=str(target_root),
        capture_output=True,
        text=True,
        check=False,
    )
    if m0_proc.returncode != 0:
        record_row(9, "FAIL", f"Scripts/test.sh milestone 0 失败（exit={m0_proc.returncode}）")
        return

    m1_proc = subprocess.run(
        ["bash", str(target_root / "Scripts" / "test.sh"), "milestone", "1"],
        cwd=str(target_root),
        capture_output=True,
        text=True,
        check=False,
    )
    if m1_proc.returncode != 0:
        record_row(9, "FAIL", f"Scripts/test.sh milestone 1 失败（exit={m1_proc.returncode}）")
        return

    m0_line = next(
        (ln.strip() for ln in m0_proc.stdout.splitlines() if "Milestone M0 summary:" in ln),
        "milestone 0 exit 0",
    )
    m1_line = next(
        (ln.strip() for ln in m1_proc.stdout.splitlines() if "Milestone M1 summary:" in ln),
        "milestone 1 exit 0",
    )
    record_row(9, "PASS", f"README 回放、milestone 0（{m0_line}）与 milestone 1（{m1_line}）全部通过")


print("================================================================", flush=True)
print("SkillArena Milestone M2 Acceptance Suite (Repository & Self-Report Verifier)", flush=True)
print("================================================================", flush=True)

if only_row_arg:
    r_num = int(only_row_arg)
    if r_num == 1:
        execute_row_1()
    elif r_num == 2:
        execute_rows_2_and_3(want_row2=True, want_row3=False)
    elif r_num == 3:
        execute_rows_2_and_3(want_row2=False, want_row3=True)
    elif r_num == 4:
        execute_row_4()
    elif r_num == 5:
        execute_row_5()
    elif r_num == 6:
        execute_row_6()
    elif r_num == 7:
        execute_row_7()
    elif r_num == 9:
        execute_row_9()
elif mutation_arg and not all_rows_flag:
    execute_row_8(mutation_arg)
else:
    execute_row_1()
    execute_rows_2_and_3(want_row2=True, want_row3=True)
    execute_row_4()
    execute_row_5()
    execute_row_6()
    execute_row_7()
    if mutation_arg:
        execute_row_8(mutation_arg)
    else:
        print("[Row 8] 默认不跑（传 --mutation <1|2|3|4|all> 执行验收器自身四条变异）", flush=True)
    execute_row_9()

passed_n = sum(1 for _, st, _ in row_results if st == "PASS")
failed_n = sum(1 for _, st, _ in row_results if st == "FAIL")
skipped_n = sum(1 for _, st, _ in row_results if st == "SKIP")

print("================================================================", flush=True)
print(
    f"M2 ACCEPTANCE SUMMARY: {passed_n} passed, {failed_n} failed, {skipped_n} skipped",
    flush=True,
)
if failed_n > 0:
    print("RESULT: M2 ACCEPTANCE FAILED (exit 1)", flush=True)
    print("================================================================", flush=True)
    raise SystemExit(1)
if skipped_n > 0:
    print("RESULT: M2 ACCEPTANCE PARTIAL / 没跑 (no failures, missing external targets -> exit 2)", flush=True)
    print("================================================================", flush=True)
    raise SystemExit(2)
print("RESULT: ALL EXECUTED M2 ACCEPTANCE CHECKS PASSED (exit 0)", flush=True)
print("================================================================", flush=True)
raise SystemExit(0)
PY
}

if [[ "$RECORD" -eq 1 ]]; then
  mkdir -p "${ROOT_DIR}/reports"
  RAW_OUT="${TMP_WORK}/raw_m2_acceptance.txt"
  set +e
  run_m2_driver 2>&1 | tee "$RAW_OUT"
  DRIVER_EC=${PIPESTATUS[0]}
  set -e
  scrub_report_file "$RAW_OUT" "$REPORT_FILE"
  exit "$DRIVER_EC"
else
  run_m2_driver
fi
