"""Unified headless CLI harness driver for Claude Code, Codex, OpenCode, Vibe, Antigravity, and Mock."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import time
import urllib.request
import uuid

from skill_arena.graders.deterministic import evaluate_sandbox, verify_test_runner_available
from skill_arena.models import ContenderConfig, TaskSpec, TrajectoryStep, TrialResult
from skill_arena.runners.wire_tap_proxy import LoopbackWireTapProxy
from skill_arena.sandbox.skill_injector import SkillInjector, SkillMountReport
from skill_arena.sandbox.worktree_pool import BoundedCommandOutcome, SandboxHandle, WorktreeSandboxPool

_REAL_MODEL_OR_SKILL_TOKEN_RE = re.compile(
    r"(?<![A-Za-z0-9])(?:claude|sonnet|opus|haiku|anthropic|gpt|openai|o1|o3|o4|codex|gemini|deepseek|mistral|qwen|llama|grok|"
    r"opencode|vibe|antigravity|agy|hallmark|superpowers)(?![A-Za-z0-9])",
    re.IGNORECASE,
)
_PATH_LIKE_IN_TEXT_RE = re.compile(
    r"(?:^|\s)(?:~/|/(?:Users|home|tmp|var|etc|opt|private)/|\.\.?/)[^\s]+|(?:\.agents|\.claude)/[^\s]*|\b[\w./-]+/SKILL\.md\b",
    re.IGNORECASE,
)


def validate_simulation_inputs(
    *,
    model: str | None = None,
    title: str | None = None,
    label: str | None = None,
    skill_paths: list[str] | None = None,
) -> None:
    """Reject real model names, real vendor/skill names, or skill paths in `--cli simulate` mode."""
    if skill_paths:
        raise ValueError(
            "`--cli simulate` generates synthetic numbers and does not accept `--skill` paths; "
            "use a real or stub agent CLI to evaluate skills. "
            "/ `--cli simulate` 仅生成合成演示数据，不接受 `--skill` 路径。"
        )
    for field_name, val in (("model", model), ("title", title), ("label", label)):
        if not val:
            continue
        m = _REAL_MODEL_OR_SKILL_TOKEN_RE.search(val)
        if m:
            raise ValueError(
                f"`--cli simulate` rejects real model/vendor/skill name '{m.group(0)}' in {field_name}={val!r}; "
                "use generic synthetic labels only. "
                f"/ `--cli simulate` 拒绝在 {field_name} 中使用真实模型或技能名称 '{m.group(0)}'。"
            )
        if _PATH_LIKE_IN_TEXT_RE.search(val):
            raise ValueError(
                f"`--cli simulate` rejects skill or filesystem paths in {field_name}={val!r}. "
                f"/ `--cli simulate` 不允许在 {field_name} 中填入技能或文件路径。"
            )


def resolve_cli_binary(cli_name: str) -> str:
    """Locate vendor CLI binaries across standard PATH and macOS desktop app bundles."""
    found = shutil.which(cli_name)
    if found:
        return found

    fallbacks: dict[str, list[str]] = {
        "codex": [
            "/Applications/Codex.app/Contents/Resources/codex",
            "/Applications/ChatGPT.app/Contents/Resources/codex",
        ],
        "claude": [
            str(Path.home() / ".local/bin/claude"),
            "/Applications/Claude.app/Contents/Resources/claude",
        ],
        "opencode": [
            str(Path.home() / ".opencode/bin/opencode"),
            str(
                Path.home()
                / "Library/Application Support/ai.opencode.desktop/cli/2.2.1-arm64/opencode-cli"
            ),
        ],
        "vibe": [str(Path.home() / ".local/bin/vibe")],
        "agy": [str(Path.home() / ".antigravity/bin/agy"), "/usr/local/bin/agy"],
    }
    for candidate in fallbacks.get(cli_name, []):
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return cli_name


def build_cli_command(contender: ContenderConfig, prompt: str) -> list[str]:
    cli = contender.cli.split(":")[0]
    binary = resolve_cli_binary(cli)
    if cli == "claude":
        cmd = [
            binary,
            "-p",
            prompt,
            "--model",
            contender.model,
            "--output-format",
            "json",
            "--dangerously-skip-permissions",
        ]
    elif cli == "codex":
        cmd = [binary, "exec", "--full-auto", "-m", contender.model, prompt]
    elif cli == "opencode":
        cmd = [binary, "run", "-m", contender.model, prompt]
    elif cli == "vibe":
        cmd = [binary, "-p", prompt, "--output", "json"]
    elif cli == "agy":
        cmd = [binary, "-p", prompt]
    else:
        raise ValueError(f"Unsupported CLI harness: {contender.cli}")
    cmd.extend(contender.extra_args)
    return cmd


class HarnessDriver:
    """Executes one (TaskSpec, ContenderConfig, rep) trial inside an isolated worktree sandbox."""

    def __init__(
        self,
        pool: WorktreeSandboxPool,
        upstream_api_url: str | None = None,
        allow_unisolated_host_home: bool = False,
    ) -> None:
        self.pool = pool
        self.upstream_api_url = upstream_api_url
        self.allow_unisolated_host_home = allow_unisolated_host_home

    def run_trial(
        self,
        experiment_id: str,
        task: TaskSpec,
        contender: ContenderConfig,
        rep_index: int,
    ) -> TrialResult:
        base_cli = contender.cli.split(":", 1)[0]
        if base_cli == "mock":
            raise ValueError(
                "`--cli mock` has been removed and renamed to `--cli simulate` (`--cli mock` 已更名为 `--cli simulate`)."
            )
        is_simulated = base_cli == "simulate"
        if is_simulated:
            validate_simulation_inputs(
                model=contender.model,
                label=contender.label,
                skill_paths=contender.skill_paths,
            )
        elif not (self.allow_unisolated_host_home or contender.allow_unisolated_host_home):
            raise PermissionError(
                "Refusing to launch real/headless agent CLI without `--allow-unisolated-host-home`: "
                "M0 runs the agent in the real user HOME directory with skipped permission prompts "
                "(--dangerously-skip-permissions / --full-auto)."
            )

        verify_test_runner_available(task.test_command)

        trial_id = f"tr_{uuid.uuid4().hex[:10]}"
        slug = f"{task.task_id}__{contender.contender_id}__r{rep_index}"
        started_at = datetime.now(timezone.utc).isoformat()

        handle = self.pool.acquire(task, slug)
        try:
            mount_report = SkillInjector.apply(handle.root, contender)
            exec_outcome: BoundedCommandOutcome | None = None
            if is_simulated:
                usage, steps = self._execute_simulated_contender(handle, task, contender, mount_report, rep_index)
            else:
                usage, steps, exec_outcome = self._execute_real_cli(handle, task, contender, mount_report)

            verdict, git_diff = evaluate_sandbox(
                handle,
                task,
                agent_timed_out=bool(exec_outcome and exec_outcome.timed_out),
                reaped_descendant_pids=list(exec_outcome.reaped_descendant_pids) if exec_outcome else [],
                unreaped_descendant_pids=list(exec_outcome.unreaped_descendant_pids) if exec_outcome else [],
                leaked_processes=list(exec_outcome.leaked_processes) if exec_outcome else [],
            )
            steps.append(
                TrajectoryStep(
                    step_index=len(steps) + 1,
                    timestamp_ms=usage.total_latency_ms + 45,
                    kind="verify",
                    summary=(
                        ("[SIMULATED] " if is_simulated else "")
                        + f"Deterministic Grader -> {'PASS' if verdict.passed else 'FAIL'}"
                    ),
                    detail=verdict.summary,
                    latency_ms=45,
                )
            )
            finished_at = datetime.now(timezone.utc).isoformat()
            return TrialResult(
                trial_id=trial_id,
                experiment_id=experiment_id,
                task_id=task.task_id,
                task_title=task.title,
                task_category=task.category,
                contender_id=contender.contender_id,
                contender_label=contender.label,
                contender_role=contender.role,
                rep_index=rep_index,
                passed=verdict.passed,
                anti_cheat_clean=verdict.anti_cheat_clean,
                usage=usage,
                verdict=verdict,
                steps=steps,
                git_diff=git_diff,
                started_at=started_at,
                finished_at=finished_at,
                is_simulated=is_simulated,
            )
        finally:
            self.pool.release(handle)

    def _execute_real_cli(
        self,
        handle: SandboxHandle,
        task: TaskSpec,
        contender: ContenderConfig,
        mount_report: SkillMountReport,
    ):
        base_cli = contender.cli.split(":", 1)[0]
        upstream = self.upstream_api_url
        if upstream is None:
            if base_cli == "claude":
                upstream = os.environ.get("ANTHROPIC_BASE_URL", "https://api.anthropic.com")
            elif base_cli == "codex":
                upstream = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com")

        proxy = LoopbackWireTapProxy(upstream_base_url=upstream, default_model=contender.model)
        proxy_url = proxy.start()
        t0 = time.perf_counter()
        try:
            env = dict(mount_report.isolated_env_vars)
            if base_cli == "claude" and upstream:
                env["ANTHROPIC_BASE_URL"] = proxy_url
            elif base_cli == "codex" and upstream:
                env["OPENAI_BASE_URL"] = f"{proxy_url}/v1"

            cmd = build_cli_command(contender, task.prompt)
            outcome = self.pool.run_bounded_command_detailed(
                cmd,
                cwd=handle.root,
                env=env,
                timeout_sec=task.timeout_sec,
                reap_sandbox_cwd=True,
            )
            rc = outcome.returncode
            stdout = outcome.stdout
            stderr = outcome.stderr
            timed_out = outcome.timed_out
            wall_ms = int((time.perf_counter() - t0) * 1000)
            usage, turn_records = proxy.summarize(injected_skill_tokens_est=mount_report.estimated_tokens)
            if usage.total_latency_ms == 0:
                usage.total_latency_ms = wall_ms

            steps: list[TrajectoryStep] = []
            if mount_report.mounted_skill_names:
                steps.append(
                    TrajectoryStep(
                        step_index=1,
                        timestamp_ms=10,
                        kind="skill_mount",
                        summary=f"Mounted skills: {', '.join(mount_report.mounted_skill_names)}",
                        detail=f"Injected {mount_report.total_injected_chars} chars (~{mount_report.estimated_tokens} tokens) into workspace.",
                    )
                )
            for rec in turn_records:
                steps.append(
                    TrajectoryStep(
                        step_index=len(steps) + 1,
                        timestamp_ms=rec.latency_ms,
                        kind="tool_call",
                        summary=f"API Turn #{rec.turn_index} ({rec.model})",
                        detail=f"Endpoint {rec.endpoint} | prompt={rec.prompt_tokens} (cached={rec.cached_tokens}), out={rec.completion_tokens}",
                        prompt_tokens=rec.prompt_tokens or 0,
                        cached_tokens=rec.cached_tokens or 0,
                        completion_tokens=rec.completion_tokens or 0,
                        latency_ms=rec.latency_ms,
                        cost_usd=rec.cost_usd or 0.0,
                    )
                )
            tail = (stdout or stderr or "").strip()[-900:]
            reap_suffix = ""
            if outcome.reaped_descendant_pids:
                reap_suffix = f" [reaped detached PIDs: {outcome.reaped_descendant_pids}]"
            if outcome.unreaped_descendant_pids:
                reap_suffix += f" [UNREAPED orphan PIDs: {outcome.unreaped_descendant_pids}]"
            steps.append(
                TrajectoryStep(
                    step_index=len(steps) + 1,
                    timestamp_ms=wall_ms,
                    kind="thought",
                    summary=f"CLI exited with code {rc}" + (" (TIMED OUT)" if timed_out else "") + reap_suffix,
                    detail=tail or "Completed headless execution.",
                    latency_ms=wall_ms,
                )
            )
            return usage, steps, outcome
        finally:
            proxy.stop()

    def _execute_simulated_contender(
        self,
        handle: SandboxHandle,
        task: TaskSpec,
        contender: ContenderConfig,
        mount_report: SkillMountReport,
        rep_index: int,
    ):
        """Deterministic simulator that drives real HTTP calls through LoopbackWireTapProxy and edits sandbox files."""
        mode = contender.cli.split(":", 1)[1] if ":" in contender.cli else (
            "skilled" if (mount_report.mounted_skill_names or contender.role == "treatment") else "baseline"
        )
        seed_int = int(hashlib.sha256(f"{task.task_id}:{contender.contender_id}:{rep_index}".encode()).hexdigest()[:8], 16)
        skill_tax = mount_report.estimated_tokens if mount_report.estimated_tokens > 0 else (640 if mode == "skilled" else 0)

        turn_counter = {"n": 0}

        def responder(path: str, req_body: dict) -> tuple[int, dict]:
            turn_counter["n"] += 1
            n = turn_counter["n"]
            base_in = 1820 + (seed_int % 240) + skill_tax + (n - 1) * 540
            cached = 0 if n == 1 else int(base_in * (0.76 if mode == "skilled" else 0.45))
            out_tok = (310 if mode == "skilled" else 560) + (seed_int % 90)
            return (
                200,
                {
                    "id": f"msg_mock_{n}",
                    "model": contender.model,
                    "usage": {
                        "input_tokens": max(120, base_in - cached),
                        "cache_read_input_tokens": cached,
                        "output_tokens": out_tok,
                    },
                },
            )

        proxy = LoopbackWireTapProxy(default_model=contender.model, synthetic_responder=responder)
        proxy_url = proxy.start()
        try:
            num_turns = 3 if mode == "skilled" else 4
            for turn_i in range(1, num_turns + 1):
                payload = json.dumps({"model": contender.model, "turn": turn_i}).encode("utf-8")
                req = urllib.request.Request(
                    f"{proxy_url}/v1/messages",
                    data=payload,
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=5) as resp:
                    resp.read()

            usage, records = proxy.summarize(injected_skill_tokens_est=skill_tax)
            usage.is_simulated = True
            usage.usage_status = "simulated"
            # Assign deterministic synthetic timing for simulator replay inspection
            synth_latencies = [1420 + (seed_int % 380) for _ in records]
            if mode != "skilled":
                synth_latencies = [2180 + (seed_int % 520) for _ in records]
            usage.ttft_ms = 390 + (seed_int % 140)
            usage.total_latency_ms = sum(synth_latencies)

            steps: list[TrajectoryStep] = []
            elapsed = 0
            if skill_tax > 0:
                skill_names = ", ".join(mount_report.mounted_skill_names) or "synthetic-pack"
                steps.append(
                    TrajectoryStep(
                        step_index=1,
                        timestamp_ms=25,
                        kind="skill_mount",
                        summary=f"[SIMULATED] Loaded synthetic profile ({skill_names})",
                        detail=f"[SIMULATED] Synthetic Turn-1 context delta: +{skill_tax} tokens.",
                        prompt_tokens=skill_tax,
                    )
                )

            if mode == "cheater":
                # Deliberately tamper with protected test file to test Anti-Cheat Integrity!
                for rel_test in task.protected_test_paths[:1]:
                    tpath = handle.root / rel_test
                    tpath.write_text("import unittest\nclass Tampered(unittest.TestCase):\n    def test_ok(self): self.assertTrue(True)\n", encoding="utf-8")
                steps.append(
                    TrajectoryStep(
                        step_index=len(steps) + 1,
                        timestamp_ms=920,
                        kind="file_patch",
                        summary=f"[SIMULATED] Modified protected test {task.protected_test_paths[0]}",
                        detail="Overwrote failing assertions in test suite instead of fixing implementation.",
                        prompt_tokens=int(records[0].prompt_tokens or 0),
                        completion_tokens=int(records[0].completion_tokens or 0),
                        latency_ms=synth_latencies[0],
                        cost_usd=float(records[0].cost_usd or 0.0),
                    )
                )
                return usage, steps

            # Decide whether baseline succeeds on easy tasks or fails on subtle edge cases
            apply_full_solution = mode == "skilled"
            if mode == "baseline":
                # Baseline passes 3 of 8 tasks consistently, and is flaky on 1 task
                easy_ids = {"refac-01-cron-expression-parser", "sec-01-dsn-credential-redactor"}
                flaky_ids = {"refac-02-jsonl-ring-compactor"}
                if task.task_id in easy_ids:
                    apply_full_solution = True
                elif task.task_id in flaky_ids and rep_index == 1:
                    apply_full_solution = True

            file_map = task.reference_solution_files if apply_full_solution else (task.naive_attempt_files or {})
            for rel_path, new_src in file_map.items():
                dest = handle.root / rel_path
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_text(new_src, encoding="utf-8")

            step_Templates = (
                [
                    ("thought", "Analyzed task invariants & SKILL.md guardrails", "Identified exact edge-case contract before editing implementation."),
                    ("tool_call", f"Read workspace & inspected {len(task.seed_files)} files", f"Checked {', '.join(list(task.seed_files.keys())[:2])} and test suite."),
                    ("file_patch", f"Applied targeted patch ({len(file_map)} file)", f"Updated {', '.join(file_map.keys()) or 'workspace'} with minimal diff."),
                ]
                if apply_full_solution
                else [
                    ("thought", "Scanned prompt without specialized skill checklist", "Drafted surface patch without checking concurrency/sloplint edge cases."),
                    ("tool_call", "Inspected source files & ran exploratory command", f"Opened {', '.join(list(task.seed_files.keys())[:2])}."),
                    ("file_patch", "Wrote partial implementation attempt", "Missed strict invariant / left off-grid CSS or unguarded lock path."),
                    ("thought", "Retried after test failure without root-cause fix", "Burned extra completion tokens re-reading stack trace."),
                ]
            )

            for idx, (rec, lat, (kind, summ, det)) in enumerate(zip(records, synth_latencies, step_Templates)):
                elapsed += lat
                steps.append(
                    TrajectoryStep(
                        step_index=len(steps) + 1,
                        timestamp_ms=elapsed,
                        kind=kind,
                        summary=f"[SIMULATED] {summ}",
                        detail=det,
                        prompt_tokens=int(rec.prompt_tokens or 0),
                        cached_tokens=int(rec.cached_tokens or 0),
                        completion_tokens=int(rec.completion_tokens or 0),
                        latency_ms=lat,
                        cost_usd=float(rec.cost_usd or 0.0),
                    )
                )
            return usage, steps
        finally:
            proxy.stop()
