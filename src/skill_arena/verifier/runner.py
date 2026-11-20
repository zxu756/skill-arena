"""End-to-end verification runner coordinating clean clone, build/test, README replay, cleanup, and reporting."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
import time
from typing import Any

from skill_arena.verifier.builder import StepExecutionRecord, execute_build_and_test
from skill_arena.verifier.env_guard import (
    RepoFingerprint,
    VerifyInputError,
    VerifyWorkspace,
    build_isolated_verify_env,
    cleanup_temp_workspace,
    clone_clean_repo,
    create_verify_workspace,
    repo_fingerprint,
    validate_git_repo_and_resolve_ref,
)
from skill_arena.verifier.readme_replay import ReadmeReplaySummary, replay_root_readmes
from skill_arena.verifier.report import PathScrubber, VerifyReport


@dataclass(slots=True)
class VerifyOptions:
    """Configuration options for `skill-arena verify`."""

    repo: Path | None = None
    ref: str = "HEAD"
    build_cmd: str | None = None
    test_cmd: str | None = None
    mutate: bool = True
    max_mutations: int = 1
    self_report: Path | None = None
    skip_cmd: list[str] = field(default_factory=list)
    timeout_sec: int = 300
    lang: str = "zh"


def _scrub_leaked_records(
    records: list[dict[str, Any]],
    scrubber: PathScrubber,
) -> list[dict[str, Any]]:
    cleaned = [
        {
            "command": scrubber.scrub(str(rec.get("command", ""))),
            "reaped": bool(rec.get("reaped", False)),
        }
        for rec in records
    ]
    return sorted(cleaned, key=lambda item: (item["command"], item["reaped"]))


def _serialize_step(step: StepExecutionRecord, scrubber: PathScrubber) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "name": step.name,
        "command": scrubber.scrub_command(step.command) if step.command else None,
        "status": step.status,
        "exit_code": step.exit_code,
        "timed_out": step.timed_out,
        "leaked_process_count": step.leaked_process_count,
        "reason": scrubber.scrub(step.reason),
        "output_summary": scrubber.scrub(step.output_summary),
    }
    if step.name == "test":
        payload["zero_tests_ran"] = step.zero_tests_ran
    return payload


def run_verify(options: VerifyOptions) -> tuple[int, VerifyReport | None, str | None]:
    """Execute repository verification and return `(exit_code, report_or_none, cli_error_message_or_none)`.

    Exit codes:
      - `0`: all enabled verification gates passed
      - `1`: one or more verification gates failed
      - `2`: invalid arguments, Stage-2 unimplemented flags, non-Git target, or source fingerprint mismatch
    """
    is_zh = options.lang == "zh"

    if options.timeout_sec <= 0:
        msg = (
            "错误：`--timeout-sec` 必须大于 0。"
            if is_zh
            else "Error: `--timeout-sec` must be greater than 0."
        )
        return 2, None, msg

    if options.max_mutations < 1:
        msg = (
            "错误：`--max-mutations` 必须至少为 1。"
            if is_zh
            else "Error: `--max-mutations` must be at least 1."
        )
        return 2, None, msg

    if options.self_report is not None:
        msg = (
            "错误：自报用语检查（`--self-report`）将在 M2 第 2 段实现；当前第 1 段不支持该参数。"
            if is_zh
            else "Error: self-report wording audit (`--self-report`) is scheduled for M2 Stage 2 (not implemented yet)."
        )
        return 2, None, msg

    if options.repo is None:
        msg = (
            "错误：必须传入 `--repo <路径>`（或在第 2 段支持后传入 `--self-report`）。"
            if is_zh
            else "Error: `--repo <path>` is required."
        )
        return 2, None, msg

    if options.mutate:
        msg = (
            "错误：破坏性变异反证（`--mutate`）将在 M2 第 2 段实现；第 1 段请显式传入 `--no-mutate`。"
            if is_zh
            else "Error: mutation verification (`--mutate`) is scheduled for M2 Stage 2; pass `--no-mutate` in Stage 1."
        )
        return 2, None, msg

    try:
        src_repo, commit_sha = validate_git_repo_and_resolve_ref(options.repo, options.ref)
        pre_fp: RepoFingerprint = repo_fingerprint(src_repo)
    except VerifyInputError as exc:
        return 2, None, f"{'错误' if is_zh else 'Error'}: {exc}"

    started_iso = datetime.now(timezone.utc).isoformat()
    t_start = time.monotonic()

    workspace: VerifyWorkspace = create_verify_workspace()
    scrubber = PathScrubber(
        src_repo=src_repo,
        tmp_base=workspace.tmp_base,
        tmp_repo=workspace.repo_dir,
        tmp_home=workspace.home_dir,
        tmp_dir=workspace.tmp_dir,
    )

    clone_wall_ms = 0
    clone_failed_msg: str | None = None
    build_rec: StepExecutionRecord | None = None
    test_rec: StepExecutionRecord | None = None
    replay_summary: ReadmeReplaySummary | None = None
    all_leaked_raw: list[dict[str, Any]] = []
    all_reaped_pids: list[int] = []
    temp_deleted = False

    try:
        iso_env = build_isolated_verify_env(workspace)
        clone_res = clone_clean_repo(
            src_repo,
            commit_sha,
            workspace,
            iso_env,
            timeout_sec=options.timeout_sec,
        )
        clone_wall_ms = clone_res.wall_ms
        all_leaked_raw.extend(clone_res.leaked_processes)
        all_reaped_pids.extend(clone_res.reaped_pids)

        if clone_res.returncode != 0:
            clone_failed_msg = scrubber.scrub(clone_res.combined_output) or (
                f"git clone --no-local 失败（退出码 {clone_res.returncode}）"
                if is_zh
                else f"git clone --no-local failed (exit {clone_res.returncode})"
            )
        else:
            b_rec, t_rec = execute_build_and_test(
                workspace.repo_dir,
                iso_env,
                build_cmd_override=options.build_cmd,
                test_cmd_override=options.test_cmd,
                timeout_sec=options.timeout_sec,
                lang=options.lang,
            )
            build_rec = b_rec
            test_rec = t_rec
            all_leaked_raw.extend(b_rec.leaked_processes)
            all_leaked_raw.extend(t_rec.leaked_processes)
            all_reaped_pids.extend(b_rec.reaped_pids)
            all_reaped_pids.extend(t_rec.reaped_pids)

            r_sum = replay_root_readmes(
                workspace.repo_dir,
                iso_env,
                skip_patterns=options.skip_cmd,
                timeout_sec=options.timeout_sec,
                lang=options.lang,
            )
            replay_summary = r_sum
            for item in r_sum.items:
                all_leaked_raw.extend(item.leaked_processes)
                all_reaped_pids.extend(item.reaped_pids)
    finally:
        temp_deleted = cleanup_temp_workspace(workspace.tmp_base)

    post_fp: RepoFingerprint = repo_fingerprint(src_repo)
    source_untouched = pre_fp == post_fp
    finished_iso = datetime.now(timezone.utc).isoformat()
    total_wall_ms = int(round((time.monotonic() - t_start) * 1000))

    failures: list[str] = []
    if not source_untouched:
        failures.append(
            f"源仓库只读指纹前后不一致（pre={pre_fp.short_digest()}, post={post_fp.short_digest()}）"
            if is_zh
            else f"Source repository fingerprint changed (pre={pre_fp.short_digest()}, post={post_fp.short_digest()})"
        )
    if not temp_deleted:
        failures.append("临时工作目录未能完全删除" if is_zh else "Temporary workspace was not deleted")
    if clone_failed_msg is not None:
        failures.append(clone_failed_msg)

    if build_rec is None:
        build_rec = StepExecutionRecord(
            name="build",
            command=None,
            status="FAIL",
            exit_code=None,
            reason=clone_failed_msg or ("克隆失败" if is_zh else "Clone failed"),
        )
    elif build_rec.status == "FAIL":
        failures.append(f"build: {scrubber.scrub(build_rec.reason)}")

    if test_rec is None:
        test_rec = StepExecutionRecord(
            name="test",
            command=None,
            status="FAIL",
            exit_code=None,
            reason=clone_failed_msg or ("克隆失败" if is_zh else "Clone failed"),
        )
    elif test_rec.status == "FAIL":
        failures.append(f"test: {scrubber.scrub(test_rec.reason)}")

    if replay_summary is None:
        replay_summary = ReadmeReplaySummary(
            status="FAIL",
            discovered_readmes=[],
            bilingual_parity_status="N/A",
            bilingual_parity_reason="因克隆失败跳过" if is_zh else "Skipped due to clone failure",
            fenced_block_count=0,
            shell_block_count=0,
            non_command_block_count=0,
            skip_cmd_patterns=list(options.skip_cmd),
            exempted_commands=[],
            items=[],
        )
    else:
        if replay_summary.bilingual_parity_status == "FAIL":
            failures.append(scrubber.scrub(replay_summary.bilingual_parity_reason))
        for item in replay_summary.items:
            if item.status == "FAIL":
                failures.append(
                    scrubber.scrub(
                        f"{item.doc_name}:L{item.line_no} `{item.command}` -> {item.reason}"
                    )
                )

    scrubbed_leaks = _scrub_leaked_records(all_leaked_raw, scrubber)
    no_leaked = len(scrubbed_leaks) == 0
    if not no_leaked:
        failures.append(
            f"检测到 {len(scrubbed_leaks)} 个脱离会话的残留进程（均已回收）"
            if is_zh
            else f"Detected {len(scrubbed_leaks)} leaked background process(es) (reaped)"
        )

    env_guard_ok = source_untouched and temp_deleted and no_leaked
    env_guard_dict: dict[str, Any] = {
        "status": "PASS" if env_guard_ok else "FAIL",
        "clone_method": "git clone --no-local",
        "pre_fingerprint": pre_fp.short_digest(),
        "post_fingerprint": post_fp.short_digest(),
        "source_repo_untouched": source_untouched,
        "temp_copy_deleted": temp_deleted,
        "no_leaked_processes": no_leaked,
        "leaked_process_count": len(scrubbed_leaks),
        "leaked_processes": scrubbed_leaks,
    }

    serialized_items: list[dict[str, Any]] = []
    cmd_timings: list[dict[str, Any]] = []
    for item in replay_summary.items:
        serialized_items.append(
            {
                "index": item.index,
                "doc_name": item.doc_name,
                "line_no": item.line_no,
                "section": item.section,
                "command": scrubber.scrub_command(item.command),
                "status": item.status,
                "category": item.category,
                "exit_code": item.exit_code,
                "leaked_process_count": item.leaked_process_count,
                "reason": scrubber.scrub(item.reason),
                "output_summary": scrubber.scrub(item.output_summary),
            }
        )
        cmd_timings.append(
            {
                "index": item.index,
                "doc_name": item.doc_name,
                "line_no": item.line_no,
                "wall_ms": item.wall_ms,
            }
        )

    counts = {
        "total_commands": len(serialized_items),
        "executed_pass": sum(1 for i in serialized_items if i["category"] == "EXECUTED_PASS"),
        "executed_fail": sum(1 for i in serialized_items if i["category"] == "EXECUTED_FAIL"),
        "unverifiable": sum(1 for i in serialized_items if i["status"] == "UNVERIFIABLE"),
        "skipped_user": sum(1 for i in serialized_items if i["category"] == "SKIPPED_USER"),
        "skipped_parity_duplicate": sum(
            1 for i in serialized_items if i["category"] == "SKIPPED_PARITY_DUPLICATE"
        ),
    }

    readme_dict: dict[str, Any] = {
        "status": replay_summary.status,
        "discovered_readmes": replay_summary.discovered_readmes,
        "bilingual_parity": {
            "status": replay_summary.bilingual_parity_status,
            "reason": scrubber.scrub(replay_summary.bilingual_parity_reason),
        },
        "fenced_block_count": replay_summary.fenced_block_count,
        "shell_block_count": replay_summary.shell_block_count,
        "non_command_block_count": replay_summary.non_command_block_count,
        "skip_cmd_patterns": [scrubber.scrub(p, scrub_inline_timings=False) for p in replay_summary.skip_cmd_patterns],
        "exempted_commands": [
            scrubber.scrub(ex, scrub_inline_timings=False) for ex in replay_summary.exempted_commands
        ],
        "summary_counts": counts,
        "items": serialized_items,
    }

    if not source_untouched:
        exit_code = 2
        conclusion = "FAIL"
    elif failures:
        exit_code = 1
        conclusion = "FAIL"
    else:
        exit_code = 0
        conclusion = "PASS"

    report = VerifyReport(
        repo_name=src_repo.name,
        ref=options.ref,
        commit_sha=commit_sha,
        conclusion=conclusion,
        exit_code=exit_code,
        env_guard=env_guard_dict,
        build_and_test={
            "build": _serialize_step(build_rec, scrubber),
            "test": _serialize_step(test_rec, scrubber),
        },
        readme_replay=readme_dict,
        mutation={
            "enabled": False,
            "status": "SKIPPED",
            "max_mutations": options.max_mutations,
            "reason": (
                "已通过 `--no-mutate` 显式关闭（变异反证将在第 2 段实现）"
                if is_zh
                else "Disabled via `--no-mutate` (mutation verification is scheduled for Stage 2)"
            ),
            "items": [],
        },
        self_report={
            "enabled": False,
            "status": "SKIPPED",
            "items": [],
        },
        failures=failures,
        timing_meta={
            "started_at": started_iso,
            "finished_at": finished_iso,
            "total_wall_ms": total_wall_ms,
            "clone_wall_ms": clone_wall_ms,
            "build_wall_ms": build_rec.wall_ms,
            "test_wall_ms": test_rec.wall_ms,
            "replay_wall_ms": replay_summary.wall_ms,
            "command_timings_ms": cmd_timings,
            "reaped_pids": sorted(set(all_reaped_pids)),
        },
    )
    return exit_code, report, None
